"""Exercise the real HTTP retry helper through the primary feed adapter."""
import json
import unittest
from unittest import mock

import requests

import marketdata_feeds as feeds
import marketdata_transport as transport


class QuoteRetryOwnerTests(unittest.TestCase):
    def setUp(self):
        # 主机短路是模块级状态；本模块会注入传输失败，先清空。
        transport.reset_transport_breakers()
        self.session = mock.Mock()
        self.outer_sleep = mock.Mock()
        self.inner_sleep = mock.Mock()
        self.health = feeds.FeedHealthRegistry()
        self.session_patch = mock.patch.object(transport, "_session", return_value=self.session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.sleep_patch = mock.patch.object(transport.time, "sleep", self.inner_sleep)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.reset = mock.Mock()

    def feed(self, **options):
        def get_json(url, params, **kwargs):
            text = transport.http_get(url, params=params, **kwargs)
            return json.loads(text) if text else None

        return feeds.EastmoneyRealtimeFeed(
            get_json=get_json, secid=lambda code: "0." + code,
            row_parser=lambda raw: {"code": raw["f12"], "price": raw["f2"]},
            reset_data_source=self.reset, ut="test", fields="f2,f12",
            sleep=self.outer_sleep, health=self.health, **options,
        )

    def response(self, rows):
        return mock.Mock(text=json.dumps({"data": {"diff": rows}}))

    def test_healthy_quotes_keep_metadata_without_extra_requests(self):
        self.session.get.return_value = self.response([{"f12": "000001", "f2": 10}])
        value = self.feed().fetch_realtime_with_meta(["000001"])
        self.assertEqual(value["rows"], [{"code": "000001", "price": 10}])
        self.assertEqual(value["coverage_pct"], 100)
        self.assertTrue(value["complete"])
        self.session.get.assert_called_once()
        self.inner_sleep.assert_not_called()
        self.outer_sleep.assert_not_called()

    def test_outage_has_one_http_attempt_per_host_and_retains_outer_backoff(self):
        self.session.get.side_effect = requests.Timeout("offline")
        value = self.feed().fetch_realtime_with_meta(["000001", "000002"])
        # Three rounds x three hosts, then the original three-host small batch.
        self.assertEqual(self.session.get.call_count, 12)
        self.assertEqual(value["rows"], [])
        self.assertEqual(value["missing_codes"], ["000001", "000002"])
        self.assertFalse(value["complete"])
        self.assertEqual(value["batches"][0]["attempts"], 3)
        self.inner_sleep.assert_not_called()
        self.assertEqual([c.args[0] for c in self.outer_sleep.call_args_list],
                         [0.25] * 3 + [0.5] * 3 + [0.75] * 3)
        self.assertEqual(self.health.snapshot()["eastmoney_ulist"]["total_failures"], 1)

    def test_temporary_failure_moves_to_next_host_and_recovers(self):
        self.session.get.side_effect = [requests.Timeout("offline"),
                                       self.response([{"f12": "000001", "f2": 10}])]
        value = self.feed().fetch_realtime_with_meta(["000001"])
        self.assertTrue(value["complete"])
        self.assertEqual(self.session.get.call_count, 2)
        urls = [call.args[0] for call in self.session.get.call_args_list]
        self.assertNotEqual(urls[0], urls[1])
        self.inner_sleep.assert_not_called()
        self.outer_sleep.assert_called_once_with(0.25)

    def test_partial_response_still_recovers_missing_codes_in_small_batch(self):
        self.session.get.side_effect = [
            self.response([{"f12": "000001", "f2": 10}]),
            self.response([{"f12": "000003", "f2": 30}, {"f12": "000002", "f2": 20}]),
        ]
        value = self.feed(hosts=("example.invalid",), attempts=1).fetch_realtime_with_meta(
            ["000001", "000002", "000003"])
        self.assertTrue(value["complete"])
        self.assertEqual([r["code"] for r in value["rows"]], ["000001", "000002", "000003"])
        self.assertEqual(self.session.get.call_args.kwargs["params"]["secids"], "0.000002,0.000003")
        self.inner_sleep.assert_not_called()

    def test_empty_response_remains_symbol_missing_and_does_not_trip_circuit(self):
        self.session.get.return_value = mock.Mock(text="")
        value = self.feed().fetch_realtime_with_meta(["000001", "000002"])
        self.assertEqual(self.session.get.call_count, 12)
        self.assertEqual(value["rows"], [])
        self.assertFalse(value["complete"])
        state = self.health.snapshot()["eastmoney_ulist"]
        self.assertEqual(state["total_failures"], 0)
        self.assertEqual(state["status"], "degraded")
        self.assertFalse(state["circuit_open"])
        self.inner_sleep.assert_not_called()

    def test_existing_circuit_still_stops_further_outage_requests(self):
        self.session.get.side_effect = requests.Timeout("offline")
        feed = self.feed()
        for _ in range(3):
            self.assertEqual(feed.fetch_realtime(["000001", "000002"]), [])
        calls = self.session.get.call_count
        self.assertGreater(calls, 0, "断电也要真的尝试过，不能一次都没发")
        # 两层短路共同生效：feed 层 circuit + 2026-10-27 的传输层主机短路
        # （连续 6 次传输失败后 90 秒内快速失败）。断言契约"断电后请求不再增长"，
        # 而不是写死次数——次数会随任一层阈值调整，契约不会。
        self.assertLess(calls, 36, "传输层短路应让断电期间的请求次数下降")
        self.assertEqual(feed.fetch_realtime(["000001", "000002"]), [])
        self.assertEqual(self.session.get.call_count, calls, "短路后不得再发新请求")
        self.assertTrue(self.health.snapshot()["eastmoney_ulist"]["circuit_open"])
        opened = [host for host, state in transport.transport_breaker_snapshot().items()
                  if state["open"]]
        self.assertTrue(opened, "传输层短路也应打开，覆盖 feed 之外的调用方")


if __name__ == "__main__":
    unittest.main()
