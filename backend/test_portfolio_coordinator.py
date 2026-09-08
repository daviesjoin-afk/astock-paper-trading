# -*- coding: utf-8 -*-
"""跨策略组合协调器（symbol/industry/theme 聚合 + 意图优先级）的回归测试。"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import portfolio_coordinator as PCO

POSITIONS = [
    {"code": "600000", "qty": 1000, "cost": 10.0, "industry": "银行", "account_id": "a"},
    {"code": "600000", "qty": 500, "cost": 11.0, "industry": "银行", "account_id": "b"},
    {"code": "000001", "qty": 2000, "cost": 20.0, "industry": "白酒", "account_id": "a"},
]


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, signal_id INTEGER,
            side TEXT, code TEXT, qty INTEGER, planned_price REAL, status TEXT);
        CREATE TABLE paper_signals(id INTEGER PRIMARY KEY AUTOINCREMENT);
        """
    )
    return conn


def _order(conn, code, qty, price, *, side="buy", status="pending_limit", signal_id=None):
    conn.execute(
        """INSERT INTO paper_orders(account_id,signal_id,side,code,qty,planned_price,status)
           VALUES('a',?,?,?,?,?,?)""",
        (signal_id, side, code, qty, price, status),
    )
    conn.commit()


class IntentPriorityTests(unittest.TestCase):
    def test_priority_order_matches_p0_to_p5(self):
        self.assertEqual(
            ["P0", "P1", "P2", "P3", "P4", "P5"],
            [name for name, _, _ in PCO.INTENT_PRIORITY],
        )

    def test_risk_exit_classifies_as_p0(self):
        intent = PCO.classify_intent("sell", "hard_stop touched; 崩盘清仓")
        self.assertEqual("P0", intent["priority"])

    def test_take_profit_classifies_as_p1(self):
        self.assertEqual("P1", PCO.classify_intent("sell", "take_profit scale_out")["priority"])

    def test_buy_classifies_as_p4_and_add_as_p5(self):
        self.assertEqual("P4", PCO.classify_intent("buy", "entry")["priority"])
        self.assertEqual("P5", PCO.classify_intent("buy", "scale_in 确认加仓")["priority"])

    def test_sort_puts_risk_exit_before_add_position(self):
        intents = [
            {"order": 1, "priority": "P5", "side": "buy"},
            {"order": 2, "priority": "P0", "side": "sell"},
            {"order": 3, "priority": "P4", "side": "buy"},
        ]
        ordered = PCO.sort_intents_by_priority(intents)
        self.assertEqual(["P0", "P4", "P5"], [item["priority"] for item in ordered])


class AggregateExposureTests(unittest.TestCase):
    def test_same_symbol_is_aggregated_across_strategies(self):
        quotes = {"600000": {"price": 12.0}, "000001": {"price": 20.0}}
        report = PCO.aggregate_exposure(POSITIONS, quotes)
        # (1000+500) × 12 = 18000
        self.assertEqual(18000.0, report["by_symbol"]["600000"])
        self.assertEqual(40000.0, report["by_symbol"]["000001"])
        self.assertEqual(58000.0, report["total_value"])

    def test_industry_and_theme_rollups(self):
        quotes = {"600000": {"price": 12.0}, "000001": {"price": 20.0}}
        report = PCO.aggregate_exposure(POSITIONS, quotes)
        self.assertEqual(18000.0, report["by_industry"]["银行"])
        # 银行→金融、白酒→消费
        self.assertEqual(18000.0, report["by_theme"]["金融"])
        self.assertEqual(40000.0, report["by_theme"]["消费"])

    def test_unknown_industry_becomes_its_own_theme(self):
        report = PCO.aggregate_exposure(
            [{"code": "300001", "qty": 100, "cost": 5.0, "industry": "冷门行业"}])
        self.assertEqual("冷门行业", PCO.theme_for("冷门行业"))
        self.assertEqual(500.0, report["by_theme"]["冷门行业"])


