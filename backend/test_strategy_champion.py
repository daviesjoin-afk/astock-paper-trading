# -*- coding: utf-8 -*-
"""策略 Champion/Challenger 版本管理（PR-16）回归测试。"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_champion as SCM

# 用真实时钟做基准（模块内部窗口计算相对该时钟）；下单时间相对 NOW 偏移。
NOW = dt.datetime.now()
PARAMS = {"max_weight_delta": 0.033, "hold_bias": 0.2}


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SCM.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, signal_id INTEGER,
            side TEXT, code TEXT, qty INTEGER, planned_price REAL, filled_price REAL,
            amount REAL, fees REAL, status TEXT, reason TEXT, risk_payload TEXT,
            realized_pnl REAL, created_at TEXT, executed_at TEXT);
        CREATE TABLE paper_positions(
            account_id TEXT, code TEXT, qty INTEGER, cost REAL,
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


class MetricsTests(unittest.TestCase):
    def test_metrics_are_derived_from_the_ledger(self):
        conn = _db()
        window = dt.timedelta(days=10)
        start = (NOW - window).isoformat(timespec="seconds")
        _order(conn, side="buy", amount=50000.0, executed_at=start)
        _order(conn, side="sell", amount=52000.0, pnl=800.0,
               executed_at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        _order(conn, side="buy", status="risk_rejected", amount=0.0)
        metrics = SCM.collect_ledger_metrics(conn, "sector_rotation", start,
                                             NOW.isoformat(timespec="seconds"))
        self.assertEqual(800.0 / 50000.0 * 100.0, metrics["return_pct"])
        self.assertEqual(100.0, metrics["execution_fill_rate"])
        self.assertGreater(metrics["turnover_amount"], 0.0)
        self.assertGreaterEqual(metrics["concentration_hhi"], 0.0)

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
        # 峰值 +3000 → 回撤 5000 → 5%
        self.assertEqual(5.0, metrics["max_drawdown_pct"])

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
        self.assertEqual([], decision["failed"])

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

    def test_execution_drop_blocks_promotion(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, execution_fill_rate=85.0))
        self.assertFalse(decision["promotable"])
        self.assertIn("成交率", decision["failed"])

    def test_concentration_worsening_blocks_promotion(self):
        decision = SCM.compare_for_promotion(
            self._metrics(), self._metrics(return_pct=2.0, concentration_hhi=0.6))
        self.assertFalse(decision["promotable"])
        self.assertIn("集中度", decision["failed"])


class LifecycleTests(unittest.TestCase):
    def _seed_champion(self, conn):
        conn.execute(
            """INSERT INTO strategy_champion_versions(strategy_id,role,params,source,
                   status,proposed_at,created_at)
               VALUES('sector_rotation','champion','{}','init','promoted',?,?)""",
            (NOW.isoformat(timespec="seconds"), NOW.isoformat(timespec="seconds")),
        )
        conn.commit()

    def test_open_challenger_clamps_to_the_profile(self):
        conn = _db()
        self._seed_champion(conn)
        result = SCM.open_challenger(conn, "sector_rotation",
                                     {"max_weight_delta": 0.9})
        self.assertEqual("shadow", result["status"])
        self.assertLessEqual(result["params"]["max_weight_delta"], 0.06)

    def test_evaluate_requires_a_one_hour_shadow_window(self):
        conn = _db()
        self._seed_champion(conn)
        SCM.open_challenger(conn, "sector_rotation", PARAMS, now=NOW)
        result = SCM.evaluate_challenger(conn, "sector_rotation", now=NOW)
        self.assertFalse(result["evaluated"])
        self.assertIn("1 小时", result["reason"])

    def test_failing_challenger_is_auto_rolled_back(self):
        conn = _db()
        self._seed_champion(conn)
        # 影子窗（最近 5 天）亏损，对照窗（前 5 天）盈利 → 收益恶化。
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','buy','600000',500,10000,'filled',0,?)""",
            ((NOW - dt.timedelta(days=9)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','sell','600000',500,11000,'filled',1000,?)""",
            ((NOW - dt.timedelta(days=7)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','buy','600000',500,10000,'filled',0,?)""",
            ((NOW - dt.timedelta(days=2)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','sell','600000',500,9000,'filled',-1000,?)""",
            ((NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"),),
        )
        conn.commit()
        SCM.open_challenger(conn, "sector_rotation", PARAMS, now=NOW - dt.timedelta(days=5))
        result = SCM.evaluate_challenger(conn, "sector_rotation", now=NOW)
        self.assertTrue(result["evaluated"])
        self.assertEqual("rolled_back", result["status"])
        row = conn.execute(
            "SELECT status FROM strategy_champion_versions WHERE role='challenger'"
        ).fetchone()
        self.assertEqual("rolled_back", row["status"])

    def test_promotion_requires_the_gate_and_creates_a_new_champion(self):
        conn = _db()
        self._seed_champion(conn)
        # 影子窗收益 +20%，对照窗 +10%，风险不恶化。
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','buy','600000',500,10000,'filled',0,?)""",
            ((NOW - dt.timedelta(days=9)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','sell','600000',500,11000,'filled',1000,?)""",
            ((NOW - dt.timedelta(days=7)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','buy','600000',500,10000,'filled',0,?)""",
            ((NOW - dt.timedelta(days=2)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO paper_orders(account_id,side,code,qty,amount,status,
                   realized_pnl,executed_at)
               VALUES('sector_rotation','sell','600000',500,12000,'filled',2000,?)""",
            ((NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"),),
        )
        conn.commit()
        SCM.open_challenger(conn, "sector_rotation", PARAMS, now=NOW - dt.timedelta(days=5))
        result = SCM.promote_challenger(conn, "sector_rotation", now=NOW)
        self.assertTrue(result["promoted"])
        champion = conn.execute(
            """SELECT status FROM strategy_champion_versions
                WHERE role='champion' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual("promoted", champion["status"])
        # 再晋升应因无 shadow Challenger 而失败。
        again = SCM.promote_challenger(conn, "sector_rotation", now=NOW)
        self.assertFalse(again["promoted"])

    def test_manual_rollback_discards_the_challenger(self):
        conn = _db()
        self._seed_champion(conn)
        SCM.open_challenger(conn, "sector_rotation", PARAMS, now=NOW - dt.timedelta(days=5))
        result = SCM.rollback_challenger(conn, "sector_rotation", reason="证据不足")
        self.assertTrue(result["rolled_back"])
        row = conn.execute(
            "SELECT status FROM strategy_champion_versions WHERE role='challenger'"
        ).fetchone()
        self.assertEqual("rolled_back", row["status"])
        # champion 保持不变
        champion = conn.execute(
            "SELECT status FROM strategy_champion_versions WHERE role='champion'"
        ).fetchone()
        self.assertEqual("promoted", champion["status"])


class WiringGuardTests(unittest.TestCase):
    @staticmethod
    def _source(name):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_schema_is_created_on_every_init_path(self):
        body = self._source("paper_trading.py")
        self.assertGreaterEqual(body.count("SCM.ensure_schema(conn)"), 2)

    def test_api_surface_exists(self):
        body = self._source("api_paper.py")
        for route in ("strategy-champions", "strategy-champion/promote",
                      "strategy-champion/rollback"):
            self.assertIn(route, body)


if __name__ == "__main__":
    unittest.main()
