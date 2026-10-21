"""Entry-plan regression suite in disposable source/data directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "test_entry_plan_fingerprint_reuse", "test_strategy_buy_commit_convergence",
    "test_paper_timing", "test_portfolio_runtime", "test_portfolio_allocation_policy",
    "test_portfolio_allocation_weights", "test_portfolio_read_model",
    "test_portfolio_order_intents", "test_paper_dynamic_allocation",
    "test_provenance_inflight_change", "test_execution_planner",
    "test_order_cycle_provenance", "test_execution_verification",
    "test_paper_trading_architecture_guard", "test_r26_execution_authority_guard",
    "test_r32e1_active_buy_provenance", "test_paper_portfolio",
    "test_portfolio_fact_contract", "test_execution_lifecycle",
    "test_market_data_boundary",
]
RUNNER = """
import json, os, sys, time, unittest
sys.path.insert(0, os.path.abspath('backend'))
start = time.monotonic()
suite = unittest.defaultTestLoader.loadTestsFromNames(json.loads(os.environ['ENTRY_TEST_MODULES']))
result = unittest.TextTestRunner(verbosity=2).run(suite)
with open(os.environ['ENTRY_TEST_RESULT'], 'w', encoding='utf-8') as output:
    json.dump(dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), elapsed_seconds=round(time.monotonic()-start,3),
                   python=sys.version.split()[0]), output, indent=2)
raise SystemExit(not result.wasSuccessful())
"""


def main():
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode("utf-8").split("\0")
    result_path = ROOT / "work/entry_plan_local_result.json"
    with tempfile.TemporaryDirectory(prefix="astock-entry-plan-") as temporary:
        base = Path(temporary)
        isolated = base / "source"
        isolated.mkdir()
        (base / "data").mkdir()
        (base / "reports").mkdir()
        for name in set(files) - {""}:
            source = ROOT / name
            if source.is_file():
                target = isolated / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "ASTOCK_DATA_DIR": str(base / "data"), "ASTOCK_REPORT_DIR": str(base / "reports"),
               "ENTRY_TEST_MODULES": json.dumps(MODULES), "ENTRY_TEST_RESULT": str(result_path)}
        with (ROOT / "work/entry_plan_local.log").open("w", encoding="utf-8") as output:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", RUNNER], cwd=isolated,
                                    env=env, stdout=output, stderr=output)
    if result_path.exists():
        print(result_path.read_text(encoding="utf-8"))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
