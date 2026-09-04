# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest

import marketdata_cache as cache


class MarketDataCacheTests(unittest.TestCase):
    def test_cached_reuses_value_and_does_not_store_empty_result(self):
        values = {"calls": 0}
        memory = {}
        lock = threading.Lock()

        def load():
            values["calls"] += 1
            return {"ok": True}

        self.assertEqual(cache.cached(memory, lock, "key", 60, load), {"ok": True})
        self.assertEqual(cache.cached(memory, lock, "key", 60, load), {"ok": True})
        self.assertEqual(values["calls"], 1)

        empty_calls = {"count": 0}

        def empty():
            empty_calls["count"] += 1
            return []

        cache.cached(memory, lock, "empty", 60, empty)
        cache.cached(memory, lock, "empty", 60, empty)
        self.assertEqual(empty_calls["count"], 2)
        self.assertNotIn("empty", memory)

    def test_full_snapshot_lock_creates_and_releases_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "snapshot.lock")
            thread_lock = threading.Lock()
            with cache.full_snapshot_singleflight_lock(thread_lock, path):
                self.assertTrue(os.path.exists(path))
            self.assertFalse(thread_lock.locked())

    def test_source_health_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "health.json")
            payload = {"healthy": True, "rows": 3}
            cache.save_source_health(path, payload)
            self.assertEqual(cache.load_source_health(path), payload)

    def test_missing_source_health_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cache.load_source_health(os.path.join(directory, "missing.json")), {})


if __name__ == "__main__":
    unittest.main()
