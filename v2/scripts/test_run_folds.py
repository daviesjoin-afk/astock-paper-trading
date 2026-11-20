#!/usr/bin/env python3
"""Offline tests for fold aggregation.

The rule under test: a distribution must not be collapsed into a single
verdict. Fewer than three usable folds cannot support one, and a mean without
its spread invites exactly the conclusion the data does not support.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_folds import run_fold, summarise  # noqa: E402

NEWLINE = chr(10)


def ok(fold: int, return_bps: int) -> dict:
    return {"fold": fold, "status": "ok", "return_bps": return_bps,
            "max_drawdown_bps": 1_000, "fills": 10, "days": 100,
            "train": ["2024-01-01", "2024-12-31"], "test": ["2025-01-01", "2025-06-30"]}


def make_stub(root: Path, payload: dict | None, exit_code: int = 0) -> Path:
    """A stand-in binary that prints a canned report.

    On Windows a `.sh` file cannot be executed directly (WinError 193), so the
    stub is a real `.bat` that shells out to the interpreter. `run_fold` passes
    its own CLI arguments, which a bare `sys.executable` would misinterpret, so
    the arguments are swallowed by the script.
    """
    report = root / "report.json"
    if payload is not None:
        report.write_text(json.dumps(payload), encoding="utf-8")
    script = root / "emit.py"
    lines = ["import sys"]
    if payload is not None:
        lines.append(f"sys.stdout.write(open(r'{report}', encoding='utf-8').read())")
    lines.append(f"sys.exit({exit_code})")
    script.write_text(NEWLINE.join(lines) + NEWLINE, encoding="utf-8")

    stub = root / "stub.bat"
    stub.write_text(
        "@echo off" + NEWLINE + f'"{sys.executable}" "{script}"' + NEWLINE,
        encoding="utf-8",
    )
    return stub


class TestVerdicts(unittest.TestCase):
    def test_all_positive_is_stronger_but_still_not_a_forecast(self):
        summary = summarise([ok(0, 100), ok(1, 200), ok(2, 50)])
        self.assertEqual(summary["verdict"], "all_positive")
        self.assertIn("not a forecast", summary["why"])

    def test_all_negative_is_not_supported(self):
        summary = summarise([ok(0, -100), ok(1, -200), ok(2, -50)])
        self.assertEqual(summary["verdict"], "all_negative")

    def test_mixed_sign_is_the_definition_of_unstable(self):
        summary = summarise([ok(0, 100), ok(1, -200), ok(2, 50)])
        self.assertEqual(summary["verdict"], "mixed")
        self.assertEqual(summary["positive_folds"], 2)
        self.assertIn("unstable", summary["why"])

    def test_two_folds_cannot_support_a_verdict(self):
        summary = summarise([ok(0, 100), ok(1, 100)])
        self.assertEqual(summary["verdict"], "too_few_folds")
        self.assertIn("minimum", summary["why"])

    def test_no_usable_fold_says_so_instead_of_reporting_zero(self):
        summary = summarise([{"fold": 0, "status": "failed", "error": ["boom"]}])
        self.assertEqual(summary["verdict"], "no_usable_fold")
        self.assertNotIn("mean_bps", summary)


class TestSpread(unittest.TestCase):
    def test_spread_is_reported_next_to_the_mean(self):
        summary = summarise([ok(0, 200), ok(1, -300), ok(2, 100)])
        self.assertEqual(summary["mean_bps"], 0)
        self.assertEqual(summary["spread_bps"], 500)
        self.assertEqual(summary["worst_bps"], -300)
        self.assertEqual(summary["best_bps"], 200)

    def test_a_mean_zero_hides_a_huge_spread(self):
        summary = summarise([ok(0, 250), ok(1, -250)])
        self.assertEqual(summary["mean_bps"], 0)
        self.assertGreaterEqual(summary["spread_bps"], 500)

    def test_failed_folds_are_listed_not_dropped_silently(self):
        summary = summarise([ok(0, 100), ok(1, 100), ok(2, 100),
                             {"fold": 3, "status": "failed", "error": ["x"]}])
        self.assertEqual(summary["folds_failed"], 1)
        self.assertEqual(summary["failed_folds"], [3])


class TestRunFold(unittest.TestCase):
    def test_a_failing_binary_is_a_failed_fold_not_a_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = make_stub(root, None, exit_code=3)
            result = run_fold(stub, root / "db.sqlite3",
                              {"fold": 0, "test": ["2025-01-01", "2025-06-30"]},
                              root / "runs", "filtered", timeout=10)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("return_bps", result)
        self.assertEqual(result["exit_code"], 3)

    def test_a_failure_keeps_the_context_that_explains_it(self):
        # The day and symbol that caused a settlement failure are printed above
        # the summary line; dropping them turns a data gap into a mystery.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "emit.py"
            script.write_text(
                "import sys" + NEWLINE +
                "for i in range(4):" + NEWLINE +
                "    print(f'held 稳健 600000.SH day=2024-12-30 step={i}', file=sys.stderr)" + NEWLINE +
                "print('regime_ab: rules: missing_close_price', file=sys.stderr)" + NEWLINE +
                "sys.exit(2)" + NEWLINE,
                encoding="utf-8",
            )
            stub = root / "stub.bat"
            stub.write_text(
                "@echo off" + NEWLINE + f'"{sys.executable}" "{script}"' + NEWLINE,
                encoding="utf-8",
            )
            result = run_fold(stub, root / "db.sqlite3",
                              {"fold": 0, "test": ["2025-01-01", "2025-06-30"]},
                              root / "runs", "filtered", timeout=10)
        joined = " ".join(result["error"])
        self.assertIn("missing_close_price", joined)
        self.assertIn("2024-12-30", joined, "the offending day must survive into the report")

    def test_an_unrunnable_binary_is_a_failed_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "does-not-exist.exe"
            result = run_fold(missing, root / "db.sqlite3",
                              {"fold": 0, "test": ["2025-01-01", "2025-06-30"]},
                              root / "runs", "filtered", timeout=10)
        self.assertEqual(result["status"], "failed")

    def test_a_missing_variant_is_a_failed_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = make_stub(root, {"unfiltered": {"account": {}}})
            result = run_fold(stub, root / "db.sqlite3",
                              {"fold": 0, "test": ["2025-01-01", "2025-06-30"]},
                              root / "runs", "filtered", timeout=10)
        self.assertEqual(result["status"], "failed")
        self.assertIn("filtered", " ".join(result["error"]))

    def test_a_valid_report_is_read_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = make_stub(root, {
                "filtered": {"account": {"return_bps": 123, "max_drawdown_bps": 45}, "fills": 7},
                "input": {"windows": [{"day": "2025-01-01"}]},
            })
            result = run_fold(stub, root / "db.sqlite3",
                              {"fold": 0, "test": ["2025-01-01", "2025-06-30"]},
                              root / "runs", "filtered", timeout=10)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["return_bps"], 123)
        self.assertEqual(result["fills"], 7)
        self.assertEqual(result["days"], 1)

    def test_non_json_output_is_a_failed_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "emit.py"
            script.write_text("print('not-json')" + NEWLINE, encoding="utf-8")
            stub = root / "stub.bat"
            stub.write_text(
                "@echo off" + NEWLINE + f'"{sys.executable}" "{script}"' + NEWLINE,
                encoding="utf-8",
            )
            result = run_fold(stub, root / "db.sqlite3",
                              {"fold": 0, "test": ["2025-01-01", "2025-06-30"]},
                              root / "runs", "filtered", timeout=10)
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
