# -*- coding: utf-8 -*-
"""信号/委托生命周期（signal expiry & staged entry）的回归测试。"""
from __future__ import annotations

import datetime as dt
import ast
import inspect
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entry_lifecycle as ELC
import paper_capital_reservations as PCR
import signal_service as SIG
import execution_dispatch as EPD

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

    def test_overnight_plan_signal_is_valid_for_its_target_session(self):
        # 收盘扫描为下一交易日生成的计划信号：挂钟年龄超过 TTL 也不失效。
        signal = {"created_at": "2026-09-04T15:20:00", "intended_date": "2026-09-07"}
        result = ELC.signal_freshness(signal, now=dt.datetime(2026, 9, 7, 14, 0),
                                      asof_day="2026-09-07")
        self.assertTrue(result["usable"])
        self.assertTrue(result["overnight_plan"])

    def test_intraday_signal_aged_past_ttl_is_stale(self):
        signal = {"created_at": "2026-09-09T07:00:00", "intended_date": TODAY}
        result = ELC.signal_freshness(signal, now=NOW, asof_day=TODAY)
        self.assertFalse(result["usable"])
        self.assertIn("有效期", result["reason"])

    def test_age_check_without_asof_day_still_applies(self):
        signal = {"created_at": _iso(200), "intended_date": TODAY}
        self.assertFalse(ELC.signal_freshness(signal, now=NOW)["usable"])

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
        summary = ELC.expire_stale_orders(conn, expire_order=EPD.expire_order, expire_signal=SIG.expire_for_expired_order, release_reservation=PCR.release_order_reservation, now=NOW, ttl_minutes=30)
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
        summary = ELC.expire_stale_orders(conn, expire_order=EPD.expire_order, expire_signal=SIG.expire_for_expired_order, release_reservation=PCR.release_order_reservation, now=NOW, ttl_minutes=30)
        self.assertEqual(0, summary["expired"])
        self.assertEqual("execution_retry", _row(conn, "paper_orders", order_id)["status"])

    def test_date_only_expiry_keeps_inclusive_day_semantics(self):
        # 手动限价单的 date-only expires_at 表示"当日有效"：盘中不能被清扫作废。
        conn = _db()
        signal_id = _signal(conn, minutes_ago=600)
        order_id = _order(conn, signal_id, minutes_ago=600, expires_at="2026-09-09")
        summary = ELC.expire_stale_orders(conn, expire_order=EPD.expire_order, expire_signal=SIG.expire_for_expired_order, release_reservation=PCR.release_order_reservation, now=NOW, ttl_minutes=30)
        self.assertEqual(0, summary["expired"])
        self.assertEqual("execution_retry", _row(conn, "paper_orders", order_id)["status"])
        # 次日同一清扫即作废。
        summary = ELC.expire_stale_orders(
            conn, expire_order=EPD.expire_order, expire_signal=SIG.expire_for_expired_order, release_reservation=PCR.release_order_reservation, now=dt.datetime(2026, 9, 10, 9, 35), ttl_minutes=30)
        self.assertEqual(1, summary["expired"])
        self.assertEqual("expired", _row(conn, "paper_orders", order_id)["status"])

    def test_filled_orders_are_untouched(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=600, status="filled")
        conn.execute(
            """INSERT INTO paper_orders(account_id,signal_id,side,code,qty,status,
                   created_at) VALUES('sector_rotation',?,'buy','600000',500,'filled',?)""",
            (signal_id, _iso(600)),
        )
        conn.commit()
        summary = ELC.expire_stale_orders(conn, expire_order=EPD.expire_order, expire_signal=SIG.expire_for_expired_order, release_reservation=PCR.release_order_reservation, now=NOW, ttl_minutes=30)
        self.assertEqual(0, summary["checked"])
        self.assertEqual("filled", _row(conn, "paper_signals", signal_id)["status"])


