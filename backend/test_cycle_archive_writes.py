"""工作包 D：周期与归档写入的 owner 级回归。

覆盖归档窗口（24h / 48h）、状态过滤、以及"已成交行不被清理改写"这条硬约束。
"""
from __future__ import annotations

import sqlite3
import unittest

import execution_dispatch as EPD
import paper_order_ledger as POL
import signal_service as SIG

SCHEMA = """
CREATE TABLE paper_orders(
    id INTEGER PRIMARY KEY,
    status TEXT,
    reason TEXT,
    risk_payload TEXT,
    cancelled_at TEXT,
    created_at TEXT
);
CREATE TABLE paper_signals(
    id INTEGER PRIMARY KEY,
    status TEXT,
    reason TEXT,
    created_at TEXT
);
"""


def _db(orders=(), signals=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO paper_orders VALUES(?,?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO paper_signals VALUES(?,?,?,?)", signals)
    conn.commit()
    return conn


def _statuses(conn, table):
    return dict(conn.execute(f"SELECT id,status FROM {table}").fetchall())


class OrderArchiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db(orders=[
            (1, "deferred_capacity", None, None, None, "2020-01-01 00:00:00"),  # 超龄
            (2, "deferred_capacity", None, None, None, "2999-01-01 00:00:00"),  # 未超龄
            (3, "pending_limit", None, None, None, "2020-01-01 00:00:00"),     # 状态不符
        ])
        self.addCleanup(self.conn.close)

    def test_delete_only_touches_the_named_status_inside_the_window(self):
        removed = POL.archive_delete_orders(self.conn, status="deferred_capacity")
        self.assertEqual(1, removed)
        self.assertEqual({2: "deferred_capacity", 3: "pending_limit"}, _statuses(self.conn, "paper_orders"))

    def test_supersede_keeps_reason_and_window(self):
        changed = POL.archive_supersede_orders(
            self.conn, status="deferred_capacity", reason="auto-archive: 超龄")
        self.assertEqual(1, changed)
        row = self.conn.execute("SELECT * FROM paper_orders WHERE id=1").fetchone()
        self.assertEqual("superseded", row["status"])
        self.assertEqual("auto-archive: 超龄", row["reason"])
        self.assertEqual("deferred_capacity",
                         self.conn.execute("SELECT status FROM paper_orders WHERE id=2").fetchone()[0])


class SignalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = _db(signals=[
            (1, "pending", None, "2020-01-01 00:00:00"),
            (2, "pending", None, "2999-01-01 00:00:00"),
            (3, "blocked", None, "2020-01-01 00:00:00"),
        ])
        self.addCleanup(self.conn.close)

    def test_delete_and_expire_respect_status_and_window(self):
        # blocked 的 3 号是超龄行 → 删除 1 行；pending 的 1 号超龄、2 号未超龄。
        self.assertEqual(1, SIG.archive_delete_signals(self.conn, status="blocked"))
        self.assertNotIn(3, _statuses(self.conn, "paper_signals"))
        self.assertEqual(1, SIG.archive_expire_signals(
            self.conn, status="pending", reason="auto-archive: pending超过24小时"))
        statuses = _statuses(self.conn, "paper_signals")
        self.assertEqual("expired", statuses[1])
        self.assertEqual("pending", statuses[2])

    def test_two_day_window_spares_the_36h_row(self):
        conn = _db(signals=[
            (1, "blocked", None, "2020-01-01 00:00:00"),
            (2, "blocked", None, "2999-01-01 00:00:00"),
        ])
        self.addCleanup(conn.close)
        changed = SIG.archive_expire_signals(
            conn, status="blocked", reason="auto-archive: blocked超过48小时", window="-2 days")
        self.assertEqual(1, changed)
        self.assertEqual("expired", _statuses(conn, "paper_signals")[1])
        self.assertEqual("blocked", _statuses(conn, "paper_signals")[2])

    def test_supersede_keeps_reason(self):
        changed = SIG.archive_supersede_signals(
            self.conn, status="blocked", reason="auto-archive: 冻结超龄",
            window="-1 day")
        self.assertEqual(1, changed)
        row = self.conn.execute("SELECT * FROM paper_signals WHERE id=3").fetchone()
        self.assertEqual("superseded", row["status"])
        self.assertEqual("auto-archive: 冻结超龄", row["reason"])


class CycleDriftTerminalisationTests(unittest.TestCase):
    def test_filled_rows_are_never_rewritten(self):
        conn = _db(orders=[
            (1, "pending_limit", None, None, None, "2026-09-11 09:30:00"),
            (2, "filled", "原始原因", "{}", None, "2026-09-11 09:30:00"),
        ])
        self.addCleanup(conn.close)
        changed = POL.terminalize_cycle_drift_order(
            conn, 1, reason="order_execution_cycle_changed：x", risk_payload="{}",
            cancelled_at="2026-09-11 10:00:00")
        self.assertEqual(1, changed)
        row = conn.execute("SELECT * FROM paper_orders WHERE id=1").fetchone()
        self.assertEqual("superseded", row["status"])
        self.assertEqual("2026-09-11 10:00:00", row["cancelled_at"])
        # 已成交行：状态与原因都不动。
        filled = conn.execute("SELECT * FROM paper_orders WHERE id=2").fetchone()
        self.assertEqual("filled", filled["status"])
        self.assertEqual("原始原因", filled["reason"])


class OwnerShapeTests(unittest.TestCase):
    def test_archive_owners_are_exported_business_operations(self):
        for name in ("archive_delete_orders", "archive_supersede_orders",
                     "terminalize_cycle_drift_order"):
            self.assertIn(name, POL.__all__)
        for name in ("archive_delete_signals", "archive_expire_signals",
                     "archive_supersede_signals"):
            self.assertIn(name, SIG.__all__)

    def test_execution_dispatch_no_longer_writes_signal_or_order_rows(self):
        import inspect
        source = inspect.getsource(EPD)
        self.assertNotIn("UPDATE paper_orders", source)
        self.assertNotIn("UPDATE paper_signals", source)


if __name__ == "__main__":
    unittest.main()
