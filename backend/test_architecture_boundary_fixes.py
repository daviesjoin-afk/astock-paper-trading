"""Behavior regressions for read-only queries and refresh process lifetime."""
import ast
import contextlib
import datetime as dt
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import api_paper as API
import dashboard_queries as DQ
import paper_runner as Runner
import paper_trading as P


class DashboardBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(P, "DB_PATH", os.path.join(directory.name, "ledger.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        P.init_db()
        self.addCleanup(P._schedule_cache.update, data=None, ts=0)
        P._schedule_cache.update(data={"installed": False}, ts=__import__("time").time())

    def test_dashboard_missing_sold_price_is_network_free_and_readonly(self):
        today = dt.date.today().isoformat()
        with P._db() as conn:
            account = dict(conn.execute("SELECT * FROM paper_accounts LIMIT 1").fetchone())
            stamp = P._strategy_stamp(conn, account["id"])
            conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,qty,planned_price,status,created_at,executed_at,execution_status,execution_verified,realized_pnl,cycle_id,strategy_id,strategy_version,strategy_checksum,risk_payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account["id"], "sell", "000001", 100, 10.0, "filled", today, today, "verified", 1, 5.0, account["cycle_id"], *stamp, "{}"),
            )
        real_metrics = P._account_metrics
        connections = []

        def metrics(conn, *args, **kwargs):
            connections.append(conn.execute("PRAGMA query_only").fetchone()[0])
            self.assertFalse(kwargs["allow_network"])
            return real_metrics(conn, *args, **kwargs)

        with mock.patch.object(P, "_quotes", side_effect=AssertionError("query requested provider")), \
                mock.patch.object(P, "_latest_price_map", return_value={}), \
                mock.patch.object(P.dfc, "load_market_snapshot_full_cached", return_value=[]), \
                mock.patch.object(P, "_account_metrics", side_effect=metrics), \
                mock.patch.object(P, "_ensure_cycle", side_effect=AssertionError("query created cycle")), \
                mock.patch.object(P.MDSvc, "read_projection", return_value={}), \
                mock.patch.object(P, "_db", side_effect=AssertionError("query opened writer")):
            result = P.dashboard()
        self.assertTrue(connections)
        self.assertEqual(set(connections), {1})
        self.assertIn("accounts", result)

    def test_missing_cycle_is_unavailable_without_creation(self):
        with P._db() as conn:
            conn.execute("UPDATE paper_cycles SET status='archived'")
        with mock.patch.object(P, "_ensure_cycle", side_effect=AssertionError("query created cycle")):
            with self.assertRaises(DQ.DashboardUnavailable):
                P.dashboard()
        with P._db_readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_cycles WHERE status != 'archived'").fetchone()[0], 0)

    def test_schedule_status_uses_readonly_connection(self):
        with mock.patch.object(P, "_db", side_effect=AssertionError("query opened writer")), \
                mock.patch.object(P.subprocess, "run", return_value=mock.Mock(returncode=1)):
            # The operator test module replaces P.schedule_status globally
            # during discovery to prevent host scheduler access. Exercise the
            # real function body with the current injected DB/transport ports.
            tree = ast.parse(Path(P.__file__).read_text(encoding="utf-8"))
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "schedule_status")
            namespace = dict(vars(P))
            exec(compile(ast.Module(body=[function], type_ignores=[]), P.__file__, "exec"), namespace)
            self.assertIn("runtime_status", namespace["schedule_status"]())

    def test_real_slot_releases_lease_and_retry_does_not_rerun_trading(self):
        def refresh(**kwargs):
            self.assertIsNone(P._active_lease_context())
            with P._db_readonly() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_runtime_locks").fetchone()[0], 0)
            return {"status": "failed", "error": "source failed"}

        with mock.patch.object(P, "monitor_risk", return_value={"slot": "risk"}) as trade, \
                mock.patch.object(P, "request_risk_snapshot_refresh", side_effect=refresh) as snapshot, \
                mock.patch.object(P.RC, "load_snapshot", return_value=None):
            first = P.run_slot("risk", force=True, risk_snapshot_background=False)
            retry = P.run_slot("risk", risk_snapshot_background=False)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(retry["status"], "already_done")
        self.assertEqual(Runner._result_exit_code(first), 1)
        self.assertEqual(Runner._result_exit_code(retry), 1)
        trade.assert_called_once()
        self.assertEqual(snapshot.call_count, 2)