class SweepSignalTests(unittest.TestCase):
    def test_cross_day_signal_expires_and_cancels_orders(self):
        conn = _db()
        signal_id = _signal(conn, minutes_ago=10, day="2026-09-08")
        order_id = _order(conn, signal_id, minutes_ago=10, status="pending_limit")
        summary = ELC.expire_stale_signals(conn, release_reservation=PCR.release_order_reservation, now=NOW, asof_day=TODAY)
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
        summary = ELC.expire_stale_signals(conn, release_reservation=PCR.release_order_reservation, now=NOW, ttl_minutes=90)
        self.assertEqual(1, summary["expired"])
        self.assertIn("有效期", _row(conn, "paper_signals", signal_id)["reason"])

    def test_fresh_today_signals_survive(self):
        conn = _db()
        _signal(conn, minutes_ago=10, status="deferred_capacity")
        summary = ELC.expire_stale_signals(conn, release_reservation=PCR.release_order_reservation, now=NOW, asof_day=TODAY)
        self.assertEqual(1, summary["checked"])
        self.assertEqual(0, summary["expired"])

    def test_terminal_signals_are_never_touched(self):
        conn = _db()
        _signal(conn, minutes_ago=600, status="filled")
        _signal(conn, minutes_ago=600, status="shadow_q3")
        summary = ELC.expire_stale_signals(conn, release_reservation=PCR.release_order_reservation, now=NOW, asof_day=TODAY)
        self.assertEqual(0, summary["checked"])


