# -*- coding: utf-8 -*-
"""``earnings_disclosure_facts`` 的回归。

四份正文夹具都取自**真实公告原文**（东方财富公告镜像的 ``notice_content``），
只做了截断，没有改写任何数字、措辞或行内空格。这一点是本测试文件的关键：

一个"业绩公告解析器"如果只用手写样例，很容易在真实版式上全线失败 —— 真实公告里
标签会被数值列打断、单位在表头声明、扣非与归母措辞只差几个字、增加额与净利润额
语法位置相同。下面每一条断言都对应一种**真实**错法，而不是想象的边界。

夹具来源（``art_code`` / 披露日 / 截断范围）：

* ``XUNFEI_FORECAST``   AN202607141826962804 / 2026-07-14 / 第 1–27 行（表格式预告，亏损区间）
* ``WPS_FORECAST``      AN202604131821157706 / 2026-04-13 / 第 1–47 行（叙述式预告，"增加额"陷阱）
* ``CMB_EXPRESS``       AN202601231818331868 / 2026-01-23 / 第 1–39 行（快报，单位百万元，含更早的每股收益行）
* ``HIK_EXPRESS``       AN202601201818153891 / 2026-01-20 / 第 1–38 行（快报，单位元，含跨行折断的扣非标签）
"""
from __future__ import annotations

import hashlib
import unittest
from decimal import Decimal

import earnings_disclosure_facts as EDF

# 表格式业绩预告：标签被数值列打断，"股东的净利润"在下一行。
XUNFEI_FORECAST = """
证券代码：002230      证券简称：科大讯飞      公告编号：2026-037
                    科大讯飞股份有限公司

                    2026 年半年度业绩预告

  本公司及董事会全体成员保证信息披露的内容真实、准确、完整，没有虚假记载、误导性陈述或重大遗漏。

    一、本期业绩预计情况

    （一）业绩预告期间

    2026 年 1 月 1 日至 2026 年 6 月 30 日

    （二）业绩预告情况

    预计净利润为负值

    项  目                          本报告期                            上年同期

归属于上市公司 亏损：-22,800 万元～-18,000 万元            亏损：-23,892.72 万元
股东的净利润    比上年同期增长：5%～25%

扣除非经常性损 亏损：-67,500 万元～-60,000 万元            亏损：-36,416.98万元
益后的净利润    比上年同期下降：-85% ～-65%
"""

# 叙述式业绩预告：第一处"净利润"后面跟的是**增加额**，不是净利润额。
WPS_FORECAST = """
证券代码：688111          证券简称：金山办公        公告编号：2026-011
          北京金山办公软件股份有限公司

    关于 2026 年第一季度业绩预告的自愿性披露公告

  重要内容提示：

       经财务部门初步测算，北京金山办公软件股份有限公司（以下简称“公
司”）预计 2026 年第一季度实现营业收入与上年同期相比，将增加 26,341 万元到 36,021 万元，同比增长 20.24%到 27.68%。

       预计 2026 年第一季度实现归属于母公司所有者的净利润与上年同期
相比，将增加 161,884 万元到 190,451 万元，同比增长 401.89%到 472.81%。
       预计 2026 年第一季度实现归属于母公司所有者的扣除非经常性损益
的净利润与上年同期相比，将增加 160,960 万元到 189,213 万元，同比增长412.89%到 485.36%。

  一、本期业绩预告情况

  （一）业绩预告期间

  2026 年 1 月 1 日至 2026 年 3 月 31 日

  （二）业绩预告情况

  1. 经财务部门初步测算，预计 2026 年第一季报实现营业收入 156,488 万元
到 166,168 万元，与上年同期相比，将增加 26,341 万元到 36,021 万元，同比增
长 20.24%到 27.68%。

  2. 预计 2026 年第一季报实现归属于母公司所有者的净利润 202,165 万元到
230,732 万元，与上年同期相比，将增加 161,884 万元到 190,451 万元，同比增长
401.89%到 472.81%。

  3. 预计 2026 年第一季度实现归属于母公司所有者的扣除非经常性损益的
净利润 199,944 万元到 228,197 万元，与上年同期相比，将增加 160,960 万元到
189,213 万元，同比增长 412.89%到 485.36%。

  二、上年同期业绩情况

  （一）实现营业收入：130,146.93 万元。

  （二）归属于母公司所有者的净利润：40,280.75 万元。
"""

