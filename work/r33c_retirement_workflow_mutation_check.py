# -*- coding: utf-8 -*-
"""R33-C semantic mutations M-C1…M-C9; all must make focused regressions RED."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

MUTATIONS = [
    # M-C1: allow a rejected approval to execute.
    (
        "backend/strategy_retirement_workflow_service.py",
        '        if approval is None or approval.approval_action != "APPROVE":\n',
        '        if approval is None:\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c5_rejection_never_transitions",
    ),
    # M-C2: remove the explicit approval requirement.
    (
        "backend/strategy_retirement_workflow_service.py",
        '        if approval is None or approval.approval_action != "APPROVE":\n',
        '        if False:\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c4_pending_proposal_cannot_execute",
    ),
    # M-C3: stop checking the exact immutable version/checksum returned by Registry.
    (
        "backend/strategy_retirement_workflow_service.py",
        '    if (int(version.version) != int(strategy_version)\n'
        '            or str(version.checksum) != str(strategy_checksum)):\n',
        '    if False:\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c3_wrong_strategy_version_or_checksum_fails",
    ),
    # M-C4: ignore the lifecycle owner's legal transition table at proposal creation.
    (
        "backend/strategy_retirement_workflow_service.py",
        '        if candidate["target_state"] not in SL.TRANSITION_TABLE.get(current_state, ()):\n',
        '        if False:\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c7_invalid_transition_is_rejected_by_transition_table",
    ),
    # M-C5: replace exact-ID lookup with a latest-proposal fallback.
    (
        "backend/strategy_retirement_workflow_repository.py",
        '        " FROM strategy_retirement_proposals WHERE proposal_id=?", (proposal_id,),\n',
        '        " FROM strategy_retirement_proposals WHERE 1=1 ORDER BY rowid DESC LIMIT 1", (),\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_repository_never_falls_back_to_latest_proposal",
    ),
    # M-C6: perform lifecycle transition while saving approval, before execute.
    (
        "backend/strategy_retirement_workflow_service.py",
        '            stored = WFR.append_approval(conn, approval)\n',
        '            stored = WFR.append_approval(conn, approval)\n'
        '            SL.transition(conn, strategy_id=proposal.strategy_id,\n'
        '                strategy_version=proposal.strategy_version,\n'
        '                strategy_checksum=proposal.strategy_checksum,\n'
        '                expected_state=proposal.current_state,\n'
        '                target_state=proposal.target_state, actor_type="human",\n'
        '                actor_id=approval.operator_identity, reason_code="test",\n'
        '                reason_text="test", transition_kind="safety")\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c14_c15_no_scheduler_or_automatic_transition_path",
    ),
    # M-C7: remove proposal fingerprint verification.
    (
        "backend/strategy_retirement_workflow.py",
        '    return (proposal.proposal_id == proposal.proposal_fingerprint\n'
        '            and _sha(proposal.fingerprint_material()) == proposal.proposal_fingerprint)\n',
        '    return True\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c10_proposal_mutation_is_detected",
    ),
    # M-C8: remove approval fingerprint verification.
    (
        "backend/strategy_retirement_workflow.py",
        '    return (approval.approval_id == approval.approval_fingerprint\n'
        '            and _sha(approval.fingerprint_material()) == approval.approval_fingerprint)\n',
        '    return True\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c11_approval_mutation_is_detected",
    ),
    # M-C9: drop transitioned_state only on an otherwise successful exact-event retry.
    (
        "backend/strategy_retirement_workflow_service.py",
        '            result["transitioned_state"] = str(already["to_state"])\n',
        '            result.pop("transitioned_state", None)\n',
        "test_r33c_retirement_workflow.RetirementWorkflowContractTests.test_c6_approved_proposal_executes_once_through_lifecycle_owner",
    ),
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    originals: dict[Path, bytes] = {}
    detected = 0
    survived = 0
    timed_out = 0
    fake = 0
    try:
        for index, (relative, old, new, test_name) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            original = path.read_bytes()
            originals.setdefault(path, original)
            old_bytes, new_bytes = old.encode(), new.encode()
            if old_bytes not in original or original.count(old_bytes) != 1:
                print(f"M-C{index} FAKE (mutation anchor missing/ambiguous)")
                fake += 1
                continue
            path.write_bytes(original.replace(old_bytes, new_bytes, 1))
            try:
                started = time.monotonic()
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", test_name], cwd=BACKEND,
                    capture_output=True, text=True, timeout=120, check=False)
                elapsed = time.monotonic() - started
                if result.returncode != 0:
                    print(f"M-C{index} DETECTED ({elapsed:.1f}s)")
                    detected += 1
                else:
                    print(f"M-C{index} SURVIVED ({elapsed:.1f}s)")
                    print((result.stdout + result.stderr)[-2500:])
                    survived += 1
            except subprocess.TimeoutExpired:
                print(f"M-C{index} TIMEOUT")
                timed_out += 1
            finally:
                path.write_bytes(originals[path])
                if digest(path.read_bytes()) != digest(original):
                    raise RuntimeError(f"restore hash mismatch: {relative}")
        print(f"mutation = {detected}/{len(MUTATIONS)} DETECTED")
        print(f"survived={survived} fake={fake} timeout={timed_out}")
        print("restore SHA256 PASS" if survived + fake + timed_out == 0 else
              "restore SHA256 PASS (mutation outcomes need attention)")
        return 0 if (detected == len(MUTATIONS) and survived == fake == timed_out == 0) else 1
    finally:
        for path, original in originals.items():
            path.write_bytes(original)
            if digest(path.read_bytes()) != digest(original):
                raise RuntimeError(f"final restore hash mismatch: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
