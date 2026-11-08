"""Tests for the weekly review CLI (network stubbed through shadow.segments)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import shadow_candidates as shadow  # noqa: E402
import weekly_review  # noqa: E402

BASE = {"segments": [
    {"days": ["2026-10-30"], "status": "valued", "performance": {"return_bps": 100, "drawdown_bps": 50}},
    {"days": ["2026-11-02"], "status": "valued", "performance": {"return_bps": 80, "drawdown_bps": 40}},
]}
GOOD = {"segments": [
    {"days": ["2026-10-30"], "status": "valued", "performance": {"return_bps": 200, "drawdown_bps": 60}},
    {"days": ["2026-11-02"], "status": "valued", "performance": {"return_bps": 180, "drawdown_bps": 70}},
]}
WEAK = {"segments": [
    {"days": ["2026-10-30"], "status": "valued", "performance": {"return_bps": 200, "drawdown_bps": 60}},
    {"days": ["2026-11-02"], "status": "unavailable", "performance": None, "reason": "no_reviewed_price_set"},
]}


class WeeklyReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original = shadow.segments

    def tearDown(self) -> None:
        shadow.segments = self.original

    def test_chunk_days_builds_full_windows_only(self) -> None:
        windows = weekly_review.chunk_days("2026-10-30,2026-10-31,2026-11-02,2026-11-03", 2)
        self.assertEqual(windows, [
            ["2026-10-30", "2026-10-31"],
            ["2026-11-02", "2026-11-03"],
        ])
        self.assertEqual(len(weekly_review.chunk_days("a,b,c", 2)), 1)
        with self.assertRaisesRegex(ValueError, "not_enough_days"):
            weekly_review.chunk_days("a,b", 3)
        with self.assertRaisesRegex(ValueError, "window_size"):
            weekly_review.chunk_days("a,b", 0)

    def test_days_mode_uses_chunked_windows(self) -> None:
        captured = {}

        def fake(api, cycle, windows):
            captured[cycle] = windows
            return GOOD if cycle == "cand" else BASE

        shadow.segments = fake
        self.assertEqual(
            weekly_review.main([
                "--base-cycle", "base", "--candidate-cycle", "cand",
                "--days", "2026-10-30,2026-11-02", "--window-size", "1",
            ]),
            0,
        )
        self.assertEqual(captured["cand"], [["2026-10-30"], ["2026-11-02"]])


    def test_consistent_returns_zero(self) -> None:
        def fake(api, cycle, windows):
            return GOOD if cycle == "cand" else BASE

        shadow.segments = fake
        self.assertEqual(
            weekly_review.main([
                "--base-cycle", "base", "--candidate-cycle", "cand",
                "--windows", "2026-10-30;2026-11-02",
            ]),
            0,
        )

    def test_missing_coverage_is_unstable(self) -> None:
        def fake(api, cycle, windows):
            return WEAK if cycle == "cand" else BASE

        shadow.segments = fake
        self.assertEqual(
            weekly_review.main([
                "--base-cycle", "base", "--candidate-cycle", "cand",
                "--windows", "2026-10-30;2026-11-02",
            ]),
            4,
        )

    def test_bad_window_expression_returns_two(self) -> None:
        self.assertEqual(
            weekly_review.main([
                "--base-cycle", "base", "--candidate-cycle", "cand", "--windows", " ; ",
            ]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
