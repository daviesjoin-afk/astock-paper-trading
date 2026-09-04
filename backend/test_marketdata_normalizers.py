# -*- coding: utf-8 -*-
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import marketdata_normalizers as normalizers


class MarketdataNormalizerTests(unittest.TestCase):
    def test_finite_number_rejects_non_finite_values(self):
        self.assertEqual(normalizers.finite_number("12.5"), 12.5)
        self.assertIsNone(normalizers.finite_number(float("nan")))
        self.assertIsNone(normalizers.finite_number(float("inf")))

    def test_security_id_mapping_keeps_bj_920_special_case(self):
        self.assertEqual(normalizers.stock_secid("600000"), "1.600000")
        self.assertIsNone(normalizers.stock_secid("bad"))
        self.assertEqual(normalizers.secid("920001"), "0.920001")
        self.assertEqual(normalizers.secid("000001"), "0.000001")

    def test_sanitize_market_row_clears_impossible_ohlc(self):
        row = {"price": 10, "open_price": 100, "high": 8, "low": 12, "prev_close": 10}
        self.assertIs(normalizers.sanitize_market_row(row), row)
        self.assertIsNone(row["open_price"])
        self.assertIsNone(row["high"])
        self.assertIsNone(row["low"])

    def test_realtime_row_supports_injected_timestamp_converter(self):
        row = normalizers.realtime_row_from_ulist(
            {"f12": "000001", "f14": "平安银行", "f2": 10.0, "f124": 123},
            quote_at_fn=lambda value: f"ts:{value}",
        )
        self.assertEqual(row["code"], "000001")
        self.assertEqual(row["quote_at"], "ts:123")
        self.assertIsNone(normalizers.realtime_row_from_ulist(None))

    def test_kline_frame_deduplicates_and_sorts_dates(self):
        frame = normalizers.kline_frame([
            {"date": "2026-09-03", "close": 3},
            {"date": "2026-09-01", "close": 1},
            {"date": "2026-09-03", "close": 4},
        ])
        self.assertEqual(list(frame.index), list(pd.to_datetime(["2026-09-01", "2026-09-03"])))
        self.assertEqual(frame.loc["2026-09-03", "close"], 4)


if __name__ == "__main__":
    unittest.main()
