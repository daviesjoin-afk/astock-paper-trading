# -*- coding: utf-8 -*-
"""``paper_position_review`` 纯决策域模块的契约矩阵（R17）。

    PR-01  评分公式逐字等价（含 model/trend/flow/momentum/return/news_penalty）
    PR-02  新仓趋势中性化：hold_days < 1 → trend 50（raw trend 不参与）
    PR-03  grade 边界（建仓复核 / 核心 65 / 观察 50 / 减仓 40 / 淘汰）
    PR-04  行情未通过核验 → quote_pending
    PR-05  T+1 锁定 → t1_locked
    PR-06  每轮卖出上限 → queued
    PR-07  紧急择强换仓 → consolidation_exit（豁免最短观察期）
    PR-08  最短观察期 → new_position
    PR-09  trend_pullback 未确认 → watch（不得直接 exit）
    PR-10  绝对低分 → consolidation_exit
    PR-11  每日换仓配额 → queued
    PR-12  替补优势 / 满位升级 → consolidation_exit
    PR-13  弱但无优势候选 → watch
    PR-14  健康持仓 → hold
    PR-15  确定性：相同输入逐字相同输出

另含阈值边界（score == 38 / 38+ / 40 / 50 / 65）不得出现 `<` vs `<=` 漂移。
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_position_review as PReview  # noqa: E402

MODULE_PATH = os.path.join(BACKEND_DIR, "paper_position_review.py")

TQ_WEIGHTS = {"model": 0.35, "trend": 0.05, "flow": 0.25, "momentum": 0.25, "return": 0.10}
POLICY = PReview.ReviewPolicy()


def _score(**over):
    kwargs = dict(model_score=50.0, trend_score=50.0, flow_score=50.0,
                  momentum_score=50.0, return_score=50.0, news_penalty=0.0,
                  weights=TQ_WEIGHTS, hold_days=3)
    kwargs.update(over)
    return PReview.score_quality(**kwargs)


def _review(**over):
    base = {"account_id": "tq_breakout", "code": "600000", "score": 60.0,
            "hold_days": 5, "min_hold_days": 2, "small_position": False,
            "at_dynamic_limit": False, "rotations_today": 0,
            "replacement_edge": None, "replacement_score": None,
            "quality_exit_confirmed": False}
    base.update(over)
    return base


def _position(**over):
    base = {"account_id": "tq_breakout", "code": "600000", "available_qty": 100}
    base.update(over)
    return base


FRESH = {"fresh": True, "reason": "ok"}
STALE = {"fresh": False, "reason": "stale"}


class ScoringEquivalenceTests(unittest.TestCase):
    """PR-01 / PR-02 / PR-03 —— 评分算术与 grade 边界。"""

    def test_pr01_formula_is_verbatim(self):
        result = _score(model_score=100.0, trend_score=0.0, flow_score=100.0,
                        momentum_score=100.0, return_score=100.0, news_penalty=12.0)
        expected = (100.0 * 0.35 + 0.0 * 0.05 + 100.0 * 0.25
                    + 100.0 * 0.25 + 100.0 * 0.10 - 12.0)
        self.assertAlmostEqual(result["score"], round(expected, 2), places=2)
        self.assertEqual(result["trend_for_score"], 0.0)
        self.assertEqual(result["review_phase"], "持仓复核")

    def test_pr01b_score_is_clamped_to_0_100(self):
        self.assertEqual(_score(model_score=100.0, flow_score=100.0,
                                momentum_score=100.0, return_score=100.0,
                                trend_score=100.0)["score"], 100.0)
        self.assertEqual(_score(model_score=0.0, flow_score=0.0, momentum_score=0.0,
                                return_score=0.0, trend_score=0.0,
                                news_penalty=24.0)["score"], 0.0)

    def test_pr02_new_position_neutralises_trend(self):
        hot = _score(trend_score=100.0, hold_days=0)
        cold = _score(trend_score=0.0, hold_days=0)
        self.assertEqual(hot["trend_for_score"], 50.0)
        self.assertEqual(cold["trend_for_score"], 50.0)
        self.assertEqual(hot["score"], cold["score"], "新仓评分不得随 raw trend 漂移")
        self.assertEqual(hot["grade"], "建仓复核")
        self.assertEqual(hot["review_phase"], "建仓复核")

    def test_pr02b_hold_day_one_uses_raw_trend(self):
        result = _score(trend_score=100.0, hold_days=1)
        self.assertEqual(result["trend_for_score"], 100.0)
        self.assertEqual(result["review_phase"], "持仓复核")

    def test_pr03_grade_boundaries(self):
        cases = [
            (0, 100.0, "建仓复核"),
            (3, 65.0, "核心"),
            (3, 64.99, "观察"),
            (3, 50.0, "观察"),
            (3, 49.99, "减仓"),
            (3, 40.0, "减仓"),
            (3, 39.99, "淘汰"),
        ]
        for hold_days, target, expected in cases:
            with self.subTest(score=target, hold_days=hold_days):
                # 用 model 权重 1.0 的权重表把 score 精确设成 target
                result = PReview.score_quality(
                    model_score=target, trend_score=50.0, flow_score=0.0,
                    momentum_score=0.0, return_score=0.0, news_penalty=0.0,
                    weights={"model": 1.0, "trend": 0.0, "flow": 0.0,
                             "momentum": 0.0, "return": 0.0},
                    hold_days=hold_days,
                )
                self.assertAlmostEqual(result["score"], target, places=2)
                self.assertEqual(result["grade"], expected)


class ConcentrationDecisionTests(unittest.TestCase):
    """PR-04 … PR-14 —— 决策优先级逐项。"""

    def test_pr04_stale_quote_is_pending(self):
        action, _ = PReview.decide_action(_review(), _position(), STALE, 0, policy=POLICY)
        self.assertEqual(action, "quote_pending")

    def test_pr04b_missing_score_is_pending(self):
        action, _ = PReview.decide_action({"score": None}, _position(), FRESH, 0,
                                          policy=POLICY)
        self.assertEqual(action, "review_pending")

    def test_pr05_t1_lock_blocks_rotation(self):
        action, _ = PReview.decide_action(_review(score=10.0), _position(available_qty=0),
                                          FRESH, 0, policy=POLICY)
        self.assertEqual(action, "t1_locked")

    def test_pr06_per_run_sell_cap_queues(self):
        action, _ = PReview.decide_action(_review(score=10.0), _position(), FRESH,
                                          POLICY.max_sells_per_run, policy=POLICY)
        self.assertEqual(action, "queued")

    def test_pr07_urgent_slot_upgrade(self):
        review = _review(score=30.0, hold_days=0, min_hold_days=2,
                         replacement_score=80.0, replacement_edge=50.0)
        action, reason = PReview.decide_action(review, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(action, "consolidation_exit")
        self.assertIn("紧急择强换仓", reason)

    def test_pr08_min_observation_window(self):
        review = _review(score=60.0, hold_days=0, min_hold_days=2)
        action, reason = PReview.decide_action(review, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(action, "new_position")
        self.assertIn("观察期", reason)

    def test_pr09_trend_pullback_needs_confirmation(self):
        review = _review(score=30.0, quality_exit_confirmed=False)
        position = _position(account_id="trend_pullback")
        action, reason = PReview.decide_action(review, position, FRESH, 0, policy=POLICY)
        self.assertEqual(action, "watch", "trend_pullback 未确认时不得直接淘汰")
        self.assertIn("连续观察确认", reason)

    def test_pr09b_trend_pullback_confirmed_exits(self):
        review = _review(score=30.0, quality_exit_confirmed=True)
        position = _position(account_id="trend_pullback")
        action, _ = PReview.decide_action(review, position, FRESH, 0, policy=POLICY)
        self.assertEqual(action, "consolidation_exit")

    def test_pr10_absolute_low_score_exits(self):
        action, reason = PReview.decide_action(_review(score=20.0), _position(), FRESH, 0,
                                               policy=POLICY)
        self.assertEqual(action, "consolidation_exit")
        self.assertIn("低于淘汰线", reason)

    def test_pr11_rotation_daily_quota_queues(self):
        # score 必须**高于**淘汰线（否则先命中绝对淘汰分支），但仍满足 can_replace
        # （score <= any_replace_score=42）—— 这样才走到每日换仓配额判定。
        review = _review(score=40.0, rotations_today=POLICY.rotation_max_per_day,
                         replacement_score=90.0, replacement_edge=60.0)
        action, reason = PReview.decide_action(review, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(action, "queued")
        self.assertIn("上限", reason)

    def test_pr12_replacement_edge_exits(self):
        # 同上：score=40（>38 且 <=42）才是"弱到可换但未到绝对淘汰"的区间。
        review = _review(score=40.0, replacement_score=80.0, replacement_edge=35.0)
        action, reason = PReview.decide_action(review, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(action, "consolidation_exit")
        self.assertIn("换仓", reason)

    def test_pr12b_full_slot_upgrade_exits(self):
        review = _review(score=60.0, at_dynamic_limit=True,
                         replacement_score=80.0, replacement_edge=20.0)
        action, reason = PReview.decide_action(review, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(action, "consolidation_exit")
        self.assertIn("动态席位已满", reason)

    def test_pr12c_replacement_execution_buffer_applied(self):
        review = _review(score=45.0, replacement_score=80.0, replacement_edge=20.0)
        PReview.decide_action(review, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(review["replacement_execution_buffer"],
                         POLICY.replacement_execution_buffer)
        self.assertAlmostEqual(review["replacement_net_edge"],
                               20.0 - POLICY.replacement_execution_buffer, places=2)

    def test_pr13_weak_without_edge_is_watch(self):
        action, reason = PReview.decide_action(_review(score=45.0), _position(), FRESH, 0,
                                               policy=POLICY)
        self.assertEqual(action, "watch")
        self.assertIn("偏弱", reason)

    def test_pr14_healthy_position_holds(self):
        action, reason = PReview.decide_action(_review(score=70.0), _position(), FRESH, 0,
                                               policy=POLICY)
        self.assertEqual(action, "hold")
        self.assertIn("保留", reason)

    def test_pr15_deterministic_same_input_same_output(self):
        review_a = _review(score=45.0, replacement_score=80.0, replacement_edge=20.0)
        review_b = dict(review_a)
        first = PReview.decide_action(review_a, _position(), FRESH, 0, policy=POLICY)
        second = PReview.decide_action(review_b, _position(), FRESH, 0, policy=POLICY)
        self.assertEqual(first, second)
        self.assertEqual(review_a, review_b)
        self.assertEqual(_score(model_score=63.0), _score(model_score=63.0))


class ThresholdBoundaryTests(unittest.TestCase):
    """阈值边界不得出现 ``<`` vs ``<=`` 漂移。"""

    def test_exit_threshold_is_inclusive(self):
        at = _review(score=POLICY.exit_score)
        just_above = _review(score=POLICY.exit_score + 0.01)
        self.assertEqual(
            PReview.decide_action(at, _position(), FRESH, 0, policy=POLICY)[0],
            "consolidation_exit", "score == 38 必须淘汰（<= 语义）")
        self.assertEqual(
            PReview.decide_action(just_above, _position(), FRESH, 0, policy=POLICY)[0],
            "watch", "score 略高于 38 不得淘汰")

    def test_replace_score_boundary(self):
        below = _review(score=POLICY.replace_score - 0.01)
        at = _review(score=POLICY.replace_score)
        self.assertEqual(
            PReview.decide_action(below, _position(), FRESH, 0, policy=POLICY)[0], "watch")
        self.assertEqual(
            PReview.decide_action(at, _position(), FRESH, 0, policy=POLICY)[0], "hold")

    def test_urgent_upgrade_requires_score_at_or_below_exit(self):
        at = _review(score=POLICY.exit_score, replacement_score=90.0, replacement_edge=60.0)
        above = _review(score=POLICY.exit_score + 0.01, replacement_score=90.0,
                        replacement_edge=60.0)
        self.assertIn("紧急择强换仓",
                      PReview.decide_action(at, _position(), FRESH, 0, policy=POLICY)[1])
        self.assertNotIn("紧急择强换仓",
                         PReview.decide_action(above, _position(), FRESH, 0, policy=POLICY)[1])

    def test_small_position_replace_uses_strict_less_than(self):
        at = _review(score=POLICY.replace_score, small_position=True,
                     replacement_score=90.0, replacement_edge=40.0)
        below = _review(score=POLICY.replace_score - 0.01, small_position=True,
                        replacement_score=90.0, replacement_edge=40.0)
        self.assertEqual(
            PReview.decide_action(at, _position(), FRESH, 0, policy=POLICY)[0], "hold",
            "small_position 分支是 score < replace_score（严格小于）")
        self.assertEqual(
            PReview.decide_action(below, _position(), FRESH, 0, policy=POLICY)[0],
            "consolidation_exit")


class ModuleBoundaryTests(unittest.TestCase):
    """纯域模块的静态硬边界。"""

    def setUp(self):
        with open(MODULE_PATH, encoding="utf-8") as fh:
            self.source = fh.read()
        self.tree = ast.parse(self.source)

    def _import_roots(self):
        roots = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def _call_names(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_zero_project_imports(self):
        project = {name[:-3] for name in os.listdir(BACKEND_DIR) if name.endswith(".py")}
        leaked = sorted(self._import_roots() & (project - {"paper_position_review"}))
        self.assertEqual(leaked, [], f"纯域模块依赖了项目模块 {leaked}")
        self.assertEqual(sorted(self._import_roots() - {"__future__", "dataclasses", "typing"}),
                         [], "出现了未登记的 import 根")

    def test_zero_io_and_wall_clock(self):
        self.assertEqual(sorted(self._call_names() & {
            "open", "execute", "executemany", "connect", "commit", "rollback",
            "urlopen", "socket", "request", "getenv", "environ", "subprocess",
            "today", "now", "utcnow", "time", "monotonic",
        }), [])


if __name__ == "__main__":
    unittest.main()
