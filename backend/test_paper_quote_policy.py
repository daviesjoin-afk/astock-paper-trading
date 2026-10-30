# -*- coding: utf-8 -*-
import datetime as dt
import unittest

import paper_quote_policy as policy


class PaperQuotePolicyTests(unittest.TestCase):
    def test_historical_live_quote_is_fresh_when_source_day_matches(self):
        quote = {"quote_source": "live", "quote_at": "2026-09-03T10:00:00+08:00"}
        self.assertTrue(policy.quote_is_fresh(
            quote, dt.date(2026, 9, 3), date_fn=lambda value: value,
            today_fn=lambda: dt.date(2026, 9, 4),
        ))

    def test_inactive_quote_requires_all_activity_fields_to_be_zero(self):
        self.assertFalse(policy.is_trading_active(
            {"pct": 0, "amount": 0, "turnover": 0, "volume": 0}, num=lambda value, default=0: value if value is not None else default,
        ))
        self.assertTrue(policy.is_trading_active(
            {"pct": 0, "amount": 1, "turnover": 0, "volume": 0}, num=lambda value, default=0: value if value is not None else default,
        ))

    def test_cross_source_unavailable_only_allows_exit(self):
        quote = {
            "quote_source": "live", "quote_validation": "cross_source_unavailable",
            "quote_at": "2026-09-03T10:00:00+08:00",
        }
        fresh = lambda _quote, _day: True
        entry = policy.execution_quote_status(quote, "2026-09-03", quote_fresh=fresh)
        exit_status = policy.execution_quote_status(quote, "2026-09-03", purpose="exit", quote_fresh=fresh)
        self.assertFalse(entry["fresh"])
        self.assertTrue(exit_status["fresh"])
        self.assertTrue(exit_status["degraded"])

    def test_cross_source_checked_is_accepted(self):
        result = policy.execution_quote_status(
            {"quote_source": "live", "quote_validation": "cross_source_checked"},
            "2026-09-03", quote_fresh=lambda _quote, _day: True,
        )
        self.assertEqual(result["status"], "cross_source_checked")
        self.assertTrue(result["fresh"])

    def verdict(self, *, skew_seconds, primary_price=10.0, primary_pct=1.0,
                cross_price=10.0, cross_pct=1.0):
        base = dt.datetime(2026, 9, 3, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        return policy.cross_source_verdict(
            primary_price, primary_pct, base.isoformat(),
            cross_price, cross_pct,
            (base + dt.timedelta(seconds=skew_seconds)).isoformat(),
            expected_day=dt.date(2026, 9, 3),
        )

    def test_sixty_second_skew_relaxes_same_direction_price_gap(self):
        """60s 采样时滞 + 1.5% 价差 + 同向：按时滞放宽后通过，但必须标 degraded。"""
        result = self.verdict(skew_seconds=60, primary_price=10.15, primary_pct=1.2,
                              cross_price=10.0, cross_pct=1.1)
        self.assertEqual(result["validation"], "cross_source_checked")
        self.assertTrue(result["degraded"])
        self.assertTrue(result["relaxed"])
        self.assertEqual(result["time_skew_seconds"], 60.0)
        self.assertEqual(result["price_gap_pct"], 1.5)
        self.assertIn("60s 采样时滞放宽", result["reason"])

    def test_sixty_second_skew_with_opposite_direction_still_fails(self):
        """时滞放宽不得掩盖方向分歧：一涨一跌必须判 failed。"""
        result = self.verdict(skew_seconds=60, primary_price=10.0, primary_pct=1.2,
                              cross_price=10.0, cross_pct=-0.9)
        self.assertEqual(result["validation"], "cross_source_failed")
        self.assertFalse(result["degraded"])
        self.assertIn("方向不一致", result["reason"])

    def test_ten_second_skew_keeps_the_strict_tolerance(self):
        """10s 内属于同一采样窗口，1.5% 价差照旧超阈值——旧口径不放松。"""
        result = self.verdict(skew_seconds=10, primary_price=10.15, primary_pct=1.2,
                              cross_price=10.0, cross_pct=1.1)
        self.assertEqual(result["validation"], "cross_source_failed")
        self.assertFalse(result["relaxed"])
        self.assertLess(result["price_tolerance_pct"], 1.5)
        self.assertIn("双源差异超阈值", result["reason"])

    def test_skew_beyond_the_relax_window_keeps_the_old_verdict(self):
        """|Δt| > 90s 维持现状：不放宽、不新增语义。"""
        result = self.verdict(skew_seconds=150, primary_price=10.15, primary_pct=1.2,
                              cross_price=10.0, cross_pct=1.1)
        self.assertEqual(result["validation"], "cross_source_failed")
        self.assertFalse(result["relaxed"])
        self.assertEqual(result["price_tolerance_pct"], 0.5)

    def test_missing_cross_source_degrades_to_single_source_without_retry(self):
        base = dt.datetime(2026, 9, 3, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        result = policy.cross_source_verdict(
            10.0, 1.0, base.isoformat(), 0, 999, None, expected_day=dt.date(2026, 9, 3),
        )
        self.assertEqual(result["validation"], "range_timestamp_checked")
        self.assertEqual(result["reason"], "独立行情源未返回有效价格或时间")
        self.assertFalse(result["available"])

    # ---- 涨跌幅差容差必须与价差同步放宽（现网实测口径） ----
    def test_sixty_second_skew_also_relaxes_pct_gap(self):
        """时滞放宽必须同时作用于价差与涨跌幅差。

        现网实测（2026-10-30 15:05，603239 急跌中）：66s 时滞下价差 0.449%、
        幅差 0.026pp，两源一致；旧口径里价差已随时滞放宽，幅差却仍卡在固定
        0.20pp 阈值上 → 判 failed → 整笔委托被判 stale 而挂起。两项口径
        必须同步。
        """
        result = self.verdict(skew_seconds=60, primary_price=13.30, primary_pct=-9.30,
                              cross_price=13.36, cross_pct=-9.3261)
        self.assertEqual(result["validation"], "cross_source_checked")
        self.assertTrue(result["relaxed"])
        self.assertGreater(result["pct_tolerance"], 0.20)

    def test_pct_gap_relaxation_still_requires_same_direction(self):
        """涨跌幅差放宽不得掩盖方向分歧：一涨一跌必须判 failed。"""
        result = self.verdict(skew_seconds=66, primary_price=10.0, primary_pct=-5.0,
                              cross_price=10.2, cross_pct=2.0)
        self.assertEqual(result["validation"], "cross_source_failed")
        self.assertIn("方向不一致", result["reason"])

    def test_pct_gap_relaxation_does_not_apply_to_tight_skew(self):
        """短时滞内幅差照旧严格：0.5pp 超阈值仍判 failed。

        注意 |pct|≥3 时 scale=2.0 是**既有**语义（急涨急跌放大固有滞后差），
        所以固定容差是 0.20pp × 2 = 0.40pp，不是 0.20pp。
        """
        result = self.verdict(skew_seconds=10, primary_price=10.0, primary_pct=-5.0,
                              cross_price=10.15, cross_pct=-5.5)
        self.assertEqual(result["validation"], "cross_source_failed")
        self.assertFalse(result["relaxed"])
        self.assertEqual(result["pct_tolerance"], 0.40)


if __name__ == "__main__":
    unittest.main()
