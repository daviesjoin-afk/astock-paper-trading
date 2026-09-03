# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_schema_migrations as migrations


class PaperSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE paper_position_lots(id INTEGER PRIMARY KEY, cost REAL, qty INTEGER);
            CREATE TABLE paper_positions(account_id TEXT, code TEXT, qty INTEGER);
            CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, initial_cash REAL, cash REAL);
            CREATE TABLE paper_jobs(slot TEXT, market_date TEXT, started_at TEXT);
            CREATE TABLE paper_job_runs(run_key TEXT PRIMARY KEY, started_at TEXT);
            CREATE TABLE paper_runtime_locks(lock_key TEXT PRIMARY KEY, acquired_at TEXT, expires_at TEXT);
            CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT);
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_legacy_columns_are_added_and_second_run_is_idempotent(self):
        first = migrations.ensure_paper_columns(self.conn)
        second = migrations.ensure_paper_columns(self.conn)
        self.assertIn("realized_pnl", first["paper_orders"])
        self.assertEqual(second["paper_orders"], ())
        self.assertTrue({"cycle_id", "params", "cooldown_until"}.issubset(migrations.table_columns(self.conn, "paper_accounts")))
        self.assertIn("asset_type", migrations.table_columns(self.conn, "paper_positions"))

    def test_runtime_lease_migration_normalizes_legacy_timestamps(self):
        self.conn.execute("INSERT INTO paper_jobs(slot,market_date,started_at) VALUES('risk','2026-09-03','2026-09-03T10:00:00')")
        self.conn.execute("INSERT INTO paper_job_runs(run_key,started_at) VALUES('run','2026-09-03T10:00:00')")
        self.conn.execute("INSERT INTO paper_runtime_locks(lock_key,acquired_at,expires_at) VALUES('lock','2026-09-03T10:00:00','2026-09-03T10:05:00')")
        migrations.ensure_runtime_lease_columns(self.conn)
        self.assertEqual(self.conn.execute("SELECT started_at FROM paper_jobs").fetchone()[0], "2026-09-03 10:00:00")
        self.assertEqual(self.conn.execute("SELECT acquired_at FROM paper_runtime_locks").fetchone()[0], "2026-09-03 10:00:00")
        self.assertEqual(self.conn.execute("SELECT fencing_token FROM paper_job_runs").fetchone()[0], 0)

    def test_ignition_shadow_table_and_indexes_are_idempotent(self):
        self.assertTrue(migrations.ensure_ignition_shadow_table(self.conn))
        self.assertTrue(migrations.ensure_ignition_shadow_table(self.conn))
        self.assertIsNotNone(self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_ignition_shadow'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_paper_ignition_shadow_unique'").fetchone())


if __name__ == "__main__":
    unittest.main()
