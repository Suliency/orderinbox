"""LLM client — a thin facade over the model gateway.

The heavy lifting (provider selection, local-first routing, escalation,
producer/verifier) now lives in `orderinbox.ai`. This module keeps the
historical `LLMClient` surface (`available()`, `complete()`,
`extract_json()`) plus the order-extraction prompts, so the parser and the
matchers are unchanged. It also remains the entry point that honours
`LLM_MODE=off` (deterministic-only) for the zero-dependency demo path.

Deployment of the model itself is described in the proposal: a local Strata
server (or Ollama / vLLM) does the volume; a cloud provider is the escalation
target for the ambiguous tail.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..ai import ModelGateway, RouteRequest
from ..ai.providers import LLMError, parse_json_loose  # re-exported for callers
from ..config import Settings

log = logging.getLogger("orderinbox.llm")

SYSTEM_PROMPT = """You are an order-entry assistant for a distributor. You read a
document (customer purchase order, order email, or spreadsheet dump) and return
ONE JSON object with the exact schema below. Do not invent data: if a field is
not present, use null or empty string. Quantities must be numbers (drop
commas). Prices must be numbers. Keep the SKU exactly as printed. If the
document is NOT an order at all, set "is_order" to false and keep all other
fields minimal."""

SCHEMA_HINT = """{
  "is_order": true,
  "customer_name": "string or null",
  "po_number": "string or null",
  "po_date": "YYYY-MM-DD or null",
  "currency": "USD or null",
  "payment_terms": "string or null",
  "ship_to": "string or null",
  "notes": "string or null",
  "lines": [
    {"line_no": 1, "sku": "string or null", "description": "string",
     "quantity": 0.0, "unit": "string or null", "unit_price": 0.0, "remarks": "string or null"}
  ]
}"""


class LLMClient:
    """Facade used by the pipeline. Delegates to the ModelGateway.

    The public contract is unchanged from the original single-backend client:
      - available() -> is any model reachable?
      - complete(system, user) -> str
      - extract_json(system, user) -> dict
    Internally each call is routed local-first through the gateway, and the
    gateway exposes the richer escalation / verifier behaviour the proposal
    asks for (see orderinbox.ai.gateway.ModelGateway).
    """

    def __init__(self, settings: Settings, gateway: Optional[ModelGateway] = None):
        self.settings = settings
        self.gateway = gateway or ModelGateway(settings)

    # ------------------------------------------------------------------
    def available(self) -> bool:
        s = self.settings
        if s.llm_mode == "off":
            return False
        return self.gateway.available()

    def providers(self) -> list[dict]:
        return self.gateway.providers_summary()

    # ------------------------------------------------------------------
    def complete(self, system: str, user: str, *, task: str = "order_extraction",
                 confidentiality: str = "normal", complexity: str = "medium") -> str:
        req = RouteRequest(task=task, confidentiality=confidentiality, complexity=complexity)
        text, _decision = self.gateway.complete(req, system, user)
        return text

    def extract_json(self, system: str, user: str, *, task: str = "order_extraction",
                     confidentiality: str = "normal", complexity: str = "medium") -> dict[str, Any]:
        req = RouteRequest(task=task, confidentiality=confidentiality, complexity=complexity)
        data, _decision = self.gateway.extract_json(req, system, user)
        return data


# ----------------------------------------------------------------------
# order-extraction prompts
# ----------------------------------------------------------------------

ORDER_EXTRACTION_PROMPT = (
    "Extract the order from the document below.\n\n"
    f"Return ONLY the JSON object matching this schema:\n{SCHEMA_HINT}\n\n"
    "Rules:\n"
    "- quantity: number only (e.g. '12' or '12 CS' -> 12, unit='CS')\n"
    "- unit_price: number only (drop $ and commas)\n"
    "- Keep SKU strings exactly as printed in the document.\n"
    "- If the same item appears on multiple lines, keep each line separate.\n"
    "- Include a line for every purchasable item; skip header/total rows.\n\n"
    "DOCUMENT:\n"
)

# Used when the deterministic parser already found the lines: asking only for
# the header keeps the output ~70-110 tokens instead of hundreds-thousands,
# which is what dominates latency on CPU.
HEADER_SCHEMA_HINT = SCHEMA_HINT.split('  "lines"')[0].rstrip().rstrip(",") + "\n}"

HEADER_EXTRACTION_PROMPT = (
    "Extract the order header from the document below. Do NOT list the line items.\n\n"
    f"Return ONLY the JSON object matching this schema:\n{HEADER_SCHEMA_HINT}\n\n"
    "DOCUMENT:\n"
)
