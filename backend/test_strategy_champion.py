# -*- coding: utf-8 -*-
"""策略 Champion/Challenger 版本管理（PR-16）回归测试。"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import self_evolution as SE
import strategy_champion as SCM

# 用真实时钟做基准（窗口计算相对该时钟）；下单时间相对 NOW 偏移。
NOW = dt.datetime.now()
EVIDENCE = 50


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SCM.ensure_schema(conn)
    SE.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, signal_id INTEGER,
            side TEXT, code TEXT, qty INTEGER, planned_price REAL, filled_price REAL,
            amount REAL, fees REAL, status TEXT, reason TEXT, risk_payload TEXT,
            realized_pnl REAL, created_at TEXT, executed_at TEXT);
        CREATE TABLE paper_positions(
            account_id TEXT, code TEXT, qty INTEGER, cost REAL, entry_date TEXT,
            PRIMARY KEY(account_id, code));
        """
    )
    return conn


def _order(conn, *, side="buy", status="filled", code="600000", amount=10000.0,
           pnl=0.0, executed_at=None, account_id="sector_rotation"):
    stamp = executed_at or NOW.isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
               realized_pnl,executed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (account_id, side, code, 500, amount, status, pnl, stamp, stamp),
    )
    conn.commit()


def _seed_window(conn, *, challenger_pnl, champion_pnl, account_id="trend_pullback"):
    """对照窗（5 天前~开 Challenger）盈利 champion_pnl；影子窗 challenger_pnl。"""
    _order(conn, side="buy", amount=10000.0, account_id=account_id,
           executed_at=(NOW - dt.timedelta(days=9)).isoformat(timespec="seconds"))
    _order(conn, side="sell", amount=11000.0, pnl=champion_pnl, account_id=account_id,
           executed_at=(NOW - dt.timedelta(days=7)).isoformat(timespec="seconds"))
    _order(conn, side="buy", amount=10000.0, account_id=account_id,
           executed_at=(NOW - dt.timedelta(days=2)).isoformat(timespec="seconds"))
    _order(conn, side="sell", amount=11000.0, pnl=challenger_pnl, account_id=account_id,
           executed_at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))


