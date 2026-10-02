"""Mock Odoo backend: in-memory catalog from a JSON seed + fake draft orders.

This is the two-minute demo path: the full pipeline (parse → match → validate
→ draft order) runs end-to-end with zero external dependencies, and every
draft "order" is recorded so the UI shows real results.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from . import OrderLinePayload, PartnerRecord, ProductRecord

log = logging.getLogger("orderinbox.odoo.mock")

BUILTIN_SEED = Path(__file__).resolve().parents[2] / "demo" / "seed.json"


class MockOdooBackend:
    name = "mock"

    def __init__(self, seed_file: Optional[str] = None):
        path = Path(seed_file) if seed_file else BUILTIN_SEED
        if not path.exists():
            raise FileNotFoundError(f"seed file not found: {path}")
        seed = json.loads(path.read_text())
        self.partners = [
            PartnerRecord(id=p["id"], name=p["name"], email=p.get("email", ""), phone=p.get("phone", ""))
            for p in seed.get("partners", [])
        ]
        self.products = [
            ProductRecord(
                id=p["id"],
                default_code=p.get("default_code", ""),
                name=p.get("name", ""),
                list_price=float(p.get("list_price", 0)),
                currency=p.get("currency", "USD"),
                pack_qty=int(p.get("pack_qty", 0)),
                min_qty=float(p.get("min_qty", 0)),
                max_qty=float(p.get("max_qty", 0)),
                aliases=list(p.get("aliases", [])),
            )
            for p in seed.get("products", [])
        ]
        self._next_order_id = 7000
        self.created_orders: list[dict] = []

    def get_partners(self) -> list[PartnerRecord]:
        return list(self.partners)

    def get_products(self) -> list[ProductRecord]:
        return list(self.products)

    def create_draft_order(self, partner_id: int, lines: list[OrderLinePayload],
                           reference: str, notes: str) -> tuple[int, str]:
        self._next_order_id += 1
        partner = next((p for p in self.partners if p.id == partner_id), None)
        total = sum(l.quantity * l.unit_price for l in lines)
        record = {
            "id": self._next_order_id,
            "reference": f"SO{self._next_order_id:05d}",
            "partner": partner.name if partner else f"partner#{partner_id}",
            "po_reference": reference,
            "notes": notes,
            "state": "draft",
            "amount_total": round(total, 2),
            "lines": [
                {"product_id": l.product_id, "name": l.name, "qty": l.quantity, "price": l.unit_price}
                for l in lines
            ],
        }
        self.created_orders.append(record)
        log.info("MOCK created draft %s for %s (%d lines, %.2f)",
                 record["reference"], record["partner"], len(lines), total)
        return self._next_order_id, record["reference"]
