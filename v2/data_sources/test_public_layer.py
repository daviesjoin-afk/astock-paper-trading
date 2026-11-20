#!/usr/bin/env python3
"""Offline tests for the public data layer.

The layer's whole job is to never let a "today-shaped" fact wear a past date, and
to say which source answered. Both are tested here without touching the network.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import closing, contextmanager
import io
import json
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from public_layer import (  # noqa: E402
    baostock_symbol, ensure_schema, fetch_bars, industry_as_of,
    store_bars, store_industry, _as_x10000,
)


class TestBaostockSymbol(unittest.TestCase):
    def test_shanghai_and_shenzhen_map_by_suffix(self):
        self.assertEqual(baostock_symbol("600519.SH"), "sh.600519")
        self.assertEqual(baostock_symbol("000001.SZ"), "sz.000001")

    def test_beijing_is_refused_before_a_session_opens(self):
        # BaoStock answers 10004011 for Beijing codes. Catching it here means the
        # caller gets a reason instead of an empty frame that looks like "no data".
        with self.assertRaises(ValueError):
            baostock_symbol("430198.BJ")


class TestScaling(unittest.TestCase):
    def test_a_blank_field_is_absent_not_zero(self):
        # A loss-making quarter reports a blank PE. Recording 0 would claim a
        # valuation the source never asserted.
        self.assertIsNone(_as_x10000(""))
        self.assertIsNone(_as_x10000("-"))

    def test_a_real_value_is_scaled_to_four_decimals(self):
        self.assertEqual(_as_x10000("21.9856"), 219_856)

    def test_text_that_is_not_a_number_is_absent(self):
        self.assertIsNone(_as_x10000("n/a"))


class TestIndustryAsOf(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)
        store_industry(self.conn, [
            {"symbol": "000001.SZ", "start_day": "1991-04-03", "industry_code": "440101",
             "l1_code": "440000", "l2_code": "440100", "source": "test"},
            {"symbol": "000001.SZ", "start_day": "2014-02-21", "industry_code": "480101",
             "l1_code": "480000", "l2_code": "480100", "source": "test"},
            {"symbol": "000001.SZ", "start_day": "2021-07-30", "industry_code": "480301",
             "l1_code": "480000", "l2_code": "480300", "source": "test"},
        ])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_a_past_date_gets_the_classification_in_force_then(self):
        # This is the difference between honest and look-ahead: the same symbol
        # belonged to three different industries over the backtest period.
        self.assertEqual(industry_as_of(self.conn, "000001.SZ", "2013-01-01")["industry_code"],
                         "440101")
        self.assertEqual(industry_as_of(self.conn, "000001.SZ", "2016-01-01")["industry_code"],
                         "480101")
        self.assertEqual(industry_as_of(self.conn, "000001.SZ", "2024-01-01")["industry_code"],
                         "480301")

    def test_the_change_day_itself_uses_the_new_classification(self):
        self.assertEqual(industry_as_of(self.conn, "000001.SZ", "2014-02-21")["industry_code"],
                         "480101")

    def test_before_any_record_there_is_no_answer(self):
        # "Not yet classified" must not fall back to the earliest row.
        self.assertIsNone(industry_as_of(self.conn, "000001.SZ", "1990-01-01"))

    def test_an_unknown_symbol_has_no_answer(self):
        self.assertIsNone(industry_as_of(self.conn, "999999.SH", "2026-01-01"))


class TestStoreBars(unittest.TestCase):
    def test_writing_the_same_day_twice_does_not_duplicate(self):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        row = {"symbol": "600519.SH", "day": "2026-11-11", "name": "贵州茅台",
               "open_cents": 100, "high_cents": 110, "low_cents": 90,
               "close_cents": 105, "prev_close_cents": 100, "volume_shares": 1000,
               "amount_cents": 100_000, "source": "tdx"}
        store_bars(conn, [row])
        store_bars(conn, [row])
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM bar").fetchone()[0]
        self.assertEqual(count, 1, "an upsert must not duplicate a re-ingested day")
        conn.close()

    def test_a_reingest_replaces_the_values(self):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        row = {"symbol": "600519.SH", "day": "2026-11-11", "name": "",
               "open_cents": 100, "high_cents": 110, "low_cents": 90,
               "close_cents": 105, "prev_close_cents": 100, "volume_shares": 1000,
               "amount_cents": 100_000, "source": "tdx"}
        store_bars(conn, [row])
        store_bars(conn, [dict(row, close_cents=999)])
        conn.commit()
        close = conn.execute("SELECT close_cents FROM bar").fetchone()[0]
        self.assertEqual(close, 999, "a corrected re-ingest must win")
        conn.close()


class TestSourceAttribution(unittest.TestCase):
    def test_a_failed_primary_names_itself_rather_than_looking_empty(self):
        """A silent fallback is worse than a failure.

        The caller has to be able to tell "no trading that day" from "the
        primary source refused and nothing replaced it".
        """
        original = sys.modules["public_layer"].tdx_daily if "public_layer" in sys.modules else None
        import public_layer
        saved = public_layer.tdx_daily
        public_layer.tdx_daily = lambda day, timeout=0: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = fetch_bars("2026-11-11")
        finally:
            public_layer.tdx_daily = saved
        self.assertTrue(result["degraded"])
        self.assertIsNone(result["source"])
        self.assertIn("boom", result["primary_error"])
        self.assertEqual(result["rows"], [])


class TestCompatibilitySemantics(unittest.TestCase):
    @staticmethod
    def bar(**values):
        return dict({"symbol": "600519.SH", "day": "2026-11-11", "name": "fixture",
                     "open_cents": 1000, "high_cents": 1100, "low_cents": 900,
                     "close_cents": 1050, "prev_close_cents": 1000,
                     "volume_shares": 1200, "amount_cents": 1200000,
                     "source": "tdx", "adjust": "none", "tradable": True}, **values)

    def test_halted_tdx_row_stays_nontradable_after_ingest(self):
        import public_layer
        from ingest_market import Row
        item = Row(**{key: value for key, value in self.bar(tradable=False).items()
                      if key not in ("source", "adjust")})
        with patch("ingest_market.fetch_package", return_value=b"fixture"), \
             patch("ingest_market.parse_package", return_value=[item]) as parse:
            rows = public_layer.tdx_daily("2026-11-11")
        parse.assert_called_once_with(b"fixture", "20261111")
        with closing(sqlite3.connect(":memory:")) as conn:
            ensure_schema(conn)
            store_bars(conn, rows)
            self.assertEqual(conn.execute("SELECT tradable,adjust FROM bar").fetchone(),
                             (0, "none"))

    def test_adjusted_or_incomplete_batch_writes_nothing(self):
        # Invalid second row cannot leave the first row silently stored.
        for changes in ({"adjust": "qfq"}, {"amount_cents": None}, {"tradable": None}):
            with self.subTest(changes=changes), closing(sqlite3.connect(":memory:")) as conn:
                ensure_schema(conn)
                with self.assertRaises(ValueError):
                    store_bars(conn, [self.bar(), self.bar(symbol="000001.SZ", **changes)])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM bar").fetchone()[0], 0)

    def test_beijing_refusal_precedes_baostock_login(self):
        import public_layer
        with patch.object(public_layer, "_bs_session") as session:
            with self.assertRaises(ValueError):
                public_layer.fetch_valuation("430198.BJ", "2026-01-01", "2026-11-11")
        session.assert_not_called()

    def test_bad_market_suffix_is_not_rerouted_to_shanghai(self):
        from source_http import SourceHttpError
        with self.assertRaises(SourceHttpError):
            baostock_symbol("000001.SH")

    def test_nonfinite_valuation_is_unknown(self):
        for value in ("nan", "inf", "-inf", True):
            with self.subTest(value=value):
                self.assertIsNone(_as_x10000(value))

    def test_tencent_raw_and_star_volume_and_unknown_fields(self):
        import public_layer
        from source_http import SourceHttp
        for symbol, key, volume in (("600519.SH", "sh600519", 1200),
                                    ("688981.SH", "sh688981", 12)):
            calls = []
            body = json.dumps({"data": {key: {"day": [
                ["2026-11-11", "10.00", "10.50", "11.00", "9.50", "12"]]}}}).encode()
            def opener(request, timeout):
                calls.append(request.full_url)
                return io.BytesIO(body)
            with self.subTest(symbol=symbol):
                rows = public_layer.tencent_daily(symbol, http=SourceHttp(opener=opener))
                self.assertEqual(rows[0]["volume_shares"], volume)
                self.assertEqual(rows[0]["close_cents"], 1050)
                self.assertEqual(rows[0]["adjust"], "none")
                self.assertIsNone(rows[0]["amount_cents"])
                self.assertIsNone(rows[0]["prev_close_cents"])
                self.assertNotIn("qfq", calls[0])

    def test_adjusted_tencent_response_not_relabelled_raw(self):
        import public_layer
        from source_http import SourceHttp
        body = json.dumps({"data": {"sh600519": {"qfqday": [
            ["2026-11-11", "10", "10.5", "11", "9.5", "12"]]}}}).encode()
        with self.assertRaises(ValueError):
            public_layer.tencent_daily("600519.SH", http=SourceHttp(
                opener=lambda request, timeout: io.BytesIO(body)))

    def test_pcf_is_fetched_scaled_and_stored_without_losing_status(self):
        import public_layer
        import types
        fields = []
        def query(code, selected, **kwargs):
            fields.append(selected)
            return object()
        @contextmanager
        def session():
            yield types.SimpleNamespace(query_history_k_data_plus=query)
        source_row = ["2026-11-11", "10.50", "21.5", "2.1", "3.2", "4.123456",
                      "0.31", "0", "1"]
        with patch.object(public_layer, "_bs_session", session), \
             patch.object(public_layer, "_rs_rows", return_value=[source_row]):
            rows = public_layer.fetch_valuation("600519.SH", "2026-11-11", "2026-11-11")
        self.assertIn("pcfNcfTTM", fields[0])
        with closing(sqlite3.connect(":memory:")) as conn:
            ensure_schema(conn)
            public_layer.store_valuation(conn, rows)
            self.assertEqual(conn.execute(
                "SELECT pcf_ncf_ttm_x10000,turnover_bps,halted,is_st FROM valuation_day"
            ).fetchone(), (41235, 31, 1, 1))

    def test_unknown_baostock_flag_is_not_assumed_normal(self):
        import public_layer
        @contextmanager
        def session():
            yield type("Fake", (), {"query_history_k_data_plus": lambda *a, **k: None})()
        for status, is_st in (("", "0"), ("1", ""), ("2", "0")):
            with self.subTest(status=status, is_st=is_st), \
                 patch.object(public_layer, "_bs_session", session), \
                 patch.object(public_layer, "_rs_rows", return_value=[[
                     "2026-11-11", "10", "2", "3", "4", "5", "0.3", status, is_st]]):
                with self.assertRaisesRegex(ValueError, "unknown"):
                    public_layer.fetch_valuation("600519.SH", "2026-11-11", "2026-11-11")

    def test_bulk_collection_continues_but_returns_partial_failure(self):
        import public_layer
        row = {"symbol": "600519.SH", "day": "2026-11-11", "close_cents": 1050,
               "pe_ttm_x10000": None, "pb_mrq_x10000": 20000, "ps_ttm_x10000": 30000,
               "pcf_ncf_ttm_x10000": 41235, "turnover_bps": 31,
               "halted": 0, "is_st": 0, "source": "baostock"}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            (root / "symbols.txt").write_text("430198.BJ\n600519.SH\n", encoding="utf-8")
            with patch.object(public_layer, "fetch_valuation", side_effect=[
                    ValueError("unsupported market"), [row]]) as fetch, \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                code = public_layer.main(["valuation", "--symbols", str(root / "symbols.txt"),
                    "--start", "2026-11-11", "--end", "2026-11-11", "--out", str(root / "rows.db")])
            result = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(code, 3)
            self.assertEqual(result["status"], "partial")
            self.assertEqual((result["failed"], result["rows"]), (1, 1))
            self.assertEqual(fetch.call_count, 2)
            with closing(sqlite3.connect(root / "rows.db")) as conn:
                self.assertEqual(conn.execute("SELECT symbol FROM valuation_day").fetchone()[0],
                                 "600519.SH")

    def test_actual_fallback_queries_requested_symbol_and_day(self):
        import public_layer
        target = self.bar(source="tencent", amount_cents=None, prev_close_cents=None)
        other = self.bar(day="2026-11-10", source="tencent")
        with patch.object(public_layer, "tdx_daily", side_effect=RuntimeError("primary down")), \
             patch.object(public_layer, "tencent_daily", return_value=[other, target]) as backup:
            result = fetch_bars("2026-11-11", symbols=["600519.SH"])
        backup.assert_called_once_with("600519.SH", timeout=30.0)
        self.assertEqual(result["rows"], [target])
        self.assertEqual(result["source"], "tencent")
        self.assertTrue(result["degraded"])
        self.assertIn("primary down", result["primary_error"])

    def test_requested_history_not_in_recent_source_is_explicit_failure(self):
        import public_layer
        with patch.object(public_layer, "tdx_daily", side_effect=RuntimeError("primary down")), \
             patch.object(public_layer, "tencent_daily", return_value=[self.bar()]):
            result = fetch_bars("2026-01-01", symbols=["600519.SH"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["failures"][0]["symbol"], "600519.SH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
