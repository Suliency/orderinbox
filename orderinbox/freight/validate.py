"""Deterministic freight validation (Section 9).

"The LLM must not be the final authority for quote safety." The model produces
a normalized quote; this module is the deterministic gate that decides whether
the quote is *safe* to act on. Every rule is pure and config-driven and emits
an `Issue` (the same shape the order pipeline and UI already understand) so a
quote's safety is auditable.

Checks (Section 9's list): required charges, currency, validity, route
consistency, equipment type, inclusion/exclusion, margin threshold, duplicate
quotes, free-time terms, and conditional surcharges.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import Issue, Severity
from . import ontology
from .quote import FreightQuote


@dataclass
class FreightContext:
    """Configuration the freight rules reason over."""
    allowed_currencies: list[str] = field(default_factory=lambda: ["USD", "CAD", "EUR"])
    margin_threshold_pct: float = 10.0          # minimum acceptable margin
    sell_rate: Optional[float] = None           # proposed sell, if known
    seen_quote_keys: set[tuple[str, str, str, str]] = field(default_factory=set)
    # route-specific expected charges, e.g. a US west-coast lane needs drayage
    extra_expected_charges: set[str] = field(default_factory=set)


def _currency_ok(code: str, allowed: list[str]) -> bool:
    return (code or "").upper() in [c.upper() for c in allowed]


def validate_quote(quote: FreightQuote, ctx: FreightContext) -> list[Issue]:
    issues: list[Issue] = []

    # --- route consistency ----------------------------------------------
    if quote.origin_port == quote.destination_port and quote.origin_port:
        issues.append(Issue(
            severity=Severity.ERROR, code="ROUTE_SAME_ENDPOINTS",
            message=f"Origin and destination are both {quote.origin_port}.",
            suggested_fix="Confirm the lane — this looks like a data-entry slip."))
    for label, code in (("origin", quote.origin_port), ("destination", quote.destination_port)):
        if code and ontology.lookup_port(code) is None:
            issues.append(Issue(
                severity=Severity.WARNING, code="ROUTE_UNKNOWN_PORT",
                message=f"{label.capitalize()} port {code} is not in the port registry.",
                suggested_fix="Confirm the UN/LOCODE or add the port to the registry."))

    # --- equipment -------------------------------------------------------
    if quote.equipment and quote.equipment not in ontology.EQUIPMENT_TYPES:
        issues.append(Issue(
            severity=Severity.WARNING, code="EQUIPMENT_UNKNOWN",
            message=f"Equipment '{quote.equipment}' is not a known type.",
            suggested_fix="Confirm the container type."))

    # --- base freight ----------------------------------------------------
    if not quote.base_freight.amount:
        issues.append(Issue(
            severity=Severity.ERROR, code="MISSING_BASE_FREIGHT",
            message="No base freight amount on the quote.",
            suggested_fix="Obtain the base ocean/air rate before pricing."))

    # --- currency --------------------------------------------------------
    if quote.base_freight.currency and not _currency_ok(quote.base_freight.currency,
                                                        ctx.allowed_currencies):
        issues.append(Issue(
            severity=Severity.WARNING, code="CURRENCY",
            message=(f"Base freight currency {quote.base_freight.currency} is not in the "
                     f"allowed list ({', '.join(ctx.allowed_currencies)})."),
            suggested_fix="Confirm the quote currency or add it to the allowlist."))
    for c in quote.charges:
        if c.currency and not _currency_ok(c.currency, ctx.allowed_currencies):
            issues.append(Issue(
                severity=Severity.WARNING, code="CURRENCY",
                message=f"Charge {c.type} is in {c.currency}, not in the allowed list.",
                suggested_fix="Confirm the charge currency."))

    # --- inclusion/exclusion consistency (Section 9) --------------------
    # If a cost-bearing charge is excluded from the base, the quote should
    # still surface it explicitly so the total cost is visible. The proposal's
    # worked rule asserts DESTINATION_THC specifically; other expected charges
    # excluded without an amount are a warning (they may be arranged separately).
    for code in quote.excluded:
        if code in ontology.EXPECTED_CHARGES and not quote.has_charge(code):
            sev = Severity.ERROR if code == "DESTINATION_THC" else Severity.WARNING
            issues.append(Issue(
                severity=sev, code="EXCLUDED_WITHOUT_CHARGE",
                message=(f"{code} is excluded from the base rate but no explicit "
                         f"{code} charge is shown — the cost is not visible."),
                suggested_fix=f"Ask the agent for the {code} amount, or confirm it is covered."))

    # --- completeness ----------------------------------------------------
    # The base freight amount covers the ocean/air freight leg.
    covered_by_base = {"OCEAN_FREIGHT", "AIR_FREIGHT"} if quote.base_freight.amount else set()
    expected = set(ontology.EXPECTED_CHARGES) | set(ctx.extra_expected_charges)
    missing = [c for c in expected
               if not quote.has_charge(c) and c not in covered_by_base]
    if missing:
        issues.append(Issue(
            severity=Severity.WARNING, code="INCOMPLETE_QUOTE",
            message=f"Quote does not account for: {', '.join(sorted(missing))}.",
            suggested_fix="Confirm these charges are genuinely nil or obtain their amounts."))

    # --- free time -------------------------------------------------------
    if not quote.free_time.demurrage_days and not quote.free_time.detention_days:
        issues.append(Issue(
            severity=Severity.WARNING, code="NO_FREE_TIME",
            message="No demurrage/detention free time stated on the quote.",
            suggested_fix="Confirm the free-time terms to avoid surprise charges."))

    # --- conditional surcharges (Section 9) ------------------------------
    if quote.conditional_charges:
        issues.append(Issue(
            severity=Severity.WARNING, code="CONDITIONAL_CHARGES",
            message=(f"Conditional surcharge(s): {', '.join(quote.conditional_charges)} "
                     f"— may or may not apply."),
            suggested_fix="Confirm which conditional surcharges apply to this shipment."))

    # --- duplicate quote -------------------------------------------------
    if quote.counterparty:
        key = (quote.counterparty.lower(), quote.origin_port, quote.destination_port,
               quote.equipment)
        if key in ctx.seen_quote_keys:
            issues.append(Issue(
                severity=Severity.WARNING, code="DUPLICATE_QUOTE",
                message=(f"A quote for this lane/equipment from {quote.counterparty} "
                         f"was already processed."),
                suggested_fix="Confirm whether this is a revised rate."))
        else:
            ctx.seen_quote_keys.add(key)

    # --- margin (Section 9: margin threshold) ----------------------------
    if ctx.sell_rate is not None and quote.base_freight.currency:
        cost = quote.total_in_currency(quote.base_freight.currency)
        if cost > 0:
            margin = (ctx.sell_rate - cost) / cost * 100.0
            if margin < ctx.margin_threshold_pct:
                issues.append(Issue(
                    severity=Severity.ERROR, code="MARGIN_BELOW_THRESHOLD",
                    message=(f"Implied margin {margin:.1f}% is below the "
                             f"{ctx.margin_threshold_pct:.0f}% threshold "
                             f"(cost {cost:.2f} {quote.base_freight.currency}, "
                             f"sell {ctx.sell_rate:.2f})."),
                    suggested_fix="Re-price, renegotiate, or flag for approval — low margin."))

    # --- validity window -------------------------------------------------
    if quote.valid_from and quote.valid_to and quote.valid_from > quote.valid_to:
        issues.append(Issue(
            severity=Severity.ERROR, code="VALIDITY_WINDOW",
            message=(f"Quote validity window is inverted "
                     f"({quote.valid_from} > {quote.valid_to})."),
            suggested_fix="Confirm the valid ETD window."))

    return issues
