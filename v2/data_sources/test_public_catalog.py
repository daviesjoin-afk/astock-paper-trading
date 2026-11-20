"""Behavioral tests of the common SDK against the vendored public sources."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from public_data import PublicDataClient, main
from source_http import SourceHttp, SourceHttpError

NOW = datetime(2026, 11, 14, 15, 40, tzinfo=timezone.utc)


class PublicCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.calls = []
        self.body = json.dumps({"data": {"klines": [
            "2026-11-13,1250.12,-250.02,50.10,450.04,1000.08,0,0"]}}).encode()
        def opener(request, timeout):
            self.calls.append(request.full_url)
            return io.BytesIO(self.body)
        self.http = SourceHttp(self.path / "cache", clock=lambda: NOW, opener=opener)
        self.client = PublicDataClient(self.http)

    def test_catalog_includes_original_routes_all_upstream_groups_and_function_params(self):
        def deny(*args, **kwargs):
            raise AssertionError("catalog accessed network")
        with patch.object(socket.socket, "connect", deny):
            entries = self.client.datasets()
        names = {item["name"] for item in entries}
        self.assertTrue({"bars", "quotes", "company", "announcements"} <= names)
        self.assertTrue({"macro.repo_fixing", "events.share_buyback", "futures.daily",
                         "options.sina_greeks", "capital.chip_distribution", "news.cls",
                         "backup.margin", "stock_fund_flow_120d", "eastmoney_stock_news"} <= names)
        flow = next(item for item in entries if item["name"] == "stock_fund_flow_120d")
        self.assertEqual(flow["parameters"], ["code"])
        news = next(item for item in entries if item["name"] == "eastmoney_stock_news")
        self.assertEqual(news["parameters"], ["code", "page_size"])

    def test_real_source_parser_is_used_and_success_is_frozen(self):
        result = self.client.dataset("stock_fund_flow_120d", code="600519")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["rows"][0]["main_net"], 1250.12)
        self.assertEqual(result["rows"][0]["super_net"], 1000.08)
        self.assertEqual(result["fetched_at"], NOW.isoformat())
        self.assertIn("push2his.eastmoney.com", self.calls[0])
        self.assertFalse(result["historical_first_seen_verified"])
        self.http.offline = True
        cached = self.client.dataset("stock_fund_flow_120d", code="600519")
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["rows"], result["rows"])
        self.assertEqual(len(self.calls), 1)

    def test_missing_source_bucket_is_unknown_not_zero(self):
        self.body = json.dumps({"data": {"klines": [
            "2026-11-13,100,-,10,20,70,0"]}}).encode()
        result = self.client.dataset("stock_fund_flow_120d", code="600519")
        self.assertEqual(result["status"], "ok", result)
        self.assertIsNone(result["rows"][0]["small_net"])
        self.assertEqual(result["rows"][0]["super_net"], 70)

    def test_current_news_does_not_answer_uncaptured_historical_as_of(self):
        with self.assertRaises(SourceHttpError) as caught:
            self.client.dataset("eastmoney_stock_news", code="600519",
                                as_of="2026-11-13T15:00:00+08:00")
        self.assertEqual(caught.exception.code, "historical_snapshot_unavailable")
        self.assertEqual(self.calls, [])

    def test_failed_source_warning_is_structured_and_never_cached_as_success(self):
        self.body = b"<html>not market data</html>"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = self.client.dataset("stock_fund_flow_120d", code="600519")
        self.assertEqual(buffer.getvalue(), "", "provider prints must not corrupt JSON CLI output")
        self.assertNotEqual(result["status"], "ok", result)
        self.assertEqual(list((self.path / "cache").glob("*.json")), [])
        self.body = json.dumps({"data": {"klines": [
            "2026-11-13,10,1,2,3,4,0"]}}).encode()
        result = self.client.dataset("stock_fund_flow_120d", code="600519")
        self.assertEqual(result["rows"][0]["main_net"], 10)
        self.assertEqual(len(self.calls), 2)

    def test_empty_legacy_reply_does_not_prove_confirmed_no_data(self):
        self.body = b'{"unexpected":"schema"}'
        result = self.client.dataset("stock_fund_flow_120d", code="600519")
        self.assertNotEqual(result["status"], "ok", result)
        self.assertEqual(list((self.path / "cache").glob("*.json")), [])

    def test_calculators_run_without_network_and_nonfinite_stays_unknown(self):
        good = self.client.dataset("valuation.forward_pe", price=100, eps_forecast=5)
        absent = self.client.dataset("valuation.forward_pe", price=100, eps_forecast=0)
        self.assertEqual(good["data"], 20)
        self.assertIsNone(absent["data"])
        json.dumps(absent, allow_nan=False)
        self.assertEqual(self.calls, [])

    def test_dataframe_attrs_dates_and_missing_values_survive_common_sdk(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas optional dependency absent")
        try:
            from astock_public import QueryResult
            target = "astock_public.client.PublicClient.query"
        except ImportError:
            from v2.data_sources.astock_public import QueryResult
            target = "v2.data_sources.astock_public.client.PublicClient.query"
        frame = pd.DataFrame({"date": [pd.Timestamp("2026-11-13")],
                              "price": [float("nan")], "size": [pd.NA]})
        frame.attrs["complete"] = False
        answer = QueryResult("ok", "trades.tencent_ticks", "tencent_ticks", frame,
                             {"source": "tencent", "temporal": "same_trading_day"})
        with patch(target, return_value=answer):
            result = self.client.dataset("trades.tencent_ticks", code="600519")
        self.assertFalse(result["data"]["attrs"]["complete"])
        self.assertIsNone(result["rows"][0]["price"])
        self.assertIsNone(result["rows"][0]["size"])
        self.assertTrue(result["rows"][0]["date"].startswith("2026-11-13"))
        json.dumps(result, allow_nan=False)

    def test_current_dataset_expires_after_thirty_seconds_without_shortening_history_ttl(self):
        from astock_public import QueryResult
        answer = QueryResult("ok", "trades.tencent_ticks", "tencent_ticks", [{"price": 10}],
                             {"source": "tencent", "temporal": "same_trading_day"})
        with patch("astock_public.client.PublicClient.query", return_value=answer) as query:
            first = self.client.dataset("trades.tencent_ticks", code="600519")
            self.assertFalse(first["cache_hit"])
            self.http.clock = lambda: NOW + timedelta(seconds=30)
            self.assertTrue(self.client.dataset("trades.tencent_ticks", code="600519")["cache_hit"])
            self.http.clock = lambda: NOW + timedelta(seconds=31)
            self.assertFalse(self.client.dataset("trades.tencent_ticks", code="600519")["cache_hit"])
            self.assertEqual(query.call_count, 2)
        self.assertEqual(self.http.ttl, 86400)

    def test_dataset_cli_lists_catalog_and_runs_real_calculator(self):
        for args in (["datasets"], ["dataset", "--name", "valuation.forward_pe",
                                   "--params", '{"price":100,"eps_forecast":5}']):
            with self.subTest(args=args), patch("sys.stdout", new_callable=io.StringIO) as out:
                code = main(args)
            self.assertEqual(code, 0, out.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"], 20)


if __name__ == "__main__":
    unittest.main()
