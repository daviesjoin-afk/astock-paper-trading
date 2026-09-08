# -*- coding: utf-8 -*-
"""信号/委托生命周期（signal expiry & staged entry）的回归测试。"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entry_lifecycle as ELC

NOW = dt.datetime(2026, 9, 9, 10, 30)
TODAY = "2026-09-09"


def _iso(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


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
            cancelled_at TEXT, order_type TEXT DEFAULT 'market', origin TEXT DEFAULT 'strategy');
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


def _signal(conn, *, minutes_ago=10, status="pending", day=TODAY, signal_id=None):
    created = _iso(minutes_ago)
    cursor = conn.execute(
        """INSERT INTO paper_signals(id,account_id,code,signal_date,intended_date,
               status,reason,payload,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (signal_id, "sector_rotation", "600000", day, day, status, "",
         "{}", created),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _order(conn, signal_id, *, minutes_ago=10, status="execution_retry", expires_at=None):
    cursor = conn.execute(
        """INSERT INTO paper_orders(account_id,signal_id,side,code,qty,status,reason,
               risk_payload,created_at,expires_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("sector_rotation", signal_id, "buy", "600000", 500, status, "等待",
         "{}", _iso(minutes_ago), expires_at),
    )
    order_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO paper_capital_reservations(order_key,status) VALUES(?,'reserved')",
        (str(order_id),),
    )
    conn.commit()
    return order_id


def _row(conn, table, row_id):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())


class SignalFreshnessTests(unittest.TestCase):
    def test_fresh_signal_is_usable(self):
        signal = {"created_at": _iso(10), "intended_date": TODAY}
        result = ELC.signal_freshness(signal, now=NOW, asof_day=TODAY)
        self.assertTrue(result["usable"])
        self.assertAlmostEqual(10.0, result["age_minutes"], places=1)

    def test_signal_older_than_ttl_is_not_usable(self):
        signal = {"created_at": _iso(91), "intended_date": TODAY}
        result = ELC.signal_freshness(signal, now=NOW, asof_day=TODAY)
        self.assertFalse(result["usable"])
        self.assertIn("有效期", result["reason"])

    def test_cross_day_signal_is_never_usable(self):
        signal = {"created_at": _iso(5), "intended_date": "2026-09-08"}
        result = ELC.signal_freshness(signal, now=NOW, asof_day=TODAY)
        self.assertFalse(result["usable"])
        self.assertIn("旧信号", result["reason"])

    def test_missing_timestamp_fails_closed(self):
        result = ELC.signal_freshness({}, now=NOW, asof_day=TODAY)
        self.assertFalse(result["usable"])
        self.assertIn("fail-closed", result["reason"])

    def test_unparsable_timestamp_fails_closed(self):
        result = ELC.signal_freshness({"created_at": "不是时间"}, now=NOW, asof_day=TODAY)
        self.assertFalse(result["usable"])


class EntrySlicePlanTests(unittest.TestCase):
    def test_single_slice_when_disabled(self):
        self.assertEqual([500], ELC.entry_slice_plan(500, 1))

    def test_slices_sum_to_the_total_and_are_whole_lots(self):
        plan = ELC.entry_slice_plan(1500, 3)
        self.assertEqual(3, len(plan))
        self.assertEqual(1500, sum(plan))
        self.assertTrue(all(qty % 100 == 0 for qty in plan))

    def test_remainder_goes_to_the_last_slice(self):
        plan = ELC.entry_slice_plan(1500, 2)
        self.assertEqual([700, 800], plan)

    def test_tiny_targets_degrade_to_one_slice(self):
        self.assertEqual([100], ELC.entry_slice_plan(100, 3))

    def test_zero_is_safe(self):
        self.assertEqual([0], ELC.entry_slice_plan(0, 3))


def _stub_conn():
    return None


class SweepOrderTests(unittest.TestCase):
    def test_aged_order_expires_and_releases_reservation(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=20)
        order_id = _order(conn, signal_id, minutes_ago=60)
        summary = ELC.expire_stale_orders(conn, now=NOW, ttl_minutes=30)
        self.assertEqual(1, summary["expired"])
        order = _row(conn, "paper_orders", order_id)
        self.assertEqual("expired", order["status"])
        self.assertIsNotNone(order["cancelled_at"])
        reservation = conn.execute(
            "SELECT status FROM paper_capital_reservations WHERE order_key=?",
            (str(order_id),),
        ).fetchone()
        self.assertEqual("released", reservation["status"])
        self.assertEqual("expired", _row(conn, "paper_signals", signal_id)["status"])

    def test_explicit_expires_at_wins_over_age(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=600)
        order_id = _order(conn, signal_id, minutes_ago=600,
                          expires_at="2026-09-09T23:00:00")
        summary = ELC.expire_stale_orders(conn, now=NOW, ttl_minutes=30)
        self.assertEqual(0, summary["expired"])
        self.assertEqual("execution_retry", _row(conn, "paper_orders", order_id)["status"])

    def test_filled_orders_are_untouched(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=600, status="filled")
        conn.execute(
            """INSERT INTO paper_orders(account_id,signal_id,side,code,qty,status,
                   created_at) VALUES('sector_rotation',?,'buy','600000',500,'filled',?)""",
            (signal_id, _iso(600)),
        )
        conn.commit()
        summary = ELC.expire_stale_orders(conn, now=NOW, ttl_minutes=30)
        self.assertEqual(0, summary["checked"])
        self.assertEqual("filled", _row(conn, "paper_signals", signal_id)["status"])


class SweepSignalTests(unittest.TestCase):
    def test_cross_day_signal_expires_and_cancels_orders(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=10, day="2026-09-08")
        order_id = _order(conn, signal_id, minutes_ago=10, status="pending_limit")
        summary = ELC.expire_stale_signals(conn, now=NOW, asof_day=TODAY)
        self.assertEqual(1, summary["expired"])
        self.assertEqual("expired", _row(conn, "paper_signals", signal_id)["status"])
        self.assertEqual(1, summary["orders_cancelled"])
        self.assertEqual("superseded", _row(conn, "paper_orders", order_id)["status"])
        reservation = conn.execute(
            "SELECT status FROM paper_capital_reservations WHERE order_key=?",
            (str(order_id),),
        ).fetchone()
        self.assertEqual("released", reservation["status"])

    def test_aged_signal_expires_without_asof_day(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=200)
        summary = ELC.expire_stale_signals(conn, now=NOW, ttl_minutes=90)
        self.assertEqual(1, summary["expired"])
        self.assertIn("有效期", _row(conn, "paper_signals", signal_id)["reason"])

    def test_fresh_today_signals_survive(self):
        conn = _db()
        _signal(conn, minutes_ago=10, status="deferred_capacity")
        summary = ELC.expire_stale_signals(conn, now=NOW, asof_day=TODAY)
        self.assertEqual(1, summary["checked"])
        self.assertEqual(0, summary["expired"])

    def test_terminal_signals_are_never_touched(self):
        conn = _db()
        _signal(conn, minutes_ago=600, status="filled")
        _signal(conn, minutes_ago=600, status="shadow_q3")
        summary = ELC.expire_stale_signals(conn, now=NOW, asof_day=TODAY)
        self.assertEqual(0, summary["checked"])


class WiringGuardTests(unittest.TestCase):
    """源码级护栏：TTL 与分片必须真正接入成交路径与扫描入口。"""

    @staticmethod
    def _source(name="paper_trading.py"):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_buy_order_rejects_stale_signals(self):
        body = self._source()
        self.assertIn("ELC.signal_freshness", body)
        self.assertIn("signal_expired", body)

    def test_buy_order_clamps_to_the_current_slice(self):
        body = self._source()
        self.assertIn("ELC.entry_slice_plan", body)
        self.assertIn("entry_slice_target_qty", body)

    def test_run_slot_sweeps_the_lifecycle(self):
        self.assertIn("ELC.expire_stale_signals", self._source())
        self.assertIn("ELC.expire_stale_orders", self._source())


if __name__ == "__main__":
    unittest.main()
