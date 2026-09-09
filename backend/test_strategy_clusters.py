# -*- coding: utf-8 -*-
"""策略相关.cluster（信号/持仓/收益/行业/DSL 结构 → cluster-first 簇预算）回归测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_clusters as SC


def _profile(signals=(), positions=(), industries=(), returns=(), dsl_ast=None):
    return SC.similarity_profile(
        signal_codes=signals, position_codes=positions,
        industries=industries, returns=returns, dsl_ast=dsl_ast,
    )


def _clone_ast(threshold=20.0):
    """一个典型 DSL：close > ma(20) 且 volume > 阈值（参数可扫描）。"""
    return {
        "op": "and",
        "conditions": [
            {"op": "gt", "left": {"op": "field", "name": "close"},
             "right": {"op": "indicator", "name": "ma", "window": 20}},
            {"op": "gt", "left": {"op": "field", "name": "volume"},
             "right": {"op": "const", "value": threshold}},
        ],
    }


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
        left = _profile(returns=[0.01, 0.02, -0.01, 0.03, 0.02, 0.01],
                        dsl_ast=_clone_ast())
        right = _profile(returns=[0.01, 0.02, -0.01, 0.03, 0.02, 0.01],
                         dsl_ast=_clone_ast())
        # 收益相关 1.0×0.15 + DSL 同构 1.0×0.15（其余行为证据为空）。
        self.assertAlmostEqual(0.30, SC.pairwise_similarity(left, right), places=6)

    def test_insufficient_return_samples_transfer_weight(self):
        # 集合证据为 0.5（position jaccard），收益只有 2 个样本：
        # 缺失证据的权重按比例归一化到剩余证据，不会稀释已知证据。
        left = _profile(positions=["600000", "000001"], returns=[0.01, 0.02])
        right = _profile(positions=["600000", "000001"], returns=[0.02, -0.01])
        self.assertGreater(SC.pairwise_similarity(left, right), 0.35)

    # -- PR-27：DSL 结构相似度（第五类证据） -------------------------
    def test_dsl_param_scan_is_still_a_structural_clone(self):
        """同构不同参数（阈值/窗口扫描）→ 结构相似度必须 = 1.0。"""
        self.assertAlmostEqual(
            1.0, SC.dsl_ast_similarity(_clone_ast(20.0), _clone_ast(25.0)), places=6,
        )

    def test_dsl_different_structure_is_not_a_structural_clone(self):
        trend = _clone_ast()
        mean_revert = {
            "op": "and",
            "conditions": [
                {"op": "lt", "left": {"op": "field", "name": "close"},
                 "right": {"op": "indicator", "name": "rsi", "window": 14}},
                {"op": "gt", "left": {"op": "field", "name": "turnover"},
                 "right": {"op": "const", "value": 3.0}},
            ],
        }
        # 结构骨架虽共享，但达不到硬证据阈值 → 不构成结构性克隆。
        self.assertLess(SC.dsl_ast_similarity(trend, mean_revert), SC.DSL_CLONE_THRESHOLD)

    def test_dsl_missing_renormalizes_weights(self):
        # 两侧 DSL 都缺失且收益样本不足 → dsl+return 权重按比例
        # 归一化到 signal/position/industry 上。
        left = _profile(positions=["600000", "000001"])
        right = _profile(positions=["600000", "000001"])
        expected = 1.0 * (SC.SIMILARITY_WEIGHTS["position"]
                          / sum(value for key, value in SC.SIMILARITY_WEIGHTS.items()
                                if key not in ("dsl", "return")))
        self.assertAlmostEqual(expected, SC.pairwise_similarity(left, right), places=6)

    def test_zero_history_clone_is_caught_by_structure_alone(self):
        """PR-27 验收前置：新复制策略零行为历史，仅凭 DSL 同构即归簇。"""
        profiles = {
            "original": _profile(dsl_ast=_clone_ast(20.0)),
            "clone": _profile(dsl_ast=_clone_ast(25.0)),
        }
        clusters = SC.strategy_clusters(profiles)
        self.assertEqual(1, len(clusters))

    def test_zero_history_different_dsl_stays_separate(self):
        profiles = {
            "trend": _profile(dsl_ast=_clone_ast(20.0)),
            "revert": _profile(dsl_ast={
                "op": "lt", "left": {"op": "field", "name": "close"},
                "right": {"op": "indicator", "name": "rsi", "window": 14},
            }),
        }
        clusters = SC.strategy_clusters(profiles)
        self.assertEqual(2, len(clusters))


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

    # -- PR-27：取消 0.3 地板 + cluster-first 封顶预算 ----------------
    def test_diversification_floor_is_gone(self):
        clusters = [{f"s{index}" for index in range(64)}]
        # 1/sqrt(64)=0.125，不再被 0.3 地板抬高。
        self.assertAlmostEqual(0.125, SC.cluster_diversification_factor("s0", clusters), places=4)

    def test_cluster_budget_is_capped_at_single_plus_bonus(self):
        """PR-27 验收：克隆 2/10/50 份，簇总预算不得明显增长（封顶 +10%）。"""
        self.assertEqual(1.0, SC.cluster_budget_multiplier([{"solo"}]))
        self.assertAlmostEqual(1.02, SC.cluster_budget_multiplier([{f"c{i}" for i in range(2)}]), places=4)
        self.assertAlmostEqual(1.10, SC.cluster_budget_multiplier([{f"c{i}" for i in range(10)}]), places=4)
        self.assertAlmostEqual(1.10, SC.cluster_budget_multiplier([{f"c{i}" for i in range(50)}]), places=4)

    def test_clone_50_cannot_squeeze_an_independent_strategy(self):
        """PR-27 验收：50 个克隆 + 1 个真正独立策略，独立策略预算分毫不减。"""
        clones = {f"clone{index}": _profile(dsl_ast=_clone_ast()) for index in range(50)}
        independent = {"independent": _profile(
            signals=["300750"], positions=["300750"], industries=["电池"],
            dsl_ast={"op": "gt", "left": {"op": "field", "name": "close"},
                     "right": {"op": "indicator", "name": "ma", "window": 60}},
        )}
        clusters = SC.strategy_clusters({**clones, **independent})
        self.assertEqual(2, len(clusters))
        clone_cluster = SC.cluster_of("clone0", clusters)
        independent_cluster = SC.cluster_of("independent", clusters)
        self.assertEqual(50, len(clone_cluster))
        self.assertEqual(1, len(independent_cluster))
        # 独立策略：无任何簇惩罚，预算倍数 = 1.0。
        self.assertEqual(
            1.0,
            SC.cluster_budget_multiplier(independent_cluster),
        )
        # 50 克隆簇总预算封顶 1.1 倍，而不是 50 倍。
        self.assertAlmostEqual(1.10, SC.cluster_budget_multiplier(clone_cluster), places=4)
        # 每成员均分：单个克隆最多拿到簇预算的 1/50。
        per_member = SC.cluster_budget_multiplier(clone_cluster) / 50
        self.assertLess(per_member, 0.03)


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


class AllocationExplainTests(unittest.TestCase):
    """PR-17：分配可解释性 API 的数据面。"""

    def test_expose_per_strategy_factors_budgets_and_waiting_reason(self):
        import tempfile

        import paper_trading as paper

        # 隔离：不碰 checkout 里的真实账本（init_db/席位分配版本会写库）。
        tmp = tempfile.mkdtemp()
        paper.DB_PATH = os.path.join(tmp, "paper_trading.sqlite3")
        data = paper.strategy_allocation_explain()
        self.assertEqual(5, len(data["strategies"]))
        for row in data["strategies"]:
            for key in ("base_priority", "regime", "confidence", "health",
                        "data_quality", "diversification", "capital_scale",
                        "target_budget", "available_budget", "position_limit",
                        "waiting_reason"):
                self.assertIn(key, row)
            # 前端不能只看到一个总额：预算字段必须可拆。
            self.assertIn("target_amount", row["target_budget"])
            self.assertIn("allowance_amount", row["available_budget"])
            self.assertIn("cluster_size", row["diversification"])
            self.assertIn("market_light", row["regime"])
        # waiting_reason 结构完整（无等待时字段为 None 而非缺失）。
        first = data["strategies"][0]["waiting_reason"]
        for key in ("status", "code", "reason", "intended_date"):
            self.assertIn(key, first)
