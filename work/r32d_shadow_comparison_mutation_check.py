#!/usr/bin/env python3
"""Reversible semantic mutations for R32-D Active/Challenger comparison evidence."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
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
        '            challenger_payload["risk_policy_identity_provenance"] = (\n'
        '                EvidenceProvenance.DECLARED.value if declared is not None\n'
        '                else declared_provenance)\n',
        '            challenger_payload["risk_policy_identity_provenance"] = declared_provenance\n',
        "test_shadow_comparison.ShadowComparisonTests.test_d8b_legacy_run_risk_identity_stays_declared",
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
        '        linked_rows = tuple(active.risk_decision_evidence or ())\n',
        '        linked_rows = tuple(active.risk_decision_evidence or ()) + (\n'
        '            {"risk_decision_id": 0, "order_id": active.order_id,\n'
        '             "decision": active.order_status, "reason": active.order_reason,\n'
        '             "decision_provenance": {\n'
        '                 "schema_version": "risk-decision-provenance-v1",\n'
        '                 "authority": RISK_AUTHORITY_LABEL,\n'
        '                 "decision_kind": "order_status"}},)\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_7_order_status_is_never_risk_evidence",
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
    # M-D17：风险证据改用 (account, code, side) 猜最近一条，而不是精确 order 关联。
    (
        "backend/shadow_comparison_service.py",
        '    rows = conn.execute(\n'
        '        "SELECT id,decision,reason,created_at,payload FROM paper_risk_decisions"\n'
        '        " WHERE order_id=? ORDER BY id",\n'
        '        (int(order_id),),\n'
        '    ).fetchall()\n',
        '    rows = conn.execute(\n'
        '        "SELECT id,decision,reason,created_at,payload FROM paper_risk_decisions"\n'
        '        " WHERE account_id=(SELECT account_id FROM paper_orders WHERE id=?)"\n'
        '        "   AND code=(SELECT code FROM paper_orders WHERE id=?)"\n'
        '        "   AND side=(SELECT side FROM paper_orders WHERE id=?)"\n'
        '        " ORDER BY id DESC LIMIT 1",\n'
        '        (int(order_id), int(order_id), int(order_id)),\n'
        '    ).fetchall()\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_5_6_legacy_and_unlinked_risk_decisions_are_never_borrowed",
    ),
    # M-D18：新 run 的 owner-issued risk policy identity 被丢弃（退回 legacy 分支）。
    (
        "backend/shadow_comparison.py",
        '        owner_projection = owner_section.get("projection")\n',
        '        owner_projection = None\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_1_both_legs_use_one_owner_risk_policy_contract",
    ),
    # M-D19：owner risk policy shape 校验被移除（任意 caller dict 都能当 owner 事实）。
    (
        "backend/shadow_runtime.py",
        '    if not SRT.is_risk_policy_projection(risk_policy):\n'
        '        raise ValueError("owner_risk_policy_projection_required")\n',
        '    if False:\n'
        '        raise ValueError("owner_risk_policy_projection_required")\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_8_superseded_risk_identity_input_is_deleted",
    ),
    # M-D20：execution authority 的 order-linked row 被重新允许进入 risk_rejection。
    (
        "backend/shadow_comparison.py",
        '        risk_rows = tuple(item for item in linked_rows\n'
        '                          if _decision_authority(item) == RISK_AUTHORITY_LABEL)\n',
        '        risk_rows = tuple(linked_rows)\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_r1_execution_blocked_row_is_not_risk_evidence",
    ),
    # M-D21：写入方不再需要声明 authority（order id 本身就当成归属证明）。
    (
        "backend/paper_trading.py",
        '    if order_id is not None and authority is None:\n'
        '        raise ValueError(\n'
        '            "order-linked risk decision requires an explicit authority")\n',
        '    if False:\n'
        '        raise ValueError(\n'
        '            "order-linked risk decision requires an explicit authority")\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_r10_authority_labels_have_one_owner",
    ),
    # M-D22：supplied risk_policy 与 exact strategy version owner projection 的相等校验被删除。
    (
        "backend/shadow_runtime.py",
        '    if dict(risk_policy) != SRT.risk_policy_projection_for_definition(definition):\n'
        '        raise ValueError("challenger_risk_policy_identity_mismatch")\n',
        '    if False:\n'
        '        raise ValueError("challenger_risk_policy_identity_mismatch")\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_r6_tampered_fingerprint_fails_closed",
    ),
    # M-D23：只比较 key 集合，canonical-shaped 的伪造 policy 因此被接受。
    (
        "backend/shadow_runtime.py",
        '    if dict(risk_policy) != SRT.risk_policy_projection_for_definition(definition):\n'
        '        raise ValueError("challenger_risk_policy_identity_mismatch")\n',
        '    if set(risk_policy) != set(SRT.risk_policy_projection_for_definition(definition)):\n'
        '        raise ValueError("challenger_risk_policy_identity_mismatch")\n',
        "test_shadow_comparison.ShadowComparisonTests.test_e1_r7_tampered_profile_fails_closed",
    ),
    # M-D24：Active BUY 复合结论已拿到 order_id 却丢弃 linkage。
    (
        "backend/paper_trading.py",
        '        _risk_log(conn, account["id"], code, "buy", decision_name, reason, risk,\n'
        '                  order_id=int(cursor.lastrowid), authority=admission_authority,\n'
        '                  decision_kind=decision_name)\n',
        '        _risk_log(conn, account["id"], code, "buy", decision_name, reason, risk,\n'
        '                  authority=admission_authority,\n'
        '                  decision_kind=decision_name)\n',
        "test_r32e1_active_buy_provenance.ActiveBuyDecisionProvenanceTests.test_r1_real_risk_authority_veto_is_exactly_linked",
    ),
    # M-D25：复合 ENTRY rejection 被错误标成 RISK。
    (
        "backend/paper_trading.py",
        '        else:\n'
        '            admission_authority = "ENTRY"\n',
        '        else:\n'
        '            admission_authority = "RISK"\n',
        "test_r32e1_active_buy_provenance.ActiveBuyDecisionProvenanceTests.test_r2_non_risk_rejection_is_never_labelled_risk",
    ),
    # M-D26：真实 Risk Authority 的 BUY evidence 被押成非 owner authority。
    (
        "backend/paper_trading.py",
        '                      order_id=int(cursor.lastrowid), authority="RISK",\n'
        '                      decision_kind="shared_risk_state_blocked")\n',
        '                      order_id=int(cursor.lastrowid), authority="ENTRY",\n'
        '                      decision_kind="shared_risk_state_blocked")\n',
        "test_r32e1_active_buy_provenance.ActiveBuyDecisionProvenanceTests.test_r1_real_risk_authority_veto_is_exactly_linked",
    ),
]

BASELINE_MODULES = ("test_shadow_comparison", "test_shadow_runtime",
                    "test_r32e1_active_buy_provenance")


def run(args: tuple[str, ...], timeout: int = 90):
    return subprocess.run(args, cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def restore(path: Path, data: bytes, attempts: int = 10) -> None:
    """Put the original bytes back, retrying transient write failures.

    A failed restore must never abort the remaining restores: leaving a mutated
    harness in the worktree would silently corrupt the next run. Writing can
    fail transiently on Windows while another process still holds the file.
    """
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
        except SystemExit as exc:  # keep restoring the others first
            failures.append(str(exc))
    return failures


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
                restore(path, original[path])
    finally:
        failures = restore_all(original)
    if failures:
        for item in failures:
            print(item)
        return 1
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
