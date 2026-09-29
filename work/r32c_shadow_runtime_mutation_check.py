#!/usr/bin/env python3
"""Reversible semantic mutations for isolated R32-C Shadow execution."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

MUTATIONS = [
    (
        "backend/shadow_runtime.py",
        'return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),',
        'return json.dumps(_plain(value), sort_keys=False, separators=(",", ":"),',
        "test_shadow_runtime.ShadowRuntimeTests.test_environment_identity_ignores_mapping_insertion_order",
    ),
    (
        "backend/shadow_runtime.py",
        'import strategy_lifecycle as SL\n',
        'import strategy_lifecycle as SL\nimport marketdata_providers\n',
        "test_shadow_runtime.ShadowRuntimeTests.test_c3_provider_and_archive_lookups_are_not_used",
    ),
    (
        "backend/shadow_runtime.py",
        '    if spec.environment_fingerprint != environment.identity.environment_fingerprint:\n'
        '        raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")',
        '    if False:\n'
        '        raise ShadowRuntimeError("NOT_COMPARABLE", "shadow_environment_not_comparable")',
        "test_shadow_runtime.ShadowRuntimeTests.test_c6_environment_mismatch_is_rejected",
    ),
    (
        "backend/shadow_run_repository.py",
        '    conn.execute(\n'
        '        """INSERT OR IGNORE INTO shadow_runs',
        '    conn.execute("UPDATE paper_accounts SET cash=cash+1")\n'
        '    conn.execute(\n'
        '        """INSERT OR IGNORE INTO shadow_runs',
        "test_shadow_runtime.ShadowRuntimeTests.test_c4_shadow_repository_does_not_change_formal_ledgers",
    ),
    (
        "backend/shadow_runtime.py",
        '        if previous_run is None or previous_run.run_id != spec.previous_shadow_run_id:\n'
        '            raise ValueError("explicit_previous_shadow_run_required")',
        '        if False:\n'
        '            raise ValueError("explicit_previous_shadow_run_required")',
        "test_shadow_runtime.ShadowRuntimeTests.test_c7_continuation_requires_explicit_previous_run",
    ),
    (
        "backend/shadow_runtime.py",
        '    if lifecycle_state != "shadow":\n'
        '        raise ValueError("challenger_lifecycle_not_shadow")',
        '    if False:\n'
        '        raise ValueError("challenger_lifecycle_not_shadow")',
        "test_shadow_runtime.ShadowRuntimeTests.test_c5_only_shadow_lifecycle_is_runnable",
    ),
    (
        "backend/execution_planner.py",
        'reference_at=(runtime_context.decision_at if runtime_context is not None else None),',
        'reference_at=None,',
        "test_shadow_runtime.ShadowRuntimeTests.test_c2_entry_freshness_uses_supplied_decision_instant",
    ),
    (
        "backend/shadow_runtime.py",
        'shared_cash=state.reference_cash,',
        'shared_cash=candidate.entry_state.shared_cash,',
        "test_shadow_runtime.ShadowRuntimeTests.test_shadow_entry_cash_is_bounded_by_reference_capital",
    ),
    (
        "backend/shadow_runtime.py",
        'sellable_quantity=state.positions, session_consumed_quantity={},',
        'sellable_quantity=state.sellable_quantity, session_consumed_quantity=state.session_consumed_quantity,',
        "test_shadow_runtime.ShadowRuntimeTests.test_c8_replaying_named_run_is_independent_of_later_run",
    ),
    (
        "backend/shadow_runtime.py",
        'not isinstance(evidence, TA.TradabilityDecision)',
        'False',
        "test_shadow_runtime.ShadowRuntimeTests.test_frozen_environment_requires_owner_typed_tradability_decision",
    ),
    (
        "backend/shadow_runtime.py",
        '        if captured_environment != expected_environment:\n',
        '        if False:\n',
        "test_shadow_runtime.ShadowRuntimeTests.test_c6_each_captured_shared_dimension_fails_not_comparable",
    ),
    (
        "backend/shadow_runtime.py",
        '            "execution_ruleset_identity": active.execution_ruleset_version,\n',
        '',
        "test_shadow_runtime.ShadowRuntimeTests.test_c6_each_captured_shared_dimension_fails_not_comparable",
    ),
    (
        "backend/shadow_runtime.py",
        '    if len({(row.symbol, row.side) for row in candidates}) != len(candidates):\n',
        '    if False:\n',
        "test_shadow_runtime.ShadowRuntimeTests.test_duplicate_candidate_legs_are_rejected",
    ),
    (
        "backend/shadow_runtime.py",
        '                "desired_quantity": int(candidate.desired_quantity),\n',
        '                "desired_quantity": 0,\n',
        "test_shadow_runtime.ShadowRuntimeTests.test_shadow_run_evidence_keeps_candidate_inputs",
    ),
    (
        "backend/shadow_runtime.py",
        '                "entry_gate_state": _captured_entry_state_projection(candidate.entry_state),\n',
        '                "entry_gate_state": {},\n',
        "test_shadow_runtime.ShadowRuntimeTests.test_shadow_run_evidence_keeps_candidate_inputs",
    ),
    # M-C16：把显式 frozen policy 变异回隐式的当前 owner 解析。
    (
        "backend/shadow_runtime.py",
        '                runtime_context=strategy_runtime_context,\n'
        '                execution_policy=execution_policy,\n',
        '                runtime_context=strategy_runtime_context,\n',
        "test_shadow_runtime.ShadowRuntimeTests.test_frozen_entry_policy_is_the_only_policy_source_on_replay",
    ),
]


def run(args: tuple[str, ...], timeout: int = 90):
    return subprocess.run(args, cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    files = sorted({ROOT / mutation[0] for mutation in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run((sys.executable, "-m", "unittest", "test_shadow_runtime", "-q"))
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
                print(f"M-C{index} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run((sys.executable, "-m", "unittest", selector, "-q"))
            except subprocess.TimeoutExpired:
                timeout_count += 1
                print(f"M-C{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-C{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-C{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1200:])
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in files)
    final = run((sys.executable, "-m", "unittest", "test_shadow_runtime", "-q"))
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