# 业绩快报：单位百万元；"归属于本行普通股股东的基本每股收益"比归母净利润行更早出现。
CMB_EXPRESS = """
  A 股简称：招商银行      A 股代码：600036      公告编号：2026-002
             招商银行股份有限公司

             2025 年度业绩快报公告

  本公告所载 2025 年度主要财务数据为招商银行股份有限公司（简称本公司或本行）初步核算的集团口径数据，未经会计师事务所审计，具体数据以本公司最终披露的 2025 年度报告财务数据为准，请投资者注意投资风险。

  一、主要财务数据和指标

                                            （除特别说明外，单位人民币百万元）

        主要财务数据          2025 年        2024 年      增减变动幅度

 营业收入                        337,532        337,488          0.01%

  其中：非利息净收入            121,939        126,211          -3.38%

 营业利润                        179,252        179,019          0.13%

 利润总额                        178,993        178,652          0.19%

 归属于本行股东的净利润          150,181        148,391          1.21%

 扣除非经常性损益后归属于本      150,005        148,011          1.35%

 行股东的净利润

 归属于本行普通股股东的基本          5.70          5.66          0.71%

 每股收益(人民币元) 1

 归属于本行普通股股东的加权        13.44          14.49    减少 1.05 个百分点
 平均净资产收益率（%）1
"""

# 业绩快报：单位元；扣非标签跨两行折断，不得因此把扣非数字当成归母净利润。
HIK_EXPRESS = """
  证券代码：002415        证券简称：海康威视      公告编号：2026-001 号

                杭州海康威视数字技术股份有限公司

                        2025 年度业绩快报

      特别提示：

      本公告所载 2025 年度的财务数据仅为初步核算数据，已经公司内部审计部
  门审计，未经会计师事务所审计，与年度报告中披露的最终数据可能存在差异，
  请投资者注意投资风险。

      一、2025 年度主要财务数据和指标

                                                                              单位：元

              项目                    本报告期            上年同期      增减变动幅度

          营业总收入              92,517,540,779.05    92,495,525,118.30        0.02%

            营业利润                16,978,423,252.44    14,312,246,696.51        18.63%

            利润总额                17,028,778,516.34    14,343,493,640.00        18.72%

  归属于上市公司股东的净利润      14,188,269,426.14    11,977,327,023.54        18.46%

 扣除非经常性损益后的归属于上市    13,696,015,459.25    11,814,861,641.16        15.92%
        公司股东的净利润

          基本每股收益                        1.545              1.297        19.12%
"""


# 业绩快报：标签被列宽折断成"净 / 利润"两行，且正文后半段有同名的叙述句。
ZGZM_EXPRESS = """
 证券代码：601888  证券简称：中国中免  公告编号：临2026-020
    中国旅游集团中免股份有限公司

      2026年半年度业绩快报公告

    一、2026年半年度主要财务数据和指标

                                                      单位：人民币万元

        项目              本报告期      上年同期    增减变动幅度（%）

    营业总收入            2,759,172.27    2,815,075.00              -1.99

      营业利润              374,154.25      370,798.85              0.90

      利润总额              374,454.96      366,347.92              2.21

归属于上市公司股东的净      310,648.47      259,975.29              19.49
        利润

归属于上市公司股东的扣      307,976.45      259,532.96              18.67
除非经常性损益的净利润

    二、经营业绩和财务状况情况说明

    报告期内，公司实现归属于上市公司股东的净利润 31.06 亿元，同比增长
 19.49%；主营业务毛利率持续改善，同比提升 0.73 个百分点。
"""

# 业绩快报：标签被折断成三行（"归属于母公司所有者的净利" / 空行 / "润  <数字>"）。
WPS_EXPRESS = """
 证券代码：688111  证券简称：金山办公  公告编号：临2026-007
          北京金山办公软件股份有限公司

            2025年度业绩快报公告

                                                                 单位：人民币元

          项目                本报告期        上年同期    增减变动幅度（％）

      营业总收入          5,928,744,673.06  5,120,838,798.82              15.78

        营业利润          1,950,114,443.42  1,741,049,069.31              12.01

        利润总额          1,956,179,787.05  1,750,422,726.33              11.75

 归属于母公司所有者的净利

          润              1,842,976,801.74  1,645,080,509.02              12.03

 归属于母公司所有者的扣除

  非经常性损益的净利润    1,807,693,940.69  1,556,338,879.94              16.15

  基本每股收益（元/股）              3.98              3.56              11.80
"""

# 真实公告（泛亚微透 2026 前三季度业绩预告，截断）：区间里的千分位是**全角逗号**，
# 同比写的是"下降"，且正文后段有「上年同期业绩情况」——三处都是取错数字的入口。
FORECAST_FULLWIDTH_AND_DECLINE = """
（1）经财务部门初步测算，预计 2026 年前三季度实现归属于母公司所有者的净利润为
4，081.97 万元到 4,989.08 万元，与上年同期相比（法定披露数据），
将减少 4,989.08 万元到 4,081.97 万元，同比下降 45.00%到 55.00%。

（2）预计 2026 年前三季度实现归属于母公司所有者的扣除非经常性损益后净利润为
3,861.97 万元至 4,769.08 万元，同比下降 46.36%到 56.56%。

二、上年同期业绩情况

归属于母公司所有者的净利润：9,071.05 万元。
归属于母公司所有者的扣除非经常性损益的净利润：8,890.44 万元。
"""

