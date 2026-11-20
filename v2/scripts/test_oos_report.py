#!/usr/bin/env python3
"""Offline tests for the out-of-sample comparison report.

The verdict is the whole point of this script: a window where the parameters
were chosen cannot produce evidence of skill. These tests pin the four
verdicts so a future edit cannot quietly turn "in-sample only" into "good".
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oos_report  # noqa: E402


def variant(return_bps: int, drawdown_bps: int = 1_000, fills: int = 10) -> dict:
    return {
        "account": {
            "equity_cents": 10_000_000 + return_bps * 1_000,
            "return_bps": return_bps,
            "max_drawdown_bps": drawdown_bps,
            "profit_cents": return_bps * 1_000,
        },
        "fills": fills,
        "fees_cents": 100,
        "rebalances": [],
        "slots": [],
        # `regime_ab` puts the settled days under input.windows, so the fixture
        # must too — a top-level `windows` would read as zero days.
        "windows": [],
        "input": {"windows": [{"day": "2026-01-05"}, {"day": "2026-01-06"}]},
    }


def write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def run(train: dict, test: dict) -> dict:
    """`train`/`test` are already full payloads, keyed by variant name."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(root / "train.json", train)
        write(root / "test.json", test)
        out = root / "oos.json"
        oos_report.main([
            "--train", str(root / "train.json"), "--test", str(root / "test.json"),
            "--train-window", "2023-07-05:2025-12-31",
            "--test-window", "2026-01-05:2026-11-11",
            "--out", str(out),
        ])
        return json.loads(out.read_text(encoding="utf-8"))


class TestNoLeak(unittest.TestCase):
    """A held-out window must not overlap the window the parameters saw."""

    def test_clean_separation_is_accepted(self):
        win = oos_report.check_no_leak("2024-01-02:2025-12-31", "2026-01-05:2026-11-11")
        self.assertFalse(win["overlap"])
        self.assertEqual(win["test"], ["2026-01-05", "2026-11-11"])

    def test_overlap_is_refused(self):
        with self.assertRaises(SystemExit):
            oos_report.check_no_leak("2024-01-02:2026-06-30", "2026-01-05:2026-11-11")

    def test_touching_boundaries_are_refused(self):
        # train_end == test_start still means the last training day is the
        # first test day, so it is not held out.
        with self.assertRaises(SystemExit):
            oos_report.check_no_leak("2024-01-02:2026-01-05", "2026-01-05:2026-11-11")

    def test_reversed_window_is_refused(self):
        with self.assertRaises(SystemExit):
            oos_report.check_no_leak("2025-12-31:2024-01-02", "2026-01-05:2026-11-11")

    def test_malformed_window_is_refused(self):
        with self.assertRaises(SystemExit):
            oos_report.check_no_leak("2024-01-02", "2026-01-05:2026-11-11")
        with self.assertRaises(SystemExit):
            oos_report.check_no_leak("2024-01-02:", "2026-01-05:2026-11-11")


class TestVerdicts(unittest.TestCase):
    def test_positive_in_both_windows_is_only_weak_support(self):
        report = run({"filtered": variant(800)}, {"filtered": variant(300)})
        self.assertEqual(report["verdict"], "consistent_positive")
        self.assertIn("one held-out sample", report["why"])

    def test_negative_in_both_windows_is_a_finding(self):
        report = run({"filtered": variant(-500)}, {"filtered": variant(-300)})
        self.assertEqual(report["verdict"], "consistent_negative")

    def test_positive_only_in_sample_is_the_overfit_signature(self):
        # This is the case that matters: a result that exists only where the
        # parameters were chosen is not a result.
        report = run({"filtered": variant(1200)}, {"filtered": variant(-800)})
        self.assertEqual(report["verdict"], "in_sample_only")
        self.assertIn("overfit", report["why"])

    def test_a_sign_flip_is_not_a_win(self):
        report = run({"filtered": variant(-400)}, {"filtered": variant(200)})
        self.assertEqual(report["verdict"], "sign_flip")

    def test_missing_curve_is_incomplete_not_zero(self):
        bad = {"filtered": {"account": {}, "fills": None, "fees_cents": None,
                            "rebalances": [], "slots": [], "windows": [],
                            "input": {"windows": []}}}
        report = run(bad, {"filtered": variant(100)})
        self.assertEqual(report["verdict"], "incomplete")


class TestReportShape(unittest.TestCase):
    def test_both_windows_are_reported_with_their_dates(self):
        report = run({"filtered": variant(100)}, {"filtered": variant(-100)})
        self.assertEqual(report["train"]["window"], "2023-07-05:2025-12-31")
        self.assertEqual(report["test"]["window"], "2026-01-05:2026-11-11")
        self.assertEqual(report["train"]["days"], 2)

    def test_caveats_travel_with_the_numbers(self):
        # A number without its limitations gets quoted alone. These ride along.
        report = run({"filtered": variant(100)}, {"filtered": variant(100)})
        joined = " ".join(report["caveats"])
        self.assertIn("One held-out window", joined)
        self.assertIn("No dividends", joined)

    def test_the_report_forbids_retuning_on_the_held_out_window(self):
        report = run({"filtered": variant(100)}, {"filtered": variant(-900)})
        self.assertIn("not a prompt", report["note"])

    def test_a_missing_variant_is_an_error_not_an_empty_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "train.json", {"unfiltered": variant(100)})
            write(root / "test.json", {"filtered": variant(100)})
            with self.assertRaises(SystemExit):
                oos_report.main([
                    "--train", str(root / "train.json"), "--test", str(root / "test.json"),
                    "--train-window", "2024-01-01:2024-12-31",
                    "--test-window", "2025-01-01:2025-12-31",
                    "--out", str(root / "o.json"),
                ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
