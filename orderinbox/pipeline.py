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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ai.escalation import EscalationPolicy, EscalationThresholds, Tier
from .config import Settings
from .db import Store
from .extract import LLMClient, extract_document, extract_order
from .freight import AliasStore, FreightContext, normalize_quote, validate_quote
from .freight.extract import classify_freight_text, extract_freight
from .freight.models import FreightCase, FreightStatus, RFQRequest
from .freight.ontology import lane_matches
from .freight.quote import FreightQuote, total_cost
from .match import match_customer, match_customer_by_email, match_product, llm_pick
from .models import Extraction, MatchMethod, OrderStatus, ProcessedOrder
from .odoo import CatalogBackend, OrderLinePayload
from .rules import RuleContext, validate

log = logging.getLogger("orderinbox.pipeline")

_METHODS = {m.value: m for m in MatchMethod}


def _method(value: str) -> MatchMethod:
    return _METHODS.get(value, MatchMethod.NONE)


def _counterparty_from_sender(sender: str) -> str:
    """Best-effort counterparty (agent/carrier) name from a sender address.

    A display name wins; a bare address falls back to the domain's first
    label ("quotes@shanghaiabc.com" -> "Shanghaiabc"). Alias learning
    (AliasStore) refines these into real counterparty names over time.
    """
    if not sender:
        return ""
    name = sender.split("<")[0].strip()
    if name and "@" not in name:
        return name
    m = re.search(r"@([a-z0-9.-]+)", sender.lower())
    if m:
        label = m.group(1).split(".")[0]
        return re.sub(r"[-_]+", " ", label).title()
    return ""


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
        # --- model-gateway policy objects (the "Strata integration") --------
        self.escalation = EscalationPolicy(EscalationThresholds(
            auto=settings.escalate_auto,
            verify=settings.escalate_verify,
            cloud=settings.escalate_cloud,
        ))
        self.alias_store = AliasStore(settings.data_dir / "alias_store.json")

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
                        sender: str = "", received_at: Optional[datetime] = None):
        """Process one .eml (with its spool directory for attachments).

        The message is triaged first: freight RFQs and agent quotes route to
        the freight workflow; everything else goes to the order pipeline.
        Returns a FreightCase or a ProcessedOrder.
        """
        eml_path = Path(eml_path)
        doc = extract_document(eml_path, spool_dir=eml_path.parent)
        if not subject:
            subject = doc.subject
        if not sender:
            sender = doc.sender
        docs = [doc] + [extract_document(a, spool_dir=eml_path.parent) for a in doc.attachments]
        subject = subject or Path(eml_path).name
        kind = classify_freight_text("\n\n".join([subject] + [d.text for d in docs if d.text]))
        if kind:
            return self._process_freight(
                docs, kind=kind, subject=subject, sender=sender,
                received_at=received_at, stored_path=str(eml_path),
                uid=self._uid_for(eml_path, subject, sender))
        return self._process_docs(docs, subject=subject,
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
        self._apply_escalation(docs, extraction, order)
        return extraction

    # ------------------------------------------------------------------
    # model-gateway policy: escalation + producer/verifier
    def _apply_escalation(self, docs, extraction: Extraction, order: ProcessedOrder) -> None:
        """Apply the confidence-tier escalation (Section 10) to an extraction.

        The tier is always logged (audit). The producer/verifier second pass
        (Section 11) runs only for the VERIFY band, only when a model is
        available, and only when the deployment has it enabled — so the
        deterministic demo path is untouched.
        """
        decision = self.escalation.tier_for(extraction.confidence)
        order.add_log(f"escalation: {decision.tier.value} — {decision.reason}")
        if decision.tier is not Tier.VERIFY:
            return
        if not (self.settings.use_producer_verifier and self.llm.available()):
            return
        agree, diffs = self._producer_verifier(docs, extraction)
        if agree:
            order.add_log("producer/verifier: second model agreed — auto-accepted")
        else:
            order.add_log(f"producer/verifier: disagreed on {diffs} — routed for review")
            extraction.confidence = min(extraction.confidence, self.settings.escalate_verify)

    def _producer_verifier(self, docs, extraction: Extraction) -> tuple[bool, list[str]]:
        """Ask a (stronger) model to independently re-derive the header fields
        and compare against the first pass. Agreement is a cheap stability
        check; disagreement flags the record up the escalation ladder."""
        from .extract.llm import HEADER_EXTRACTION_PROMPT
        # Re-ask the model for just the header on the same documents.
        text = "\n\n".join((d.text or "") for d in docs)
        try:
            check = self.llm.extract_json(
                "You are an order-entry data extractor. Verify by re-extracting.",
                HEADER_EXTRACTION_PROMPT + text[:60000],
                task="order_verification", complexity="high")
        except Exception as exc:
            log.warning("producer/verifier second pass failed: %s", exc)
            return True, []
        a = {
            "customer_name": extraction.customer_name_as_written,
            "po_number": extraction.po_number,
            "currency": extraction.currency,
            "payment_terms": extraction.payment_terms,
        }
        b = {
            "customer_name": str(check.get("customer_name") or ""),
            "po_number": str(check.get("po_number") or ""),
            "currency": str(check.get("currency") or ""),
            "payment_terms": str(check.get("payment_terms") or ""),
        }
        from .ai.verifier import compare
        verdict = compare(a, b, list(a.keys()))
        return verdict.agree, verdict.diff_fields

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
    # freight (RateScout) domain
    def freight_context(self, sell_rate: Optional[float] = None) -> FreightContext:
        """A validation context for freight quotes, wired to the settings."""
        return FreightContext(
            allowed_currencies=self.settings.currencies(),
            margin_threshold_pct=self.settings.freight_margin_threshold_pct,
            sell_rate=sell_rate,
        )

    def process_freight_quote(self, raw: dict, sell_rate: Optional[float] = None,
                              currency: str = "USD") -> tuple[FreightQuote, list]:
        """Normalize a raw/messy freight quote and run the deterministic
        freight validation over it.

        This is the freight-domain counterpart of `_process_docs`: the model
        (upstream, via the gateway) interprets the document into `raw`; here
        the ontology normalizes it and the rules engine decides if it is safe.
        Deterministic code — not the model — is the final authority.
        """
        quote = normalize_quote(raw or {})
        issues = validate_quote(quote, self.freight_context(sell_rate=sell_rate))
        return quote, issues

    # ------------------------------------------------------------------
    # freight workflow: RFQ intake, quote intake, compare, approval
    def _process_freight(self, docs, *, kind: str, subject: str, sender: str,
                        received_at: Optional[datetime], stored_path: str,
                        uid: str) -> FreightCase:
        case = FreightCase(
            uid=uid, kind=kind, status=FreightStatus.RECEIVED,
            subject=subject, sender=sender,
            received_at=received_at or _utcnow(), stored_path=stored_path,
        )
        case.add_log(f"received: {subject} ({sender or 'n/a'}) [freight:{kind}]")
        try:
            if kind == "rfq":
                self._process_rfq(docs, case)
            else:
                self._process_quote(docs, case)
            case.processed_at = _utcnow()
        except Exception as exc:
            log.exception("freight processing failed for %s", uid)
            case.status = FreightStatus.FAILED
            case.error = str(exc)
            case.add_log(f"FAILED: {exc}")
        self.store.upsert_freight(case)
        if kind == "quote" and case.rfq_uid:
            self._refresh_siblings(case)
        return case

    def _refresh_siblings(self, case: FreightCase) -> None:
        """When a new quote lands on an RFQ, re-rank the recommendation text
        of the other quotes on that RFQ so the whole lane reads consistently."""
        for sib in self.store.list_freight(kind="quote", limit=500):
            if sib.rfq_uid == case.rfq_uid and sib.uid != case.uid:
                sib.recommendation = self._compare_siblings(sib)
                self.store.upsert_freight(sib)

    def _process_rfq(self, docs, case: FreightCase) -> None:
        """RFQ intake: extract the request, match the customer, flag what is
        missing. Preparing the agent RFQs is an automatic action; actually
        sending them is the approval step (Section 18/19)."""
        combined = "\n\n".join([case.subject] + [d.text for d in docs if d.text])
        raw, method = extract_freight(combined, "rfq", self.llm, self.settings)
        rfq = RFQRequest(
            customer_name_as_written=str(raw.get("customer_name") or ""),
            origin_port=str(raw.get("origin_port") or ""),
            destination_port=str(raw.get("destination_port") or ""),
            equipment=str(raw.get("equipment") or ""),
            container_count=int(raw.get("container_count") or 1),
            ready_date=str(raw.get("ready_date") or ""),
            free_demurrage_days=int(raw.get("free_demurrage_days") or 0),
            free_detention_days=int(raw.get("free_detention_days") or 0),
            cargo=str(raw.get("cargo") or ""),
            dangerous_goods=bool(raw.get("dangerous_goods") or False),
            incoterm=str(raw.get("incoterm") or ""),
            budget_total=float(raw.get("budget_total") or 0.0),
            budget_currency=str(raw.get("budget_currency") or ""),
            notes=str(raw.get("notes") or ""),
            confidence=float(raw.get("confidence") or 0.0),
            extract_method=method,
        )
        # normalize ports/equipment to canonical codes
        from .freight import ontology
        if rfq.origin_port:
            p = ontology.lookup_port(rfq.origin_port)
            rfq.origin_port = p.un_locode if p else rfq.origin_port.upper()
        if rfq.destination_port:
            p = ontology.lookup_port(rfq.destination_port)
            rfq.destination_port = p.un_locode if p else rfq.destination_port.upper()
        if rfq.equipment:
            rfq.equipment = ontology.normalize_equipment(rfq.equipment) or rfq.equipment.upper()

        # customer match: sender domain first, then name as written
        partners, _ = self.catalog()
        if case.sender:
            m = match_customer_by_email(case.sender, partners)
            if m.partner:
                rfq.customer_matched = m.partner.name
                rfq.customer_match_score = m.score
                rfq.customer_name_as_written = m.partner.name
                case.add_log(f"customer from sender domain: {m.partner.name}")
        if not rfq.customer_matched and rfq.customer_name_as_written:
            m = match_customer(rfq.customer_name_as_written, partners)
            if m.partner:
                rfq.customer_matched = m.partner.name
                rfq.customer_match_score = m.score
                case.add_log(f"customer matched '{m.partner.name}' score {m.score:.0f}")
        if not rfq.customer_matched:
            from .models import Issue, Severity
            rfq_issues = [Issue(severity=Severity.ERROR, code="MISSING_CUSTOMER",
                                message="No matching customer found for this RFQ.",
                                suggested_fix="Pick the customer in the review UI.")]
            case.issues.extend(rfq_issues)

        case.rfq = rfq
        case.lane = rfq.lane
        case.equipment = rfq.equipment
        case.container_count = rfq.container_count
        case.status = FreightStatus.PARSED
        case.add_log(f"rfq extracted via {method}: {rfq.lane} {rfq.equipment} x{rfq.container_count}"
                     + (f", ready {rfq.ready_date}" if rfq.ready_date else "")
                     + (f", budget {rfq.budget_total:.0f} {rfq.budget_currency}" if rfq.budget_total else ""))

        # completeness gate
        if not (rfq.origin_port and rfq.destination_port):
            from .models import Issue, Severity
            case.issues.append(Issue(
                severity=Severity.ERROR, code="NO_LANE",
                message="RFQ has no resolvable origin/destination lane.",
                suggested_fix="Ask the customer for the ports."))
        if not rfq.equipment:
            from .models import Issue, Severity
            case.issues.append(Issue(
                severity=Severity.WARNING, code="NO_EQUIPMENT",
                message="RFQ does not state the equipment type.",
                suggested_fix="Confirm container type with the customer."))
        if rfq.dangerous_goods:
            from .models import Issue, Severity
            case.issues.append(Issue(
                severity=Severity.WARNING, code="DG_CARGO",
                message="Dangerous-goods cargo — DG handling assumptions need approval (Section 19)."))

        if any(i.severity.value == "error" for i in case.issues):
            case.status = FreightStatus.EXCEPTION
        else:
            case.status = FreightStatus.READY
        n_agents = 3
        case.add_log(f"agent RFQs prepared (draft, {n_agents} agents) — send on approval")
        case.add_log(f"status={case.status.value}")

    def _process_quote(self, docs, case: FreightCase) -> None:
        """Quote intake: normalize, validate, link to the open RFQ, compare
        against sibling quotes, compute margin, recommend."""
        combined = "\n\n".join([case.subject] + [d.text for d in docs if d.text])
        raw, method = extract_freight(combined, "quote", self.llm, self.settings)
        quote = normalize_quote(raw)
        if not quote.counterparty:
            quote.counterparty = _counterparty_from_sender(case.sender)
        case.quote = quote
        case.counterparty = quote.counterparty
        case.lane = f"{quote.origin_port} -> {quote.destination_port}"
        case.equipment = quote.equipment

        # link to an open RFQ on the same lane (hub-aware)
        rfq_case = self._find_matching_rfq(quote)
        if rfq_case is not None:
            case.rfq_uid = rfq_case.uid
            if rfq_case.rfq:
                case.container_count = rfq_case.rfq.container_count

        # pricing (deterministic)
        currency = quote.base_freight.currency or next(
            (c.currency for c in quote.charges if c.currency), "USD")
        case.quote_currency = currency
        case.buy_cost = total_cost(quote, currency)

        # margin against the customer's budget when the RFQ has one
        budget = budget_currency = None
        if rfq_case is not None and rfq_case.rfq and rfq_case.rfq.budget_total:
            budget = rfq_case.rfq.budget_total
            budget_currency = rfq_case.rfq.budget_currency or currency
        from .freight import fx
        sell = None
        if budget and budget_currency == currency:
            sell = budget
        elif budget:
            try:
                sell = fx.convert(budget, budget_currency, currency)
            except ValueError:
                sell = None
        if sell:
            case.sell_total = round(sell, 2)
            m = (sell - case.buy_cost) / case.buy_cost * 100.0 if case.buy_cost else None
            case.margin_pct = round(m, 2) if m is not None else None
        else:
            # no budget: recommend a sell at the policy margin
            thr = self.settings.freight_margin_threshold_pct
            case.sell_total = round(case.buy_cost * (1 + thr / 100.0), 2) if case.buy_cost else None
            case.margin_pct = thr

        # deterministic validation (Section 9) — the margin gate fires here
        # against the proposed sell (customer budget when the RFQ has one,
        # otherwise the policy-margin recommendation)
        ctx = self.freight_context(sell_rate=case.sell_total)
        case.issues.extend(validate_quote(quote, ctx))

        # comparison against sibling quotes on the same RFQ
        case.recommendation = self._compare_siblings(case)

        if any(i.severity.value == "error" for i in case.issues):
            case.status = FreightStatus.EXCEPTION
        else:
            case.status = FreightStatus.READY
        case.add_log(f"quote normalized via {method}: {case.lane} {quote.equipment} "
                     f"cost {case.buy_cost:.0f} {currency}"
                     + (f" (from {quote.counterparty})" if quote.counterparty else ""))
        if case.rfq_uid:
            case.add_log(f"linked to RFQ {case.rfq_uid}")
        case.add_log(f"sell {case.sell_total:.0f} {currency}, margin {case.margin_pct:.1f}%"
                     if case.sell_total and case.margin_pct is not None else
                     f"sell {case.sell_total} {currency}")
        if case.recommendation:
            case.add_log(f"recommendation: {case.recommendation}")
        case.add_log(f"status={case.status.value}")

    def _find_matching_rfq(self, quote: FreightQuote) -> Optional[FreightCase]:
        for rfq in self.store.open_rfqs():
            if not rfq.rfq:
                continue
            if lane_matches(quote.origin_port, quote.destination_port,
                            rfq.rfq.origin_port, rfq.rfq.destination_port):
                if not quote.equipment or not rfq.rfq.equipment \
                        or quote.equipment.upper() == rfq.rfq.equipment.upper():
                    return rfq
        return None

    def _compare_siblings(self, case: FreightCase) -> str:
        """Rank this quote against siblings on the same RFQ (cheapest first)."""
        if not case.rfq_uid:
            return "only quote on this lane (no open RFQ link)"
        siblings = [c for c in self.store.list_freight(kind="quote", limit=500)
                    if c.rfq_uid == case.rfq_uid and c.uid != case.uid]
        allq = [case] + siblings
        rankable = [c for c in allq if c.buy_cost]
        if not rankable:
            return ""
        rankable.sort(key=lambda c: c.buy_cost)
        best = rankable[0]
        n = len(rankable)
        if n == 1:
            return f"only quote received on this lane"
        pos = [i for i, c in enumerate(rankable) if c.uid == case.uid][0] + 1
        best_cp = best.counterparty or f"quote #{best.id}"
        best_cost = best.buy_cost
        cur = best.quote_currency if best.quote else (case.quote_currency or "")
        if pos == 1:
            return (f"lowest of {n} quotes on this lane "
                    f"({best_cp} at {best_cost:.0f} {cur})")
        return (f"ranked {pos} of {n} on this lane; lowest is {best_cp} "
                f"at {best_cost:.0f} {cur}")

    def approve_freight(self, case: FreightCase) -> FreightCase:
        """Human-approved action for a freight case.

        RFQ  -> mark SENT (agent RFQs dispatched).
        Quote -> mark SENT (customer quote issued).
        """
        if case.kind == "rfq":
            case.status = FreightStatus.SENT
            case.add_log("approved: agent RFQs dispatched")
        else:
            case.status = FreightStatus.SENT
            case.add_log(f"approved: customer quote issued "
                         f"(sell {case.sell_total:.0f} {case.quote_currency})"
                         if case.sell_total else "approved: customer quote issued")
        self.store.upsert_freight(case)
        return case

    def reject_freight(self, case: FreightCase, reason: str = "") -> FreightCase:
        case.status = FreightStatus.REJECTED
        case.error = reason or "Rejected by reviewer"
        case.add_log(f"rejected: {reason or 'no reason'}")
        self.store.upsert_freight(case)
        return case

    # ------------------------------------------------------------------
    @staticmethod
    def _uid_for(path: Path, subject: str, sender: str) -> str:
        try:
            return "eml:" + hashlib.sha256(
                (subject + "|" + sender + "|" + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:16]
        except OSError:
            return "eml:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
