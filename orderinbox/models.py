"""Core data models shared by extraction, matching, validation, and the UI.

Design principle from the business plan: the LLM proposes, deterministic code
disposes. Every field carries a *score* and a *method* so a human reviewer can
see exactly how confident the system is and why.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    RECEIVED = "received"          # email/attachment stored, not processed
    PARSED = "parsed"              # structured data extracted
    READY = "ready"               # validated, awaiting approval
    EXCEPTION = "exception"       # needs human review
    APPROVED = "approved"         # user approved in UI
    SENT = "sent"                 # draft order created in Odoo
    REJECTED = "rejected"         # user rejected / not an order
    FAILED = "failed"             # pipeline error


class Severity(str, Enum):
    ERROR = "error"     # blocks auto-ready; goes to exception queue
    WARNING = "warning" # flagged but may proceed to ready


class MatchMethod(str, Enum):
    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    LLM = "llm"
    NONE = "none"


class OrderLine(BaseModel):
    line_no: int = 0
    # As written in the document
    sku_as_written: str = ""
    description: str = ""
    quantity: Optional[float] = None
    quantity_as_written: str = ""
    unit: str = ""
    unit_price: Optional[float] = None
    currency: str = ""
    remarks: str = ""
    # Resolved against the Odoo catalog
    sku_matched: str = ""
    product_name: str = ""
    match_score: float = 0.0
    match_method: MatchMethod = MatchMethod.NONE
    catalog_price: Optional[float] = None
    line_total: Optional[float] = None

    @property
    def is_matched(self) -> bool:
        return bool(self.sku_matched)


class Extraction(BaseModel):
    """The structured order, exactly as understood from the document."""

    source: str = ""                       # e.g. email subject / filename
    customer_name_as_written: str = ""
    customer_matched: str = ""
    customer_match_score: float = 0.0
    customer_match_method: MatchMethod = MatchMethod.NONE
    po_number: str = ""
    po_date: str = ""                      # ISO date, may be ""
    currency: str = ""
    payment_terms: str = ""
    ship_to: str = ""
    notes: str = ""
    lines: list[OrderLine] = Field(default_factory=list)
    confidence: float = 0.0
    extract_method: str = ""               # deterministic | llm | hybrid
    raw_text_chars: int = 0

    @property
    def total(self) -> float:
        return round(sum(l.line_total or (l.quantity or 0) * (l.unit_price or 0) for l in self.lines), 2)


class Issue(BaseModel):
    severity: Severity = Severity.WARNING
    code: str = ""                 # PACK_MULTIPLE | PRICE_DEV | DUPLICATE_PO | ...
    message: str = ""
    line: Optional[int] = None
    suggested_fix: str = ""


class ProcessedOrder(BaseModel):
    """Persistent state of one inbox document through the pipeline."""

    id: Optional[int] = None
    uid: str = ""                    # stable id (email UID or file hash)
    source_type: str = "email"       # email | file
    subject: str = ""
    sender: str = ""
    received_at: Optional[datetime] = None
    stored_path: str = ""            # spool path of the raw document(s)
    status: OrderStatus = OrderStatus.RECEIVED
    extraction: Optional[Extraction] = None
    issues: list[Issue] = Field(default_factory=list)
    odoo_order_id: Optional[int] = None
    odoo_reference: str = ""
    error: str = ""
    processed_at: Optional[datetime] = None
    # audit trail: timestamped log lines shown in the UI
    log: list[dict[str, Any]] = Field(default_factory=list)

    def add_log(self, msg: str) -> None:
        self.log.append({"at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"), "msg": msg})
