"""Tests for the /api/* bridge endpoints consumed by the Odoo Apps Store module.

The Odoo module pulls orders (GET /api/orders), re-reads a single order
(GET /api/orders/{uid}), and triggers approve/reject (POST). Auth is a shared
token (ORDERINBOX_API_TOKEN) via ?token= or Authorization: Bearer.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from orderinbox.config import Settings
from orderinbox.db import Store
from orderinbox.extract import LLMClient
from orderinbox.odoo.mock import MockOdooBackend
from orderinbox.pipeline import Pipeline
from orderinbox.web.app import create_app

TOKEN = "bridge-test-token"


@pytest.fixture()
def api_client(env, monkeypatch):
    monkeypatch.setenv("ORDERINBOX_API_TOKEN", TOKEN)
    settings = Settings()  # re-read env incl. the token
    store = Store(settings.db_path())
    backend = MockOdooBackend()
    llm = LLMClient(settings)
    pipeline = Pipeline(settings, backend, llm, store)
    # seed the demo dataset (deterministic: 3 ready, 3 exception, 1 rejected)
    from orderinbox.demo.seed import build_demo
    for p in build_demo(settings):
        pipeline.process_message(p)
    ctx = SimpleNamespace(store=store, pipeline=pipeline, mail_monitor=None)
    app = create_app(settings, ctx)
    app.state.sessions = {}
    with TestClient(app) as c:
        c._ctx = ctx  # for direct store assertions
        yield c


def _orders(c: TestClient) -> list[dict]:
    r = c.get("/api/orders", params={"token": TOKEN})
    assert r.status_code == 200
    return r.json()["orders"]


def test_api_requires_token(api_client):
    assert api_client.get("/api/orders").status_code == 401
    assert api_client.get("/api/orders", params={"token": "wrong"}).status_code == 401


def test_list_orders(api_client):
    orders = _orders(api_client)
    assert len(orders) == 7
    statuses = {o["status"] for o in orders}
    assert statuses == {"ready", "exception", "rejected"}
    for o in orders:
        assert o["uid"]
        assert o["subject"]
        assert isinstance(o["lines"], list)
        assert isinstance(o["issues"], list)


def test_order_detail(api_client):
    orders = _orders(api_client)
    target = next(o for o in orders if o["status"] == "exception")
    r = api_client.get(f"/api/orders/{target['uid']}", params={"token": TOKEN})
    assert r.status_code == 200
    body = r.json()["order"]
    assert body["uid"] == target["uid"]
    assert any(i["code"] for i in body["issues"])
    assert api_client.get("/api/orders/does-not-exist",
                          params={"token": TOKEN}).status_code == 404


def test_approve_ready_order(api_client):
    orders = _orders(api_client)
    ready = next(o for o in orders if o["status"] == "ready")
    r = api_client.post(f"/api/orders/{ready['uid']}/approve", params={"token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "sent"
    assert body["reference"]
    # reflected in the store
    after = next(o for o in _orders(api_client) if o["uid"] == ready["uid"])
    assert after["status"] == "sent"


def test_reject_order(api_client):
    orders = _orders(api_client)
    exc = next(o for o in orders if o["status"] == "exception")
    r = api_client.post(f"/api/orders/{exc['uid']}/reject",
                        params={"token": TOKEN},
                        json={"reason": "customer changed quantity"})
    assert r.status_code == 200
    after = next(o for o in _orders(api_client) if o["uid"] == exc["uid"])
    assert after["status"] == "rejected"


def test_approve_unknown_order(api_client):
    assert api_client.post("/api/orders/nope/approve",
                           params={"token": TOKEN}).status_code == 404


def test_ui_still_works_after_api_changes(api_client):
    """The session-auth middleware must still guard the HTML pages."""
    assert api_client.get("/", follow_redirects=False).status_code == 303  # → /login
    r = api_client.post("/login", data={"password": "orderinbox"})
    assert r.status_code == 200  # followed the 303 → dashboard
    assert api_client.cookies.get("oi_token")
    assert api_client.get("/orders").status_code == 200