# 表格式业绩预告：区间用 en dash（–）分隔，同比用"88.80% - 98.97%"这种带空格连字符。
WULIANGYE_FORECAST = """
    一、本期业绩预计情况

  （一）业绩预告期间：2026 年 1 月 1 日至 2026 年 6 月 30 日

  （二）业绩预告情况：预计净利润为正值且属于同向上升 50%以上情形

    项  目                  本报告期                  上年同期

归属于上市公司  盈利：873,000 万元–920,000 万元

股东的净利润                                        盈利：462,385 万元
                比上年同期增长：88.80% - 98.97%

扣除非经常性损  盈利：846,000 万元–893,000 万元

益后的净利润                                        盈利：461,155 万元
                比上年同期增长：83.45% - 93.64%
"""

# 表格式业绩预告：区间用连字符分隔，金额单位被折到下一行（"万" / "元"）。
WANKE_FORECAST = """
    一、本期业绩预计情况

    （一）业绩预告期间：2026 年 1 月 1 日至 2026 年 6 月 30 日

    （二）业绩预告情况：预计净利润为负值

    项 目                      本报告期                        上年同期

归属于上市公司股  亏损：1,200,000.00 万元-1,500,000.00 万  亏损：1,194,657.37 万元
东的净利润        元

扣除非经常性损益  亏损：1,100,000.00 万元-1,400,000.00 万  亏损：1,159,102.32 万元
后的净利润        元

基本每股收益      亏损：1.0058 元/股-1.2573 元/股        亏损：1.0075 元/股
"""

# 披露对象是控股子公司，数字不属于上市公司本体（真实公告，已截断）。
CITICS_SUBSIDIARY_BODY = """
 证券代码：600030          证券简称：中信证券      公告编号：临2026-063
            中信证券股份有限公司

    关于华夏基金2026年半年度业绩快报的公告

  公司控股子公司华夏基金管理有限公司（以下简称华夏基金）的股东 Mackenzie
Financial Corporation 持有华夏基金 27.8% 股权。

  截至2026年6月30日，华夏基金总资产人民币230.93亿元，总负债人民币77.28亿元；
2026年上半年实现营业收入人民币57.08亿元，净利润人民币14.13亿元。
"""


def _claim(**overrides) -> EDF.EarningsClaim:
    base = dict(
        kind=EDF.ANNOUNCEMENT_KIND_FORECAST,
        report_period="2026-06-30",
        metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
        low_wan=Decimal("-22800"),
        high_wan=Decimal("-18000"),
        yoy_low_pct=Decimal("5"),
        yoy_high_pct=Decimal("25"),
    )
    base.update(overrides)
    return EDF.EarningsClaim(**base)


