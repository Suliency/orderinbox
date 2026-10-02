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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings
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
        if not request.url.path.startswith("/static"):
            if not authed(request) and request.url.path not in ("/login", "/logout"):
                return RedirectResponse("/login?next=" + request.url.path, status_code=303)
        return await call_next(request)

    # ------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, next: str = "/", password: str = "", error: str = ""):
        return templates.TemplateResponse("login.html", {
            "request": request, "next": next, "error": error,
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
        return templates.TemplateResponse("login.html", {
            "request": request, "next": next, "error": "Wrong password.", "submitted": True,
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
        lines_total = sum(len(o.extraction.lines) if o.extraction else 0 for o in orders)
        return templates.TemplateResponse("dashboard.html", {
            "request": request, "counts": counts, "orders": orders,
            "ready": ready, "exceptions": exceptions, "sent": sent,
            "backend": ctx.pipeline.backend.name,
        })

    @app.get("/orders", response_class=HTMLResponse)
    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def orders(request: Request, order_id: int | None = None, status: str = ""):
        if order_id is not None:
            order = ctx.store.get_order(order_id)
            if order is None:
                return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
            return templates.TemplateResponse("order_detail.html", {"request": request, "o": order, "backend": ctx.pipeline.backend.name})
        status = status if status in ("ready", "exception", "sent", "rejected", "failed", "received", "approved") else ""
        orders = ctx.store.list_orders(status=status or None, limit=300)
        return templates.TemplateResponse("orders.html", {
            "request": request, "orders": orders, "status": status,
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
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        return templates.TemplateResponse("inbox.html", {
            "request": request, "settings": settings,
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
            ("Matching", f"auto-ready ≥ {s.match_threshold:.0f}, low bar {s.low_match_threshold:.0f}, price tolerance {s.price_deviation_tolerance:.0%}", ""),
            ("Currencies", ", ".join(s.currencies()), ""),
            ("Version", f"OrderInbox AI {__import__('orderinbox').__version__}", ""),
        ]
        return templates.TemplateResponse("settings.html", {"request": request, "rows": rows})

    @app.get("/settings/test-odoo")
    def test_odoo(request: Request):
        ok, message = ctx.test_odoo()
        return {"ok": ok, "message": message}

    return app
