"""工作包 A 第一片：订单行 owner（`paper_order_ledger`）的边界测试。

本文件只守 owner 自己的契约，不重复业务链对照 —— 端到端等价由
`test_entry_lifecycle` / `test_signal_order_reconcile_write` 通过注入同一批函数覆盖。
"""
from __future__ import annotations

import inspect
import sqlite3
import unittest
from unittest import mock

import entry_lifecycle as ELC
import execution_dispatch as EPD
import paper_order_ledger as POL

SCHEMA = """
CREATE TABLE paper_orders(
    id INTEGER PRIMARY KEY,
    account_id INTEGER,
    code TEXT,
    side TEXT,
    status TEXT,
    reason TEXT,
    signal_id INTEGER,
    cancelled_at TEXT,
    name TEXT,
    qty INTEGER,
    planned_price REAL,
    risk_payload TEXT,
    origin TEXT,
    created_at TEXT,
    strategy_id TEXT,
    strategy_version TEXT,
    strategy_checksum TEXT,
    retry_of_order_id INTEGER,
    cycle_id INTEGER
);
"""


def _db(rows=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO paper_orders(id,account_id,code,side,status,reason,signal_id,cancelled_at)"
        " VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _row(conn, order_id):
    return conn.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()


class CreationAndCancelTests(unittest.TestCase):
    """A 第三片：创建、撤单、重试接管三个入口的边界。"""

    def setUp(self):
        self.conn = _db([(1, 7, "AAA", "buy", "execution_retry", None, 11, None),
                         (2, 7, "AAA", "buy", "pending_limit", None, 12, None)])
        self.addCleanup(self.conn.close)

    def test_cancel_returns_rowcount_and_respects_the_status_whitelist(self):
        changed = POL.cancel_order(self.conn, 2, reason="用户撤销模拟委托",
                                   cancelled_at="T", statuses=("pending_limit",))
        self.assertEqual(1, changed)
        row = _row(self.conn, 2)
        self.assertEqual("cancelled", row["status"])
        self.assertEqual("用户撤销模拟委托", row["reason"])
        self.assertEqual("T", row["cancelled_at"])
        # 状态不在白名单 → 0 行，调用方据此判断"已成交/不可撤销"。
        self.assertEqual(0, POL.cancel_order(self.conn, 2, reason="重复撤销",
                                             cancelled_at="T2", statuses=("pending_limit",)))

    def test_cancel_requires_a_status_vocabulary(self):
        with self.assertRaises(ValueError):
            POL.cancel_order(self.conn, 2, reason="x", cancelled_at="T", statuses=())

    def test_previous_retry_retirement_appends_reason(self):
        POL.supersede_previous_execution_retry(self.conn, 1)
        row = _row(self.conn, 1)
        self.assertEqual("superseded", row["status"])
        self.assertEqual("；新一轮执行重试已接管", row["reason"])
        # 不是执行重试的行不动。
        POL.supersede_previous_execution_retry(self.conn, 2)
        self.assertEqual("pending_limit", _row(self.conn, 2)["status"])

    def test_insert_order_keeps_the_column_order_and_returns_the_row_id(self):
        order_id = POL.insert_order(
            self.conn, account_id=7, signal_id=13, side="buy", code="BBB", name="测试",
            qty=100, planned_price=10.5, status="entry_frozen_waitlist", reason="冻结",
            risk_payload="{}", origin="strategy", created_at="2026-09-11T10:00:00",
            strategy_id="s1", strategy_version="v1", strategy_checksum="abc",
            retry_of_order_id=1, cycle_id=3,
        )
        self.assertIsInstance(order_id, int)
        row = self.conn.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()
        self.assertEqual(7, row["account_id"])
        self.assertEqual(13, row["signal_id"])
        self.assertEqual("BBB", row["code"])
        self.assertEqual("entry_frozen_waitlist", row["status"])
        self.assertEqual("s1", row["strategy_id"])


class OwnerBoundaryTests(unittest.TestCase):
    def test_terminate_only_accepts_terminal_statuses(self):
        conn = _db([(1, 7, "AAA", "buy", "pending_limit", None, None, None)])
        POL.terminate_order(conn, 1, status="superseded", reason="回收", cancelled_at="T")
        self.assertEqual("superseded", _row(conn, 1)["status"])
        with self.assertRaises(ValueError):
            POL.terminate_order(conn, 1, status="pending_limit", reason="x", cancelled_at="T")
        with self.assertRaises(ValueError):
            POL.terminate_order(conn, 1, status="anything", reason="x", cancelled_at="T")

    def test_reason_is_appended_not_overwritten(self):
        conn = _db([(1, 7, "AAA", "buy", "pending_limit", "原始原因", None, None)])
        POL.expire_order(conn, 1, reason="超时", cancelled_at="T")
        row = _row(conn, 1)
        self.assertEqual("expired", row["status"])
        self.assertEqual("原始原因；超时", row["reason"])
        self.assertEqual("T", row["cancelled_at"])

    def test_supersede_for_expired_signal_keeps_the_status_vocabulary(self):
        states = ("pending_limit", "execution_retry", "manual_execution_retry",
                  "deferred_capacity", "entry_frozen_waitlist")
        conn = _db([(index, 7, "A", "buy", status, None, 11, None)
                    for index, status in enumerate(states, start=1)])
        conn.executemany(
            "INSERT INTO paper_orders(id,account_id,code,side,status,reason,signal_id,cancelled_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(9, 7, "B", "buy", "awaiting_batch", None, 12, None),
             (10, 7, "C", "buy", "partially_filled", None, 13, None)])
        for order_id in (1, 2, 3, 4, 5):
            POL.supersede_for_expired_signal(conn, order_id, cancelled_at="T")
        statuses = dict(conn.execute("SELECT id,status FROM paper_orders").fetchall())
        for order_id in (1, 2, 3, 4, 5):
            self.assertEqual("superseded", statuses[order_id])
        # 不在 RECOVERABLE_ORDER_STATUSES 里的状态不被动。
        self.assertEqual("awaiting_batch", statuses[9])
        self.assertEqual("partially_filled", statuses[10])
        self.assertEqual(len(ELC.RECOVERABLE_ORDER_STATUSES), 5)

    def test_terminal_signal_owner_requires_a_status_tuple(self):
        conn = _db([(1, 7, "AAA", "buy", "pending_limit", None, 11, None)])
        with self.assertRaises(ValueError):
            POL.supersede_for_terminal_signal(conn, 1, reason="x", statuses=())
        with self.assertRaises(ValueError):
            POL.retire_gated_order(conn, 1, reason="x", statuses=())

    def test_duplicate_waitlist_keeps_exists_semantics(self):
        conn = _db([
            (1, 7, "AAA", "buy", "deferred_capacity", None, 11, None),
            (2, 7, "AAA", "buy", "deferred_capacity", None, 12, None),
            (3, 9, "AAA", "buy", "deferred_capacity", None, 13, None),
            (4, 7, "BBB", "sell", "deferred_capacity", None, 14, None),
        ])
        POL.retire_duplicate_waitlist_orders(conn, waitlist_status=ELC.ENTRY_FROZEN_WAITLIST_STATUS)
        statuses = dict(conn.execute("SELECT id,status FROM paper_orders").fetchall())
        self.assertEqual("superseded", statuses[1])
        self.assertEqual("deferred_capacity", statuses[2])
        self.assertEqual("deferred_capacity", statuses[3])
        self.assertEqual("deferred_capacity", statuses[4])

    def test_owner_never_reads_the_clock_or_commits(self):
        conn = _db([(1, 7, "AAA", "buy", "pending_limit", None, 11, None)])
        with mock.patch.object(POL, "_now", create=True,
                               side_effect=AssertionError("owner read clock")):
            POL.expire_order(conn, 1, reason="x", cancelled_at="T")
            POL.supersede_order(conn, 1, reason="x", cancelled_at="T")
        self.assertTrue(conn.in_transaction)

    def test_execution_dispatch_no_longer_writes_orders(self):
        source = inspect.getsource(EPD)
        self.assertNotIn("UPDATE paper_orders", source)
        self.assertNotIn("INSERT INTO paper_orders", source)
        self.assertNotIn("DELETE FROM paper_orders", source)

    def test_owner_is_the_only_runtime_order_writer_for_terminal_paths(self):
        # 终态/回收入口只在 owner 里出现；业务模块拿到的是函数引用而不是第二份 SQL。
        for name in ("expire_order", "supersede_order", "terminate_order",
                     "supersede_for_expired_signal", "supersede_duplicate_execution_retry",
                     "supersede_for_terminal_signal", "retire_gated_order",
                     "retire_duplicate_waitlist_orders"):
            self.assertIn(name, POL.__all__)
            self.assertTrue(callable(getattr(POL, name)))

    def test_every_owner_entry_is_a_keyword_only_business_operation(self):
        for name in ("expire_order", "supersede_order", "terminate_order",
                     "supersede_for_expired_signal", "supersede_duplicate_execution_retry",
                     "supersede_for_terminal_signal", "retire_gated_order",
                     "retire_duplicate_waitlist_orders"):
            signature = inspect.signature(getattr(POL, name))
            params = list(signature.parameters.values())
            self.assertEqual("conn", params[0].name, name)
            rest = params[1:]
            if rest and rest[0].name == "order_id":
                # 单行操作：身份位置参数，其余一律关键字必填。
                self.assertEqual(inspect.Parameter.POSITIONAL_OR_KEYWORD, rest[0].kind, name)
                rest = rest[1:]
            else:
                # 批量操作：没有"顺手传一个 id"的位置参数。
                self.assertNotIn("order_id", [p.name for p in params], name)
            for param in rest:
                self.assertEqual(inspect.Parameter.KEYWORD_ONLY, param.kind, name)
                self.assertEqual(inspect.Parameter.empty, param.default, name)


if __name__ == "__main__":
    unittest.main()
