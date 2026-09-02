import os
import sys
import tempfile
import unittest
import datetime as dt
from unittest import mock


# The unit tests mock all network access and do not require requests locally.
sys.modules.setdefault("requests", mock.MagicMock())
sys.path.insert(0, os.path.dirname(__file__))
import alt_data as AD  # noqa: E402


class AltDataPointInTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_patch = mock.patch.object(AD, "CACHE_DIR", self.tmp.name)
        self.cache_patch.start()
        AD._mem_cache.clear()

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_lhb_age_is_relative_to_asof_day(self):
        row = {"date": "2020-01-08", "net_buy_wan": 100.0, "reason": "test"}
        with mock.patch.object(AD, "lhb_for_stock", return_value=[row]):
            score, _ = AD.lhb_position_signal("000001", asof_day="2020-01-10")
        self.assertEqual(score, 6.0)

    def test_lockup_window_is_relative_to_asof_day(self):
        rows = {"000001": [{"date": "2020-01-20", "ratio": 0.04}]}
        with mock.patch.object(AD, "lockup_upcoming", return_value=rows):
            score, detail = AD.lockup_penalty("000001", asof_day="2020-01-01")
        self.assertEqual(score, 12.0)
        self.assertIn("T-19", detail)

    def test_margin_query_has_point_in_time_upper_bound(self):
        seen = {}

        def fake_query(*args, **kwargs):
            seen.update(kwargs)
            return []

        with mock.patch.object(AD, "eastmoney_datacenter", side_effect=fake_query):
            AD.margin_trading("000001", asof_day="2020-01-10")
        self.assertIn("DATE<='2020-01-10'", seen["filter_str"])

    def test_holder_requires_disclosure_date_not_report_period(self):
        future = {
            "END_DATE": "2019-12-31", "HOLD_NOTICE_DATE": "2020-02-01",
            "HOLDER_NUM_RATIO": -10, "HOLDER_NUM": 100,
        }
        with mock.patch.object(AD, "eastmoney_datacenter", return_value=[future]):
            score, detail = AD.holder_signal("000001", asof_day="2020-01-10")
        self.assertEqual((score, detail), (0.0, None))

    def test_block_trade_query_and_age_use_asof_day(self):
        seen = {}
        row = {
            "TRADE_DATE": "2020-01-05", "CLOSE_PRICE": 10,
            "DEAL_PRICE": 9, "DEAL_AMT": 20_000_000,
        }

        def fake_query(*args, **kwargs):
            seen.update(kwargs)
            return [row]

        with mock.patch.object(AD, "eastmoney_datacenter", side_effect=fake_query):
            score, _ = AD.block_trade_signal("000001", asof_day="2020-01-10")
        self.assertEqual(score, -5.0)
        self.assertIn("TRADE_DATE<='2020-01-10'", seen["filter_str"])

    def test_provider_failure_is_not_persisted_as_empty_daily_cache(self):
        with mock.patch.object(AD, "eastmoney_datacenter", side_effect=RuntimeError("down")):
            self.assertEqual(AD.margin_trading("000001", asof_day="2020-01-10"), [])
        expected = os.path.join(self.tmp.name, "margin_000001_2020-01-10.json")
        self.assertFalse(os.path.exists(expected))

    def test_current_only_eps_is_unavailable_to_historical_replay(self):
        self.assertIsNone(AD.ths_eps_forecast("000001", asof_day="2020-01-10"))

    def test_minute_flow_refuses_historical_replay(self):
        result = AD.eastmoney_minute_fund_flow("000001", asof_day="2020-01-10")
        self.assertEqual(result["status"], "historical_unavailable")
        self.assertEqual(result["points"], [])

    def test_minute_flow_trajectory_uses_cumulative_differences(self):
        points = [
            {"time": f"09:{30 + idx:02d}", "main_net": value,
             "small_net": 0.0, "mid_net": 0.0, "large_net": 0.0,
             "super_net": value / 2}
            for idx, value in enumerate([0, -100, -200, -100, 100, 400, 700])
        ]
        raw = {
            "status": "ok", "points": points, "source": "fixture",
            "source_at": "2026-08-25T09:36:00", "data_at": "09:36",
        }
        with mock.patch.object(AD, "eastmoney_minute_fund_flow", return_value=raw):
            result = AD.fund_flow_trajectory("000001", asof_day="2026-08-25")
        self.assertEqual(result["main_delta_3m"], 800.0)
        self.assertEqual(result["main_delta_5m"], 800.0)
        self.assertEqual(result["direction"], "inflow")
        self.assertEqual(result["reversal"], "outflow_to_inflow")
        self.assertFalse(result["score_applied"])

    def test_microstructure_combines_depth_ticks_and_vwap_as_shadow(self):
        def fixture(url, params, timeout=5):
            if url == AD.QUOTE_DEPTH_URL:
                return {"f32": 100, "f34": 100, "f36": 100, "f38": 100,
                        "f40": 100, "f20": 50, "f18": 50, "f16": 50,
                        "f14": 50, "f12": 50}
            if url == AD.TICK_DETAIL_URL:
                return {"details": ["09:31:00,10.00,100,1,2",
                                     "09:31:03,10.01,40,1,1"]}
            return {"trends": ["09:30,10.00,9.98,1,1,1",
                                "09:31,10.02,10.00,1,1,1"]}

        with mock.patch.object(AD, "_micro_get", side_effect=fixture), \
             mock.patch.object(AD, "_tencent_depth", return_value={
                 "bid_volume": 500, "ask_volume": 250, "quote_at": "20260825093100",
             }):
            result = AD.market_microstructure(
                "000001", asof_day=dt.date.today(), ttl_seconds=0,
            )
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["depth_imbalance"], 1 / 3, places=4)
        self.assertAlmostEqual(result["active_buy_sell_imbalance"], 3 / 7, places=4)
        self.assertAlmostEqual(result["vwap_deviation_pct"], 0.2, places=3)
        self.assertFalse(result["score_applied"])

    def test_microstructure_refuses_historical_replay(self):
        result = AD.market_microstructure("000001", asof_day="2020-01-10")
        self.assertEqual(result["status"], "historical_unavailable")
        self.assertFalse(result["score_applied"])


if __name__ == "__main__":
    unittest.main()
