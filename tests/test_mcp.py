"""MCP tool layer: the registry, the deterministic tools, and the HTTP surface.

Verifies the "agent reasons, tools perform business operations" contract:
tools are named, schema-described, callable with a JSON dict, and returned as
JSON. Also checks the /mcp endpoints and their token auth (mirroring /api).
"""
from fastapi.testclient import TestClient

from orderinbox.mcp import Tool, ToolRegistry, build_tools


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def test_registry_registers_and_lists():
    reg = ToolRegistry()
    reg.register(Tool(name="x.y", description="does x",
                      parameters={"type": "object", "properties": {"q": {"type": "string"}}},
                      handler=lambda a: {"echo": a.get("q")}))
    names = reg.names()
    assert "x.y" in names
    schema = reg.get("x.y").schema()
    assert schema["name"] == "x.y"
    assert "q" in schema["parameters"]["properties"]


def test_registry_call_ok_and_error():
    reg = ToolRegistry()
    reg.register(Tool(name="ok", description="", handler=lambda a: {"n": a.get("n")}))
    assert reg.call("ok", {"n": 5})["result"] == {"n": 5}
    assert reg.call("missing")["ok"] is False
    reg.register(Tool(name="boom", description="",
                      handler=lambda a: 1 / 0))
    assert reg.call("boom")["ok"] is False


def test_default_tools_present():
    reg = build_tools()
    expected = {
        "freight.port.search", "freight.customer.search", "freight.rfq.search",
        "freight.rate.search",
        "freight.rate.save", "freight.quote.normalize", "freight.quote.compare",
        "freight.quote.calculate_sell", "freight.email.read", "freight.email.send",
        "freight.tms.create_shipment", "freight.approval.request",
    }
    assert expected.issubset(set(reg.names()))


def test_rfq_search_tool(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    from email.message import EmailMessage
    body = ("Need 2x 40HC from Shanghai to Toronto. Cargo is ready Oct 12. "
            "Our budget is around USD 2,400 per container.")
    msg = EmailMessage()
    msg["From"] = "logistics@prairieridge.build"
    msg["Subject"] = "RFQ"
    msg.set_content(body)
    p = tmp_path / "rfq.eml"
    p.write_bytes(msg.as_bytes())
    pipeline.process_message(p)

    reg = build_tools(store=store)
    all_open = reg.call("freight.rfq.search", {})["result"]
    assert len(all_open) == 1
    assert all_open[0]["lane"] == "CNSHA -> CATOR"
    assert all_open[0]["budget_total"] == 2400.0
    # origin filter matches; wrong destination filters it out
    assert len(reg.call("freight.rfq.search", {"origin": "CNSHA"})["result"]) == 1
    assert reg.call("freight.rfq.search", {"destination": "NLRRT"})["result"] == []


def test_quote_compare_ranks_by_cost():
    reg = build_tools()
    quotes = [
        {"origin_port": "Shanghai", "destination_port": "Vancouver",
         "equipment": "40HC", "base_freight": {"amount": 2000, "currency": "USD"},
         "counterparty": "Agent B"},
        {"origin_port": "Shanghai", "destination_port": "Vancouver",
         "equipment": "40HC", "base_freight": {"amount": 1900, "currency": "USD"},
         "counterparty": "Agent A"},
    ]
    out = reg.call("freight.quote.compare", {"quotes": quotes, "currency": "USD"})
    ranked = out["result"]["ranked"]
    assert ranked[0]["counterparty"] == "Agent A"
    assert ranked[0]["total"] == 1900


def test_calculate_sell_applies_margin():
    reg = build_tools()
    out = reg.call("freight.quote.calculate_sell",
                   {"quote": {"base_freight": {"amount": 1000, "currency": "USD"}},
                    "margin_pct": 20})
    assert out["result"]["suggested_sell"] == 1200


def test_rate_save_then_search():
    reg = build_tools()
    reg.call("freight.rate.save",
             {"origin_port": "CNSHA", "destination_port": "CAYVR",
              "equipment": "40HC", "amount": 1925, "currency": "USD"})
    found = reg.call("freight.rate.search",
                     {"origin": "CNSHA", "destination": "CAYVR", "equipment": "40HC"})
    assert found["result"][0]["amount"] == 1925


def test_alias_lookup_tool(tmp_path):
    from orderinbox.freight import AliasStore
    store = AliasStore(tmp_path / "a.json")
    store.learn("Shanghai ABC Logistics", "PCS", "PORT_CONGESTION_SURCHARGE")
    reg = build_tools(alias_store=store)
    out = reg.call("freight.alias.lookup",
                   {"counterparty": "Shanghai ABC Logistics", "term": "PCS"})
    assert out["result"]["match"] == "PORT_CONGESTION_SURCHARGE"
    assert out["result"]["conventions"] == {"PCS": "PORT_CONGESTION_SURCHARGE"}


def test_exchange_rate_tool():
    reg = build_tools()
    out = reg.call("freight.exchange.rate", {"from": "USD", "to": "CAD", "amount": 100})
    assert out["result"]["to"] == "CAD"
    assert out["result"]["converted"] > 100  # CAD per USD is > 1


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------

def _app(store, pipeline, token):
    from orderinbox.config import Settings
    from orderinbox.web.app import create_app
    from types import SimpleNamespace
    import os
    os.environ["ORDERINBOX_API_TOKEN"] = token
    settings = Settings()
    ctx = SimpleNamespace(store=store, pipeline=pipeline, mail_monitor=None)
    return create_app(settings, ctx)


def test_mcp_endpoints_and_token(monkeypatch, env):
    settings, backend, llm, store, pipeline = env
    monkeypatch.setenv("ORDERINBOX_API_TOKEN", "mcp-token")
    app = _app(store, pipeline, "mcp-token")
    app.state.sessions = {}
    with TestClient(app) as c:
        # no token -> 401
        assert c.get("/mcp/tools").status_code == 401
        # with token -> tool list
        r = c.get("/mcp/tools", params={"token": "mcp-token"})
        assert r.status_code == 200
        assert any(t["name"] == "freight.port.search" for t in r.json()["tools"])
        # call a tool
        r = c.post("/mcp/tools/freight.port.search",
                   params={"token": "mcp-token"}, json={"query": "Shanghai"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["result"][0]["un_locode"] == "CNSHA"
        # unknown tool
        r = c.post("/mcp/tools/nope", params={"token": "mcp-token"}, json={})
        assert r.json()["ok"] is False


def test_mcp_open_when_no_token(env):
    settings, backend, llm, store, pipeline = env
    app = _app(store, pipeline, "")
    app.state.sessions = {}
    with TestClient(app) as c:
        assert c.get("/mcp/tools").status_code == 200
