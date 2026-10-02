"""SQLite persistence. The appliance is a single-node Docker container;
SQLite keeps state with zero configuration and trivial backup (one file).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import ProcessedOrder

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'email',
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    stored_path TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    odoo_order_id INTEGER,
    odoo_reference TEXT,
    error TEXT,
    processed_at TEXT,
    payload TEXT,
    log TEXT,
    issues TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Lightweight in-place upgrades for databases created by older builds."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(orders)")}
        if "issues" not in cols:
            self.conn.execute("ALTER TABLE orders ADD COLUMN issues TEXT")

    # ------------------------------------------------------------------
    def upsert_order(self, order: ProcessedOrder) -> int:
        payload = json.dumps(order.extraction.model_dump() if order.extraction else None)
        log_json = json.dumps(order.log, ensure_ascii=False)
        issues_json = json.dumps([i.model_dump() for i in order.issues], ensure_ascii=False)
        cur = self.conn.execute(
            """INSERT INTO orders (uid, source_type, subject, sender, received_at, stored_path,
                                   status, odoo_order_id, odoo_reference, error, processed_at, payload, log, issues)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET
                 status=excluded.status, odoo_order_id=excluded.odoo_order_id,
                 odoo_reference=excluded.odoo_reference, error=excluded.error,
                 processed_at=excluded.processed_at, payload=excluded.payload,
                 log=excluded.log, issues=excluded.issues
            """,
            (
                order.uid, order.source_type, order.subject, order.sender,
                order.received_at.isoformat() if order.received_at else None,
                order.stored_path, order.status.value, order.odoo_order_id,
                order.odoo_reference, order.error,
                order.processed_at.isoformat() if order.processed_at else None,
                payload, log_json, issues_json,
            ),
        )
        self.conn.commit()
        if order.id is None:
            row = self.conn.execute("SELECT id FROM orders WHERE uid=?", (order.uid,)).fetchone()
            order.id = row["id"] if row else cur.lastrowid
        return order.id

    def get_order(self, order_id: int) -> Optional[ProcessedOrder]:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self._row_to_order(row) if row else None

    def list_orders(self, status: Optional[str] = None, limit: int = 500) -> list[ProcessedOrder]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY received_at DESC, id DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM orders ORDER BY received_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_order(r) for r in rows]

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def seen_po_keys(self) -> set[tuple[str, str]]:
        keys = set()
        rows = self.conn.execute("SELECT payload FROM orders WHERE status NOT IN ('rejected','failed')").fetchall()
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "null")
            except json.JSONDecodeError:
                continue
            if not payload:
                continue
            customer = (payload.get("customer_matched") or "").lower()
            po = (payload.get("po_number") or "").strip().upper()
            if customer and po:
                keys.add((customer, po))
        return keys

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> ProcessedOrder:
        from .models import Extraction, OrderStatus
        order = ProcessedOrder(
            id=row["id"], uid=row["uid"], source_type=row["source_type"],
            subject=row["subject"] or "", sender=row["sender"] or "",
            received_at=datetime.fromisoformat(row["received_at"]) if row["received_at"] else None,
            stored_path=row["stored_path"] or "", status=OrderStatus(row["status"]),
            odoo_order_id=row["odoo_order_id"], odoo_reference=row["odoo_reference"] or "",
            error=row["error"] or "",
            processed_at=datetime.fromisoformat(row["processed_at"]) if row["processed_at"] else None,
        )
        if row["payload"]:
            try:
                order.extraction = Extraction.model_validate(json.loads(row["payload"]))
            except Exception:
                order.extraction = None
        try:
            order.log = json.loads(row["log"] or "[]")
        except (json.JSONDecodeError, TypeError):
            order.log = []
        if row["issues"]:
            try:
                from .models import Issue
                order.issues = [Issue.model_validate(i) for i in json.loads(row["issues"])]
            except (json.JSONDecodeError, TypeError, ValueError):
                order.issues = []
        return order
