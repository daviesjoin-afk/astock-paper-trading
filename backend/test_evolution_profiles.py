# -*- coding: utf-8 -*-
"""策略级自进化画像（per-strategy evolution profile）回归测试。"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import evolution_profiles as EP
import self_evolution as SE


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SE.ensure_schema(conn)
    return conn


class ProfileTests(unittest.TestCase):
    def test_all_five_strategies_have_profiles(self):
        for strategy_id in ("tq_breakout", "trend_pullback", "sector_rotation",
                            "reported_profit_breakout", "main_force_top10"):
            profile = EP.evolution_profile_for(strategy_id)
            self.assertFalse(profile["fallback"], strategy_id)
            self.assertTrue(profile["tunable"])
            self.assertTrue(profile["min_samples"] >= SE.MIN_SAMPLES_FOR_EVOLUTION)

    def test_unknown_strategy_falls_back_to_conservative_default(self):
        profile = EP.evolution_profile_for("unknown_strategy")
        self.assertTrue(profile["fallback"])
        self.assertIn("confidence_threshold", profile["locked"])

    def test_locked_params_are_profile_specific(self):
        # 质量策略锁最多；做T锁最少但仍锁方向阈值。
        quality = EP.evolution_profile_for("reported_profit_breakout")
        self.assertIn("confidence_threshold", quality["locked"])
        self.assertIn("consensus_weight_ratio", quality["locked"])
        day_trading = EP.evolution_profile_for("tq_breakout")
        self.assertNotIn("confidence_threshold", day_trading["locked"])
        self.assertIn("consensus_direction_threshold", day_trading["locked"])

    def test_min_evidence_is_at_least_global_threshold(self):
        for strategy_id in ("tq_breakout", "trend_pullback", "sector_rotation",
                            "reported_profit_breakout", "main_force_top10"):
            self.assertGreaterEqual(
                EP.min_evidence_for(strategy_id), SE.MIN_SAMPLES_FOR_EVOLUTION,
                strategy_id,
            )


class ValidationTests(unittest.TestCase):
    def test_locked_param_is_rejected(self):
        result = EP.validate_strategy_adjustment(
            "reported_profit_breakout", {"confidence_threshold": 70},
            {"confidence_threshold": 75},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("锁定参数", result["violations"][0])

    def test_step_violation_is_rejected(self):
        result = EP.validate_strategy_adjustment(
            "tq_breakout", {"max_weight_delta": 0.03}, {"max_weight_delta": 0.06},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("步长", result["violations"][0])

    def test_within_step_and_bounds_is_allowed(self):
        result = EP.validate_strategy_adjustment(
            "tq_breakout", {"max_weight_delta": 0.03}, {"max_weight_delta": 0.033},
            evidence_count=20,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(0.033, result["adjusted"]["max_weight_delta"])

    def test_omitted_evidence_is_rejected(self):
        result = EP.validate_strategy_adjustment(
            "tq_breakout", {"max_weight_delta": 0.03}, {"max_weight_delta": 0.033},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("未提供证据", result["violations"][0])

    def test_monotonic_hold_bias_only_tightens(self):
        # trend_pullback 的 hold_bias 只许更保守（增大）；放松被拒。
        loosened = EP.validate_strategy_adjustment(
            "trend_pullback", {"hold_bias": 0.10}, {"hold_bias": 0.08},
            evidence_count=20,
        )
        tightened = EP.validate_strategy_adjustment(
            "trend_pullback", {"hold_bias": 0.10}, {"hold_bias": 0.12},
            evidence_count=20,
        )
        self.assertFalse(loosened["allowed"])
        self.assertIn("单向收紧", loosened["violations"][0])
        self.assertTrue(tightened["allowed"])

    def test_out_of_bounds_is_rejected_not_clamped(self):
        # 步长内但越过边界：trend_pullback hold_bias 边界 [0.05, 0.5]。
        result = EP.validate_strategy_adjustment(
            "trend_pullback", {"hold_bias": 0.49}, {"hold_bias": 0.505},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("策略边界", result["violations"][0])

    def test_min_evidence_gates_the_adjustment(self):
        allowed = EP.validate_strategy_adjustment(
            "main_force_top10", {"max_weight_delta": 0.03},
            {"max_weight_delta": 0.032}, evidence_count=12,
        )
        blocked = EP.validate_strategy_adjustment(
            "main_force_top10", {"max_weight_delta": 0.03},
            {"max_weight_delta": 0.032}, evidence_count=5,
        )
        self.assertTrue(allowed["allowed"])
        self.assertFalse(blocked["allowed"])
        self.assertIn("证据不足", blocked["violations"][0])

    def test_unknown_key_is_rejected(self):
        result = EP.validate_strategy_adjustment(
            "tq_breakout", {}, {"free_text": "x"})
        self.assertFalse(result["allowed"])


class StorageTests(unittest.TestCase):
    def test_strategy_params_inherit_global_active_when_no_pointer(self):
        """策略没有专属指针 = 继承全局 active（不是"没有参数"）。"""
        conn = _db()
        SE.init_params(conn)
        state = SE.get_strategy_params(conn, "tq_breakout")
        self.assertEqual("__global__", state["inherited_from"])
        self.assertEqual(SE.get_current_params(conn)["id"], state["id"])
        # 画像边界生效：全局默认 0.03 在做T边界内保持不变。
        self.assertEqual(0.03, state["params"]["max_weight_delta"])

    def test_adjust_only_creates_candidate_until_activated(self):
        """调整先只落地候选；未经显式激活，runtime 参数不变。"""
        conn = _db()
        SE.init_params(conn)
        result = SE.adjust_strategy_params(
            conn, "tq_breakout", {"max_weight_delta": 0.033},
            evidence_count=20,
        )
        self.assertTrue(result["adjusted"])
        self.assertFalse(result["activated"])
        # 关键：尚未激活 → 策略仍在继承全局 0.03。
        state = SE.get_strategy_params(conn, "tq_breakout")
        self.assertEqual(0.03, state["params"]["max_weight_delta"])
        # 显式激活后才生效。
        SE.activate_params_candidate(conn, result["new_params_id"], actor="test")
        state = SE.get_strategy_params(conn, "tq_breakout")
        self.assertEqual(0.033, state["params"]["max_weight_delta"])
        # 全局参数不受影响。
        self.assertEqual(0.03, SE.get_current_params(conn)["params"]["max_weight_delta"])

    def test_rejected_adjustment_is_audited_not_applied(self):
        conn = _db()
        SE.init_params(conn)
        result = SE.adjust_strategy_params(
            conn, "reported_profit_breakout",
            {"confidence_threshold": 90}, evidence_count=50,
        )
        self.assertFalse(result["adjusted"])
        self.assertEqual(70, SE.get_current_params(conn)["params"]["confidence_threshold"])
        log = SE.get_evolution_log(conn, 5)
        self.assertTrue(any(item["event_type"] == "strategy_adjust_rejected"
                            for item in log))

    def test_rollback_restores_previous_activated_version(self):
        """回滚目标来自激活历史：回到上一次**生效**的版本。"""
        conn = _db()
        SE.init_params(conn)
        first = SE.adjust_strategy_params(conn, "trend_pullback",
                                          {"max_weight_delta": 0.032},
                                          evidence_count=20)
        SE.activate_params_candidate(conn, first["new_params_id"], actor="test")
        second = SE.adjust_strategy_params(conn, "trend_pullback",
                                           {"max_weight_delta": 0.034},
                                           evidence_count=20)
        SE.activate_params_candidate(conn, second["new_params_id"], actor="test")
        # 再插一条从未激活、id 最大的候选：回滚绝不能落到它身上。
        SE.adjust_strategy_params(conn, "trend_pullback",
                                  {"max_weight_delta": 0.036}, evidence_count=20)
        rollback = SE.rollback_strategy_params(conn, "trend_pullback")
        self.assertTrue(rollback["rolled_back"])
        state = SE.get_strategy_params(conn, "trend_pullback")
        self.assertEqual(0.032, state["params"]["max_weight_delta"])

    def test_noop_adjustment_creates_no_version(self):
        conn = _db()
        SE.init_params(conn)
        first = SE.adjust_strategy_params(conn, "trend_pullback",
                                          {"max_weight_delta": 0.032},
                                          evidence_count=20)
        SE.activate_params_candidate(conn, first["new_params_id"], actor="test")
        # 与生效值同值 → 无变化，不落新版本。
        result = SE.adjust_strategy_params(conn, "trend_pullback",
                                           {"max_weight_delta": 0.032},
                                           evidence_count=20)
        self.assertFalse(result["adjusted"])
        self.assertEqual("无变化", result["reason"])
        # 已有等价的待激活候选 → 不重复落版本。
        pending = SE.adjust_strategy_params(conn, "trend_pullback",
                                            {"max_weight_delta": 0.034},
                                            evidence_count=20)
        self.assertTrue(pending["adjusted"])
        duplicate = SE.adjust_strategy_params(conn, "trend_pullback",
                                              {"max_weight_delta": 0.034},
                                              evidence_count=20)
        self.assertFalse(duplicate["adjusted"])
        self.assertEqual(pending["new_params_id"], duplicate["existing_params_id"])
        # 激活后回滚 → 回到上一真实版本（不是第二新行）。
        SE.activate_params_candidate(conn, pending["new_params_id"], actor="test")
        SE.rollback_strategy_params(conn, "trend_pullback")
        state = SE.get_strategy_params(conn, "trend_pullback")
        self.assertEqual(0.032, state["params"]["max_weight_delta"])

    def test_rollback_single_version_restores_inherited_global_baseline(self):
        conn = _db()
        SE.init_params(conn)
        manual = SE.manual_adjust(conn, {"confidence_threshold": 80},
                                  reason="全局先调整")
        SE.activate_params_candidate(conn, manual["new_params_id"], actor="test")
        scoped = SE.adjust_strategy_params(conn, "tq_breakout",
                                           {"max_weight_delta": 0.033},
                                           evidence_count=20)
        SE.activate_params_candidate(conn, scoped["new_params_id"], actor="test")
        SE.rollback_strategy_params(conn, "tq_breakout")
        state = SE.get_strategy_params(conn, "tq_breakout")
        # 回落的是继承时的全局基线（confidence=80），不是出厂默认 70。
        self.assertEqual(80, state["params"]["confidence_threshold"])
        self.assertEqual(0.03, state["params"]["max_weight_delta"])

    def test_global_evolution_path_is_unchanged(self):
        conn = _db()
        SE.init_params(conn)
        result = SE.manual_adjust(conn, {"confidence_threshold": 72}, reason="test")
        self.assertTrue(result["adjusted"])
        # 人工调整同样只生成候选，必须显式激活。
        self.assertFalse(result["activated"])
        self.assertEqual(70, SE.get_current_params(conn)["params"]["confidence_threshold"])
        SE.activate_params_candidate(conn, result["new_params_id"], actor="test")
        self.assertEqual(72, SE.get_current_params(conn)["params"]["confidence_threshold"])
        # 策略画像的锁定参数约束只作用于策略级路径。
        rejected = SE.adjust_strategy_params(
            conn, "reported_profit_breakout", {"confidence_threshold": 90},
            evidence_count=50,
        )
        self.assertFalse(rejected["adjusted"])


if __name__ == "__main__":
    unittest.main()
