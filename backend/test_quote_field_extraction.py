# -*- coding: utf-8 -*-
"""价格-only 回退也要给出真实的成交额/换手率，而不是整列 unknown。

背景：东财 clist 返回 0 行时走腾讯/新浪价格-only 回退，旧解析器只取价格与
涨跌幅，于是选股因子表的 ``amount`` / ``turnover`` 整列缺失，每轮打
``factor_columns_missing`` 告警。但腾讯/新浪报文本来就带这些字段。

这些用例用**固定报文 fixture**（取自真实响应的字段布局）钉住三件事：

1. 能取到就必须取到（volume/amount/turnover 落到行里，单位对齐东财 clist）；
2. 取不到或自相矛盾就**保持缺失**——不写零、不写猜测值（"未知不是零"）；
3. 报文错位（例如把成交额当成交量）必须被交叉校验挡下来。

第 3 条是对抗性的核心：公开报文格式改过版，盲信下标会把错误数字喂进因子表，
而错误的成交额不会报错，只会静默改变排序。
"""
import unittest

import marketdata_providers as M

# 真实 sh600000 响应的字段布局（88 段），这里只填用例关心的位置。
def _tencent_line(*, price="9.83", prev_close="9.62", pct="2.18",
                  volume_lots="461758", amount_wan="44897", turnover="0.14",
                  quote_at="20261027132005", code="600000", name="浦发银行"):
    parts = [""] * 88
    parts[0] = "1"
    parts[1] = name
    parts[2] = code
    parts[3] = price
    parts[4] = prev_close
    parts[5] = "9.56"
    parts[6] = volume_lots
    parts[30] = quote_at
    parts[32] = pct
    parts[37] = amount_wan
    parts[38] = turnover
    return f'v_sh{code}="' + "~".join(parts) + '";'


def _sina_line(*, price="9.830", prev_close="9.620", volume_shares="45797793",
               amount_yuan="445251713.000", code="600000", name="浦发银行"):
    values = [""] * 34
    values[0] = name
    values[1] = "9.560"
    values[2] = prev_close
    values[3] = price
    values[4] = "9.900"
    values[5] = "9.500"
    values[8] = volume_shares
    values[9] = amount_yuan
    values[30] = "2026-10-27"
    values[31] = "13:20:05"
    return f'var hq_str_sh{code}="' + ",".join(values) + '";'


class TencentFieldExtractionTests(unittest.TestCase):
    def _row(self, **kwargs):
        rows = M.parse_tencent_realtime_text(_tencent_line(**kwargs))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_volume_amount_turnover_are_extracted(self):
        row = self._row()
        self.assertAlmostEqual(row["volume"], 461758.0)
        # [37] 是万元，落到行里统一成元（与东财 clist 的 f6 同口径）。
        self.assertAlmostEqual(row["amount"], 448970000.0)
        self.assertAlmostEqual(row["turnover"], 0.14)

    def test_units_match_the_primary_feed(self):
        """amount 用元、turnover 用百分数，才能和 clist 的 f6 / f8 直接拼表。"""
        row = self._row()
        implied = row["volume"] * 100.0 * row["price"]
        # amount 与 volume×100×price 必须同量级（1e8 元级，不是 1e4 或 1e12）。
        self.assertLess(abs(implied - row["amount"]) / row["amount"], 0.6)

    def test_misaligned_amount_is_rejected_not_written(self):
        """成交额放错量纲（例如误把"元"当"万元"）时必须判为不可用。"""
        # 44897 元 vs volume×100×price ≈ 4.5e8 元：差 4 个数量级。
        row = self._row(amount_wan="4.4897")
        self.assertNotIn("amount", row)
        # 其它字段不受牵连。
        self.assertAlmostEqual(row["volume"], 461758.0)

    def test_absurd_turnover_is_rejected(self):
        row = self._row(turnover="12345")
        self.assertNotIn("turnover", row)

    def test_missing_fields_stay_missing(self):
        row = self._row(volume_lots="", amount_wan="", turnover="")
        self.assertNotIn("volume", row)
        self.assertNotIn("amount", row)
        self.assertNotIn("turnover", row)
        # 价格与涨跌幅仍必须可用：不能因为补字段把主路径弄坏。
        self.assertAlmostEqual(row["price"], 9.83)

    def test_zero_volume_keeps_amount_unverified_but_usable(self):
        """停牌/无成交时 volume=0，成交额只能按"正值即接受"处理。"""
        row = self._row(volume_lots="0", amount_wan="5000")
        self.assertIn("amount", row)
        self.assertAlmostEqual(row["amount"], 50000000.0)

    def test_quote_at_and_pct_unchanged(self):
        row = self._row()
        self.assertEqual(row["quote_at"], "20261027132005")
        self.assertAlmostEqual(row["pct"], 2.18)
        self.assertEqual(row["source"], "tencent_public_quote")


class SinaFieldExtractionTests(unittest.TestCase):
    def _row(self, **kwargs):
        rows = M.parse_sina_realtime_text(_sina_line(**kwargs))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_volume_and_amount_are_extracted(self):
        row = self._row()
        # 新浪成交量是"股"，行里统一成"手"（对齐腾讯口径）。
        self.assertAlmostEqual(row["volume"], 457977.93, places=2)
        self.assertAlmostEqual(row["amount"], 445251713.0, places=1)

    def test_misaligned_amount_is_rejected(self):
        row = self._row(amount_yuan="1000")
        self.assertNotIn("amount", row)

    def test_missing_values_stay_missing(self):
        row = self._row(volume_shares="", amount_yuan="")
        self.assertNotIn("volume", row)
        self.assertNotIn("amount", row)
        self.assertAlmostEqual(row["price"], 9.83)

    def test_turnover_is_not_invented(self):
        """新浪报文没有换手率；不得用别的字段冒充。"""
        row = self._row()
        self.assertNotIn("turnover", row)


class SourceAgreementTests(unittest.TestCase):
    """同一时刻两个源给的成交额应落在同一量级，否则说明有一侧字段读错了。"""

    def test_tencent_and_sina_agree(self):
        tencent = M.parse_tencent_realtime_text(_tencent_line())[0]
        sina = M.parse_sina_realtime_text(_sina_line())[0]
        drift = abs(tencent["amount"] - sina["amount"]) / max(sina["amount"], 1.0)
        self.assertLess(drift, 0.05)
        volume_drift = abs(tencent["volume"] - sina["volume"]) / max(sina["volume"], 1.0)
        self.assertLess(volume_drift, 0.05)


if __name__ == "__main__":
    unittest.main()
