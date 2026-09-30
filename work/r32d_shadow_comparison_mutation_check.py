#!/usr/bin/env python3
"""Reversible semantic mutations for R32-D Active/Challenger comparison evidence."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

MUTATIONS = [
    # M-D1：共享环境相等不再是硬前提，环境不一致也照算 delta。
    (
        "backend/shadow_comparison.py",
        '    if equality != "EQUAL":\n',
        '    if False:\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d2_each_shared_environment_dimension_mismatch_blocks_comparison",
    ),
    # M-D2：缺证据的一侧被当成"被拒"，而不是 MISSING。
    (
        "backend/shadow_comparison.py",
        '        elif challenger_state == unavailable:\n'
        '            reasons.append("challenger_admission_evidence_unusable")\n'
        '        return _leg_dimension(active=active_payload, challenger=challenger_payload,\n'
        '                              active_state=active_state,\n'
        '                              challenger_state=challenger_state, delta=None,\n'
        '                              reasons=reasons)\n',
        '        elif challenger_state == unavailable:\n'
        '            reasons.append("challenger_admission_evidence_unusable")\n'
        '        return _leg_dimension(active=active_payload, challenger=challenger_payload,\n'
        '                              active_state=active_state,\n'
        '                              challenger_state=challenger_state,\n'
        '                              delta={"challenger_admission_decision": False},\n'
        '                              reasons=reasons)\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d4_absence_is_missing_not_false_reject_or_zero",
    ),
    # M-D3：coverage 分母/分子失真，ratio 恒为 1。
    (
        "backend/shadow_comparison.py",
        '        coverage_ratio=round(available / total, 6),\n',
        '        coverage_ratio=1.0,\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d5_coverage_ratio_keeps_expected_as_the_denominator",
    ),
    # M-D4：显式 Active evidence 缺失时回退到"最新一条订单"。
    (
        "backend/shadow_comparison_service.py",
        '        if not values:\n'
        '            raise SC.ShadowComparisonError(\n'
        '                f"active_order_evidence_unavailable:{int(order_id)}")\n',
        '        if not values:\n'
        '            cursor = conn.execute("SELECT " + ",".join(_ORDER_COLUMNS)\n'
        '                                  + " FROM paper_orders ORDER BY id DESC LIMIT 1")\n'
        '            values = _row_mapping(cursor, cursor.fetchone())\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d7b_a_missing_named_active_evidence_row_fails_closed",
    ),
    # M-D5：report fingerprint 不再覆盖 Active evidence identity 与消费投影。
    (
        "backend/shadow_comparison.py",
        '        "active_evidence": active_evidence.projection(),\n',
        '        "active_evidence": {"source_schema_version": ACTIVE_EVIDENCE_SCHEMA_VERSION},\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d1b_report_fingerprint_binds_the_exact_evidence_identities",
    ),
    # M-D6：report fingerprint 不再覆盖 ShadowRun identity。
    (
        "backend/shadow_comparison.py",
        '        "shadow_run_id": shadow_run.run_id,\n'
        '        "shadow_run_fingerprint": shadow_run.run_fingerprint,\n',
        '        "shadow_run_id": "0" * 64,\n'
        '        "shadow_run_fingerprint": "0" * 64,\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d1b_report_fingerprint_binds_the_exact_evidence_identities",
    ),
    # M-D7：caller-declared risk identity 被提升成 owner 事实。
    (
        "backend/shadow_comparison.py",
        '        "challenger.risk_policy_identity": EvidenceProvenance.DECLARED.value,\n',
        '        "challenger.risk_policy_identity": EvidenceProvenance.OWNER_ISSUED.value,\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d8_declared_risk_identity_is_never_promoted_to_verified",
    ),
    # M-D8：缺 exact 估值时不再 fail closed，而是用能找到的价格继续算。
    (
        "backend/shadow_comparison.py",
        '    if missing:\n',
        '    if False:\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d9_missing_exact_valuation_stays_unavailable",
    ),
    # M-D9：允许 ComparisonSpec 不带 exact Active evidence fingerprint。
    (
        "backend/shadow_comparison.py",
        '        if not _SHA256.fullmatch(str(self.active_evidence_id or "")):\n'
        '            raise ValueError("exact Active evidence fingerprint is required")\n',
        '        if False:\n'
        '            raise ValueError("exact Active evidence fingerprint is required")\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d13_mutated_active_row_fails_closed_against_the_pinned_fingerprint",
    ),
    # M-D10：production service 跳过 Active fingerprint 相等校验。
    (
        "backend/shadow_comparison_service.py",
        '    if active_evidence.source_fingerprint != spec.active_evidence_id:\n'
        '        raise SC.ShadowComparisonError("active_evidence_fingerprint_mismatch")\n',
        '    if False:\n'
        '        raise SC.ShadowComparisonError("active_evidence_fingerprint_mismatch")\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d13_mutated_active_row_fails_closed_against_the_pinned_fingerprint",
    ),
    # M-D11：candidate 存在即被当成 execution evidence 存在。
    (
        "backend/shadow_comparison.py",
        '        if execution_mapping is not None:\n'
        '            challenger_state = present\n',
        '        if isinstance(challenger, Mapping):\n'
        '            challenger_state = present\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d14_candidate_without_execution_evidence_is_not_execution_evidence",
    ),
    # M-D12：缺失的 fill quantity 被转成 0。
    (
        "backend/shadow_comparison.py",
        '            "execution_fill_quantity": ((execution_mapping or {}).get("fill_quantity")\n'
        '                                        if execution_mapping is not None else None),\n',
        '            "execution_fill_quantity": ((execution_mapping or {}).get("fill_quantity")\n'
        '                                        or 0),\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d18_absent_execution_never_becomes_zero_or_false",
    ),
    # M-D13：order lifecycle status 被升级成 Active risk evidence。
    (
        "backend/shadow_comparison.py",
        '        active_payload = {\n'
        '            "risk_rejection_evidence": None,\n'
        '            "risk_rejection_availability": EvidenceProvenance.UNAVAILABLE.value,\n',
        '        active_payload = {\n'
        '            "risk_rejection_evidence": {"order_status": active.order_status},\n'
        '            "risk_rejection_availability": EvidenceProvenance.OWNER_ISSUED.value,\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d17_order_status_is_not_risk_authority_evidence",
    ),
    # M-D14：admission_score 退回旧键（owner projection 已不再有裸 score 键）。
    (
        "backend/shadow_comparison.py",
        '            "admission_score": (admission or {}).get("admission_score"),\n',
        '            "admission_score": (admission or {}).get("score"),\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d19_admission_score_comes_from_its_canonical_owner_key",
    ),
    # M-D15：capture 的 fingerprint 不再由加载到的 exact 行内容决定
    #        （等价于让占位 fingerprint 也能通过）。
    (
        "backend/shadow_comparison.py",
        '        "orders": [order.projection() for order in orders],\n',
        '        "orders": [],\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d13_mutated_active_row_fails_closed_against_the_pinned_fingerprint",
    ),
    # M-D16：capture owner 重新接受 ComparisonSpec 参数（占位循环依赖回流）。
    (
        "backend/shadow_comparison_service.py",
        'def capture_active_comparison_evidence(\n'
        '        conn: sqlite3.Connection, *, active_order_ids: tuple[int, ...],\n'
        ') -> SC.ActiveComparisonEvidence:\n',
        'def capture_active_comparison_evidence(\n'
        '        conn: sqlite3.Connection, *, active_order_ids: tuple[int, ...],\n'
        '        comparison_spec: SC.ComparisonSpec | None = None,\n'
        ') -> SC.ActiveComparisonEvidence:\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d20_active_capture_depends_only_on_explicit_order_ids",
    ),
]

BASELINE_MODULES = ("test_shadow_comparison", "test_shadow_runtime")


def run(args: tuple[str, ...], timeout: int = 90):
    return subprocess.run(args, cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    files = sorted({ROOT / mutation[0] for mutation in MUTATIONS})
    original = {path: path.read_bytes() for path in files}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run((sys.executable, "-m", "unittest", *BASELINE_MODULES, "-q"))
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
                print(f"M-D{index} FAKE (anchor count={source.count(old)})")
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="")
            try:
                result = run((sys.executable, "-m", "unittest", selector, "-q"))
            except subprocess.TimeoutExpired:
                timeout_count += 1
                print(f"M-D{index} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"M-D{index} DETECTED")
                else:
                    survived += 1
                    print(f"M-D{index} SURVIVED")
                    print((result.stdout + result.stderr)[-1200:])
            finally:
                path.write_bytes(original[path])
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in files)
    final = run((sys.executable, "-m", "unittest", *BASELINE_MODULES, "-q"))
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
