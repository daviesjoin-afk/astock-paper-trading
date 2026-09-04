# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_storage as storage


class _LockedConnection:
    def __init__(self, failures=2):
        self.failures = failures
        self.attempts = 0

    def execute(self, sql, params=()):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return (sql, params)


class PaperStorageTests(unittest.TestCase):
    def test_db_commits_and_supports_immediate_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.sqlite3")
            with storage.db(path, immediate=True) as conn:
                conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                conn.execute("INSERT INTO sample(value) VALUES (?)", ("saved",))

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "saved")
            finally:
                conn.close()

    def test_db_rolls_back_failed_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.sqlite3")
            with storage.db(path) as conn:
                conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            with self.assertRaisesRegex(ValueError, "stop"):
                with storage.db(path) as conn:
                    conn.execute("INSERT INTO sample(value) VALUES (?)", ("discarded",))
                    raise ValueError("stop")

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
            finally:
                conn.close()

    def test_readonly_connection_rejects_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.sqlite3")
            with storage.db(path) as conn:
                conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                conn.execute("INSERT INTO sample(value) VALUES (?)", ("read",))

            with storage.db_readonly(path) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "read")
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO sample(value) VALUES ('blocked')")

    def test_execute_with_retry_retries_only_locked_errors(self):
        conn = _LockedConnection()
        with mock.patch.object(storage.time, "sleep") as sleep:
            result = storage.execute_with_retry(conn, "SELECT 1", max_retries=3)
        self.assertEqual(result, ("SELECT 1", ()))
        self.assertEqual(conn.attempts, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