class ClassificationTest(unittest.TestCase):
    def test_real_titles_are_classified_with_period(self):
        cases = (
            ("科大讯飞:2026年半年度业绩预告", EDF.ANNOUNCEMENT_KIND_FORECAST, "2026-06-30"),
            ("招商银行:招商银行股份有限公司2025年度业绩快报公告", EDF.ANNOUNCEMENT_KIND_EXPRESS,
             "2025-12-31"),
            ("金山办公:金山办公关于2026年第一季度业绩预告的自愿性披露公告",
             EDF.ANNOUNCEMENT_KIND_FORECAST, "2026-03-31"),
            ("海康威视:2025年度业绩快报", EDF.ANNOUNCEMENT_KIND_EXPRESS, "2025-12-31"),
            ("某公司:2026年前三季度业绩预告", EDF.ANNOUNCEMENT_KIND_FORECAST, "2026-09-30"),
        )
        for title, kind, period in cases:
            with self.subTest(title=title):
                meta = EDF.classify_announcement(title)
                self.assertTrue(meta.supported, meta)
                self.assertEqual(meta.kind, kind)
                self.assertEqual(meta.report_period, period)
                self.assertFalse(meta.is_correction)

    def test_correction_announcement_is_flagged(self):
        meta = EDF.classify_announcement("某公司:2025年度业绩预告修正公告")
        self.assertTrue(meta.supported)
        self.assertTrue(meta.is_correction)
        self.assertEqual(meta.report_period, "2025-12-31")
        self.assertEqual(EDF.classify_announcement("某公司:2026年一季度业绩快报更正公告").kind,
                         EDF.ANNOUNCEMENT_KIND_EXPRESS)

    def test_english_mirror_is_not_a_second_event(self):
        meta = EDF.classify_announcement("海康威视:2025年度业绩快报(英文版)")
        self.assertFalse(meta.supported)
        self.assertEqual(meta.unsupported_reason, EDF.UNSUPPORTED_ENGLISH_MIRROR)
        self.assertTrue(meta.english_mirror)
        self.assertIsNone(meta.report_period)

    def test_earnings_briefing_is_not_a_forecast(self):
        meta = EDF.classify_announcement(
            "贵州茅台:贵州茅台关于召开2026年半年度业绩说明会的公告")
        self.assertFalse(meta.supported)
        self.assertEqual(meta.unsupported_reason, EDF.UNSUPPORTED_NOT_EARNINGS)
        self.assertIsNone(meta.kind)

    def test_missing_period_fails_closed_instead_of_guessing(self):
        meta = EDF.classify_announcement("某公司:业绩预告")
        self.assertFalse(meta.supported)
        self.assertEqual(meta.unsupported_reason, EDF.UNSUPPORTED_MISSING_PERIOD)

    def test_subsidiary_earnings_report_is_not_the_listed_company_fact(self):
        # 中信证券披露的是控股子公司华夏基金的业绩快报。
        meta = EDF.classify_announcement(
            "中信证券:中信证券股份有限公司关于华夏基金2026年半年度业绩快报的公告")
        self.assertFalse(meta.supported)
        self.assertEqual(meta.unsupported_reason, EDF.UNSUPPORTED_THIRD_PARTY_SUBJECT)

    def test_own_company_wording_is_not_mistaken_for_a_subsidiary(self):
        meta = EDF.classify_announcement("某公司:某公司关于公司2026年半年度业绩快报的公告")
        self.assertTrue(meta.supported, meta)
        self.assertEqual(meta.report_period, "2026-06-30")

    def test_empty_title_is_unsupported(self):
        self.assertEqual(EDF.classify_announcement("").unsupported_reason,
                         EDF.UNSUPPORTED_NOT_EARNINGS)
        self.assertEqual(EDF.classify_announcement(None).unsupported_reason,
                         EDF.UNSUPPORTED_NOT_EARNINGS)


