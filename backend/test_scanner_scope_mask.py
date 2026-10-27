# -*- coding: utf-8 -*-
"""证券范围掩码：换成 `itertuples` 之后必须与逐行 `apply` 完全等价。

`main.scanner` 原来用 `DataFrame.apply(axis=1)` 给每一行判断"是否属于可买入
证券范围"。这一步为 5000 行 × 64 列的表构造 5000 个 Series（实测 46ms），而
同样的规则用 `itertuples` 只需 9.5ms。本文件锁定替换后的语义：**同一份规则、
同一个结果**，包括缺列、NaN 与各类受限标的。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import main as M
import paper_trading_rules as rules

_buy_scope = M._buy_scope


class SelectionScopeOwnershipTests(unittest.TestCase):
    def test_http_facade_reuses_selection_scope_owner_without_changing_rules(self):
        self.assertIs(M._buy_scope, rules.selection_buy_scope)
        cases = [
            (("600000", "普通股票", 0, "stock"), True),
            (("300750", "普通股票", "false", "stock"), True),
            (("1", "普通股票", 0, "stock"), False),
            (("600001", "ST股票", 0, "stock"), False),
            (("600001", "普通股票", "true", "stock"), False),
            (("600001", "普通股票", 0, "fund"), False),
            (("688001", "普通股票", 0, "stock"), False),
            (("830001", "普通股票", 0, "stock"), False),
        ]
        for args, allowed in cases:
            with self.subTest(args=args):
                self.assertEqual(rules.selection_buy_scope(*args)["allowed"], allowed)
        # Selection is stricter about malformed short codes than the legacy
        # paper ledger helper; moving ownership must not merge these policies.
        self.assertTrue(rules.security_scope("1")["allowed"])


def _reference_mask(table):
    """旧写法（逐行 Series），作为等价性基准。"""
    return table.apply(
        lambda row: _buy_scope(
            row.get("code"), row.get("name"), row.get("risk_flag"),
            row.get("instrument_type") or row.get("security_type"),
        )["allowed"],
        axis=1,
    )


def _table(**overrides):
    rows = [
        # code, name, risk_flag, instrument_type, security_type
        ("600000", "浦发银行", None, "stock", None),      # 主板普通股
        ("000001", "平安银行", 0, None, None),            # 列值缺失
        ("300750", "宁德时代", None, "stock", None),      # 创业板
        ("688111", "金山办公", None, "stock", None),      # 科创板 → 拒绝
        ("830001", "北交所票", None, "stock", None),      # 北交所 → 拒绝
        ("430001", "北交所票2", None, "stock", None),     # 北交所 → 拒绝
        ("600001", "ST 某某", None, "stock", None),       # ST → 拒绝
        ("600002", "退市某某", None, "stock", None),      # 退市 → 拒绝
        ("510300", "沪深300ETF", None, "etf", None),      # ETF → 拒绝
        ("600003", "某基金", None, None, "fund"),         # security_type 兜底 → 拒绝
        ("159915", None, None, None, None),               # 名称缺失
        ("abc", "坏代码", None, None, None),              # 代码格式无效
        (float("nan"), None, None, None, None),           # code 为 NaN
        ("601000", "唐山港", "1", "stock", None),          # risk_flag="1" → 拒绝
    ]
    data = {
        "code": [row[0] for row in rows],
        "name": [row[1] for row in rows],
        "risk_flag": [row[2] for row in rows],
        "instrument_type": [row[3] for row in rows],
        "security_type": [row[4] for row in rows],
    }
    data.update(overrides)
    return pd.DataFrame(data)


class ScopeMaskEquivalenceTests(unittest.TestCase):
    def test_mask_matches_the_row_wise_reference(self):
        table = _table()
        actual = M._permitted_scope_mask(table)
        expected = _reference_mask(table)
        pd.testing.assert_series_equal(actual, expected, check_names=False)
        self.assertEqual(int(actual.sum()), 3, "只有主板/创业板普通股可通过")

    def test_restricted_targets_are_rejected_by_the_shared_rule(self):
        table = _table()
        mask = M._permitted_scope_mask(table)
        rejected = set(table.loc[~mask, "code"].astype(str))
        for code in ("688111", "830001", "430001", "600001", "600002",
                     "159915", "510300", "600003", "601000"):
            self.assertIn(code, rejected, code)
        for code in ("600000", "000001", "300750"):
            self.assertNotIn(code, rejected, code)

    def test_missing_optional_columns_still_match_the_reference(self):
        for dropped in ("risk_flag", "instrument_type", "security_type",
                        ("risk_flag", "instrument_type")):
            table = _table()
            table = table.drop(columns=list(dropped) if isinstance(dropped, tuple) else [dropped])
            pd.testing.assert_series_equal(
                M._permitted_scope_mask(table), _reference_mask(table),
                check_names=False, obj=str(dropped),
            )

    def test_missing_code_column_rejects_everything(self):
        table = _table().drop(columns=["code"])
        mask = M._permitted_scope_mask(table)
        self.assertEqual(len(mask), len(table))
        self.assertFalse(bool(mask.any()), "没有 code 列时不得放行任何标的")

    def test_mask_keeps_the_frame_index(self):
        table = _table()
        shifted = table.set_index(pd.Index(range(100, 100 + len(table))))
        mask = M._permitted_scope_mask(shifted)
        self.assertTrue(mask.index.equals(shifted.index))
        self.assertEqual(int((~mask).sum()), (~_reference_mask(shifted)).sum())

    def test_numeric_and_nan_heavy_columns_do_not_change_the_outcome(self):
        rng = np.random.default_rng(3)
        table = _table()
        for position in range(48):
            table[f"aux_{position}"] = rng.standard_normal(len(table))
        table["pe"] = np.nan
        table["industry"] = "银行"
        pd.testing.assert_series_equal(
            M._permitted_scope_mask(table), _reference_mask(table), check_names=False,
        )

    def test_float_coerced_values_agree_with_the_reference(self):
        """``None`` 与整数混列会被 pandas 归一成 float：两种写法必须同样处理。

        共享规则按 ``str(value)`` 判定，``1.0`` 与 ``"1"`` 并不等价；这里锁定的是
        "掩码与旧写法逐行一致"，而不是改动共享规则本身。
        """
        table = pd.DataFrame({
            "code": ["600000", "601000", "600001"],
            "name": ["某行", "某行", "某行"],
            "risk_flag": [None, 1, 0],
            "instrument_type": ["stock"] * 3,
        })
        self.assertEqual(str(table["risk_flag"].dtype), "float64")
        pd.testing.assert_series_equal(
            M._permitted_scope_mask(table), _reference_mask(table), check_names=False,
        )


if __name__ == "__main__":
    unittest.main()
