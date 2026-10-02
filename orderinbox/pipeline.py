"""The pipeline: inbox document → draft Odoo order.

Stages (mirrors the architecture diagram in the business plan):
    document parser → structured order → customer matching → product matching
    → business rules → READY (awaiting approval) | EXCEPTION (review queue)

Approval is always a human action in the UI; the pipeline never writes to
Odoo on its own. That is the "draft" principle: AI automates the work,
deterministic validation and approval handle the consequential step.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Settings
from .db import Store
from .extract import LLMClient, extract_document, extract_order
from .match import match_customer, match_customer_by_email, match_product, llm_pick
from .models import Extraction, MatchMethod, OrderStatus, ProcessedOrder
from .odoo import CatalogBackend, OrderLinePayload
from .rules import RuleContext, validate

log = logging.getLogger("orderinbox.pipeline")

_METHODS = {m.value: m for m in MatchMethod}


def _method(value: str) -> MatchMethod:
    return _METHODS.get(value, MatchMethod.NONE)


def _utcnow() -> datetime:
    """Naive UTC timestamp (kept naive so SQLite/JSON round-trips stay simple)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Pipeline:
    def __init__(self, settings: Settings, backend: CatalogBackend, llm: LLMClient, store: Store):
        self.settings = settings
        self.backend = backend
        self.llm = llm
        self.store = store
        self._partners = None
        self._products = None
        self._products_by_code: dict[str, object] = {}

    # ------------------------------------------------------------------
    # catalog
    def catalog(self) -> tuple[list, list]:
        if self._partners is None:
            self._partners = self.backend.get_partners()
            self._products = self.backend.get_products()
            for p in self._products:
                if p.default_code:
                    self._products_by_code[p.default_code.strip().upper()] = p
        return self._partners, self._products

    def refresh_catalog(self) -> None:
        self._partners = None
        self.catalog()

    # ------------------------------------------------------------------
    # processing
    def process_message(self, eml_path: str | Path, subject: str = "",
                        sender: str = "", received_at: Optional[datetime] = None) -> ProcessedOrder:
        """Process one .eml (with its spool directory for attachments)."""
        eml_path = Path(eml_path)
        doc = extract_document(eml_path, spool_dir=eml_path.parent)
        if not subject:
            subject = doc.subject
        if not sender:
            sender = doc.sender
        docs = [doc] + [extract_document(a, spool_dir=eml_path.parent) for a in doc.attachments]
        return self._process_docs(docs, subject=subject or Path(eml_path).name,
                                  sender=sender, received_at=received_at,
                                  stored_path=str(eml_path),
                                  uid=self._uid_for(eml_path, subject, sender))

    def process_file(self, path: str | Path) -> ProcessedOrder:
        """Process a standalone document (demo / manual import)."""
        path = Path(path)
        doc = extract_document(path, spool_dir=path.parent)
        uid = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        received = datetime.fromtimestamp(path.stat().st_mtime)
        return self._process_docs([doc], subject=path.name, sender="manual import",
                                  received_at=received, stored_path=str(path), uid=uid)

    def _process_docs(self, docs, *, subject: str, sender: str,
                      received_at: Optional[datetime], stored_path: str,
                      uid: str) -> ProcessedOrder:
        order = ProcessedOrder(
            uid=uid, source_type="email" if any(d.kind == "eml" for d in docs) else "file",
            subject=subject, sender=sender,
            received_at=received_at or _utcnow(),
            stored_path=stored_path, status=OrderStatus.RECEIVED,
        )
        order.add_log(f"received: {subject} ({sender or 'n/a'})")
        try:
            extraction = self._extract(docs, order)
            if extraction is None:
                order.status = OrderStatus.REJECTED
                order.error = "Not an order (or unreadable)."
                order.add_log("skipped: does not look like a purchase order")
            else:
                extraction.source = subject
                self._match_customer(extraction, order)
                self._match_products(extraction, order)
                self._validate(extraction, order)
                order.status = (OrderStatus.EXCEPTION
                                if any(i.severity.value == "error" for i in order.issues)
                                else OrderStatus.READY)
                order.add_log(f"done: {len(extraction.lines)} lines, "
                              f"customer={extraction.customer_matched or '?'} "
                              f"({extraction.customer_match_score:.0f}), "
                              f"status={order.status.value}")
            order.processed_at = _utcnow()
        except Exception as exc:
            log.exception("pipeline failed for %s", uid)
            order.status = OrderStatus.FAILED
            order.error = str(exc)
            order.add_log(f"FAILED: {exc}")
        self.store.upsert_order(order)
        return order

    # ------------------------------------------------------------------
    def _extract(self, docs, order: ProcessedOrder) -> Optional[Extraction]:
        extraction = extract_order(docs, self.llm, self.settings, subject=order.subject)
        if extraction is None:
            return None
        order.extraction = extraction
        order.add_log(f"parsed: {len(extraction.lines)} lines via {extraction.extract_method} "
                      f"(confidence {extraction.confidence:.0%})"
                      + (" — OCR used" if next((d.ocr_used for d in docs if d.ocr_used), False) else ""))
        return extraction

    def _match_customer(self, extraction: Extraction, order: ProcessedOrder) -> None:
        partners, _ = self.catalog()
        # 1) sender email domain
        if order.sender:
            m = match_customer_by_email(order.sender, partners)
            if m.partner:
                extraction.customer_matched = m.partner.name
                extraction.customer_match_score = m.score
                extraction.customer_match_method = _method(m.method)
                extraction.customer_name_as_written = m.partner.name
                order.add_log(f"customer from sender domain: {m.partner.name}")
                return
        # 2) name as written in the document
        if extraction.customer_name_as_written:
            m = match_customer(extraction.customer_name_as_written, partners)
            if m.partner:
                extraction.customer_matched = m.partner.name
                extraction.customer_match_score = m.score
                extraction.customer_match_method = _method(m.method)
                order.add_log(f"customer matched '{m.partner.name}' score {m.score:.0f} ({m.method})")
                return
        order.add_log("no customer match")

    def _match_products(self, extraction: Extraction, order: ProcessedOrder) -> None:
        _, products = self.catalog()
        n_matched = 0
        for line in extraction.lines:
            pm = match_product(line.sku_as_written, products)
            if pm.product is None and (line.sku_as_written or line.description):
                # second chance with description text, then LLM
                dm = match_product(line.description[:60], products)
                if dm.product is not None and dm.score >= self.settings.low_match_threshold:
                    pm = dm
            if pm.product is None and line.description:
                pm = llm_pick(line.sku_as_written, line.description, products, self.llm)
            if pm.product is not None:
                line.sku_matched = pm.product.default_code
                line.product_name = pm.product.name
                line.match_score = pm.score
                line.match_method = _method(pm.method)
                line.catalog_price = pm.product.list_price
                n_matched += 1
            else:
                line.match_score = pm.score
                line.match_method = _method("none")
        order.add_log(f"sku matching: {n_matched}/{len(extraction.lines)} lines matched")

    def _validate(self, extraction: Extraction, order: ProcessedOrder) -> None:
        seen = self.store.seen_po_keys()
        ctx = RuleContext(
            settings=self.settings,
            products_by_code=self._products_by_code,
            seen_po_keys=seen,
        )
        order.issues = validate(extraction, ctx)
        for i in order.issues:
            order.add_log(f"{i.severity.value.upper()} {i.code}: {i.message}"
                          + (f" → {i.suggested_fix}" if i.suggested_fix else ""))

    # ------------------------------------------------------------------
    # approval → Odoo
    def approve_and_send(self, order: ProcessedOrder) -> ProcessedOrder:
        """Create the draft sales order in Odoo for an approved order."""
        extraction = order.extraction
        if not extraction:
            raise ValueError("order has no extracted data")
        partners, _ = self.catalog()
        partner = next((p for p in partners if p.name == extraction.customer_matched), None)
        if partner is None:
            from .match import match_customer
            m = match_customer(extraction.customer_name_as_written or extraction.customer_matched, partners)
            partner = m.partner
        if partner is None:
            raise ValueError(f"customer '{extraction.customer_matched}' not found in Odoo")
        lines = []
        for l in extraction.lines:
            if not l.sku_matched:
                raise ValueError(f"line {l.line_no} has no matched product — resolve it first")
            prod = self._products_by_code.get(l.sku_matched.strip().upper())
            if prod is None:
                raise ValueError(f"product '{l.sku_matched}' not found in catalog")
            unit_price = l.unit_price if l.unit_price is not None else prod.list_price
            lines.append(OrderLinePayload(
                product_id=prod.id,
                quantity=l.quantity or 0,
                unit_price=unit_price,
                name=l.description,
                remarks=l.remarks,
            ))
        notes = (f"Auto-drafted by OrderInbox AI from {order.subject or 'email'} "
                 f"(PO {extraction.po_number or 'n/a'}, received {order.received_at:%Y-%m-%d}).")
        order_id, reference = self.backend.create_draft_order(
            partner.id, lines, extraction.po_number or order.subject, notes)
        order.odoo_order_id = order_id
        order.odoo_reference = reference
        order.status = OrderStatus.SENT
        order.add_log(f"sent: draft {reference} created in {self.backend.name} odoo")
        self.store.upsert_order(order)
        return order

    def reject(self, order: ProcessedOrder, reason: str = "") -> ProcessedOrder:
        order.status = OrderStatus.REJECTED
        order.error = reason or "Rejected by reviewer"
        order.add_log(f"rejected: {reason or 'no reason'}")
        self.store.upsert_order(order)
        return order

    # ------------------------------------------------------------------
    @staticmethod
    def _uid_for(path: Path, subject: str, sender: str) -> str:
        try:
            return "eml:" + hashlib.sha256(
                (subject + "|" + sender + "|" + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:16]
        except OSError:
            return "eml:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
