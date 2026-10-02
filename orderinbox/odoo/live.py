"""Live Odoo backend over XML-RPC (no heavy SDK — stdlib xmlrpc.client).

Expected Odoo modules: sale, product, partner (standard). Products are read
with default_code (internal reference), list_price (sale price), and a
`pack_qty`-style field when the customer has one configured (we read it from
the product's attributes if present, else 0).
"""
from __future__ import annotations

import logging
from typing import Optional

import xmlrpc.client

from . import OrderLinePayload, PartnerRecord, ProductRecord

log = logging.getLogger("orderinbox.odoo.live")


class OdooError(RuntimeError):
    pass


class LiveOdooBackend:
    name = "live"

    def __init__(self, url: str, db: str, username: str, password: str):
        if not (url and db and username and password):
            raise OdooError("ODOO_URL, ODOO_DB, ODOO_USERNAME and ODOO_PASSWORD are required when ODOO_MODE=live")
        self.common = xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/common")
        self.uid = self.common.authenticate(db, username, password, {})
        if not self.uid:
            raise OdooError(f"Odoo authentication failed for {username}@{db}")
        self.db = db
        self.username = username

    def _model(self, model: str):
        return xmlrpc.client.ServerProxy(f"{self.db.rstrip('/')}/{model}")

    def call(self, model: str, method: str, *args):
        res = self._model(model)
        fn = getattr(res, method)
        try:
            return fn(self.uid, *args)
        except xmlrpc.client.Fault as exc:
            raise OdooError(f"Odoo {model}.{method} failed: {exc.faultString}") from exc

    def get_partners(self) -> list[PartnerRecord]:
        rows = self.call("res.partner", "search_read",
                         [["is_company", "=", True], ["parent_id", "=", False]],
                         ["id", "name", "email", "phone"])
        return [PartnerRecord(id=r["id"], name=r["name"], email=r.get("email") or "",
                              phone=r.get("phone") or "") for r in rows]

    def get_products(self) -> list[ProductRecord]:
        rows = self.call("product.product", "search_read",
                         [["sale_ok", "=", True]],
                         ["id", "default_code", "name", "list_price", "alias_ids", "pack_qty", "min_qty", "max_qty"])
        out: list[ProductRecord] = []
        for r in rows:
            out.append(ProductRecord(
                id=r["id"],
                default_code=r.get("default_code") or "",
                name=r.get("name") or "",
                list_price=float(r.get("list_price") or 0),
                currency="USD",
                pack_qty=int(r.get("pack_qty") or 0),
                min_qty=float(r.get("min_qty") or 0),
                max_qty=float(r.get("max_qty") or 0),
                aliases=[a["name"] for a in (r.get("alias_ids") or [])],
            ))
        return out

    def create_draft_order(self, partner_id: int, lines: list[OrderLinePayload],
                           reference: str, notes: str) -> tuple[int, str]:
        order_lines = [
            {
                "product_id": l.product_id,
                "product_uom_qty": l.quantity,
                "price_unit": l.unit_price,
                "name": l.name or f"PO {reference} line",
                "sequence": i + 1,
            }
            for i, l in enumerate(lines)
        ]
        vals = {
            "partner_id": partner_id,
            "client_order_ref": reference,
            "note": notes or "",
            "order_line": [(0, 0, line) for line in order_lines],
        }
        order_id = self.call("sale.order", "create", vals)
        ref = self.call("sale.order", "read", [order_id], ["name"])[0].get("name", "")
        log.info("created draft sale order %s (%s) for partner %s", order_id, ref, partner_id)
        return int(order_id), ref
