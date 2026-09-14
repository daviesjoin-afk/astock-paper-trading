# -*- coding: utf-8 -*-
"""Test scheduler-visible exit code contract for evolution runner."""
from __future__ import annotations

import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from evolution_loop_runner import _result_exit_code, main
import evolution_loop as EL
import universe as U
from unittest.mock import Mock, patch
from contextlib import contextmanager
import tempfile


class DeterministicBackend(EL.Backend):
    """Deterministic EL backend that successfully completes all 5 stages for persistence testing."""

    def __init__(self):
        self.params = {"learning_rate": 0.05, "threshold": 0.50}

    def observe(self, conn, generation, ctx=None):
        return {
            "params_id": generation,
            "params": dict(self.params),
            "has_data": True,
            "sample_count": 10,
            "evidence_count": 10,
        }

    def evaluate(self, conn, generation, ctx=None):
        return {"intelligence_score": 0.85}

    def mutate(self, conn, generation, ctx=None):
        self.params["learning_rate"] = round(self.params["learning_rate"] + 0.01, 4)
        return {
            "mutated": True,
            "params_id": generation,
            "adjustments": ["lr+0.01"],
            "changed_keys": ["learning_rate"],
        }

    def validate(self, conn, generation, ctx=None):
        return {
            "valid": True,
            "params_id": generation,
            "out_of_bounds_corrected": False,
            "params": dict(self.params),
        }

    def apply(self, conn, generation, ctx=None):
        return {"applied_params_id": generation, "active_params_id": generation}


