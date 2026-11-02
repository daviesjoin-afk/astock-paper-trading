"""Cron runner retries risk deferrals itself; no manual force/re-entry needed."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

import paper_risk_scan_state as PRSS
import paper_runner as runner
import paper_trading as PT
from test_paper_risk_exit_production_path import PaperRiskExitProductionPathTestCase
from test_paper_risk_lane import REAL_DATETIME


DEFERRED = {"status": "failed", "deferred": True, "retry_reason": "risk_in_progress"}


class RunnerRiskRetryTests(unittest.TestCase):
    def _main(self, slot):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["paper_runner", "--slot", slot]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner.main()
        return code, json.loads(out.getvalue()), json.loads(err.getvalue())

    def test_cron_entry_retries_deferred_result_without_force(self):
        with mock.patch.object(PT, "run_slot", side_effect=[DEFERRED, {"status": "completed"}]) as run, \
                mock.patch.object(runner, "window_run_key", return_value="same-window"), \
                mock.patch.object(runner.time, "sleep") as sleep:
            code, result, event = self._main("execution-resume")
        self.assertEqual(0, code)
        self.assertEqual("completed", result["status"])
        self.assertEqual([mock.call("execution-resume")] * 2, run.call_args_list)
        sleep.assert_called_once_with(10)
        self.assertEqual(1, event["timing"]["stages"]["execution_risk_retry_backoff"]["calls"])

    def test_persistent_deferral_has_only_two_automatic_retries(self):
        with mock.patch.object(PT, "run_slot", return_value=DEFERRED) as run, \
                mock.patch.object(runner, "window_run_key", return_value="same-window"), \
                mock.patch.object(runner.time, "sleep") as sleep:
            code, result, _ = self._main("execution-resume")
        self.assertEqual(1, code)
        self.assertEqual("failed", result["status"])
        self.assertEqual(3, run.call_count)
        self.assertEqual([mock.call(10)] * 2, sleep.call_args_list)

    def test_other_slots_do_not_gain_result_retries(self):
        for slot in ("risk", "intraday", "open"):
            with self.subTest(slot=slot), mock.patch.object(PT, "run_slot", return_value=DEFERRED) as run, \
                    mock.patch.object(runner.time, "sleep") as sleep:
                code, _, _ = self._main(slot)
            self.assertEqual(1, code)
            run.assert_called_once_with(slot)
            sleep.assert_not_called()

    def test_unrelated_result_failures_do_not_retry(self):
        for result in ({"status": "failed"}, {"status": "in_progress"},
                       {"status": "blocked"}, {"status": "completed"},
                       {"status": "failed", "retry_reason": "provider_failure"}):
            with self.subTest(result=result), mock.patch.object(PT, "run_slot", return_value=result) as run, \
                    mock.patch.object(runner, "window_run_key", return_value="same-window"), \
                    mock.patch.object(runner.time, "sleep") as sleep:
                self._main("execution-resume")
            run.assert_called_once_with("execution-resume")
            sleep.assert_not_called()

    def test_business_exception_still_exits_without_a_risk_retry(self):
        with mock.patch.object(PT, "run_slot", side_effect=RuntimeError("paper lease lost: retry test")) as run, \
                mock.patch.object(runner.time, "sleep") as sleep:
            code, result, _ = self._main("execution-resume")
        self.assertEqual(1, code)
        self.assertIn("paper lease lost", result["error"])
        run.assert_called_once()
        sleep.assert_not_called()

    def test_window_change_before_or_during_backoff_stops_retries(self):
        for windows, sleep_count in ((["old", "new"], 0), (["old", "old", "new"], 1)):
            with self.subTest(windows=windows), mock.patch.object(PT, "run_slot", return_value=DEFERRED) as run, \
                    mock.patch.object(runner, "window_run_key", side_effect=windows), \
                    mock.patch.object(runner.time, "sleep") as sleep:
                self.assertEqual(DEFERRED, runner._run_slot_with_risk_retry(PT, "execution-resume"))
            run.assert_called_once()
            self.assertEqual(sleep_count, sleep.call_count)


class RealRunnerRiskRetryTests(PaperRiskExitProductionPathTestCase):
    def _run_waiting_peer(self, *, finish_at, start=None):
        """Real SQLite/risk gate/fill path, with a virtual elapsed-time clock."""
        start = start or REAL_DATETIME(2026, 9, 10, 9, 33)
        elapsed, attempts, backoffs, observed = [0.0], [], [], []
        self._insert_lot("tq_breakout", self.code, 1000, 10.0)
        self._set_fresh_exit_quote(self.code)
        self.quotes_map[self.code].update(
            quote_at=(start + dt.timedelta(seconds=30)).isoformat(sep=" "),
            execution_asof=(start + dt.timedelta(seconds=30)).isoformat(sep=" "))
        with PT._db(immediate=True) as conn:
            stamp = PT._strategy_stamp(conn, "tq_breakout")
            order_id = conn.execute("""INSERT INTO paper_orders(
                account_id,side,code,name,qty,remaining_qty,planned_price,status,
                reason,risk_payload,created_at,strategy_id,strategy_version,strategy_checksum,
                cycle_id,order_type,origin) VALUES(
                'tq_breakout','sell',?,'测试股',1000,1000,9,'pending_execution',
                '已批准等待恢复','{}',?,?,?, ?,1,'market','strategy')""",
                (self.code, start.isoformat(sep=" "), *stamp)).lastrowid
            ident = {"cycle_id": 1, "asof_date": self.day.isoformat(),
                     "scan_minute": PRSS.window_minute(start)}
            self.assertTrue(PRSS.claim_scan(conn, **ident, started_at=start.isoformat(sep=" "))["claimed"])

        class Clock(REAL_DATETIME):
            @classmethod
            def now(cls, tz=None):
                moment = start + dt.timedelta(seconds=elapsed[0])
                return moment if tz is None else moment.replace(tzinfo=tz)

        original_wait, original_run, original_date = PRSS.await_settled, PT.run_slot, PT._date

        def poll_sleep(seconds):
            elapsed[0] += seconds

        def backoff_sleep(seconds):
            # First timeout is a real persisted failure. No execution lease or
            # writer transaction may survive into this runner-owned wait.
            with PT._db(immediate=True) as conn:
                row = conn.execute("SELECT status,detail FROM paper_job_runs WHERE slot='execution-resume'").fetchone()
                observed.append((elapsed[0], row[0], json.loads(row[1])["status"]))
                self.assertEqual("failed", row[0])
                self.assertIsNone(conn.execute("SELECT 1 FROM paper_runtime_locks WHERE lock_key='paper-runner-execution'").fetchone())
            self.assertIsNone(PT._active_lease_context())
            backoffs.append(seconds)
            elapsed[0] += seconds
            if finish_at is not None and elapsed[0] >= finish_at:
                with PT._db(immediate=True) as conn:
                    PRSS.complete_scan(conn, **ident, finished_at=Clock.now().isoformat(sep=" "))

        def run(slot):
            result = original_run(slot)  # production path; no force/asof override
            attempts.append((elapsed[0], result["status"]))
            return result

        output = io.StringIO()
        with mock.patch.object(PT.dt, "datetime", Clock), \
                mock.patch.object(PT, "_date", side_effect=lambda value=None: self.day if value is None else original_date(value)), \
                mock.patch.object(PT, "RISK_RUNNING_WAIT_SECONDS", 20.0), \
                mock.patch.object(PT, "RISK_RUNNING_POLL_SECONDS", 0.25), \
                mock.patch.object(PRSS, "await_settled", side_effect=lambda read, **kw: original_wait(read, clock=lambda: elapsed[0], sleep=poll_sleep, **kw)), \
                mock.patch.object(runner.time, "sleep", side_effect=backoff_sleep), \
                mock.patch.object(PT, "_monitor_risk_impl", side_effect=AssertionError("peer owns risk scan")), \
                mock.patch.object(PT, "run_slot", side_effect=run) as slot_calls, \
                mock.patch("sys.argv", ["paper_runner", "--slot", "execution-resume"]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            exit_code = runner.main()  # exactly one cron-launched runner call
        self.assertEqual([mock.call("execution-resume")] * len(attempts), slot_calls.call_args_list)
        with PT._db() as conn:
            fills = list(conn.execute("SELECT qty FROM paper_fills WHERE order_id=?", (order_id,)))
            risk = PRSS.scan_run(conn, **ident)
        return exit_code, json.loads(output.getvalue()), attempts, backoffs, observed, fills, risk

    def test_risk_finishes_at_30s_after_first_20s_timeout_and_runner_automatically_fills(self):
        code, result, attempts, backoffs, observed, fills, risk = self._run_waiting_peer(finish_at=30)
        self.assertEqual(0, code)
        self.assertEqual([(20.0, "failed"), (30.0, "completed")], attempts)
        self.assertEqual([(20.0, "failed", "risk_in_progress")], observed)
        self.assertEqual([10], backoffs)
        self.assertEqual("completed", result["status"])
        self.assertEqual("filled", result["execution_orders"][0]["status"])
        self.assertEqual([1000], [row[0] for row in fills])
        self.assertEqual("completed", risk["status"])
        self.assertEqual(1, risk["attempt"])

    def test_persistently_running_risk_stops_at_80s_without_any_fill(self):
        code, result, attempts, backoffs, _, fills, risk = self._run_waiting_peer(finish_at=None)
        self.assertEqual(1, code)
        self.assertEqual([(20.0, "failed"), (50.0, "failed"), (80.0, "failed")], attempts)
        self.assertEqual([10, 10], backoffs)
        self.assertEqual("failed", result["status"])
        self.assertEqual([], fills)
        self.assertEqual("running", risk["status"])

    def test_timeout_that_crosses_the_window_does_not_chase_new_risk(self):
        code, _, attempts, backoffs, _, fills, _ = self._run_waiting_peer(
            finish_at=30, start=REAL_DATETIME(2026, 9, 10, 9, 35, 50))
        self.assertEqual(1, code)
        self.assertEqual([(20.0, "failed")], attempts)
        self.assertEqual([], backoffs)
        self.assertEqual([], fills)


if __name__ == "__main__":
    unittest.main()
