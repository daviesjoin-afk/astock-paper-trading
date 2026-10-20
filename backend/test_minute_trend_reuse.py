"""Minute-series and microstructure consumers share raw evidence, not cache age."""
import datetime as dt
import unittest
from unittest import mock

import alt_data as AD


class MinuteTrendReuseTests(unittest.TestCase):
    def setUp(self):
        with AD._cache_lock:
            AD._mem_cache.clear()
        self.day = dt.date.today()
        self.code = "000001"
        self.raw = {"trends": ["09:30,10.00,9.98,10,10000", "09:31,10.02,10.00,20,20040"]}

    def check_order(self, minute_first):
        def source(url, params):
            return self.raw if url == AD.MINUTE_TREND_URL else {"details": ["09:31,10.02,100,1,2"]}
        with mock.patch.object(AD, "_micro_get", side_effect=source) as get, \
                mock.patch.object(AD, "_tencent_depth", return_value={"bid_volume": 100, "ask_volume": 50}):
            if minute_first:
                series = AD.minute_trend_series(self.code, self.day)
                micro = AD.market_microstructure(self.code, self.day)
            else:
                micro = AD.market_microstructure(self.code, self.day)
                series = AD.minute_trend_series(self.code, self.day)
        self.assertEqual(sum(call.args[0] == AD.MINUTE_TREND_URL for call in get.call_args_list), 1)
        self.assertEqual(series[-1], {"time": "09:31", "price": 10.02, "vwap": 10., "volume": 20., "amount": 20040.})
        self.assertEqual(micro["status"], "ok")
        self.assertAlmostEqual(micro["vwap_deviation_pct"], 0.2)
        self.assertFalse(micro["score_applied"])

    def test_both_consumer_orders_keep_same_evidence_with_one_trend_request(self):
        for minute_first in (True, False):
            with self.subTest(minute_first=minute_first):
                self.setUp()
                self.check_order(minute_first)

    def test_derived_caches_keep_raw_age_and_expiry_fetches_again(self):
        original_time = dt.datetime.now() - dt.timedelta(seconds=80)
        raw_key = f"minute_trend_raw:{self.day}:{self.code}"
        AD._store(raw_key, self.raw, stored_at=original_time)
        with mock.patch.object(AD, "_micro_trend", return_value=self.raw) as fetch, \
                mock.patch.object(AD, "_tencent_depth", return_value={}), \
                mock.patch.object(AD, "_micro_ticks", return_value={}):
            AD.minute_trend_series(self.code, self.day)
            AD.market_microstructure(self.code, self.day)
            fetch.assert_not_called()
            for prefix in ("minute_trend", "microstructure"):
                self.assertEqual(AD._mem_cache[f"{prefix}:{self.day}:{self.code}"][0], original_time)
            # A stricter caller's TTL must also expire the derived entries.
            AD.minute_trend_series(self.code, self.day, ttl_seconds=60)
            fetch.assert_called_once_with(self.code)

    def test_failure_and_empty_response_do_not_hide_a_later_recovery(self):
        with mock.patch.object(AD, "_micro_trend", side_effect=[TimeoutError, {}, {"trends": ["malformed"]}, self.raw]) as fetch:
            self.assertEqual(AD.minute_trend_series(self.code, self.day), [])
            self.assertEqual(AD.minute_trend_series(self.code, self.day), [])
            self.assertEqual(AD.minute_trend_series(self.code, self.day), [])
            self.assertEqual(len(AD.minute_trend_series(self.code, self.day)), 2)
            self.assertEqual(fetch.call_count, 4)

    def test_historical_requests_never_consume_live_cache_and_raw_copies_are_isolated(self):
        with mock.patch.object(AD, "_micro_trend", return_value=self.raw) as fetch:
            first, stamp = AD._minute_trend_payload(self.code, self.day.isoformat(), 90)
            first["trends"].clear()
            second, again = AD._minute_trend_payload(self.code, self.day.isoformat(), 90)
            self.assertEqual(len(second["trends"]), 2)
            self.assertEqual(stamp, again)
            self.assertEqual(AD.minute_trend_series(self.code, "2020-01-01"), [])
            self.assertEqual(AD.market_microstructure(self.code, "2020-01-01")["status"], "historical_unavailable")
            fetch.assert_called_once()
