# -*- coding: utf-8 -*-
"""PR-29 回归：单一归属的 Order TTL 生命周期。

核心不变式（验收口径）
----------------------
1. 过期 order row 永远 terminal——不允许把已过期委托改写成
   ``execution_retry``（该状态只允许由成交路径为未过期尝试新建）；
2. 信号仍 fresh 时，过期委托终态化后由信号在下一轮重建新委托
   （新 order id / 新 expires_at），旧订单以 ``retry_of_order_id``
   保留完整审计血缘；
3. 数据库中不存在 active/retry order 带过期 ``expires_at``
   （entry_lifecycle + execution_dispatch 两个清扫器覆盖全集后）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())

import entry_lifecycle as ELC  # noqa: E402
import execution_dispatch as EPD  # noqa: E402
import paper_trading as PT  # noqa: E402

NOW = dt.datetime(2026, 9, 9, 15, 30)
TODAY = dt.date(2026, 9, 9)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, signal_id INTEGER,
            side TEXT, code TEXT, name TEXT, qty INTEGER, planned_price REAL,
            filled_price REAL, amount REAL, fees REAL, status TEXT, reason TEXT,
            risk_payload TEXT, created_at TEXT, executed_at TEXT, expires_at TEXT,
            cancelled_at TEXT, order_type TEXT DEFAULT 'market',
            origin TEXT DEFAULT 'strategy', retry_of_order_id INTEGER);
        CREATE TABLE paper_signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, code TEXT,
            signal_date TEXT, intended_date TEXT, status TEXT,
            reason TEXT, payload TEXT, created_at TEXT);
        CREATE TABLE paper_capital_reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_key TEXT, status TEXT,
            released_at TEXT);
        """
    )
    return conn


def _signal(conn, signal_id=1, *, minutes_ago=10, status="pending",
            account_id="sector_rotation", code="600000"):
    created = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO paper_signals(id,account_id,code,intended_date,signal_date,
               status,reason,payload,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (signal_id, account_id, code, TODAY.isoformat(), TODAY.isoformat(),
         status, "测试信号", "{}", created),
    )
    conn.commit()
    return signal_id


