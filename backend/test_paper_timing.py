"""计时必须能定位等待、保留业务异常，并与真实任务台账关联。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import paper_runner as runner
import paper_storage as storage
import paper_timing as timing


class TimingTests(unittest.TestCase):
    def test_snapshot_diagnostic_failure_cannot_skip_the_risk_pass(self):
        import datetime as dt
        from contextlib import ExitStack
        import paper_trading as paper
        gate = {"ready": False, "covered_codes": 0, "eligible_codes": 5000,
                "coverage_pct": 0.0, "required_codes": 4500}
        with ExitStack() as stack:
            for name, value in (("init_db", None), ("_cleanup_stale_data", None),
                                ("_validated_live_universe", []), ("_live_scan_gate", gate),
                                ("_ignition_shadow_backfill", None), ("_active_account_rows", []),
                                ("_market_state", {}), ("_active_cycle", {"id": 1}),
                                ("_cached_close_market", {}), ("process_pending_execution_orders", [])):
                stack.enter_context(mock.patch.object(paper, name, return_value=value))
            stack.enter_context(mock.patch.object(paper, "_db"))
            stack.enter_context(mock.patch.object(paper.dfc, "check_data_source_health", return_value={}))
            stack.enter_context(mock.patch.object(paper.MDSvc, "refresh_rows", return_value=[]))
            stack.enter_context(mock.patch.object(paper.MDSvc, "read_snapshot", side_effect=ValueError))
            stack.enter_context(mock.patch.object(paper.dfc, "full_market_snapshot_source", side_effect=ValueError))
            risk = stack.enter_context(mock.patch.object(paper, "monitor_risk", return_value={"orders": []}))
            result = paper.monitor_intraday(dt.datetime(2026, 10, 20, 10, 0), force=True)
        risk.assert_called_once()
        self.assertEqual(result["bootstrap"]["status"], "blocked")

    def test_explicit_worker_binding_collects_timing_without_other_contexts(self):
        import contextvars
        from concurrent.futures import ThreadPoolExecutor
        private = contextvars.ContextVar("private_lease", default=None)
        token = private.set("must-stay-in-owner")
        def worker():
            with timing.phase("microstructure.depth"):
                return private.get()
        try:
            with contextlib.redirect_stderr(io.StringIO()), timing.capture("intraday"):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(timing.bind_worker(worker)) for _ in range(40)]
                    self.assertEqual([future.result() for future in futures], [None] * 40)
                snap = timing.snapshot("test")
                self.assertEqual(snap["stages"]["microstructure.depth"]["calls"], 40)
                self.assertIn("+08:00", snap["started_at"])
                self.assertIn("runtime_commit", snap)
        finally:
            private.reset(token)

    def test_quote_phases_keep_same_source_guard_and_failure_evidence(self):
        import datetime as dt
        import paper_trading as paper
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        quote = {"code": "600001", "price": 10.0, "pct": 1.0,
                 "quote_at": stamp, "source": "tencent_public_quote"}
        output = io.StringIO()
        with mock.patch.object(paper.dfc, "fetch_realtime_for_codes", return_value=[quote]), \
                mock.patch.object(paper.dfc, "enrich_live_flow_details", return_value={}), \
                mock.patch.object(paper.dfc, "fetch_independent_realtime_for_codes", return_value=[quote]), \
                mock.patch.object(paper.dfc, "_fetch_sina_realtime_for_codes",
                                  side_effect=RuntimeError("private-marker")), \
                mock.patch.object(paper, "_latest_price_map", return_value={}), \
                mock.patch.object(paper, "_universe_snapshot_time", return_value=None), \
                mock.patch.object(paper.PLTrad, "prefetch", return_value={}), \
                contextlib.redirect_stderr(output), timing.capture("intraday"):
            row = paper._quotes(["600001"], asof_date=dt.date.today())["600001"]
            stages = timing.snapshot("test")["stages"]
        self.assertNotEqual(row["quote_validation"], "cross_source_checked")
        self.assertFalse(row["quote_cross_check"]["passed"])
        self.assertIn("private-marker", row["quote_source_errors"]["sina"])
        for name in ("primary", "flow", "independent", "same_source_fallback", "local_metadata", "tradability"):
            self.assertEqual(stages["quote." + name]["calls"], 1)
        self.assertEqual(stages["quote.same_source_fallback"]["errors"], 1)
        self.assertNotIn("private-marker", output.getvalue())

    def test_open_failure_keeps_risk_first_and_records_guard_boundaries(self):
        import paper_trading as paper
        order = []
        gate = {"ready": False, "covered_codes": 0, "eligible_codes": 5000,
                "coverage_pct": 0.0, "required_codes": 4500}
        with mock.patch.object(paper, "init_db"), \
                mock.patch.object(paper, "monitor_risk", side_effect=lambda *_: order.append("risk") or {}), \
                mock.patch.object(paper, "process_pending_execution_orders", return_value=[]), \
                mock.patch.object(paper.dfc, "check_data_source_health",
                                  side_effect=lambda **_: order.append("health") or {}), \
                mock.patch.object(paper, "_entry_freeze_status"), \
                mock.patch.object(paper.MDSvc, "refresh_rows", return_value=[]), \
                mock.patch.object(paper, "_live_scan_gate", return_value=gate), \
                mock.patch.object(paper, "process_pending_manual_orders") as manual, \
                contextlib.redirect_stderr(io.StringIO()), timing.capture("open"):
            result = paper.execute_open("2026-10-19")
            stages = timing.snapshot("test")["stages"]
        self.assertEqual(order, ["risk", "health"])
        self.assertEqual(result["status"], "blocked")
        manual.assert_not_called()
        for name in ("source_health", "market_snapshot", "coverage_gate"):
            self.assertEqual(stages["execution." + name]["calls"], 1)

    def test_inactive_has_no_clock_reads_or_output(self):
        with mock.patch.object(timing.time, "perf_counter", side_effect=AssertionError), \
                mock.patch.object(timing.time, "thread_time", side_effect=AssertionError), \
                mock.patch("builtins.print") as output:
            with timing.phase("unused"):
                pass
            self.assertEqual(timing.snapshot("unused"), {})
        output.assert_not_called()

    def test_nested_stages_are_inclusive_and_calls_accumulate(self):
        clock = [10.0]
        output = io.StringIO()
        with mock.patch.object(timing.time, "perf_counter", side_effect=lambda: clock[0]), \
                mock.patch.object(timing.time, "thread_time", side_effect=lambda: clock[0] / 2), \
                contextlib.redirect_stderr(output), timing.capture("intraday"):
            with timing.phase("scan"):
                clock[0] += 1
                for _ in range(2):
                    with timing.phase("quote_batch"):
                        clock[0] += 2
            value = timing.snapshot("test")
        self.assertEqual(value["elapsed_seconds"], 5)
        self.assertEqual(value["stages"]["scan"]["seconds"], 5)
        self.assertEqual(value["stages"]["quote_batch"], {
            "seconds": 4, "thread_cpu_seconds": 2, "calls": 2, "errors": 0,
        })
        self.assertEqual(value["stages"]["scan"]["thread_cpu_seconds"], 2.5)
        self.assertEqual(json.loads(output.getvalue())["timing"]["scope"], "finished")

    def test_wait_and_cpu_are_distinct_and_decorated_result_is_unchanged(self):
        wall, cpu = [0.0], [0.0]
        result = {"status": "unknown", "evidence": None}

        @timing.timed("candidate.strategy")
        def compute():
            wall[0] += 2
            cpu[0] += 0.5
            return result

        with mock.patch.object(timing.time, "perf_counter", side_effect=lambda: wall[0]), \
                mock.patch.object(timing.time, "thread_time", side_effect=lambda: cpu[0]), \
                contextlib.redirect_stderr(io.StringIO()), timing.capture("intraday"):
            self.assertIs(compute(), result)
            with timing.phase("candidate.quote_source"):
                wall[0] += 8
            stages = timing.snapshot("test")["stages"]
        self.assertEqual(stages["candidate.strategy"]["thread_cpu_seconds"], 0.5)
        self.assertEqual(stages["candidate.quote_source"]["seconds"], 8)
        self.assertEqual(stages["candidate.quote_source"]["thread_cpu_seconds"], 0)

    def test_http_attempts_keep_retry_and_failure_semantics_without_private_logs(self):
        import marketdata_transport as transport
        import requests
        session = mock.Mock()
        response = mock.Mock(text="payload")
        session.get.side_effect = [requests.Timeout("private-marker"), response]
        response.raise_for_status.return_value = None
        output = io.StringIO()
        with mock.patch.object(transport, "_session", return_value=session), \
                mock.patch.object(transport.time, "sleep") as sleep, \
                contextlib.redirect_stderr(output), timing.capture("intraday"):
            self.assertEqual(transport.http_get("private-marker", params={"private": "marker"},
                                              timeout=3, retries=1), "payload")
            stages = timing.snapshot("test")["stages"]
        self.assertEqual(session.get.call_args_list, [
            mock.call("private-marker", params={"private": "marker"}, timeout=3),
        ] * 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(stages["http_get_attempt"]["calls"], 2)
        self.assertEqual(stages["http_get_attempt"]["errors"], 1)
        self.assertEqual(stages["http_retry_backoff"]["calls"], 1)
        self.assertNotIn("private-marker", output.getvalue())
        session.post.side_effect = requests.Timeout("private-marker")
        with mock.patch.object(transport, "_session", return_value=session), \
                contextlib.redirect_stderr(io.StringIO()), timing.capture("intraday"):
            self.assertEqual(transport.http_post_json("private-marker", {}), {})
            self.assertEqual(timing.snapshot("test")["stages"]["http_post_attempt"]["errors"], 1)

    def test_failure_records_stage_and_does_not_log_exception_contents(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(ValueError, "private-marker"):
                with timing.capture("risk"), timing.phase("risk_pass"):
                    raise ValueError("private-marker")
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["timing"]["stages"]["risk_pass"]["errors"], 1)
        self.assertNotIn("private-marker", output.getvalue())
        self.assertEqual(timing.snapshot("after"), {})

    def test_log_failure_does_not_change_result_or_leak_context(self):
        @timing.traced_slot
        def work(slot, asof_date=None, force=False):
            return {"status": "completed", "orders": [1]}

        with mock.patch("builtins.print", side_effect=BrokenPipeError):
            result = work("open")
        self.assertEqual(result["orders"], [1])
        self.assertEqual(timing.snapshot("after"), {})

    def test_capture_isolated_between_threads_and_reused_only_in_call_chain(self):
        output = io.StringIO()
        other = []
        with contextlib.redirect_stderr(output), timing.capture("open"):
            first = timing.snapshot("outer")["attempt_id"]
            with timing.capture("risk"):
                self.assertEqual(first, timing.snapshot("inner")["attempt_id"])
            thread = threading.Thread(target=lambda: other.append(timing.snapshot("other")))
            thread.start()
            thread.join(timeout=2)
        self.assertEqual(other, [{}])
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_host_wait_missing_or_invalid_is_unknown_not_zero(self):
        with contextlib.redirect_stderr(io.StringIO()), timing.capture("risk"):
            for value in (None, "nan", "-1", "86401"):
                with mock.patch.dict(os.environ, {}, clear=True):
                    if value is not None:
                        os.environ["ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS"] = value
                    self.assertNotIn("host_paper_lock_wait_seconds", timing.snapshot("test"))
            with mock.patch.dict(os.environ, {"ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS": "12"}):
                self.assertEqual(timing.snapshot("test")["host_paper_lock_wait_seconds"], 12)

    def test_real_sqlite_commit_and_rollback_keep_semantics(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), \
                timing.capture("risk"):
            path = Path(tmp) / "ledger.db"
            with storage.db(path, immediate=True) as conn:
                conn.execute("CREATE TABLE sample(value)")
                conn.execute("INSERT INTO sample VALUES (1)")
            with self.assertRaisesRegex(ValueError, "rollback"):
                with storage.db(path, immediate=True) as conn:
                    conn.execute("INSERT INTO sample VALUES (2)")
                    raise ValueError("rollback")
            with storage.db_readonly(path) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchall()[0][0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 1)
            stages = timing.snapshot("test")["stages"]
        self.assertEqual(stages["db_acquire"]["calls"], 2)
        self.assertEqual(stages["db_commit"]["calls"], 1)
        self.assertEqual(stages["db_transaction_body"]["errors"], 1)

    def test_runner_retries_busy_error_and_preserves_scheduler_exit_status(self):
        import paper_trading as paper
        calls = [sqlite3.OperationalError("database is locked"), {"status": "blocked"}]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(paper, "run_slot", side_effect=calls), \
                mock.patch.object(runner.time, "sleep"), \
                mock.patch("sys.argv", ["paper_runner", "--slot", "intraday"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = runner.main()
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "blocked")
        value = json.loads(err.getvalue())
        self.assertEqual(value["status"], "blocked")
        self.assertEqual(value["timing"]["stages"]["engine_import"]["calls"], 2)
        self.assertEqual(value["timing"]["stages"]["sqlite_retry_backoff"]["calls"], 1)

    def test_sqlite_writer_wait_is_measured_in_acquire_not_commit(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            path = Path(tmp) / "busy.db"
            with storage.db(path) as conn:
                conn.execute("CREATE TABLE sample(value)")
            owner = sqlite3.connect(path, check_same_thread=False)
            owner.execute("BEGIN IMMEDIATE")
            release = threading.Timer(0.15, owner.rollback)
            release.start()
            try:
                with timing.capture("risk"):
                    with storage.db(path, immediate=True, hot_path=True) as conn:
                        conn.execute("INSERT INTO sample VALUES (1)")
                    stages = timing.snapshot("test")["stages"]
                self.assertGreater(stages["db_acquire"]["seconds"], 0.05)
                self.assertLess(stages["db_commit"]["seconds"], stages["db_acquire"]["seconds"])
            finally:
                release.join(timeout=2)
                owner.close()


class SlotLedgerTimingTests(unittest.TestCase):
    def setUp(self):
        import paper_timing as timing
        import paper_trading as paper
        # The unittest process is shared with earlier modules. A stale outer
        # capture must not suppress this test's own slot timing record.
        timing._CURRENT.set(None)
        self.paper = paper
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.object(paper, "DB_PATH", str(Path(directory) / "paper.db")))
        self.stack.enter_context(mock.patch.object(paper, "_RUNNER_BOOT_RECOVERED", False))
        self.stack.enter_context(mock.patch.object(paper, "_PERFORMANCE_INDEXES_READY", False))
        self.stack.enter_context(mock.patch.object(paper.PSS, "run_preflight", return_value={}))
        self.output = self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def _job(self):
        with self.paper._db_readonly() as conn:
            row = dict(conn.execute("SELECT * FROM paper_jobs WHERE slot='risk'").fetchone())
        row["detail"] = json.loads(row["detail"])
        return row

    def test_real_slot_persists_same_trace_and_keeps_idempotency(self):
        paper = self.paper
        risk = timing.timed("risk_pass")(lambda *_: {"orders": []})
        refresh = timing.timed("risk_snapshot")(lambda **_: {"status": "completed"})
        with mock.patch.object(paper, "monitor_risk", side_effect=risk) as work, \
                mock.patch.object(paper, "request_risk_snapshot_refresh", side_effect=refresh):
            first = paper.run_slot("risk", asof_date="2026-10-16", force=True)
            saved = self._job()
            second = paper.run_slot("risk", asof_date="2026-10-16", force=True)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["detail"]["orders"], [])
        before = saved["detail"]["timing"]
        self.assertEqual(before["scope"], "before_job_finalize")
        self.assertEqual(before["attempt_id"], first["timing"]["attempt_id"])
        self.assertEqual(before["identity"]["market_date"], "2026-10-16")
        self.assertIn("risk_pass", before["stages"])
        self.assertNotIn("risk_snapshot", before["stages"])
        self.assertIn("risk_snapshot", first["timing"]["stages"])
        self.assertEqual(second["status"], "already_done")
        self.assertNotEqual(first["timing"]["attempt_id"], second["timing"]["attempt_id"])
        self.assertEqual(work.call_count, 1)
        self.assertGreaterEqual(first["timing"]["elapsed_seconds"], before["elapsed_seconds"])

    def test_failure_persists_diagnostics_and_preserves_original_exception(self):
        paper = self.paper
        # The full suite shares sys.stderr across modules. Assert the emitted
        # event at the print boundary instead of depending on global redirection;
        # TimingTests separately covers the real stderr contract.
        with mock.patch.object(paper, "monitor_risk", side_effect=RuntimeError("original-failure")), \
                mock.patch("builtins.print") as emitted:
            with self.assertRaisesRegex(RuntimeError, "original-failure"):
                paper.run_slot("risk", asof_date="2026-10-16", force=True)
        saved = self._job()
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["detail"]["error"], "original-failure")
        self.assertEqual(saved["detail"]["timing"]["scope"], "before_failure_finalize")
        events = [
            json.loads(call.args[0]) for call in emitted.call_args_list
            if call.args and isinstance(call.args[0], str)
            and call.args[0].startswith('{"event": "paper_slot_timing"')
        ]
        self.assertEqual(len(events), 1, emitted.call_args_list)
        self.assertEqual(events[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
