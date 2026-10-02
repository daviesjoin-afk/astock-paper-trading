# -*- coding: utf-8 -*-
"""R33-B semantic mutations (M-R1 … M-R9).

每条变异只破坏一个 contract，并且必须让指定回归变 RED：

    M-R1  snapshot identity leaves the decision fingerprint
    M-R2  UNAVAILABLE treated as sufficient evidence
    M-R3  PARTIAL treated as sufficient evidence
    M-R4  a latest-snapshot fallback introduced
    M-R5  the policy calls a lifecycle transition
    M-R6  the policy version leaves the decision fingerprint
    M-R7  the snapshot identity check removed
    M-R8  the decision table stops being append-only
    M-R9  health snapshot repository rejections stop being translated

Usage:  python work/r33b_retirement_mutation_check.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

MUTATIONS = [
    # M-R1：快照身份离开决策指纹。
    (
        "backend/strategy_retirement_policy.py",
        '        "snapshot_id": snapshot.snapshot_id,\n'
        '        "snapshot_fingerprint": snapshot.snapshot_fingerprint,\n',
        '        "snapshot_id": None,\n'
        '        "snapshot_fingerprint": None,\n',
        "test_r33b_retirement_policy.RetirementDecisionContractTests.test_the_snapshot_identity_is_part_of_the_decision_identity",
    ),
    # M-R2：把 UNAVAILABLE 当成「够用」（等价于把「不知道」当成 0/没问题）。
    (
        "backend/strategy_retirement_policy.py",
        'SUFFICIENT_STATUSES = frozenset({SH.STATUS_AVAILABLE, SH.STATUS_NOT_APPLICABLE})\n',
        'SUFFICIENT_STATUSES = frozenset({SH.STATUS_AVAILABLE, SH.STATUS_NOT_APPLICABLE,\n'
        '                                  SH.STATUS_UNAVAILABLE})\n',
        "test_r33b_retirement_policy.RetirementEvidenceGateTests.test_p5b_an_unavailable_dimension_on_a_complete_snapshot_still_blocks",
    ),
    # M-R3：把 PARTIAL 当成「够用」。
    (
        "backend/strategy_retirement_policy.py",
        'SUFFICIENT_STATUSES = frozenset({SH.STATUS_AVAILABLE, SH.STATUS_NOT_APPLICABLE})\n',
        'SUFFICIENT_STATUSES = frozenset({SH.STATUS_AVAILABLE, SH.STATUS_NOT_APPLICABLE,\n'
        '                                  SH.STATUS_PARTIAL})\n',
        "test_r33b_retirement_policy.RetirementEvidenceGateTests.test_p6_partial_execution_evidence_can_never_retire",
    ),
    # M-R4：评估时兜底到「最新一份快照」。
    (
        "backend/strategy_retirement_service.py",
        '    if snapshot is None:\n'
        '        raise RP.RetirementPolicyError("health_snapshot_not_found")\n',
        '    if snapshot is None:\n'
        '        row = conn.execute("SELECT snapshot_id FROM strategy_health_snapshots"\n'
        '                           " ORDER BY rowid DESC LIMIT 1").fetchone()\n'
        '        snapshot = SHRepo.get_snapshot(conn, str(row[0])) if row else None\n'
        '    if snapshot is None:\n'
        '        raise RP.RetirementPolicyError("health_snapshot_not_found")\n',
        "test_r33b_retirement_policy.RetirementApiSurfaceTests.test_unknown_evidence_is_404_and_identity_conflicts_are_400",
    ),
    # M-R5：policy 里出现 lifecycle transition 调用。
    (
        "backend/strategy_retirement_policy.py",
        "    summary = _evidence_summary(snapshot)\n",
        '    __import__("strategy_lifecycle").transition(snapshot)\n'
        "    summary = _evidence_summary(snapshot)\n",
        "test_r33b_retirement_policy.RetirementPurityAndSafetyTests.test_p10_the_policy_never_mutates_the_lifecycle",
    ),
    # M-R6：policy version 离开决策指纹。
    (
        "backend/strategy_retirement_policy.py",
        '        "policy_version": policy_version,\n'
        '        "snapshot_id": snapshot.snapshot_id,\n',
        '        "policy_version": None,\n'
        '        "snapshot_id": snapshot.snapshot_id,\n',
        "test_r33b_retirement_policy.RetirementDecisionContractTests.test_p3_policy_version_is_part_of_the_decision_identity",
    ),
    # M-R7：快照身份校验被拿掉。
    (
        "backend/strategy_retirement_policy.py",
        "    if not SH.verify_snapshot_fingerprint(snapshot):\n"
        "        # 被篡改或损坏的快照不得作为决策输入。\n"
        "        raise RetirementPolicyError(REASON_SNAPSHOT_IDENTITY_MISMATCH)\n",
        "    if False:\n"
        "        raise RetirementPolicyError(REASON_SNAPSHOT_IDENTITY_MISMATCH)\n",
        "test_r33b_retirement_policy.RetirementDecisionContractTests.test_p13_a_tampered_snapshot_fails_closed",
    ),
    # M-R8：决策表不再是 append-only（UPDATE 闸门被拿掉）。
    (
        "backend/paper_schema_migrations.py",
        '        """CREATE TRIGGER IF NOT EXISTS strategy_retirement_decisions_no_update\n'
        "           BEFORE UPDATE ON strategy_retirement_decisions\n"
        "           BEGIN SELECT RAISE(ABORT,'strategy retirement decisions are append-only'); END\"\"\"\n",
        '        """SELECT 1"""\n',
        "test_r33b_retirement_policy.RetirementDecisionContractTests.test_the_decision_table_rejects_update_and_delete",
    ),
    # M-R9：health snapshot 的仓库级拒绝不再被翻译（畸形 id 逃成 5xx）。
    (
        "backend/strategy_retirement_service.py",
        '    try:\n'
        '        snapshot = SHRepo.get_snapshot(conn, snapshot_id)\n'
        '    except SHRepo.StrategyHealthRepositoryError as exc:\n'
        '        # 畸形 id 是 caller 的输入错误，存储行自校验失败是损坏：两者都在这里翻译成\n'
        '        # 受控的 retirement 拒绝，HTTP 层才会给出 4xx 而不是 500。\n'
        '        reason = ("explicit_health_snapshot_id_required"\n'
        '                  if str(exc) == "explicit_health_snapshot_id_required"\n'
        '                  else "health_snapshot_corrupt")\n'
        '        raise RP.RetirementPolicyError(reason) from exc\n',
        '    snapshot = SHRepo.get_snapshot(conn, snapshot_id)\n',
        "test_r33b_retirement_policy.RetirementApiSurfaceTests.test_a_malformed_or_corrupt_snapshot_id_is_a_controlled_4xx",
    ),
]

