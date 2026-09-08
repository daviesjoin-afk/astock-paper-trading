# -*- coding: utf-8 -*-
"""策略相关.cluster（信号/持仓/收益/行业 → 分散化惩罚 + 簇预算）回归测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_clusters as SC


def _profile(signals=(), positions=(), industries=(), returns=()):
    return SC.similarity_profile(
        signal_codes=signals, position_codes=positions,
        industries=industries, returns=returns,
    )


class SimilarityTests(unittest.TestCase):
    def test_jaccard_basics(self):
        self.assertEqual(0.0, SC.jaccard(set(), set()))
        self.assertEqual(1.0, SC.jaccard({"a"}, {"a"}))
        self.assertAlmostEqual(1 / 3, SC.jaccard({"a", "b"}, {"b", "c"}), places=6)

    def test_identical_strategies_are_full_similarity(self):
        a = _profile(signals=["600000", "000001"], positions=["600000"],
                     industries=["银行"])
        b = _profile(signals=["600000", "000001"], positions=["600000"],
                     industries=["银行"])
        self.assertAlmostEqual(1.0, SC.pairwise_similarity(a, b), places=6)

    def test_disjoint_strategies_are_zero(self):
        a = _profile(signals=["600000"], positions=["600000"], industries=["银行"])
        b = _profile(signals=["300001"], positions=["300001"], industries=["医药"])
        self.assertEqual(0.0, SC.pairwise_similarity(a, b))

    def test_return_correlation_contributes_when_samples_are_enough(self):
        left = _profile(returns=[0.01, 0.02, -0.01, 0.03, 0.02, 0.01])
        right = _profile(returns=[0.01, 0.02, -0.01, 0.03, 0.02, 0.01])
        # 完全相关的收益序列 → 相关性 1.0 × 0.2 权重
        self.assertAlmostEqual(0.2, SC.pairwise_similarity(left, right), places=6)

    def test_insufficient_return_samples_transfer_weight(self):
        # 集合证据为 0.5（position jaccard），收益只有 2 个样本：
        # 权重让渡后总相似度应高于 0.35*0 = 仅靠集合证据的部分。
        left = _profile(positions=["600000", "000001"], returns=[0.01, 0.02])
        right = _profile(positions=["600000", "000001"], returns=[0.02, -0.01])
        self.assertGreater(SC.pairwise_similarity(left, right), 0.4)


class ClusterTests(unittest.TestCase):
    def test_ten_near_identical_strategies_form_one_cluster(self):
        profiles = {
            f"clone{index}": _profile(
                signals=["600000", "000001", "600036"],
                positions=["600000", "000001"], industries=["银行"],
            )
            for index in range(10)
        }
        clusters = SC.strategy_clusters(profiles)
        self.assertEqual(1, len(clusters))
        self.assertEqual(10, len(clusters[0]))

    def test_unrelated_strategies_stay_separate(self):
        profiles = {
            "bank": _profile(signals=["600000"], positions=["600000"], industries=["银行"]),
            "pharma": _profile(signals=["300001"], positions=["300001"], industries=["医药"]),
            "tech": _profile(signals=["002230"], positions=["002230"], industries=["软件"]),
        }
        clusters = SC.strategy_clusters(profiles)
        self.assertEqual(3, len(clusters))

    def test_cluster_of_returns_own_cluster(self):
        clusters = [{"a", "b"}, {"c"}]
        self.assertEqual({"a", "b"}, SC.cluster_of("b", clusters))
        self.assertEqual({"z"}, SC.cluster_of("z", clusters))


class BudgetPenaltyTests(unittest.TestCase):
    def test_single_strategy_has_no_penalty(self):
        self.assertEqual(1.0, SC.cluster_diversification_factor("a", [{"a"}]))
        self.assertEqual(1.0, SC.cluster_budget_multiplier([{"a"}]))

    def test_ten_clones_get_sqrt_ten_total_not_ten_times(self):
        """核心不变式：复制 10 个近似策略不能获得 10 倍风险额度。"""
        clusters = [{f"clone{index}" for index in range(10)}]
        factors = [SC.cluster_diversification_factor(name, clusters)
                   for name in sorted(clusters[0])]
        total_multiple = sum(factors)
        self.assertAlmostEqual(1.0 / (10 ** 0.5), factors[0], places=3)
        self.assertAlmostEqual(10 ** 0.5, total_multiple, places=3)
        self.assertLess(total_multiple, 10.0)
        # 同簇任意两个策略的合计也不超过 sqrt(2)。
        pair = sum(SC.cluster_diversification_factor(name, clusters)
                   for name in ("clone0", "clone1"))
        self.assertLess(pair, 2.0)

    def test_floor_keeps_budget_nonzero(self):
        clusters = [{f"s{index}" for index in range(64)}]
        factor = SC.cluster_diversification_factor("s0", clusters)
        self.assertEqual(0.3, factor)  # 1/sqrt(64)=0.125 → 地板 0.3
        self.assertGreater(factor, 0.0)

    def test_budget_multiplier_is_sublinear(self):
        clusters = [{f"s{index}" for index in range(n)} for n in (2, 4, 9)]
        multipliers = [SC.cluster_budget_multiplier(cluster) for cluster in clusters]
        self.assertAlmostEqual(2 ** 0.5, multipliers[0], places=3)
        self.assertAlmostEqual(4 ** 0.5, multipliers[1], places=3)
        self.assertAlmostEqual(9 ** 0.5, multipliers[2], places=3)
        for size, multiplier in zip((2, 4, 9), multipliers, strict=False):
            self.assertLess(multiplier, float(size))

    def test_budget_multiplier_honors_the_floor(self):
        # 64 簇：1/sqrt(64)=0.125 触发 0.3 地板 → 倍数 = 64×0.3 = 19.2，
        # 与 size × cluster_diversification_factor 严格一致。
        clusters = [{f"s{index}" for index in range(64)}]
        self.assertAlmostEqual(19.2, SC.cluster_budget_multiplier(clusters), places=3)
        self.assertAlmostEqual(
            SC.cluster_budget_multiplier(clusters),
            64 * SC.cluster_diversification_factor("s0", clusters), places=3,
        )


class WiringGuardTests(unittest.TestCase):
    @staticmethod
    def _source(name="paper_trading.py"):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_runtime_compilation_feeds_the_diversification_factor(self):
        body = self._source()
        self.assertIn("_strategy_cluster_factors", body)
        self.assertIn("diversification=diversification", body)

    def test_both_budget_paths_use_cluster_factors(self):
        body = self._source()
        # 席位分配与资金预算两条路径都要接簇系数。
        self.assertGreaterEqual(body.count("cluster_factors"), 3)


if __name__ == "__main__":
    unittest.main()