class ForecastParsingTest(unittest.TestCase):
    def test_table_layout_recovers_the_loss_range(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, XUNFEI_FORECAST)
        self.assertEqual(figures.report_period, "2026-06-30")
        self.assertEqual(figures.unit, "万元")
        self.assertEqual(figures.low_wan, Decimal("-22800"))
        self.assertEqual(figures.high_wan, Decimal("-18000"))
        self.assertEqual(figures.yoy_low_pct, Decimal("5"))
        self.assertEqual(figures.yoy_high_pct, Decimal("25"))

    def test_table_layout_does_not_take_the_deducted_row(self):
        # 扣非行是 -67,500～-60,000；若取错，区间会整体偏到那里。
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, XUNFEI_FORECAST)
        self.assertNotEqual(figures.low_wan, Decimal("-67500"))

    def test_narrative_layout_prefers_level_over_increase_amount(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, WPS_FORECAST)
        self.assertEqual(figures.report_period, "2026-03-31")
        self.assertEqual(figures.low_wan, Decimal("202165"))
        self.assertEqual(figures.high_wan, Decimal("230732"))
        self.assertEqual(figures.yoy_low_pct, Decimal("401.89"))
        self.assertEqual(figures.yoy_high_pct, Decimal("472.81"))
        # 161,884 是"将增加"的金额，不是净利润额 —— 这是最容易犯的抽取错误。
        self.assertNotEqual(figures.low_wan, Decimal("161884"))

    def test_hyphen_range_and_split_unit_are_recovered(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, WANKE_FORECAST)
        self.assertEqual(figures.low_wan, Decimal("1200000.00"))
        self.assertEqual(figures.high_wan, Decimal("1500000.00"))
        self.assertEqual(figures.unit, "万元")
        # 上一期亏损 1,194,657.37 万元紧跟在区间后面，不得被当成区间上界。
        self.assertNotEqual(figures.high_wan, Decimal("1194657.37"))

    def test_full_width_thousands_separator_is_a_number(self):
        # "4，081.97 万元"（全角逗号）必须被当成数字。只认半角时第一个锚点匹配失败，
        # 解析器会继续往后找，最终取到「上年同期业绩情况」里的 9,071.05 万元。
        figures = EDF.parse_reported_figures(
            EDF.ANNOUNCEMENT_KIND_FORECAST, FORECAST_FULLWIDTH_AND_DECLINE)
        self.assertEqual(figures.low_wan, Decimal("4081.97"))
        self.assertEqual(figures.high_wan, Decimal("4989.08"))
        self.assertNotEqual(figures.low_wan, Decimal("9071.05"))

    def test_decline_wording_becomes_a_signed_ascending_interval(self):
        # "同比下降 45.00%到 55.00%" 的真实区间是 [-55%, -45%]。
        # 按字面取正数会让一个声称"同比 +45%"的错误 claim 通过核验。
        figures = EDF.parse_reported_figures(
            EDF.ANNOUNCEMENT_KIND_FORECAST, FORECAST_FULLWIDTH_AND_DECLINE)
        self.assertEqual(figures.yoy_low_pct, Decimal("-55.00"))
        self.assertEqual(figures.yoy_high_pct, Decimal("-45.00"))
        self.assertLessEqual(figures.yoy_low_pct, figures.yoy_high_pct)

    def test_number_after_anchor_with_unknown_shape_fails_closed(self):
        # 第一个锚点后面确实是数字，只是单位写法没被认出。此时继续往后找，
        # 下一个"归母净利润"会落在上年同期那一段 —— 必须拒绝，不能静默换期间。
        body = ("归属于母公司所有者的净利润 4,000 到 5,000\n"
                "二、上年同期业绩情况\n"
                "归属于母公司所有者的净利润：9,071.05 万元。\n")
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, body)
        self.assertEqual(ctx.exception.reason, "unparsable_forecast")

    def test_en_dash_range_is_not_collapsed_to_a_single_value(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, WULIANGYE_FORECAST)
        self.assertEqual(figures.low_wan, Decimal("873000"))
        self.assertEqual(figures.high_wan, Decimal("920000"))
        self.assertEqual(figures.yoy_low_pct, Decimal("88.80"))
        self.assertEqual(figures.yoy_high_pct, Decimal("98.97"))

    def test_unsupported_separator_fails_closed_instead_of_taking_the_lower_bound(self):
        # 假设出现一个本模块不认识的区间写法：必须 unparsable，而不是读成单点值。
        body = "归属于上市公司股东的净利润 盈利：1,000 万元※2,000 万元\n"
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, body)
        self.assertEqual(ctx.exception.reason, "unparsable_forecast")

    def test_hyphen_after_unit_does_not_make_a_negative_range_ambiguous(self):
        body = ("归属于上市公司股东的净利润 亏损：-2,000 万元～-1,000 万元\n"
                "比上年同期下降：-85% ～-65%\n")
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, body)
        self.assertEqual(figures.low_wan, Decimal("-2000"))
        self.assertEqual(figures.high_wan, Decimal("-1000"))
        self.assertEqual(figures.yoy_low_pct, Decimal("-85"))
        self.assertEqual(figures.yoy_high_pct, Decimal("-65"))

    def test_deducted_label_appearing_first_does_not_win(self):
        # 扣非标签完整出现在归母标签之前（快报里两种顺序都有）。若不移除"扣除"锚点，
        # 会先命中扣非那一行，把 1,000~2,000 当成归母净利润。
        body = ("归属于上市公司股东的净利润\n"
                "扣除非经常性损益后的归属于上市公司股东的净利润 1,000 万元到 2,000 万元\n"
                "归属于上市公司股东的净利润 3,000 万元到 4,000 万元\n")
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, body)
        self.assertEqual(figures.low_wan, Decimal("3000"))
        self.assertEqual(figures.high_wan, Decimal("4000"))

    def test_missing_body_is_a_distinct_reason(self):
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, "   ")
        self.assertEqual(ctx.exception.reason, "body_unavailable")

    def test_unknown_shape_is_unparsable_not_guessed(self):
        body = "本公司预计本期业绩与上年同期相比发生较大变动，具体情况请见定期报告。"
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, body)
        self.assertEqual(ctx.exception.reason, "unparsable_forecast")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures("flash", XUNFEI_FORECAST)
        self.assertEqual(ctx.exception.reason, "unknown_kind")

    def test_body_fingerprint_binds_to_the_exact_text(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, XUNFEI_FORECAST)
        self.assertEqual(figures.body_sha256,
                         hashlib.sha256(XUNFEI_FORECAST.encode("utf-8")).hexdigest())
        other = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_FORECAST, WPS_FORECAST)
        self.assertNotEqual(figures.body_sha256, other.body_sha256)


