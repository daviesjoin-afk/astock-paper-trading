# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_migrate


class DbMigrateTests(unittest.TestCase):
    def test_legacy_paper_schema_reaches_latest_version_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            conn = sqlite3.connect(path)
            conn.executescript(
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
            conn.commit()
            conn.close()

            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0], 4)
                order_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_orders)")}
                self.assertTrue({"realized_pnl", "order_type", "origin"}.issubset(order_columns))
                self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_ignition_shadow'").fetchone())
            finally:
                conn.close()

            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0], 4)
            finally:
                conn.close()

    def test_failed_callable_migration_rolls_back_and_keeps_previous_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.sqlite3")
            sqlite3.connect(path).close()
            with redirect_stdout(StringIO()):
                db_migrate.migrate("paper_trading", path=path)

            original = db_migrate.MIGRATIONS["paper_trading"]

            def fail(conn):
                conn.execute("CREATE TABLE should_rollback (id INTEGER)")
                raise RuntimeError("migration failed")

            db_migrate.MIGRATIONS["paper_trading"] = [*original, (5, "故意失败", fail)]
            try:
                with self.assertRaisesRegex(RuntimeError, "migration failed"), redirect_stdout(StringIO()):
                    db_migrate.migrate("paper_trading", path=path)
            finally:
                db_migrate.MIGRATIONS["paper_trading"] = original

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0], 4)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'").fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