class MetricsTests(unittest.TestCase):
    def test_metrics_are_derived_from_the_ledger(self):
        conn = _db()
        start = (NOW - dt.timedelta(days=10)).isoformat(timespec="seconds")
        _order(conn, side="buy", amount=50000.0, executed_at=start)
        _order(conn, side="sell", amount=52000.0, pnl=800.0,
               executed_at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        _order(conn, side="buy", status="risk_rejected", amount=0.0,
               executed_at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        metrics = SCM.collect_ledger_metrics(conn, "sector_rotation", start,
                                             NOW.isoformat(timespec="seconds"))
        self.assertEqual(800.0 / 50000.0 * 100.0, metrics["return_pct"])
        self.assertEqual(50.0, metrics["execution_fill_rate"])  # 1 成交 / (1 成交 + 1 被拒)

    def test_carried_positions_are_part_of_the_denominator(self):
        conn = _db()
        start = (NOW - dt.timedelta(days=5)).isoformat(timespec="seconds")
        # 窗口前建仓（entry_date 早于窗口），窗口内卖出获利 100 元、窗口内无新买入。
        conn.execute(
            "INSERT INTO paper_positions VALUES('sector_rotation','600000',500,10.0,?)",
            ((NOW - dt.timedelta(days=30)).date().isoformat(),),
        )
        _order(conn, side="sell", amount=5000.0, pnl=100.0,
               executed_at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        metrics = SCM.collect_ledger_metrics(conn, "sector_rotation", start,
                                             NOW.isoformat(timespec="seconds"))
        # 分母 = 0 买入 + 500×10 = 5000 持仓市值 → 收益 2%，而非 10000%。
        self.assertEqual(2.0, metrics["return_pct"])
        self.assertEqual(5000.0, metrics["carried_value"])

    def test_drawdown_from_cumulative_daily_pnl(self):
        conn = _db()
        start = (NOW - dt.timedelta(days=10)).isoformat(timespec="seconds")
        _order(conn, side="buy", amount=100000.0, executed_at=start)
        _order(conn, side="sell", pnl=3000.0,
               executed_at=(NOW - dt.timedelta(days=4)).isoformat(timespec="seconds"))
        _order(conn, side="sell", pnl=-5000.0,
               executed_at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        metrics = SCM.collect_ledger_metrics(conn, "sector_rotation", start,
                                             NOW.isoformat(timespec="seconds"))
        self.assertGreater(metrics["max_drawdown_pct"], 0.0)

    def test_empty_window_is_safe(self):
        conn = _db()
        metrics = SCM.collect_ledger_metrics(
            conn, "sector_rotation",
            (NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"),
            NOW.isoformat(timespec="seconds"))
        self.assertEqual(0.0, metrics["return_pct"])
        self.assertEqual(0.0, metrics["execution_fill_rate"])


class PromotionGateTests(unittest.TestCase):
    def _metrics(self, **overrides):
        base = {
            "return_pct": 1.0, "max_drawdown_pct": 3.0,
            "turnover_amount": 10000.0, "execution_fill_rate": 95.0,
            "concentration_hhi": 0.3,
        }
        base.update(overrides)
        return base

    def test_improvement_with_stable_risk_is_promotable(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, max_drawdown_pct=3.2))
        self.assertTrue(decision["promotable"])

    def test_no_return_improvement_blocks_promotion(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=0.5))
        self.assertFalse(decision["promotable"])
        self.assertIn("收益（必须改善）", decision["failed"])

    def test_drawdown_deterioration_beyond_tolerance_blocks(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, max_drawdown_pct=3.8))
        self.assertFalse(decision["promotable"])
        self.assertIn("最大回撤", decision["failed"])

    def test_turnover_surge_blocks_promotion(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, turnover_amount=20000.0))
        self.assertFalse(decision["promotable"])
        self.assertIn("换手", decision["failed"])

    def test_execution_drop_beyond_five_pp_blocks(self):
        # 容差 = 5 个百分点：95 → 91 应通过，95 → 89 应拒绝。
        within = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, execution_fill_rate=91.0))
        beyond = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, execution_fill_rate=89.0))
        self.assertTrue(within["promotable"])
        self.assertFalse(beyond["promotable"])
        self.assertIn("成交率", beyond["failed"])

    def test_concentration_worsening_blocks_promotion(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, concentration_hhi=0.6))
        self.assertFalse(decision["promotable"])
        self.assertIn("集中度", decision["failed"])


