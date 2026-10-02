"""Shared application context: wires settings → backend → LLM → pipeline → store.

One factory used by the CLI (serve), so demo mode, live mode, and tests all
build the same object graph.

Boot behavior: when ORDERINBOX_DEMO=1 and the database is empty, the
appliance seeds and processes the demo dataset on first start — this is what
makes `docker compose up` a two-minute sales demo.
"""
from __future__ import annotations

import logging
import os

from ..config import Settings
from ..db import Store
from ..extract import LLMClient
from ..mail import MailMonitor
from ..odoo import CatalogBackend
from ..odoo.live import LiveOdooBackend
from ..odoo.mock import MockOdooBackend
from ..pipeline import Pipeline

log = logging.getLogger("orderinbox.boot")


class AppContext:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        logging.basicConfig(level=self.settings.log_level.upper(),
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        self.store = Store(self.settings.db_path())
        self.backend: CatalogBackend = self._make_backend()
        self.llm = LLMClient(self.settings)
        self.pipeline = Pipeline(self.settings, self.backend, self.llm, self.store)
        self.mail_monitor = MailMonitor(self.settings, self.pipeline)
        self._maybe_seed_demo()

    def _make_backend(self) -> CatalogBackend:
        s = self.settings
        if s.odoo_mode == "live":
            return LiveOdooBackend(s.odoo_url, s.odoo_db, s.odoo_username, s.odoo_password)
        return MockOdooBackend(seed_file=s.seed_file or None)

    def _maybe_seed_demo(self) -> None:
        """Seed + process the demo dataset on first boot (empty DB + ORDERINBOX_DEMO=1)."""
        if os.environ.get("ORDERINBOX_DEMO", "").strip().lower() not in ("1", "true", "yes"):
            return
        if self.store.counts_by_status():
            return  # already seeded / has data
        log.info("ORDERINBOX_DEMO=1 and database is empty — seeding demo dataset")
        try:
            from ..demo.seed import build_demo
            created = build_demo(self.settings)
            for p in created:
                self.pipeline.process_message(p)
            counts = self.store.counts_by_status()
            log.info("demo seeded: %s", counts)
        except Exception:
            log.exception("demo seeding failed (app continues without demo data)")

    def test_odoo(self) -> tuple[bool, str]:
        try:
            partners = self.backend.get_partners()
            products = self.backend.get_products()
            return True, f"OK — {len(partners)} partners, {len(products)} products loaded from {self.backend.name} backend"
        except Exception as exc:
            return False, f"FAILED — {exc}"
