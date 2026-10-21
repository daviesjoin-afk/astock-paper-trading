# -*- coding: utf-8 -*-
"""自进化候选通道门禁：结构变更必须条件化，纯权重候选才进自动通道。

背景：``_structure_target`` 曾对三个加权账户无条件返回结构变更，而结构变更
必须人工确认，导致候选永久停在 ``eligible_structural_review``，唯一能自动
应用的纯权重形态从未被生成。这里的断言在修复前都会失败。
"""
import unittest

import adaptive_selection as AS

WEIGHTED_ACCOUNTS = ("trend_pullback", "sector_rotation", "tq_breakout")
HARD_RULE_ACCOUNTS = ("reported_profit_breakout", "main_force_top10")


class StructureTargetGatingTests(unittest.TestCase):
    def test_no_loss_evidence_keeps_structure_unchanged(self):
        for account in WEIGHTED_ACCOUNTS:
            with self.subTest(account=account):
                model, _paths, mutation, reason = AS._structure_target(
                    account, "base_model", "mature", 0.05)
                self.assertEqual("none", mutation)
                self.assertEqual("base_model", model)
                self.assertEqual("", reason)

    def test_missing_reward_evidence_keeps_structure_unchanged(self):
        _, _paths, mutation, _ = AS._structure_target("tq_breakout", "one_to_two", "mature", None)
        self.assertEqual("none", mutation)

    def test_loss_without_enough_evidence_keeps_structure_unchanged(self):
        for tier in ("waiting", "fast_shadow", "micro"):
            with self.subTest(tier=tier):
                _, _paths, mutation, _ = AS._structure_target("tq_breakout", "one_to_two", tier, -0.50)
                self.assertEqual("none", mutation)

    def test_loss_with_standard_evidence_requests_structural_change(self):
        cases = {
            "trend_pullback": ("model_family_switch", "trend_continuation"),
            "sector_rotation": ("add_individual_strong_path", None),
            "tq_breakout": ("add_board_acceleration_path", None),
        }
        for account, (expected_mutation, expected_model) in cases.items():
            with self.subTest(account=account):
                model, _paths, mutation, reason = AS._structure_target(
                    account, "base_model", "standard", -0.20)
                self.assertEqual(expected_mutation, mutation)
                self.assertTrue(reason)
                if expected_model:
                    self.assertEqual(expected_model, model)

    def test_hard_rule_accounts_never_request_structure(self):
        for account in HARD_RULE_ACCOUNTS:
            with self.subTest(account=account):
                _, _paths, mutation, _ = AS._structure_target(account, account, "mature", -0.90)
                self.assertEqual("none", mutation)


class ProposalChannelTests(unittest.TestCase):
    """奖励没有明显偏离时，候选必须是纯权重补丁。"""

    BASE_CONDITIONS = {"pct_high": 5.0}
    BASE_DELTA = 0.01

    def _proposal(self, account="tq_breakout", model="one_to_two", reward=0.0, tier="mature"):
        return AS._proposal(
            account, model, dict(AS.BASE_WEIGHTS[model]), self.BASE_DELTA,
            dict(self.BASE_CONDITIONS), "balanced", reward, tier,
        )

    def test_neutral_reward_produces_weight_only_patch(self):
        proposal = self._proposal(reward=0.02)
        self.assertEqual("none", proposal["mutation_type"])
        self.assertEqual(self.BASE_DELTA, proposal["entry_score_delta"])
        self.assertEqual(self.BASE_CONDITIONS, proposal["conditions"])

    def test_large_negative_reward_moves_delta_and_conditions(self):
        proposal = self._proposal(reward=-0.30)
        self.assertNotEqual(self.BASE_DELTA, proposal["entry_score_delta"])
        self.assertNotEqual(self.BASE_CONDITIONS, proposal["conditions"])

    def test_large_positive_reward_also_moves_conditions(self):
        proposal = self._proposal(reward=0.30)
        self.assertNotEqual(self.BASE_CONDITIONS, proposal["conditions"])

    def test_neutral_reward_still_adjusts_weights_under_a_regime_shift(self):
        # 平衡市乘子全为 1，权重本就不该动（那会落到 no_change）；换一个
        # 乘子有偏离的盘面，纯权重通道必须真的调整权重。
        proposal = AS._proposal(
            "tq_breakout", "one_to_two", dict(AS.BASE_WEIGHTS["one_to_two"]),
            self.BASE_DELTA, dict(self.BASE_CONDITIONS), "risk_off", 0.02, "mature",
        )
        self.assertNotEqual(dict(AS.BASE_WEIGHTS["one_to_two"]), proposal["weights"])
        self.assertEqual("none", proposal["mutation_type"])

    def test_structural_change_carries_its_reason(self):
        proposal = self._proposal(account="trend_pullback", model="bottom_reversal", reward=-0.30)
        self.assertEqual("model_family_switch", proposal["mutation_type"])
        self.assertTrue(proposal["structure_reason"])


if __name__ == "__main__":
    unittest.main()