class SymbolHeadroomTests(unittest.TestCase):
    def _aggregate(self, pending=None):
        quotes = {"600000": {"price": 12.0}}
        return PCO.aggregate_exposure(
            [POSITIONS[0], POSITIONS[1]], quotes, pending_by_symbol=pending or {})

    def test_pending_buys_count_against_the_cap(self):
        aggregate = self._aggregate(pending={"600000": 3000.0})
        check = PCO.symbol_headroom("600000", aggregate, cap_amount=20000.0)
        self.assertFalse(check["allowed"])  # 18000 持仓 + 3000 在途 > 20000
        self.assertIn("在途", check["reason"])

    def test_headroom_is_the_cap_minus_committed(self):
        aggregate = self._aggregate(pending={"600000": 500.0})
        check = PCO.symbol_headroom("600000", aggregate, cap_amount=20000.0)
        self.assertTrue(check["allowed"])
        self.assertEqual(1500.0, check["headroom_amount"])

    def test_zero_cap_means_disabled(self):
        check = PCO.symbol_headroom("600000", self._aggregate(), cap_amount=0)
        self.assertTrue(check["allowed"])
        self.assertIsNone(check["headroom_amount"])


class PendingAmountTests(unittest.TestCase):
    def test_active_buy_orders_are_summed_by_symbol(self):
        conn = _db()
        _order(conn, "600000", 500, 10.0)
        _order(conn, "600000", 300, 20.0, status="execution_retry")
        _order(conn, "000001", 100, 5.0)
        amounts = PCO.pending_symbol_amounts(conn)
        self.assertEqual(11000.0, amounts["600000"])  # 5000 + 6000
        self.assertEqual(500.0, amounts["000001"])

    def test_filled_and_superseded_orders_are_excluded(self):
        conn = _db()
        _order(conn, "600000", 500, 10.0, status="filled")
        _order(conn, "600000", 300, 10.0, status="superseded")
        self.assertEqual({}, PCO.pending_symbol_amounts(conn))

    def test_same_signal_can_be_excluded_for_retry_sizing(self):
        conn = _db()
        _order(conn, "600000", 500, 10.0, signal_id=7)
        _order(conn, "600000", 200, 10.0, signal_id=8)
        amounts = PCO.pending_symbol_amounts(conn, exclude_signal_id=7)
        self.assertEqual(2000.0, amounts["600000"])

    def test_pending_risk_exit_codes(self):
        conn = _db()
        _order(conn, "600000", 500, 10.0, side="sell", status="unfilled_limit_down")
        _order(conn, "000001", 100, 5.0, side="sell", status="filled")
        self.assertEqual({"600000"}, PCO.pending_risk_exit_codes(conn))

    def test_db_error_is_tolerated(self):
        class Broken:
            def execute(self, *args, **kwargs):
                raise sqlite3.Error("boom")

        self.assertEqual({}, PCO.pending_symbol_amounts(Broken()))
        self.assertEqual(set(), PCO.pending_risk_exit_codes(Broken()))


class WiringGuardTests(unittest.TestCase):
    @staticmethod
    def _source(name="paper_trading.py"):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_buy_sizing_includes_cross_strategy_pending(self):
        body = self._source()
        self.assertIn("PCO.pending_symbol_amounts", body)
        self.assertIn("exclude_signal_id=signal.get", body)

    def test_scale_in_yields_to_pending_risk_exit(self):
        body = self._source()
        self.assertIn("PCO.pending_risk_exit_codes", body)
        self.assertIn("P0 风控退出在途", body)

    def test_aggregate_cap_setting_is_wired(self):
        self.assertIn("symbol_aggregate_cap_pct", self._source("runtime_settings.py"))
        self.assertIn("symbol_aggregate_cap_pct", self._source())


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """回归护栏：等待池标记不算敞口、余量钳制与加仓上限已接线。"""

    def test_waitlist_markers_are_not_committed_exposure(self):
        conn = _db()
        _order(conn, "600000", 500, 10.0, status="deferred_capacity")
        _order(conn, "600000", 300, 10.0, status="entry_frozen_waitlist")
        _order(conn, "000001", 100, 10.0, status="pending_limit")
        amounts = PCO.pending_symbol_amounts(conn)
        self.assertNotIn("600000", amounts)
        self.assertEqual(1000.0, amounts["000001"])

    def test_headroom_clamp_is_wired_into_both_buy_paths(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("symbol_headroom_clamped_qty", source)
        # 新开仓与确认加仓两条路径都读取聚合上限设置并应用余量钳制。
        self.assertGreaterEqual(source.count('RSET.get(conn, "symbol_aggregate_cap_pct"'), 2)
        self.assertGreaterEqual(source.count("symbol_headroom("), 2)
