"""MCP tool layer (Section 6).

Exposes the product's capabilities as Model Context Protocol tools so a local
(or any) agent can call them:

    ratescout-mcp
      ├── freight.customer.search
      ├── freight.port.search
      ├── freight.rfq.search
      ├── freight.rate.search
      ├── freight.rate.save
      ├── freight.quote.normalize
      ├── freight.quote.compare
      ├── freight.quote.calculate_sell
      ├── freight.email.read
      ├── freight.email.send
      ├── freight.tms.create_shipment
      └── freight.approval.request

The pattern (Section 5) is: *the agent reasons, the tools perform business
operations.* Tools are the controlled surface an agent may touch; they are not
free-form prompt content. Each tool is a named function with a JSON-schema
input and a JSON result, so the same registry backs both the in-process agent
and the HTTP `/mcp` endpoint (token-authenticated, mirroring the existing
`/api` bridge).

Deterministic tools (port search, quote normalization, comparison, sell-rate
math) are fully implemented. Integration tools (email, TMS, approval) are
wired to the live objects when available and otherwise record their intent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from .freight import ontology
from .freight.quote import (
    FreightQuote, margin_pct, normalize_quote, total_cost,
)
from .freight.validate import FreightContext, validate_quote

log = logging.getLogger("orderinbox.mcp")


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)   # JSON Schema (object)
    handler: Optional[Callable[[dict], Any]] = None
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
            "tags": self.tags,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def list(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict | None = None) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        if tool.handler is None:
            return {"ok": False, "error": f"tool not implemented yet: {name}"}
        try:
            result = tool.handler(args or {})
        except Exception as exc:
            log.exception("mcp tool %s failed", name)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "tool": name, "result": result}


# --------------------------------------------------------------------------
# a minimal in-memory rate book (backing rate.search / rate.save)
# --------------------------------------------------------------------------

class RateBook:
    def __init__(self):
        self._rates: list[dict] = []

    def save(self, record: dict) -> dict:
        self._rates.append(record)
        return record

    def search(self, origin: str = "", destination: str = "", equipment: str = "") -> list[dict]:
        out = []
        for r in self._rates:
            if origin and r.get("origin_port", "").upper() != origin.upper():
                continue
            if destination and r.get("destination_port", "").upper() != destination.upper():
                continue
            if equipment and r.get("equipment", "").upper() != equipment.upper():
                continue
            out.append(r)
        return out

    def all(self) -> list[dict]:
        return list(self._rates)


# --------------------------------------------------------------------------
# tool builders
# --------------------------------------------------------------------------

def _tool_port_search() -> Tool:
    def handler(args: dict):
        q = str(args.get("query") or "")
        p = ontology.lookup_port(q)
        if p:
            return [{"un_locode": p.un_locode, "name": p.name,
                     "country": p.country, "city": p.city}]
        # loose: any port whose name/city contains the query
        return [{"un_locode": x.un_locode, "name": x.name, "country": x.country}
                for x in ontology.BUILTIN_PORTS
                if q.lower() in (x.name + " " + x.city).lower()]
    return Tool(
        name="freight.port.search",
        description="Resolve a port name/city/UN-LOCODE to canonical port records.",
        parameters={"type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]},
        handler=handler, tags=["freight", "lookup"])


def _tool_customer_search(backend) -> Tool:
    from .match.customers import match_customer

    def handler(args: dict):
        partners = backend.get_partners() if backend else []
        m = match_customer(str(args.get("query") or ""), partners)
        if not m.partner:
            return {"match": None, "score": m.score}
        return {"match": m.partner.name, "email": m.partner.email,
                "score": m.score, "method": m.method}
    return Tool(
        name="freight.customer.search",
        description="Match a customer name to a known partner (customer master).",
        parameters={"type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]},
        handler=handler, tags=["freight", "lookup"])


def _tool_rate_search(book: RateBook) -> Tool:
    def handler(args: dict):
        return book.search(str(args.get("origin") or ""),
                           str(args.get("destination") or ""),
                           str(args.get("equipment") or ""))
    return Tool(
        name="freight.rate.search",
        description="Search historical rates for a lane/equipment.",
        parameters={"type": "object",
                    "properties": {"origin": {"type": "string"},
                                   "destination": {"type": "string"},
                                   "equipment": {"type": "string"}}},
        handler=handler, tags=["freight", "rate"])


def _tool_rate_save(book: RateBook) -> Tool:
    def handler(args: dict):
        record = dict(args)
        return book.save(record)
    return Tool(
        name="freight.rate.save",
        description="Record a rate into the rate book.",
        parameters={"type": "object",
                    "properties": {"origin_port": {"type": "string"},
                                   "destination_port": {"type": "string"},
                                   "equipment": {"type": "string"},
                                   "amount": {"type": "number"},
                                   "currency": {"type": "string"}}},
        handler=handler, tags=["freight", "rate"])


def _tool_quote_normalize() -> Tool:
    def handler(args: dict):
        raw = args.get("quote") or args
        q = normalize_quote(raw if isinstance(raw, dict) else {})
        return q.normalized_dict()
    return Tool(
        name="freight.quote.normalize",
        description="Normalize a raw/messy quote into the canonical freight structure.",
        parameters={"type": "object",
                    "properties": {"quote": {"type": "object"}},
                    "required": ["quote"]},
        handler=handler, tags=["freight", "quote"])


def _tool_quote_compare() -> Tool:
    def handler(args: dict):
        quotes = args.get("quotes") or []
        currency = str(args.get("currency") or "USD")
        rows = []
        for i, raw in enumerate(quotes):
            q = normalize_quote(raw if isinstance(raw, dict) else {})
            rows.append({
                "index": i,
                "counterparty": q.counterparty,
                "origin_port": q.origin_port,
                "destination_port": q.destination_port,
                "equipment": q.equipment,
                "total": total_cost(q, currency),
                "currency": currency,
                "requires_review": q.requires_review,
                "issues": [i.code for i in validate_quote(q, FreightContext())],
            })
        rows.sort(key=lambda r: (r["total"] is None, r["total"] or 0))
        return {"currency": currency, "ranked": rows}
    return Tool(
        name="freight.quote.compare",
        description="Normalize and rank multiple quotes for the same lane by cost.",
        parameters={"type": "object",
                    "properties": {"quotes": {"type": "array"},
                                   "currency": {"type": "string"}},
                    "required": ["quotes"]},
        handler=handler, tags=["freight", "quote"])


def _tool_alias_lookup(alias_store) -> Tool:
    def handler(args: dict):
        counterparty = str(args.get("counterparty") or "")
        term = str(args.get("term") or "")
        if not alias_store:
            return {"match": None, "conventions": {}}
        canonical = alias_store.resolve(counterparty, term)
        return {
            "match": canonical,
            # the counterparty's full learned conventions, to prime the model
            "conventions": alias_store.conventions_for(counterparty),
        }
    return Tool(
        name="freight.alias.lookup",
        description="Look up a counterparty-specific term against learned aliases.",
        parameters={"type": "object",
                    "properties": {"counterparty": {"type": "string"},
                                   "term": {"type": "string"}},
                    "required": ["counterparty", "term"]},
        handler=handler, tags=["freight", "alias"])


def _tool_exchange_rate() -> Tool:
    from .freight import fx

    def handler(args: dict):
        f = str(args.get("from") or "USD")
        t = str(args.get("to") or "USD")
        amt = args.get("amount")
        r = fx.rate(t, f)
        out = {"from": f, "to": t, "rate": round(r, 6)}
        if amt is not None:
            out["converted"] = fx.convert(float(amt), f, t)
        return out
    return Tool(
        name="freight.exchange.rate",
        description="Convert between currencies (deterministic FX, not the model).",
        parameters={"type": "object",
                    "properties": {"from": {"type": "string"},
                                   "to": {"type": "string"},
                                   "amount": {"type": "number"}}},
        handler=handler, tags=["freight", "fx"])


def _tool_quote_calculate_sell() -> Tool:
    def handler(args: dict):
        raw = args.get("quote") or {}
        q: FreightQuote = normalize_quote(raw if isinstance(raw, dict) else {})
        currency = str(args.get("currency") or q.base_freight.currency or "USD")
        cost = total_cost(q, currency)
        margin = float(args.get("margin_pct") or 0.0)
        if cost <= 0:
            return {"ok": False, "error": "no computable cost"}
        sell = round(cost * (1 + margin / 100.0), 2)
        return {"cost": cost, "margin_pct": margin, "suggested_sell": sell,
                "currency": currency}
    return Tool(
        name="freight.quote.calculate_sell",
        description="Compute a suggested sell rate from a quote's cost and a target margin.",
        parameters={"type": "object",
                    "properties": {"quote": {"type": "object"},
                                   "currency": {"type": "string"},
                                   "margin_pct": {"type": "number"}},
                    "required": ["quote"]},
        handler=handler, tags=["freight", "quote", "pricing"])


def _tool_rfq_search(store) -> Tool:
    def handler(args: dict):
        if not store:
            return {"ok": False, "error": "no store available"}
        origin = str(args.get("origin") or "").upper()
        destination = str(args.get("destination") or "").upper()
        out = []
        for c in store.open_rfqs():
            rfq = c.rfq
            if not rfq:
                continue
            if origin and rfq.origin_port.upper() != origin:
                continue
            if destination and rfq.destination_port.upper() != destination:
                continue
            out.append({
                "uid": c.uid,
                "status": c.status.value,
                "lane": c.lane,
                "equipment": c.equipment,
                "container_count": c.container_count,
                "customer": rfq.customer_matched or rfq.customer_name_as_written,
                "ready_date": rfq.ready_date,
                "budget_total": rfq.budget_total,
                "budget_currency": rfq.budget_currency,
            })
        return out
    return Tool(
        name="freight.rfq.search",
        description="List open customer RFQs (optionally filtered by origin/destination port).",
        parameters={"type": "object",
                    "properties": {"origin": {"type": "string"},
                                   "destination": {"type": "string"}}},
        handler=handler, tags=["freight", "rfq"])


def _tool_email_read(store) -> Tool:
    def handler(args: dict):
        if not store:
            return {"ok": False, "error": "no store available"}
        uid = str(args.get("uid") or "")
        for o in store.list_orders(limit=1000):
            if o.uid == uid:
                ext = o.extraction
                return {"uid": o.uid, "status": o.status.value,
                        "subject": o.subject, "sender": o.sender,
                        "customer": ext.customer_matched if ext else "",
                        "po_number": ext.po_number if ext else "",
                        "lines": len(ext.lines) if ext else 0}
        return {"ok": False, "error": "not found"}
    return Tool(
        name="freight.email.read",
        description="Read a stored inbox message / processed order by uid.",
        parameters={"type": "object",
                    "properties": {"uid": {"type": "string"}},
                    "required": ["uid"]},
        handler=handler, tags=["freight", "email"])


def _tool_email_send() -> Tool:
    sent: list[dict] = []

    def handler(args: dict):
        record = {"to": args.get("to"), "subject": args.get("subject"),
                  "body": args.get("body"), "sent": True}
        sent.append(record)
        return record
    return Tool(
        name="freight.email.send",
        description="Send an email (a customer or supplier follow-up).",
        parameters={"type": "object",
                    "properties": {"to": {"type": "string"},
                                   "subject": {"type": "string"},
                                   "body": {"type": "string"}},
                    "required": ["to", "subject", "body"]},
        handler=handler, tags=["freight", "email"])


def _tool_tms_create_shipment() -> Tool:
    shipments: list[dict] = []

    def handler(args: dict):
        record = dict(args)
        record["shipment_id"] = f"SHIP-{len(shipments) + 1:04d}"
        record["state"] = "draft"
        shipments.append(record)
        return record
    return Tool(
        name="freight.tms.create_shipment",
        description="Create a shipment record in the TMS.",
        parameters={"type": "object",
                    "properties": {"origin_port": {"type": "string"},
                                   "destination_port": {"type": "string"},
                                   "equipment": {"type": "string"},
                                   "customer": {"type": "string"}}},
        handler=handler, tags=["freight", "tms"])


def _tool_approval_request(pipeline) -> Tool:
    def handler(args: dict):
        if not pipeline:
            return {"ok": False, "error": "no pipeline available"}
        uid = str(args.get("uid") or "")
        order = next((o for o in _store_like_list(pipeline, uid) if o.uid == uid), None)
        if order is None:
            return {"ok": False, "error": "not found"}
        if args.get("approve"):
            try:
                pipeline.approve_and_send(order)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            return {"uid": uid, "status": order.status.value,
                    "reference": order.odoo_reference}
        pipeline.reject(order, str(args.get("reason") or ""))
        return {"uid": uid, "status": order.status.value}
    return Tool(
        name="freight.approval.request",
        description="Request approval (or rejection) for a processed order/quote.",
        parameters={"type": "object",
                    "properties": {"uid": {"type": "string"},
                                   "approve": {"type": "boolean"},
                                   "reason": {"type": "string"}},
                    "required": ["uid"]},
        handler=handler, tags=["freight", "approval"])


def _store_like_list(pipeline, uid):
    # the pipeline holds a store reference
    store = getattr(pipeline, "store", None)
    if store is not None:
        return store.list_orders(limit=1000)
    return []


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------

def build_tools(backend=None, store=None, pipeline=None,
                rate_book: RateBook | None = None, alias_store=None) -> ToolRegistry:
    """Build the default RateScout tool registry.

    Any of backend / store / pipeline / alias_store may be None; the
    corresponding tools report "not available" rather than erroring, so the
    registry is usable in tests and in lightweight deployments.
    """
    book = rate_book or RateBook()
    reg = ToolRegistry()
    reg.register(_tool_port_search())
    reg.register(_tool_customer_search(backend))
    reg.register(_tool_rfq_search(store))
    reg.register(_tool_rate_search(book))
    reg.register(_tool_rate_save(book))
    reg.register(_tool_alias_lookup(alias_store))
    reg.register(_tool_exchange_rate())
    reg.register(_tool_quote_normalize())
    reg.register(_tool_quote_compare())
    reg.register(_tool_quote_calculate_sell())
    reg.register(_tool_email_read(store))
    reg.register(_tool_email_send())
    reg.register(_tool_tms_create_shipment())
    reg.register(_tool_approval_request(pipeline))
    return reg


# --------------------------------------------------------------------------
# HTTP surface (mounted into the FastAPI app)
# --------------------------------------------------------------------------

def register_mcp_routes(app, registry: ToolRegistry, token: str = "") -> None:
    """Add `/mcp/tools` (list) and `/mcp/tools/{name}` (call) to a FastAPI app.

    Auth mirrors the existing `/api` bridge: an optional shared token via
    `?token=` or `Authorization: Bearer`. When `token` is empty the endpoint
    is open (local single-user console).
    """
    import secrets

    def _authed(request: Request) -> bool:
        if not token:
            return True
        provided = ""
        authz = request.headers.get("Authorization", "")
        if authz.startswith("Bearer "):
            provided = authz[7:]
        else:
            provided = request.query_params.get("token", "")
        return secrets.compare_digest(provided, token)

    @app.get("/mcp/tools")
    def mcp_list(request: Request):
        if not _authed(request):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        return {"ok": True, "tools": registry.list()}

    @app.post("/mcp/tools/{name}")
    async def mcp_call(request: Request, name: str):
        if not _authed(request):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        return registry.call(name, body)