BASELINE_MODULE = "test_r33b_retirement_policy"


def run(selector: str) -> subprocess.CompletedProcess:
    return subprocess.run((sys.executable, "-m", "unittest", selector, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)


def restore(path: Path, data: bytes, attempts: int = 10) -> None:
    for attempt in range(1, attempts + 1):
        try:
            path.write_bytes(data)
            if path.read_bytes() == data:
                return
        except OSError:
            pass
        time.sleep(0.5 * attempt)
    raise SystemExit(f"RESTORE FAILED for {path}: worktree is not clean")


def restore_all(original: dict[Path, bytes]) -> list[str]:
    failures = []
    for path, data in original.items():
        try:
            restore(path, data)
        except SystemExit as exc:
            failures.append(str(exc))
    return failures


def main() -> int:
    files = sorted({ROOT / mutation[0] for mutation in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    baseline = run(BASELINE_MODULE)
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-3000:])
        return 1
    print("baseline = GREEN")
    detected = fake = timed_out = survived = 0
    try:
        for index, (relative, old, new, selector) in enumerate(MUTATIONS, 1):
            path = ROOT / relative
            source = original[path].decode("utf-8")
            if source.count(old) != 1:
                fake += 1
                print(f"M-R{index} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run(selector)
            except subprocess.TimeoutExpired:
                timed_out += 1
                print(f"M-R{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-R{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-R{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1000:])
            finally:
                restore(path, original[path])
    finally:
        failures = restore_all(original)
    if failures:
        for item in failures:
            print(item)
        return 1
    print(f"mutation = {detected}/{len(MUTATIONS)} DETECTED")
    print(f"survived = {survived}")
    print(f"fake = {fake}")
    print(f"timeout = {timed_out}")
    same = {str(path): (path.read_bytes() == data) for path, data in original.items()}
    print("restore SHA256 = " + ("PASS" if all(same.values()) else "FAIL"))
    after = run(BASELINE_MODULE)
    print("baseline after restore = " + ("GREEN" if after.returncode == 0 else "RED"))
    return 0 if (survived == 0 and fake == 0 and timed_out == 0 and not failures
                 and after.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
