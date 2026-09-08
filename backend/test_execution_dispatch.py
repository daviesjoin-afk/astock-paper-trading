# -*- coding: utf-8 -*-
"""执行器（PR-11）：批量窗口、人工核验、TTL 清扫的回归测试。"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import unittest

import execution_dispatch as EPD
import execution_profiles as EPF

ROTATION = EPF.EXECUTION_PROFILES["rotation"]
EVENT = EPF.EXECUTION_PROFILES["event_driven"]
FLOW = EPF.EXECUTION_PROFILES["flow_momentum"]
BREAKOUT = EPF.EXECUTION_PROFILES["breakout"]


def _profile(family: str) -> dict:
    resolved = dict(EPF.EXECUTION_PROFILES[family])
    resolved["family"] = family
    resolved["version"] = EPF.EXECUTION_PROFILE_VERSION
    return resolved


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


def _order(
    conn,
    *,
    status=EPD.BATCH_HOLD_STATUS,
    signal_id=1,
    expires_at=None,
    payload=None,
    account_id="sector_rotation",
    code="600000",
):
    conn.execute(
        """INSERT INTO paper_signals(id,account_id,code,intended_date,signal_date,
               status,reason,payload,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (signal_id, account_id, code, "2026-09-08", "2026-09-08",
         "deferred_capacity", "等待放行", "{}", "2026-09-08 10:00:00"),
    )
    cursor = conn.execute(
        """INSERT INTO paper_orders(account_id,signal_id,side,code,name,qty,planned_price,
               status,reason,risk_payload,created_at,expires_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, signal_id, "buy", code, "浦发银行", 500, 10.0, status,
         "挂起", json.dumps(payload or {}, ensure_ascii=False),
         "2026-09-08 10:00:00", expires_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _row(conn, table, row_id):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())


class BatchWindowTests(unittest.TestCase):
    def test_inside_first_window(self):
        state = EPD.batch_window_state(dt.datetime(2026, 9, 8, 14, 32))
        self.assertTrue(state["in_window"])
        self.assertEqual("14:30", state["current_window"]["start"])

    def test_between_windows_points_at_the_next_one(self):
        state = EPD.batch_window_state(dt.datetime(2026, 9, 8, 14, 47))
        self.assertFalse(state["in_window"])
        self.assertEqual("14:50", state["next_window"]["start"])

    def test_after_last_window_has_no_next(self):
        state = EPD.batch_window_state(dt.datetime(2026, 9, 8, 15, 20))
        self.assertFalse(state["in_window"])
        self.assertIsNone(state["next_window"])

    def test_custom_windows_are_honoured(self):
        state = EPD.batch_window_state(
            dt.datetime(2026, 9, 8, 9, 40), windows=(("09:35", "09:45"),),
        )
        self.assertTrue(state["in_window"])


class PlanDispatchTests(unittest.TestCase):
    def test_breakout_never_gates(self):
        plan = EPD.plan_execution_dispatch(_profile("breakout"))
        self.assertEqual("none", plan["gate"])
        self.assertIsNone(plan["status"])

    def test_batch_profile_holds_outside_the_window(self):
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 10, 5),
        )
        self.assertEqual("batch", plan["gate"])
        self.assertEqual(EPD.BATCH_HOLD_STATUS, plan["status"])
        self.assertIsNotNone(plan["expires_at"])
        self.assertTrue(plan["reason"])
        self.assertTrue(plan["explanation"])

    def test_batch_profile_fills_inside_the_window(self):
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 14, 33),
        )
        self.assertEqual("none", plan["gate"])
        self.assertTrue(plan["in_batch_window"])

    def test_batch_gate_can_be_switched_off(self):
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 10, 5),
            dispatch_settings={"execution_batch_gate": False},
        )
        self.assertEqual("none", plan["gate"])

    def test_verification_gate_is_off_by_default(self):
        plan = EPD.plan_execution_dispatch(_profile("event_driven"))
        self.assertEqual("none", plan["gate"])
        self.assertIn("关闭", "".join(plan["explanation"]))

    def test_verification_gate_holds_when_enabled(self):
        plan = EPD.plan_execution_dispatch(
            _profile("event_driven"),
            dispatch_settings={"execution_verification_gate": True},
        )
        self.assertEqual("verification", plan["gate"])
        self.assertEqual(EPD.VERIFICATION_HOLD_STATUS, plan["status"])

    def test_approved_signal_skips_verification_gate(self):
        plan = EPD.plan_execution_dispatch(
            _profile("event_driven"),
            dispatch_settings={"execution_verification_gate": True},
            signal_payload={"execution_verification": {"approved": True}},
        )
        self.assertEqual("none", plan["gate"])

    def test_expiry_is_anchored_to_the_next_window(self):
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 10, 5),
        )
        # 下一窗口 14:30 + 45 分钟宽限 = 15:15
        self.assertEqual("2026-09-08T15:15:00", plan["expires_at"])

    def test_no_window_left_uses_a_grace_period(self):
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 15, 20),
        )
        self.assertEqual("batch", plan["gate"])
        self.assertEqual("2026-09-08T16:05:00", plan["expires_at"])


class GatedOrderTests(unittest.TestCase):
    def test_active_gated_order_is_found_once(self):
        conn = _db()
        order_id = _order(conn)
        found = EPD.active_gated_order(conn, 1)
        self.assertIsNotNone(found)
        self.assertEqual(order_id, int(found["id"]))

    def test_no_active_gate_after_retirement(self):
        conn = _db()
        _order(conn)
        self.assertEqual(1, EPD.retire_gated_orders(conn, 1))
        self.assertIsNone(EPD.active_gated_order(conn, 1))

    def test_settings_defaults(self):
        flags = EPD.settings(None)
        self.assertTrue(flags["execution_batch_gate"])
        self.assertFalse(flags["execution_verification_gate"])
        self.assertTrue(flags["execution_ttl_sweep"])


class SweepTests(unittest.TestCase):
    def test_expired_batch_order_is_released_not_lost(self):
        conn = _db()
        order_id = _order(conn, expires_at="2026-09-08T15:15:00")
        summary = EPD.run_execution_dispatch(conn, now=dt.datetime(2026, 9, 8, 15, 30))
        self.assertEqual(1, summary["released"])
        order = _row(conn, "paper_orders", order_id)
        self.assertEqual("execution_retry", order["status"])
        signal = _row(conn, "paper_signals", 1)
        self.assertEqual("pending", signal["status"])

    def test_strict_ttl_order_expires_terminally(self):
        conn = _db()
        order_id = _order(
            conn, expires_at="2026-09-08T10:05:00",
            payload={"execution_profile": {"strict_ttl": True}},
        )
        summary = EPD.run_execution_dispatch(conn, now=dt.datetime(2026, 9, 8, 10, 10))
        self.assertEqual(1, summary["expired"])
        order = _row(conn, "paper_orders", order_id)
        self.assertEqual(EPD.EXPIRED_ORDER_STATUS, order["status"])
        self.assertIsNotNone(order["cancelled_at"])
        self.assertEqual("rejected", _row(conn, "paper_signals", 1)["status"])

    def test_terminal_signal_retires_the_gate(self):
        conn = _db()
        order_id = _order(conn)
        conn.execute("UPDATE paper_signals SET status='filled' WHERE id=1")
        summary = EPD.run_execution_dispatch(conn, now=dt.datetime(2026, 9, 8, 15, 30))
        self.assertEqual(1, summary["retired"])
        self.assertEqual("superseded", _row(conn, "paper_orders", order_id)["status"])

    def test_pending_orders_are_untouched(self):
        conn = _db()
        order_id = _order(conn, expires_at="2026-09-08T15:15:00")
        summary = EPD.run_execution_dispatch(conn, now=dt.datetime(2026, 9, 8, 11, 0))
        self.assertEqual(0, summary["released"])
        self.assertEqual(EPD.BATCH_HOLD_STATUS, _row(conn, "paper_orders", order_id)["status"])

    def test_sweep_can_be_disabled(self):
        conn = _db()
        order_id = _order(conn, expires_at="2026-09-08T15:15:00")
        summary = EPD.run_execution_dispatch(
            conn, now=dt.datetime(2026, 9, 8, 15, 30),
            dispatch_settings={"execution_ttl_sweep": False},
        )
        self.assertEqual(0, summary["checked"])
        self.assertEqual(EPD.BATCH_HOLD_STATUS, _row(conn, "paper_orders", order_id)["status"])

    def test_reservation_is_released_on_termination(self):
        conn = _db()
        order_id = _order(conn)
        conn.execute(
            "INSERT INTO paper_capital_reservations(order_key,status) VALUES(?,'reserved')",
            (str(order_id),),
        )
        conn.commit()
        EPD.retire_gated_orders(conn, 1)
        row = conn.execute(
            "SELECT status FROM paper_capital_reservations WHERE order_key=?",
            (str(order_id),),
        ).fetchone()
        self.assertEqual("released", row["status"])


class VerificationTests(unittest.TestCase):
    def test_approval_releases_into_the_retry_pipeline(self):
        conn = _db()
        order_id = _order(conn, status=EPD.VERIFICATION_HOLD_STATUS)
        result = EPD.resolve_verification(
            conn, order_id, approved=True, operator="运营A", note="财报已核实",
        )
        self.assertTrue(result["ok"])
        self.assertEqual("execution_retry", _row(conn, "paper_orders", order_id)["status"])
        self.assertEqual("pending", _row(conn, "paper_signals", 1)["status"])
        payload = json.loads(_row(conn, "paper_signals", 1)["payload"] or "{}")
        self.assertTrue(payload["execution_verification"]["approved"])
        self.assertEqual("运营A", payload["execution_verification"]["operator"])

    def test_rejection_cancels_the_order(self):
        conn = _db()
        order_id = _order(conn, status=EPD.VERIFICATION_HOLD_STATUS)
        result = EPD.resolve_verification(conn, order_id, approved=False, note="证据不足")
        self.assertTrue(result["ok"])
        self.assertEqual(EPD.CANCELLED_ORDER_STATUS, result["status"])
        order = _row(conn, "paper_orders", order_id)
        self.assertEqual(EPD.CANCELLED_ORDER_STATUS, order["status"])
        self.assertIsNotNone(order["cancelled_at"])
        self.assertEqual("rejected", _row(conn, "paper_signals", 1)["status"])

    def test_unknown_order_is_rejected(self):
        conn = _db()
        result = EPD.resolve_verification(conn, 999, approved=True)
        self.assertFalse(result["ok"])

    def test_non_gated_order_cannot_be_verified(self):
        conn = _db()
        order_id = _order(conn, status="filled")
        result = EPD.resolve_verification(conn, order_id, approved=True)
        self.assertFalse(result["ok"])
        self.assertIn("不在核验队列", result["reason"])

    def test_queue_only_lists_verification_holds(self):
        conn = _db()
        _order(conn, status=EPD.VERIFICATION_HOLD_STATUS, signal_id=1)
        _order(conn, status=EPD.BATCH_HOLD_STATUS, signal_id=2)
        self.assertEqual(1, len(EPD.verification_queue(conn)))
        self.assertEqual(1, len(EPD.batch_queue(conn)))


class PersistenceTests(unittest.TestCase):
    """P1 回归：驳回与放行必须跨越"信号被日内重建"依然生效。"""

    def _reject_then_recreate(self, conn):
        order_id = _order(conn, status=EPD.VERIFICATION_HOLD_STATUS, signal_id=1,
                          account_id="reported_profit_breakout", code="600519")
        EPD.resolve_verification(conn, order_id, approved=False, note="证据不足")
        # 日内引导把普通 rejected 信号 supersede 掉，并用新的 signal 行重建候选。
        conn.execute("UPDATE paper_signals SET status='superseded' WHERE id=1")
        conn.execute(
            """INSERT INTO paper_signals(id,account_id,code,intended_date,signal_date,
                   status,reason,payload,created_at)
               VALUES(2,?,?,?,?,'pending','重新入选','{}','2026-09-08 13:00:00')""",
            ("reported_profit_breakout", "600519", "2026-09-08", "2026-09-08"),
        )
        conn.commit()
        return 2

    def test_rejection_survives_candidate_regeneration(self):
        conn = _db()
        self._reject_then_recreate(conn)
        self.assertTrue(EPD.is_verification_rejected(
            conn, "reported_profit_breakout", "600519", "2026-09-08"))
        # 重建出来的新 signal 行 payload 是空的，必须靠历史行判定。
        plan = EPD.plan_execution_dispatch(
            _profile("event_driven"), signal_payload={},
            dispatch_settings={"execution_verification_gate": True},
            verification_rejected=EPD.is_verification_rejected(
                conn, "reported_profit_breakout", "600519", "2026-09-08"),
        )
        self.assertTrue(plan["blocked"])
        self.assertEqual("none", plan["gate"])
        self.assertIn("驳回", plan["blocked_reason"])

    def test_other_codes_are_not_blocked(self):
        conn = _db()
        self._reject_then_recreate(conn)
        self.assertFalse(EPD.is_verification_rejected(
            conn, "reported_profit_breakout", "000001", "2026-09-08"))

    def test_rejection_expires_on_another_day(self):
        conn = _db()
        self._reject_then_recreate(conn)
        self.assertFalse(EPD.is_verification_rejected(
            conn, "reported_profit_breakout", "600519", "2026-09-09"))

    def test_batch_release_marker_is_one_use_and_same_day(self):
        today = {"execution_batch_release": {"released": True, "at": "2026-09-08T15:20:00"}}
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 15, 25),
            signal_payload=today,
        )
        self.assertEqual("none", plan["gate"])
        self.assertTrue(plan["batch_released"])

        stale = {"execution_batch_release": {"released": True, "at": "2026-09-07T15:20:00"}}
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 10, 5),
            signal_payload=stale,
        )
        self.assertEqual("batch", plan["gate"])

    def test_expired_release_writes_the_batch_marker(self):
        conn = _db()
        _order(conn, expires_at="2026-09-08T15:15:00")
        EPD.run_execution_dispatch(conn, now=dt.datetime(2026, 9, 8, 15, 30))
        payload = json.loads(_row(conn, "paper_signals", 1)["payload"] or "{}")
        self.assertTrue(payload["execution_batch_release"]["released"])
        # 标记写入后，同一信号在同一轮次内不会再被挂起。
        plan = EPD.plan_execution_dispatch(
            _profile("rotation"), now=dt.datetime(2026, 9, 8, 15, 31),
            signal_payload=payload,
        )
        self.assertEqual("none", plan["gate"])

    def test_approval_marker_is_recorded(self):
        conn = _db()
        order_id = _order(conn, status=EPD.VERIFICATION_HOLD_STATUS, signal_id=1)
        EPD.resolve_verification(conn, order_id, approved=True, operator="运营A")
        payload = json.loads(_row(conn, "paper_signals", 1)["payload"] or "{}")
        self.assertTrue(payload["execution_verification"]["approved"])
        plan = EPD.plan_execution_dispatch(
            _profile("event_driven"), signal_payload=payload,
            dispatch_settings={"execution_verification_gate": True},
        )
        self.assertEqual("none", plan["gate"])


class OverviewTests(unittest.TestCase):
    def test_overview_exposes_settings_windows_and_queues(self):
        conn = _db()
        _order(
            conn, signal_id=1,
            payload={"execution_profile": _profile("rotation"),
                     "sizing": {"target_amount": 5000.0}},
        )
        overview = EPD.dispatch_overview(conn, now=dt.datetime(2026, 9, 8, 10, 5))
        self.assertEqual(EPD.EXECUTION_DISPATCH_VERSION, overview["engine"])
        self.assertIn("execution_batch_gate", overview["settings"])
        self.assertEqual("14:30", overview["windows"]["next_window"]["start"])
        self.assertEqual(1, len(overview["batch_queue"]))
        self.assertEqual([], overview["verification_queue"])
        self.assertEqual("rotation", overview["batch_queue"][0]["family"])
        self.assertEqual("轮动批量", overview["batch_queue"][0]["profile_label"])
        self.assertEqual(5000.0, overview["batch_queue"][0]["target_amount"])


class WiringGuardTests(unittest.TestCase):
    """源码级护栏：执行器必须真正接入扫描与成交路径。"""

    @staticmethod
    def _source(name="paper_trading.py"):
        import os

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_run_slot_sweeps_the_dispatch_queue(self):
        source = self._source()
        self.assertIn("EPD.run_execution_dispatch", source)

    def test_buy_order_creates_gated_orders(self):
        source = self._source()
        self.assertIn("EPD.plan_execution_dispatch", source)
        self.assertIn("order_status in EPD.GATED_ORDER_STATUSES", source)


if __name__ == "__main__":
    unittest.main()
