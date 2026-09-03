# -*- coding: utf-8 -*-
import unittest

import marketdata_providers as providers


class MarketDataProviderParserTests(unittest.TestCase):
    def test_tencent_parser_uses_timestamp_and_allowed_codes(self):
        parts = [""] * 33
        parts[1] = "平安银行"
        parts[3] = "10.5"
        parts[4] = "10"
        parts[30] = "20260903100000"
        parts[32] = "5"
        text = 'v_sh000001="' + "~".join(parts) + '";v_sh000002="bad";'
        rows = providers.parse_tencent_realtime_text(text, attempt=2, allowed_codes=["000001"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(rows[0]["quote_at"], "20260903100000")
        self.assertEqual(rows[0]["attempt"], 2)

    def test_sina_parser_locates_board_specific_date_and_time(self):
        values = ["平安银行", "0", "10", "10.5", "10.6", "9.9", "", "", "2026-09-03", "10:00:00", "extra"]
        text = 'var hq_str_sz000001="' + ",".join(values) + '";'
        rows = providers.parse_sina_realtime_text(text, allowed_codes=["000001"])
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(rows[0]["quote_at"], "2026-09-03T10:00:00+08:00")
        self.assertAlmostEqual(rows[0]["pct"], 5.0, places=4)

    def test_parsers_ignore_empty_or_malformed_rows(self):
        self.assertEqual(providers.parse_tencent_realtime_text(""), [])
        self.assertEqual(providers.parse_sina_realtime_text('var hq_str_sz000001="a,b";'), [])


if __name__ == "__main__":
    unittest.main()
