"""Incoming orders received from the OrderInbox AI appliance.

This model is the review surface inside Odoo: the appliance watches the
inbox and does the extraction/matching/validation, then pushes results here.
Approving creates a *draft* sale.order — the human-in-the-loop principle
from the product design.
"""
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

log = logging.getLogger(__name__)


class OrderInboxOrderLine(models.Model):
    _name = "orderinbox.order.line"
    _description = "OrderInbox Order Line"
    _order = "line_no, id"

    order_id = fields.Many2one("orderinbox.order", required=True, ondelete="cascade")
    line_no = fields.Integer(default=1)
    sku_as_written = fields.Char(string="SKU (as written)")
    description = fields.Char(string="Description")
    quantity = fields.Float()
    unit = fields.Char(size=16)
    unit_price = fields.Float(string="Quoted price")
    product_id = fields.Many2one("product.product", string="Matched product")
    catalog_price = fields.Float(string="Catalog price")
    match_score = fields.Float(string="Match score")
    match_method = fields.Char(size=16)


class OrderInboxOrder(models.Model):
    _name = "orderinbox.order"
    _description = "OrderInbox Incoming Order"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "received_at desc, id desc"

    _status_selection = [
        ("ready", "Ready for approval"),
        ("exception", "Exception — needs review"),
        ("sent", "Draft created in Odoo"),
        ("rejected", "Rejected"),
        ("failed", "Failed"),
    ]

    uid = fields.Char(string="Appliance UID", required=True, index=True, readonly=True)
    name = fields.Char(string="Subject", required=True)
    sender = fields.Char(string="From")
    received_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    status = fields.Selection(_status_selection, required=True, default="ready",
                              group_expand="_expand_status")
    customer_id = fields.Many2one("res.partner", string="Customer")
    customer_as_written = fields.Char(string="Customer (as written)")
    customer_score = fields.Float(string="Customer match score")
    po_number = fields.Char(string="Customer PO number")
    po_date = fields.Date(string="PO date")
    currency_id = fields.Many2one("res.currency", string="Currency")
    total = fields.Float(string="Total (quoted)")
    confidence = fields.Float(string="Extraction confidence")
    extract_method = fields.Char(size=16)
    payment_terms = fields.Char()
    notes = fields.Text()
    line_ids = fields.One2many("orderinbox.order.line", "order_id", string="Lines")
    issues = fields.Text(string="Validation issues")
    error = fields.Text()
    sale_order_id = fields.Many2one("sale.order", string="Draft sales order", readonly=True)
    appliance_synced = fields.Boolean(string="Synced from appliance")
    line_count = fields.Integer(string="Line count", compute="_compute_line_count")
    match_min_score = fields.Float(
        string="Min SKU match score", compute="_compute_match_min_score")

    @api.depends("line_ids")
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends("line_ids.match_score")
    def _compute_match_min_score(self):
        for rec in self:
            scored = [l.match_score for l in rec.line_ids if l.match_score > 0]
            rec.match_min_score = min(scored) if scored else 0.0

    def _expand_status(self, name, domain, context):
        return [key for key, _label in self._status_selection]

    # ------------------------------------------------------------------
    # sync (called by the cron / controller — data comes from the appliance)
    def _upsert_from_payload(self, payload):
        """Upsert one order record from an appliance JSON payload."""
        uid = payload.get("uid")
        if not uid:
            return self.browse()
        record = self.search([("uid", "=", uid)], limit=1)
        vals = self._vals_from_payload(payload)
        if record:
            record.write(vals)
        else:
            record = self.create(dict(vals, uid=uid))
            record.appliance_synced = True
        return record

    def _vals_from_payload(self, payload):
        currency = payload.get("currency") or ""
        currency_id = False
        if currency:
            cur = self.env["res.currency"].search([("name", "=", currency.upper())], limit=1)
            if cur:
                currency_id = cur.id
        partner_id = False
        if payload.get("customer_matched"):
            partner = self.env["res.partner"].search(
                [("name", "=ilike", payload["customer_matched"])], limit=1)
            if partner:
                partner_id = partner.id
        lines = []
        for l in payload.get("lines") or []:
            product_id = False
            if l.get("sku_matched"):
                prod = self.env["product.product"].search(
                    [("default_code", "=", l["sku_matched"])], limit=1)
                if not prod:
                    prod = self.env["product.product"].search(
                        [("name", "=ilike", l["sku_matched"])], limit=1)
                if prod:
                    product_id = prod.id
            lines.append((0, 0, {
                "line_no": l.get("line_no") or 0,
                "sku_as_written": l.get("sku_as_written") or "",
                "description": l.get("description") or "",
                "quantity": l.get("quantity"),
                "unit": l.get("unit") or "",
                "unit_price": l.get("unit_price"),
                "product_id": product_id,
                "catalog_price": l.get("catalog_price"),
                "match_score": l.get("match_score") or 0.0,
                "match_method": l.get("match_method") or "",
            }))
        issues = "\n".join(
            f"{'[ERROR] ' if i.get('severity') == 'error' else '[warning] '}"
            f"{i.get('code', '')}: {i.get('message', '')}"
            + (f" — {i['suggested_fix']}" if i.get("suggested_fix") else "")
            for i in payload.get("issues") or [])
        status = payload.get("status") or "ready"
        if status in ("received", "parsed", "approved"):
            status = "ready"
        if status not in ("ready", "exception", "sent", "rejected", "failed"):
            status = "ready"
        return {
            "name": payload.get("subject") or payload.get("uid"),
            "sender": payload.get("sender") or "",
            "received_at": payload.get("received_at") or fields.Datetime.now(),
            "status": status,
            "customer_id": partner_id,
            "customer_as_written": payload.get("customer_as_written") or "",
            "customer_score": payload.get("customer_score") or 0.0,
            "po_number": payload.get("po_number") or "",
            "po_date": payload.get("po_date") or False,
            "currency_id": currency_id,
            "total": payload.get("total") or 0.0,
            "confidence": payload.get("confidence") or 0.0,
            "extract_method": payload.get("extract_method") or "",
            "payment_terms": payload.get("payment_terms") or "",
            "notes": payload.get("notes") or "",
            "issues": issues,
            "error": payload.get("error") or "",
            "line_ids": [(5, 0, 0)] + lines,
        }

    def sync_from_appliance(self):
        """Pull the order list from the configured appliance and upsert."""
        icp = self.env["ir.config_parameter"].sudo()
        base = (icp.get_param("orderinbox.appliance_url") or "").strip().rstrip("/")
        token = icp.get_param("orderinbox.api_token") or ""
        if not base:
            return 0
        try:
            resp = requests.get(f"{base}/api/orders",
                                params={"token": token}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            log.warning("OrderInbox sync failed (appliance at %s unreachable)", base)
            return 0
        created = 0
        for payload in data.get("orders", []):
            before = self.search_count([("uid", "=", payload.get("uid"))])
            self._upsert_from_payload(payload)
            if not before:
                created += 1
        if created:
            log.info("OrderInbox sync: %d new orders from appliance", created)
        return created

    # ------------------------------------------------------------------
    # actions
    def action_approve(self):
        """Create the draft sales order.

        If an appliance is configured, delegate to it (it owns the
        validation context and creates the draft through its own Odoo
        connection). Otherwise build the draft directly in Odoo from the
        matched lines.
        """
        self.ensure_one()
        if self.status == "sent":
            return self
        icp = self.env["ir.config_parameter"].sudo()
        base = (icp.get_param("orderinbox.appliance_url") or "").strip().rstrip("/")
        token = icp.get_param("orderinbox.api_token") or ""
        if base:
            try:
                resp = requests.post(f"{base}/api/orders/{self.uid}/approve",
                                     params={"token": token}, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                self.status = "sent"
                if data.get("sale_order_id"):
                    sale = self.env["sale.order"].browse(int(data["sale_order_id"]))
                    if sale.exists():
                        self.sale_order_id = sale.id
                self.message_post(body=_(
                    "Approved — draft order %(ref)s created via OrderInbox appliance.",
                    ref=data.get("reference") or ""))
                return self
            except Exception as exc:
                log.warning("appliance approve failed, falling back to in-Odoo draft: %s", exc)
        # in-Odoo draft creation
        matched = self.line_ids.filtered("product_id")
        if not matched:
            raise UserError(_(
                "No lines have a matched product. Assign products to the lines "
                "or resolve them in the appliance before approving."))
        skipped = len(self.line_ids) - len(matched)
        so = self.env["sale.order"].create({
            "partner_id": self.customer_id.id or self.env["res.partner"].search([], limit=1).id,
            "client_order_ref": self.po_number,
            "currency_id": self.currency_id.id,
            "note": _("Created by OrderInbox AI from “%s” (customer PO %s).")
                     % (self.name, self.po_number or "n/a"),
            "order_line": [
                (0, 0, {
                    "product_id": l.product_id.id,
                    "product_uom_qty": l.quantity or 0,
                    "price_unit": l.unit_price if l.unit_price else l.product_id.list_price,
                    "name": l.description or l.sku_as_written,
                }) for l in matched
            ],
        })
        self.sale_order_id = so.id
        self.status = "sent"
        msg = _("Approved — draft %(ref)s created in Odoo.", ref=so.name)
        if skipped:
            msg += " " + _("%d line(s) without a matched product were skipped.", skipped)
        self.message_post(body=msg)
        return self

    def action_reject(self, reason=""):
        self.ensure_one()
        self.status = "rejected"
        self.error = reason
        self.message_post(body=reason or _("Rejected by reviewer."))
        return self

    def action_resync(self):
        """Re-pull this order's current state from the appliance."""
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        base = (icp.get_param("orderinbox.appliance_url") or "").strip().rstrip("/")
        token = icp.get_param("orderinbox.api_token") or ""
        if not base:
            raise UserError(_("No appliance configured (Settings → OrderInbox)."))
        try:
            resp = requests.get(
                f"{base}/api/orders/{self.uid}", params={"token": token}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            payload = data.get("order") if isinstance(data, dict) else None
        except Exception as exc:
            raise UserError(_("Appliance unreachable: %s") % exc) from exc
        if not payload:
            raise UserError(_("Order not found on the appliance."))
        self._upsert_from_payload(payload)
        return self
