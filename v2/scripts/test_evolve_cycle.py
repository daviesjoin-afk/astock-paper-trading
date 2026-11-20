#!/usr/bin/env python3
"""Offline tests for the evolution-cycle gate check.

The property that matters: a cycle cannot be reported complete unless every gate
passed. The stop fill and the calm-divisor rescale are the two real cases that
shaped it.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evolve_cycle import check_isolation, check_measurement, check_power, evaluate  # noqa: E402


class TestPower(unittest.TestCase):
    def test_a_paired_spread_gives_a_far_lower_floor(self):
        # The same effect against a raw fold spread and a paired spread.
        raw = check_power(154, 507, 22)
        paired = check_power(154, 31, 22)
        self.assertLess(paired["floor_bps"], raw["floor_bps"] / 10)
        self.assertTrue(paired["ok"], "154 bps over a paired spread of 31 is resolvable")
        self.assertFalse(raw["ok"])

    def test_the_floor_scales_with_the_sample(self):
        few = check_power(100, 100, 10)
        many = check_power(100, 100, 100)
        self.assertLess(many["floor_bps"], few["floor_bps"])

    def test_no_spread_is_refused_rather_than_assumed(self):
        self.assertFalse(check_power(100, 0, 22)["ok"])

    def test_no_folds_is_refused(self):
        self.assertFalse(check_power(100, 50, 0)["ok"])


class TestMeasurement(unittest.TestCase):
    def test_a_positive_paired_interval_excludes_zero(self):
        result = check_measurement([64, 15, 31, 5, 11, 19, 7, 5, 6, 64,
                                    -4, 0, 0, 0, 16, 31, 0, 106, 48, 43, 88, 12])
        self.assertTrue(result["ok"])
        self.assertTrue(result["excludes_zero"])
        self.assertTrue(result["positive"])
        self.assertEqual(result["folds"], 22)

    def test_a_noisy_interval_includes_zero(self):
        result = check_measurement([438, 326, -97, 273, 15, 248, -146, -191,
                                    -395, -72, 37, -254, 91, 130, 126, 3,
                                    -43, 1, -566, 117, 155, 18])
        self.assertFalse(result["excludes_zero"])
        self.assertFalse(result["positive"])

    def test_one_fold_has_no_spread_and_is_refused(self):
        self.assertFalse(check_measurement([5])["ok"])

    def test_no_observations_is_refused(self):
        self.assertFalse(check_measurement([])["ok"])


def record(**overrides):
    base = {
        "hypothesis": "something measured",
        "decided_at": "session_close",
        "filled_at": "session_close",
        "power": {"expected_bps": 154, "paired_stdev_bps": 31, "folds": 22},
        "paired_bps": [64, 15, 31, 5, 11, 19, 7, 5, 6, 64,
                       -4, 0, 0, 0, 16, 31, 0, 106, 48, 43, 88, 12],
        "audit": {"passed": True, "independent": True,
                  "passed_folds": 22, "folds": 22},
        "decision": {"action": "adopt"},
    }
    base.update(overrides)
    return base


class TestEvaluate(unittest.TestCase):
    def test_the_stop_fill_cycle_is_complete(self):
        result = evaluate(record())
        self.assertTrue(result["complete"], result["missing"])
        self.assertEqual(result["missing"], [])

    def test_an_adopted_change_without_power_is_not_complete(self):
        result = evaluate(record(power={}))
        self.assertFalse(result["complete"])
        self.assertIn("power_checked", result["missing"])

    def test_adopting_an_interval_that_includes_zero_is_a_wrong_decision(self):
        # Measurement says "no effect"; the record says "adopt". The gate must
        # catch the contradiction rather than accept the claim.
        result = evaluate(record(
            paired_bps=[438, 326, -97, 273, 15, 248, -146, -191, -395, -72,
                        37, -254, 91, 130, 126, 3, -43, 1, -566, 117, 155, 18],
            decision={"action": "adopt"},
        ))
        self.assertFalse(result["gates"]["decided"]["ok"])
        self.assertEqual(result["gates"]["decided"]["should_be"], "revert")

    def test_reverting_a_significant_positive_is_also_wrong(self):
        result = evaluate(record(decision={"action": "revert"}))
        self.assertFalse(result["gates"]["decided"]["ok"])
        self.assertEqual(result["gates"]["decided"]["should_be"], "adopt")

    def test_an_audit_that_was_not_independent_does_not_count(self):
        result = evaluate(record(
            audit={"passed": True, "independent": False, "folds": 22}))
        self.assertFalse(result["gates"]["audited"]["ok"])

    def test_a_missing_audit_fails_the_gate(self):
        result = evaluate(record(audit={}))
        self.assertFalse(result["complete"])
        self.assertIn("audited", result["missing"])

    def test_no_hypothesis_fails_the_first_gate(self):
        self.assertFalse(evaluate(record(hypothesis=""))["gates"]["proposed"]["ok"])

    def test_the_calm_divisor_case_is_refused_by_power(self):
        # The real record: a 30 bps expectation that never cleared the floor.
        result = evaluate(record(
            power={"expected_bps": 30, "paired_stdev_bps": 96.9, "folds": 22},
            paired_bps=[438, 326, -97, 273, 15, 248, -146, -191, -395, -72,
                        37, -254, 91, 130, 126, 3, -43, 1, -566, 117, 155, 18],
            decision={"action": "revert"},
        ))
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing"], ["power_checked"])


class TestCli(unittest.TestCase):
    def test_it_exits_non_zero_when_the_cycle_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "cycle.json"
            source.write_text(json.dumps(record(power={})), encoding="utf-8")
            import subprocess
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "evolve_cycle.py"),
                 "--record", str(source), "--out", str(root / "v.json")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            verdict = json.loads((root / "v.json").read_text(encoding="utf-8"))
            self.assertFalse(verdict["complete"])




class TestIsolation(unittest.TestCase):
    """The gate that catches the mistake the other five could not.

    The stop-fill change decided from the session close and filled at an
    intraday line. Every other gate passed: the effect was real, the interval
    excluded zero, and an independent audit rebuilt the ledger perfectly. The
    ledger was consistent -- it just described a sequence that could not happen.
    """

    def test_deciding_and_filling_at_the_same_close_is_fine(self):
        result = check_isolation({"decided_at": "session_close",
                                  "filled_at": "session_close"})
        self.assertTrue(result["ok"])

    def test_filling_at_an_intraday_line_read_from_the_close_is_look_ahead(self):
        result = check_isolation({"decided_at": "session_close",
                                  "filled_at": "intraday_line"})
        self.assertFalse(result["ok"])
        self.assertIn("before the decision", result["why"])

    def test_a_next_session_fill_after_a_prior_close_is_fine(self):
        result = check_isolation({"decided_at": "prior_close",
                                  "filled_at": "session_open"})
        self.assertTrue(result["ok"])

    def test_a_resting_order_placed_at_the_open_may_fill_at_its_line(self):
        # The order was in the book before the session ran, so its line is not
        # information the decision borrowed from the future.
        result = check_isolation({"decided_at": "session_open",
                                  "filled_at": "intraday_line"})
        self.assertTrue(result["ok"])

    def test_an_unspecified_moment_is_refused_rather_than_assumed(self):
        self.assertFalse(check_isolation({})["ok"])
        self.assertFalse(check_isolation({"decided_at": "session_close"})["ok"])

    def test_an_unknown_moment_name_is_refused(self):
        result = check_isolation({"decided_at": "tea_time",
                                  "filled_at": "session_close"})
        self.assertFalse(result["ok"])

    def test_the_stop_fill_case_now_fails_the_whole_cycle(self):
        # The real record, with the isolation fields it was missing.
        result = evaluate(record(decided_at="session_close",
                                 filled_at="intraday_line",
                                 decision={"action": "adopt"}))
        self.assertFalse(result["complete"])
        self.assertIn("isolated", result["missing"])


class TestIsolationIsRequired(unittest.TestCase):
    def test_a_record_without_isolation_fields_is_not_complete(self):
        bare = record()
        del bare["decided_at"]
        del bare["filled_at"]
        result = evaluate(bare)
        self.assertFalse(result["complete"])
        self.assertIn("isolated", result["missing"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
