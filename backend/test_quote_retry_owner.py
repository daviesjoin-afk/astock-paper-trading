"""Exercise the real HTTP retry helper through the primary feed adapter."""
import json
import unittest
from unittest import mock

import requests

import marketdata_feeds as feeds
import marketdata_transport as transport


class QuoteRetryOwnerTests(unittest.TestCase):
    def setUp(self):
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
        self.assertEqual(calls, 36)
        self.assertEqual(feed.fetch_realtime(["000001", "000002"]), [])
        self.assertEqual(self.session.get.call_count, calls)
        self.assertTrue(self.health.snapshot()["eastmoney_ulist"]["circuit_open"])


if __name__ == "__main__":
    unittest.main()
