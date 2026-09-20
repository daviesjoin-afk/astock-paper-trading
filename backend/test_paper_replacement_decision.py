# -*- coding: utf-8 -*-
"""R18：纯 replacement 决策域契约（``paper_replacement_decision``）。

覆盖规格 §57-§58：

    RD-1  score formula equivalence
    RD-2  score normalization 0..1
    RD-3  score normalization 0..100
    RD-4  choose strongest allowed candidate
    RD-5  held code excluded
    RD-6  T+1 locked
    RD-7  min-hold observation
    RD-8  urgent upgrade
    RD-9  regular upgrade
    RD-10 full-slot upgrade
    RD-11 edge insufficient
    RD-12 pool donor borrow
    RD-13 strategy donor borrow
    RD-14 no donor => no borrow
    RD-15 deterministic same-input same-output

外加规格 §58 的阈值边界（``>=`` / ``>``、``<`` / ``<=`` 漂移）与模块硬边界。
"""
from __future__ import annotations

import ast
import json
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_replacement_decision as PRep  # noqa: E402

POLICY = PRep.ReplacementPolicy()


def signal(*, entry=None, t_score=None, rank_score=None):
    payload = {}
    if entry is not None:
        payload = {"decision": {"entry_model": {"score": entry}}}
    return {"payload": json.dumps(payload), "t_score": t_score, "rank_score": rank_score}


def weakest(**over):
    base = {"code": "600001", "name": "测试股", "score": 40.0, "hold_days": 10,
            "available_qty": 100, "review_action": "hold"}
    base.update(over)
    return base


def urgent_weak_score(candidate_score):
    """挑一个同时满足 urgent 三个条件的 weakest 分。

    urgent 要求 ``candidate >= upgrade_min_candidate``、``edge >= upgrade_min_edge``
    且 ``weakest.score <= exit_score``；因此 weakest 必须同时低于
    ``candidate - upgrade_min_edge`` 与 ``exit_score``。
    """
    return min(POLICY.exit_score, candidate_score - POLICY.upgrade_min_edge)


def replaceable_score(candidate_score):
    """挑一个满足 regular upgrade 的 weakest 分（net edge 恰好达标）。"""
    return candidate_score - POLICY.replace_edge - POLICY.execution_buffer


class ScoringEquivalenceTests(unittest.TestCase):
    """RD-1 … RD-3 —— 复合公式逐字等价 + 两种量纲归一化。"""

    def test_rd1_formula_is_entry_45_t_35_rank_20(self):
        self.assertEqual(PRep.score_candidate(signal(entry=100, t_score=100, rank_score=100)),
                         100.0)
        self.assertEqual(PRep.score_candidate(signal(entry=0, t_score=0, rank_score=0)), 0.0)
        # 80*0.45 + 60*0.35 + 40*0.20 = 36 + 21 + 8 = 65
        self.assertEqual(PRep.score_candidate(signal(entry=80, t_score=60, rank_score=40)),
                         65.0)

    def test_rd1b_missing_fields_use_zero_not_default_50(self):
        """缺字段按 0 处理（旧实现的默认参数是 0.0，不是 _score100 的 50）。"""
        self.assertEqual(PRep.score_candidate(signal()), 0.0)
        self.assertEqual(PRep.score_candidate({"payload": "{}", "t_score": None,
                                               "rank_score": None}), 0.0)

    def test_rd2_zero_to_one_normalization(self):
        # 0.8 -> 80, 0.6 -> 60, 0.4 -> 40 ⇒ 与 0..100 写法同分
        self.assertEqual(PRep.score_candidate(signal(entry=0.8, t_score=0.6, rank_score=0.4)),
                         65.0)

    def test_rd3_zero_to_hundred_normalization(self):
        self.assertEqual(PRep.score_candidate(signal(entry=80, t_score=60, rank_score=40)),
                         PRep.score_candidate(signal(entry=0.8, t_score=0.6, rank_score=0.4)))

    def test_rd3b_values_are_clamped_and_rounded(self):
        self.assertEqual(PRep.score_candidate(signal(entry=500, t_score=500, rank_score=500)),
                         100.0)
        self.assertEqual(PRep.score_candidate(signal(entry=-50, t_score=-50, rank_score=-50)),
                         0.0)
        value = PRep.score_candidate(signal(entry=33.333, t_score=66.667, rank_score=11.111))
        self.assertEqual(value, round(value, 2))

    def test_rd3c_malformed_payload_is_treated_as_no_entry_score(self):
        self.assertEqual(PRep.score_candidate({"payload": "not json", "t_score": 100,
                                               "rank_score": 100}), 55.0)
        self.assertEqual(PRep.score_candidate(None), 0.0)


