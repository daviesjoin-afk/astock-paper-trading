# -*- coding: utf-8 -*-
"""热门启动段画像对缺列/NaN 的容错测试。

背景（2026-09-08 生产事故）：「策略选股」页复用 ``main._select_uncached``
流水线，其因子表不含模拟盘扫描器注入的 ``sector_early_rotation_score``
列；``_hot_leader_profile`` 把缺失列的 NaN 直接汇入 score 加权和，导致
``ranked[score > -990]`` 全表清空（NaN 与任何阈值比较恒为 False），5 个
策略全部 empty(0)。契约：可选列缺失时保持中性 0，绝不产生 NaN。
"""
import unittest

import numpy as np
import pandas as pd

import strategies as S


def _mini_table(rows=8):
    codes = [f"60000{i}" for i in range(rows)]
    return pd.DataFrame(
        {
            "name": [f"股{i}" for i in range(rows)],
            "industry": "电子",
            "price": np.linspace(10, 12, rows),
            "pct": np.linspace(0.5, 5.0, rows),
            "amount": np.linspace(5e7, 9e7, rows),
            "turnover": np.linspace(1.0, 5.0, rows),
            "mom_short": np.linspace(-1, 2, rows),
            "mom5_raw": np.linspace(-2, 6, rows),
            "mom20_raw": np.linspace(-5, 15, rows),
            "mom60_raw": np.linspace(-10, 20, rows),
            "flow": np.linspace(-1, 2, rows),
            "volsurge": np.linspace(-1, 3, rows),
            "sentiment": np.linspace(-0.5, 1.5, rows),
            "vol_surge_raw": np.linspace(0.4, 2.5, rows),
            "main_pct": np.linspace(-1, 4, rows),
            "super_net_raw": np.linspace(-5e6, 9e6, rows),
            "ma20": np.linspace(9.5, 11.5, rows),
            "ma60": np.linspace(9.0, 11.0, rows),
            # build_factor_table 恒建的估值列（_core_stock_preference 依赖）
            "value": np.linspace(-0.5, 1.0, rows),
            "quality": np.linspace(-0.3, 0.8, rows),
            "rsi": np.linspace(30, 70, rows),
        },
        index=codes,
    )


class HotLeaderProfileNaNTests(unittest.TestCase):
    def test_missing_optional_columns_stay_neutral(self):
        table = _mini_table()
        self.assertNotIn("sector_early_rotation_score", table.columns)
        self.assertNotIn("sector_heat_score", table.columns)
        profile = S._hot_leader_profile(table)
        self.assertEqual(int(profile["score"].isna().sum()), 0)
        self.assertEqual(int(profile["overheat"].isna().sum()), 0)
        self.assertGreater(float(profile["score"].max()), 0.0)

    def test_missing_column_does_not_wipe_paper_picks(self):
        table = _mini_table()
        result = S.run_strategy("one_to_two", table, topn=5, gate=None,
                                first_board_codes=None)
        self.assertEqual(len(result["picks"]), 5)
        scores = [pick["score"] for pick in result["picks"]]
        self.assertTrue(all(np.isfinite(s) for s in scores))

    def test_present_column_still_counts(self):
        table = _mini_table()
        table["sector_early_rotation"] = True
        table["sector_early_rotation_score"] = 1.0
        profile = S._hot_leader_profile(table)
        self.assertEqual(int(profile["score"].isna().sum()), 0)

    def test_nan_cells_in_optional_columns_stay_neutral(self):
        table = _mini_table()
        table["sector_early_rotation_score"] = np.nan
        table["sector_heat_score"] = np.nan
        profile = S._hot_leader_profile(table)
        self.assertEqual(int(profile["score"].isna().sum()), 0)
        result = S.run_strategy("bottom_reversal", table, topn=3, gate=None,
                                first_board_codes=None)
        self.assertEqual(len(result["picks"]), 3)


if __name__ == "__main__":
    unittest.main()
