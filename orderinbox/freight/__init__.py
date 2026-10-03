"""RateScout freight domain: ontology, quote normalization, validation, alias
learning.

This is the freight-forwarding face of the product. The model (via the gateway)
interprets quotes; this package owns the canonical concepts, the normalization
to them, the deterministic safety checks, and the learned counterparty
conventions. Deterministic code — not the model — is the final authority on
whether a quote is safe to price.
"""
from . import fx, ontology
from .alias import AliasRecord, AliasStore
from .extract import (
    classify_freight_text,
    extract_freight,
    extract_quote_from_text,
    extract_rfq_from_text,
)
from .models import FreightCase, FreightStatus, OPEN_RFK_STATUSES, RFQRequest
from .quote import (
    Charge,
    FreeTime,
    FreightQuote,
    Money,
    margin_pct,
    meets_margin,
    normalize_charge_term,
    normalize_charges,
    normalize_included_excluded,
    normalize_quote,
    total_cost,
)
from .validate import FreightContext, validate_quote

__all__ = [
    "ontology",
    "AliasRecord", "AliasStore",
    "FreightQuote", "Charge", "FreeTime", "Money",
    "normalize_quote", "normalize_charges", "normalize_charge_term",
    "normalize_included_excluded", "total_cost", "margin_pct", "meets_margin",
    "FreightContext", "validate_quote",
    "FreightCase", "FreightStatus", "RFQRequest", "OPEN_RFK_STATUSES",
    "classify_freight_text", "extract_freight",
    "extract_quote_from_text", "extract_rfq_from_text",
]
