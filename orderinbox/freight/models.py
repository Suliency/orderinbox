"""Freight workflow models: RFQ requests and quote cases.

The freight domain is now the primary workflow: a customer's RFQ comes in,
the system prepares agent RFQs, agent quotes come back, are normalized and
compared, a sell rate is computed against the margin policy, and a human
approves the consequential step. This module defines the state of that
workflow.

The design principle is the same as the order pipeline: the AI prepares, the
deterministic rules gate, the human decides. A FreightCase is never "sent"
or "accepted" by itself.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..models import Issue
from .quote import FreightQuote


class FreightStatus(str, Enum):
    RECEIVED = "received"        # stored, not processed
    PARSED = "parsed"            # structured data extracted
    READY = "ready"              # validated; awaiting the next action
    EXCEPTION = "exception"      # needs human review (margin, validation, ambiguity)
    SENT = "sent"                # approved action taken (RFQ sent / quote issued)
    ACCEPTED = "accepted"        # counterparty accepted (customer took the quote)
    REJECTED = "rejected"        # human rejected
    EXPIRED = "expired"          # validity window passed
    FAILED = "failed"            # pipeline error


# Statuses in which an RFQ is still open to receiving quotes.
OPEN_RFK_STATUSES = (FreightStatus.RECEIVED, FreightStatus.PARSED,
                     FreightStatus.READY, FreightStatus.SENT)


class RFQRequest(BaseModel):
    """A customer's request for quote, as understood from the message."""
    customer_name_as_written: str = ""
    customer_matched: str = ""
    customer_match_score: float = 0.0
    origin_port: str = ""            # UN/LOCODE
    destination_port: str = ""       # UN/LOCODE
    equipment: str = ""              # normalized, e.g. 40HC
    container_count: int = 1
    ready_date: str = ""             # ETD / cargo-ready date (ISO)
    free_demurrage_days: int = 0
    free_detention_days: int = 0
    cargo: str = ""                  # commodity / description
    dangerous_goods: bool = False
    incoterm: str = ""
    budget_total: float = 0.0        # customer's stated budget / target price
    budget_currency: str = ""
    notes: str = ""
    confidence: float = 0.0
    extract_method: str = ""         # deterministic | llm | hybrid

    @property
    def lane(self) -> str:
        return f"{self.origin_port} -> {self.destination_port}"


class FreightCase(BaseModel):
    """One freight workflow item: an RFQ or a quote, through its lifecycle.

    For kind="rfq": the `rfq` field holds the request.
    For kind="quote": the `quote` field holds the normalized quote; `rfq_uid`
    links it to the open RFQ it answers (when one is found); pricing fields
    carry the margin computation against the policy.
    """
    id: Optional[int] = None
    uid: str = ""
    kind: str = "rfq"                # rfq | quote
    status: FreightStatus = FreightStatus.RECEIVED
    subject: str = ""
    sender: str = ""
    received_at: Optional[datetime] = None
    stored_path: str = ""
    processed_at: Optional[datetime] = None

    rfq_uid: str = ""                # quote -> the RFQ it answers
    counterparty: str = ""           # quote: agent/carrier name
    lane: str = ""                   # "CNSHA -> CAYVR"
    equipment: str = ""
    container_count: int = 0

    rfq: Optional[RFQRequest] = None
    quote: Optional[FreightQuote] = None

    # pricing (deterministic)
    buy_cost: float = 0.0
    quote_currency: str = ""
    sell_total: Optional[float] = None
    margin_pct: Optional[float] = None
    recommendation: str = ""         # comparison summary for reviewers
    issues: list[Issue] = Field(default_factory=list)
    error: str = ""
    log: list[dict[str, Any]] = Field(default_factory=list)

    def add_log(self, msg: str) -> None:
        from datetime import datetime as _dt, timezone
        self.log.append({"at": _dt.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"), "msg": msg})
