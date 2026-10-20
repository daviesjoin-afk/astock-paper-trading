"""Run scan regressions in a disposable source copy, never local paper data."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCOPED = [
    "test_paper_scan_baseline.py", "test_candidate_shared_inputs.py",
    "test_candidate_snapshot_replay.py", "test_candidate_column_reads.py",
    "test_paper_candidate_scan.py", "test_microstructure_budget.py",
    "test_minute_trend_reuse.py", "test_alt_data_point_in_time.py",
    "test_provenance_inflight_change.py", "test_production_path_golden_replay.py",
]
RUNNER = """
import contextlib, json, os, sys, time, unittest
sys.path.insert(0, os.path.abspath('backend'))
started = time.monotonic()
suite = unittest.TestSuite()
for pattern in json.loads(os.environ['SCAN_TEST_PATTERNS']):
    suite.addTests(unittest.defaultTestLoader.discover('backend', pattern=pattern))
result = unittest.TextTestRunner(verbosity=2).run(suite)
with open(os.environ['SCAN_TEST_RESULT'], 'w', encoding='utf-8') as output:
    json.dump(dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), elapsed_seconds=round(time.monotonic()-started, 3),
                   python=sys.version.split()[0]), output, indent=2)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    label = "full" if args.full else "scoped"
    result_path = ROOT / "work" / f"scan_input_reuse_{label}_result.json"
    log_path = ROOT / "work" / f"scan_input_reuse_{label}.log"
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode("utf-8").split("\0")
    with tempfile.TemporaryDirectory(prefix="astock-scan-reuse-") as temporary:
        isolated = Path(temporary) / "source"
        isolated.mkdir()
        for name in set(files) - {""}:
            source = ROOT / name
            if source.is_file():
                target = isolated / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "ASTOCK_DATA_DIR": str(Path(temporary) / "data"),
               "ASTOCK_REPORT_DIR": str(Path(temporary) / "reports"),
               "SCAN_TEST_PATTERNS": json.dumps(["test_*.py"] if args.full else SCOPED),
               "SCAN_TEST_RESULT": str(result_path)}
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run([sys.executable, "-X", "utf8", "-c", RUNNER],
                                       cwd=isolated, env=env, stdout=log, stderr=log)
    if result_path.exists():
        print(result_path.read_text(encoding="utf-8"))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