class CandidateRankingTests(unittest.TestCase):
    """RD-4 / RD-5 —— 选最强、排除已持有。"""

    def candidates(self):
        return [
            {"id": 1, "code": "600001", "name": "A", "status": "pending",
             "intended_date": "2026-09-10", **signal(entry=10, t_score=10, rank_score=10)},
            {"id": 2, "code": "600002", "name": "B", "status": "pending",
             "intended_date": "2026-09-10", **signal(entry=90, t_score=90, rank_score=90)},
            {"id": 3, "code": "600003", "name": "C", "status": "deferred_capacity",
             "intended_date": "2026-09-10", **signal(entry=50, t_score=50, rank_score=50)},
        ]

    def test_rd4_chooses_the_strongest(self):
        best = PRep.choose_best_candidate(self.candidates(), held_codes=set())
        self.assertEqual(best["signal_id"], 2)
        self.assertEqual(best["code"], "600002")
        self.assertEqual(best["score"], 90.0)
        self.assertEqual(best["intended_date"], "2026-09-10")

    def test_rd5_held_code_is_excluded(self):
        best = PRep.choose_best_candidate(self.candidates(), held_codes={"600002"})
        self.assertEqual(best["signal_id"], 3)

    def test_rd5b_all_held_yields_none(self):
        best = PRep.choose_best_candidate(
            self.candidates(), held_codes={"600001", "600002", "600003"})
        self.assertIsNone(best)

    def test_rd5c_empty_input_yields_none(self):
        self.assertIsNone(PRep.choose_best_candidate([], held_codes=set()))
        self.assertIsNone(PRep.choose_best_candidate(None, held_codes=set()))

    def test_rd5d_ties_keep_the_first_candidate(self):
        rows = [
            {"id": 1, "code": "600001", "name": "A", "status": "pending",
             "intended_date": "2026-09-10", **signal(entry=80, t_score=80, rank_score=80)},
            {"id": 2, "code": "600002", "name": "B", "status": "pending",
             "intended_date": "2026-09-10", **signal(entry=80, t_score=80, rank_score=80)},
        ]
        self.assertEqual(PRep.choose_best_candidate(rows, held_codes=set())["signal_id"], 1)


class DonorTests(unittest.TestCase):
    """RD-12 / RD-13 / RD-14 —— donor 判定。"""

    def test_rd12_pool_donor_when_pool_has_free_seats(self):
        donors = PRep.derive_donors(
            limits={"tq_breakout": 3, "sector_rotation": 3},
            counts={"tq_breakout": 3, "sector_rotation": 3},
            account_id="tq_breakout", pool_limit=15, occupied_pool_count=6,
            policy=POLICY)
        self.assertEqual([d["account_id"] for d in donors], ["shared_pool"])
        self.assertEqual(donors[0]["unused_pool_slots"], 9)

    def test_rd13_strategy_donor_with_unused_slot_above_floor(self):
        donors = PRep.derive_donors(
            limits={"tq_breakout": 3, "sector_rotation": 5},
            counts={"tq_breakout": 3, "sector_rotation": 2},
            account_id="tq_breakout", pool_limit=15, occupied_pool_count=15,
            policy=POLICY)
        self.assertEqual([d["account_id"] for d in donors], ["sector_rotation"])
        self.assertEqual(donors[0]["remaining_after"], 4)

    def test_rd13b_donor_cannot_drop_below_the_floor(self):
        donors = PRep.derive_donors(
            limits={"tq_breakout": 3, "sector_rotation": 2},
            counts={"tq_breakout": 3, "sector_rotation": 2},
            account_id="tq_breakout", pool_limit=15, occupied_pool_count=15,
            policy=POLICY)
        self.assertEqual(donors, [], "出让策略跌破最小保留席位")

    def test_rd13c_self_is_never_a_donor(self):
        donors = PRep.derive_donors(
            limits={"tq_breakout": 5}, counts={"tq_breakout": 1},
            account_id="tq_breakout", pool_limit=15, occupied_pool_count=15,
            policy=POLICY)
        self.assertEqual(donors, [])

    def test_rd14_no_donor_means_no_borrow(self):
        ctx = PRep.decide_slot_upgrade(
            candidate_score=100.0, weakest=weakest(score=10.0), target_limit=3,
            donors=[], at_dynamic_limit=True, min_hold_days=2, policy=POLICY)
        self.assertFalse(ctx["borrow_ready"])
        self.assertNotEqual(ctx["state"], "slot_borrow_ready")