class CacheDeadlineTests(unittest.TestCase):
    def setUp(self):
        API._cclear()
        self.addCleanup(API._cclear)
        patcher = mock.patch.object(API, "_current_cache_generation", return_value="test")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeout_waits_once_without_replacing_owner(self):
        event = mock.Mock()
        event.wait.return_value = False
        flight = {"event": event, "result": API._CACHE_MISS, "error": None}
        API._cache_inflight["blocked"] = flight
        self.addCleanup(API._cache_inflight.pop, "blocked", None)
        loader = mock.Mock()
        with self.assertRaises(API.HTTPException) as caught:
            API._cache_load("blocked", loader)
        self.assertEqual(caught.exception.status_code, 503)
        event.wait.assert_called_once_with(timeout=API.READ_MODEL_WAIT_SECONDS)
        loader.assert_not_called()
        self.assertIs(API._cache_inflight["blocked"], flight)

    def test_owner_error_is_shared_and_next_request_can_retry(self):
        failure = ValueError("owner failed")
        flight = {"event": mock.Mock(), "result": API._CACHE_MISS, "error": failure}
        API._cache_inflight["failed"] = flight
        try:
            with self.assertRaises(ValueError) as caught:
                API._cache_load("failed", mock.Mock())
            self.assertIs(caught.exception, failure)
        finally:
            API._cache_inflight.pop("failed")
        with self.assertRaises(ValueError):
            API._cache_load("failed", lambda: (_ for _ in ()).throw(failure))
        self.assertNotIn("failed", API._cache_inflight)
        self.assertEqual(API._cache_load("failed", lambda: "recovered"), "recovered")

    def test_overview_preserves_unavailable_status(self):
        for error in (API.HTTPException(status_code=503, detail="waiting"), DQ.DashboardUnavailable("missing cycle")):
            with self.subTest(error=type(error).__name__), mock.patch.object(API, "_cache_load", side_effect=error):
                with self.assertRaises(API.HTTPException) as caught:
                    API.overview(refresh=False, activity=False, history_symbols=False)
                self.assertEqual(caught.exception.status_code, 503)


class SnapshotLifetimeTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(P, "_RISK_SNAPSHOT_REFRESH", {"running": False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inline_refresh_finishes_without_creating_thread(self):
        with mock.patch.object(P, "risk_dashboard", return_value={}) as refresh, \
                mock.patch.object(P.threading, "Thread", side_effect=AssertionError("daemon created")):
            result = P.request_risk_snapshot_refresh(background=False)
        self.assertEqual(result["status"], "completed")
        refresh.assert_called_once_with(refresh=True)
        self.assertFalse(P.risk_snapshot_refresh_status()["running"])
        self.assertEqual(P.risk_snapshot_refresh_status()["status"], "completed")

    def test_failure_keeps_deduplication_until_callback_finishes(self):
        duplicate = []
        with mock.patch.object(P, "risk_dashboard", side_effect=RuntimeError("source unavailable")):
            result = P.request_risk_snapshot_refresh(
                background=False,
                on_complete=lambda: duplicate.append(P.request_risk_snapshot_refresh(background=False)),
            )
        self.assertEqual(duplicate[0]["status"], "already_running")
        self.assertEqual(result["status"], "failed")
        self.assertIn("source unavailable", result["error"])
        self.assertEqual(P.risk_snapshot_refresh_status()["status"], "failed")

    def test_background_refresh_remains_nonblocking_and_deduplicated(self):
        entered = threading.Event()
        release = threading.Event()

        def refresh(**kwargs):
            entered.set()
            release.wait(timeout=3)

        with mock.patch.object(P, "risk_dashboard", side_effect=refresh):
            try:
                self.assertEqual(P.request_risk_snapshot_refresh()["status"], "scheduled")
                self.assertTrue(entered.wait(timeout=1))
                self.assertEqual(P.request_risk_snapshot_refresh()["status"], "already_running")
            finally:
                release.set()
                P.wait_risk_snapshot_refresh(timeout=3)
        self.assertEqual(P.risk_snapshot_refresh_status()["status"], "completed")

    def test_slot_refresh_runs_after_lease_release_and_completed_jobs_retry_refresh(self):
        for status in ("completed", "already_done"):
            with self.subTest(status=status), mock.patch.object(P, "init_db"), \
                    mock.patch.object(P.PSS, "run_preflight"), \
                    mock.patch.object(P, "_run_slot_impl", return_value={"status": status}), \
                    mock.patch.object(P.RC, "load_snapshot", return_value={"asof": "test"}), \
                    mock.patch.object(P, "request_risk_snapshot_refresh", return_value={"status": "completed"}) as refresh:
                result = P.run_slot("risk", risk_snapshot_background=False)
                self.assertIsNone(P._active_lease_context())
                refresh.assert_called_once_with(trigger="slot:risk", background=False)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["risk_snapshot_at"], "test")

    def test_failed_refresh_is_retryable_without_marking_trading_failed(self):
        with mock.patch.object(P, "init_db"), mock.patch.object(P.PSS, "run_preflight"), \
                mock.patch.object(P, "_run_slot_impl", return_value={"status": "completed"}), \
                mock.patch.object(P.RC, "load_snapshot", return_value=None), \
                mock.patch.object(P, "request_risk_snapshot_refresh", return_value={"status": "failed", "error": "source failed"}):
            result = P.run_slot("intraday", risk_snapshot_background=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Runner._result_exit_code(result), 1)
        self.assertEqual(Runner._result_exit_code({"status": "already_done", "risk_snapshot_refresh": "completed"}), 0)

    def test_runner_explicitly_selects_inline_refresh(self):
        with mock.patch.object(sys, "argv", ["paper_runner", "--slot", "risk"]), \
                mock.patch.object(P, "run_slot", return_value={"status": "completed", "risk_snapshot_refresh": "completed"}) as slot, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(Runner.main(), 0)
        slot.assert_called_once_with("risk", risk_snapshot_background=False)


if __name__ == "__main__":
    unittest.main()
