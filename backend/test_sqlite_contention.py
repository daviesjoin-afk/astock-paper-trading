# -*- coding: utf-8 -*-
"""Real SQLite contention tests.

Verifies that:
1. When Connection A holds a write lock, Connection B with hot_path=True fails
   promptly within the expected short timeout (< 2.5s) instead of hanging for 60s.
2. The raised exception is correctly recognized as an SQLite busy error.
3. Once Connection A releases the write lock, Connection B succeeds with zero data corruption.
4. Default/cold path (hot_path=False) preserves the standard batch timeouts.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_storage as storage


class SQLiteContentionTests(unittest.TestCase):

    def test_hot_path_fails_quickly_on_lock_contention(self):
        """Connection B with hot_path=True must fail within ~1s when Connection A holds write lock."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            db_path = os.path.join(directory, "contention.sqlite3")

            # Initialize schema
            with storage.db(db_path, immediate=True) as conn:
                conn.execute("CREATE TABLE test_ledger (id INTEGER PRIMARY KEY, val TEXT)")
                conn.execute("INSERT INTO test_ledger(id, val) VALUES(1, 'init')")

            # Connection A acquires EXCLUSIVE / RESERVED lock via BEGIN IMMEDIATE
            conn_a = sqlite3.connect(db_path, timeout=120)
            conn_a.execute("PRAGMA journal_mode=WAL")
            conn_a.execute("BEGIN IMMEDIATE")
            conn_a.execute("INSERT INTO test_ledger(id, val) VALUES(2, 'from_conn_a')")

            try:
                start_time = time.monotonic()
                caught_exc = None
                try:
                    # Connection B attempts immediate write with hot_path=True
                    with storage.db(db_path, immediate=True, hot_path=True) as conn_b:
                        conn_b.execute("INSERT INTO test_ledger(id, val) VALUES(3, 'from_conn_b')")
                except sqlite3.OperationalError as exc:
                    caught_exc = exc

                elapsed = time.monotonic() - start_time

                self.assertIsNotNone(caught_exc, "Connection B must raise OperationalError on locked DB")
                self.assertTrue(
                    storage.is_sqlite_busy_error(caught_exc),
                    f"Exception must be recognized as busy error: {caught_exc}",
                )
                # Key assertion: Must fail fast! Under 2.5s, not 60s.
                self.assertLess(
                    elapsed,
                    2.5,
                    f"hot_path=True should fail fast (< 2.5s), but took {elapsed:.2f}s",
                )
            finally:
                # Release Connection A
                conn_a.rollback()
                conn_a.close()

            # Now that Connection A is closed/released, Connection B with hot_path=True must succeed
            with storage.db(db_path, immediate=True, hot_path=True) as conn_b:
                conn_b.execute("INSERT INTO test_ledger(id, val) VALUES(3, 'from_conn_b')")

            # Verify contents
            conn_verify = sqlite3.connect(db_path)
            try:
                rows = conn_verify.execute("SELECT id, val FROM test_ledger ORDER BY id").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][0], 1)
                self.assertEqual(rows[0][1], "init")
                self.assertEqual(rows[1][0], 3)
                self.assertEqual(rows[1][1], "from_conn_b")
            finally:
                conn_verify.close()

    def test_hot_path_vs_batch_profile_pragmas(self):
        """Verify busy_timeout setting difference between hot_path and batch."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            db_path = os.path.join(directory, "profiles.sqlite3")

            # Batch profile: busy_timeout should be 60000ms
            with storage.db(db_path, hot_path=False) as conn:
                res = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(res, 60000)

            # Hot path profile: busy_timeout should be 800ms
            with storage.db(db_path, hot_path=True) as conn:
                res = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(res, 800)

    def test_production_run_slot_contention_fails_fast_and_recovers(self):
        """Production run_slot on hot path fails fast under lock contention and recovers cleanly."""
        import paper_trading as PT
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            db_path = os.path.join(directory, "paper_contention.sqlite3")
            old_db_path = PT.DB_PATH
            PT.DB_PATH = db_path
            try:
                PT.init_db()

                # Connection A locks the database
                conn_a = sqlite3.connect(db_path, timeout=120)
                conn_a.execute("PRAGMA journal_mode=WAL")
                conn_a.execute("BEGIN IMMEDIATE")
                conn_a.execute("INSERT INTO paper_audit(event, detail, created_at) VALUES('lock_holder', '{}', '2026-09-14')")

                try:
                    start_time = time.monotonic()
                    with patch.object(PT, "_is_trade_weekday", return_value=True):
                        caught_exc = None
                        try:
                            PT.run_slot("intraday", force=True)
                        except Exception as exc:
                            caught_exc = exc
                    elapsed = time.monotonic() - start_time

                    self.assertIsNotNone(caught_exc, "run_slot must fail when DB is locked")
                    self.assertTrue(
                        storage.is_sqlite_busy_error(caught_exc),
                        f"Exception must be recognized as sqlite busy error: {caught_exc}",
                    )
                    self.assertLess(
                        elapsed,
                        3.5,
                        f"Hot path run_slot must fail fast under 3.5s, took {elapsed:.2f}s",
                    )
                finally:
                    conn_a.rollback()
                    conn_a.close()

                # After Connection A releases lock, run_slot recovers and completes
                with patch.object(PT, "_is_trade_weekday", return_value=True):
                    res = PT.run_slot("intraday", force=True)
                self.assertEqual(res.get("status"), "completed")
            finally:
                PT.DB_PATH = old_db_path


if __name__ == "__main__":
    unittest.main()
