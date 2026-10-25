import copy
import unittest

import main_force_confirmation as M
import paper_trading as P

ASOF = "2026-10-23T10:00:00+08:00"


def quote(**changes):
    result = {"price": 10.0, "pct": 1.2, "main_pct": 6.0, "main_net": 600000,
              "amount": 10000000, "super_net": 200000, "vol_ratio": 2.0,
              "quote_at": "2026-10-23T09:59:00+08:00"}
    return {**result, **changes}


class MainForceConfirmationTests(unittest.TestCase):
    def test_positive_flow_at_unextended_price_confirms_without_mutation(self):
        source = quote()
        before = copy.deepcopy(source)
        result = M.assess(source, asof=ASOF)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["computed_main_pct"], 6)
        self.assertEqual(source, before)
        self.assertEqual(result["evidence_status"], "exploratory_not_validated_alpha")

    def test_unusable_or_incomplete_quotes_never_confirm(self):
        variants = [
            {"quote_at": None}, {"quote_at": "2026-10-23T09:56:59+08:00"},
            {"quote_at": "2026-10-23T10:00:01+08:00"}, {"price": 0},
            {"amount": 0}, {"super_net": None}, {"main_net": float("inf")},
            {"main_pct": float("nan")}, {"main_pct": True}, {"main_net": "bad"},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertFalse(M.assess(quote(**changes), asof=ASOF)["confirmed"])

    def test_higher_flow_does_not_hide_overheat_or_conflicting_inputs(self):
        for changes in ({"pct": 7}, {"vol_ratio": 8}, {"super_net": -1},
                        {"main_pct": -2}, {"main_pct": 60}, {"vol_ratio": 0.5}):
            with self.subTest(changes=changes):
                self.assertFalse(M.assess(quote(**changes), asof=ASOF)["confirmed"])
        self.assertEqual(M.assess(quote(main_pct=60), asof=ASOF)["status"], "conflicting")

    def test_bonus_cannot_rescue_baseline_or_hard_veto(self):
        evidence = M.assess(quote(), asof=ASOF)
        for score, blockers in ((0.63, []), (0.99, ["T+1"]), (float("nan"), [])):
            result = M.entry_effect("tq_breakout", evidence, base_score=score,
                                    threshold=0.64, blockers=blockers)
            self.assertEqual(result["bonus"], 0)
            self.assertFalse(result["passed"])

    def test_strategy_specific_bonus_and_quality_evidence_only(self):
        for strategy, expected in (("tq_breakout", .03), ("trend_pullback", .02),
                                   ("reported_profit_breakout", 0), ("main_force_top10", 0),
                                   ("user_strategy", 0)):
            result = M.entry_effect(strategy, {"confirmed": True}, base_score=.8,
                                    threshold=.64, blockers=[])
            self.assertEqual(result["bonus"], expected)
            self.assertTrue(result["passed"])

    def test_live_entry_uses_confirmation_without_changing_position_scale(self):
        # 基线分数必须留出加分空间：满分 1.0 的样本只能证明封顶生效，
        # 不能证明辅助确认真的进入了分数。
        args = ({"id": "tq_breakout"}, {"code": "000001", "score": .5})
        decision = {"hard_vetoes": [], "avg_score": .8}
        base = quote(pct=2.0, main_pct=4.0, vol_ratio=1.2)
        current = P._strategy_entry_assessment(*args, base, None, decision, asof=ASOF)
        missing = P._strategy_entry_assessment(
            *args, quote(pct=2.0, main_pct=4.0, vol_ratio=1.2, super_net=None),
            None, decision, asof=ASOF)
        self.assertTrue(current["passed"])
        self.assertTrue(missing["passed"])
        self.assertAlmostEqual(current["score"] - missing["score"], .03, places=3)
        self.assertEqual(current["position_scale"], missing["position_scale"])
        blocked = P._strategy_entry_assessment(
            *args, quote(), None, {"hard_vetoes": ["测试硬否决"], "avg_score": .8}, asof=ASOF)
        self.assertFalse(blocked["passed"])
        self.assertEqual(blocked["main_force_confirmation"]["entry_effect"]["bonus"], 0)

    def test_standalone_main_force_retains_gates_but_does_not_double_vote_flow(self):
        """只验证入场模型本身；**开仓许可**由周期参与者决定，不看这里的 passed。

        main_force_top10 已退出独立资金账户（retiring）：它不在周期参与者集合里，
        根本走不到 ``_buy_order``。这里的 ``passed`` 只说明"若被允许，模型怎么算分"，
        不能被读成"该策略还能开仓"。
        """
        import strategy_roster as ROSTER

        self.assertTrue(ROSTER.is_exit_only("main_force_top10"))
        self.assertIsNotNone(ROSTER.entry_block_reason("main_force_top10"))
        result = P._strategy_entry_assessment(
            {"id": "main_force_top10"}, {"code": "000001", "score": .8},
            quote(), None, {"hard_vetoes": [], "avg_score": .8}, asof=ASOF)
        self.assertTrue(result["passed"])
        flow_checks = [row for row in result["checks"] if row["name"].startswith("资金成交强度")]
        self.assertEqual(len(flow_checks), 1)
        self.assertEqual(flow_checks[0]["weight"], 0)
        bad = P._strategy_entry_assessment(
            {"id": "main_force_top10"}, {"code": "000001", "score": .99},
            quote(super_net=-1), None, {"hard_vetoes": [], "avg_score": .99}, asof=ASOF)
        self.assertFalse(bad["passed"])


if __name__ == "__main__":
    unittest.main()
