#!/usr/bin/env python3
"""Reversible R34-B semantic mutations with byte-for-byte restore checks.

Each mutation protects a business invariant of the canonical allocation policy.
A mutation that survives (the detector still passes) means the invariant is not
actually enforced by the code.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = ("test_portfolio_allocation_policy", "test_paper_allocation_limits_equivalence")

POLICY = "backend/portfolio_allocation_policy.py"
SERVICE = "backend/portfolio_allocation_service.py"
REPO = "backend/portfolio_allocation_repository.py"


def _case(suite, name):
    return f"test_portfolio_allocation_policy.{suite}.{name}"


MUTATIONS = [
    {
        "id": "M-B1",
        "semantic": "remove the snapshot fingerprint binding",
        "edits": [(POLICY,
                   '    if not PR.verify_snapshot_fingerprint(snapshot):\n'
                   '        raise PortfolioAllocationPolicyError("portfolio_snapshot_fingerprint_mismatch")',
                   '    if False:\n'
                   '        raise PortfolioAllocationPolicyError("portfolio_snapshot_fingerprint_mismatch")')],
        "detectors": [_case("PlanIdentityTests", "test_b4_corrupt_snapshot_fingerprint_fails_closed")],
    },
    {
        "id": "M-B2",
        "semantic": "replace the explicit snapshot with a latest lookup",
        "edits": [(SERVICE,
                   '        snapshot = PRRepo.get_snapshot(conn, portfolio_snapshot_id)',
                   '        snapshot = PRRepo.get_snapshot(conn, portfolio_snapshot_id) or '
                   'PRRepo.get_snapshot(conn, str(conn.execute('
                   '"SELECT snapshot_id FROM portfolio_runtime_snapshots'
                   ' ORDER BY created_at DESC, snapshot_id DESC LIMIT 1").fetchone()[0]))')],
        "detectors": [_case("LedgerImmutabilityTests",
                            "test_b27_evaluation_leaves_the_formal_ledger_untouched")],
    },
    {
        "id": "M-B3",
        "semantic": "allow a current strategy head to replace the snapshot pin",
        "edits": [(POLICY,
                   '        if pin is None or (str(pin.get("strategy_id")) != item.strategy_id\n'
                   '                           or int(pin.get("strategy_version")) != int(item.strategy_version)\n'
                   '                           or str(pin.get("strategy_checksum")) != item.strategy_checksum):',
                   '        if False and (pin is None or (str(pin.get("strategy_id")) != item.strategy_id\n'
                   '                           or int(pin.get("strategy_version")) != int(item.strategy_version)\n'
                   '                           or str(pin.get("strategy_checksum")) != item.strategy_checksum)):')],
        "detectors": [_case("DeclarationAndWeightContractTests",
                            "test_b7_current_registry_head_cannot_replace_a_snapshot_pin")],
    },
    {
        "id": "M-B4",
        "semantic": "a paused economic owner receives new-resource allocation",
        "edits": [(POLICY,
                   '        new_ok = bool(entry and account in eligible)',
                   '        new_ok = bool(entry)')],
        "detectors": [_case("EligibilityScopeTests",
                            "test_b11_paused_economic_owner_gets_no_new_resource")],
    },
    {
        "id": "M-B5",
        "semantic": "a risk-exit-only account receives new-entry allocation",
        "edits": [(POLICY,
                   '        new_ok = bool(entry and account in eligible)',
                   '        new_ok = bool(entry and account in exit_scope)')],
        "detectors": [_case("EligibilityScopeTests",
                            "test_b12_risk_exit_only_account_gets_no_entry_resource")],
    },
    {
        "id": "M-B6",
        "semantic": "missing capacity evidence is treated as zero pending",
        "edits": [(POLICY,
                   '            "used_amount": None,\n'
                   '            "pending_amount": None,\n'
                   '            "headroom_amount": None,',
                   '            "used_amount": None,\n'
                   '            "pending_amount": 0.0,\n'
                   '            "headroom_amount": None,')],
        "detectors": [_case("EvidenceGateTests", "test_b16_missing_pending_is_not_pending_zero")],
    },
    {
        "id": "M-B7",
        "semantic": "cost basis is used as market exposure",
        "edits": [(POLICY,
                   '        "market_value_by_account": (\n'
                   '            _thaw(exposure.facts.get("market_value_by_account"))\n'
                   '            if exposure.status == PR.AVAILABLE else None),',
                   '        "market_value_by_account": '
                   '_thaw(exposure.facts.get("position_cost_by_account")),')],
        "detectors": [_case("EvidenceGateTests",
                            "test_b18_cost_basis_never_substitutes_market_value")],
    },
    {
        "id": "M-B8",
        "semantic": "missing correlation is treated as zero",
        "edits": [(POLICY,
                   '    return {"status": UNAVAILABLE, "correlation": None,',
                   '    return {"status": UNAVAILABLE, "correlation": 0.0,')],
        "detectors": [_case("EvidenceGateTests",
                            "test_b19_missing_correlation_does_not_become_zero")],
    },
    {
        "id": "M-B9",
        "semantic": "a missing canonical weight silently defaults to 1.0",
        "edits": [(POLICY,
                   '    if set(eligible) - set(validated_weights):\n'
                   '        raise PortfolioAllocationPolicyError(\n'
                   '            "canonical_allocation_weight_set_incomplete")',
                   '    if set(eligible) - set(validated_weights):\n'
                   '        validated_weights.update({key: 1.0 for key in set(eligible) - set(validated_weights)})')],
        "detectors": [_case("DeclarationAndWeightContractTests",
                            "test_b14_missing_factor_never_becomes_a_canonical_one")],
    },
    {
        "id": "M-B10",
        "semantic": "remove the hard pool slot cap",
        "edits": [(POLICY,
                   '        hard_pool_cap=hard_pool_cap,\n'
                   '        strategy_max_positions=int(strategy_max_positions),',
                   '        hard_pool_cap=10 ** 9,\n'
                   '        strategy_max_positions=int(strategy_max_positions),')],
        "detectors": [_case("SlotPlanTests", "test_b9_total_slots_never_exceed_the_hard_pool_cap")],
    },
    {
        "id": "M-B11",
        "semantic": "net opposite strategy intents together",
        "edits": [(POLICY,
                   '            deferred.extend(deferred_ids)',
                   '            deferred.extend(deferred_ids)\n'
                   '            rows = [row for row in rows if row["intent_id"] not in deferred_ids]')],
        "detectors": [_case("ConflictPolicyTests",
                            "test_b22_opposite_intents_stay_distinct_and_are_not_netted")],
    },
    {
        "id": "M-B12",
        "semantic": "new entry outranks risk exit",
        "edits": [(POLICY,
                   'INTENT_PRIORITY = {kind: index for index, kind in enumerate(INTENT_KINDS)}',
                   'INTENT_PRIORITY = {kind: len(INTENT_KINDS) - 1 - index'
                   ' for index, kind in enumerate(INTENT_KINDS)}')],
        "detectors": [_case("ConflictPolicyTests",
                            "test_b21_risk_exit_sorts_before_every_entry_intent")],
    },
    {
        "id": "M-B13",
        "semantic": "remove the allocation policy version from the fingerprint",
        "edits": [(POLICY, '        "allocation_policy_version": POLICY_VERSION,\n', ''),
                  (POLICY, '            "allocation_policy_version": self.allocation_policy_version,\n', '')],
        "detectors": [_case("PlanIdentityTests",
                            "test_b3_policy_version_enters_the_fingerprint")],
    },
    {
        "id": "M-B14",
        "semantic": "plan append becomes replace/update instead of append-only",
        "edits": [(REPO,
                   '        "INSERT OR IGNORE INTO portfolio_allocation_plans"',
                   '        "INSERT OR REPLACE INTO portfolio_allocation_plans"')],
        "detectors": [_case("PersistenceTests",
                            "test_b26_same_plan_id_with_different_content_conflicts")],
    },
    {
        "id": "M-B15",
        "semantic": "a negative pool or slot bound reaches the arithmetic",
        "edits": [(POLICY,
                   '        if number < 0:\n'
                   '            raise PortfolioAllocationPolicyError(f"canonical_{name}_invalid")',
                   '        if False:\n'
                   '            raise PortfolioAllocationPolicyError(f"canonical_{name}_invalid")')],
        "detectors": [_case("SlotPlanTests",
                            "test_b31_negative_pool_and_slot_bounds_fail_closed")],
    },
    {
        "id": "M-B16",
        "semantic": "a negative canonical weight reaches the arithmetic",
        "edits": [(POLICY,
                   '        if weight < 0.0:\n'
                   '            raise PortfolioAllocationPolicyError("canonical_weight_invalid")',
                   '        if False:\n'
                   '            raise PortfolioAllocationPolicyError("canonical_weight_invalid")')],
        "detectors": [_case("DeclarationAndWeightContractTests",
                            "test_b32_negative_canonical_weight_fails_closed")],
    },
    {
        "id": "M-B17",
        "semantic": "an already-denied risk exit still arbitrates a valid entry",
        "edits": [(POLICY,
                   '        blockers = [row for row in group if row["intent_kind"] == "RISK_EXIT"\n'
                   '                    and row["exit_right_eligible"]]',
                   '        blockers = [row for row in group if row["intent_kind"] == "RISK_EXIT"]')],
        "detectors": [_case("ConflictPolicyTests",
                            "test_b33_a_denied_risk_exit_does_not_arbitrate")],
    },
    {
        "id": "M-B18",
        "semantic": "the raw lifecycle state is used as the allocation stage",
        "edits": [(SERVICE,
                   '        stage = _allocation_stage(conn, strategy_id, pin.get("lifecycle_state"))',
                   '        stage = str(pin.get("lifecycle_state") or "quarantined")')],
        "detectors": [_case("LedgerImmutabilityTests",
                            "test_b27_evaluation_leaves_the_formal_ledger_untouched")],
    },
]


def run(*args):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *args), cwd=BACKEND,
                          capture_output=True, text=True, timeout=300, env=env)


def main() -> int:
    paths = sorted({ROOT / edit[0] for case in MUTATIONS for edit in case["edits"]})
    original = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}

    baseline = run(*BASELINE, "-q")
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-4000:])
        return 1

    detected = survived = fake = timeout = 0
    try:
        for case in MUTATIONS:
            sources = {path: original[path].decode("utf-8")
                       for path in {ROOT / edit[0] for edit in case["edits"]}}
            mutated = dict(sources)
            anchors_ok = True
            for relative, before, after in case["edits"]:
                path = ROOT / relative
                count = mutated[path].count(before)
                if count != 1:
                    fake += 1
                    anchors_ok = False
                    print(f"{case['id']} FAKE anchor_count={count} in {relative}")
                    break
                mutated[path] = mutated[path].replace(before, after, 1)
            if not anchors_ok:
                continue
            for path, text in mutated.items():
                path.write_text(text, encoding="utf-8", newline="")
            try:
                result = run(*case["detectors"], "-q")
            except subprocess.TimeoutExpired:
                timeout += 1
                print(f"{case['id']} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"{case['id']} DETECTED  ({case['semantic']})")
                else:
                    survived += 1
                    print(f"{case['id']} SURVIVED  ({case['semantic']})")
                    print((result.stdout + result.stderr)[-1500:])
            finally:
                for path, data in original.items():
                    path.write_bytes(data)
    finally:
        for path, data in original.items():
            path.write_bytes(data)

    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in paths)
    final = run(*BASELINE, "-q")
    print(f"M-B detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and not survived and not fake and not timeout
                 and restored and final.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