def _order(conn, signal_id, *, minutes_ago=60, status="execution_retry",
           expires_at=None):
    created = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    cursor = conn.execute(
        """INSERT INTO paper_orders(account_id,signal_id,side,code,name,qty,planned_price,
               status,reason,risk_payload,created_at,expires_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("sector_rotation", signal_id, "buy", "600000", "浦发银行", 500, 10.0,
         status, "测试委托", "{}", created, expires_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _row(conn, table, row_id):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())


def _run_both_sweeps(conn, now=NOW):
    """三端生产中两个清扫器同属扫描环；测试里同样成对运行。"""
    entry_summary = ELC.expire_stale_orders(conn, now=now)
    dispatch_summary = EPD.run_execution_dispatch(conn, now=now)
    return entry_summary, dispatch_summary


class OrderTtlInvariantTests(unittest.TestCase):
    """验收：清扫后不存在 active/retry order 带过期 expires_at。"""

    def test_all_active_statuses_swept_clean(self):
        conn = _db()
        expired_stamp = (NOW - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
        statuses = list(ELC.ACTIVE_OR_RETRY_ORDER_STATUSES)
        for index, status in enumerate(statuses, start=1):
            _signal(conn, index, minutes_ago=200, status="pending")
            _order(conn, index, minutes_ago=200, status=status,
                   expires_at=expired_stamp)
        conn.commit()
        # 清扫前不变式必须命中全部 7 条。
        self.assertEqual(len(statuses), len(ELC.stale_active_orders(conn, now=NOW)))
        _run_both_sweeps(conn)
        remaining = ELC.stale_active_orders(conn, now=NOW)
        self.assertEqual([], remaining)
        # 任何 active/retry 行都不允许残留过期 expires_at。
        status_placeholders = ",".join("?" for _ in ELC.ACTIVE_OR_RETRY_ORDER_STATUSES)
        rows = conn.execute(
            """SELECT COUNT(*) FROM paper_orders
                 WHERE side='buy' AND expires_at IS NOT NULL
                   AND expires_at < ?
                   AND status IN (""" + status_placeholders + ")",
            (expired_stamp, *ELC.ACTIVE_OR_RETRY_ORDER_STATUSES),
        ).fetchone()[0]
        self.assertEqual(0, rows)
        # 不允许出现 execution_retry 行（新语义下清扫不产生该状态）。
        retry_rows = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE status='execution_retry'"
        ).fetchone()[0]
        self.assertEqual(0, retry_rows)

    def test_gated_order_expiry_is_owned_by_dispatch_sweep(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=10)
        order_id = _order(conn, signal_id, status="awaiting_batch",
                          expires_at=(NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"))
        entry_summary, dispatch_summary = _run_both_sweeps(conn)
        # entry_lifecycle 不碰执行器挂起态（单一归属，无双 Owner）。
        self.assertEqual(0, entry_summary["expired"])
        self.assertEqual(1, dispatch_summary["released"])
        self.assertEqual("superseded", _row(conn, "paper_orders", order_id)["status"])

    def test_recoverable_order_expiry_is_owned_by_entry_sweep(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=10)
        order_id = _order(conn, signal_id, status="pending_limit",
                          expires_at=(NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"))
        entry_summary, dispatch_summary = _run_both_sweeps(conn)
        self.assertEqual(1, entry_summary["expired"])
        self.assertEqual(0, dispatch_summary["checked"])
        self.assertEqual("expired", _row(conn, "paper_orders", order_id)["status"])


class RetryLineageTests(unittest.TestCase):
    """审计血缘：Signal → 尝试1（终态）→ 尝试2（retry_of_order_id=尝试1）。"""

    def test_previous_attempt_lineage_is_resolved(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=30)
        first = _order(conn, signal_id, minutes_ago=30, status="execution_retry")
        conn.execute(
            """UPDATE paper_orders SET status='superseded',
                   reason=COALESCE(reason,'') || '；执行时限到期' WHERE id=?""",
            (first,),
        )
        conn.commit()
        self.assertEqual(first, PT._previous_attempt_order_id(conn, signal_id))
        # 重建第二次尝试并指向第一次。
        second = _order(conn, signal_id, minutes_ago=5, status="pending_limit")
        conn.execute("UPDATE paper_orders SET retry_of_order_id=? WHERE id=?",
                     (first, second))
        conn.commit()
        row = _row(conn, "paper_orders", second)
        self.assertEqual(first, row["retry_of_order_id"])
        self.assertEqual("pending_limit", row["status"])
        self.assertEqual("superseded", _row(conn, "paper_orders", first)["status"])

    def test_no_lineage_without_terminal_attempt(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=5)
        self.assertIsNone(PT._previous_attempt_order_id(conn, signal_id))
        self.assertIsNone(PT._previous_attempt_order_id(conn, None))

    def test_buy_order_insert_writes_lineage_column(self):
        """源码级护栏：_buy_order 的 INSERT 必须携带 retry_of_order_id。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("retry_of_order_id", source)
        self.assertIn("_previous_attempt_order_id(conn, signal.get(\"id\"))", source)

    def test_schema_ensure_is_idempotent(self):
        conn = _db()
        first = PT.PSM.ensure_paper_columns(conn)
        # :memory: 库已含全部列 → 幂等无新增。
        self.assertNotIn("retry_of_order_id", first["paper_orders"])
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
        }
        self.assertIn("retry_of_order_id", columns)


class FreshSignalRearmTests(unittest.TestCase):
    """信号仍 fresh：过期委托终态化后，信号回到 pending 等待重建新委托。"""

    def test_fresh_signal_is_rearmed_not_expired(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=5)
        order_id = _order(conn, signal_id, status="awaiting_batch",
                          expires_at=(NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"))
        _run_both_sweeps(conn)
        self.assertEqual("superseded", _row(conn, "paper_orders", order_id)["status"])
        self.assertEqual("pending", _row(conn, "paper_signals", signal_id)["status"])
        # 批量放行标记仍然写入，重建的新委托不会被同一画像再次挂起。
        payload = json.loads(_row(conn, "paper_signals", signal_id)["payload"] or "{}")
        self.assertTrue(payload["execution_batch_release"]["released"])

    def test_stale_signal_is_expired_alongside_order(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=600)
        order_id = _order(conn, signal_id, status="pending_verification",
                          expires_at=(NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"))
        _run_both_sweeps(conn)
        self.assertEqual("superseded", _row(conn, "paper_orders", order_id)["status"])
        self.assertEqual("expired", _row(conn, "paper_signals", signal_id)["status"])

    def test_frozen_waitlist_expiry_releases_reservation(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=10)
        order_id = _order(conn, signal_id, status="entry_frozen_waitlist",
                          expires_at=(NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"))
        conn.execute(
            "INSERT INTO paper_capital_reservations(order_key,status) VALUES(?,'reserved')",
            (str(order_id),),
        )
        conn.commit()
        _run_both_sweeps(conn)
        self.assertEqual("expired", _row(conn, "paper_orders", order_id)["status"])
        reservation = conn.execute(
            "SELECT status FROM paper_capital_reservations WHERE order_key=?",
            (str(order_id),),
        ).fetchone()
        self.assertEqual("released", reservation["status"])


if __name__ == "__main__":
    unittest.main()
