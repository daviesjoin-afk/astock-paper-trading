#!/usr/bin/env python3
"""Reversible semantic mutations for R32-B Active comparable evidence."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

MUTATIONS = [
    (
        "backend/execution_planner.py",
        'f"{row.get(\'code\')}@{asof_day}": tradability_fingerprint,',
        'f"{row.get(\'code\')}@{asof_day}": "0" * 64,',
        "test_execution_planner.ExecutionStateBuilderTests.test_production_fact_capture_binds_the_single_owner_read_to_context_and_execution",
    ),
    (
        "backend/execution_planner.py",
        '    if not bool(getattr(tradability, "evidence_present", False)):\n'
        '        return SRC.ActiveRuntimeContextResult.unavailable(\n'
        '            "missing_tradability_evidence",\n'
        '        )\n'
        '    tradability_fingerprint = str(getattr(tradability, "fingerprint", "") or "")\n'
        '    if not tradability_fingerprint:\n'
        '        return SRC.ActiveRuntimeContextResult.unavailable(\n'
        '            "missing_tradability_evidence",\n'
        '        )',
        '    if not bool(getattr(tradability, "evidence_present", False)):\n'
        '        tradability_fingerprint = "0" * 64\n'
        '    else:\n'
        '        tradability_fingerprint = str(getattr(tradability, "fingerprint", "") or "")\n'
        '    if not tradability_fingerprint:\n'
        '        tradability_fingerprint = "0" * 64',
        "test_execution_planner.ExecutionStateBuilderTests.test_missing_owner_fact_remains_unavailable_after_a_later_archive_fact",
    ),
    (
        "backend/execution_planner.py",
        'tradability = TA.tradability_at(\n'
        '        row.get("code"), day, decision_time=execution_asof,\n'
        '        repository=TA.TradabilityArchiveRepository(conn),\n'
        '    )',
        'tradability = TA.tradability_at(\n'
        '        row.get("code"), day, decision_time=execution_asof,\n'
        '        repository=TA.TradabilityArchiveRepository(conn),\n'
        '    )\n'
        '    TA.tradability_at(\n'
        '        row.get("code"), day, decision_time=execution_asof,\n'
        '        repository=TA.TradabilityArchiveRepository(conn),\n'
        '    )',
        "test_execution_planner.ExecutionStateBuilderTests.test_production_fact_capture_binds_the_single_owner_read_to_context_and_execution",
    ),
    (
        "backend/paper_decision_audit.py",
        'None if context_fingerprint else unavailable_reason',
        'None if context_fingerprint else "missing_strategy_identity"',
        "test_paper_decision_audit.WithDecisionSnapshotContractTests.test_snapshot_preserves_specific_unavailable_reason_without_lookup",
    ),
    (
        "backend/execution_planner.py",
        '"AVAILABLE" if runtime_context is not None else "UNAVAILABLE"',
        '"AVAILABLE"',
        "test_execution_planner.ExecutionStateBuilderTests.test_missing_owner_fact_remains_unavailable_after_a_later_archive_fact",
    ),
    (
        "backend/execution_planner.py",
        "    except SRT.StrategyRuntimeContextUnavailable:",
        "    except ValueError:",
        "test_execution_planner.ExecutionStateBuilderTests.test_expected_cycle_runtime_absence_is_unavailable_but_system_errors_propagate",
    ),
]


def run(args: tuple[str, ...], timeout: int = 90):
    return subprocess.run(
        args, cwd=BACKEND, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def main() -> int:
    files = sorted({ROOT / mutation[0] for mutation in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run((sys.executable, "-m", "unittest", "test_execution_planner",
                    "test_paper_decision_audit", "test_simulation_runtime_context", "-q"))
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-4000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timeout_count = survived = 0
    try:
        for index, (relative, old, new, selector) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-B{index} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run((sys.executable, "-m", "unittest", selector, "-q"))
            except subprocess.TimeoutExpired:
                timeout_count += 1
                print(f"M-B{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-B{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-B{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1200:])
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in files)
    final = run((sys.executable, "-m", "unittest", "test_execution_planner",
                 "test_paper_decision_audit", "test_simulation_runtime_context", "-q"))
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
