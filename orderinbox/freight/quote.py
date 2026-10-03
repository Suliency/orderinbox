"""Freight quote model and normalization (Section 8).

The LLM interprets a messy quote; this module turns that interpretation into a
*canonical*, validated structure the deterministic layers can reason about.

    raw / LLM interpretation          normalized canonical quote
    (messy terms, mixed ends)      ->  (ports, equipment, charges,
                                         free time, conditions)

Normalization is deterministic: given the same input it always produces the
same output, and it leans on the ontology to resolve terminology. This is what
keeps "PCS from Agent A" and "PCS from Agent B" comparable, and what lets the
margin math (Section 9) run on stable identifiers instead of free text.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from . import ontology


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class Money(BaseModel):
    amount: float = 0.0
    currency: str = "USD"


class Charge(BaseModel):
    """One normalized charge line. `type` is a canonical ontology code."""
    type: str = ""                       # canonical, e.g. DESTINATION_THC
    amount: Optional[float] = None
    currency: str = ""
    as_written: str = ""                 # the original term, for display/audit
    confidence: float = 0.0
    # whether this charge is expected to be *covered* by the base freight
    included_in_base: bool = False


class FreeTime(BaseModel):
    demurrage_days: int = 0
    detention_days: int = 0


class FreightQuote(BaseModel):
    """A normalized freight quotation — the canonical object the system trades
    in. Ports are UN/LOCODE, equipment and charges are ontology codes."""
    origin_port: str = ""
    destination_port: str = ""
    equipment: str = ""
    incoterm: str = ""

    base_freight: Money = Field(default_factory=lambda: Money(currency="USD"))

    included: list[str] = Field(default_factory=list)     # canonical codes
    excluded: list[str] = Field(default_factory=list)      # canonical codes
    charges: list[Charge] = Field(default_factory=list)

    free_time: FreeTime = Field(default_factory=FreeTime)
    conditional_charges: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)    # free-text caveats

    valid_from: str = ""            # e.g. "2026-10-12"
    valid_to: str = ""              # e.g. "2026-10-19"
    counterparty: str = ""          # agent / carrier that issued the quote
    confidence: float = 0.0
    requires_review: bool = False

    # ------------------------------------------------------------------
    @property
    def charge_codes(self) -> set[str]:
        return {c.type for c in self.charges if c.type}

    def has_charge(self, code: str) -> bool:
        return code in self.charge_codes or code in self.included

    def charge_amount(self, code: str) -> Optional[float]:
        for c in self.charges:
            if c.type == code and c.amount is not None:
                return c.amount
        return None

    def total_in_currency(self, currency: str) -> float:
        """Sum of base freight + charges denominated in `currency`.

        Multi-currency quotes are not folded here — the rate database /
        currency service owns FX conversion. This only sums like-for-like.
        """
        total = self.base_freight.amount if self.base_freight.currency == currency else 0.0
        for c in self.charges:
            if c.currency == currency and c.amount is not None:
                total += c.amount
        return total

    # ------------------------------------------------------------------
    def normalized_dict(self) -> dict:
        return self.model_dump()


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------

def _norm_list(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def normalize_charge_term(term: str) -> tuple[str, str]:
    """Return (canonical_code, as_written) for a raw charge phrase."""
    term = (term or "").strip()
    if not term:
        return "", ""
    code = ontology.normalize_charge_term(term) or term.upper()
    return code, term


def normalize_included_excluded(included, excluded) -> tuple[list[str], list[str]]:
    """Normalize inclusion/exclusion lists to canonical codes, de-duplicated."""
    inc: list[str] = []
    for term in _norm_list(included):
        code, _ = normalize_charge_term(term)
        if code and code not in inc:
            inc.append(code)
    exc: list[str] = []
    for term in _norm_list(excluded):
        code, _ = normalize_charge_term(term)
        if code and code not in exc:
            exc.append(code)
    return inc, exc


def normalize_charges(raw_charges) -> list[Charge]:
    """Normalize a list of raw charge dicts to canonical Charge objects.

    Accepts entries shaped like {"type": "...", "amount": 735, "currency":
    "CAD"} or the looser {"charge": "...", ...}. Terms are mapped through the
    ontology; unknown terms are kept (upper-cased) so nothing is silently
    dropped and validation can flag them.
    """
    out: list[Charge] = []
    for raw in raw_charges or []:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("type") or raw.get("charge") or raw.get("name") or "")
        code, as_written = normalize_charge_term(term)
        if not code:
            continue
        amount = raw.get("amount")
        try:
            amount = float(amount) if amount not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        out.append(Charge(
            type=code,
            amount=amount,
            currency=str(raw.get("currency") or "").upper(),
            as_written=as_written or term,
            confidence=float(raw.get("confidence") or 0.0),
            included_in_base=bool(raw.get("included_in_base") or False),
        ))
    return out


def normalize_quote(data: dict) -> FreightQuote:
    """Turn a raw / LLM-produced dict into a normalized FreightQuote.

    This is the single normalization entry point. It resolves ports, equipment,
    incoterm, and charges against the ontology and flags conditional surcharges
    for review (Section 9: `if quote.conditional_charges: requires_review`).
    """
    origin = ontology.lookup_port(str(data.get("origin_port") or ""))
    dest = ontology.lookup_port(str(data.get("destination_port") or ""))
    equipment = ontology.normalize_equipment(str(data.get("equipment") or "")) or ""
    incoterm = ontology.normalize_incoterm(str(data.get("incoterm") or "")) or ""

    inc, exc = normalize_included_excluded(data.get("included"), data.get("excluded"))
    charges = normalize_charges(data.get("charges"))

    conditional: list[str] = []
    for c in charges:
        if c.type in ontology.CONDITIONAL_CHARGES and c.type not in conditional:
            conditional.append(c.type)
    for code in data.get("conditional_charges") or []:
        code, _ = normalize_charge_term(str(code))
        if code and code not in conditional:
            conditional.append(code)

    ft = data.get("free_time") or {}
    free_time = FreeTime(
        demurrage_days=int(ft.get("demurrage_days") or 0),
        detention_days=int(ft.get("detention_days") or 0),
    )

    base = data.get("base_freight") or {}
    if isinstance(base, dict):
        base_money = Money(
            amount=float(base.get("amount") or 0.0),
            currency=str(base.get("currency") or "USD").upper(),
        )
    else:
        base_money = Money(amount=float(base or 0.0), currency="USD")

    q = FreightQuote(
        origin_port=origin.un_locode if origin else str(data.get("origin_port") or "").upper(),
        destination_port=dest.un_locode if dest else str(data.get("destination_port") or "").upper(),
        equipment=equipment,
        incoterm=incoterm,
        base_freight=base_money,
        included=inc,
        excluded=exc,
        charges=charges,
        free_time=free_time,
        conditional_charges=conditional,
        conditions=_norm_list(data.get("conditions")),
        valid_from=str(data.get("valid_from") or ""),
        valid_to=str(data.get("valid_to") or ""),
        counterparty=str(data.get("counterparty") or ""),
        confidence=float(data.get("confidence") or 0.0),
    )
    # Section 9: conditional surcharges require review.
    q.requires_review = bool(q.conditional_charges)
    return q


# --------------------------------------------------------------------------
# deterministic margin math (Section 9: "mathematical pricing" is done by
# deterministic code, never by the LLM)
# --------------------------------------------------------------------------

def total_cost(quote: FreightQuote, currency: str = "USD") -> float:
    """All-in buy cost in `currency` (base + charges denominated in it)."""
    return round(quote.total_in_currency(currency), 2)


def margin_pct(quote: FreightQuote, sell_total: float, currency: str = "USD") -> Optional[float]:
    cost = total_cost(quote, currency)
    if cost <= 0:
        return None
    return round((sell_total - cost) / cost * 100.0, 2)


def meets_margin(quote: FreightQuote, sell_total: float, threshold_pct: float,
                currency: str = "USD") -> bool:
    m = margin_pct(quote, sell_total, currency)
    if m is None:
        return False
    return m >= threshold_pct
