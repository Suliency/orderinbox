from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-standard", "-at_install")
class TestOrderInbox(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Acme Hardware"})
        cls.product = cls.env["product.product"].create({
            "name": "Hex Bolt M8",
            "default_code": "HB-M8",
            "list_price": 0.42,
            "type": "consu",
            "taxes_id": False,  # deterministic totals, independent of company chart
        })

    def _make_order(self, **overrides):
        vals = {
            "uid": "eml:test-1234567890abcd",
            "name": "PO-2026-001 — Acme Hardware",
            "sender": "orders@acmehardware.example",
            "customer_id": self.partner.id,
            "customer_as_written": "Acme Hardware",
            "customer_score": 100.0,
            "po_number": "PO-2026-001",
            "currency_id": self.env.ref("base.USD").id,
            "total": 12.60,
            "line_ids": [
                (0, 0, {
                    "line_no": 1,
                    "sku_as_written": "HB-M8",
                    "description": "Hex bolt M8",
                    "quantity": 30,
                    "unit_price": 0.42,
                    "product_id": self.product.id,
                    "catalog_price": 0.42,
                    "match_score": 100.0,
                    "match_method": "exact",
                }),
            ],
        }
        vals.update(overrides)
        return self.env["orderinbox.order"].create(vals)

    def test_approve_creates_draft_sales_order(self):
        order = self._make_order()
        self.assertEqual(order.status, "ready")
        order.action_approve()
        self.assertEqual(order.status, "sent")
        self.assertTrue(order.sale_order_id)
        self.assertEqual(order.sale_order_id.state, "draft")
        self.assertEqual(order.sale_order_id.partner_id, self.partner)
        self.assertEqual(order.sale_order_id.client_order_ref, "PO-2026-001")
        self.assertEqual(len(order.sale_order_id.order_line), 1)
        line = order.sale_order_id.order_line
        self.assertEqual(line.product_id, self.product)
        self.assertEqual(line.product_uom_qty, 30)
        self.assertAlmostEqual(order.sale_order_id.amount_total, 12.60)

    def test_approve_is_idempotent(self):
        order = self._make_order()
        order.action_approve()
        first = order.sale_order_id.id
        order.action_approve()
        self.assertEqual(order.sale_order_id.id, first)

    def test_approve_without_product_raises(self):
        order = self._make_order(line_ids=[(0, 0, {
            "line_no": 1, "sku_as_written": "???", "quantity": 1,
            "product_id": False, "match_score": 12.0,
        })])
        with self.assertRaises(Exception):
            order.action_approve()
        self.assertEqual(order.status, "ready")

    def test_reject(self):
        order = self._make_order()
        order.action_reject(reason="wrong quantity on line 1")
        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.error, "wrong quantity on line 1")

    def test_upsert_from_payload(self):
        payload = {
            "uid": "eml:from-appliance-1",
            "subject": "PO-999",
            "sender": "buyer@example.com",
            "received_at": "2026-09-30 14:00:00",
            "status": "exception",
            "customer_matched": "Acme Hardware",
            "customer_score": 91.0,
            "po_number": "PO-999",
            "currency": "USD",
            "total": 500.0,
            "confidence": 0.9,
            "issues": [
                {"severity": "error", "code": "PACK_MULTIPLE",
                 "message": "qty 25 not a multiple of pack 12", "line": 2,
                 "suggested_fix": "order 24 or 36"},
            ],
            "lines": [
                {"line_no": 1, "sku_as_written": "HB-M8", "description": "Hex bolt M8",
                 "quantity": 25, "unit_price": 0.40, "sku_matched": "HB-M8",
                 "match_score": 100.0, "match_method": "exact",
                 "catalog_price": 0.42},
            ],
        }
        model = self.env["orderinbox.order"]
        before = model.search_count([("uid", "=", payload["uid"])])
        self.assertEqual(before, 0)
        rec = model._upsert_from_payload(payload)
        self.assertFalse(before)
        self.assertTrue(rec)
        self.assertEqual(rec.status, "exception")
        self.assertEqual(rec.customer_id, self.partner)
        self.assertTrue("PACK_MULTIPLE" in rec.issues)
        self.assertEqual(rec.line_count, 1)
        # upsert again: same record, no duplicate
        rec2 = model._upsert_from_payload(payload)
        self.assertEqual(rec2.id, rec.id)
        self.assertEqual(model.search_count([("uid", "=", payload["uid"])]), 1)

    def test_upsert_maps_internal_statuses(self):
        for incoming, expected in [("received", "ready"), ("approved", "ready"),
                                   ("sent", "sent"), ("garbage", "ready")]:
            payload = {"uid": f"eml:map-{incoming}", "subject": "x", "status": incoming}
            rec = self.env["orderinbox.order"]._upsert_from_payload(payload)
            self.assertEqual(rec.status, expected, incoming)

    def test_sync_no_appliance_is_noop(self):
        created = self.env["orderinbox.order"].sync_from_appliance()
        self.assertEqual(created, 0)
