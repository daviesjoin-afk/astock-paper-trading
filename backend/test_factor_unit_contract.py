# -*- coding: utf-8 -*-
"""因子单位契约回归测试（P0：fraction vs percentage points 的 100 倍错配）。

背景
----
``compute_price_factors`` 用 ``close_now / close_past - 1`` 产出动量，
因此 ``mom5`` / ``mom20`` / ``mom60`` / ``rev5`` 以及因子表里的
``mom5_raw`` / ``mom20_raw`` / ``mom60_raw`` **都是 fraction**：

    0.02 == +2%      0.18 == +18%      -0.03 == -3%

而 ``pct`` / ``main_pct`` 这类行情字段是 **percentage points**（2.0 == +2%）。
策略打分里曾把 18 / 35 / 1.0~10.0 / 15 / 2.0 这些**百分点**字面量直接与
``mom*_raw`` 比较，导致对应分量恒为 0（死分支），
例如 ``mom5.ge(18)`` 相对真实的 +18%（=0.18）永远不成立。

本文件只锁这条契约：**每一个断言在修复前都应当失败**。
"""
import ast
import os
import unittest

import numpy as np
import pandas as pd

import factor_units as FU
import factors as F
import strategies as S

BACKEND = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_SRC = os.path.join(BACKEND, "strategies.py")


# --------------------------------------------------------------------------
# 公共夹具
# --------------------------------------------------------------------------
def _base_row(**overrides):
    """一行"中性"因子数据；只覆盖调用方关心的列。"""
    row = {
        "name": "测试股",
        "industry": "测试行业",
        "price": 10.0,
        "pct": 1.0,                  # percentage points：1.0 == +1%
        "amount": 5e7,
        "turnover": 3.0,
        "main_pct": 0.0,
        "super_net_raw": 0.0,
        "mom5_raw": 0.0,             # fraction
        "mom20_raw": 0.0,            # fraction
        "mom60_raw": 0.0,            # fraction
        # sentiment_pioneer 的权重列（_run_paper_strategy 按权重取列）。
        "mom_short": 0.0,
        "volsurge": 0.0,
        "vol_surge_raw": 1.0,
        "rsi14_raw": 55.0,
        "flow": 0.0,
        "above_boll_mid": False,
        "boll_mid_breakout": False,
        "ma20": 10.0,
        "ma60": 10.0,
    }
    row.update(overrides)
    return row


def _table(rows):
    return pd.DataFrame(rows, index=[f"T{i}" for i in range(len(rows))])


def _kline_frame(last_close, periods=70, base=10.0):
    """确定性收盘序列：除最后一根外恒为 ``base``。

    这样 5 日动量恰好等于 ``last_close / base - 1``。
    """
    dates = pd.bdate_range("2026-01-05", periods=periods)
    close = np.full(periods, base, dtype=float)
    close[-1] = last_close
    return pd.DataFrame({"close": close, "amount": np.full(periods, 8e7)}, index=dates)


