# -*- coding: utf-8 -*-
"""进化中心的"下跌防线"必须与执行引擎使用同一组阈值。

`adaptive_risk.DOWNSIDE_BASE` 曾是执行策略表的一份手写副本：趋势/板块的
预警线比真实策略宽 0.3 个百分点，而且漏了 `reported_profit_breakout`，
导致"风险 / 执行边界"页面显示的是另一套阈值。这里把两处钉死。
"""
import unittest

import adaptive_risk as AR
import strategy_policies as SPOL


class DownsideDefaultsMatchEngineTests(unittest.TestCase):
    def test_every_configured_strategy_matches_the_execution_policy(self):
        for account_id, defaults in AR.DOWNSIDE_BASE.items():
            policy = SPOL.intraday_downside_policy(account_id)
            with self.subTest(account_id=account_id):
                self.assertTrue(policy, f"{account_id} has no execution downside policy")
                self.assertAlmostEqual(defaults["downside_warning_pct"], policy["warning_pct"], places=6)
                self.assertAlmostEqual(defaults["downside_partial_pct"], policy["partial_pct"], places=6)
                self.assertAlmostEqual(defaults["downside_full_pct"], policy["full_pct"], places=6)
                self.assertAlmostEqual(defaults["downside_relative_pct"], policy["relative_pct"], places=6)
                self.assertAlmostEqual(defaults["downside_peak_retrace_pct"],
                                       policy["peak_retrace_pct"], places=6)
                self.assertAlmostEqual(defaults["downside_partial_ratio"],
                                       policy["partial_ratio"], places=6)

    def test_active_quality_strategy_is_not_missing(self):
        """reported_profit_breakout（三日策略）是当前三套参与者之一。"""
        self.assertIn("reported_profit_breakout", AR.DOWNSIDE_BASE)
        self.assertIn("reported_profit_breakout", SPOL.INTRADAY_DOWNSIDE_POLICIES)


if __name__ == "__main__":
    unittest.main()
