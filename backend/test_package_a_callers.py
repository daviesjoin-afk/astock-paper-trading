"""工作包 A 的调用方级回归：三条迁移后的写入在真实入口上仍然这样工作。

owner 自己的边界见 ``test_order_ledger``；这里守的是**调用方**——撤单入口的
错误语义、重试接管的退役+释放、冻结等待池的去重早退。
"""
from __future__ import annotations

import contextlib
import sqlite3
import types
import unittest

import manual_orders as MO
import paper_capital_reservations as PCR
import paper_order_ledger as POL
import paper_trading as PT

SCHEMA = """
CREATE TABLE paper_orders(
    id INTEGER PRIMARY KEY,
    account_id INTEGER,
    signal_id INTEGER,
    code TEXT,
    side TEXT,
    status TEXT,
    reason TEXT,
    origin TEXT,
    cancelled_at TEXT
);
CREATE TABLE paper_capital_reservations(
    id INTEGER PRIMARY KEY,
    order_key TEXT,
    status TEXT,
    released_at TEXT
);
"""


def _db(orders=(), reservations=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO paper_orders(id,account_id,signal_id,code,side,status,reason,origin,cancelled_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)", orders)
    conn.executemany(
        "INSERT INTO paper_capital_reservations(order_key,status,released_at) VALUES(?,?,?)",
        reservations)
    conn.commit()
    return conn


class ManualCancelTests(unittest.TestCase):
    """``manual_orders.cancel_manual_order`` 走注入后的撤单实现。"""

    def setUp(self):
        self.conn = _db(
            orders=[(1, 7, 11, "AAA", "buy", "pending_limit", None, "manual", None),
                    (2, 7, 12, "BBB", "buy", "filled", None, "manual", None)],
            reservations=[("1", "reserved", None), ("2", "reserved", None)],
        )
        self.addCleanup(self.conn.close)
        self.audits = []
        self.released = []

        def finish(conn, order_key, status):
            self.released.append((int(order_key), status))
            return PCR.finish_capital_reservation(
                conn, order_key, status, now_fn=lambda: "2026-09-11 10:00:00")

        self.deps = types.SimpleNamespace(
            db=lambda: contextlib.nullcontext(self.conn),
            audit=lambda conn, account_id, event, detail: self.audits.append(event),
            now=lambda: "2026-09-11 10:00:00",
            init_db=lambda: None,
            finish_capital_reservation=finish,
            cancel_order=POL.cancel_order,
        )
        MO.bind_dependencies(lambda: self.deps)

    def test_cancel_retires_the_order_releases_the_reservation_and_audits(self):
        result = MO.cancel_manual_order(1)
        self.assertEqual({"order_id": 1, "status": "cancelled"}, result)
        row = self.conn.execute("SELECT * FROM paper_orders WHERE id=1").fetchone()
        self.assertEqual("cancelled", row["status"])
        self.assertEqual("用户撤销模拟委托", row["reason"])
        self.assertEqual("2026-09-11 10:00:00", row["cancelled_at"])
        self.assertEqual([(1, "released")], self.released)
        self.assertEqual(["manual_order_cancelled"], self.audits)

    def test_already_filled_order_cannot_be_cancelled(self):
        with self.assertRaisesRegex(ValueError, "仍有未成交份额"):
            MO.cancel_manual_order(2)
        self.assertEqual([], self.released)
        self.assertEqual([], self.audits)

    def test_missing_order_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "未找到手动模拟委托"):
            MO.cancel_manual_order(999)

    def test_second_cancel_is_rejected_without_touching_the_row(self):
        MO.cancel_manual_order(1)
        with self.assertRaisesRegex(ValueError, "仍有未成交份额"):
            MO.cancel_manual_order(1)


class RetryTakeoverTests(unittest.TestCase):
    """``_supersede_signal_execution_retries`` 退役全部旧重试并释放预占。"""

    def test_every_previous_retry_is_retired_and_released(self):
        conn = _db(
            orders=[(1, 7, 51, "AAA", "buy", "execution_retry", None, "strategy", None),
                    (2, 7, 51, "AAA", "buy", "execution_retry", "旧原因", "strategy", None),
                    (3, 7, 51, "AAA", "buy", "pending_limit", None, "strategy", None)],
            reservations=[("1", "reserved", None), ("2", "reserved", None),
                          ("3", "reserved", None)],
        )
        self.addCleanup(conn.close)
        retired = PT._supersede_signal_execution_retries(conn, 51)
        self.assertEqual(2, retired)
        rows = {row["id"]: row["status"] for row in conn.execute("SELECT * FROM paper_orders")}
        self.assertEqual("superseded", rows[1])
        self.assertEqual("superseded", rows[2])
        self.assertEqual("pending_limit", rows[3])
        reservations = {row["order_key"]: row["status"]
                        for row in conn.execute("SELECT * FROM paper_capital_reservations")}
        self.assertEqual("released", reservations["1"])
        self.assertEqual("released", reservations["2"])
        self.assertEqual("reserved", reservations["3"])

    def test_no_retry_rows_is_a_noop(self):
        conn = _db(orders=[(1, 7, 52, "AAA", "buy", "pending_limit", None, "strategy", None)])
        self.addCleanup(conn.close)
        self.assertEqual(0, PT._supersede_signal_execution_retries(conn, 52))
        self.assertEqual(0, PT._supersede_signal_execution_retries(conn, None))


class FrozenWaitlistDedupeTests(unittest.TestCase):
    """冻结等待池标记：同一信号重复调用返回同一行，不重复建单。"""

    def test_existing_marker_is_returned_instead_of_inserting(self):
        conn = _db(orders=[(5, 7, 61, "AAA", "buy", "entry_frozen_waitlist", "冻结", "strategy", None)])
        self.addCleanup(conn.close)
        order_id, created, reason, payload = PT._record_entry_frozen_waitlist(
            conn, 7, "AAA", signal_id=61, qty=100, planned_price=10.0,
        )
        self.assertEqual(5, order_id)
        self.assertFalse(created)
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
