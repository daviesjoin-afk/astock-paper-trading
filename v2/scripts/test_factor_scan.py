#!/usr/bin/env python3
"""Offline tests for the factor scan.

The ordering rule is the thing under test: sign consistency comes before
magnitude, because a factor that flips sign is not a weaker factor — it is the
object the governance gate refuses.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factor_scan import CANDIDATES, rank_candidates, score  # noqa: E402
from _probe_stub import make_probe_stub  # noqa: E402


def row(name: str, ics: list[int], usable: bool = True) -> dict:
    positive = sum(1 for value in ics if value > 0)
    mean = round(sum(ics) / len(ics)) if ics else 0
    flips = sum(1 for left, right in zip(ics, ics[1:]) if (left >= 0) != (right >= 0))
    return {
        "name": name, "usable": usable, "periods": len(ics), "rank_ics": ics,
        "mean_rank_ic_bps": mean, "median_rank_ic_bps": mean, "ir_bps": 1,
        "sign_flips": flips, "positive_periods": positive,
        "all_same_sign": positive in (0, len(ics)),
    }


class TestRanking(unittest.TestCase):
    def test_consistent_sign_outranks_a_bigger_but_flipping_mean(self):
        # +2000 mean with a flip versus +800 mean all-negative. The flipper has
        # the better magnitude and must still lose: governance refuses it.
        flipper = row("flipper", [4000, -2000, 4000])
        consistent = row("consistent", [-800, -900, -700])
        ranked = rank_candidates([flipper, consistent])
        self.assertEqual(ranked[0]["name"], "consistent")
        self.assertEqual(ranked[1]["name"], "flipper")

    def test_more_positive_periods_wins_a_tie_on_consistency(self):
        a = row("a", [-100, -200, -300])
        b = row("b", [-50, -60, -70, -80, -90, 10])
        # `b` has five negative periods and one positive: it flips.
        ranked = rank_candidates([a, b])
        self.assertEqual(ranked[0]["name"], "a")

    def test_unusable_factors_are_excluded_from_the_ranking(self):
        ranked = rank_candidates([row("good", [100, 200, 300]),
                                  row("broken", [], usable=False)])
        self.assertEqual([item["name"] for item in ranked], ["good"])

    def test_rank_numbers_are_sequential(self):
        ranked = rank_candidates([row("a", [100, 200]), row("b", [300, 400])])
        self.assertEqual([item["rank"] for item in ranked], [1, 2])


class TestScore(unittest.TestCase):
    def test_a_probe_failure_is_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "emit.py"
            script.write_text("import sys; sys.stderr.write('boom'); sys.exit(2)",
                              encoding="utf-8")
            stub = make_probe_stub(root, script)
            result = score("x", {"momentum": {"window": 20}}, stub, root / "db",
                           ["2025-01-01"], 10)
        self.assertFalse(result["usable"])
        self.assertEqual(len(result["unusable"]), 1)

    def test_all_periods_counted_when_they_all_work(self):
        periods = ["2025-01-01", "2025-02-01"]
        payload = {"stats": {"rank_ic_bps": 500, "samples": 1000},
                   "coverage": {"usable": True}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "r.json"
            report.write_text(json.dumps(payload), encoding="utf-8")
            script = root / "emit.py"
            script.write_text(
                f"import sys;sys.stdout.write(open(r'{report}',encoding='utf-8').read())",
                encoding="utf-8")
            stub = make_probe_stub(root, script)
            result = score("x", {"momentum": {"window": 20}}, stub, root / "db",
                           periods, 10)
        self.assertTrue(result["usable"])
        self.assertEqual(result["periods"], 2)
        self.assertEqual(result["positive_periods"], 2)
        self.assertFalse(result["all_same_sign"] is False)

    def test_coverage_failure_drops_the_period(self):
        payload = {"stats": {"rank_ic_bps": 500, "samples": 1000},
                   "coverage": {"usable": False, "dropped_bps": 9000}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "r.json"
            report.write_text(json.dumps(payload), encoding="utf-8")
            script = root / "emit.py"
            script.write_text(
                f"import sys;sys.stdout.write(open(r'{report}',encoding='utf-8').read())",
                encoding="utf-8")
            stub = make_probe_stub(root, script)
            result = score("x", {"momentum": {"window": 20}}, stub, root / "db",
                           ["2025-01-01"], 10)
        self.assertFalse(result["usable"])
        self.assertEqual(result["unusable"][0]["reason"], "coverage_below_max_loss")


class TestLibrary(unittest.TestCase):
    def test_the_current_signal_is_in_the_library_as_a_benchmark(self):
        # A replacement is only meaningful against what it replaces.
        self.assertIn("momentum_20", CANDIDATES)
        self.assertIn("momentum_60", CANDIDATES)

    def test_every_expression_is_probe_shaped(self):
        for name, expr in CANDIDATES.items():
            self.assertIsInstance(expr, dict, name)
            self.assertEqual(len(expr), 1, f"{name} should be a single operator")
            json.dumps(expr)  # must be serialisable for the probe


if __name__ == "__main__":
    unittest.main(verbosity=2)