class ExpressParsingTest(unittest.TestCase):
    def test_million_yuan_unit_is_normalised_to_ten_thousand(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, CMB_EXPRESS)
        self.assertEqual(figures.report_period, "2025-12-31")
        self.assertEqual(figures.unit, "百万元")
        self.assertEqual(figures.low_wan, Decimal("15018100"))
        self.assertEqual(figures.high_wan, Decimal("15018100"))
        self.assertEqual(figures.yoy_low_pct, Decimal("1.21"))

    def test_earnings_per_share_line_is_not_taken_as_net_profit(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, CMB_EXPRESS)
        # "归属于本行普通股股东的基本每股收益" 是 5.70，位置在正文里也很醒目。
        self.assertNotEqual(figures.low_wan, Decimal("570"))

    def test_yuan_unit_is_normalised_and_deducted_row_is_skipped(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, HIK_EXPRESS)
        self.assertEqual(figures.unit, "元")
        self.assertEqual(figures.low_wan, Decimal("1418826.942614"))
        self.assertEqual(figures.yoy_low_pct, Decimal("18.46"))
        # 扣非是 13,696,015,459.25 元；跨行折断的标签不得让它命中。
        self.assertNotEqual(figures.low_wan, Decimal("1369601.545925"))

    def test_express_without_unit_declaration_fails_closed(self):
        body = ("主要财务数据 2025 年 2024 年 增减变动幅度\n"
                "归属于上市公司股东的净利润 150,181 148,391 1.21%\n")
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, body)
        self.assertEqual(ctx.exception.reason, "unknown_unit")

    def test_express_body_with_only_eps_row_is_unparsable(self):
        body = ("单位：元\n"
                "归属于上市公司普通股股东的基本每股收益 1.545 1.297 19.12%\n")
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, body)
        self.assertEqual(ctx.exception.reason, "unparsable_express")

    def test_mid_line_narrative_with_bare_number_is_not_a_table_row(self):
        # 这一条**只**由"锚点必须行首"挡住：数字是裸的（不带单位），碰不到单位守卫。
        # 少了行首约束，"报告期内，公司实现…净利润 310,648.47"就会被当成表格单元格。
        body = ("单位：元\n"
                "报告期内，公司实现归属于上市公司股东的净利润 310,648.47，同比增长 19.49%。\n")
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, body)
        self.assertEqual(ctx.exception.reason, "unparsable_express")

    def test_line_start_amount_with_unit_is_a_narrative_not_a_cell(self):
        # 这一条**只**由"单元格不得自带单位"挡住：锚点在行首，行首约束放它过去。
        body = ("单位：元\n"
                "归属于上市公司股东的净利润 31.06 亿元\n")
        with self.assertRaises(EDF.EarningsFactError) as ctx:
            EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, body)
        self.assertEqual(ctx.exception.reason, "unparsable_express")

    def test_label_split_across_lines_is_recovered(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, ZGZM_EXPRESS)
        self.assertEqual(figures.low_wan, Decimal("310648.47"))
        self.assertEqual(figures.unit, "万元")

    def test_narrative_amount_is_not_taken_as_a_table_cell(self):
        # 正文后半段有"实现归属于上市公司股东的净利润 31.06 亿元"。
        # 若拿它当表格单元格并乘表头单位（万元），会得到 31.06 —— 错四个数量级。
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, ZGZM_EXPRESS)
        self.assertNotEqual(figures.low_wan, Decimal("31.06"))
        self.assertEqual(figures.low_wan, Decimal("310648.47"))

    def test_three_line_label_fragment_is_recovered(self):
        figures = EDF.parse_reported_figures(EDF.ANNOUNCEMENT_KIND_EXPRESS, WPS_EXPRESS)
        self.assertEqual(figures.low_wan, Decimal("184297.680174"))
        # 这张表的"增减变动幅度"列是裸数字（表头声明了 ％），单元格里没有百分号。
        # 本模块**不**猜它是百分比：宁可少核一个字段，也不凭空认定列语义。
        self.assertIsNone(figures.yoy_low_pct)
        # 扣非是 1,807,693,940.69 元；跨三行折断的标签不得让它命中。
        self.assertNotEqual(figures.low_wan, Decimal("180769.394069"))


