"""Fund-flow refreshes must not wait inside selection requests."""
import json
import pathlib
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest import mock

import main as M

Thread = threading.Thread
ROWS = [{"code": "600000", "super_net": 123., "quote_at": "2026-10-14T15:00:00+08:00"}]


class BackgroundFlowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = pathlib.Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / "flow.json"
        self.threads = []
        self.stack.enter_context(mock.patch.object(M, "_SUPER_FLOW_PATH", str(self.path)))
        self.stack.enter_context(mock.patch.object(M, "_SUPER_FLOW_FILE_LOCK_PATH", str(self.path)+".lock"))
        self.stack.enter_context(mock.patch.dict(M._SUPER_FLOW_CACHE, {
            "data": None, "ts": 0., "refreshing": False, "retry_at": 0., "file_signature": None,
        }, clear=True))
        self.stack.enter_context(mock.patch.dict(M._SELECT_RESULT_CACHE, {}, clear=True))
        self.stack.enter_context(mock.patch.object(M.time, "time", return_value=1000.))
        self.clock = self.stack.enter_context(mock.patch.object(M.time, "monotonic", return_value=100.))
        def spawn(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            self.threads.append(thread)
            return thread
        self.stack.enter_context(mock.patch.object(M.threading, "Thread", side_effect=spawn))
        self.addCleanup(self.join_workers)

    def join_workers(self):
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "test left a refresh worker running")

    def provider(self, **kwargs):
        return self.stack.enter_context(mock.patch.object(M.dfc, "fetch_fund_flow_rank", **kwargs))

    def test_cold_read_returns_before_the_provider_completes(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(**kwargs):
            entered.set()
            release.wait(2)
            finished.set()
            return []
        fetch = self.provider(side_effect=slow)
        self.assertEqual([], M._load_super_flow(4000))
        self.assertTrue(entered.wait(1))
        self.assertFalse(finished.is_set(), "request waited for the provider")
        for _ in range(10):
            self.assertEqual([], M._load_super_flow(4000))
            M._schedule_super_flow_refresh(4000)
        fetch.assert_called_once_with(topn=4000)

    def test_concurrent_readers_share_one_background_attempt(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(**kwargs):
            entered.set()
            release.wait(2)
            return []
        fetch = self.provider(side_effect=slow)
        results = []
        readers = [Thread(target=lambda: results.append(M._load_super_flow(4000))) for _ in range(12)]
        for reader in readers: reader.start()
        for reader in readers:
            reader.join(1)
            self.assertFalse(reader.is_alive(), "reader blocked on source I/O")
        self.assertEqual([[]]*12, results)
        self.assertTrue(entered.wait(1))
        fetch.assert_called_once()

    def test_retry_wait_starts_when_a_slow_attempt_finishes(self):
        def slow_failure(**kwargs):
            self.clock.return_value = 180.
            return []
        fetch = self.provider(side_effect=slow_failure)
        M._load_super_flow(4000)
        self.join_workers()
        self.assertEqual(210., M._SUPER_FLOW_CACHE["retry_at"])
        self.clock.return_value = 200.
        M._load_super_flow(4000)
        fetch.assert_called_once()

    def test_failed_attempt_has_a_retry_deadline_without_promoting_facts(self):
        fetch = self.provider(side_effect=OSError("source unavailable"))
        M._load_super_flow(4000)
        self.join_workers()
        for now in (100., 129.999):
            self.clock.return_value = now
            M._load_super_flow(4000)
            M._schedule_super_flow_refresh(4000)
        fetch.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(0., M._SUPER_FLOW_CACHE["ts"])
        self.clock.return_value = 130.
        M._load_super_flow(4000)
        self.join_workers()
        self.assertEqual(2, fetch.call_count)

    def test_empty_or_failed_refresh_retains_the_old_file_and_timestamp(self):
        self.path.write_text(json.dumps({"updated_at": 42., "data": ROWS}), encoding="utf-8")
        before = self.path.read_bytes()
        fetch = self.provider(side_effect=[[], OSError("source unavailable")])
        for now in (100., 130.):
            self.clock.return_value = now
            self.assertEqual(ROWS, M._load_super_flow(4000))
            self.join_workers()
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(42., M._SUPER_FLOW_CACHE["ts"])
        self.assertEqual(2, fetch.call_count)

    def test_publication_makes_new_facts_available_and_invalidates_results(self):
        M._SELECT_RESULT_CACHE["old"] = {"data": "old selection"}
        self.provider(return_value=ROWS)
        M._load_super_flow(4000)
        self.join_workers()
        self.assertEqual({}, M._SELECT_RESULT_CACHE)
        self.assertEqual(ROWS, M._load_super_flow(4000))
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({"updated_at": 1000., "data": ROWS}, payload)
        self.assertEqual(M._health_file_signature(str(self.path)), M._SUPER_FLOW_CACHE["file_signature"])

    def test_another_process_can_publish_during_the_retry_wait(self):
        fetch = self.provider(return_value=[])
        M._load_super_flow(4000)
        self.join_workers()
        self.path.write_text(json.dumps({"updated_at": 1000., "data": ROWS}), encoding="utf-8")
        self.assertEqual(ROWS, M._load_super_flow(4000))
        fetch.assert_called_once()

    def test_file_publication_invalidates_the_public_selection_result(self):
        with mock.patch.object(M, "_selection_signature", return_value=("same factors",)), \
                mock.patch.object(M, "_select_uncached", side_effect=[{"marker":"old"},{"marker":"new"}]) as select, \
                mock.patch.object(M, "_save_selection_result"):
            self.assertEqual({"marker":"old"}, M.select("three_day", 10))
            self.assertEqual({"marker":"old"}, M.select("three_day", 10))
            self.path.write_text(json.dumps({"updated_at":1000.,"data":ROWS}), encoding="utf-8")
            self.assertEqual({"marker":"new"}, M.select("three_day", 10))
        self.assertEqual(2, select.call_count)

    def test_corrupt_cache_cannot_replace_the_last_known_facts(self):
        M._SUPER_FLOW_CACHE.update(data=ROWS, ts=42.)
        self.path.write_text(json.dumps({"updated_at": 1000., "data": [17]}), encoding="utf-8")
        self.provider(return_value=[])
        self.assertEqual(ROWS, M._load_super_flow(4000))
        self.join_workers()
        self.assertEqual(42., M._SUPER_FLOW_CACHE["ts"])

    def test_thread_start_failure_releases_the_inflight_flag(self):
        with mock.patch.object(M.threading, "Thread") as factory:
            factory.return_value.start.side_effect = RuntimeError("thread limit")
            self.assertEqual([], M._load_super_flow(4000))
            self.assertFalse(M._SUPER_FLOW_CACHE["refreshing"])
            self.assertEqual([], M._load_super_flow(4000))
            factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
