import logging

from odoo import http
from odoo.http import request

log = logging.getLogger(__name__)


class OrderInboxController(http.Controller):
    """Token-authenticated bridge for the OrderInbox AI appliance.

    The appliance may *push* orders here (in addition to the module's own
    pull cron). The token must match the one set in
    Settings → OrderInbox (orderinbox.api_token).
    """

    def _check_token(self, token):
        icp = request.env["ir.config_parameter"].sudo()
        expected = (icp.get_param("orderinbox.api_token") or "").strip()
        if not expected:
            return False
        return (token or "").strip() == expected

    @http.route("/orderinbox/sync", type="json", auth="user", methods=["POST"])
    def sync(self, payload=None, token=None):
        payload = payload or {}
        if not self._check_token(token):
            return {"ok": False, "error": "bad token"}
        orders = payload.get("orders") or [payload]
        model = request.env["orderinbox.order"]
        created = 0
        for item in orders:
            if not item.get("uid"):
                continue
            before = model.search_count([("uid", "=", item["uid"])])
            model._upsert_from_payload(item)
            if not before:
                created += 1
        log.info("OrderInbox push: %d upserted, %d new", len(orders), created)
        return {"ok": True, "upserted": len(orders), "created": created}

    @http.route("/orderinbox/orders", type="json", auth="user", methods=["GET"])
    def orders(self):
        """Let the appliance check how the module sees things (debug aid)."""
        icp = request.env["ir.config_parameter"].sudo()
        if not (icp.get_param("orderinbox.api_token") or "").strip():
            return {"ok": False, "error": "token not configured"}
        recs = request.env["orderinbox.order"].search([], limit=200)
        return {
            "ok": True,
            "orders": [
                {"uid": r.uid, "name": r.name, "status": r.status,
                 "sale_order": r.sale_order_id.name or ""}
                for r in recs
            ],
        }
