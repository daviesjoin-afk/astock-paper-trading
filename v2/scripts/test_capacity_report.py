#!/usr/bin/env python3
"""Offline tests for the capacity report.

The finding under test: a slot can be "full" in position count while its edge is
negative, and another can be "under-filled" while its edge is positive. The
report must show both, in the units that reach the account.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capacity_report import (  # noqa: E402
    DEFAULT_CAP, analyse, slot_capacity, summarise,
)


def slot(name: str, invested: int, open_positions: int, fills: int = 10) -> dict:
    return {"slot": name, "invested_days": invested, "open_positions": open_positions,
            "fills": fills, "fees_cents": 0, "net_transferred_cents": 0}


class TestSlotCapacity(unittest.TestCase):
    def test_a_full_slot_has_a_fill_rate_of_one(self):
        result = slot_capacity(slot("steady", 30, 3), 30, 1_500, 3)
        self.assertEqual(result["fill_rate"], 1.0)
        self.assertEqual(result["time_in_market"], 1.0)

    def test_a_half_filled_slot_says_so(self):
        result = slot_capacity(slot("aggressive", 18, 1), 30, 3_000, 3)
        self.assertAlmostEqual(result["fill_rate"], 0.333, places=3)

    def test_realised_exposure_is_fill_rate_times_weight(self):
        # 2/3 filled at 3000 bps per position = 0.667 * 0.30 = 0.20
        result = slot_capacity(slot("aggressive", 30, 2), 30, 3_000, 3)
        self.assertAlmostEqual(result["realised_exposure"], 0.2, places=3)

    def test_no_positions_is_zero_fill_not_missing(self):
        # An empty book is a real measurement, not an absence of data.
        result = slot_capacity(slot("aggressive", 0, 0), 30, 3_000, 3)
        self.assertEqual(result["fill_rate"], 0.0)
        self.assertEqual(result["realised_exposure"], 0.0)

    def test_a_slot_with_no_settled_days_has_no_ratio(self):
        result = slot_capacity(slot("steady", 0, 0), 0, 1_500, 3)
        self.assertIsNone(result["time_in_market"])


class TestAnalyse(unittest.TestCase):
    def test_the_capital_share_multiplies_the_exposure(self):
        report = {
            "input": {"settled_days": 30},
            "filtered": {
                "weights_bps": [2_000, 8_000],
                "slots": [slot("steady", 30, 3), slot("aggressive", 18, 1)],
            },
        }
        result = analyse(report)
        steady = result["slots"][0]
        aggressive = result["slots"][1]
        # steady: 1.0 fill x 0.15 weight x 0.20 capital = 0.03
        self.assertAlmostEqual(steady["account_exposure"], 0.03, places=4)
        # aggressive: 0.333 x 0.30 x 0.80 = 0.08
        self.assertAlmostEqual(aggressive["account_exposure"], 0.08, places=3)

    def test_an_unknown_slot_gets_no_weight_and_no_exposure(self):
        report = {
            "input": {"settled_days": 30},
            "filtered": {"weights_bps": [5_000, 5_000],
                         "slots": [slot("mystery", 30, 3)]},
        }
        result = analyse(report)
        self.assertEqual(result["slots"][0]["per_position_bps"], 0)
        self.assertEqual(result["slots"][0]["realised_exposure"], 0.0)


def analysed(name: str, invested: int, open_positions: int, days: int = 30) -> dict:
    """A slot dict in the shape `summarise` consumes, i.e. post-`slot_capacity`."""
    return slot_capacity(slot(name, invested, open_positions), days,
                         {"steady": 1_500, "aggressive": 3_000}.get(name, 0), DEFAULT_CAP)


class TestSummarise(unittest.TestCase):
    def test_a_full_slot_is_labelled_full(self):
        folds = [{"slots": [analysed("steady", 30, 3)]} for _ in range(3)]
        self.assertEqual(summarise(folds)["steady"]["finding"], "capacity_full")

    def test_an_underused_slot_is_labelled(self):
        folds = [{"slots": [analysed("aggressive", 18, 1)]} for _ in range(3)]
        self.assertEqual(summarise(folds)["aggressive"]["finding"], "capacity_underused")

    def test_empty_folds_are_counted_not_averaged_away(self):
        folds = [
            {"slots": [analysed("aggressive", 0, 0)]},
            {"slots": [analysed("aggressive", 30, 3)]},
        ]
        result = summarise(folds)["aggressive"]
        self.assertEqual(result["folds_with_no_position"], 1)

    def test_the_two_slots_are_reported_separately(self):
        folds = [{"slots": [analysed("steady", 30, 3), analysed("aggressive", 6, 1)]}]
        result = summarise(folds)
        self.assertEqual(set(result), {"steady", "aggressive"})
        self.assertGreater(result["steady"]["mean_fill_rate"],
                           result["aggressive"]["mean_fill_rate"])


class TestRealShape(unittest.TestCase):
    def test_the_default_cap_is_three(self):
        self.assertEqual(DEFAULT_CAP, 3)

    def test_it_reads_a_replay_directory(self):
        payload = {
            "input": {"settled_days": 30},
            "filtered": {
                "account": {"return_bps": 100},
                "weights_bps": [4_500, 5_500],
                "slots": [slot("steady", 30, 3), slot("aggressive", 12, 1)],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fold-0.json").write_text(json.dumps(payload), encoding="utf-8")
            spec = root / "folds.json"
            spec.write_text(json.dumps({"folds": [{"fold": 0, "test": ["a", "b"]}]}),
                            encoding="utf-8")
            import subprocess
            out = root / "cap.json"
            code = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "capacity_report.py"),
                 "--replays", str(root), "--folds", str(spec), "--out", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(code.returncode, 0, code.stderr)
            written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["by_slot"]["aggressive"]["folds"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
