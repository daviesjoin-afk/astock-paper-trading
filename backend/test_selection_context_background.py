"""Cold financial/sentiment context must not wait inside selection requests."""
import threading
import unittest
from contextlib import ExitStack
from unittest import mock

import main as M

Thread = threading.Thread
FINANCE = {"data": {"600000": {"net_profit": 123., "report_date": "2026-06-30",
                              "notice_date": "2026-08-01"}}, "report_dates": ["2026-06-30"]}
SENTIMENT = {"600000": {"hot_rank": 1, "sentiment": .6}}


class BackgroundContextTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(M._LIVE_CACHE,
                                                finance={"data": {}, "report_dates": []}, sentiment={}))
        self.stack.enter_context(mock.patch.dict(M._SELECTION_CONTEXT_STATE, {
            key: {"refreshing": False, "retry_at": 0.} for key in ("finance", "sentiment")
        }, clear=True))
        self.stack.enter_context(mock.patch.object(M, "_SELECTION_CONTEXT_VERSION", 0))
        self.stack.enter_context(mock.patch.dict(M._SELECT_RESULT_CACHE, {}, clear=True))
        self.clock = self.stack.enter_context(mock.patch.object(M.time, "monotonic", return_value=100.))
        self.threads = []
        def spawn(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            self.threads.append(thread)
            return thread
        self.stack.enter_context(mock.patch.object(M.threading, "Thread", side_effect=spawn))
        self.addCleanup(self.join_workers)

    def join_workers(self):
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "test left a context worker running")

    def test_cold_context_returns_before_both_providers_complete(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = {key: threading.Event() for key in ("finance", "sentiment")}
        def slow(key, value):
            entered[key].set()
            release.wait(2)
            return value
        finance = mock.Mock(side_effect=lambda: slow("finance", FINANCE))
        sentiment = mock.Mock(side_effect=lambda: slow("sentiment", SENTIMENT))
        self.assertEqual({"data": {}, "report_dates": []}, M._load_selection_context("finance", finance))
        self.assertEqual({}, M._load_selection_context("sentiment", sentiment))
        self.assertTrue(all(event.wait(1) for event in entered.values()))
        self.assertFalse(release.is_set())
        for _ in range(10):
            M._load_selection_context("finance", finance)
            M._load_selection_context("sentiment", sentiment)
        finance.assert_called_once_with()
        sentiment.assert_called_once_with()
        release.set()
        self.join_workers()
        self.assertEqual(FINANCE, M._load_selection_context("finance", finance))
        self.assertEqual(SENTIMENT, M._load_selection_context("sentiment", sentiment))
        self.assertEqual(2, M._SELECTION_CONTEXT_VERSION)

    def test_concurrent_readers_share_one_attempt_per_context(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow():
            entered.set()
            release.wait(2)
            return {}
        fetch = mock.Mock(side_effect=slow)
        readers = [Thread(target=M._load_selection_context, args=("sentiment", fetch)) for _ in range(12)]
        for reader in readers: reader.start()
        for reader in readers:
            reader.join(1)
            self.assertFalse(reader.is_alive(), "reader waited for provider I/O")
        self.assertEqual(1, len(self.threads))
        self.assertTrue(entered.wait(1))
        fetch.assert_called_once_with()

    def test_empty_and_exception_retry_after_completion_without_publication(self):
        def slow_empty():
            self.clock.return_value = 180.
            return {"data": {}, "report_dates": ["2026-06-30"]}
        fetch = mock.Mock(side_effect=slow_empty)
        M._load_selection_context("finance", fetch)
        self.join_workers()
        self.assertEqual(210., M._SELECTION_CONTEXT_STATE["finance"]["retry_at"])
        self.assertEqual([], M._LIVE_CACHE["finance"]["report_dates"])
        self.assertEqual(0, M._SELECTION_CONTEXT_VERSION)
        self.clock.return_value = 209.99
        M._load_selection_context("finance", fetch)
        fetch.assert_called_once()
        self.clock.return_value = 210.
        fetch.side_effect = OSError("source unavailable")
        M._load_selection_context("finance", fetch)
        self.join_workers()
        self.assertEqual(2, fetch.call_count)
        self.assertFalse(M._SELECTION_CONTEXT_STATE["finance"]["refreshing"])
        self.assertEqual(240., M._SELECTION_CONTEXT_STATE["finance"]["retry_at"])
        self.assertEqual(0, M._SELECTION_CONTEXT_VERSION)

    def test_warm_context_is_returned_without_scheduling_or_changing_dates(self):
        M._LIVE_CACHE.update(finance=FINANCE, sentiment=SENTIMENT)
        fetch = mock.Mock(side_effect=AssertionError("warm context must not fetch"))
        self.assertIs(FINANCE, M._load_selection_context("finance", fetch))
        self.assertIs(SENTIMENT, M._load_selection_context("sentiment", fetch))
        self.assertEqual([], self.threads)
        self.assertEqual("2026-08-01", FINANCE["data"]["600000"]["notice_date"])

    def test_short_lived_job_reads_data_synchronously_without_a_daemon(self):
        thread = threading.current_thread()
        def fetch():
            self.assertIs(thread, threading.current_thread())
            return FINANCE
        self.assertIs(FINANCE, M._load_selection_context("finance", fetch, background=False))
        self.assertIs(FINANCE, M._LIVE_CACHE["finance"])
        self.assertEqual([], self.threads)

    def test_public_selection_explicitly_requests_background_context(self):
        with mock.patch.object(M, "_selection_signature", return_value=("factors",)), \
                mock.patch.object(M, "_health_file_signature", return_value=None), \
                mock.patch.object(M, "_select_uncached", return_value={"picks": []}) as select, \
                mock.patch.object(M, "_save_selection_result"):
            M.select("three_day", 10)
            select.assert_called_once_with(strategy="three_day", topn=10, background_context=True)

    def test_bad_or_failed_refresh_retains_previous_facts(self):
        for key, value in (("finance", FINANCE), ("sentiment", SENTIMENT)):
            M._LIVE_CACHE[key] = value
            malformed = {"data": {"600000": 12}} if key == "finance" else {"600000": 12}
            for bad in (None, {}, [], malformed):
                M._refresh_selection_context(key, lambda bad=bad: bad)
                self.assertIs(value, M._LIVE_CACHE[key])
            M._refresh_selection_context(key, mock.Mock(side_effect=OSError("source unavailable")))
            self.assertIs(value, M._LIVE_CACHE[key])
        self.assertEqual(0, M._SELECTION_CONTEXT_VERSION)

    def test_publication_clears_an_already_cached_selection(self):
        M._SELECT_RESULT_CACHE["previous"] = {"data": "missing finance"}
        M._load_selection_context("finance", lambda: FINANCE)
        self.join_workers()
        self.assertEqual({}, M._SELECT_RESULT_CACHE)
        self.assertEqual(FINANCE, M._LIVE_CACHE["finance"])
        self.assertEqual(1, M._SELECTION_CONTEXT_VERSION)

    def test_incomplete_context_is_not_saved_as_a_completed_selection(self):
        with mock.patch.object(M.os, "makedirs") as mkdir:
            M._save_selection_result("three_day", 10, {
                "picks": [{"code": "600000"}], "context_pending": ["finance"],
            })
            mkdir.assert_not_called()

    def test_publication_during_selection_cannot_cache_unknown_context_for_60s(self):
        attempted, allow_clear = threading.Event(), threading.Event()
        self.addCleanup(allow_clear.set)
        result_lock = threading.RLock()
        class PausedResultLock:
            def __enter__(self):
                if threading.current_thread().name.startswith("selection-"):
                    attempted.set()
                    allow_clear.wait(2)
                result_lock.acquire()
            def __exit__(self, *args): result_lock.release()
        calls = []
        def select(**kwargs):
            data = M._load_selection_context("finance", lambda: FINANCE)
            if not calls:
                self.assertTrue(attempted.wait(1), "worker did not publish during selection")
            calls.append(data)
            return {"finance": data}
        with mock.patch.object(M, "_SELECT_RESULT_LOCK", PausedResultLock()), \
                mock.patch.object(M, "_selection_signature", return_value=("same factors",)), \
                mock.patch.object(M, "_health_file_signature", return_value=None), \
                mock.patch.object(M, "_select_uncached", side_effect=select), \
                mock.patch.object(M, "_save_selection_result"):
            self.assertFalse(M.select("three_day", 10)["finance"]["data"])
            self.assertEqual(FINANCE, M.select("three_day", 10)["finance"])
            self.assertEqual(FINANCE, M.select("three_day", 10)["finance"])
            self.assertEqual(2, len(calls))
            allow_clear.set()
            self.join_workers()

    def test_thread_start_failure_releases_flag_and_keeps_backoff(self):
        fetch = mock.Mock()
        with mock.patch.object(M.threading, "Thread") as factory:
            factory.return_value.start.side_effect = RuntimeError("thread limit")
            self.assertEqual({}, M._load_selection_context("sentiment", fetch))
            self.assertFalse(M._SELECTION_CONTEXT_STATE["sentiment"]["refreshing"])
            self.assertEqual({}, M._load_selection_context("sentiment", fetch))
            factory.assert_called_once()
            fetch.assert_not_called()

    def test_finance_warmup_uses_the_same_inflight_guard(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow():
            entered.set()
            release.wait(2)
            return FINANCE
        with mock.patch.object(M.dfc, "fetch_finance_latest", side_effect=slow) as fetch:
            M._warm_finance()
            self.assertTrue(entered.wait(1))
            M._load_selection_context("finance", M.dfc.fetch_finance_latest)
            fetch.assert_called_once()
            release.set()
            self.join_workers()


class FinancialProviderCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(M.dfc._mem_cache, {}, clear=True))
        self.stack.enter_context(mock.patch.dict(M.dfc._finance_fetch_state, {"retry_at": 0.}, clear=True))
        self.clock = self.stack.enter_context(mock.patch.object(M.dfc.time, "monotonic", return_value=100.))

    def test_empty_envelope_is_not_cached_for_six_hours(self):
        with mock.patch.object(M.dfc, "_fetch_finance_report", return_value=[]) as fetch:
            for _ in range(2):
                self.assertEqual({"data": {}, "report_dates": [], "annual_report_date": None},
                                 M.dfc.fetch_finance_latest())
                self.assertNotIn("finance", M.dfc._mem_cache)
            self.assertEqual(5, fetch.call_count)
            self.clock.return_value = 130.
            M.dfc.fetch_finance_latest()
            self.assertEqual(10, fetch.call_count)

    def test_valid_report_retains_the_existing_success_cache(self):
        row = dict(FINANCE["data"]["600000"], code="600000")
        with mock.patch.object(M.dfc, "_fetch_finance_report", side_effect=lambda _: [dict(row)]) as fetch:
            first = M.dfc.fetch_finance_latest()
            count = fetch.call_count
            self.assertIs(first, M.dfc.fetch_finance_latest())
            self.assertEqual(count, fetch.call_count)
            self.assertEqual("2026-08-01", first["data"]["600000"]["notice_date"])

    def test_provider_exception_has_a_short_retry_deadline(self):
        with mock.patch.object(M.dfc, "_fetch_finance_report", side_effect=ValueError("invalid report")) as fetch:
            with self.assertRaises(ValueError): M.dfc.fetch_finance_latest()
            self.clock.return_value = 129.99
            self.assertEqual({}, M.dfc.fetch_finance_latest()["data"])
            fetch.assert_called_once()
            self.clock.return_value = 130.
            with self.assertRaises(ValueError): M.dfc.fetch_finance_latest()
            self.assertEqual(2, fetch.call_count)

    def test_concurrent_provider_misses_share_one_empty_attempt(self):
        release, entered = threading.Event(), threading.Event()
        results = []
        def slow(_):
            entered.set()
            release.wait(2)
            return []
        with mock.patch.object(M.dfc, "_fetch_finance_report", side_effect=slow) as fetch:
            readers = [Thread(target=lambda: results.append(M.dfc.fetch_finance_latest())) for _ in range(12)]
            try:
                for reader in readers: reader.start()
                self.assertTrue(entered.wait(1))
                self.assertEqual(1, fetch.call_count)
            finally:
                release.set()
                for reader in readers:
                    reader.join(3)
                    self.assertFalse(reader.is_alive())
            self.assertEqual(12, len(results))
            self.assertTrue(all(row["data"] == {} for row in results))
            self.assertEqual(5, fetch.call_count)


if __name__ == "__main__":
    unittest.main()
