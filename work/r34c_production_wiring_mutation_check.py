#!/usr/bin/env python3
"""Reversible R34-C production-wiring semantic mutations.

Each mutation alters one safety boundary and runs the focused regression that
owns it. Source bytes are restored after every case and SHA256-checked at end.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = (
    "test_portfolio_allocation_policy.PlanIdentityTests.test_b3a_historical_v1_plan_fingerprint_remains_verifiable",
    "test_portfolio_allocation_policy.EligibilityScopeTests.test_b11_paused_economic_owner_gets_no_new_resource",
    "test_portfolio_allocation_policy.EligibilityScopeTests.test_b12_risk_exit_only_account_gets_no_entry_resource",
    "test_portfolio_allocation_policy.EligibilityScopeTests.test_b36_economic_owner_without_risk_exit_right_cannot_block_entry",
    "test_portfolio_allocation_policy.ConflictPolicyTests.test_b23_free_text_cannot_manufacture_a_canonical_intent_kind",
    "test_portfolio_runtime.PortfolioRuntimeContractTests.test_rc2_missing_exact_quote_is_partial_and_not_cost_filled",
    "test_portfolio_runtime.PortfolioRuntimeContractTests.test_rc10_runtime_pending_query_failure_is_not_composed_as_empty",
    "test_portfolio_workspace.PortfolioWorkspaceServiceTests.test_workspace_requires_matching_cycle_and_never_looks_up_latest",
    "test_portfolio_allocation_policy.PersistenceTests.test_corrupt_stored_plan_is_rejected",
    "test_paper_trading_architecture_guard.EntryCapitalPlanningIsBounded.test_guard10g_production_buy_callers_pass_cycle_and_asof",
)

POLICY = "backend/portfolio_allocation_policy.py"
RUNTIME = "backend/portfolio_runtime_service.py"
ALLOCATION_REPO = "backend/portfolio_allocation_repository.py"
WORKSPACE = "backend/portfolio_workspace_service.py"
PAPER = "backend/paper_trading.py"
MANUAL = "backend/manual_orders.py"

GUARD = ("test_paper_trading_architecture_guard.EntryCapitalPlanningIsBounded."
         "test_guard10g_production_buy_callers_pass_cycle_and_asof")

MUTATIONS = [
    {"id": "M-C1", "semantic": "missing exact plan falls back to legacy allocator",
     "edits": [(PAPER,
        'return {"filled": False, "deferred": True,\n'
        '                "status": "deferred_capacity", "reason": allocation_reason}',
        'return _strategy_pool_budget(conn, account, code)')], "detectors": [GUARD]},
    {"id": "M-C2", "semantic": "workspace selects a latest plan instead of the named plan",
     "edits": [(WORKSPACE,
        'plan = PAPRepo.get_plan(conn, str(plan_id or ""))',
        'plan = PAPRepo.get_latest_plan(conn)')],
     "detectors": ["test_portfolio_workspace.PortfolioWorkspaceServiceTests."
                   "test_workspace_requires_matching_cycle_and_never_looks_up_latest"]},
    {"id": "M-C3", "semantic": "stored allocation plan checksum is not verified",
     "edits": [(ALLOCATION_REPO,
        'or not PAP.verify_plan_fingerprint(plan)):',
        'or False):')],
     "detectors": ["test_portfolio_allocation_policy.PersistenceTests."
                   "test_corrupt_stored_plan_is_rejected"]},
    {"id": "M-C4", "semantic": "paused owner receives new-entry resources",
     "edits": [(POLICY, 'new_ok = bool(entry and account in eligible)',
                'new_ok = bool(entry)')],
     "detectors": ["test_portfolio_allocation_policy.EligibilityScopeTests."
                   "test_b11_paused_economic_owner_gets_no_new_resource"]},
    {"id": "M-C5", "semantic": "risk-exit-only owner loses its exit right",
     "edits": [(POLICY,
        'exit_ok = bool((not entry) and account in exit_scope)',
        'exit_ok = bool((not entry) and account in eligible)')],
     "detectors": ["test_portfolio_allocation_policy.EligibilityScopeTests."
                   "test_b12_risk_exit_only_account_gets_no_entry_resource"]},
    {"id": "M-C6", "semantic": "missing market quote falls back to position cost",
     "edits": [(RUNTIME, 'price = float((row or {}).get("price"))',
                'price = float((row or {}).get("price", position.get("cost")))')],
     "detectors": ["test_portfolio_runtime.PortfolioRuntimeContractTests."
                   "test_rc2_missing_exact_quote_is_partial_and_not_cost_filled"]},
    {"id": "M-C7", "semantic": "pending-order read failure becomes an empty intent list",
     "edits": [(RUNTIME,
        'except POI.PendingIntentEvidenceUnavailable:\n'
        '        pending_intents = None',
        'except POI.PendingIntentEvidenceUnavailable:\n'
        '        pending_intents = []')],
     "detectors": ["test_portfolio_runtime.PortfolioRuntimeContractTests."
                   "test_rc10_runtime_pending_query_failure_is_not_composed_as_empty"]},
    {"id": "M-C8", "semantic": "free text manufactures a canonical intent kind",
     "edits": [(POLICY, 'if kind not in INTENT_PRIORITY:', 'if False:')],
     "detectors": ["test_portfolio_allocation_policy.ConflictPolicyTests."
                   "test_b23_free_text_cannot_manufacture_a_canonical_intent_kind"]},
    {"id": "M-C9", "semantic": "economic ownership manufactures risk-exit eligibility",
     "edits": [(POLICY,
        'exit_scope = set(snapshot.risk_exit_participant_ids)',
        'exit_scope = set(snapshot.risk_exit_participant_ids) | '
        'set(snapshot.economic_owner_ids)')],
     "detectors": ["test_portfolio_allocation_policy.EligibilityScopeTests."
                   "test_b36_economic_owner_without_risk_exit_right_cannot_block_entry"]},
    {"id": "M-C10", "semantic": "manual retry re-resolves provenance from a newer plan",
     "edits": [(MANUAL,
        'allocation_provenance["portfolio_snapshot_id"]',
        'PAPRepo.get_latest_plan(conn).portfolio_snapshot_id')],
     "detectors": [GUARD]},
    {"id": "M-C11", "semantic": "stale exact plan skips snapshot revalidation",
     "edits": [(PAPER,
        'return (current["snapshot_id"] == plan["portfolio_snapshot_id"]\n'
        '            and current["snapshot_fingerprint"]\n'
        '            == plan["portfolio_snapshot_fingerprint"])',
        'return True')], "detectors": [GUARD]},
    {"id": "M-C12", "semantic": "a PLANNED allocation overrides independent entry/Risk blocks",
     "edits": [(PAPER,
        'allowed = not reasons and not q3_shadow_ready and not limit_deferred',
        'allowed = not q3_shadow_ready and not limit_deferred')], "detectors": [GUARD]},
    {"id": "M-C13", "semantic": "order loses allocation plan provenance",
     "edits": [(PAPER,
        '_order_cycle_id(conn, current_cycle["id"]), allocation_intent_kind,\n'
        '          exact_plan["portfolio_snapshot_id"], exact_plan["plan_id"],\n'
        '          exact_plan["plan_fingerprint"], exact_plan["allocation_policy_version"]),',
        '_order_cycle_id(conn, current_cycle["id"]), allocation_intent_kind,\n'
        '          exact_plan["portfolio_snapshot_id"], None,\n'
        '          exact_plan["plan_fingerprint"], exact_plan["allocation_policy_version"]),')],
     "detectors": [GUARD]},
    {"id": "M-C14", "semantic": "legacy slot allocator runs beside the exact plan",
     "edits": [(PAPER, 'slot_plan = exact_plan["slot_plan"]',
                'slot_plan = exact_plan["slot_plan"]\n    _dynamic_position_limits(conn, account)')],
     "detectors": [GUARD]},
    {"id": "M-C15", "semantic": "legacy capital allocator fallback remains in BUY",
     "edits": [(PAPER, 'slot_plan = exact_plan["slot_plan"]',
                'slot_plan = exact_plan["slot_plan"]\n    _strategy_pool_budget(conn, account, code)')],
     "detectors": [GUARD]},
    {"id": "M-C16", "semantic": "old coordinator cost fallback reaches BUY production",
     "edits": [(PAPER, '    import manual_orders as MO\n',
                '    import manual_orders as MO\n    import portfolio_coordinator\n')],
     "detectors": [GUARD]},
    {"id": "M-C17", "semantic": "current v2 policy requirement invalidates historical v1 fingerprints",
     "edits": [(POLICY,
        'and _sha(plan.fingerprint_material()) == plan.plan_fingerprint)',
        'and _sha(plan.fingerprint_material()) == plan.plan_fingerprint\n'
        '            and plan.allocation_policy_version == POLICY_VERSION)')],
     "detectors": ["test_portfolio_allocation_policy.PlanIdentityTests."
                   "test_b3a_historical_v1_plan_fingerprint_remains_verifiable"]},
]


def run(*selectors):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *selectors, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=180, env=env)


def main() -> int:
    paths = sorted({ROOT / edit[0] for case in MUTATIONS for edit in case["edits"]})
    original = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run(*BASELINE)
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-5000:])
        return 1

    detected = survived = fake = timeout = 0
    modified_paths = set()
    try:
        for case in MUTATIONS:
            touched = {ROOT / edit[0] for edit in case["edits"]}
            mutated = {path: original[path].decode("utf-8") for path in touched}
            for relative, before, after in case["edits"]:
                path = ROOT / relative
                count = mutated[path].count(before)
                if count != 1:
                    fake += 1
                    print(f"{case['id']} FAKE anchor_count={count} in {relative}")
                    break
                mutated[path] = mutated[path].replace(before, after, 1)
            else:
                for path, source in mutated.items():
                    path.write_text(source, encoding="utf-8", newline="")
                    modified_paths.add(path)
                try:
                    result = run(*case["detectors"])
                except subprocess.TimeoutExpired:
                    timeout += 1
                    print(f"{case['id']} TIMEOUT")
                else:
                    if result.returncode:
                        detected += 1
                        print(f"{case['id']} DETECTED ({case['semantic']})")
                    else:
                        survived += 1
                        print(f"{case['id']} SURVIVED ({case['semantic']})")
                        print((result.stdout + result.stderr)[-1800:])
            for path in tuple(modified_paths):
                data = original[path]
                path.write_bytes(data)
                modified_paths.remove(path)
    finally:
        for path in tuple(modified_paths):
            data = original[path]
            path.write_bytes(data)
            modified_paths.remove(path)

    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in paths)
    final = run(*BASELINE)
    print(f"M-C detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and not survived and not fake and not timeout
                 and restored and final.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
