#!/usr/bin/env python3
"""Small reversible semantic mutations for R32-A comparable context."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

MUTATIONS = [
    ("backend/market_data_contract.py", '"rows": sorted(row_blobs),', '"rows": [],',
     "test_market_data_boundary"),
    ("backend/market_data_contract.py", '"observed_at": snapshot.observed_at,',
     '"observed_at": None,', "test_market_data_boundary"),
    ("backend/simulation_runtime_context.py", '"strategy_version": strategy_version,',
     '"strategy_version": 1,', "test_simulation_runtime_context"),
    ("backend/simulation_runtime_context.py", '"market_policy_name": str(market_policy_name),',
     '"market_policy_name": "ignored",', "test_simulation_runtime_context"),
    ("backend/paper_decision_audit.py", '"active_runtime_context_unavailable"',
     '"available"', "test_paper_decision_audit"),
    ("backend/execution_planner.py", '    day = MDC.canonical_day(asof_day) or ""\n    quote = dict(quote or {})',
     '    conn.execute("SELECT 1")\n    day = MDC.canonical_day(asof_day) or ""\n    quote = dict(quote or {})',
     "test_execution_planner.ExecutionStateBuilderTests.test_explicit_execution_state_builder_has_no_ledger_reads"),
    ("backend/execution_planner.py", 'fees = round(estimate_execution_fees(amount, side), 2)',
     'fees = round(amount * 0.002, 2)',
     "test_execution_planner.ExecutionStateBuilderTests.test_execution_fee_model_has_one_canonical_call_site"),
    ("backend/execution_planner.py", 'reasons.extend(account_risk_gate(state.account_risk_state))',
     'reasons.extend([])',
     "test_execution_planner.PlanEntryTests.test_plan_entry_collects_reasons_from_every_gate"),
    ("backend/simulation_runtime_context.py", '"tradability_evidence_fingerprints": dict(tradability),',
     '"tradability_evidence_fingerprints": {},', "test_simulation_runtime_context"),
    ("backend/simulation_runtime_context.py", '"execution_ruleset_version": str(execution_ruleset_version),',
     '"execution_ruleset_version": "ignored",', "test_simulation_runtime_context"),
    ("backend/execution_planner.py", '"buying_power": state.buying_power,',
     '"buying_power": None,',
     "test_execution_planner.ExecutionStateBuilderTests.test_execution_state_fingerprint_tracks_every_decision_field"),
    ("backend/market_data_contract.py", '"degraded_reason": snapshot.degraded_reason,',
     '"degraded_reason": None,',
     "test_market_data_boundary.MarketDataContractTests.test_R32A_degraded_reason_is_part_of_market_fact_identity"),
]


def run(command: tuple[str, ...], timeout: int = 60):
    return subprocess.run(command, cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    files = sorted({ROOT / item[0] for item in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run((sys.executable, "-m", "unittest", "test_market_data_boundary",
                    "test_simulation_runtime_context", "test_execution_planner",
                    "test_paper_decision_audit", "-q"))
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-5000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timeout_count = survived = 0
    try:
        for index, (relative, old, new, selector) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-R32A-{index:02d} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run((sys.executable, "-m", "unittest", selector, "-q"))
            except subprocess.TimeoutExpired:
                timeout_count += 1
                print(f"M-R32A-{index:02d} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-R32A-{index:02d} DETECTED")
                else:
                    survived += 1
                    print(f"M-R32A-{index:02d} SURVIVED")
                    print((result.stdout + result.stderr)[-1200:])
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in files)
    final = run((sys.executable, "-m", "unittest", "test_market_data_boundary",
                 "test_simulation_runtime_context", "test_execution_planner",
                 "test_paper_decision_audit", "-q"))
    print(f"mutation = {detected}/{len(MUTATIONS)} DETECTED")
    print(f"survived = {survived}")
    print(f"fake = {fake}")
    print(f"timeout = {timeout_count}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if detected == len(MUTATIONS) and not survived and not fake \
        and not timeout_count and restored and final.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