class ClaimVerificationTest(unittest.TestCase):
    def setUp(self):
        self.meta = EDF.classify_announcement("科大讯飞:2026年半年度业绩预告")

    def test_matching_claim_is_verified(self):
        result = EDF.verify_earnings_claim(_claim(), self.meta, XUNFEI_FORECAST)
        self.assertTrue(result.is_verified)
        self.assertEqual(result.status, EDF.STATUS_VERIFIED)
        self.assertEqual(result.disagreements, ())
        self.assertIsNotNone(result.figures)
        self.assertEqual(result.body_sha256,
                         hashlib.sha256(XUNFEI_FORECAST.encode("utf-8")).hexdigest())

    def test_single_digit_error_is_caught(self):
        result = EDF.verify_earnings_claim(
            _claim(high_wan=Decimal("-17500")), self.meta, XUNFEI_FORECAST)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_FIGURE_MISMATCH)
        self.assertEqual(result.disagreements, ("high_wan",))

    def test_lost_sign_is_caught_on_every_affected_field(self):
        # AI 常见的读法：把"亏损 -22,800"读成"亏损 22,800"（丢符号但仍为负号表述）。
        result = EDF.verify_earnings_claim(
            _claim(low_wan=Decimal("-22800"), high_wan=Decimal("18000")),
            self.meta, XUNFEI_FORECAST)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_FIGURE_MISMATCH)
        self.assertIn("high_wan", result.disagreements)

    def test_tolerance_boundary_is_declared_not_incidental(self):
        # 相对容差 0.1%：22,800 的 0.1% 是 22.8 万元。
        within = EDF.verify_earnings_claim(
            _claim(low_wan=Decimal("-22820")), self.meta, XUNFEI_FORECAST)
        self.assertTrue(within.is_verified, within.status)
        outside = EDF.verify_earnings_claim(
            _claim(low_wan=Decimal("-22830")), self.meta, XUNFEI_FORECAST)
        self.assertEqual(outside.status, EDF.STATUS_CLAIM_FIGURE_MISMATCH)
        self.assertEqual(outside.disagreements, ("low_wan",))

    def test_superseded_wins_over_matching_numbers(self):
        result = EDF.verify_earnings_claim(
            _claim(), self.meta, XUNFEI_FORECAST, superseded_by="AN202610010000000001")
        self.assertEqual(result.status, EDF.STATUS_SUPERSEDED)
        self.assertFalse(result.is_verified)

    def test_superseded_wins_over_missing_body_too(self):
        # 顺序是语义顺序：过期事实不必先证明原文在不在。
        result = EDF.verify_earnings_claim(
            _claim(), self.meta, "", superseded_by="AN202610010000000001")
        self.assertEqual(result.status, EDF.STATUS_SUPERSEDED)

    def test_missing_body_is_not_a_verification_failure(self):
        result = EDF.verify_earnings_claim(_claim(), self.meta, "")
        self.assertEqual(result.status, EDF.STATUS_BODY_UNAVAILABLE)
        self.assertFalse(result.is_verified)
        self.assertIsNone(result.figures)

    def test_unparsable_body_is_reported_as_such(self):
        body = "本公司预计本期业绩与上年同期相比发生较大变动。"
        result = EDF.verify_earnings_claim(_claim(), self.meta, body)
        self.assertEqual(result.status, EDF.STATUS_BODY_UNPARSABLE)

    def test_kind_mismatch_is_caught_before_numbers(self):
        result = EDF.verify_earnings_claim(
            _claim(kind=EDF.ANNOUNCEMENT_KIND_EXPRESS), self.meta, XUNFEI_FORECAST)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_KIND_MISMATCH)

    def test_body_period_disagreeing_with_title_fails_closed(self):
        # 标题说 2026 半年度，正文写的却是 2025 年 1–6 月：这已经不是"数字对不对"，
        # 而是比对了错的期间 —— 必须报 period_mismatch，而不是拿 2025 的数字判 verified。
        stale = ("业绩预告期间\n2025 年 1 月 1 日至 2025 年 6 月 30 日\n"
                 "归属于上市公司股东的净利润 10,000 万元到 20,000 万元\n")
        result = EDF.verify_earnings_claim(
            _claim(low_wan=Decimal("10000"), high_wan=Decimal("20000"),
                   yoy_low_pct=None, yoy_high_pct=None),
            self.meta, stale)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_PERIOD_MISMATCH)

    def test_period_mismatch_is_caught_before_numbers(self):
        result = EDF.verify_earnings_claim(
            _claim(report_period="2026-09-30"), self.meta, XUNFEI_FORECAST)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_PERIOD_MISMATCH)

    def test_unsupported_metric_is_rejected(self):
        result = EDF.verify_earnings_claim(
            _claim(metric="net_profit_deducted"), self.meta, XUNFEI_FORECAST)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_METRIC_UNSUPPORTED)

    def test_unsupported_announcement_never_reaches_number_comparison(self):
        meta = EDF.classify_announcement("海康威视:2025年度业绩快报(英文版)")
        result = EDF.verify_earnings_claim(_claim(), meta, HIK_EXPRESS)
        self.assertEqual(result.status, EDF.STATUS_ANNOUNCEMENT_UNSUPPORTED)
        self.assertIsNone(result.figures)

    def test_subsidiary_numbers_never_become_a_verified_company_fact(self):
        meta = EDF.classify_announcement(
            "中信证券:中信证券股份有限公司关于华夏基金2026年半年度业绩快报的公告")
        claim = EDF.EarningsClaim(
            kind=EDF.ANNOUNCEMENT_KIND_EXPRESS, report_period="2026-06-30",
            metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
            low_wan=Decimal("141300"), high_wan=Decimal("141300"))
        result = EDF.verify_earnings_claim(claim, meta, CITICS_SUBSIDIARY_BODY)
        self.assertEqual(result.status, EDF.STATUS_ANNOUNCEMENT_UNSUPPORTED)
        self.assertFalse(result.is_verified)

    def test_claimed_yoy_missing_from_body_is_a_disagreement(self):
        body = "业绩预告期间\n2026 年 1 月 1 日至 2026 年 6 月 30 日\n归属于上市公司股东的净利润 10,000 万元到 20,000 万元\n"
        result = EDF.verify_earnings_claim(
            _claim(low_wan=Decimal("10000"), high_wan=Decimal("20000")),
            self.meta, body)
        self.assertEqual(result.status, EDF.STATUS_CLAIM_FIGURE_MISMATCH)
        self.assertEqual(result.disagreements, ("yoy_low_pct", "yoy_high_pct"))

    def test_claim_without_yoy_does_not_fail_on_absent_yoy(self):
        body = "业绩预告期间\n2026 年 1 月 1 日至 2026 年 6 月 30 日\n归属于上市公司股东的净利润 10,000 万元到 20,000 万元\n"
        result = EDF.verify_earnings_claim(
            _claim(low_wan=Decimal("10000"), high_wan=Decimal("20000"),
                   yoy_low_pct=None, yoy_high_pct=None),
            self.meta, body)
        self.assertTrue(result.is_verified, result.status)

    def test_express_claim_round_trip(self):
        meta = EDF.classify_announcement("招商银行:2025年度业绩快报公告")
        claim = EDF.EarningsClaim(
            kind=EDF.ANNOUNCEMENT_KIND_EXPRESS, report_period="2025-12-31",
            metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
            low_wan=Decimal("15018100"), high_wan=Decimal("15018100"),
            yoy_low_pct=Decimal("1.21"), yoy_high_pct=Decimal("1.21"))
        result = EDF.verify_earnings_claim(claim, meta, CMB_EXPRESS)
        self.assertTrue(result.is_verified, result.status)

    def test_wrong_typed_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            EDF.verify_earnings_claim({"kind": "forecast"}, self.meta, XUNFEI_FORECAST)
        with self.assertRaises(TypeError):
            EDF.verify_earnings_claim(_claim(), {"kind": "forecast"}, XUNFEI_FORECAST)


