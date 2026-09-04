# -*- coding: utf-8 -*-
import os
import sys
import unittest
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_performance as performance


def _num(value, default=0.0):
    return float(value) if value is not None else default


def _date(value):
    return dt.date.fromisoformat(str(value)[:10])


class PaperPerformanceTests(unittest.TestCase):
    def test_quote_is_usable_requires_allowed_source_and_matching_day(self):
        quote = {"quote_source": "live", "quote_at": "2026-09-02T10:00:00+08:00"}
        self.assertTrue(performance.quote_is_usable(quote, "2026-09-02", date_fn=_date))
        self.assertFalse(performance.quote_is_usable({**quote, "quote_source": "local_cache"}, "2026-09-02", date_fn=_date))

    def test_position_performance_separates_today_and_carried_cost(self):
        result = performance.position_performance(
            {"qty": 200, "today_acquired_qty": 100, "today_acquired_cost": 1000},
            12.0,
            {"quote_source": "live", "quote_at": "2026-09-02T10:00:00+08:00", "pct": 2.0, "previous_close": 11.0},
            "2026-09-02",
            date_fn=_date,
            market_session=lambda: {"today_pnl_available": True},
            num=_num,
        )
        self.assertEqual(result, (300.0, 14.29, 2100.0))

    def test_sell_performance_reports_missing_quote_codes(self):
        result = performance.sell_performance(
            [{"code": "000001", "qty": 100, "filled_price": 11.0, "fees": 1.0}, {"code": "000002", "qty": 100, "filled_price": 9.0, "fees": 1.0}],
            {"000001": {"quote_source": "live", "quote_at": "2026-09-02T10:00:00+08:00", "price": 11.5, "pct": 1.0, "previous_close": 11.0}},
            "2026-09-02",
            date_fn=_date,
            num=_num,
        )
        self.assertEqual(result["covered"], 1)
        self.assertEqual(result["missing_codes"], ["000002"])


if __name__ == "__main__":
    unittest.main()
