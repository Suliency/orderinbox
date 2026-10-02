"""Customer and product matching."""
from orderinbox.match.customers import match_customer, match_customer_by_email
from orderinbox.match.products import match_product
from orderinbox.odoo.mock import MockOdooBackend


def _catalog():
    backend = MockOdooBackend()
    return backend.partners, backend.products


def test_customer_exact():
    partners, _ = _catalog()
    m = match_customer("Northline Building Supply", partners)
    assert m.partner is not None
    assert m.partner.name == "Northline Building Supply"
    assert m.method == "exact"
    assert m.score == 100.0


def test_customer_fuzzy():
    partners, _ = _catalog()
    m = match_customer("northline bldg supply", partners)
    assert m.partner is not None
    assert m.partner.name.startswith("Northline")
    assert m.score >= 55


def test_customer_email_domain():
    partners, _ = _catalog()
    m = match_customer_by_email("ap@prairieridge.build", partners)
    assert m.partner is not None
    assert m.partner.name.startswith("Prairie")


def test_customer_no_match():
    partners, _ = _catalog()
    m = match_customer("Totally Unknown Widgets Inc", partners)
    assert m.score < 80  # weak at best


def test_product_exact():
    _, products = _catalog()
    m = match_product("PLM-103", products)
    assert m.product is not None
    assert m.product.default_code == "PLM-103"
    assert m.method == "exact"


def test_product_alias():
    _, products = _catalog()
    # ROM-142 is an alias of ELC-201
    m = match_product("ROM-142", products)
    assert m.product is not None
    assert m.product.default_code == "ELC-201"
    assert m.method == "alias"


def test_product_case_insensitive_exact():
    _, products = _catalog()
    m = match_product("plm-103", products)
    assert m.product is not None
    assert m.product.default_code == "PLM-103"


def test_product_name_match():
    _, products = _catalog()
    m = match_product("Water Heater 50 gal Gas", products)
    assert m.product is not None
    assert m.product.default_code == "PLM-131"


def test_product_unknown():
    _, products = _catalog()
    m = match_product("ZZZ-999", products)
    assert m.product is None
