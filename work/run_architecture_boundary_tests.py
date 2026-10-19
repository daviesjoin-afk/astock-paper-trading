"""Run boundary regressions or the full backend suite with UTF-8 logs."""

import argparse
import contextlib
import json
import sys
import time
import unittest
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    backend = root / "backend"
    sys.path.insert(0, str(backend))
    patterns = (
        ["test_*.py"]
        if args.full
        else [
            "test_architecture_boundary_fixes.py",
            "test_read_optimizations.py",
            "test_paper_hotpath_boundaries.py",
            "test_runtime_simplifications.py",
            "test_business_decoupling.py",
            "test_paper_risk_exit_production_path.py",
            "test_health_probe_scope.py",
            "test_market_session_shared_pnl.py",
            "test_shared_pool_late_join.py",
        ]
    )
    label = "full" if args.full else "scoped"
    log_path = root / "work" / f"architecture_boundary_tests_{label}.log"
    started = time.monotonic()
    with (
        log_path.open("w", encoding="utf-8") as log,
        contextlib.redirect_stdout(log),
        contextlib.redirect_stderr(log),
    ):
        suite = unittest.TestSuite()
        for pattern in patterns:
            suite.addTests(unittest.defaultTestLoader.discover(str(backend), pattern=pattern))
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    summary = {
        "scope": label,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "successful": result.wasSuccessful(),
        "log": str(log_path),
    }
    (root / "work" / f"architecture_boundary_tests_{label}_result.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
