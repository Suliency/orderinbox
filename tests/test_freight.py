"""Freight (RateScout) domain: ontology, quote normalization, deterministic
validation, and persistent alias learning.

These tests use the worked example from Section 8 of the proposal (a
Shanghai→Vancouver 40HQ quote) plus targeted edge cases, and assert that the
deterministic layers — not the model — decide whether a quote is safe.
"""
import os

import pytest

from orderinbox.freight import (
    AliasStore,
    FreightContext,
    normalize_quote,
    ontology,
    validate_quote,
)
from orderinbox.freight.quote import margin_pct, total_cost


def _md8_quote() -> dict:
    """The Section 8 worked example, as a raw/messy input dict."""
    return {
        "origin_port": "SHA",
        "destination_port": "VAN",
        "equipment": "40HQ",
        "base_freight": {"amount": 1925, "currency": "USD"},
        "included": ["BAF", "CAF"],
        "excluded": ["ORIGIN_THC", "DESTINATION_THC", "VANCOUVER_TORONTO_RAIL"],
        "charges": [
            {"type": "DTHC", "amount": 735, "currency": "CAD"},
            {"type": "DOC", "amount": 50, "currency": "USD"},
        ],
        "free_time": {"demurrage_days": 14, "detention_days": 7},
        "conditional_charges": ["PSS"],
        "conditions": ["subject to space", "subject to equipment"],
        "valid_from": "2026-10-12",
        "valid_to": "2026-10-19",
        "counterparty": "Shanghai ABC Logistics",
    }


# --------------------------------------------------------------------------
# ontology
# --------------------------------------------------------------------------

def test_charge_alias_normalization():
    assert ontology.normalize_charge_term("DTHC") == "DESTINATION_THC"
    assert ontology.normalize_charge_term("O/F") == "OCEAN_FREIGHT"
    assert ontology.normalize_charge_term("b/l fee") == "BILL_OF_LADING_FEE"
    assert ontology.normalize_charge_term("Origin-THC") == "ORIGIN_THC"
    assert ontology.normalize_charge_term("PCS") == "PORT_CONGESTION_SURCHARGE"
    assert ontology.normalize_charge_term("not a thing") is None


def test_equipment_normalization():
    assert ontology.normalize_equipment("40HQ") == "40HC"
    assert ontology.normalize_equipment("40'") == "40GP"
    assert ontology.normalize_equipment("20GP") == "20GP"
    assert ontology.normalize_equipment("weird") is None


def test_port_lookup():
    assert ontology.lookup_port("Shanghai").un_locode == "CNSHA"
    assert ontology.lookup_port("VAN").un_locode == "CAYVR"
    assert ontology.lookup_port("CNSHA").un_locode == "CNSHA"
    assert ontology.lookup_port("Nowhere") is None


def test_incoterm_normalization():
    assert ontology.normalize_incoterm("free on board") == "FOB"
    assert ontology.normalize_incoterm("CIF") == "CIF"
    assert ontology.normalize_incoterm("nonsense") is None


# --------------------------------------------------------------------------
# normalization (Section 8)
# --------------------------------------------------------------------------

def test_normalize_md8_quote():
    q = normalize_quote(_md8_quote())
    assert q.origin_port == "CNSHA"
    assert q.destination_port == "CAYVR"
    assert q.equipment == "40HC"
    assert q.base_freight.amount == 1925
    assert q.base_freight.currency == "USD"
    assert set(q.included) == {"BAF", "CAF"}
    # "ORIGIN_THC" and "DESTINATION_THC" resolve; the rail note -> DELIVERY
    assert "DESTINATION_THC" in q.excluded
    assert "DELIVERY" in q.excluded
    types = {c.type: c for c in q.charges}
    assert types["DESTINATION_THC"].amount == 735
    assert types["DESTINATION_THC"].currency == "CAD"
    assert types["DOCUMENTATION_FEE"].amount == 50
    assert q.free_time.demurrage_days == 14
    assert q.free_time.detention_days == 7
    assert q.conditional_charges == ["PSS"]
    # Section 9: conditional surcharges require review
    assert q.requires_review is True
    assert q.valid_from == "2026-10-12"
    assert q.valid_to == "2026-10-19"


def test_normalize_is_deterministic():
    a = normalize_quote(_md8_quote()).normalized_dict()
    b = normalize_quote(_md8_quote()).normalized_dict()
    assert a == b


def test_normalize_unknown_terms_preserved_not_dropped():
    q = normalize_quote({"charges": [{"type": "MYSTERY_FEE", "amount": 10}]})
    assert any(c.type == "MYSTERY_FEE" for c in q.charges)


# --------------------------------------------------------------------------
# validation (Section 9)
# --------------------------------------------------------------------------

