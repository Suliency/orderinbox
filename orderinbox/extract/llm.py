"""LLM client: local-first (Ollama) with optional cloud fallback.

Any OpenAI-compatible endpoint works for cloud: OpenAI, OpenRouter, DeepSeek,
vLLM, LM Studio, Together, Groq, ...

`auto` mode: try the local Ollama model first; on failure or timeout, fall
back to the configured cloud endpoint if one is configured. This implements
the plan's "Mode C: local smaller model with optional cloud fallback".
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

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


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ollama_available: Optional[bool] = None

    # ------------------------------------------------------------------
    def available(self) -> bool:
        """True if any backend can be used at all."""
        s = self.settings
        if s.llm_mode == "off":
            return False
        if s.llm_mode in ("auto", "ollama"):
            if self._ollama_available is None:
                self._ollama_available = self._ping_ollama()
            if self._ollama_available:
                return True
        if s.llm_mode in ("auto", "cloud"):
            return bool(s.cloud_base_url and s.cloud_api_key)
        return False

    def _ping_ollama(self) -> bool:
        s = self.settings
        try:
            r = httpx.get(f"{s.ollama_url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def _backends(self) -> list[tuple[str, str, str, str]]:
        """Yield (kind, base_url, model, api_key) in priority order.
        kind is "ollama" or "openai" (any OpenAI-compatible endpoint)."""
        s = self.settings
        out: list[tuple[str, str, str, str]] = []
        if s.llm_mode in ("auto", "ollama") and (self._ollama_available or self._ping_ollama()):
            out.append(("ollama", s.ollama_url.rstrip("/"), s.ollama_model, ""))
        if s.llm_mode in ("auto", "cloud") and s.cloud_base_url and s.cloud_api_key:
            out.append(("openai", s.cloud_base_url.rstrip("/"), s.cloud_model, s.cloud_api_key))
        return out

    # ------------------------------------------------------------------
    def complete(self, system: str, user: str) -> str:
        last_err: Optional[Exception] = None
        for kind, base, model, key in self._backends():
            try:
                log.info("LLM call via %s (%s)", kind, model)
                return self._call(kind, base, model, key, system, user)
            except Exception as exc:
                last_err = exc
                log.warning("LLM backend %s failed: %s", kind, exc)
        raise LLMError(f"all LLM backends failed: {last_err}")

    def _call(self, kind: str, base: str, model: str, key: str, system: str, user: str) -> str:
        if kind == "ollama":
            url = f"{base}/api/chat"
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "stream": False,
                "options": {"temperature": self.settings.llm_temperature},
                "format": "json",
            }
            headers = {}
        else:
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": self.settings.llm_temperature,
                "response_format": {"type": "json_object"},
            }
            headers = {"Authorization": f"Bearer {key}"}
        r = httpx.post(url, json=payload, headers=headers, timeout=self.settings.llm_timeout)
        if r.status_code != 200:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if kind == "ollama":
            return data["message"]["content"]
        return data["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------
    def extract_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.complete(system, user)
        return parse_json_loose(raw)


def parse_json_loose(raw: str) -> dict[str, Any]:
    """Parse JSON that an LLM may have wrapped in fences or commentary."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError(f"could not parse LLM JSON: {raw[:200]}")


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
