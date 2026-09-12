# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

import marketdata_cache as cache
import marketdata_feeds as feeds


class FeedHealthRegistryTests(unittest.TestCase):
    def test_repeated_failures_open_only_that_feed_and_half_open_after_cooldown(self):
        clock = {"mono": 100.0, "wall": 1_700_000_000.0}
        registry = feeds.FeedHealthRegistry(
            monotonic=lambda: clock["mono"],
            wall_clock=lambda: clock["wall"],
        )
        policy = feeds.FeedReliabilityPolicy(failure_threshold=2, cooldown_seconds=30)

        self.assertTrue(registry.allow("primary"))
        registry.record_result("primary", requested=1, returned=0, policy=policy, reason="timeout")
        self.assertTrue(registry.allow("primary"))
        registry.record_result("primary", requested=1, returned=0, policy=policy, reason="timeout")

        self.assertFalse(registry.allow("primary"))
        self.assertTrue(registry.allow("fallback"))
        snap = registry.snapshot()
        self.assertEqual(snap["primary"]["status"], "circuit_open")
        self.assertTrue(snap["primary"]["circuit_open"])
        self.assertEqual(snap["primary"]["last_error"], "timeout")

        clock["mono"] += 31
        self.assertTrue(registry.allow("primary"))
        self.assertEqual(registry.snapshot()["primary"]["status"], "half_open")

        registry.record_result("primary", requested=1, returned=1, policy=policy)
        recovered = registry.snapshot()["primary"]
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertFalse(recovered["circuit_open"])

    def test_partial_result_is_degraded_but_does_not_trip_circuit(self):
        registry = feeds.FeedHealthRegistry()
        policy = feeds.FeedReliabilityPolicy(failure_threshold=1, cooldown_seconds=30)
        registry.record_result(
            "eastmoney", requested=3, returned=2, policy=policy,
            reason="partial realtime coverage",
        )
        state = registry.snapshot()["eastmoney"]
        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertFalse(state["circuit_open"])


class ReliableFeedIntegrationTests(unittest.TestCase):
    def test_tencent_uses_shared_timeout_and_opens_circuit_after_empty_failures(self):
        registry = feeds.FeedHealthRegistry()
        policy = feeds.FeedReliabilityPolicy(
            timeout_seconds=3.5, failure_threshold=2, cooldown_seconds=60,
        )
        seen = []

        def http_get(_url, **kwargs):
            seen.append(kwargs["timeout"])
            return ""

        feed = feeds.TencentRealtimeFeed(
            http_get=http_get,
            parser=lambda *_args, **_kwargs: [],
            reset_data_source=lambda *_args, **_kwargs: None,
            sleep=lambda _seconds: None,
            attempts=1,
            reliability=policy,
            health=registry,
        )
        self.assertEqual(feed.fetch_realtime(["000001"]), [])
        self.assertEqual(feed.fetch_realtime(["000001"]), [])
        self.assertEqual(seen, [3.5, 3.5])
        self.assertEqual(registry.snapshot()[feed.name]["status"], "circuit_open")

        # Open circuit must fail fast without touching transport again.
        self.assertEqual(feed.fetch_realtime(["000001"]), [])
        self.assertEqual(seen, [3.5, 3.5])

    def test_chain_continues_when_primary_raises(self):
        class BrokenFeed:
            name = "broken"
            reliability = feeds.FeedReliabilityPolicy(failure_threshold=1)
            health = feeds.FeedHealthRegistry()

            def fetch_realtime(self, _codes):
                raise TimeoutError("provider timed out")

        class FallbackFeed:
            name = "fallback"

            def fetch_realtime(self, codes):
                return [
                    {"code": code, "price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00"}
                    for code in codes
                ]

        primary = BrokenFeed()
        chain = feeds.DataFeedChain((primary, FallbackFeed()), retry_last_feed=False)
        rows = chain.fetch_realtime(["000001"])
        self.assertEqual([row["code"] for row in rows], ["000001"])
        state = primary.health.snapshot()["broken"]
        self.assertEqual(state["status"], "circuit_open")
        self.assertIn("TimeoutError", state["last_error"])


class SourceHealthPersistenceTests(unittest.TestCase):
    def test_save_source_health_attaches_runtime_feed_snapshot(self):
        feeds.FEED_HEALTH.reset()
        feeds.FEED_HEALTH.record_result(
            "tencent_public_quote",
            requested=2,
            returned=0,
            policy=feeds.FeedReliabilityPolicy(failure_threshold=1, cooldown_seconds=10),
            reason="timeout",
        )
        tmp = tempfile.TemporaryDirectory(prefix="astock-feed-health-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "health.json")
        cache.save_source_health(path, {"healthy": False, "action": "degraded"})
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIn("runtime_feeds", payload)
        state = payload["runtime_feeds"]["tencent_public_quote"]
        self.assertEqual(state["status"], "circuit_open")
        self.assertEqual(state["last_error"], "timeout")
        self.assertTrue(state["circuit_open"])


if __name__ == "__main__":
    unittest.main()
