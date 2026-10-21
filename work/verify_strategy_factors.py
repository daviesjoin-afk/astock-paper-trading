"""Run factor dependency regressions without reading local paper data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "test_strategy_factor_requirements", "test_candidate_factor_wiring",
    "test_factor_calibration_v2", "test_factor_group_caps", "test_factor_unit_contract",
    "test_strategy_plugins", "test_strategy_invariant_matrix", "test_market_session_boundary",
    "test_market_data_boundary", "test_candidate_snapshot_replay", "test_candidate_shared_inputs",
    "test_paper_candidate_scan", "test_production_path_golden_replay", "test_provenance_inflight_change",
    "test_hot_leader_profile", "test_paper_selection", "test_api_strategies",
    "test_paper_trading_architecture_guard", "test_selection_factor_streaming",
]
RUNNER = """
import json, os, sys, time, unittest
sys.path.insert(0, os.path.abspath('backend'))
start = time.monotonic()
suite = unittest.defaultTestLoader.loadTestsFromNames(json.loads(os.environ['FACTOR_TEST_MODULES']))
result = unittest.TextTestRunner(verbosity=2).run(suite)
with open(os.environ['FACTOR_TEST_RESULT'], 'w', encoding='utf-8') as output:
    json.dump(dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), elapsed_seconds=round(time.monotonic()-start,3),
                   python=sys.version.split()[0]), output, indent=2)
raise SystemExit(not result.wasSuccessful())
"""


def main():
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode("utf-8").split("\0")
    result_path = ROOT / "work/strategy_factor_local_result.json"
    result_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="astock-factor-review-") as temporary:
        base = Path(temporary)
        isolated = base / "source"
        for directory in (isolated, base / "data", base / "reports"):
            directory.mkdir()
        for name in set(files) - {""}:
            source = ROOT / name
            if source.is_file():
                target = isolated / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "ASTOCK_DATA_DIR": str(base / "data"), "ASTOCK_REPORT_DIR": str(base / "reports"),
               "FACTOR_TEST_MODULES": json.dumps(MODULES), "FACTOR_TEST_RESULT": str(result_path)}
        with (ROOT / "work/strategy_factor_local.log").open("w", encoding="utf-8") as output:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", RUNNER], cwd=isolated,
                                    env=env, stdout=output, stderr=output)
    if result_path.exists():
        print(result_path.read_text(encoding="utf-8"))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
