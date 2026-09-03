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


if __name__ == "__main__":
    unittest.main()
