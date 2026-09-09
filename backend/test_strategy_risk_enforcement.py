# -*- coding: utf-8 -*-
"""PR-30 回归：编译策略风险画像（PR-03/04）接入生产风控。

验收口径
--------
1. 同样资金下，breakout / trend / mean-reversion 三种 DSL 编译出的
   stop / sizing / execution **各不相同**；
2. 三者都受同一 System Risk 上限（单笔止损预算界、全局敞口帽、系统
   drawdown 政策不被策略画像触碰）；
3. Composite / 低置信度 / 解析失败一律回落最保守模板（fail-closed）；
4. 画像只能收紧生产现值，永不放宽；System hard rules（T+1、证券范围、
   stale quote、全局 pool exposure、系统 drawdown）不在策略层出现。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("requests", mock.MagicMock())

import execution_profiles as EPF  # noqa: E402
import paper_trading as PT  # noqa: E402
import strategy_registry as registry  # noqa: E402
import strategy_risk_enforcement as SRE  # noqa: E402
import strategy_runtime as runtime  # noqa: E402
from strategy_risk_profiles import _SYSTEM_HARD_RULE_KEYS  # noqa: E402


def _dsl(archetype: str):
    if archetype == "breakout":
        return {"all": [{"op": "breakout"}, {"field": "realtime_quote"}]}
    if archetype == "trend":
        return {"all": [
            {"op": "gt", "left": {"field": "close"},
             "right": {"indicator": "ma", "window": 20}},
            {"op": "pullback"},
        ]}
    if archetype == "mean_reversion":
        return {"all": [{"op": "oversold_rsi"}, {"op": "reversion_confirm"}]}
    raise ValueError(archetype)


# DSL 是严格 schema 校验的（只允许 field/indicator/比较/布尔），archetype
# 证据通过 metadata 声明进入指纹（compile_strategy_risk_fingerprint 的
# 第二输入）。
_METADATA = {
    # 指纹按词计分（DSL + metadata 词法），证据需唯一占优才判 archetype；
    # "ma" 之类指标名会给 trend 计分，因此 breakout 的证据要压过它。
    "breakout": {"style": "breakout", "pattern": "breakout_surge", "entry": "realtime_quote"},
    "trend": {"style": "trend_pullback", "hold": 10},
    "mean_reversion": {"style": "mean_reversion"},
}

_VALID_DSL = {"op": "gt", "left": {"op": "field", "name": "close"},
              "right": {"op": "indicator", "name": "ma", "window": 20}}


class _RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "enforce.sqlite3"))
        self.conn.row_factory = sqlite3.Row
        for archetype in ("breakout", "trend", "mean_reversion"):
            strategy = registry.create_user_definition(
                self.conn, f"acc_{archetype}", f"{archetype} strategy",
                dsl_ast=_VALID_DSL, metadata=_METADATA[archetype], actor="test",
            )
            registry.transition(self.conn, strategy.id, "validated", actor="test")
            registry.transition(self.conn, strategy.id, "active", actor="test")
        runtime.clear_cache()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        runtime.clear_cache()


class CompiledProfileTests(_RegistryCase):
    def test_three_archetypes_compile_to_distinct_templates(self):
        for archetype, template in (
            ("breakout", "Momentum"), ("trend", "Trend"), ("mean_reversion", "MeanReversion"),
        ):
            with self.subTest(archetype=archetype):
                compiled = SRE.compiled_profile_for(self.conn, f"acc_{archetype}")
                self.assertEqual(template, compiled["template"])
                self.assertEqual(archetype, compiled["archetype"])

    def test_soft_risk_parameters_differ_across_archetypes(self):
        breakout = SRE.compiled_profile_for(self.conn, "acc_breakout")
        trend = SRE.compiled_profile_for(self.conn, "acc_trend")
        meanrev = SRE.compiled_profile_for(self.conn, "acc_mean_reversion")
        # sizing 帽各不相同
        self.assertEqual(len({breakout["max_weight"], trend["max_weight"], meanrev["max_weight"]}), 3)
        self.assertEqual(len({breakout["max_exposure"], trend["max_exposure"], meanrev["max_exposure"]}), 3)
        # stop 各不相同
        self.assertEqual(len({breakout["hard_stop"], trend["hard_stop"], meanrev["hard_stop"]}), 3)
        # 风险预算（risk_per_trade）不同（trend 与 breakout 同为 0.012，meanrev 更小）
        self.assertLess(meanrev["risk_per_trade"], breakout["risk_per_trade"])

    def test_execution_profiles_differ_across_archetypes(self):
        executions = {
            archetype: EPF.execution_profile_for(
                SRE.compiled_profile_for(self.conn, f"acc_{archetype}")["archetype"],
            )
            for archetype in ("breakout", "trend", "mean_reversion")
        }
        families = {key: value["family"] for key, value in executions.items()}
        self.assertEqual(len(set(families.values())), 3)
        # execution 方式不同：市价 vs 小让价限价 vs 零让价被动限价
        self.assertEqual(executions["breakout"]["order_type"], "market")
        self.assertEqual(executions["trend"]["order_type"], "limit")
        self.assertEqual(executions["trend"]["limit_offset_pct"], 0.2)
        self.assertEqual(executions["mean_reversion"]["limit_offset_pct"], 0.0)
        self.assertNotEqual(executions["trend"]["ttl_minutes"], executions["mean_reversion"]["ttl_minutes"])

    def test_compiled_profile_carries_pyramiding_and_holding(self):
        breakout = SRE.compiled_profile_for(self.conn, "acc_breakout")
        trend = SRE.compiled_profile_for(self.conn, "acc_trend")
        self.assertEqual(0, breakout["max_pyramiding"])
        self.assertEqual(2, trend["max_pyramiding"])
        self.assertEqual(10, trend["holding_days"])


class AcceptanceTests(_RegistryCase):
    """验收：不同 DSL → 不同 stop/sizing/execution，且同受 System 上限。"""

    def test_effective_risk_differs_but_stays_within_system_caps(self):
        merged = {}
        for archetype, profile_key in (
            ("breakout", "breakout"), ("trend", "trend"), ("mean_reversion", "trend"),
        ):
            account = {"id": f"acc_{archetype}", "risk_profile": profile_key}
            profile = PT._risk_profile(account, conn=self.conn)
            merged[archetype] = profile
        breakout, trend, meanrev = merged["breakout"], merged["trend"], merged["mean_reversion"]
        # sizing 帽不同（画像收紧后）
        self.assertEqual(len({breakout["max_weight"], trend["max_weight"], meanrev["max_weight"]}), 3)
        # stop 不同（编译画像的 hard_stop 落到生效参数层）
        self.assertEqual(len({
            breakout["compiled_risk_profile"]["template"],
            trend["compiled_risk_profile"]["template"],
            meanrev["compiled_risk_profile"]["template"],
        }), 3)
        # 都受同一 System Risk 上限：单笔风险预算不越过自适应界、敞口不越过
        # 全局策略敞口界、系统 drawdown 政策不被画像改写。
        for archetype, profile in merged.items():
            with self.subTest(archetype=archetype):
                self.assertLessEqual(profile["single_risk"], PT.ADAPTIVE_RISK_BOUNDS["single_risk"][1])
                self.assertLessEqual(profile["max_exposure"], PT.ADAPTIVE_RISK_BOUNDS["max_exposure"][1])
                self.assertEqual(profile["drawdown"], PT.RISK_PROFILES[profile_key_of(archetype)]["drawdown"])
                self.assertNotIn("t_plus_one", profile)
                self.assertFalse(set(profile) & _SYSTEM_HARD_RULE_KEYS)


def profile_key_of(archetype):
    return {"breakout": "breakout", "trend": "trend", "mean_reversion": "trend"}[archetype]


class FailClosedTests(unittest.TestCase):
    def test_unknown_strategy_falls_back_to_conservative_composite(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        compiled = SRE.compiled_profile_for(conn, "acc_unknown")
        self.assertEqual("Composite", compiled["template"])
        self.assertEqual("composite", compiled["archetype"])
        self.assertEqual(0.22, compiled["max_weight"])
        self.assertEqual(0.65, compiled["max_exposure"])
        self.assertEqual(0.008, compiled["risk_per_trade"])

    def test_tighten_never_loosens_production_values(self):
        compiled = SRE.composite_compiled_profile()
        base = {"max_weight": 0.20, "max_exposure": 0.60, "single_risk": 0.005}
        merged, audit = SRE.tighten_caps(base, compiled)
        self.assertEqual(0.20, merged["max_weight"])  # 模板 0.30 不能放宽现值 0.20
        self.assertEqual(0.60, merged["max_exposure"])
        self.assertEqual(0.005, merged["single_risk"])
        # 帽类一个都没收紧；纪律类只允许"新增/收紧"，不允许放宽。
        self.assertEqual(
            {"max_weight", "max_exposure", "single_risk"} & set(audit["tightened"]),
            set(),
        )
        self.assertEqual(-0.04, merged["hard_stop"])  # Composite 止损作为纪律层补齐


class MergeRuleTests(unittest.TestCase):
    def test_caps_take_the_min_and_stops_take_the_tighter(self):
        compiled = {
            "template": "Trend", "archetype": "trend",
            "max_weight": 0.28, "max_exposure": 0.85, "max_industry": 0.45,
            "risk_per_trade": 0.010,
            "hard_stop": -0.04, "trail_after": 0.05, "trail_stop": 0.06,
            "holding_days": 10, "max_pyramiding": 2,
        }
        base = {
            "max_weight": 0.34, "max_exposure": 0.95, "max_industry": 0.45,
            "single_risk": 0.012, "hard_stop": -0.05,
            "trail_after": 0.06, "trail_stop": 0.07,
        }
        merged, audit = SRE.tighten_caps(base, compiled)
        self.assertEqual(0.28, merged["max_weight"])
        self.assertEqual(0.85, merged["max_exposure"])
        self.assertEqual(0.010, merged["single_risk"])
        self.assertEqual(0.45, merged["max_industry"])  # 相同 → 不收紧
        # 止损：max(-0.05, -0.04) = -0.04（亏损上限更小 = 更紧）
        self.assertEqual(-0.04, merged["hard_stop"])
        self.assertEqual(0.05, merged["trail_after"])
        self.assertEqual(0.06, merged["trail_stop"])
        self.assertIn("max_weight", audit["tightened"])
        self.assertEqual(0.34, audit["tightened"]["max_weight"]["before"])
        self.assertEqual(0.28, audit["tightened"]["max_weight"]["after"])

    def test_tighten_spec_merges_holding_positions_and_pyramiding(self):
        compiled = {
            "hard_stop": -0.04, "trail_after": 0.05, "trail_stop": 0.06,
            "holding_days": 10, "max_pyramiding": 2, "max_positions": 3,
        }
        spec = {"hard_stop": -0.05, "hold_min": 3, "hold_max": 12,
                "max_positions": 4, "trail_stop": 0.07}
        merged = SRE.tighten_spec(spec, compiled)
        self.assertEqual(-0.04, merged["hard_stop"])
        self.assertEqual(10, merged["hold_max"])  # min(12, 10)
        self.assertEqual(3, merged["hold_min"] + 0)  # hold_min 不受影响
        self.assertEqual(3, merged["max_positions"])  # min(4, 3)
        self.assertEqual(0.06, merged["trail_stop"])  # min(0.07, 0.06)
        self.assertEqual(2, merged["max_pyramiding"])

    def test_effective_spec_uses_composite_when_registry_unknown(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        merged = SRE.effective_spec(conn, "acc_unknown", {"hard_stop": -0.05, "hold_max": 8})
        self.assertEqual(-0.04, merged["hard_stop"])  # Composite -0.04 更紧
        self.assertEqual(5, merged["hold_max"])  # Composite holding_days=5


class HardRuleGuardTests(unittest.TestCase):
    def test_enforcement_payload_never_publishes_system_rule_keys(self):
        compiled = SRE.composite_compiled_profile()
        merged, audit = SRE.tighten_caps({"max_weight": 0.32, "single_risk": 0.012}, compiled)
        emitted = set(merged) | set(audit) | set(audit.get("tightened", {}))
        self.assertFalse(emitted & _SYSTEM_HARD_RULE_KEYS)
        spec = SRE.tighten_spec({"hard_stop": -0.05}, compiled)
        self.assertFalse(set(spec) & _SYSTEM_HARD_RULE_KEYS)


if __name__ == "__main__":
    unittest.main()
