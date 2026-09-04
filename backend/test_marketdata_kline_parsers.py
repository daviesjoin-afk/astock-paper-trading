# -*- coding: utf-8 -*-
import unittest

import marketdata_providers as providers


class MarketDataKlineParserTests(unittest.TestCase):
    def test_tencent_rows_estimate_amount_and_use_previous_close_for_amplitude(self):
        rows = providers.parse_tencent_kline_rows([
            ["2026-09-02", "10", "11", "12", "9", "100"],
            ["2026-09-03", "11", "10", "11", "9", "200"],
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], 105000.0)
        self.assertAlmostEqual(rows[1]["amplitude"], 18.181818, places=5)

    def test_sina_rows_filter_date_range_and_mark_unadjusted_source_upstream(self):
        payload = [
            {"day": "2026-09-02", "open": "10", "close": "11", "high": "12", "low": "9", "volume": "100"},
            {"day": "2026-09-04", "open": "11", "close": "12", "high": "13", "low": "10", "volume": "100"},
        ]
        rows = providers.parse_sina_kline_rows(payload, "20260901", "20260903")
        self.assertEqual([row["date"] for row in rows], ["2026-09-02"])
        self.assertEqual(rows[0]["amount"], 1050.0)

    def test_eastmoney_rows_parse_optional_amplitude(self):
        rows = providers.parse_eastmoney_kline_rows(["2026-09-03,10,11,12,9,100,105000,27.27"])
        self.assertEqual(rows[0]["date"], "2026-09-03")
        self.assertEqual(rows[0]["amount"], 105000.0)
        self.assertAlmostEqual(rows[0]["amplitude"], 27.27)


if __name__ == "__main__":
    unittest.main()
