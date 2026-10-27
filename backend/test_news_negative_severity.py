# -*- coding: utf-8 -*-
"""负面公告严重度分级：重事件仍拦，常规事项只压额度。

这个模块的存在本身就是对抗性约束。原实现把"一条已核验负面"一律判为禁止新增
仓位，负面关键词表里"股东减持/股权质押/业绩下滑"与"立案调查/退市风险"同级，
一次常规减持公告就能把该股彻底冻住。分级后必须同时证明两件事：

1. **重事件没有被放宽**：severe 档仍然 `new_entry_allowed=False`、`risk_scale=0`；
2. **放开的是额度不是门禁**：serious/minor 只压 scale，市场红灯、未核验负面、
   数据源过期这些既有约束全部照旧生效。

任何后续想把 severe 降到 serious 的改动都会在这里失败——这是有意的。
"""
import datetime as dt
import unittest

import news_risk_policy as NRP
import paper_trading as PT

GREEN = {"light": "green"}


def _row(code, keywords, *, verified=True, tone=-1, summary="", minutes_ago=1):
    observed = dt.datetime.now() - dt.timedelta(minutes=minutes_ago)
    return {
        "code": code,
        "name": code,
        "tone": tone,
        "keywords": list(keywords),
        "summary": summary or ("、".join(keywords) + " 相关事项"),
        "time": observed.strftime("%Y%m%d%H%M%S"),
        "verified": verified,
        "event_type": "公司公告" if verified else "快讯关键词",
    }


class NegativeSeverityTests(unittest.TestCase):
    def setUp(self):
        # 让"扫描过期"这条既有降级不干扰本组断言。
        self._meta = dict(PT._NEWS_SCAN_META)
        PT._NEWS_SCAN_META = {"observed_at": None, "stale": False, "error": None}

    def tearDown(self):
        PT._NEWS_SCAN_META = self._meta

    def _risk(self, news, code="600000", market=GREEN):
        return PT._dynamic_news_risk(news, code, market=market)

    def test_severe_announcement_still_blocks_new_positions(self):
        for word in NRP.NEGATIVE_SEVERE:
            with self.subTest(word=word):
                result = self._risk([_row("600000", [word])])
                self.assertFalse(result["new_entry_allowed"], word)
                self.assertEqual(result["risk_scale"], 0.0, word)
                self.assertIn("禁止该股新增仓位", result["reason"])

    def test_serious_announcement_reduces_but_allows(self):
        result = self._risk([_row("600000", ["处罚"])])
        self.assertTrue(result["new_entry_allowed"])
        self.assertAlmostEqual(result["risk_scale"], 0.30)

    def test_minor_announcement_reduces_less_than_serious(self):
        minor = self._risk([_row("600000", ["减持"])])
        serious = self._risk([_row("600000", ["处罚"])])
        self.assertTrue(minor["new_entry_allowed"])
        self.assertAlmostEqual(minor["risk_scale"], 0.60)
        # 分级必须单调：轻事项的额度不得低于重事项。
        self.assertGreater(minor["risk_scale"], serious["risk_scale"])

    def test_mixed_rows_take_the_severe_tier(self):
        """同批里既有轻条目又有重条目时，重条目不能被稀释。"""
        result = self._risk([
            _row("600000", ["减持"]),
            _row("600000", ["立案"]),
        ])
        self.assertFalse(result["new_entry_allowed"])
        self.assertEqual(result["risk_scale"], 0.0)

    def test_severity_is_read_from_summary_too(self):
        """关键词没进 keywords 但出现在正文时同样要识别。"""
        result = self._risk([_row("600000", [], summary="公司收到证监会立案告知书")])
        self.assertFalse(result["new_entry_allowed"])

    def test_unverified_negative_is_unchanged(self):
        result = self._risk([_row("600000", ["立案"], verified=False)])
        self.assertTrue(result["new_entry_allowed"])
        self.assertAlmostEqual(result["risk_scale"], 0.50)

    def test_no_negative_is_normal(self):
        result = self._risk([])
        self.assertTrue(result["new_entry_allowed"])
        self.assertAlmostEqual(result["risk_scale"], 1.0)
        self.assertIn("未命中", result["reason"])

    def test_market_red_still_vetoes_even_a_minor_item(self):
        result = self._risk([_row("600000", ["减持"])], market={"light": "red"})
        self.assertFalse(result["new_entry_allowed"])
        self.assertEqual(result["risk_scale"], 0.0)
        self.assertIn("市场暂停", result["mode"] or result["reason"])

    def test_market_unknown_still_vetoes(self):
        result = self._risk([_row("600000", ["减持"])], market={"light": "unknown"})
        self.assertFalse(result["new_entry_allowed"])

    def test_stale_source_still_degrades_unverified_cases(self):
        PT._NEWS_SCAN_META = {"observed_at": None, "stale": True, "error": None}
        result = self._risk([_row("600000", ["立案"], verified=False)])
        self.assertAlmostEqual(result["risk_scale"], 0.50 * 0.75)

    def test_stale_source_does_not_downgrade_verified_severe(self):
        """已核验的重事件本来就已经否决；过期不该把它"解释"成更轻。"""
        PT._NEWS_SCAN_META = {"observed_at": None, "stale": True, "error": None}
        result = self._risk([_row("600000", ["立案"])])
        self.assertFalse(result["new_entry_allowed"])
        self.assertEqual(result["risk_scale"], 0.0)

    def test_other_codes_do_not_leak(self):
        result = self._risk([_row("600519", ["立案"])], code="600000")
        self.assertTrue(result["new_entry_allowed"])
        self.assertAlmostEqual(result["risk_scale"], 1.0)


class SeverityVocabularyTests(unittest.TestCase):
    def test_tiers_are_disjoint(self):
        severe = set(NRP.NEGATIVE_SEVERE)
        serious = set(NRP.NEGATIVE_SERIOUS)
        minor = set(NRP.NEGATIVE_MINOR)
        self.assertFalse(severe & serious)
        self.assertFalse(severe & minor)
        self.assertFalse(serious & minor)

    def test_severe_vocabulary_keeps_the_dangerous_words(self):
        """这些词一旦被移出 severe 档，等于放宽真实的重大风险。"""
        for word in ("立案", "调查", "退市"):
            self.assertIn(word, NRP.NEGATIVE_SEVERE, word)


class RunupBonusTests(unittest.TestCase):
    """追高帽的时段余量：只按时段单调收窄，午后那一档必须小于早盘。"""

    def _bonus(self, hour, minute):
        return PT._open_runup_bonus(dt.datetime(2026, 10, 27, hour, minute))

    def test_morning_window_is_the_widest(self):
        self.assertAlmostEqual(self._bonus(9, 35), 0.025)
        self.assertAlmostEqual(self._bonus(10, 0), 0.010)

    def test_afternoon_window_is_smaller_than_morning(self):
        afternoon = self._bonus(13, 30)
        self.assertAlmostEqual(afternoon, 0.005)
        self.assertLess(afternoon, self._bonus(10, 0))

    def test_no_bonus_outside_windows(self):
        for hour, minute in ((10, 45), (12, 0), (14, 45), (15, 0)):
            with self.subTest(hour=hour, minute=minute):
                self.assertAlmostEqual(self._bonus(hour, minute), 0.0)


if __name__ == "__main__":
    unittest.main()
