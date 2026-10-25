# -*- coding: utf-8 -*-
"""权重决策参与者与有界投影的契约测试。"""
from __future__ import annotations

import unittest

import strategy_weight_scope as SCOPE

THREE = ["tq_breakout", "trend_pullback", "reported_profit_breakout"]


class ProjectWeightsTests(unittest.TestCase):
    def test_roster_budget_is_kept_verbatim_when_already_valid(self):
        projected = SCOPE.project_weights(
            {"tq_breakout": 0.20, "trend_pullback": 0.50, "reported_profit_breakout": 0.30},
            THREE, min_pct=5.0, max_pct=60.0)
        self.assertEqual(projected, {"tq_breakout": 20.0, "trend_pullback": 50.0,
                                     "reported_profit_breakout": 30.0})

    def test_overweighted_preference_is_capped_and_the_rest_redistributed(self):
        projected = SCOPE.project_weights(
            {"tq_breakout": 0.90, "trend_pullback": 0.05, "reported_profit_breakout": 0.05},
            THREE, min_pct=15.0, max_pct=60.0)
        self.assertEqual(projected["tq_breakout"], 60.0)
        self.assertAlmostEqual(sum(projected.values()), 100.0)
        for value in projected.values():
            self.assertGreaterEqual(value, 15.0)
            self.assertLessEqual(value, 60.0)

    def test_flat_preference_produces_exact_hundred_percent(self):
        projected = SCOPE.project_weights({}, THREE, min_pct=5.0, max_pct=60.0)
        self.assertAlmostEqual(sum(projected.values()), 100.0)
        self.assertEqual(len(projected), 3)

    def test_five_sleeves_floor_still_feasible_but_caps_fifty_percent(self):
        five = ["a", "b", "c", "d", "e"]
        projected = SCOPE.project_weights({"a": 1.0}, five, min_pct=15.0, max_pct=60.0)
        self.assertAlmostEqual(sum(projected.values()), 100.0)
        self.assertLessEqual(max(projected.values()), 60.0)
        for value in projected.values():
            self.assertGreaterEqual(value, 15.0)

    def test_infeasible_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            SCOPE.project_weights({}, ["only_one"], min_pct=5.0, max_pct=60.0)
        with self.assertRaises(ValueError):
            SCOPE.project_weights({}, THREE, min_pct=40.0, max_pct=60.0)
        with self.assertRaises(ValueError):
            SCOPE.project_weights({}, [], min_pct=5.0, max_pct=60.0)
        with self.assertRaises(ValueError):
            SCOPE.project_weights({}, ["a", "a"], min_pct=5.0, max_pct=60.0)

    def test_nan_and_negative_preferences_do_not_break_the_projection(self):
        projected = SCOPE.project_weights(
            {"tq_breakout": float("nan"), "trend_pullback": -3.0,
             "reported_profit_breakout": None}, THREE, min_pct=5.0, max_pct=60.0)
        self.assertAlmostEqual(sum(projected.values()), 100.0)


class ParticipantScopeTests(unittest.TestCase):
    def test_unreadable_ledger_returns_empty_not_all_labels(self):
        """没有账本时必须是"空"，不能退化成"全部注册策略"。"""
        self.assertEqual(SCOPE.participant_ids(None), ())
        self.assertEqual(SCOPE.participant_ids(object()), ())


if __name__ == "__main__":
    unittest.main()
