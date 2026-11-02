"""P11-C: isolated recovery, risk ordering, fencing and frozen fill parity."""
from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest import mock

import paper_dispatch_metrics as PDM
import paper_runner as runner
import paper_schedule_contract as PSch
import paper_slot_service as PSS
import paper_trading as PT
from test_paper_risk_exit_production_path import PaperRiskExitProductionPathTestCase
from test_paper_risk_lane import REAL_DATETIME, _fixed_clock


class ExecutionLaneContractTests(unittest.TestCase):
    def test_lane_is_scheduled_hot_and_independent_of_both_other_locks(self):
        self.assertIn("execution-resume", PSch.RUN_SLOTS)
        self.assertIn("execution-resume", PSch.SCHEDULED_SLOTS)
        self.assertTrue(PSch.is_hot_path("execution-resume"))
        self.assertEqual("execution-scan.lock", PSch.host_lock_name("execution-resume"))
        self.assertFalse(PSch.joins_full_scan_intent("execution-resume"))
        self.assertEqual(PSch.INTRADAY_CRON_MINUTES, PSch.REPEAT_CRON_MINUTES["execution-resume"])

    def test_window_boundary_and_lane_identities(self):
        for minute, expected in ((48, "1448"), (50, "1448"), (51, "1451")):
            moment = REAL_DATETIME(2026, 9, 10, 14, minute)
            keys = {PT._slot_run_key(slot, moment) for slot in ("risk", "intraday", "execution-resume")}
            self.assertEqual(3, len(keys))
            self.assertIn(f"execution-resume:20260910{expected}", keys)

    def test_unfinished_risk_never_enters_resume(self):
        for status in ("risk_running", "risk_unavailable", "failed", "risk_in_progress"):
            resume = mock.Mock()
            result = PSS.run_execution_resume(
                REAL_DATETIME(2026, 9, 10).date(),
                monitor_risk=lambda _, status=status: {"status": status, "deferred": True}, resume_orders=resume)
            self.assertEqual("risk_in_progress", result["status"])
            self.assertNotEqual(0, runner._result_exit_code(result))
            resume.assert_not_called()

    def test_risk_and_resume_failures_propagate(self):
        for target in ("risk", "resume"):
            error = RuntimeError("paper lease lost: execution test")
            risk = mock.Mock(return_value={"orders": []})
            resume = mock.Mock(return_value=[])
            (risk if target == "risk" else resume).side_effect = error
            with self.assertRaises(RuntimeError) as caught:
                PSS.run_execution_resume(REAL_DATETIME(2026, 9, 10).date(),
                                         monitor_risk=risk, resume_orders=resume)
            self.assertIs(error, caught.exception)
            if target == "risk":
                resume.assert_not_called()

    def test_recovery_function_is_frozen_and_only_the_new_slot_dispatches_it(self):
        frozen = Path(__file__).with_name("fixtures") / "execution_resume_b8b3875.txt"
        before = ast.parse(frozen.read_text(encoding="utf-8")).body[0]
        current = ast.parse(inspect.getsource(PT.process_pending_execution_orders.__wrapped__)).body[0]
        current.decorator_list = []
        self.assertEqual(ast.dump(before), ast.dump(current))
        for fn in (PT.execute_open, PT.monitor_intraday):
            source = inspect.getsource(fn)
            self.assertNotIn("process_pending_execution_orders(", source)

    def test_successful_risk_scan_in_execution_lane_has_its_own_source(self):
        event = {"event": "paper_slot_timing", "status": "completed", "timing": {
            "slot": "execution-resume", "started_at": "2026-09-10T14:48:00",
            "stages": {"risk_pass": {"errors": 0}},
            "risk_scans": [{"status": "completed"}],
        }}
        summary = PDM.summarize([event])
        self.assertEqual("execution_resume_risk_phase", summary["last_successful_risk_scan"]["source"])
        self.assertEqual({"execution-resume": {"completed": 1}}, summary["risk_scans"]["by_slot"])


