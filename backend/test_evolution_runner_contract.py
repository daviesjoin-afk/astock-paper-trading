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
from unittest.mock import patch
from contextlib import contextmanager
import tempfile


class TestEvolutionRunnerContract(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