class TestEvolutionRunnerContract(unittest.TestCase):
    def test_failed_snapshot_does_not_consume_next_daily_retry_window(self):
        """A failed generation snapshot must let the next daily runner enter run_loop."""
        import sqlite3

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = os.path.join(d, "failed_snapshot.sqlite3")
            conn = sqlite3.connect(db_path)
            try:
                EL.ensure_loop_schema(conn)
                conn.execute(
                    """INSERT INTO evolution_loop_state
                       (generation, status, finished_at) VALUES (1, 'failed', ?)""",
                    (f"{EL._now()[:10]}T16:45:00",),
                )
                conn.execute(
                    """INSERT INTO evolution_generation
                       (generation, created_at) VALUES (1, ?)""",
                    (f"{EL._now()[:10]}T16:45:00",),
                )
                conn.commit()
                self.assertFalse(EL.is_today_generation_completed(conn))
            finally:
                conn.close()

            @contextmanager
            def lease_granted(name):
                yield {"allowed": True}

            report = {
                "completed": 1, "failed": 0, "interrupted": 0,
                "total_stage_errors": 0, "generations_run": 1,
                "details": [{"generation": 2, "status": "completed"}],
            }
            with patch.object(U, "is_trade_day", return_value=True), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=lease_granted), \
                 patch.object(EL, "run_loop", return_value=report) as mock_run:
                self.assertEqual(main(["--db", db_path, "--daily"]), 0)
                mock_run.assert_called_once()

    def test_all_completed_returns_exit_0(self):
        report = {
            "completed": 1,
            "failed": 0,
            "interrupted": 0,
            "total_stage_errors": 0,
            "generations_run": 1,
            "details": [{"generation": 1, "status": "completed"}],
        }
        self.assertEqual(_result_exit_code(report), 0)

    def test_interrupted_returns_non_zero(self):
        report = {
            "completed": 0,
            "failed": 0,
            "interrupted": 1,
            "total_stage_errors": 1,
            "generations_run": 1,
            "details": [{"generation": 1, "status": "interrupted"}],
        }
        self.assertNotEqual(_result_exit_code(report), 0)

    def test_failed_returns_non_zero(self):
        report = {
            "completed": 0,
            "failed": 1,
            "interrupted": 0,
            "total_stage_errors": 1,
            "generations_run": 1,
            "details": [{"generation": 1, "status": "failed"}],
        }
        self.assertNotEqual(_result_exit_code(report), 0)

    def test_stage_errors_greater_than_zero_returns_non_zero(self):
        # Even if completed count appears positive, any stage errors must fail
        report = {
            "completed": 1,
            "failed": 0,
            "interrupted": 0,
            "total_stage_errors": 1,
            "generations_run": 1,
            "details": [{"generation": 1, "status": "completed"}],
        }
        self.assertNotEqual(_result_exit_code(report), 0)

    def test_legitimate_nodata_and_skipped_returns_exit_0(self):
        # Legitimate business skip: sample insufficient, completed with skipped stages
        report = {
            "completed": 1,
            "failed": 0,
            "interrupted": 0,
            "total_stage_errors": 0,
            "total_stages_skipped": 1,
            "generations_run": 1,
            "details": [{
                "generation": 1,
                "status": "completed",
                "stages_skipped": 1,
                "stages_failed": 0,
                "candidate_created": False,
            }],
        }
        self.assertEqual(_result_exit_code(report), 0)

    def test_already_done_idempotent_returns_exit_0(self):
        report = {
            "status": "already_done",
            "reason": "already_done",
            "completed": 0,
            "failed": 0,
            "interrupted": 0,
            "total_stage_errors": 0,
            "generations_run": 0,
        }
        self.assertEqual(_result_exit_code(report), 0)

    def test_non_dict_fails_closed(self):
        self.assertNotEqual(_result_exit_code(None), 0)
        self.assertNotEqual(_result_exit_code("completed"), 0)
        self.assertNotEqual(_result_exit_code([{"completed": 1}]), 0)
        self.assertNotEqual(_result_exit_code(123), 0)

    def test_missing_fields_fails_closed(self):
        self.assertNotEqual(_result_exit_code({}), 0)
        self.assertNotEqual(_result_exit_code({"completed": 1}), 0)
        self.assertNotEqual(_result_exit_code({"completed": 1, "failed": 0}), 0)
        self.assertNotEqual(_result_exit_code({"completed": 1, "failed": 0, "interrupted": 0}), 0)

    def test_explicit_error_status_fails_closed(self):
        report = {
            "status": "failed",
            "error": "something bad",
            "completed": 0,
            "failed": 1,
            "interrupted": 0,
            "total_stage_errors": 1,
        }
        self.assertNotEqual(_result_exit_code(report), 0)

    def test_deferred_status_returns_75(self):
        report = {
            "status": "deferred",
            "reason": "heavy_job_busy",
            "retryable": True,
            "admission": {"allowed": False, "reason": "heavy_job_busy"},
        }
        self.assertEqual(_result_exit_code(report), 75)

    def test_non_trading_day_skipped_returns_0(self):
        report = {
            "status": "skipped",
            "reason": "non_trading_day",
            "date": "2026-09-13",
            "completed": 0,
            "failed": 0,
            "interrupted": 0,
            "total_stage_errors": 0,
        }
        self.assertEqual(_result_exit_code(report), 0)

    def test_runner_deferred_when_heavy_lease_denied(self):
        """Runner must output deferred JSON and return exit code 75 when heavy lease is denied."""
        report = {
            "status": "deferred",
            "reason": "memory_high_water",
            "retryable": True,
            "admission": {"allowed": False, "reason": "memory_high_water"},
        }
        self.assertEqual(_result_exit_code(report), 75)

    def test_main_runner_heavy_lease_denied_exits_75_and_does_not_run_loop(self):
        """When heavy lease is denied, runner exits with code 75 and run_loop is not called."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = os.path.join(d, "test_evo.sqlite3")

            @contextmanager
            def mock_lease(name):
                yield {"allowed": False, "reason": "memory_exhausted"}

            with patch("evolution_loop_runner.heavy_job_lease", side_effect=mock_lease), \
                 patch.object(EL, "run_loop") as mock_run_loop:
                ret = main(["--db", db_path])
                self.assertEqual(ret, 75)
                mock_run_loop.assert_not_called()

    def test_main_runner_non_trading_day_skips_and_exits_0(self):
        """On a non-trading day with --daily, runner exits 0 and run_loop is not called."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = os.path.join(d, "test_evo.sqlite3")

            with patch.object(U, "is_trade_day", return_value=False), \
                 patch.object(EL, "run_loop") as mock_run_loop:
                ret = main(["--db", db_path, "--daily"])
                self.assertEqual(ret, 0)
                mock_run_loop.assert_not_called()

    def test_main_runner_already_done_today_exits_0_and_does_not_acquire_lease(self):
        """When today's generation is already completed, runner exits 0 without acquiring lease."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = os.path.join(d, "test_evo.sqlite3")

            lease_called = []

            @contextmanager
            def mock_lease(name):
                lease_called.append(name)
                yield {"allowed": True}

            with patch.object(U, "is_trade_day", return_value=True), \
                 patch.object(EL, "is_today_generation_completed", return_value=True), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=mock_lease), \
                 patch.object(EL, "run_loop") as mock_run_loop:
                ret = main(["--db", db_path, "--daily"])
                self.assertEqual(ret, 0)
                mock_run_loop.assert_not_called()
                self.assertEqual(len(lease_called), 0, "Heavy lease must not be acquired when already done")

    def test_main_runner_three_window_flow_deferred_then_success_then_already_done(self):
        """Simulate 3-window scheduler flow: deferred (exit 75) -> success (exit 0) -> already_done (exit 0)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = os.path.join(d, "test_evo.sqlite3")

            # Window 1: Heavy lease busy -> exit 75
            @contextmanager
            def lease_busy(name):
                yield {"allowed": False, "reason": "system_busy"}

            with patch.object(U, "is_trade_day", return_value=True), \
                 patch.object(EL, "is_today_generation_completed", return_value=False), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=lease_busy), \
                 patch.object(EL, "run_loop") as mock_run:
                code_w1 = main(["--db", db_path, "--daily"])
                self.assertEqual(code_w1, 75)
                mock_run.assert_not_called()

            # Window 2: Heavy lease granted -> run_loop completes successfully -> exit 0
            completed_report = {
                "completed": 1, "failed": 0, "interrupted": 0,
                "total_stage_errors": 0, "generations_run": 1,
                "details": [{"generation": 1, "status": "completed"}],
            }

            @contextmanager
            def lease_free(name):
                yield {"allowed": True}

            with patch.object(U, "is_trade_day", return_value=True), \
                 patch.object(EL, "is_today_generation_completed", return_value=False), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=lease_free), \
                 patch.object(EL, "run_loop", return_value=completed_report) as mock_run:
                code_w2 = main(["--db", db_path, "--daily"])
                self.assertEqual(code_w2, 0)
                mock_run.assert_called_once()

            # Window 3: Same day subsequent run -> already_done -> exit 0, no lease acquired
            with patch.object(U, "is_trade_day", return_value=True), \
                 patch.object(EL, "is_today_generation_completed", return_value=True), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=AssertionError("should not acquire lease")), \
                 patch.object(EL, "run_loop") as mock_run:
                code_w3 = main(["--db", db_path, "--daily"])
                self.assertEqual(code_w3, 0)
                mock_run.assert_not_called()

    def test_main_runner_three_window_real_db_persistence_flow(self):
        """Verify real SQLite persistence across 3 scheduler windows:
        Window 1: heavy lease denied -> exit 75, DB rows = 0
        Window 2: heavy lease allowed -> real run_loop with DeterministicBackend completes -> exit 0, DB rows = 1
        Window 3: real is_today_generation_completed checks DB -> already_done exit 0, lease never called, DB rows = 1
        """
        import sqlite3

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = os.path.join(d, "real_flow.sqlite3")

            # --- Window 1: Heavy lease busy ---
            @contextmanager
            def lease_denied(name):
                yield {"allowed": False, "reason": "worker_lease_busy"}

            with patch.object(U, "is_trade_day", return_value=True), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=lease_denied):
                code_w1 = main(["--db", db_path, "--daily"])
                self.assertEqual(code_w1, 75)

            # Assert 0 completed generations persisted in DB
            conn = sqlite3.connect(db_path)
            try:
                count_w1 = conn.execute("SELECT count(*) FROM evolution_loop_state WHERE status='completed'").fetchone()[0]
                self.assertEqual(count_w1, 0, "Window 1 deferred run must persist 0 completed generations")
            finally:
                conn.close()

            # --- Window 2: Heavy lease granted, real run_loop execution ---
            @contextmanager
            def lease_granted(name):
                yield {"allowed": True}

            with patch.object(U, "is_trade_day", return_value=True), \
                 patch("evolution_loop_runner.heavy_job_lease", side_effect=lease_granted), \
                 patch("evolution_loop_runner.ProductionBackend", DeterministicBackend):
                code_w2 = main(["--db", db_path, "--daily"])
                self.assertEqual(code_w2, 0)

            # Assert exactly 1 completed generation persisted in DB
            conn = sqlite3.connect(db_path)
            try:
                rows_w2 = conn.execute("SELECT generation, status, finished_at FROM evolution_loop_state").fetchall()
                self.assertEqual(len(rows_w2), 1, "Window 2 must persist exactly 1 completed generation row")
                self.assertEqual(rows_w2[0][1], "completed")
                self.assertIsNotNone(rows_w2[0][2])
            finally:
                conn.close()

            # --- Window 3: Real DB query finds today's completed generation, skips lease & run ---
            lease_mock = Mock(side_effect=AssertionError("Heavy lease must NOT be acquired when already done"))
            with patch.object(U, "is_trade_day", return_value=True), \
                 patch("evolution_loop_runner.heavy_job_lease", lease_mock), \
                 patch.object(EL, "run_loop") as mock_run_loop:
                code_w3 = main(["--db", db_path, "--daily"])
                self.assertEqual(code_w3, 0)
                mock_run_loop.assert_not_called()
                lease_mock.assert_not_called()

            # Assert row count remains 1 (idempotent)
            conn = sqlite3.connect(db_path)
            try:
                count_w3 = conn.execute("SELECT count(*) FROM evolution_loop_state").fetchone()[0]
                self.assertEqual(count_w3, 1, "Window 3 must not add any new generations")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
