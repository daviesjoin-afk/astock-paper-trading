# -*- coding: utf-8 -*-
"""PR-33：非对称风险门必须串在**每一条**风险变更路径上。

覆盖四条公开路径：

1. **manual API**：``PATCH /api/strategies/{id}``（PR-45 起 Strategy Admin：
   api_strategies.update_strategy）；
2. **AI / 自进化**：``self_evolution.adjust_strategy_dsl_parameters``；
3. **Champion promotion**：``strategy_champion.promote_challenger``；
4. **UI**：``runtime_settings.update``（见 test_asymmetric_risk.py）。

验收：除直接改 SQL 外，公开应用层没有任何绕过路径。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asymmetric_risk as AR
import self_evolution as SE
import strategy_champion as SCM
import strategy_registry as registry
import strategy_runtime as runtime

EVIDENCE = 50
STRATEGY = "trend_pullback"


def _parameter(parameter_id, parameter_type, value, minimum, maximum, max_step,
               *, locked=False, risk_direction="neutral", min_evidence=10):
    return {
        "op": "parameter", "parameter_id": parameter_id, "type": parameter_type,
        "value": value, "min": minimum, "max": maximum, "max_step": max_step,
        "locked": locked, "risk_direction": risk_direction,
        "min_evidence": min_evidence,
    }


def _rule(risk_per_trade=0.01):
    """一条带声明式风险参数的可执行 DSL。"""
    return {
        "op": "strategy",
        "rule": {
            "op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "indicator", "name": "ma",
                      "window": _parameter("ma_period", "integer", 20, 5, 60, 1)},
        },
        "parameters": [
            _parameter("risk_per_trade", "number", risk_per_trade, 0.002, 0.02, 0.002,
                       risk_direction="higher_is_riskier"),
            _parameter("holding_days", "integer", 5, 1, 20, 1,
                       risk_direction="higher_is_riskier"),
        ],
    }


class ManualApiPathTests(unittest.TestCase):
    """路径 1：人工 API（策略定义 PATCH）不能放大风险。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        registry.ensure_schema(self.conn)
        created = registry.create_user_definition(
            self.conn, "gate_api", "Gate API", dsl_ast=_rule(), actor="test")
        registry.transition(self.conn, created.id, "validated", actor="test")
        registry.transition(self.conn, created.id, "active", actor="test")
        self.conn.commit()
        self.conn.close()
        runtime.clear_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def _patch(self, changes, **extra):
        import api_strategies as API

        original_path = API.P.DB_PATH
        API.P.DB_PATH = self.path
        try:
            payload = {"changes": changes, "actor": "human-ui", **extra}
            return API.update_strategy("gate_api", payload)
        finally:
            API.P.DB_PATH = original_path

    def test_manual_api_cannot_expand_risk(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._patch({"dsl_ast": _rule(risk_per_trade=0.02)})
        self.assertEqual(422, ctx.exception.status_code)
        self.assertIn("风险放大", str(ctx.exception.detail))
        # 未落库：版本仍为 1。
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(1, registry.get_version("gate_api", conn=conn).version)
        finally:
            conn.close()

    def test_manual_api_can_tighten_immediately(self):
        result = self._patch({"dsl_ast": _rule(risk_per_trade=0.008)})
        self.assertEqual(2, result["version"]["version"])

    def test_every_definition_write_passes_the_gate(self):
        """save_definition 是唯一落库口，dsl_ast 变更必然触发闸门。"""
        calls = []
        original = AR.guard_definition_change

        def spy(conn, strategy_id, old_ast, new_ast, **kwargs):
            calls.append((strategy_id, kwargs.get("challenger_win")))
            return original(conn, strategy_id, old_ast, new_ast, **kwargs)

        AR.guard_definition_change = spy
        try:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                registry.save_definition(conn, "gate_api",
                                         {"dsl_ast": _rule(risk_per_trade=0.008)},
                                         actor="human-ui")
            finally:
                conn.close()
        finally:
            AR.guard_definition_change = original
        self.assertEqual([("gate_api", False)], calls)


class ChampionPromotionPathTests(unittest.TestCase):
    """路径 3：晋升必须申报 Challenger 胜出，且不能绕开闸门。"""

    def setUp(self):
        self.paper = sqlite3.connect(":memory:")
        self.paper.row_factory = sqlite3.Row
        self.paper.executescript(
            """
            CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cash REAL);
            CREATE TABLE paper_orders(id INTEGER PRIMARY KEY, account_id TEXT, side TEXT,
                                      status TEXT, amount REAL, realized_pnl REAL,
                                      created_at TEXT, executed_at TEXT, code TEXT);
            CREATE TABLE paper_positions(account_id TEXT, code TEXT, qty INTEGER);
            CREATE TABLE paper_capital_reservations(id INTEGER PRIMARY KEY, amount REAL);
            """
        )
        self.paper.execute("INSERT INTO paper_accounts VALUES(?,?)", (STRATEGY, 100000.0))
        self.paper.execute("INSERT INTO paper_positions VALUES(?,?,?)", (STRATEGY, "600000", 100))
        self.paper.execute("INSERT INTO paper_capital_reservations VALUES(1, 5000.0)")
        SCM.ensure_schema(self.paper)
        self.evo = sqlite3.connect(":memory:")
        self.evo.row_factory = sqlite3.Row
        SE.ensure_schema(self.evo)
        SE.init_params(self.evo)

    def tearDown(self):
        self.paper.close()
        self.evo.close()

    def _output(self, *, pnl: float, nav: float, code="600000"):
        return {
            "signals": [{"signal_key": "same-signal", "code": code, "side": "buy"}],
            "orders": [{"signal_key": "same-signal", "code": code, "side": "buy", "qty": 100,
                        "planned_price": 10.0, "amount": 10000.0, "status": "filled"}],
            "fills": [
                {"code": code, "side": "buy", "qty": 100, "price": 10.0, "amount": 10000.0},
                {"code": code, "side": "sell", "qty": 100, "price": 10.0, "amount": 10000.0,
                 "realized_pnl": pnl},
            ],
            "nav": {"nav_date": dt.datetime.now().date().isoformat(), "cash": nav,
                    "market_value": 0, "nav": nav},
        }

    def _record_counterfactual(self, *, challenger_pnl=2000.0, at=None):
        return SCM.run_shadow_counterfactual(
            self.paper, STRATEGY, {"asof": "2026-09-09", "600000": {"close": 10.0}},
            self._output(pnl=1000.0, nav=101000.0),
            self._output(pnl=challenger_pnl, nav=100000.0 + challenger_pnl),
            observed_at=at or dt.datetime.now().replace(microsecond=0) - dt.timedelta(days=1),
        )

    def test_promotion_declares_challenger_win(self):
        """晋升路径显式声明 challenger_win=True，仍受证据/观察期/步长约束。"""
        captured: dict = {}
        now = dt.datetime.now().replace(microsecond=0)
        SCM.open_challenger(self.paper, self.evo, STRATEGY, {"max_weight_delta": 0.032},
                            evidence_count=EVIDENCE, now=now - dt.timedelta(days=5))
        self._record_counterfactual()
        original = SE.adjust_strategy_params

        def spy(conn, strategy_id, adjustments, **kwargs):
            captured.update(kwargs)
            return original(conn, strategy_id, adjustments, **kwargs)

        SE.adjust_strategy_params = spy
        try:
            result = SCM.promote_challenger(self.paper, self.evo, STRATEGY)
        finally:
            SE.adjust_strategy_params = original
        self.assertTrue(result.get("promoted"), result)
        self.assertTrue(captured.get("challenger_win"))
        self.assertEqual(EVIDENCE, captured.get("evidence_count"))

    def test_risk_direction_expansion_still_needs_observation(self):
        """即使申报了 Challenger 胜出，风险方向放大仍要满足观察期。"""
        key = "max_weight_delta"
        original = dict(AR.RISK_DIRECTION_BY_KEY)
        AR.RISK_DIRECTION_BY_KEY[key] = "higher_is_riskier"
        AR.MAX_SINGLE_ROUND_STEP[key] = 0.01
        now = dt.datetime.now().replace(microsecond=0)
        try:
            SCM.open_challenger(self.paper, self.evo, STRATEGY, {key: 0.032},
                                evidence_count=EVIDENCE, now=now - dt.timedelta(days=5))
            self._record_counterfactual()
            result = SCM.promote_challenger(self.paper, self.evo, STRATEGY)
            self.assertFalse(result.get("promoted"))
            self.assertIn("观察", str(result.get("reason")))
        finally:
            AR.RISK_DIRECTION_BY_KEY.clear()
            AR.RISK_DIRECTION_BY_KEY.update(original)
            AR.MAX_SINGLE_ROUND_STEP.pop(key, None)


class NoBypassInventoryTests(unittest.TestCase):
    """元数据级不变式：方向表与步长上限必须一一对应。"""

    def test_every_declared_direction_has_a_step_cap(self):
        self.assertEqual(set(AR.RISK_DIRECTION_BY_KEY), set(AR.MAX_SINGLE_ROUND_STEP))

    def test_overrides_are_part_of_the_unified_model(self):
        self.assertTrue(set(AR.RISK_DIRECTION_KEYS) <= set(AR.RISK_DIRECTION_BY_KEY))

    def test_dsl_parameter_ids_with_risk_direction_are_covered(self):
        import strategy_dsl_schema as DSL

        # 真正带风险方向的 DSL 参数必须能在方向表里找到（否则会退化为保守默认）。
        self.assertTrue({"risk_per_trade", "holding_days", "atr_stop_multiplier",
                         "entry_slices"} <= set(AR.RISK_DIRECTION_BY_KEY))
        self.assertEqual({"higher_is_riskier", "lower_is_riskier", "neutral"},
                         set(DSL.RISK_DIRECTIONS))


if __name__ == "__main__":
    unittest.main()
