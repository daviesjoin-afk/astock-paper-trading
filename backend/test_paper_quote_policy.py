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


if __name__ == "__main__":
    unittest.main()
