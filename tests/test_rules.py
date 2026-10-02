"""Deterministic business rules: pack multiples, min/max qty, price deviation,
currency, duplicate PO.

Seed facts (demo/seed.json): SAF-401 pack=2 min=2 price=10.22; FST-321 min=10;
PLM-103 pack=4 min=2 price=33.54; ELC-220 pack=1 min=1 price=70.55.
"""
from orderinbox.config import Settings
from orderinbox.models import Extraction, OrderLine, Severity
from orderinbox.rules import RuleContext, validate
from orderinbox.odoo.mock import MockOdooBackend


def _ctx():
    settings = Settings()
    backend = MockOdooBackend()
    products_by_code = {p.default_code.upper(): p for p in backend.get_products()}
    return RuleContext(settings=settings, products_by_code=products_by_code,
                       seen_po_keys=set())


def _extraction(**kw):
    base = dict(
        customer_matched="Test Customer", customer_match_score=100.0,
        po_number="12345", currency="USD", confidence=0.95,
    )
    base.update(kw)
    return Extraction(**base)


def test_clean_order_passes():
    # SAF-401: qty 4 is a multiple of pack 2 and >= min 2; price = list price
    e = _extraction(lines=[OrderLine(line_no=1, sku_as_written="SAF-401",
                                     sku_matched="SAF-401", description="Hard Hat",
                                     quantity=4, unit_price=10.22)])
    assert validate(e, _ctx()) == []


def test_pack_multiple_violation():
    # SAF-401 packs of 2 → qty 3 violates
    e = _extraction(lines=[OrderLine(line_no=1, sku_as_written="SAF-401",
                                     sku_matched="SAF-401", description="Hard Hat",
                                     quantity=3, unit_price=10.22)])
    issues = validate(e, _ctx())
    assert any(i.code == "PACK_MULTIPLE" and i.severity == Severity.ERROR for i in issues)


def test_min_qty_violation():
    # FST-321 min order 10 → qty 2 violates
    e = _extraction(lines=[OrderLine(line_no=1, sku_as_written="FST-321",
                                     sku_matched="FST-321", description="Anchor",
                                     quantity=2, unit_price=8.54)])
    issues = validate(e, _ctx())
    assert any(i.code == "MIN_QTY" and i.severity == Severity.ERROR for i in issues)


def test_price_deviation_warning():
    # ELC-220 list 70.55; quoting 141.10 is a 100% deviation
    e = _extraction(lines=[OrderLine(line_no=1, sku_as_written="ELC-220",
                                     sku_matched="ELC-220", description="Breaker",
                                     quantity=1, unit_price=141.10)])
    issues = validate(e, _ctx())
    assert any(i.code == "PRICE_DEV" and i.severity == Severity.WARNING for i in issues)


def test_bad_currency_warning():
    e = _extraction(currency="GBP")
    issues = validate(e, _ctx())
    assert any(i.code == "CURRENCY" for i in issues)


def test_duplicate_po_error():
    ctx = _ctx()
    ctx.seen_po_keys.add(("test customer", "12345"))
    e = _extraction()
    issues = validate(e, ctx)
    assert any(i.code == "DUPLICATE_PO" and i.severity == Severity.ERROR for i in issues)


def test_missing_sku_error():
    e = _extraction(lines=[OrderLine(line_no=1, sku_as_written="NOPE-1",
                                     description="?", quantity=1)])
    issues = validate(e, _ctx())
    assert any(i.code == "MISSING_SKU" for i in issues)


def test_missing_qty_error():
    e = _extraction(lines=[OrderLine(line_no=1, sku_as_written="SAF-401",
                                     sku_matched="SAF-401", description="Hard Hat",
                                     quantity=None)])
    issues = validate(e, _ctx())
    assert any(i.code == "MISSING_QTY" and i.severity == Severity.ERROR for i in issues)
