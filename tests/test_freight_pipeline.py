"""Freight (RateScout) workflow tests: classification, extraction, RFQ/quote
intake through the pipeline, lane linking, comparison, margin gate, and the
approval actions. LLM off — deterministic paths only.
"""
from email.message import EmailMessage
from types import SimpleNamespace

from fastapi.testclient import TestClient

from orderinbox.freight.extract import (
    classify_freight_text, extract_quote_from_text, extract_rfq_from_text,
)
from orderinbox.freight.models import FreightCase
from orderinbox.freight.quote import normalize_quote

RFQ_BODY = (
    "Hi,\n\n"
    "We need 2x 40HC from Shanghai to Toronto, non-DG electronics.\n"
    "Cargo is ready Oct 12.\n"
    "We need 14 free days demurrage + 7 detention.\n"
    "Our budget is around USD 2,400 per container.\n\n"
    "Thanks"
)

QUOTE_A = (
    "Quote 234 — Shanghai ABC Logistics\n"
    "USD 1,925 / 40HQ\nPOL: SHA\nPOD: VAN\nincl. BAF / CAF\nexcl. THC both ends\n"
    "DTHC CAD 735\nDOC USD 50\n14 DEM + 7 DET\nPSS subject to vessel\n"
    "valid ETD 12-19 OCT\nsubject to space/equipment\nrail VAN-TOR excluded"
)

QUOTE_B = (
    "Our rate for your RFQ, North Bridge Freight:\n"
    "O/F USD 1,850 / 40HC\nPOL SHA\nPOD VAN\nincl BAF, CAF\nexcl THC\n"
    "THC USD 450\nDOC USD 45\n14 DEM + 7 DET\nvalid ETD Oct 12-19\nsubject to space"
)

ORDER_BODY = "PO 555. Quantity 5 of SAF-401 at unit price $4.00 each, total $20.00."


def _eml(sender, subject, body, path):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Fri, 02 Oct 2026 09:00:00 -0500"
    msg.set_content(body)
    path.write_bytes(msg.as_bytes())
    return path


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def test_classify_rfq_vs_quote_vs_order():
    assert classify_freight_text(RFQ_BODY) == "rfq"
    assert classify_freight_text(QUOTE_A) == "quote"
    assert classify_freight_text(QUOTE_B) == "quote"
    assert classify_freight_text(ORDER_BODY) is None
    assert classify_freight_text("Short") is None


def test_agent_reply_mentioning_rfq_is_a_quote():
    # the word "RFQ" appears, but a stated rate is decisive
    text = ("Quote for your RFQ: O/F USD 1,500 / 20GP, valid ETD Nov 5-12, "
            "subject to space, subject to equipment.")
    assert classify_freight_text(text) == "quote"


# --------------------------------------------------------------------------
# deterministic extraction
# --------------------------------------------------------------------------

def test_quote_extraction_section8_format():
    raw = extract_quote_from_text(QUOTE_A)
    assert raw is not None
    assert raw["base_freight"] == {"amount": 1925.0, "currency": "USD"}
    assert raw["origin_port"] == "SHA"
    assert raw["destination_port"] == "VAN"
    assert raw["free_time"] == {"demurrage_days": 14, "detention_days": 7}
    assert "PSS" in raw["conditional_charges"]
    assert raw["conditions"] == ["space/equipment"]   # not "vessel"
    assert raw["valid_from"].endswith("-12") and raw["valid_to"].endswith("-19")

    q = normalize_quote(raw)
    assert q.origin_port == "CNSHA" and q.destination_port == "CAYVR"
    assert q.equipment == "40HC"                      # 40HQ normalized
    assert "DESTINATION_THC" in q.excluded
    assert "DELIVERY" in q.excluded                   # rail excluded
    assert q.requires_review                          # conditional PSS


def test_rfq_extraction():
    raw = extract_rfq_from_text(RFQ_BODY)
    assert raw is not None
    assert raw["origin_port"] == "CNSHA"              # "from Shanghai" cleaned
    assert raw["destination_port"] == "CATOR"
    assert raw["equipment"] == "40HC" and raw["container_count"] == 2
    assert raw["ready_date"].endswith("-10-12")
    assert raw["free_demurrage_days"] == 14
    assert raw["free_detention_days"] == 7
    assert raw["dangerous_goods"] is False            # "non-DG" is not DG
    assert raw["budget_total"] == 2400.0 and raw["budget_currency"] == "USD"


def test_budget_variants():
    raw = extract_rfq_from_text("Need 1x 20GP Shanghai to Rotterdam. "
                                "Our target price is USD 950 per container.")
    assert raw["budget_total"] == 950.0
    assert extract_rfq_from_text("Need 1x 20GP Shanghai to Rotterdam.").get("budget_total", 0.0) == 0.0


# --------------------------------------------------------------------------
# pipeline routing
# --------------------------------------------------------------------------