class ForgeryResistanceTest(unittest.TestCase):
    def test_parsed_figures_cannot_be_constructed_by_callers(self):
        with self.assertRaises(TypeError):
            EDF.ReportedFigures(
                version=EDF.EARNINGS_FACT_CONTRACT_VERSION,
                kind=EDF.ANNOUNCEMENT_KIND_FORECAST, metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
                report_period="2026-06-30", low_wan=Decimal("1"), high_wan=Decimal("2"),
                yoy_low_pct=None, yoy_high_pct=None, unit="万元", body_sha256="deadbeef")

    def test_verification_result_cannot_be_constructed_by_callers(self):
        with self.assertRaises(TypeError):
            EDF.ClaimVerification(
                version=EDF.EARNINGS_FACT_CONTRACT_VERSION, status=EDF.STATUS_VERIFIED,
                disagreements=(), figures=None, body_sha256="deadbeef")

    def test_verified_with_disagreements_is_structurally_impossible(self):
        issue = EDF._issue_claim_verification
        with self.assertRaises(ValueError):
            issue(version=EDF.EARNINGS_FACT_CONTRACT_VERSION, status=EDF.STATUS_VERIFIED,
                  disagreements=("low_wan",), figures=None, body_sha256="deadbeef")

    def test_figure_mismatch_must_name_the_field(self):
        issue = EDF._issue_claim_verification
        with self.assertRaises(ValueError):
            issue(version=EDF.EARNINGS_FACT_CONTRACT_VERSION,
                  status=EDF.STATUS_CLAIM_FIGURE_MISMATCH,
                  disagreements=(), figures=None, body_sha256="deadbeef")

    def test_unknown_status_is_rejected(self):
        issue = EDF._issue_claim_verification
        with self.assertRaises(ValueError):
            issue(version=EDF.EARNINGS_FACT_CONTRACT_VERSION, status="probably_fine",
                  disagreements=(), figures=None, body_sha256="deadbeef")

    def test_claim_rejects_floats(self):
        with self.assertRaises(TypeError):
            EDF.EarningsClaim(
                kind=EDF.ANNOUNCEMENT_KIND_FORECAST, report_period="2026-06-30",
                metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
                low_wan=-22800.0, high_wan=Decimal("-18000"))

    def test_claim_rejects_inverted_range(self):
        with self.assertRaises(ValueError):
            EDF.EarningsClaim(
                kind=EDF.ANNOUNCEMENT_KIND_FORECAST, report_period="2026-06-30",
                metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
                low_wan=Decimal("200"), high_wan=Decimal("100"))


if __name__ == "__main__":
    unittest.main()