class SlotUpgradeStateTests(unittest.TestCase):
    """RD-6 … RD-11 —— 状态机与优先级。"""

    def decide(self, **over):
        kwargs = dict(candidate_score=60.0, weakest=weakest(), target_limit=3,
                      donors=[], at_dynamic_limit=True, min_hold_days=2, policy=POLICY)
        kwargs.update(over)
        return PRep.decide_slot_upgrade(**kwargs)

    def test_rd6_t1_locked_wins_over_everything(self):
        ctx = self.decide(candidate_score=100.0,
                          weakest=weakest(score=5.0, available_qty=0))
        self.assertEqual(ctx["state"], "t1_locked")
        self.assertFalse(ctx["eligible"])

    def test_rd6b_t1_locked_is_not_urgent_upgraded(self):
        """T+1 锁定不得因为候选极强被普通/紧急 upgrade 绕过。"""
        ctx = self.decide(candidate_score=100.0,
                          weakest=weakest(score=0.0, available_qty=0))
        self.assertEqual(ctx["state"], "t1_locked")
        self.assertFalse(ctx["eligible"])

    def test_rd7_min_hold_produces_observe(self):
        """非紧急（候选未达升级线）但净优势足够的升级被观察期挡住。"""
        ctx = self.decide(candidate_score=70.0, weakest=weakest(score=30.0, hold_days=0))
        self.assertFalse(ctx["urgent"])
        self.assertEqual(ctx["state"], "observe")
        self.assertFalse(ctx["eligible"])

    def test_rd7b_min_hold_does_not_block_urgent(self):
        ctx = self.decide(candidate_score=90.0, weakest=weakest(score=30.0, hold_days=0))
        self.assertEqual(ctx["state"], "urgent_upgrade")
        self.assertTrue(ctx["eligible"])

    def test_rd8_urgent_upgrade(self):
        ctx = self.decide(candidate_score=80.0, weakest=weakest(score=30.0))
        self.assertTrue(ctx["urgent"])
        self.assertEqual(ctx["state"], "urgent_upgrade")
        self.assertTrue(ctx["eligible"])

    def test_rd9_regular_upgrade(self):
        """非紧急但净优势足够 + 最弱仓已到 any_replace 线。"""
        ctx = self.decide(candidate_score=65.0, weakest=weakest(score=40.0),
                          at_dynamic_limit=False)
        self.assertFalse(ctx["urgent"])
        self.assertEqual(ctx["state"], "upgrade_ready")
        self.assertTrue(ctx["eligible"])

    def test_rd9b_regular_upgrade_via_review_action(self):
        """review_action 在 {watch,reduce,exit} 且弱于 replace_score 时也可换。"""
        ctx = self.decide(candidate_score=71.0,
                          weakest=weakest(score=50.0, review_action="watch"),
                          at_dynamic_limit=False)
        self.assertFalse(ctx["urgent"])
        self.assertEqual(ctx["state"], "upgrade_ready")

    def test_rd10_full_slot_upgrade(self):
        ctx = self.decide(candidate_score=60.0, weakest=weakest(score=30.0),
                          target_limit=3, at_dynamic_limit=True)
        self.assertEqual(ctx["state"], "upgrade_ready")
        self.assertTrue(ctx["eligible"])

    def test_rd10b_full_slot_requires_at_dynamic_limit(self):
        """未达动态上限时，同样的分数差不得走满席升级。"""
        ctx = self.decide(candidate_score=65.0, weakest=weakest(score=60.0),
                          at_dynamic_limit=False)
        self.assertNotEqual(ctx["state"], "upgrade_ready")

    def test_rd11_edge_insufficient(self):
        ctx = self.decide(candidate_score=50.0, weakest=weakest(score=48.0))
        self.assertEqual(ctx["state"], "edge_insufficient")
        self.assertFalse(ctx["eligible"])

    def test_rd11b_no_weakest_position_returns_borrow_only_schema(self):
        ctx = PRep.decide_slot_upgrade(
            candidate_score=60.0, weakest=None, target_limit=3, donors=[],
            at_dynamic_limit=False, min_hold_days=2, policy=POLICY)
        self.assertNotIn("weakest", ctx)
        self.assertNotIn("state", ctx)
        self.assertIn("borrow_candidate_score", ctx)
        self.assertFalse(ctx["eligible"])

    def test_rd15_same_input_same_output(self):
        cases = [
            dict(candidate_score=80.0, weakest=weakest(score=30.0)),
            dict(candidate_score=50.0, weakest=weakest(score=48.0)),
            dict(candidate_score=100.0, weakest=weakest(score=5.0, available_qty=0)),
            dict(candidate_score=65.0, weakest=weakest(score=40.0), at_dynamic_limit=False),
        ]
        for extra in cases:
            with self.subTest(**{k: v for k, v in extra.items() if k != "weakest"}):
                self.assertEqual(self.decide(**extra), self.decide(**extra))


