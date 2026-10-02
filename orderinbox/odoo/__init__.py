"""Odoo backend abstraction.

The pipeline never talks to "Odoo" directly — it talks to a CatalogBackend:
  - LiveOdooBackend: real Odoo over XML-RPC (products/partners/orders)
  - MockOdooBackend: in-memory demo catalog + fake order creation

This is what makes the appliance demo-able in two minutes, and what lets a
deployment start in mock mode and flip to live mode by changing one env var.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class PartnerRecord:
    id: int
    name: str
    email: str = ""
    phone: str = ""

    @property
    def normalized(self) -> str:
        import re
        return re.sub(r"[^a-z0-9 ]", "", self.name.lower()).strip()


@dataclass
class ProductRecord:
    id: int
    default_code: str = ""            # SKU
    name: str = ""
    list_price: float = 0.0
    currency: str = ""
    pack_qty: int = 0                 # must order in multiples of this (0 = any)
    min_qty: float = 0.0
    max_qty: float = 0.0              # 0 = unlimited
    aliases: list[str] = field(default_factory=list)  # legacy codes, customer codes


@dataclass
class OrderLinePayload:
    product_id: int
    quantity: float
    unit_price: float
    name: str = ""
    remarks: str = ""


class CatalogBackend(Protocol):
    name: str

    def get_partners(self) -> list[PartnerRecord]: ...
    def get_products(self) -> list[ProductRecord]: ...
    def create_draft_order(self, partner_id: int, lines: list[OrderLinePayload],
                           reference: str, notes: str) -> tuple[int, str]: ...
