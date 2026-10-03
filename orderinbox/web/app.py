"""FastAPI web app: the OrderInbox review console.

Pages:
  /            dashboard (KPIs + recent activity)
  /orders      order list with status filters
  /orders/{id} full order detail — extraction, match scores, issues, actions
  /inbox       trigger a manual inbox poll
  /settings    effective configuration

Single admin password + session cookie. Deliberately simple: the people using
this are order-entry staff, not developers.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..mcp import build_tools, register_mcp_routes
from ..pipeline import Pipeline
from ..web.app_context import AppContext

log = logging.getLogger("orderinbox.web")


def create_app(settings: Settings, ctx: AppContext) -> FastAPI:
    base = Path(__file__).parent
    app = FastAPI(title="OrderInbox AI", docs_url=None)
    app.mount("/static", StaticFiles(directory=str(base / "static")), name="static")
    templates = Jinja2Templates(directory=str(base / "templates"))
    templates.env.globals["product"] = "OrderInbox AI"
    templates.env.filters["dt"] = lambda v: v.strftime("%b %d, %Y %H:%M") if v else ""
    templates.env.filters["pct"] = lambda v: f"{v:.0f}%" if v is not None else ""

    def authed(request: Request) -> bool:
        token = request.cookies.get("oi_token")
        if not token:
            return False
        session = request.app.state.sessions
        return session.get(token) == settings.web_password

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        path = request.url.path
        # /api and /mcp use their own token auth (consumed by the Odoo module
        # and by agents), so the session guard skips them.
        if not (path.startswith("/static") or path.startswith("/api/")
                or path.startswith("/mcp/")):
            if not authed(request) and path not in ("/login", "/logout"):
                return RedirectResponse("/login?next=" + path, status_code=303)
        return await call_next(request)

    # ------------------------------------------------------------------
    # /api/* — bridge for the Odoo Apps Store module (token auth, JSON)
    def _api_auth(request: Request) -> bool:
        if not settings.api_token:
            return False
        provided = ""
        authz = request.headers.get("Authorization", "")
        if authz.startswith("Bearer "):
            provided = authz[7:]
        else:
            provided = request.query_params.get("token", "")
        return secrets.compare_digest(provided, settings.api_token)

    def _order_payload(o) -> dict:
        ext = o.extraction
        return {
            "uid": o.uid,
            "subject": o.subject,
            "sender": o.sender,
            "received_at": o.received_at.isoformat(sep=" ") if o.received_at else None,
            "status": o.status.value,
            "customer_as_written": ext.customer_name_as_written if ext else "",
            "customer_matched": ext.customer_matched if ext else "",
            "customer_score": ext.customer_match_score if ext else 0.0,
            "po_number": ext.po_number if ext else "",
            "po_date": ext.po_date if ext else "",
            "currency": ext.currency if ext else "",
            "total": ext.total if ext else 0.0,
            "confidence": ext.confidence if ext else 0.0,
            "extract_method": ext.extract_method if ext else "",
            "payment_terms": ext.payment_terms if ext else "",
            "notes": ext.notes if ext else "",
            "lines": [
                {
                    "line_no": l.line_no,
                    "sku_as_written": l.sku_as_written,
                    "description": l.description,
                    "quantity": l.quantity,
                    "unit": l.unit,
                    "unit_price": l.unit_price,
                    "sku_matched": l.sku_matched,
                    "match_score": l.match_score,
                    "match_method": l.match_method.value,
                    "catalog_price": l.catalog_price,
                }
                for l in ext.lines
            ] if ext else [],
            "issues": [
                {"severity": i.severity.value, "code": i.code, "message": i.message,
                 "line": i.line, "suggested_fix": i.suggested_fix}
                for i in o.issues
            ],
            "error": o.error,
            "odoo_reference": o.odoo_reference,
        }

    # ------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, next: str = "/", password: str = "", error: str = ""):
        return templates.TemplateResponse(request, "login.html", {
            "next": next, "error": error,
            "submitted": bool(password),
        })

    @app.post("/login")
    def login_post(request: Request, next: str = Form("/"), password: str = Form("")):
        if secrets.compare_digest(password, settings.web_password):
            token = secrets.token_urlsafe(32)
            request.app.state.sessions[token] = password
            resp = RedirectResponse(next or "/", status_code=303)
            resp.set_cookie("oi_token", token, max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax")
            return resp
        return templates.TemplateResponse(request, "login.html", {
            "next": next, "error": "Wrong password.", "submitted": True,
        }, status_code=401)

    @app.get("/logout")
    def logout(request: Request):
        token = request.cookies.get("oi_token")
        if token:
            request.app.state.sessions.pop(token, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("oi_token")
        return resp

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        counts = ctx.store.counts_by_status()
        orders = ctx.store.list_orders(limit=12)
        ready = counts.get("ready", 0)
        exceptions = counts.get("exception", 0)
        sent = counts.get("sent", 0)
        freight_counts = ctx.store.freight_counts()
        return templates.TemplateResponse(request, "dashboard.html", {
            "counts": counts, "orders": orders,
            "ready": ready, "exceptions": exceptions, "sent": sent,
            "freight_counts": freight_counts,
            "freight": ctx.store.list_freight(limit=8),
            "backend": ctx.pipeline.backend.name,
        })

    @app.get("/orders", response_class=HTMLResponse)
    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def orders(request: Request, order_id: int | None = None, status: str = ""):
        if order_id is not None:
            order = ctx.store.get_order(order_id)
            if order is None:
                return templates.TemplateResponse(request, "404.html", {}, status_code=404)
            return templates.TemplateResponse(request, "order_detail.html", {"o": order, "backend": ctx.pipeline.backend.name})
        status = status if status in ("ready", "exception", "sent", "rejected", "failed", "received", "approved") else ""
        orders = ctx.store.list_orders(status=status or None, limit=300)
        return templates.TemplateResponse(request, "orders.html", {
            "orders": orders, "status": status,
            "counts": ctx.store.counts_by_status(),
        })

    @app.post("/orders/{order_id}/approve")
    def approve(request: Request, order_id: int):
        order = ctx.store.get_order(order_id)
        if order is None:
            return RedirectResponse("/orders", status_code=303)
        if order.status.value not in ("ready", "exception"):
            return RedirectResponse(f"/orders/{order_id}", status_code=303)
        try:
            ctx.pipeline.approve_and_send(order)
        except Exception as exc:
            log.exception("approve failed")
            order.add_log(f"approve failed: {exc}")
            ctx.store.upsert_order(order)
        return RedirectResponse(f"/orders/{order_id}", status_code=303)

    @app.post("/orders/{order_id}/reject")
    def reject(request: Request, order_id: int, reason: str = Form("")):
        order = ctx.store.get_order(order_id)
        if order is not None:
            ctx.pipeline.reject(order, reason)
        return RedirectResponse(f"/orders/{order_id}", status_code=303)

    @app.post("/orders/{order_id}/reprocess")
    def reprocess(request: Request, order_id: int):
        order = ctx.store.get_order(order_id)
        if order and order.stored_path and Path(order.stored_path).exists():
            ctx.pipeline.process_message(order.stored_path, subject=order.subject,
                                         sender=order.sender, received_at=order.received_at)
        return RedirectResponse(f"/orders/{order_id}", status_code=303)

    # ------------------------------------------------------------------
    # freight (RateScout) workflow: RFQs + quotes
    _FREIGHT_STATUSES = ("ready", "exception", "sent", "rejected", "failed",
                         "received", "parsed", "accepted", "expired")

    @app.get("/freight", response_class=HTMLResponse)
    @app.get("/freight/{case_id}", response_class=HTMLResponse)
    def freight(request: Request, case_id: int | None = None, kind: str = "", status: str = ""):
        if case_id is not None:
            case = ctx.store.get_freight(case_id)
            if case is None:
                return templates.TemplateResponse(request, "404.html", {}, status_code=404)
            return templates.TemplateResponse(request, "freight_detail.html", {
                "c": case,
                "margin_threshold": settings.freight_margin_threshold_pct,
                "backend": ctx.pipeline.backend.name,
            })
        status = status if status in _FREIGHT_STATUSES else ""
        kind = kind if kind in ("rfq", "quote") else ""
        cases = ctx.store.list_freight(status=status or None, kind=kind or None, limit=300)
        all_cases = ctx.store.list_freight(limit=1000)
        return templates.TemplateResponse(request, "freight.html", {
            "cases": cases,
            "kind": kind, "status": status,
            "counts": ctx.store.freight_counts(),
            "total": len(all_cases),
            "rfq_count": sum(1 for x in all_cases if x.kind == "rfq"),
            "quote_count": sum(1 for x in all_cases if x.kind == "quote"),
        })

    @app.post("/freight/{case_id}/approve")
    def freight_approve(request: Request, case_id: int):
        case = ctx.store.get_freight(case_id)
        if case is None:
            return RedirectResponse("/freight", status_code=303)
        if case.status.value not in ("ready", "exception"):
            return RedirectResponse(f"/freight/{case_id}", status_code=303)
        try:
            ctx.pipeline.approve_freight(case)
        except Exception as exc:
            log.exception("freight approve failed")
            case.add_log(f"approve failed: {exc}")
            ctx.store.upsert_freight(case)
        return RedirectResponse(f"/freight/{case_id}", status_code=303)

    @app.post("/freight/{case_id}/reject")
    def freight_reject(request: Request, case_id: int, reason: str = Form("")):
        case = ctx.store.get_freight(case_id)
        if case is not None:
            ctx.pipeline.reject_freight(case, reason)
        return RedirectResponse(f"/freight/{case_id}", status_code=303)

    @app.post("/freight/{case_id}/reprocess")
    def freight_reprocess(request: Request, case_id: int):
        case = ctx.store.get_freight(case_id)
        if case and case.stored_path and Path(case.stored_path).exists():
            ctx.pipeline.process_message(case.stored_path, subject=case.subject,
                                         sender=case.sender, received_at=case.received_at)
        return RedirectResponse(f"/freight/{case_id}", status_code=303)

    # ------------------------------------------------------------------
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        return templates.TemplateResponse(request, "inbox.html", {
            "settings": settings,
            "orders": ctx.store.list_orders(limit=20),
        })

    @app.post("/inbox/poll")
    def inbox_poll(request: Request):
        results = ctx.mail_monitor.poll_once()
        return RedirectResponse("/inbox?processed=%d" % len(results), status_code=303)

    # ------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        s = settings
        rows = [
            ("Mode", f"Odoo backend: {s.odoo_backend()}", ""),
            ("Odoo URL", s.odoo_url or "(mock)", ""),
            ("Odoo DB", s.odoo_db or "(mock)", ""),
            ("Inbox", f"{s.mail_provider} — {s.imap_host}:{s.imap_port} folder={s.imap_folder}" if s.mail_provider != "none" else "manual only", ""),
            ("LLM", f"mode={s.llm_mode}, ollama={s.ollama_url} model={s.ollama_model}"
                    + (f", cloud={s.cloud_base_url}/{s.cloud_model}" if s.cloud_base_url else ""), ""),
            ("Model gateway", _providers_line(ctx), ""),
            ("Escalation", f"auto>={s.escalate_auto}, verify>={s.escalate_verify}, "
                           f"cloud>={s.escalate_cloud}; producer/verifier="
                           f"{'on' if s.use_producer_verifier else 'off'}, "
                           f"cloud-escalation={'on' if s.use_cloud_escalation else 'off'}", ""),
            ("Matching", f"auto-ready ≥ {s.match_threshold:.0f}, low bar {s.low_match_threshold:.0f}, price tolerance {s.price_deviation_tolerance:.0%}", ""),
            ("Currencies", ", ".join(s.currencies()), ""),
            ("Version", f"OrderInbox AI {__import__('orderinbox').__version__}", ""),
        ]
        return templates.TemplateResponse(request, "settings.html", {"rows": rows})

    @app.get("/settings/test-odoo")
    def test_odoo(request: Request):
        ok, message = ctx.test_odoo()
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------------
    # /api/* — bridge consumed by the Odoo module (orderinbox in Odoo):
    #   GET  /api/orders               list of order payloads (token)
    #   GET  /api/orders/{uid}         one order payload (token)
    #   POST /api/orders/{uid}/approve  create the draft order (token)
    #   POST /api/orders/{uid}/reject   reject with reason (token)
    def _api_denied() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "api disabled or bad token — "
                                                    "set ORDERINBOX_API_TOKEN"}, status_code=401)

    @app.get("/api/orders")
    def api_orders(request: Request):
        if not _api_auth(request):
            return _api_denied()
        orders = ctx.store.list_orders(limit=500)
        return {"ok": True, "orders": [_order_payload(o) for o in orders]}

    @app.get("/api/orders/{uid}")
    def api_order(request: Request, uid: str):
        if not _api_auth(request):
            return _api_denied()
        order = next((o for o in ctx.store.list_orders(limit=1000) if o.uid == uid), None)
        if order is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return {"ok": True, "order": _order_payload(order)}

    @app.post("/api/orders/{uid}/approve")
    def api_approve(request: Request, uid: str):
        if not _api_auth(request):
            return _api_denied()
        order = next((o for o in ctx.store.list_orders(limit=1000) if o.uid == uid), None)
        if order is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if order.status.value not in ("ready", "exception"):
            return {"ok": True, "uid": uid, "status": order.status.value,
                    "reference": order.odoo_reference}
        try:
            ctx.pipeline.approve_and_send(order)
        except Exception as exc:
            order.add_log(f"approve failed: {exc}")
            ctx.store.upsert_order(order)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {
            "ok": True,
            "uid": uid,
            "status": order.status.value,
            "sale_order_id": order.odoo_order_id,
            "reference": order.odoo_reference,
        }

    @app.post("/api/orders/{uid}/reject")
    async def api_reject(request: Request, uid: str):
        if not _api_auth(request):
            return _api_denied()
        order = next((o for o in ctx.store.list_orders(limit=1000) if o.uid == uid), None)
        if order is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        reason = (body.get("reason") or "").strip()
        ctx.pipeline.reject(order, reason)
        return {"ok": True, "uid": uid, "status": order.status.value}

    # ------------------------------------------------------------------
    # /mcp/* — Model Context Protocol tools (Section 6). Same token auth as
    # the /api bridge so an external agent (or the local Strata model) can
    # call the product's capabilities: freight.*, customer, quote, approval.
    try:
        registry = build_tools(
            backend=ctx.pipeline.backend, store=ctx.store, pipeline=ctx.pipeline,
            alias_store=ctx.pipeline.alias_store)
        register_mcp_routes(app, registry, token=settings.api_token)
    except Exception:
        log.exception("MCP tool registration failed (continuing without /mcp)")

    return app


def _providers_line(ctx) -> str:
    """One-line summary of the model gateway providers for the settings page."""
    try:
        summary = ctx.pipeline.llm.providers()
    except Exception:
        return "(gateway unavailable)"
    if not summary:
        return "no providers configured (deterministic only)"
    parts = []
    for p in summary:
        state = "ready" if p["available"] else "off"
        parts.append(f"{p['name']} [{p['kind']}/{state}]")
    return ", ".join(parts)
