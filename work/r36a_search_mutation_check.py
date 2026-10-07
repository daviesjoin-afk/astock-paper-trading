#!/usr/bin/env python3
"""Reversible R36-A semantic mutations（search control plane）。

Each mutation breaks exactly one **search-controller** boundary and runs the focused
regression that owns it. Source bytes are restored after every case and SHA256-checked
at the end, and the focused baseline is re-run after restore.

ID 前缀 **M-SC** 与旧的 ``M-SCOPE1`` / ``M-SCOPE2``（tradability position harness）是
不同的精确 ID，不构成冲突；与 ``M-B`` / ``M-G`` / ``M-X`` / ``M-AIG`` 也不重叠。

Design note: the anchors are semantic (they change a real boundary), not cosmetic. A
mutation that no test can detect would mean the contract is only documented, not
enforced. Only mutations that map onto a real production boundary are listed — the
count is not a goal.
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
    "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
    "test_s1e_batch_fingerprint_must_match_original_material_and_row",
    "test_r36a_experiment_search_controller.StateMachineTests."
    "test_s15b_completion_is_rejected_without_a_verifiable_binding",
    "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
    "test_s1_known_batch_is_accepted_and_unknown_or_malformed_is_rejected",
    "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
    "test_s1b_source_has_no_latest_or_recent_batch_lookup",
    "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
    "test_s1c_batch_payload_identity_must_match_the_lookup_key",
    "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
    "test_s1d_batch_payload_without_a_canonical_input_fingerprint_is_rejected",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s2_a_candidate_from_another_batch_is_rejected",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s2b_mixed_subset_is_rejected_not_silently_filtered",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s3_tampered_candidate_row_fails_closed",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s3b_proposal_json_alone_cannot_vouch_for_a_candidate",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s3c_a_candidate_row_missing_from_the_ledger_is_not_skipped",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s4_unknown_candidate_schema_is_rejected",
    "test_r36a_experiment_search_controller.CandidatePoolTests."
    "test_s4b_v1_and_v2_rows_still_self_verify",
    "test_r36a_experiment_search_controller.CandidateSubsetTests."
    "test_s5_subset_order_does_not_change_identity_or_jobs",
    "test_r36a_experiment_search_controller.CandidateSubsetTests."
    "test_s6_duplicate_candidate_ids_are_rejected_not_deduped",
    "test_r36a_experiment_search_controller.CandidateSubsetTests."
    "test_s7_budget_overflow_without_explicit_subset_is_rejected",
    "test_r36a_experiment_search_controller.CandidateSubsetTests."
    "test_s8_explicit_subset_within_budget_is_accepted",
    "test_r36a_experiment_search_controller.RunIdentityTests."
    "test_s9_same_spec_twice_same_input_fingerprint_different_run_id",
    "test_r36a_experiment_search_controller.RunIdentityTests."
    "test_s9b_search_run_id_is_opaque_not_derived_from_content",
    "test_r36a_experiment_search_controller.RunIdentityTests."
    "test_s10_creation_is_atomic",
    "test_r36a_experiment_search_controller.RunIdentityTests."
    "test_s11_job_identity_is_deterministic",
    "test_r36a_experiment_search_controller.RunIdentityTests."
    "test_s12_initial_state_is_queued_with_zero_attempts",
    "test_r36a_experiment_search_controller.StateMachineTests."
    "test_s13_queued_to_claimed_then_reclaim_is_rejected",
    "test_r36a_experiment_search_controller.StateMachineTests."
    "test_s13b_illegal_transitions_are_rejected",
    "test_r36a_experiment_search_controller.StateMachineTests."
    "test_s14_attempt_budget_is_enforced",
    "test_r36a_experiment_search_controller.StateMachineTests."
    "test_s15_completed_is_terminal",
    "test_r36a_experiment_search_controller.StateMachineTests."
    "test_s16_cancelled_is_terminal",
    "test_r36a_experiment_search_controller.EventOrderingTests."
    "test_s17_current_state_uses_event_seq_not_created_at_or_event_id",
    "test_r36a_experiment_search_controller.EventOrderingTests."
    "test_s17b_controller_never_orders_by_created_at",
    "test_r36a_experiment_search_controller.QueuePolicyTests."
    "test_s18_claim_order_is_canonical_and_insertion_order_independent",
    "test_r36a_experiment_search_controller.QueuePolicyTests."
    "test_s18b_queue_policy_version_is_part_of_the_identity",
    "test_r36a_experiment_search_controller.QueuePolicyTests."
    "test_s19_two_workers_never_double_claim",
    "test_r36a_experiment_search_controller.QueuePolicyTests."
    "test_s19c_a_second_claim_of_the_same_job_is_refused_by_the_transition_table",
    "test_r36a_experiment_search_controller.TerminalSemanticsTests."
    "test_s20_controller_stores_no_evaluation_metrics",
    "test_r36a_experiment_search_controller.TerminalSemanticsTests."
    "test_s20c_queue_state_has_no_business_meaning",
    "test_r36a_experiment_search_controller.TerminalSemanticsTests."
    "test_s21b_controller_does_not_execute_runners",
    "test_r36a_experiment_search_controller.TerminalSemanticsTests."
    "test_s22_no_ai_provider_dependency",
    "test_r36a_experiment_search_controller.TerminalSemanticsTests."
    "test_s22b_no_selection_ranking_or_priority_authority",
    "test_r36a_experiment_search_controller.SchemaTests."
    "test_append_only_triggers_reject_update_and_delete",
    "test_r36a_experiment_search_controller.SchemaTests."
    "test_foreign_keys_are_enforced",
    "test_r36a_experiment_search_controller.SchemaTests."
    "test_migration_is_idempotent_and_needs_no_backfill",
    "test_r36a_experiment_search_controller.SchemaTests."
    "test_normal_bootstrap_creates_the_search_tables",
)

CONTRACT = "backend/experiment_search_contract.py"
REPOSITORY = "backend/experiment_search_repository.py"
SERVICE = "backend/experiment_search_service.py"
MIGRATIONS = "backend/paper_schema_migrations.py"
CANDIDATE = "backend/strategy_candidate.py"
PAPER = "backend/paper_trading.py"

MUTATIONS = [
    # M-SC1 —— exact generation batch 退化成"最近一批"：一次 search 的输入集合会在
    # 无人察觉的情况下换掉，而 fingerprint 看不出来。
    {"id": "M-SC1",
     "semantic": "an exact generation batch falls back to the most recent batch",
     "edits": [(SERVICE,
                '    if batch is None:\n'
                '        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, requested)\n',
                '    if batch is None:\n'
                '        row = conn.execute(\n'
                '            "SELECT batch_json FROM strategy_candidate_generation_batches"\n'
                '            " ORDER BY created_at DESC LIMIT 1").fetchone()\n'
                '        if row is None:\n'
                '            raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, requested)\n'
                '        import json as _json\n'
                '        batch = _json.loads(str(row[0]))\n'
                '        requested = str(batch.get("batch_id") or requested)\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
         "test_s1_known_batch_is_accepted_and_unknown_or_malformed_is_rejected",
         "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
         "test_s1b_source_has_no_latest_or_recent_batch_lookup",
     ]},
    # M-SC2 —— 允许 batch 外的 candidate 混入：pool 不再受 exact batch 约束。
    {"id": "M-SC2",
     "semantic": "a candidate outside the exact generation batch is accepted",
     "edits": [(SERVICE,
                '    outside = [item for item in requested if item not in allowed]\n'
                '    if outside:\n'
                '        # batch 外的 candidate 混入：拒绝，绝不"忽略多余项继续"。\n'
                '        raise ExperimentSearchError(REASON_CANDIDATE_NOT_IN_BATCH, outside[0])\n',
                '    outside = [item for item in requested if item not in allowed]\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.CandidatePoolTests."
         "test_s2_a_candidate_from_another_batch_is_rejected",
         "test_r36a_experiment_search_controller.CandidatePoolTests."
         "test_s2b_mixed_subset_is_rejected_not_silently_filtered",
     ]},
    # M-SC3 —— 不再从 canonical ledger 自证 candidate：只相信 proposal 行。
    #
    # 真实边界有**两半**：候选行缺失时必须拒绝（不是 `continue` 跳过），以及拿到的行
    # 必须重算指纹自证。`get_candidate` 自身已经会自证，所以单删 service 的 verify 不会
    # 被发现 —— 上一版 M-SC3 因此 SURVIVED。这里改成同时打破两半，其中 `continue`
    # 那半正是"幽灵 candidate 进入队列"的真实缺口。
    {"id": "M-SC3",
     "semantic": "candidate ledger self-verification is bypassed",
     "edits": [(SERVICE,
                '        if candidate is None:\n'
                '            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE, candidate_id)\n'
                '        if not SC.verify_candidate_fingerprint(candidate):\n'
                '            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE, candidate_id)\n',
                '        if candidate is None:\n'
                '            continue\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.CandidatePoolTests."
         "test_s3_tampered_candidate_row_fails_closed",
         "test_r36a_experiment_search_controller.CandidatePoolTests."
         "test_s3b_proposal_json_alone_cannot_vouch_for_a_candidate",
         "test_r36a_experiment_search_controller.CandidatePoolTests."
         "test_s3c_a_candidate_row_missing_from_the_ledger_is_not_skipped",
     ]},
    # M-SC4 —— 未知 candidate schema 当 legacy 读：未来版本的行在旧语义下"自证通过"。
    {"id": "M-SC4",
     "semantic": "an unknown candidate schema version is read as legacy",
     "edits": [(CANDIDATE,
                '    if schema_version not in CANDIDATE_SCHEMA_VERSIONS:\n'
                '        # 缺失 / 未知 / 拼写漂移都拒绝：不猜、不回落 legacy。\n'
                '        raise CandidateValidationError(\n'
                '            f"unsupported_candidate_schema_version:{schema_version or \'<missing>\'}")\n',
                '    if False:\n'
                '        raise CandidateValidationError(\n'
                '            f"unsupported_candidate_schema_version:{schema_version or \'<missing>\'}")\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.CandidatePoolTests."
         "test_s4_unknown_candidate_schema_is_rejected",
         "test_r35a_strategy_candidate.CandidateIdentityTests."
         "test_c2c_identity_material_carries_no_evaluation_or_provenance_fact",
     ]},
    # M-SC5 —— budget 溢出时自动截断：控制器偷偷替用户决定"删掉哪些候选"。
    {"id": "M-SC5",
     "semantic": "a budget overflow is silently truncated instead of rejected",
     "edits": [(CONTRACT,
                '        if len(ids) > self.budget.max_candidates:\n'
                '            # 预算不是截断许可：超了就拒绝，绝不擅自取前 N 个。\n'
                '            raise SearchContractError(\n'
                '                "candidate_count_exceeds_budget",\n'
                '                f"{len(ids)} > {self.budget.max_candidates}")\n',
                '        if len(ids) > self.budget.max_candidates:\n'
                '            ids = ids[:self.budget.max_candidates]\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.CandidateSubsetTests."
         "test_s7_budget_overflow_without_explicit_subset_is_rejected",
     ]},
    # M-SC6 —— search_run_id 变成 content fingerprint：同一次请求重复执行会得到同一个
    # "事件"身份，两次独立调度无法区分。
    {"id": "M-SC6",
     "semantic": "the search run id becomes a content fingerprint",
     "edits": [(CONTRACT,
                '    import secrets\n'
                '    return secrets.token_hex(32)\n',
                '    return ExperimentSearchSpec.__new__(ExperimentSearchSpec) if False else (\n'
                '        "0" * 64)\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.RunIdentityTests."
         "test_s9_same_spec_twice_same_input_fingerprint_different_run_id",
         "test_r36a_experiment_search_controller.RunIdentityTests."
         "test_s9b_search_run_id_is_opaque_not_derived_from_content",
     ]},
    # M-SC7 —— 给 jobs 表加可变 status 列：append-only 证据 + 可变快照 = 两套 authority。
    {"id": "M-SC7",
     "semantic": "the job table gains a mutable status column",
     "edits": [(MIGRATIONS,
                '        stage TEXT NOT NULL,\n'
                '        job_contract_version TEXT NOT NULL,\n',
                '        stage TEXT NOT NULL,\n'
                '        status TEXT,\n'
                '        job_contract_version TEXT NOT NULL,\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.TerminalSemanticsTests."
         "test_s20_controller_stores_no_evaluation_metrics",
     ]},
    # M-SC8 —— current state 用 created_at 而不是 event_seq：两条同 timestamp 的事件
    # 顺序变得不可判定。
    {"id": "M-SC8",
     "semantic": "current state is derived from created_at instead of event_seq",
     "edits": [(REPOSITORY,
                '        " FROM experiment_search_job_events WHERE job_id=? ORDER BY event_seq ASC",\n',
                '        " FROM experiment_search_job_events WHERE job_id=?"\n'
                '        " ORDER BY created_at DESC, event_id DESC",\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.EventOrderingTests."
         "test_s17_current_state_uses_event_seq_not_created_at_or_event_id",
         "test_r36a_experiment_search_controller.EventOrderingTests."
         "test_s17b_controller_never_orders_by_created_at",
     ]},
    # M-SC9 —— retry 预算被绕过：可以无限重试。
    {"id": "M-SC9",
     "semantic": "the per-job attempt budget is bypassed",
     "edits": [(SERVICE,
                '        if used >= max_attempts:\n'
                '            # retry 预算耗尽：绝不无限重试。\n'
                '            raise ExperimentSearchError(REASON_ATTEMPT_BUDGET_EXHAUSTED,\n'
                '                                        f"{used}/{max_attempts}")\n',
                '        if False:\n'
                '            raise ExperimentSearchError(REASON_ATTEMPT_BUDGET_EXHAUSTED,\n'
                '                                        f"{used}/{max_attempts}")\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.StateMachineTests."
         "test_s14_attempt_budget_is_enforced",
     ]},
    # M-SC10 —— 转换表被放宽，终态可以再转换（completed → claimed 等）。
    {"id": "M-SC10",
     "semantic": "terminal job states are no longer terminal",
     "edits": [(CONTRACT,
                '    "completed": frozenset(),\n'
                '    "cancelled": frozenset(),\n',
                '    "completed": frozenset({"claimed", "cancelled", "failed"}),\n'
                '    "cancelled": frozenset({"claimed"}),\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.StateMachineTests."
         "test_s15_completed_is_terminal",
         "test_r36a_experiment_search_controller.StateMachineTests."
         "test_s16_cancelled_is_terminal",
     ]},
    # M-SC11 —— 正常 bootstrap 不建 search 表：v35 migration 只是升级路径，应用自己
    # open/create 的库缺少三张表，第一次 search 写入直接 `no such table`。
    # 这是 code review 抓到的真实缺陷（两条 init_db 路径都必须建表）。
    {"id": "M-SC11",
     "semantic": "normal bootstrap stops creating the search tables",
     "edits": [(PAPER,
                '                # R36-A（v35）：实验搜索控制面三张追加表（DDL 同样只在\n'
                '                # paper_schema_migrations）。**必须**在这里也建：v35 migration 只是\n'
                '                # 升级路径，正常 bootstrap 若缺少它，第一次 search 写入会直接\n'
                '                # `no such table`。\n'
                '                PSM.ensure_experiment_search(conn)\n',
                '                pass  # bootstrap no longer initialises search tables\n'),
               (PAPER,
                '        # R36-A v35：实验搜索控制面三张追加表。与上面同样必须在这里幂等建表，\n'
                '        # 否则正常 bootstrap 出来的库缺少 search 表，第一次写入即 `no such table`。\n'
                '        PSM.ensure_experiment_search(conn)\n',
                '        pass  # bootstrap no longer initialises search tables\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.SchemaTests."
         "test_normal_bootstrap_creates_the_search_tables",
     ]},
    # M-SC12 —— 不校验 batch payload 自述身份：请求 A 可以静默拿到 B 的候选集合，
    # 并把 B 的事实记为 A。这是 code review 抓到的第二个真实缺陷。
    {"id": "M-SC12",
     "semantic": "the batch payload identity is not checked against the lookup key",
     "edits": [(SERVICE,
                '    if str(batch.get("batch_id") or "") != requested:\n'
                '        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, "batch identity mismatch")\n'
                '    if not ESC.is_search_identity(str(batch.get("generation_input_fingerprint") or "")):\n'
                '        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND,\n'
                '                                    "batch payload lacks a canonical input fingerprint")\n',
                '    pass  # payload identity is trusted as-is\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
         "test_s1c_batch_payload_identity_must_match_the_lookup_key",
         "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
         "test_s1d_batch_payload_without_a_canonical_input_fingerprint_is_rejected",
     ]},
    {"id": "M-SC13",
     "semantic": "generation input fingerprint is not recomputed from original material",
     "edits": [("backend/strategy_candidate_repository.py",
                '        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != batch["generation_input_fingerprint"]:\n'
                '            raise ValueError("batch input fingerprint mismatch")\n',
                '        pass  # trust a well-shaped fingerprint\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.ExactGenerationBatchTests."
         "test_s1e_batch_fingerprint_must_match_original_material_and_row",
     ]},
    {"id": "M-SC14",
     "semantic": "direct repository writes can manufacture completed events",
     "edits": [(REPOSITORY,
                '    if event_kind == "completed":\n'
                '        raise ExperimentSearchRepositoryError("completion_evidence_binding_unavailable")\n',
                '    pass  # accept unverified completion\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.StateMachineTests."
         "test_s15b_completion_is_rejected_without_a_verifiable_binding",
     ]},
    {"id": "M-SC15",
     "semantic": "a second worker can claim a job already claimed by the first worker",
     "edits": [(CONTRACT,
                '    "claimed": frozenset({"failed"}),\n',
                '    "claimed": frozenset({"failed", "claimed"}),\n')],
     "detectors": [
         "test_r36a_experiment_search_controller.QueuePolicyTests."
         "test_s19_two_workers_never_double_claim",
     ]},
]


def run(*selectors):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *selectors, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600, env=env)


def main() -> int:
    paths = sorted({ROOT / edit[0] for case in MUTATIONS for edit in case["edits"]})
    original = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run(*BASELINE)
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-4000:])
        return 1

    detected = survived = fake = timeout = 0
    modified_paths: set[Path] = set()
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
                        print((result.stdout + result.stderr)[-1500:])
            for path in tuple(modified_paths):
                path.write_bytes(original[path])
                modified_paths.discard(path)
    finally:
        for path in tuple(modified_paths):
            path.write_bytes(original[path])
            modified_paths.discard(path)

    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in paths)
    final = run(*BASELINE)
    print(f"M-SC detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and final.returncode == 0 and restored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
