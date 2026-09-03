# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_repository as repository


class PaperRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        self.conn.execute("CREATE TABLE paper_audit (account_id TEXT, event TEXT, detail TEXT, created_at TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_rows_returns_plain_dicts(self):
        self.conn.execute("INSERT INTO sample(id,value) VALUES(?,?)", (1, "ok"))
        self.assertEqual(repository.rows(self.conn, "SELECT * FROM sample WHERE id=?", (1,)), [{"id": 1, "value": "ok"}])

    def test_audit_writes_explicit_timestamp(self):
        repository.audit(self.conn, "acct", "test_event", "detail", "2026-09-03T10:00:00+08:00")
        row = self.conn.execute("SELECT * FROM paper_audit").fetchone()
        self.assertEqual(dict(row), {
            "account_id": "acct", "event": "test_event", "detail": "detail",
            "created_at": "2026-09-03T10:00:00+08:00",
        })

    def test_account_metric_inputs_batches_legacy_safe_projection(self):
        self.conn.executescript(
            """
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, qty INTEGER,
                filled_price REAL, amount REAL, fees REAL, status TEXT, side TEXT,
                created_at TEXT
            );
            CREATE TABLE paper_fills(account_id TEXT, side TEXT);
            CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT, nav REAL, benchmark REAL, created_at TEXT);
            INSERT INTO paper_orders VALUES(1,'acct','000001',100,10,1000,1,'filled','sell','2026-09-03 10:00:00');
            INSERT INTO paper_fills VALUES('acct','buy');
            INSERT INTO paper_nav VALUES('acct','2026-09-02',100000,1,'2026-09-02 15:00:00');
            """
        )
        result = repository.account_metric_inputs(self.conn, ["acct"], "2026-09-03")
        self.assertEqual(result["buy_count"], {"acct": 1})
        self.assertEqual(result["sells"]["acct"][0]["realized_pnl"], None)
        self.assertEqual(result["previous_nav"]["acct"]["nav_date"], "2026-09-02")

    def test_account_metric_inputs_empty_accounts_avoids_schema_reads(self):
        self.assertEqual(repository.account_metric_inputs(self.conn, [], "2026-09-03"), {
            "latest_nav": {}, "navs": {}, "previous_nav": {}, "sells": {},
            "buy_count": {}, "rejected": {},
        })


if __name__ == "__main__":
    unittest.main()