def _seed_rfq_and_quotes(env, tmp_path, quote_bodies):
    settings, backend, llm, store, pipeline = env
    p = _eml("logistics@prairieridge.build",
             "RFQ — 40HC Shanghai to Toronto", RFQ_BODY, tmp_path / "rfq.eml")
    rfq = pipeline.process_message(p)
    assert isinstance(rfq, FreightCase) and rfq.kind == "rfq"
    quotes = []
    for i, (sender, body) in enumerate(quote_bodies):
        q = pipeline.process_message(
            _eml(sender, f"quote {i}", body, tmp_path / f"q{i}.eml"))
        assert isinstance(q, FreightCase) and q.kind == "quote"
        quotes.append(q)
    return rfq, quotes


def test_rfq_intake_matches_customer_and_gate(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    rfq = pipeline.process_message(_eml(
        "logistics@prairieridge.build", "RFQ", RFQ_BODY, tmp_path / "rfq.eml"))
    assert isinstance(rfq, FreightCase)
    assert rfq.status.value == "ready"
    assert rfq.rfq.customer_matched.startswith("Prairie")   # sender domain
    assert rfq.lane == "CNSHA -> CATOR"
    assert rfq.rfq.budget_total == 2400.0
    # persisted
    assert any(c.uid == rfq.uid for c in store.list_freight(kind="rfq"))


def test_quote_links_rfq_ranks_and_margins(env, tmp_path):
    store = env[3]
    rfq, quotes = _seed_rfq_and_quotes(env, tmp_path, [
        ("quotes@shanghaiabc.com", QUOTE_A),
        ("sales@northbridge-freight.com", QUOTE_B),
    ])
    a, b = quotes
    # both linked to the RFQ via hub routing (CAYVR serves CATOR)
    assert a.rfq_uid == rfq.uid and b.rfq_uid == rfq.uid
    # all-in cost, not base: B is cheaper on O/F but more all-in
    assert a.buy_cost == 1975.0 and b.buy_cost == 2345.0
    # budget 2400 from the RFQ drives margin + gate
    assert a.status.value == "ready" and a.margin_pct > 10
    assert b.status.value == "exception"
    assert any(i.code == "MARGIN_BELOW_THRESHOLD" for i in b.issues)
    # comparison text names the cheapest counterparty
    assert "Shanghaiabc" in b.recommendation
    # earlier sibling's recommendation was refreshed when B arrived
    a_fresh = next(c for c in store.list_freight(kind="quote") if c.uid == a.uid)
    assert "ranked 2 of 2" in a_fresh.recommendation or "lowest of 2" in a_fresh.recommendation


def test_unlinked_quote_uses_policy_margin(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    q = pipeline.process_message(_eml(
        "rates@somewhere.com", "quote", "USD 1,000 / 20GP POL SHA POD VAN",
        tmp_path / "q.eml"))
    assert isinstance(q, FreightCase)
    # no RFQ link, no budget → recommended sell at policy margin
    assert q.rfq_uid == ""
    assert q.margin_pct == settings.freight_margin_threshold_pct
    assert q.sell_total and q.sell_total > q.buy_cost


def test_order_still_routes_to_order_pipeline(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    r = pipeline.process_message(_eml(
        "ap@prairieridge.build", "PO 555", ORDER_BODY, tmp_path / "o.eml"))
    # routed to the order pipeline, not the freight workflow
    assert not isinstance(r, FreightCase)
    assert r.status.value in ("ready", "exception", "rejected")
    assert store.list_freight() == []


def test_freight_approve_and_reject(env, tmp_path):
    store, pipeline = env[3], env[4]
    rfq, quotes = _seed_rfq_and_quotes(env, tmp_path, [("quotes@shanghaiabc.com", QUOTE_A)])
    pipeline.approve_freight(quotes[0])
    assert quotes[0].status.value == "sent"
    pipeline.approve_freight(rfq)
    assert rfq.status.value == "sent"
    pipeline.reject_freight(quotes[0], "customer moved the ETD")
    assert quotes[0].status.value == "rejected"
    assert quotes[0].error == "customer moved the ETD"
    # persisted
    fresh = next(c for c in store.list_freight(kind="quote") if c.uid == quotes[0].uid)
    assert fresh.status.value == "rejected"


# --------------------------------------------------------------------------
# web console
# --------------------------------------------------------------------------

def test_freight_pages(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    rfq, quotes = _seed_rfq_and_quotes(env, tmp_path, [("quotes@shanghaiabc.com", QUOTE_A)])
    ctx = SimpleNamespace(store=store, pipeline=pipeline, mail_monitor=None)
    from orderinbox.web.app import create_app
    app = create_app(settings, ctx)
    app.state.sessions = {}
    with TestClient(app) as c:
        assert c.get("/freight", follow_redirects=False).status_code == 303
        c.post("/login", data={"password": settings.web_password})
        r = c.get("/freight")
        assert r.status_code == 200
        assert "Freight" in r.text
        detail_id = quotes[0].id
        d = c.get(f"/freight/{detail_id}")
        assert d.status_code == 200
        assert quotes[0].counterparty in d.text
        # approve through the UI
        r = c.post(f"/freight/{rfq.id}/approve", follow_redirects=True)
        assert r.status_code == 200
        assert next(x for x in store.list_freight(kind="rfq")
                    if x.uid == rfq.uid).status.value == "sent"
