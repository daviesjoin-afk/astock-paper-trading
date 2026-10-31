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


class _ScriptedConnection:
    """确定性锁竞争模拟：按脚本返回成功 / SQLITE_BUSY / 其他错误。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.attempts = 0

    def execute(self, sql, params=()):
        self.attempts += 1
        outcome = self.outcomes[min(self.attempts - 1, len(self.outcomes) - 1)]
        if outcome == "busy":
            raise sqlite3.OperationalError("database is locked")
        if outcome == "error":
            raise sqlite3.OperationalError("no such table: paper_orders")
        return (sql, params)


class JournalModeRetryTests(unittest.TestCase):
    """R4：journal_mode 重试必须按热/冷路径分档，热路径不得用冷路径的退避。"""

    def _run(self, conn, *, hot_path):
        """在可控的睡/退避记录下调用 apply_journal_mode（异常也回收成结果）。"""
        sleeps = []
        backoffs = []
        real_backoff = storage.sqlite_busy_backoff

        def recording_backoff(attempt, hot_path=False):
            value = real_backoff(attempt, hot_path=hot_path)
            backoffs.append((hot_path, value))
            return value

        error = None
        sleep_patch = mock.patch("time.sleep",
                                 side_effect=lambda seconds: sleeps.append(seconds))
        with mock.patch.object(storage, "sqlite_busy_backoff",
                              side_effect=recording_backoff), sleep_patch:
            try:
                storage.apply_journal_mode(conn, hot_path=hot_path)
            except sqlite3.OperationalError as exc:
                error = exc
        return sleeps, backoffs, error

    def test_cold_path_retries_through_a_transient_lock(self):
        conn = _ScriptedConnection(["busy", "busy", "ok"])
        sleeps, backoffs, error = self._run(conn, hot_path=False)
        self.assertIsNone(error, "两次瞬时锁之后应当成功")
        self.assertEqual(conn.attempts, 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(hot is False for hot, _ in backoffs))

    def test_cold_path_raises_after_the_attempt_budget(self):
        limit = storage.journal_mode_attempts(hot_path=False)
        conn = _ScriptedConnection(["busy"] * (limit + 5))
        _, _, error = self._run(conn, hot_path=False)
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertEqual(conn.attempts, limit)

    def test_non_busy_errors_are_not_retried(self):
        conn = _ScriptedConnection(["error"])
        _, _, error = self._run(conn, hot_path=False)
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertEqual(conn.attempts, 1, "非锁竞争错误必须立刻上抛")

    def test_hot_path_uses_the_short_budget(self):
        self.assertLess(
            storage.journal_mode_attempts(hot_path=True),
            storage.journal_mode_attempts(hot_path=False),
            "热路径的重试次数必须少于冷路径",
        )
        limit = storage.journal_mode_attempts(hot_path=True)
        conn = _ScriptedConnection(["busy"] * (limit + 5))
        _, _, error = self._run(conn, hot_path=True)
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertEqual(conn.attempts, limit)

    def test_hot_path_never_uses_cold_backoff(self):
        limit = storage.journal_mode_attempts(hot_path=True)
        conn = _ScriptedConnection(["busy"] * (limit + 5))
        sleeps, backoffs, _ = self._run(conn, hot_path=True)
        self.assertTrue(backoffs, "应当发生退避")
        self.assertTrue(all(hot is True for hot, _ in backoffs),
                        "热路径必须把 hot_path 传给退避函数")
        self.assertLess(sum(sleeps), 0.5, "热路径累计等待必须留在短预算内")

    def test_cold_budget_is_materially_larger_than_hot(self):
        cold = storage.journal_mode_attempts(hot_path=False)
        conn = _ScriptedConnection(["busy"] * (cold + 5))
        sleeps, _, error = self._run(conn, hot_path=False)
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertGreaterEqual(sum(sleeps), 2.0, "冷路径必须保留有界但足够的等待")

    def test_db_passes_hot_path_into_journal_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.sqlite3")
            with mock.patch.object(storage, "apply_journal_mode") as apply_mode:
                with storage.db(path, hot_path=True) as conn:
                    conn.execute("SELECT 1")
            apply_mode.assert_called_once()
            self.assertTrue(apply_mode.call_args.kwargs.get("hot_path"))

    def test_attempt_budget_is_defined_in_one_place(self):
        self.assertEqual(
            set(storage.JOURNAL_MODE_MAX_ATTEMPTS), {"cold", "hot"},
            "重试次数只能有热/冷两档，且必须集中定义",
        )
