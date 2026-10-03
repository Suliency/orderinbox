"""OrderInbox AI CLI.

    orderinbox serve            start the review console (web UI + poll loop)
    orderinbox demo             build a demo dataset and serve it
    orderinbox poll             poll the configured inbox once
    orderinbox process FILE     process a standalone document
    orderinbox odoo-test        check the Odoo/backend connection
    orderinbox gmail-auth       one-time Gmail OAuth authorization
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import typer

from . import __version__
from .config import Settings
from .web.app import create_app
from .web.app_context import AppContext

app = typer.Typer(help="OrderInbox AI — unattended AI order entry for Odoo.", no_args_is_help=True)


def _ctx() -> AppContext:
    try:
        from dotenv import load_dotenv
        load_dotenv()  # no-op when no .env is present
    except ImportError:
        pass
    return AppContext()


def _print_order(item) -> None:
    from .freight.models import FreightCase
    if isinstance(item, FreightCase):
        tag = item.kind.upper()
        lane = f" {item.lane}" if item.lane else ""
        typer.secho(f"[{item.status.value.upper():9}] ({tag:5}) {item.subject or item.stored_path}{lane}",
                    fg=typer.colors.CYAN)
        meta = []
        if item.equipment:
            meta.append(f"{item.equipment} x{item.container_count or 1}" if item.container_count else item.equipment)
        if item.counterparty:
            meta.append(f"from {item.counterparty}")
        if item.buy_cost:
            meta.append(f"cost {item.buy_cost:.0f} {item.quote_currency or ''}".rstrip())
        if item.sell_total is not None:
            m = f" margin {item.margin_pct:.1f}%" if item.margin_pct is not None else ""
            meta.append(f"sell {item.sell_total:.0f} {item.quote_currency or ''}{m}")
        if meta:
            typer.echo(f"    {'   '.join(meta)}")
        if item.recommendation:
            typer.echo(f"    rec: {item.recommendation}")
        for i in item.issues:
            prefix = "ERR " if i.severity.value == "error" else "warn"
            typer.echo(f"    {prefix} {i.code}: {i.message}")
        if item.error:
            typer.echo(f"    note: {item.error}")
        return
    order = item
    e = order.extraction
    typer.secho(f"[{order.status.value.upper():9}] {order.subject or order.stored_path}", fg=typer.colors.CYAN)
    if e:
        typer.echo(f"    customer: {e.customer_matched or '?'} ({e.customer_match_score:.0f})"
                   f"   po: {e.po_number or '?'}   lines: {len(e.lines)}   total: ${e.total:.2f}")
        for i in order.issues:
            prefix = "ERR " if i.severity.value == "error" else "warn"
            typer.echo(f"    {prefix} {i.code}: {i.message}")
    if order.error:
        typer.echo(f"    note: {order.error}")


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(f"OrderInbox AI {__version__}")


@app.command()
def odoo_test() -> None:
    """Test the Odoo (or mock) backend connection."""
    ctx = _ctx()
    ok, message = ctx.test_odoo()
    typer.secho(message, fg=typer.colors.GREEN if ok else typer.colors.RED)
    raise typer.Exit(0 if ok else 1)


@app.command()
def poll() -> None:
    """Poll the configured inbox once and process what looks like orders."""
    ctx = _ctx()
    results = ctx.mail_monitor.poll_once()
    if not results:
        typer.echo("No new order-like messages.")
        return
    for r in results:
        _print_order(r)


@app.command()
def process(path: Path) -> None:
    """Process a standalone document (PDF/XLSX/CSV/EML) through the pipeline."""
    if not path.exists():
        typer.secho(f"file not found: {path}", fg=typer.colors.RED)
        raise typer.Exit(1)
    ctx = _ctx()
    order = ctx.pipeline.process_file(path)
    _print_order(order)


@app.command()
def demo(serve: bool = typer.Option(True, "--serve/--no-serve", help="start the web console after seeding")) -> None:
    """Seed a realistic dataset (freight RFQ + agent quotes, order emails) and run the pipeline.

    This is the sales demo: two minutes from `docker compose up` to a screen
    full of quoted lanes, matched orders, and one-click approvals.
    """
    ctx = _ctx()
    from .demo.seed import build_demo
    typer.echo("Building demo dataset (freight RFQ + quotes, PDF/XLSX/CSV order emails)…")
    created = build_demo(settings=ctx.settings)
    for p in created:
        typer.echo(f"  · {p.name}")
    typer.echo("Processing…")
    for p in created:
        order = ctx.pipeline.process_message(p)
        _print_order(order)
    if serve:
        _serve(ctx)


@app.command()
def serve() -> None:
    """Start the review console (web UI + background inbox poller)."""
    ctx = _ctx()
    _serve(ctx)


def _serve(ctx: AppContext) -> None:
    import uvicorn

    app_obj = create_app(ctx.settings, ctx)
    app_obj.state.sessions = {}
    # background inbox poller (no-op when provider=none)
    def loop() -> None:
        while True:
            try:
                n = len(ctx.mail_monitor.poll_once())
                if n:
                    logging.getLogger("orderinbox").info("poll processed %d messages", n)
            except Exception:
                logging.getLogger("orderinbox").exception("poll loop error")
            time.sleep(ctx.settings.inbox_poll_seconds)

    threading.Thread(target=loop, daemon=True).start()
    typer.secho(
        f"\n  OrderInbox AI console →  http://localhost:{ctx.settings.web_port}\n"
        f"  password: {ctx.settings.web_password}\n\n",
        fg=typer.colors.CYAN,
    )
    uvicorn.run(app_obj, host=ctx.settings.web_host, port=ctx.settings.web_port, log_level="warning")


if __name__ == "__main__":
    app()