class ThresholdBoundaryTests(unittest.TestCase):
    """规格 §58 —— 钉住 ``>=`` / ``>``、``<`` / ``<=``，防止重构漂移。"""

    def test_borrow_min_candidate_score_is_inclusive(self):
        at = POLICY.borrow_min_candidate_score
        donors = [{"account_id": "shared_pool", "limit": 15, "count": 3,
                   "remaining_after": 3, "unused_pool_slots": 12}]
        ctx = PRep.decide_slot_upgrade(
            candidate_score=at, weakest=None, target_limit=3, donors=donors,
            at_dynamic_limit=False, min_hold_days=2, policy=POLICY)
        self.assertTrue(ctx["borrow_ready"], "candidate == borrow_min_candidate 必须借位")
        below = PRep.decide_slot_upgrade(
            candidate_score=at - 0.01, weakest=None, target_limit=3, donors=donors,
            at_dynamic_limit=False, min_hold_days=2, policy=POLICY)
        self.assertFalse(below["borrow_ready"])

    def test_borrow_min_edge_is_inclusive(self):
        donors = [{"account_id": "sector_rotation", "limit": 5, "count": 2,
                   "remaining_after": 4}]
        base = POLICY.borrow_min_candidate_score
        # edge == borrow_min_edge 时仍可借（非 pool donor 必须满足 edge）
        ctx = PRep.decide_slot_upgrade(
            candidate_score=base, weakest=weakest(score=base - POLICY.borrow_min_edge),
            target_limit=3, donors=donors, at_dynamic_limit=False, min_hold_days=2,
            policy=POLICY)
        self.assertTrue(ctx["borrow_ready"], "edge == borrow_min_edge 必须借位")
        below = PRep.decide_slot_upgrade(
            candidate_score=base,
            weakest=weakest(score=base - POLICY.borrow_min_edge + 0.01),
            target_limit=3, donors=donors, at_dynamic_limit=False, min_hold_days=2,
            policy=POLICY)
        self.assertFalse(below["borrow_ready"])

    def test_upgrade_min_candidate_score_is_inclusive(self):
        """``candidate >= upgrade_min_candidate`` 必须成立，且 edge 同时达标。"""
        at = POLICY.upgrade_min_candidate_score
        weak = urgent_weak_score(at)
        ctx = PRep.decide_slot_upgrade(
            candidate_score=at, weakest=weakest(score=weak),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=2,
            policy=POLICY)
        self.assertTrue(ctx["urgent"], "candidate == upgrade_min_candidate 必须紧急")
        below = PRep.decide_slot_upgrade(
            candidate_score=at - 0.01, weakest=weakest(score=weak),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=2,
            policy=POLICY)
        self.assertFalse(below["urgent"], "candidate < upgrade_min_candidate 不得紧急")

    def test_upgrade_min_edge_is_inclusive(self):
        at = POLICY.upgrade_min_candidate_score
        weak = urgent_weak_score(at)
        ctx = PRep.decide_slot_upgrade(
            candidate_score=at, weakest=weakest(score=weak),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=2,
            policy=POLICY)
        self.assertTrue(ctx["urgent"], "edge == upgrade_min_edge 必须紧急")
        below = PRep.decide_slot_upgrade(
            candidate_score=at, weakest=weakest(score=weak + 0.01),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=2,
            policy=POLICY)
        self.assertFalse(below["urgent"], "edge < upgrade_min_edge 不得紧急")

    def test_full_cap_edge_uses_net_edge_inclusive(self):
        """full_slot 用的是扣除执行缓冲后的 net edge，且边界含等号。"""
        target = POLICY.full_cap_edge + POLICY.execution_buffer
        weakest_score = POLICY.full_cap_max_score - 1.0
        at = weakest_score + target
        ctx = PRep.decide_slot_upgrade(
            candidate_score=at, weakest=weakest(score=weakest_score),
            target_limit=3, donors=[], at_dynamic_limit=True, min_hold_days=10,
            policy=POLICY)
        self.assertEqual(ctx["state"], "upgrade_ready",
                         "net_edge == full_cap_edge 必须升级")
        below = PRep.decide_slot_upgrade(
            candidate_score=at - 0.01, weakest=weakest(score=weakest_score),
            target_limit=3, donors=[], at_dynamic_limit=True, min_hold_days=10,
            policy=POLICY)
        self.assertNotEqual(below["state"], "upgrade_ready")

    def test_full_cap_max_score_is_exclusive(self):
        """``weakest.score < full_cap_max_score``：等于该分不得走满席升级。"""
        at = POLICY.full_cap_max_score
        ctx = PRep.decide_slot_upgrade(
            candidate_score=100.0, weakest=weakest(score=at),
            target_limit=3, donors=[], at_dynamic_limit=True, min_hold_days=10,
            policy=POLICY)
        self.assertNotEqual(ctx["state"], "upgrade_ready",
                            "weakest == full_cap_max_score 仍走了满席升级")

    def test_any_replace_score_is_inclusive(self):
        """``weakest.score <= any_replace_score`` 时，净优势达标即可换。"""
        at = POLICY.any_replace_score
        candidate = at + POLICY.replace_edge + POLICY.execution_buffer
        ctx = PRep.decide_slot_upgrade(
            candidate_score=candidate, weakest=weakest(score=at), target_limit=3,
            donors=[], at_dynamic_limit=False, min_hold_days=10, policy=POLICY)
        self.assertFalse(ctx["urgent"], "fixture 落进了 urgent 分支")
        self.assertEqual(ctx["state"], "upgrade_ready",
                         "weakest == any_replace_score 必须可换")
        above = PRep.decide_slot_upgrade(
            candidate_score=candidate, weakest=weakest(score=at + 0.01),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=10,
            policy=POLICY)
        self.assertNotEqual(above["state"], "upgrade_ready",
                            "weakest > any_replace_score 仍走了 any-replace 分支")

    def test_replace_score_is_strict_for_review_action_branch(self):
        """review_action 分支要求 ``weakest.score < replace_score``（严格小于）。"""
        at = POLICY.replace_score
        need = POLICY.replace_edge + POLICY.execution_buffer
        ctx = PRep.decide_slot_upgrade(
            candidate_score=at + need, weakest=weakest(score=at, review_action="watch"),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=10,
            policy=POLICY)
        self.assertNotEqual(ctx["state"], "upgrade_ready",
                            "weakest == replace_score 走了 review_action 分支")

    def test_exit_score_boundary(self):
        at = POLICY.exit_score
        ctx = PRep.decide_slot_upgrade(
            candidate_score=90.0, weakest=weakest(score=at),
            target_limit=3, donors=[], at_dynamic_limit=False, min_hold_days=10,
            policy=POLICY)
        self.assertTrue(ctx["urgent"], "weakest == exit_score 必须算紧急")

    def test_policy_defaults_match_production_constants(self):
        """policy 默认值必须与 paper_trading 顶部常量一致（防止悄悄调参）。"""
        import paper_trading as PT
        self.assertEqual(POLICY.strategy_min_positions, PT.STRATEGY_MIN_POSITIONS)
        self.assertEqual(POLICY.strategy_max_positions, PT.STRATEGY_MAX_POSITIONS)
        self.assertEqual(POLICY.shared_pool_max_positions, PT.SHARED_POOL_MAX_POSITIONS)
        self.assertEqual(POLICY.borrow_min_candidate_score,
                         PT.SLOT_BORROW_MIN_CANDIDATE_SCORE)
        self.assertEqual(POLICY.borrow_min_edge, PT.SLOT_BORROW_MIN_EDGE)
        self.assertEqual(POLICY.upgrade_min_candidate_score,
                         PT.SLOT_UPGRADE_MIN_CANDIDATE_SCORE)
        self.assertEqual(POLICY.upgrade_min_edge, PT.SLOT_UPGRADE_MIN_EDGE)
        self.assertEqual(POLICY.execution_buffer, PT.POSITION_REPLACEMENT_EXECUTION_BUFFER)
        self.assertEqual(POLICY.full_cap_edge, PT.POSITION_FULL_CAP_REPLACEMENT_EDGE)
        self.assertEqual(POLICY.full_cap_max_score, PT.POSITION_FULL_CAP_MAX_SCORE)
        self.assertEqual(POLICY.replace_edge, PT.POSITION_REVIEW_REPLACEMENT_EDGE)
        self.assertEqual(POLICY.any_replace_score, PT.POSITION_REVIEW_ANY_REPLACE_SCORE)
        self.assertEqual(POLICY.replace_score, PT.POSITION_REVIEW_REPLACE_SCORE)
        self.assertEqual(POLICY.exit_score, PT.POSITION_REVIEW_EXIT_SCORE)
        self.assertEqual(POLICY.lot_size, PT.LOT_SIZE)
        self.assertEqual(PT.REPLACEMENT_POLICY, POLICY)

    def test_score_candidate_matches_production_adapter(self):
        """adapter 与纯模块必须给出同一个分数（不再各留一份公式）。"""
        import paper_trading as PT
        sample = signal(entry=77.7, t_score=0.8, rank_score=33.3)
        self.assertEqual(PT.PRep.score_candidate(sample), PRep.score_candidate(sample))


class ModuleBoundaryTests(unittest.TestCase):
    """规格 §61-§62 —— 纯模块的硬边界（零项目 import / 零 I/O / 零时钟）。"""

    def source(self):
        with open(os.path.join(BACKEND_DIR, "paper_replacement_decision.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def code_only(self):
        """剥掉模块 docstring —— 它按设计会**列举**被禁止的 import 作为说明。"""
        raw = self.source()
        tree = ast.parse(raw)
        first = tree.body[0]
        if isinstance(first, ast.Expr):
            return "\n".join(raw.splitlines()[first.end_lineno:])
        return raw

    def test_zero_project_imports(self):
        tree = ast.parse(self.source())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(sorted(roots - {"__future__", "dataclasses", "json", "typing"}),
                         [], f"纯模块出现未登记 import 根：{sorted(roots)}")

    def test_zero_io_and_zero_clock(self):
        body = self.code_only()
        for token in ("sqlite3", "requests", "httpx", "socket", "subprocess", "connect("):
            self.assertNotIn(token, body, f"纯模块出现 {token}")
        for token in ("date.today", "datetime.now", "time.time"):
            self.assertNotIn(token, body, f"纯模块出现 {token}")


if __name__ == "__main__":
    unittest.main()