class OrderExpiryOwnerParityTests(unittest.TestCase):
    """与 5c46100 冻结函数比较完整行、统计、SQL 顺序及异常后的事务状态。"""

    BASELINE = Path(__file__).with_name("fixtures") / "order_expiry_5c46100.txt"
    RELEASE_NOW = dt.datetime(2026, 9, 9, 10, 45, 59)

    def state(self, conn):
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("paper_orders", "paper_signals", "paper_capital_reservations")}

    def run_sweep(self, conn, *, legacy, fault=None, finalize=None, repeat=False,
                  signal_sweep=False, **kwargs):
        name = "expire_stale_signals" if signal_sweep else "expire_stale_orders"
        statements = []
        summary, error = None, None
        conn.set_trace_callback(statements.append)
        with mock.patch.object(ELC, "_now", return_value=self.RELEASE_NOW) as clock, \
                mock.patch.object(EPD, "_now", side_effect=AssertionError("writer read clock")):
            if legacy:
                namespace = dict(vars(ELC))
                exec(compile(self.BASELINE.read_text(encoding="utf-8"), str(self.BASELINE), "exec"), namespace)
                sweep = namespace[name]
            else:
                sweep = getattr(ELC, name)
            def release(*args, **kw):
                if fault == "release_value":
                    raise ValueError("release failure")
                return PCR.release_order_reservation(*args, **kw)
            options = {"now": NOW, "ttl_minutes": 30, **kwargs, "release_reservation": release}
            if not legacy and not signal_sweep:
                options.update(expire_order=EPD.expire_order, expire_signal=SIG.expire_for_expired_order)
            try:
                summary = [sweep(conn, **options) for _ in range(2 if repeat else 1)]
            except Exception as exc:
                error = (type(exc).__name__, str(exc))
            if finalize == "commit":
                conn.commit()
            elif finalize == "rollback":
                conn.rollback()
            clock_calls = clock.call_count
        conn.set_trace_callback(None)
        return {"summary": summary, "error": error, "state": self.state(conn),
                "sql": [" ".join(sql.split()) for sql in statements],
                "clock_calls": clock_calls, "in_transaction": conn.in_transaction}

    def compare(self, *, order=None, signal=None, link="normal", fault=None,
                finalize=None, repeat=False, signal_sweep=False, extra_order=False, **kwargs):
        conn = _db()
        self.addCleanup(conn.close)
        for table in ("paper_orders", "paper_signals"):
            for column, kind in (("cycle_id", "INTEGER"), ("strategy_id", "TEXT"),
                                 ("strategy_version", "INTEGER"), ("strategy_checksum", "TEXT")):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        sid = _signal(conn)
        order_id = _order(conn, sid if link == "normal" else (999 if link == "missing" else None),
                          minutes_ago=60)
        if extra_order:
            _order(conn, None, minutes_ago=1)
        for table, values, row_id in (("paper_orders", order or {}, order_id),
                                      ("paper_signals", signal or {}, sid)):
            for column, value in values.items():
                conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (value, row_id))
            conn.execute(f"UPDATE {table} SET cycle_id=8,strategy_id='frozen',"
                         "strategy_version=7,strategy_checksum='checksum'")
        conn.execute("UPDATE paper_signals SET payload='  raw signal detail  '")
        conn.execute("UPDATE paper_orders SET risk_payload='  raw order detail  '")
        trigger_table = {"release_sqlite": "paper_capital_reservations",
                         "order_sqlite": "paper_orders", "signal_sqlite": "paper_signals"}.get(fault)
        if trigger_table:
            conn.execute(f"CREATE TRIGGER fail_write BEFORE UPDATE ON {trigger_table} "
                         "BEGIN SELECT RAISE(ABORT, 'write failed'); END")
        conn.commit()
        before = self.state(conn)
        old = sqlite3.connect(":memory:")
        self.addCleanup(old.close)
        old.row_factory = sqlite3.Row
        conn.backup(old)
        opts = dict(fault=fault, finalize=finalize, repeat=repeat, signal_sweep=signal_sweep, **kwargs)
        expected = self.run_sweep(old, legacy=True, **opts)
        actual = self.run_sweep(conn, legacy=False, **opts)
        self.assertEqual(expected, actual)
        return actual, before

    def test_deadline_boundaries_priority_and_unparseable_times_match(self):
        for values in (
            {"created_at": _iso(30)}, {"created_at": _iso(29)}, {"created_at": _iso(31)},
            {"expires_at": NOW.isoformat()},
            {"expires_at": (NOW - dt.timedelta(seconds=1)).isoformat(), "created_at": _iso(1)},
            {"expires_at": (NOW + dt.timedelta(seconds=1)).isoformat(), "created_at": _iso(600)},
            {"expires_at": TODAY, "created_at": _iso(600)},
            {"expires_at": "2026-09-08", "created_at": _iso(1)},
            {"expires_at": "invalid", "created_at": _iso(31)},
            {"expires_at": None, "created_at": None},
            {"expires_at": "invalid", "created_at": "invalid"},
        ):
            with self.subTest(order=values):
                self.compare(order=values)

    def test_all_order_statuses_and_sell_exclusion_match(self):
        for status in (*ELC.RECOVERABLE_ORDER_STATUSES, "filled", "pending_execution",
                       "partially_filled", "awaiting_batch", "pending_verification", "expired"):
            with self.subTest(status=status):
                self.compare(order={"status": status})
        actual, before = self.compare(order={"side": "sell"})
        self.assertEqual(before, actual["state"])
        self.assertEqual(0, actual["summary"][0]["checked"])

    def test_linked_signal_states_missing_links_and_reasons_match(self):
        for status in (*ELC.RETRY_SIGNAL_STATUSES, "filled", "rejected", "approved", "expired", None, ""):
            for reason in (None, "", "原始原因；保留"):
                with self.subTest(status=status, reason=reason):
                    self.compare(signal={"status": status, "reason": reason}, order={"reason": reason})
        for link in ("none", "missing"):
            with self.subTest(link=link):
                self.compare(link=link)

    def test_repeat_sweep_statistics_and_different_clocks_match(self):
        actual, _ = self.compare(repeat=True, extra_order=True)
        self.assertEqual((2, 1), (actual["summary"][0]["checked"], actual["summary"][0]["expired"]))
        self.assertEqual((1, 0), (actual["summary"][1]["checked"], actual["summary"][1]["expired"]))
        reservation = actual["state"]["paper_capital_reservations"][0]
        self.assertEqual(self.RELEASE_NOW.isoformat(timespec="seconds"), reservation[-1])
        self.compare(now=None)

    def test_sql_order_and_uncommitted_transaction_match(self):
        actual, _ = self.compare()
        writes = [sql for sql in actual["sql"] if sql.startswith("UPDATE")]
        self.assertTrue(writes[0].startswith("UPDATE paper_capital_reservations"))
        self.assertTrue(writes[1].startswith("UPDATE paper_orders"))
        self.assertTrue(writes[2].startswith("UPDATE paper_signals"))
        self.assertNotIn("COMMIT", actual["sql"])
        self.assertTrue(actual["in_transaction"])

    def test_reservation_sqlite_error_is_tolerated_but_other_errors_propagate(self):
        actual, _ = self.compare(fault="release_sqlite")
        self.assertIsNone(actual["error"])
        self.assertEqual(1, actual["summary"][0]["expired"])
        actual, before = self.compare(fault="release_value")
        self.assertEqual(("ValueError", "release failure"), actual["error"])
        self.assertEqual(before, actual["state"])

    def test_order_and_signal_errors_keep_partial_transaction_and_caller_finalization(self):
        for fault in ("order_sqlite", "signal_sqlite"):
            for finalize in (None, "commit", "rollback"):
                with self.subTest(fault=fault, finalize=finalize):
                    actual, before = self.compare(fault=fault, finalize=finalize)
                    self.assertEqual(("IntegrityError", "write failed"), actual["error"])
                    if finalize == "rollback":
                        self.assertEqual(before, actual["state"])
                    else:
                        self.assertNotEqual(before, actual["state"])
        actual, before = self.compare(finalize="rollback")
        self.assertEqual(before, actual["state"])

    def test_signal_expiry_still_uses_superseded_and_its_original_order(self):
        actual, _ = self.compare(signal_sweep=True, order={"created_at": _iso(1)},
                                 signal={"created_at": _iso(200)})
        writes = [sql for sql in actual["sql"] if sql.startswith("UPDATE")]
        self.assertTrue(writes[0].startswith("UPDATE paper_signals"))
        self.assertTrue(writes[1].startswith("UPDATE paper_capital_reservations"))
        self.assertIn("status='superseded'", writes[2])
        self.assertEqual(1, actual["summary"][0]["orders_cancelled"])
        baseline = ast.parse(self.BASELINE.read_text(encoding="utf-8"))
        frozen = next(node for node in baseline.body if isinstance(node, ast.FunctionDef)
                      and node.name == "expire_stale_signals")
        current = ast.parse(inspect.getsource(ELC.expire_stale_signals)).body[0]
        self.assertEqual(ast.dump(frozen), ast.dump(current))


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
        self.assertIn("PSZ.entry_quantity(", body)
        self.assertIn("slice_request=PSZ.EntrySliceRequest(", body)
        self.assertIn("plan_builder=ELC.entry_slice_plan", body)
        self.assertIn("quantity.slice_state, quantity.slice_detail", body)
        decision = self._source("paper_sizing.py")
        self.assertIn("entry_slice_target_qty", decision)
        self.assertIn("min(int(qty or 0), target)", decision)

    def test_run_slot_sweeps_the_lifecycle(self):
        """扫描入口仍会清扫 lifecycle。

        PR #113 之后清扫动作由 ``paper_slot_service.run_preflight`` 承担，
        facade 通过 ``PSS.run_preflight`` 委托。护栏的意图不变 —— 清扫必须
        仍在 slot 路径上发生 —— 所以这里同时锁住"service 里有清扫"和
        "facade 确实委托给 service"，删掉任一侧都会失败。
        """
        service = self._source("paper_slot_service.py")
        self.assertIn("ELC.expire_stale_signals", service)
        self.assertIn("ELC.expire_stale_orders", service)
        self.assertIn("PSS.run_preflight(", self._source())


if __name__ == "__main__":
    unittest.main()