class ExecutionLaneRuntimeTests(PaperRiskExitProductionPathTestCase):
    def _run_lane(self, *, moment=None):
        clock = _fixed_clock(moment or REAL_DATETIME(2026, 9, 10, 14, 50))
        with clock, contextlib.redirect_stderr(io.StringIO()):
            return PT.run_slot("execution-resume", asof_date=self.day, force=True)

    def test_global_lease_does_not_block_and_same_window_runs_once(self):
        with PT._db(immediate=True) as conn:
            self.assertTrue(PT._claim_runtime_lease(
                conn, "paper-runner-global", "candidate-holder", "intraday", ttl_seconds=2400)[0])
        calls = []
        def resume(day):
            calls.append(day)
            with PT._db() as conn:
                PT._require_active_lease(conn, "test execution fence")
                self.assertEqual("paper-runner-execution", PT._active_lease_context()["lock_key"])
            return [{"order_id": 7, "status": "partially_filled"}]
        with mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT, "process_pending_execution_orders", side_effect=resume), \
                mock.patch.object(PT.MDSvc, "refresh_rows", side_effect=AssertionError("candidate scan")):
            self.assertEqual("completed", self._run_lane()["status"])
            self.assertEqual("already_done", self._run_lane()["status"])
            self.assertEqual("completed", self._run_lane(moment=REAL_DATETIME(2026, 9, 10, 14, 51))["status"])
        self.assertEqual(2, len(calls))
        with PT._db() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM paper_runtime_locks WHERE lock_key='paper-runner-global'").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM paper_runtime_locks WHERE lock_key='paper-runner-execution'").fetchone())

    def test_risk_gate_failure_is_retryable_in_the_same_window(self):
        with mock.patch.object(PT, "monitor_risk", side_effect=[
                {"status": "risk_running", "deferred": True}, {"orders": []}]), \
                mock.patch.object(PT, "process_pending_execution_orders", return_value=[]) as resume:
            first = self._run_lane()
            self.assertEqual("failed", first["status"])
            resume.assert_not_called()
            self.assertEqual("completed", self._run_lane()["status"])
            resume.assert_called_once()

    def test_resume_error_records_failure_releases_lease_and_retries(self):
        with mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT, "process_pending_execution_orders", side_effect=[RuntimeError("provider"), []]):
            with self.assertRaisesRegex(RuntimeError, "provider"):
                self._run_lane()
            with PT._db() as conn:
                self.assertEqual("failed", conn.execute("SELECT status FROM paper_job_runs WHERE slot='execution-resume'").fetchone()[0])
                self.assertIsNone(conn.execute("SELECT 1 FROM paper_runtime_locks WHERE lock_key='paper-runner-execution'").fetchone())
            self.assertEqual("completed", self._run_lane()["status"])

    def test_concurrent_and_adjacent_window_cannot_enter_the_same_running_batch(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def resume(_):
            entered.set()
            if not release.wait(10):
                raise AssertionError("test barrier timeout")
            return []
        def worker():
            try:
                results.append(PT.run_slot("execution-resume", asof_date=self.day, force=True))
            except BaseException as exc:
                errors.append(exc)
        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 50)), \
                mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT, "process_pending_execution_orders", side_effect=resume) as recovered, \
                contextlib.redirect_stderr(io.StringIO()):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                repeated = PT.run_slot("execution-resume", asof_date=self.day, force=True)
                self.assertEqual("in_progress", repeated["status"])
                self.assertEqual(1, runner._result_exit_code(repeated))
                # A different run key still uses the same lane lease.
                with mock.patch.object(PT, "_slot_run_key", return_value="execution-resume:202609101451"):
                    self.assertEqual("in_progress", PT.run_slot("execution-resume", asof_date=self.day, force=True)["status"])
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, recovered.call_count)
            self.assertEqual("completed", results[0]["status"])

    def test_lost_lease_before_execution_write_is_not_swallowed(self):
        def stolen(_):
            with PT._db(immediate=True) as conn:
                conn.execute("UPDATE paper_runtime_locks SET owner_key='new-owner',fencing_token=fencing_token+1 WHERE lock_key='paper-runner-execution'")
            return []
        with mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT, "process_pending_execution_orders", side_effect=stolen):
            with self.assertRaisesRegex(RuntimeError, "paper lease lost"):
                self._run_lane()
        with PT._db() as conn:
            self.assertEqual("new-owner", conn.execute("SELECT owner_key FROM paper_runtime_locks WHERE lock_key='paper-runner-execution'").fetchone()[0])

    def test_exhausted_failure_never_becomes_an_idempotent_success(self):
        with mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT, "process_pending_execution_orders", side_effect=RuntimeError("provider")):
            with self.assertRaisesRegex(RuntimeError, "provider"):
                self._run_lane()
        with PT._db(immediate=True) as conn:
            conn.execute("UPDATE paper_job_runs SET detail=? WHERE slot='execution-resume'",
                         (json.dumps({"retry_count": 2}),))
        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 50)), \
                mock.patch.object(PT, "process_pending_execution_orders") as resume, \
                contextlib.redirect_stderr(io.StringIO()):
            result = PT.run_slot("execution-resume", asof_date=self.day)
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, runner._result_exit_code(result))
        resume.assert_not_called()

    def test_real_risk_completion_is_observable_in_the_execution_lane(self):
        output = io.StringIO()
        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 50)), contextlib.redirect_stderr(output):
            result = PT.run_slot("execution-resume", asof_date=self.day, force=True)
        self.assertEqual("completed", result["status"])
        events = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        summary = PDM.summarize(events)
        self.assertEqual("execution_resume_risk_phase", summary["last_successful_risk_scan"]["source"])

    def test_partial_fill_same_event_and_new_quote_across_windows(self):
        self._insert_lot("tq_breakout", self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code)
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, "tq_breakout")
            order_id = conn.execute("""INSERT INTO paper_orders(
                account_id,side,code,name,qty,remaining_qty,planned_price,status,
                reason,risk_payload,created_at,strategy_id,strategy_version,strategy_checksum,
                cycle_id,order_type,origin) VALUES(
                'tq_breakout','sell',?,'测试股',1000,1000,9,'pending_execution',
                '原始批准原因','{}','2026-09-10 14:40:00',?,?,?,1,'market','strategy')""",
                (self.code, *stamp)).lastrowid
        quote = self.quotes_map[self.code]
        quote["amount"] = quote["price"] * 30_100
        with mock.patch.object(PT, "monitor_risk", return_value={"orders": []}):
            first = self._run_lane()
            self.assertEqual(300, first["execution_orders"][0]["filled_qty"])
            self.assertEqual("already_done", self._run_lane()["status"])
            duplicate = self._run_lane(moment=REAL_DATETIME(2026, 9, 10, 14, 51))
            self.assertEqual(300, duplicate["execution_orders"][0]["filled_qty"])
            quote.update(amount=quote["price"] * 130_000,
                         quote_at="2026-09-10 14:54:00", execution_asof="2026-09-10 14:54:00")
            final = self._run_lane(moment=REAL_DATETIME(2026, 9, 10, 14, 54))
            self.assertEqual("filled", final["execution_orders"][0]["status"])
            self.assertEqual(1000, final["execution_orders"][0]["filled_qty"])
        with PT._db() as conn:
            fills = list(conn.execute("SELECT qty FROM paper_fills WHERE order_id=?", (order_id,)))
            self.assertEqual([300, 700], [row[0] for row in fills])
            self.assertEqual("原始批准原因", conn.execute("SELECT reason FROM paper_orders WHERE id=?", (order_id,)).fetchone()[0])

    def test_real_fill_rows_match_frozen_recovery_on_cloned_ledgers(self):
        self._insert_lot("tq_breakout", self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code)
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, "tq_breakout")
            conn.execute("""INSERT INTO paper_orders(
                account_id,side,code,name,qty,remaining_qty,planned_price,status,
                reason,risk_payload,created_at,strategy_id,strategy_version,strategy_checksum,
                cycle_id,order_type,origin) VALUES(
                'tq_breakout','sell',?,'测试股',1000,1000,9,'pending_execution',
                '原始批准原因','{}','2026-09-10 14:40:00',?,?,?,1,'market','strategy')""",
                (self.code, *stamp))
        cloned = str(Path(self.tmp_dir) / "lane.sqlite3")
        with contextlib.closing(sqlite3.connect(self.db_path)) as source, \
                contextlib.closing(sqlite3.connect(cloned)) as target:
            source.backup(target)
        frozen_source = (Path(__file__).with_name("fixtures") / "execution_resume_b8b3875.txt").read_text(encoding="utf-8")
        namespace = dict(PT.__dict__)
        exec(compile(frozen_source, "frozen_b8b3875", "exec"), namespace)
        tables = ("paper_orders", "paper_fills", "paper_positions", "paper_position_lots",
                  "paper_accounts", "paper_signals", "paper_capital_reservations", "paper_risk_decisions", "paper_audit")
        def rows():
            with PT._db() as conn:
                return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
        original_quotes = PT._quotes
        def fresh(codes, asof_date=None):
            # Quote I/O must not hold a writer transaction.
            with PT._db(immediate=True):
                pass
            return original_quotes(codes, asof_date=asof_date)
        try:
            with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 50)), \
                    mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                    mock.patch.object(PT, "_quotes", side_effect=fresh):
                expected_result = namespace["process_pending_execution_orders"](self.day)
                expected_rows = rows()
                PT.DB_PATH = cloned
                with contextlib.redirect_stderr(io.StringIO()):
                    result = PT.run_slot("execution-resume", asof_date=self.day, force=True)
                self.assertEqual("completed", result["status"])
                self.assertEqual(expected_result, result["execution_orders"])
                self.assertEqual(expected_rows, rows())
                self.assertEqual("filled", expected_result[0]["status"])
        finally:
            PT.DB_PATH = self.db_path


if __name__ == "__main__":
    unittest.main()