class LifecycleTests(unittest.TestCase):
    def test_open_validates_against_the_full_profile(self):
        conn = _db()
        # 锁定参数（trend_pullback 的 max_delta_threshold）被拒绝。
        result = SCM.open_challenger(
            conn, conn, "trend_pullback", {"max_delta_threshold": 0.006},
            evidence_count=EVIDENCE)
        self.assertFalse(result["opened"])
        self.assertTrue(any("锁定" in v for v in result["violations"]))
        # 证据不足被拒绝。
        result = SCM.open_challenger(
            conn, conn, "trend_pullback", {"max_weight_delta": 0.032},
            evidence_count=2)
        self.assertFalse(result["opened"])
        self.assertTrue(any("证据" in v for v in result["violations"]))

    def test_open_writes_params_into_the_runtime_store(self):
        conn = _db()
        evo = _db()
        SE.init_params(evo)
        result = SCM.open_challenger(
            conn, evo, "trend_pullback", {"max_weight_delta": 0.032},
            evidence_count=EVIDENCE)
        self.assertTrue(result["opened"])
        runtime = SE.get_strategy_params(evo, "trend_pullback")
        self.assertEqual(0.032, runtime["params"]["max_weight_delta"])
        # shadow 行记录了 Champion 参数快照（回滚用）。
        row = conn.execute(
            "SELECT champion_params,status FROM strategy_champion_versions WHERE role='challenger'"
        ).fetchone()
        self.assertEqual("shadow", row["status"])
        self.assertNotEqual(0.032, SCM._json_loads(row["champion_params"])["max_weight_delta"])

    def test_duplicate_shadow_challenger_is_rejected(self):
        conn = _db()
        evo = _db()
        SE.init_params(evo)
        first = SCM.open_challenger(conn, evo, "trend_pullback",
                                    {"max_weight_delta": 0.032}, evidence_count=EVIDENCE)
        self.assertTrue(first["opened"])
        second = SCM.open_challenger(conn, evo, "trend_pullback",
                                     {"max_weight_delta": 0.033}, evidence_count=EVIDENCE)
        self.assertFalse(second["opened"])

    def test_failing_challenger_auto_rolls_back_params(self):
        conn = _db()
        evo = _db()
        SE.init_params(evo)
        _seed_window(conn, champion_pnl=1000.0, challenger_pnl=-1000.0)
        SCM.open_challenger(conn, evo, "trend_pullback",
                            {"max_weight_delta": 0.032},
                            evidence_count=EVIDENCE,
                            now=NOW - dt.timedelta(days=5))
        result = SCM.evaluate_challenger(conn, evo, "trend_pullback", now=NOW)
        self.assertTrue(result["evaluated"])
        self.assertEqual("rolled_back", result["status"])
        # 参数仓已恢复 Champion（默认 0.03）。
        runtime = SE.get_strategy_params(evo, "trend_pullback")
        self.assertEqual(0.03, runtime["params"]["max_weight_delta"])

    def test_passing_challenger_becomes_ready_not_promoted(self):
        conn = _db()
        evo = _db()
        SE.init_params(evo)
        _seed_window(conn, champion_pnl=1000.0, challenger_pnl=2000.0)
        SCM.open_challenger(conn, evo, "trend_pullback",
                            {"max_weight_delta": 0.032},
                            evidence_count=EVIDENCE,
                            now=NOW - dt.timedelta(days=5))
        result = SCM.evaluate_challenger(conn, evo, "trend_pullback", now=NOW)
        self.assertEqual("ready", result["status"])
        # 查看/轮询绝不晋升：不建立任何 champion 版本行。
        champion = conn.execute(
            "SELECT COUNT(*) FROM strategy_champion_versions WHERE role='champion'"
        ).fetchone()[0]
        self.assertEqual(0, champion)

    def test_promotion_only_from_ready(self):
        conn = _db()
        evo = _db()
        SE.init_params(evo)
        _seed_window(conn, champion_pnl=1000.0, challenger_pnl=2000.0)
        SCM.open_challenger(conn, evo, "trend_pullback",
                            {"max_weight_delta": 0.032},
                            evidence_count=EVIDENCE,
                            now=NOW - dt.timedelta(days=5))
        result = SCM.promote_challenger(conn, evo, "trend_pullback", now=NOW)
        self.assertTrue(result["promoted"])
        champion = conn.execute(
            """SELECT params FROM strategy_champion_versions
                WHERE role='champion' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(0.032, SCM._json_loads(champion["params"])["max_weight_delta"])
        # 晋升后再 promote 无可晋升对象。
        again = SCM.promote_challenger(conn, evo, "trend_pullback", now=NOW)
        self.assertFalse(again["promoted"])

    def test_manual_rollback_restores_champion_params(self):
        conn = _db()
        evo = _db()
        SE.init_params(evo)
        SCM.open_challenger(conn, evo, "trend_pullback",
                            {"max_weight_delta": 0.032}, evidence_count=EVIDENCE)
        result = SCM.rollback_challenger(conn, evo, "trend_pullback", reason="证据不足")
        self.assertTrue(result["rolled_back"])
        runtime = SE.get_strategy_params(evo, "trend_pullback")
        self.assertEqual(0.03, runtime["params"]["max_weight_delta"])


class WiringGuardTests(unittest.TestCase):
    @staticmethod
    def _source(name):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_runtime_store_is_the_single_source_of_truth(self):
        body = self._source("strategy_champion.py")
        self.assertIn("adjust_strategy_params", body)
        self.assertIn("_restore_champion_params", body)

    def test_paper_api_opens_the_evolution_store(self):
        body = self._source("paper_trading.py")
        self.assertIn("_evolution_conn", body)
        self.assertIn('DBM.DB_PATHS["adaptive_learning"]', body)

    def test_schema_is_created_on_every_init_path(self):
        body = self._source("paper_trading.py")
        self.assertGreaterEqual(body.count("SCM.ensure_schema(conn)"), 2)


if __name__ == "__main__":
    unittest.main()