def test_margin_math():
    q = normalize_quote(_md8_quote())
    # 1925 USD base + 50 USD doc = 1975 USD (the CAD THC is excluded from USD)
    assert total_cost(q, "USD") == 1975.0
    m = margin_pct(q, sell_total=2400, currency="USD")
    assert m is not None and m > 0


def test_margin_below_threshold_is_error():
    q = normalize_quote(_md8_quote())
    ctx = FreightContext(sell_rate=1990, margin_threshold_pct=10)
    issues = validate_quote(q, ctx)
    assert any(i.code == "MARGIN_BELOW_THRESHOLD" for i in issues)


def test_excluded_destination_thc_without_charge_is_error():
    raw = _md8_quote()
    raw["charges"] = [{"type": "DOC", "amount": 50, "currency": "USD"}]
    raw["excluded"] = ["DESTINATION_THC"]
    q = normalize_quote(raw)
    issues = validate_quote(q, FreightContext())
    dt = [i for i in issues if i.code == "EXCLUDED_WITHOUT_CHARGE"
          and "DESTINATION_THC" in i.message]
    assert dt and dt[0].severity.value == "error"


def test_missing_base_freight_is_error():
    q = normalize_quote({"base_freight": {"amount": 0}, "charges": []})
    issues = validate_quote(q, FreightContext())
    assert any(i.code == "MISSING_BASE_FREIGHT" for i in issues)


def test_same_endpoints_is_error():
    q = normalize_quote({"origin_port": "Shanghai", "destination_port": "Shanghai"})
    issues = validate_quote(q, FreightContext())
    assert any(i.code == "ROUTE_SAME_ENDPOINTS" for i in issues)


def test_bad_currency_is_warning():
    q = normalize_quote({"base_freight": {"amount": 100, "currency": "JPY"},
                         "charges": []})
    issues = validate_quote(q, FreightContext(allowed_currencies=["USD", "CAD"]))
    assert any(i.code == "CURRENCY" for i in issues)


def test_inverted_validity_window_is_error():
    q = normalize_quote({"valid_from": "2026-10-19", "valid_to": "2026-10-12"})
    issues = validate_quote(q, FreightContext())
    assert any(i.code == "VALIDITY_WINDOW" for i in issues)


def test_duplicate_quote_flagged():
    raw = _md8_quote()
    ctx = FreightContext()
    q1 = normalize_quote(raw)
    validate_quote(q1, ctx)                      # first time: recorded
    q2 = normalize_quote(raw)
    issues = validate_quote(q2, ctx)              # second time: duplicate
    assert any(i.code == "DUPLICATE_QUOTE" for i in issues)


# --------------------------------------------------------------------------
# alias learning (Section 13)
# --------------------------------------------------------------------------

def test_alias_learn_and_lookup(tmp_path):
    store = AliasStore(tmp_path / "aliases.json")
    store.learn("Shanghai ABC Logistics", "PCS", "PORT_CONGESTION_SURCHARGE",
                evidence="confirmed by operator")
    assert store.resolve("Shanghai ABC Logistics", "PCS") == "PORT_CONGESTION_SURCHARGE"
    assert store.resolve("Other Agent", "PCS") is None
    assert store.conventions_for("Shanghai ABC Logistics") == {
        "PCS": "PORT_CONGESTION_SURCHARGE"}


def test_alias_persists_to_file(tmp_path):
    path = tmp_path / "aliases.json"
    store = AliasStore(path)
    store.learn("Agent X", "PCS", "PORT_CONGESTION_SURCHARGE")
    store.save()
    reloaded = AliasStore(path)
    assert reloaded.resolve("Agent X", "PCS") == "PORT_CONGESTION_SURCHARGE"


def test_alias_reinforcement(tmp_path):
    store = AliasStore(tmp_path / "a.json")
    store.learn("Agent X", "PCS", "PORT_CONGESTION_SURCHARGE")
    store.learn("Agent X", "PCS", "PORT_CONGESTION_SURCHARGE")
    rec = store.lookup("Agent X", "PCS")
    assert rec.times_confirmed == 2


# --------------------------------------------------------------------------
# FX (deterministic money, never the model)
# --------------------------------------------------------------------------

def test_fx_convert_roundtrip():
    from orderinbox.freight import fx
    assert fx.convert(100, "USD", "USD") == 100
    cad = fx.convert(100, "USD", "CAD")
    assert cad > 100
    # converting back recovers the original (within rounding)
    assert fx.convert(cad, "CAD", "USD") == 100


def test_fx_unknown_currency_raises():
    from orderinbox.freight import fx
    with pytest.raises(ValueError):
        fx.rate("XXX")