# --------------------------------------------------------------------------
# 单位契约助手
# --------------------------------------------------------------------------
class FactorUnitHelperTests(unittest.TestCase):
    """``factor_units`` 的换算与有限性契约。"""

    def test_positive_values(self):
        self.assertAlmostEqual(FU.fraction_to_pct_points(0.02), 2.0, places=12)
        self.assertAlmostEqual(FU.fraction_to_pct_points(0.18), 18.0, places=12)
        self.assertAlmostEqual(FU.pct_points_to_fraction(2.0), 0.02, places=12)
        self.assertAlmostEqual(FU.pct_points_to_fraction(18.0), 0.18, places=12)

    def test_negative_values(self):
        self.assertAlmostEqual(FU.fraction_to_pct_points(-0.03), -3.0, places=12)
        self.assertAlmostEqual(FU.pct_points_to_fraction(-3.0), -0.03, places=12)

    def test_zero_values(self):
        self.assertEqual(FU.fraction_to_pct_points(0), 0.0)
        self.assertEqual(FU.pct_points_to_fraction(0), 0.0)
        self.assertTrue(FU.is_fraction_like(0.0))

    def test_round_trip_is_lossless_for_typical_thresholds(self):
        for pct in (1.0, 2.0, 8.0, 10.0, 15.0, 18.0, 35.0, -5.0, -3.0):
            with self.subTest(pct=pct):
                self.assertAlmostEqual(
                    FU.fraction_to_pct_points(FU.pct_points_to_fraction(pct)), pct, places=12
                )

    def test_non_finite_input_is_rejected_explicitly(self):
        for bad in (None, "0.02", float("nan"), float("inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    FU.fraction_to_pct_points(bad)
                with self.assertRaises(ValueError):
                    FU.pct_points_to_fraction(bad)

    def test_booleans_are_not_treated_as_numbers(self):
        self.assertFalse(FU.is_finite_number(True))
        self.assertFalse(FU.is_finite_number(False))

    def test_is_fraction_like_bounds(self):
        self.assertTrue(FU.is_fraction_like(0.35))
        self.assertTrue(FU.is_fraction_like(-1.0))
        self.assertFalse(FU.is_fraction_like(2.0))
        self.assertFalse(FU.is_fraction_like(18.0))
        self.assertFalse(FU.is_fraction_like(float("nan")))


# --------------------------------------------------------------------------
# A. 因子生成
# --------------------------------------------------------------------------
class PriceFactorGenerationTests(unittest.TestCase):
    """``compute_price_factors`` 产出 fraction，不是 percentage points。"""

    def setUp(self):
        klines = {
            "TST901": _kline_frame(10.0 * 1.02),   # +2%
            "TST902": _kline_frame(10.0 * 0.97),   # -3%
        }
        self.factors = F.compute_price_factors(klines)
        self.assertFalse(self.factors.empty, "确定性 K 线应当产出因子行")

    def test_two_percent_return_is_fraction_not_percentage_points(self):
        mom5 = float(self.factors.loc["TST901", "mom5"])
        self.assertAlmostEqual(mom5, 0.02, places=9)
        self.assertNotAlmostEqual(mom5, 2.0, places=6)

    def test_negative_return_stays_a_negative_fraction(self):
        mom5 = float(self.factors.loc["TST902", "mom5"])
        self.assertLess(mom5, 0.0)
        self.assertAlmostEqual(mom5, -0.03, places=9)

    def test_all_momentum_columns_are_fraction_sized(self):
        row = self.factors.loc["TST901"]
        for column in ("mom5", "mom20", "mom60", "rev5"):
            with self.subTest(column=column):
                value = float(row[column])
                self.assertTrue(
                    FU.is_fraction_like(value),
                    "%s 应当是 fraction 量级，实际 %r" % (column, value),
                )

    def test_rev5_is_a_drawdown_from_the_five_day_high(self):
        # 最后一根是区间最高价 → 回撤深度恰好为 0；且永远 <= 0。
        self.assertAlmostEqual(float(self.factors.loc["TST901", "rev5"]), 0.0, places=9)
        self.assertLessEqual(float(self.factors.loc["TST902", "rev5"]), 0.0)


# --------------------------------------------------------------------------
# B. 热门启动段：+18% 过热分量
# --------------------------------------------------------------------------
class HotLeaderOverheatTests(unittest.TestCase):
    """``_hot_leader_profile`` 的 mom5/mom20 过热分量必须按 fraction 触发。"""

    def _overheat(self, mom5, mom20=0.0, pct=1.0, price=10.0, ma20=10.0):
        table = _table([_base_row(mom5_raw=mom5, mom20_raw=mom20, pct=pct,
                                  price=price, ma20=ma20)])
        return float(S._hot_leader_profile(table)["overheat"].iloc[0])

    def test_eighteen_percent_mom5_triggers_the_momentum_component(self):
        # 0.30 恰好是 mom5 分量的权重（其余分量在此夹具下均为 0）。
        self.assertAlmostEqual(self._overheat(0.18), 0.30, places=9)

    def test_eight_percent_mom5_does_not_trigger_it(self):
        self.assertAlmostEqual(self._overheat(0.08), 0.0, places=9)

    def test_component_difference_is_attributable_to_momentum_only(self):
        # 同一夹具下仅 mom5 跨越 18%，差值必须精确等于该分量权重。
        self.assertAlmostEqual(self._overheat(0.18) - self._overheat(0.08), 0.30, places=9)

    def test_thirty_five_percent_mom20_triggers_the_momentum_component(self):
        self.assertAlmostEqual(self._overheat(0.0, mom20=0.35), 0.25, places=9)
        self.assertAlmostEqual(self._overheat(0.0, mom20=0.30), 0.0, places=9)


# --------------------------------------------------------------------------
# C. 底部反转：1%~10% 短转带 与 15% 过热
# --------------------------------------------------------------------------
class BottomReversalMomentumTests(unittest.TestCase):
    """``_bottom_reversal_profile`` 的动量带/过热必须按 fraction 触发。"""

    def _profile(self, rows):
        return S._bottom_reversal_profile(_table(rows))

    def test_three_percent_enters_the_one_to_ten_percent_band(self):
        # 控制变量：只改目标行的 mom5，其余行逐字节相同 → 排名类分量不变。
        in_band = self._profile([
            _base_row(mom5_raw=0.30, mom20_raw=-0.02),
            _base_row(mom5_raw=0.03, mom20_raw=-0.02),   # +3%，落在 1%~10%
            _base_row(mom5_raw=0.10, mom20_raw=-0.02),
            _base_row(mom5_raw=0.00, mom20_raw=-0.02),
        ])
        out_of_band = self._profile([
            _base_row(mom5_raw=0.30, mom20_raw=-0.02),
            _base_row(mom5_raw=0.005, mom20_raw=-0.02),  # +0.5%，带外
            _base_row(mom5_raw=0.10, mom20_raw=-0.02),
            _base_row(mom5_raw=0.00, mom20_raw=-0.02),
        ])
        delta = float(in_band["score"].loc["T1"]) - float(out_of_band["score"].loc["T1"])
        self.assertAlmostEqual(delta, 0.25 * 0.28, places=9)
        # 两端都不得被 clip 到边界，否则上面的差值会失真。
        self.assertGreater(float(in_band["score"].loc["T1"]), 0.0)
        self.assertLess(float(in_band["score"].loc["T1"]), 1.0)

    def test_fifteen_percent_overheat_component_uses_fraction(self):
        profile = self._profile([
            _base_row(mom5_raw=0.15, mom20_raw=-0.02),
            _base_row(mom5_raw=0.14, mom20_raw=-0.02),
        ])
        self.assertAlmostEqual(float(profile["overheat"].loc["T0"]), 0.30, places=9)
        self.assertAlmostEqual(float(profile["overheat"].loc["T1"]), 0.0, places=9)

    def test_thirty_five_percent_mom20_overheat_component_uses_fraction(self):
        profile = self._profile([
            _base_row(mom5_raw=0.0, mom20_raw=0.35),
            _base_row(mom5_raw=0.0, mom20_raw=0.30),
        ])
        self.assertAlmostEqual(float(profile["overheat"].loc["T0"]), 0.25, places=9)
        self.assertAlmostEqual(float(profile["overheat"].loc["T1"]), 0.0, places=9)


# --------------------------------------------------------------------------
# D. sentiment_pioneer 独立强势路径：+2% 门槛
# --------------------------------------------------------------------------
class SentimentPioneerIndividualMomentumTests(unittest.TestCase):
    """``individual_mom5_min = 0.02`` 必须在 +2% 处放行、在 +1% 处拦截。"""

    def _run(self, mom5_raw):
        table = _table([_base_row(
            pct=5.0,                 # 落在 individual_pct_min/max = 3.5~8.5
            flow=1.0,                # >= individual_flow_min = 0.65
            vol_surge_raw=2.0,       # >= individual_vol_surge_min = 1.20
            sentiment=0.0,           # >= sentiment_min = -0.5（不触发板块门禁）
            mom5_raw=mom5_raw,
            mom20_raw=0.0,
        )])
        return S.run_strategy("sentiment_pioneer", table, topn=5, gate=None,
                              first_board_codes=None)

    def test_two_percent_satisfies_the_momentum_gate(self):
        result = self._run(0.02)
        self.assertEqual(len(result["picks"]), 1)
        self.assertEqual(result["picks"][0]["entry_path"], "individual_strong")

    def test_one_percent_does_not_satisfy_the_momentum_gate(self):
        result = self._run(0.01)
        self.assertEqual(len(result["picks"]), 1)
        self.assertNotEqual(result["picks"][0]["entry_path"], "individual_strong")

    def test_gate_difference_equals_the_configured_bonus(self):
        bonus = S.PAPER_CONDITION_DEFAULTS["sentiment_pioneer"]["individual_bonus"]
        delta = (self._run(0.02)["picks"][0]["score"]
                 - self._run(0.01)["picks"][0]["score"])
        self.assertAlmostEqual(delta, bonus, places=9)


# --------------------------------------------------------------------------
# E. 既有正确的 fraction 阈值必须保持不变
# --------------------------------------------------------------------------
class ExistingFractionThresholdTests(unittest.TestCase):
    """0.05 == 5%、0.15 == 15% 的既有规则不能被二次除以 100。"""

    def test_overheat_constants_are_fractions(self):
        self.assertAlmostEqual(S.MOM5_OVERHEAT_PCT, 0.05, places=12)
        self.assertAlmostEqual(S.MOM20_OVERHEAT_PCT, 0.15, places=12)
        self.assertAlmostEqual(S.SECTOR_CLIMAX_MOM5, 0.08, places=12)

    def test_overheat_mask_boundaries_are_five_and_fifteen_percent(self):
        table = _table([
            _base_row(mom5_raw=0.06, mom20_raw=0.10),   # +6%  → 超过 5%
            _base_row(mom5_raw=0.04, mom20_raw=0.10),   # +4%  → 未过热
            _base_row(mom5_raw=0.04, mom20_raw=0.16),   # +16% → 超过 15%
            _base_row(mom5_raw=0.04, mom20_raw=0.14),   # +14% → 未过热
        ])
        mask = [bool(v) for v in S._overheat_mask(table)]
        self.assertEqual(mask, [True, False, True, False])

    def test_overheat_penalty_is_applied_to_fraction_hot_rows_only(self):
        table = _table([
            _base_row(mom5_raw=0.06, mom20_raw=0.10),
            _base_row(mom5_raw=0.04, mom20_raw=0.10),
        ])
        score = pd.Series([0.8, 0.8], index=table.index)
        penalized = S._apply_overheat_penalty(score, table, 0.35)
        self.assertAlmostEqual(float(penalized.iloc[0]), 0.45, places=9)
        self.assertAlmostEqual(float(penalized.iloc[1]), 0.80, places=9)


# --------------------------------------------------------------------------
# 窄范围回归守卫：禁止 mom*_raw 与 percentage-point 字面量直接比较
# --------------------------------------------------------------------------
class MomentumUnitGuardTests(unittest.TestCase):
    """只守已知失败类：策略打分函数里与原始动量直接比较的数字必须是 fraction。

    刻意**不**做全仓正则，也不覆盖 ``clip``（那属于"边界是否该收紧"的
    独立问题，见 PR 的 Residual Risk），以免产生高误报的噪声守卫。
    """

    GUARDED_FUNCTIONS = ("_hot_leader_profile", "_bottom_reversal_profile")
    MOMENTUM_LOCALS = ("mom5", "mom20")
    COMPARISON_METHODS = ("ge", "gt", "le", "lt", "between")

    @classmethod
    def setUpClass(cls):
        with open(STRATEGIES_SRC, encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def _momentum_literals(self):
        """产出被守卫函数里"直接与原始动量比较"的数值字面量。"""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in self.GUARDED_FUNCTIONS:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not isinstance(func, ast.Attribute) or func.attr not in self.COMPARISON_METHODS:
                    continue
                if not isinstance(func.value, ast.Name) or func.value.id not in self.MOMENTUM_LOCALS:
                    continue
                for argument in call.args:
                    for literal in ast.walk(argument):
                        if (isinstance(literal, ast.Constant)
                                and isinstance(literal.value, (int, float))
                                and not isinstance(literal.value, bool)):
                            yield node.name, func.value.id, func.attr, literal.value

    def test_guard_actually_inspects_something(self):
        # 防止函数/局部变量改名后守卫退化成"永远绿灯"。
        found = list(self._momentum_literals())
        self.assertTrue(found, "守卫没有匹配到任何动量比较，说明函数名或局部变量名已变更")

    def test_momentum_comparisons_use_fraction_literals(self):
        offenders = [
            (func_name, variable, operator, value)
            for func_name, variable, operator, value in self._momentum_literals()
            if abs(value) > 1.0
        ]
        self.assertEqual(
            [], offenders,
            "以下比较把 percentage-point 字面量用在了 fraction 动量上：%s" % (offenders,),
        )

    def test_no_momentum_comparison_uses_clip(self):
        # clip 不在守卫范围内；此断言只记录事实，防止有人误以为 clip 也被覆盖。
        guarded_bodies = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name in self.GUARDED_FUNCTIONS
        ]
        clips = [
            call for body in guarded_bodies for call in ast.walk(body)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "clip"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in self.MOMENTUM_LOCALS
        ]
        self.assertEqual(len(clips), 2, "预期 _hot_leader_profile 里有 2 处动量 clip；请同步更新 Residual Risk")

    def test_condition_defaults_bound_to_raw_momentum_are_fractions(self):
        bound_fields = {
            ("sentiment_pioneer", "individual_mom5_min"): "mom5_raw",
            ("bottom_reversal", "mom20_min"): "mom20_raw",
            ("trend_continuation", "mom20_min"): "mom20_raw",
        }
        for (strategy_id, key), raw_field in bound_fields.items():
            with self.subTest(strategy=strategy_id, key=key):
                value = S.PAPER_CONDITION_DEFAULTS[strategy_id][key]
                self.assertTrue(
                    FU.is_fraction_like(value),
                    "%s.%s 与 %s 直接比较，必须是 fraction，实际 %r"
                    % (strategy_id, key, raw_field, value),
                )

    def test_any_momentum_min_max_default_is_fraction_like(self):
        import re
        pattern = re.compile(r"_mom(?:5|20|60)_(?:min|max)$")
        offenders = []
        for strategy_id, conditions in S.PAPER_CONDITION_DEFAULTS.items():
            for key, value in conditions.items():
                if pattern.search(key) and not FU.is_fraction_like(value):
                    offenders.append((strategy_id, key, value))
        self.assertEqual([], offenders, "动量阈值默认值必须是 fraction 量级：%s" % (offenders,))

    def test_inline_momentum_defaults_in_conditions_get_are_fractions(self):
        offenders = []
        for call in ast.walk(self.tree):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute) or func.attr != "get" or len(call.args) < 2:
                continue
            key = call.args[0]
            default = call.args[1]
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if not key.value.endswith(("_mom5_min", "_mom20_min", "_mom60_min")):
                continue
            if not isinstance(default, ast.Constant) or not isinstance(default.value, (int, float)):
                continue
            if not FU.is_fraction_like(default.value):
                offenders.append((key.value, default.value))
        self.assertEqual(
            [], offenders,
            "conditions.get(<动量阈值>, <默认值>) 的内联默认值必须是 fraction：%s" % (offenders,),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
