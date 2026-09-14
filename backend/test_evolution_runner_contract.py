# -*- coding: utf-8 -*-
"""Test scheduler-visible exit code contract for evolution runner."""
from __future__ import annotations

import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from evolution_loop_runner import _result_exit_code


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


if __name__ == "__main__":
    unittest.main()
