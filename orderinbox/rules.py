"""Deterministic business-rule validation.

The LLM proposes; this module disposes. Every rule is pure, config-driven, and
produces an explicit Issue with a suggested fix so the review queue is
actionable.

Rules implemented (from the business plan's validation stage):
  PACK_MULTIPLE   quantity must be a multiple of the product pack size
  MIN_QTY         quantity must meet the product minimum
  MAX_QTY         quantity must not exceed the product maximum
  PRICE_DEV       unit price deviates from catalog list price beyond tolerance
  CURRENCY        order currency not in the allowed list
  DUPLICATE_PO    same customer + PO number already processed
  MISSING_SKU     line has no confident product match
  WEAK_SKU        line matched below the "auto-ready" confidence bar
  MISSING_CUSTOMER / WEAK_CUSTOMER
  MISSING_QTY     line has no quantity
  LOW_CONFIDENCE  overall extraction confidence below auto-ready bar
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Extraction, Issue, Severity


@dataclass
class RuleContext:
    settings: Settings
    products_by_code: dict[str, object]      # normalized code -> ProductRecord
    seen_po_keys: set[tuple[str, str]]       # (customer_norm, po_number)
    catalog_currency: str = "USD"


def _norm_code(code: str) -> str:
    return (code or "").strip().upper()


def validate(extraction: Extraction, ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    s = ctx.settings

    # --- order-level -----------------------------------------------------
    if extraction.currency and extraction.currency.upper() not in s.currencies():
        issues.append(Issue(
            severity=Severity.WARNING, code="CURRENCY",
            message=f"Order currency {extraction.currency} is not in the allowed list ({', '.join(s.currencies())}).",
            suggested_fix="Confirm the currency with the customer, or add it to ALLOWED_CURRENCIES.",
        ))

    po_key = (extraction.customer_matched.lower(), (extraction.po_number or "").strip().upper())
    if extraction.customer_matched and po_key in ctx.seen_po_keys:
        issues.append(Issue(
            severity=Severity.ERROR, code="DUPLICATE_PO",
            message=f"PO {extraction.po_number} for {extraction.customer_matched} was already processed.",
            suggested_fix="Reject this email or confirm it is a re-send with changes.",
        ))

    if not extraction.customer_matched:
        issues.append(Issue(
            severity=Severity.ERROR, code="MISSING_CUSTOMER",
            message="No matching customer found in Odoo partners.",
            suggested_fix="Pick the correct partner in the review UI.",
        ))
    elif extraction.customer_match_score < s.match_threshold:
        issues.append(Issue(
            severity=Severity.WARNING, code="WEAK_CUSTOMER",
            message=f"Customer match '{extraction.customer_matched}' scored {extraction.customer_match_score:.0f} (below {s.match_threshold:.0f}).",
            suggested_fix="Verify the customer before approving.",
        ))

    if extraction.confidence < s.confidence_auto_ready:
        issues.append(Issue(
            severity=Severity.WARNING, code="LOW_CONFIDENCE",
            message=f"Extraction confidence {extraction.confidence:.0%} is below the auto-ready bar ({s.confidence_auto_ready:.0%}).",
        ))

    # --- line-level ------------------------------------------------------
    for line in extraction.lines:
        if line.quantity is None:
            issues.append(Issue(
                severity=Severity.ERROR, code="MISSING_QTY", line=line.line_no,
                message=f"Line {line.line_no} ({line.sku_as_written or line.description}) has no quantity.",
            ))
            continue

        prod = None
        if line.sku_matched:
            prod = ctx.products_by_code.get(_norm_code(line.sku_matched))
        if prod is None and line.match_score < s.match_threshold:
            sev = Severity.ERROR if line.match_score < s.low_match_threshold else Severity.WARNING
            if line.match_score == 0:
                issues.append(Issue(
                    severity=Severity.ERROR, code="MISSING_SKU", line=line.line_no,
                    message=f"Line {line.line_no}: no catalog product matched '{line.sku_as_written or line.description}'.",
                    suggested_fix="Pick the correct product in the review UI.",
                ))
            else:
                issues.append(Issue(
                    severity=sev, code="WEAK_SKU", line=line.line_no,
                    message=f"Line {line.line_no}: best match '{line.sku_matched or '?'}' scored {line.match_score:.0f} — below auto-ready bar.",
                    suggested_fix="Verify the SKU before approving.",
                ))
        if prod is None:
            continue

        if prod.pack_qty and line.quantity % prod.pack_qty != 0:
            issues.append(Issue(
                severity=Severity.ERROR, code="PACK_MULTIPLE", line=line.line_no,
                message=(f"Line {line.line_no}: quantity {line.quantity:g} is not a multiple of the pack size "
                         f"({prod.pack_qty}) for {prod.default_code}."),
                suggested_fix=f"Round to {int(line.quantity // prod.pack_qty * prod.pack_qty) + prod.pack_qty} "
                              f"(next valid multiple) — confirm with the customer.",
            ))
        if prod.min_qty and line.quantity < prod.min_qty:
            issues.append(Issue(
                severity=Severity.ERROR, code="MIN_QTY", line=line.line_no,
                message=f"Line {line.line_no}: quantity {line.quantity:g} is below the minimum order quantity ({prod.min_qty:g}).",
            ))
        if prod.max_qty and line.quantity > prod.max_qty:
            issues.append(Issue(
                severity=Severity.ERROR, code="MAX_QTY", line=line.line_no,
                message=f"Line {line.line_no}: quantity {line.quantity:g} exceeds the maximum ({prod.max_qty:g}).",
            ))
        if line.unit_price is not None and prod.list_price > 0:
            dev = abs(line.unit_price - prod.list_price) / prod.list_price
            if dev > s.price_deviation_tolerance:
                issues.append(Issue(
                    severity=Severity.WARNING, code="PRICE_DEV", line=line.line_no,
                    message=(f"Line {line.line_no}: unit price {line.unit_price:.2f} deviates {dev:.0%} from catalog "
                             f"price {prod.list_price:.2f} for {prod.default_code}."),
                    suggested_fix="Check negotiated pricing for this customer.",
                ))
    return issues
