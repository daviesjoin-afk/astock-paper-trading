# -*- coding: utf-8 -*-
import unittest

import marketdata_providers as providers


class MarketDataProviderParserTests(unittest.TestCase):
    def test_eastmoney_clist_reports_missing_page(self):
        health = {}

        def fake_get_json(_url, params, **_kwargs):
            page = int(params["pn"])
            if page == 1:
                return {"data": {"total": 6, "diff": [{"f12": "000001"}, {"f12": "000002"}]}}
            if page == 2:
                return {"data": {"total": 6, "diff": [{"f12": "000003"}, {"f12": "000004"}]}}
            return {"data": {"total": 6, "diff": []}}

        result = providers.fetch_eastmoney_clist(
            get_json=fake_get_json,
            hosts=["test-host"],
            host_health=health,
            fields="f12",
            pages=None,
            pz=2,
            return_meta=True,
            ut="test-ut",
            fs="test-fs",
        )
        self.assertEqual(result["pages_expected"], 3)
        self.assertEqual(result["pages_ok"], 2)
        self.assertEqual(result["failed_pages"], [3])
        self.assertFalse(result["complete"])

    def test_concept_members_marks_capped_pages_incomplete(self):
        def fake_get_json(_url, _params, **_kwargs):
            return {"data": {"total": 101, "diff": [{
                "f12": "000001", "f14": "平安银行", "f2": "10.5", "f3": "1.2",
                "f124": "20260903100000",
            }]}}

        result = providers.fetch_eastmoney_concept_members(
            get_json=fake_get_json,
            hosts=["test-host"],
            board_code="BK0001",
            ut="test-ut",
            quote_at=lambda value: f"quote:{value}",
            finite_number=lambda value: float(value) if value is not None else None,
            max_pages=1,
        )
        self.assertEqual(result["members"][0]["code"], "000001")
        self.assertEqual(result["members"][0]["quote_at"], "quote:20260903100000")
        self.assertEqual(result["pages_expected"], 2)
        self.assertFalse(result["complete"])

    def test_concept_ref_parser_filters_non_theme_and_duplicate_boards(self):
        refs = providers.parse_eastmoney_concept_refs(
            {
                "one": {"f12": "BK0001", "f14": "液冷服务器"},
                "duplicate": {"f12": "BK0001", "f14": "液冷服务器"},
                "excluded": {"f12": "BK0002", "f14": "沪股通"},
                "non_board": {"f12": "000001", "f14": "平安银行"},
            },
            excluded_tokens=("沪股通",),
        )
        self.assertEqual(refs, [{"code": "BK0001", "name": "液冷服务器"}])

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
