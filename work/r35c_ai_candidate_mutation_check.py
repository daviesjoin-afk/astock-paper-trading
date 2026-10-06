#!/usr/bin/env python3
"""Reversible R35-C semantic mutations.

Each mutation breaks exactly one **AI candidate generation** boundary and runs the
focused regression that owns it. Source bytes are restored after every case and
SHA256-checked at the end, and the focused baseline is re-run after restore.

Design note: the anchors are semantic (they change a real boundary), not cosmetic.
A mutation that no test can detect would mean the contract is only documented, not
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
    "test_r35c_ai_candidate_generation.ExactResearchRunTests."
    "test_c1_exact_run_id_is_read_and_unknown_id_fails_closed",
    "test_r35c_ai_candidate_generation.ExactResearchRunTests."
    "test_c1b_non_numeric_or_negative_run_id_fails_closed",
    "test_r35c_ai_candidate_generation.ExactResearchRunTests."
    "test_c1b2_canonical_run_id_and_ints_are_accepted",
    "test_r35c_ai_candidate_generation.ExactResearchRunTests."
    "test_c1b3_strict_identity_is_checked_before_reading_the_ledger",
    "test_r35c_ai_candidate_generation.ExactResearchRunTests."
    "test_c1c_no_latest_fallback_in_the_source",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_disabled_slot_is_blocked_by_the_service_itself",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_unready_slots_are_blocked_with_canonical_reasons",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_readiness_reuses_the_canonical_authority_not_a_second_copy",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_readiness_precedes_the_provider_call_but_follows_the_audit_gates",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_unknown_slot_is_rejected_by_the_service_itself",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_canonical_slot_is_normalised_before_the_provider_call",
    "test_r35c_ai_candidate_generation.ProviderReadinessTests."
    "test_canonicalisation_reuses_the_authority_not_a_second_copy",
    "test_r35c_ai_candidate_generation.CrossModelDedupTests."
    "test_c12c_provider_slot_is_recorded_and_bound_into_the_input_fingerprint",
    "test_r35c_ai_candidate_generation.CrossModelDedupTests."
    "test_c12d_provider_identity_is_absent_when_the_slot_is_unknown",
    "test_r35c_ai_candidate_generation.ResearchGateTests."
    "test_c2_unsupported_research_is_rejected_before_the_provider",
    "test_r35c_ai_candidate_generation.ResearchGateTests."
    "test_c3_confidence_has_no_authority",
    "test_r35c_ai_candidate_generation.AsofPinningTests."
    "test_c4_asof_mismatch_is_rejected_before_the_provider",
    "test_r35c_ai_candidate_generation.AsofPinningTests."
    "test_c4c_omitting_asof_adopts_the_research_run_day",
    "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
    "test_c5_ai_cannot_choose_the_parent",
    "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
    "test_c6_ai_cannot_control_risk_universe_or_regime",
    "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
    "test_c7_authority_and_scoring_fields_are_rejected",
    "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
    "test_c7b_unknown_fields_are_rejected_not_ignored",
    "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
    "test_c7c_authority_fields_are_distinguished_from_unknown_fields",
    "test_r35c_ai_candidate_generation.ProposalShapeTests."
    "test_c8_invalid_dsl_is_rejected",
    "test_r35c_ai_candidate_generation.ProposalShapeTests."
    "test_c9_invalid_parameter_is_rejected_by_the_existing_schema",
    "test_r35c_ai_candidate_generation.CandidateCapTests.test_c10_cap_boundary",
    "test_r35c_ai_candidate_generation.CandidateCapTests."
    "test_c10b_over_cap_is_rejected_not_truncated",
    "test_r35c_ai_candidate_generation.CandidateCapTests."
    "test_c10c_requested_cap_above_the_ai_cap_is_rejected",
    "test_r35c_ai_candidate_generation.NoOpProposalTests."
    "test_c11_no_op_proposal_is_rejected",
    "test_r35c_ai_candidate_generation.CrossModelDedupTests."
    "test_c12_same_semantics_across_models_dedup_with_separate_provenance",
    "test_r35c_ai_candidate_generation.CrossModelDedupTests."
    "test_c12b_each_proposal_retains_its_own_model",
    "test_r35c_ai_candidate_generation.ResearchProvenanceTests."
    "test_c13_provenance_binds_the_exact_run_and_record_hash",
    "test_r35c_ai_candidate_generation.ResearchProvenanceTests."
    "test_c13b_switching_the_run_changes_the_generation_input_fingerprint",
    "test_r35c_ai_candidate_generation.TransactionBoundaryTests."
    "test_c14_network_call_holds_no_write_transaction",
    "test_r35c_ai_candidate_generation.TransactionBoundaryTests."
    "test_c15_provider_failure_leaves_zero_writes",
    "test_r35c_ai_candidate_generation.TransactionBoundaryTests."
    "test_c15b_provider_failure_does_not_recount_old_candidates",
    "test_r35c_ai_candidate_generation.TransactionBoundaryTests."
    "test_c16_write_failure_rolls_back_the_whole_batch",
    "test_r35c_ai_candidate_generation.BatchAuditabilityTests."
    "test_c17_persisted_search_space_self_verifies",
    "test_r35c_ai_candidate_generation.BatchAuditabilityTests."
    "test_c17b_tampered_material_does_not_verify",
    "test_r35c_ai_candidate_generation.AuthorityBoundaryTests."
    "test_c18_generator_path_has_no_authority_dependency",
    "test_r35c_ai_candidate_generation.AuthorityBoundaryTests."
    "test_c18b_no_second_ai_provider_transport",
    "test_r35c_ai_candidate_generation.AuthorityBoundaryTests."
    "test_c18c_no_ai_specific_database_table",
    "test_r35c_ai_candidate_generation.EvidenceCountTests."
    "test_ai_cannot_declare_evidence_count",
    "test_r35c_ai_candidate_generation.EvidenceCountTests."
    "test_evidence_count_comes_from_the_canonical_hypothesis",
    "test_r35c_ai_candidate_generation.ProviderProtocolTests."
    "test_model_identity_records_no_fabricated_model",
    "test_r35c_ai_candidate_generation.ProviderProtocolTests."
    "test_research_model_and_proposal_model_stay_distinct",
    "test_r35c_ai_candidate_generation.ProviderProtocolTests."
    "test_malformed_parameter_variants_fail_closed_not_typeerror",
    "test_r35c_ai_candidate_generation.ProviderProtocolTests."
    "test_malformed_provider_shape_is_a_provider_error_not_a_crash",
    "test_r35c_ai_candidate_generation.ProviderProtocolTests."
    "test_corrupt_research_record_is_translated_at_the_service_boundary",
)

PROPOSAL = "backend/strategy_ai_proposal.py"
PROVIDER = "backend/strategy_ai_provider.py"
SERVICE = "backend/strategy_ai_candidate_service.py"
API = "backend/api_strategies.py"

MUTATIONS = [
    # M-AIG1 —— exact research_run_id 退化成"最近一次研究"：坏输入不再被拒，
    # 请求的 run 变成隐式输入。
    {"id": "M-AIG1",
     "semantic": "an exact research run id falls back to the recent run",
     "edits": [(SERVICE,
                '    if run is None:\n'
                '        # 绝不回退到 recent_runs(...)[0]：那是把"最新一次研究"变成隐式输入。\n'
                '        raise AICandidateGenerationError(REASON_RESEARCH_NOT_FOUND, str(run_key))\n',
                '    if run is None:\n'
                '        recent = ARR.recent_runs(conn, limit=1)\n'
                '        run = recent[0] if recent else None\n'
                '    if run is None:\n'
                '        raise AICandidateGenerationError(REASON_RESEARCH_NOT_FOUND, str(run_key))\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ExactResearchRunTests."
         "test_c1_exact_run_id_is_read_and_unknown_id_fails_closed",
         "test_r35c_ai_candidate_generation.ExactResearchRunTests."
         "test_c1c_no_latest_fallback_in_the_source",
     ]},
    # M-AIG2 —— 移除 supported gate：unsupported 研究也会触发一次付费 provider 调用。
    {"id": "M-AIG2",
     "semantic": "the research support gate is removed",
     "edits": [(SERVICE,
                '    if str(run.get("status")) != ARC.HYPOTHESIS_SUPPORTED:\n'
                '        raise AICandidateGenerationError(REASON_RESEARCH_UNSUPPORTED,\n'
                '                                         str(run.get("status")))\n',
                '    if False:\n'
                '        raise AICandidateGenerationError(REASON_RESEARCH_UNSUPPORTED,\n'
                '                                         str(run.get("status")))\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ResearchGateTests."
         "test_c2_unsupported_research_is_rejected_before_the_provider",
     ]},
    # M-AIG3 —— confidence 被偷偷升级成资格阈值：AI 自评变成 authority。
    {"id": "M-AIG3",
     "semantic": "confidence is promoted into an eligibility threshold",
     "edits": [(SERVICE,
                '    if str(run.get("status")) != ARC.HYPOTHESIS_SUPPORTED:\n',
                '    if float(run.get("confidence") or 0.0) < 0.7:\n'
                '        raise AICandidateGenerationError(REASON_RESEARCH_UNSUPPORTED, "low")\n'
                '    if str(run.get("status")) != ARC.HYPOTHESIS_SUPPORTED:\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ResearchGateTests."
         "test_c3_confidence_has_no_authority",
         "test_r35c_ai_candidate_generation.ResearchGateTests."
         "test_c3b_no_confidence_threshold_in_the_source",
     ]},
    # M-AIG4 —— as-of 不再与 research run 钉死：历史思想可以自动流到新业务日。
    {"id": "M-AIG4",
     "semantic": "the candidate asof stops being pinned to the research run",
     "edits": [(SERVICE,
                '    elif str(asof) != run_asof:\n'
                '        raise AICandidateGenerationError(REASON_ASOF_MISMATCH,'
                ' f"{run_asof} != {asof}")\n',
                '    elif False:\n'
                '        raise AICandidateGenerationError(REASON_ASOF_MISMATCH,'
                ' f"{run_asof} != {asof}")\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.AsofPinningTests."
         "test_c4_asof_mismatch_is_rejected_before_the_provider",
     ]},
    # M-AIG5 —— provider 被允许声明 risk / universe / regime / asof：
    # AI 拿到了它不该有的风险与标的范围权力。
    {"id": "M-AIG5",
     "semantic": "the provider may declare risk, universe, regime or asof",
     "edits": [(PROPOSAL,
                '    "asof",\n'
                '    "universe_spec", "intended_market_regime",\n'
                '    "constraints",\n',
                '    # mutated: risk / universe / regime / asof no longer forbidden\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
         "test_c6_ai_cannot_control_risk_universe_or_regime",
     ]},
    # M-AIG5b —— 单独放开 parent 三件套（与 M-AIG5 分开：这正是"AI 选 parent"边界）。
    {"id": "M-AIG5b",
     "semantic": "the provider may choose the parent strategy",
     "edits": [(PROPOSAL,
                '    "strategy_id", "strategy_version", "strategy_checksum",\n',
                '    # mutated: parent pin no longer forbidden\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
         "test_c5_ai_cannot_choose_the_parent",
     ]},
    # M-AIG6 —— 未知 provider 字段被静默忽略：越权字段可以悄悄搭车。
    {"id": "M-AIG6",
     "semantic": "unknown provider fields are silently ignored",
     "edits": [(PROPOSAL,
                '    unknown = sorted(set(payload) - _ALLOWED_TOP_LEVEL)\n'
                '    if unknown:\n'
                '        raise AIProposalError(Reason.UNKNOWN_PROVIDER_FIELD,'
                ' f"unknown fields {unknown}")\n',
                '    unknown = sorted(set(payload) - _ALLOWED_TOP_LEVEL)\n'
                '    if False:\n'
                '        raise AIProposalError(Reason.UNKNOWN_PROVIDER_FIELD,'
                ' f"unknown fields {unknown}")\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
         "test_c7b_unknown_fields_are_rejected_not_ignored",
         "test_r35c_ai_candidate_generation.NoProviderAuthorityTests."
         "test_c7_authority_and_scoring_fields_are_rejected",
     ]},
    # M-AIG7 —— 超过 AI cap 时截断而不是拒绝：AI 可以看起来产出受限批次，
    # 实际候选集被悄悄削掉。
    {"id": "M-AIG7",
     "semantic": "an over-cap proposal is truncated instead of rejected",
     "edits": [(SERVICE,
                '    if max_candidates > SAIP.MAX_AI_CANDIDATES_PER_REQUEST:\n'
                '        # 只能收紧，不能放宽。\n'
                '        raise AICandidateGenerationError(REASON_AI_CANDIDATE_CAP,'
                ' str(max_candidates))\n',
                '    max_candidates = min(int(max_candidates),'
                ' SAIP.MAX_AI_CANDIDATES_PER_REQUEST)\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.CandidateCapTests."
         "test_c10c_requested_cap_above_the_ai_cap_is_rejected",
     ]},
    # M-AIG8 —— generation provenance 不再绑定 exact research record_hash：
    # 候选不再能追回是哪一条 canonical research 记录。
    {"id": "M-AIG8",
     "semantic": "the generation provenance stops binding the exact research record hash",
     "edits": [(SERVICE,
                '        "source_fingerprint": str(run.get("record_hash") or ""),\n',
                '        "source_fingerprint": "",\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ResearchProvenanceTests."
         "test_c13_provenance_binds_the_exact_run_and_record_hash",
     ]},
    # M-AIG9 —— no-op proposal 被允许：AI 可以"生成"父策略自己，看起来做了事。
    {"id": "M-AIG9",
     "semantic": "a no-op proposal is accepted",
     "edits": [(PROPOSAL,
                '    if not axes:\n'
                '        raise AIProposalError(Reason.NO_VARIATION,\n'
                '                              "proposal declares no parameter or slot variant")\n',
                '    if not axes and False:\n'
                '        raise AIProposalError(Reason.NO_VARIATION,\n'
                '                              "proposal declares no parameter or slot variant")\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.NoOpProposalTests."
         "test_c11_no_op_proposal_is_rejected",
     ]},
    # M-AIG10 —— provider 的 model identity 被替换成 research run 的 model：
    # "谁把 hypothesis 变成 candidate proposal"与"最初谁提出 hypothesis"混为一谈。
    {"id": "M-AIG10",
     "semantic": "the proposal model identity is replaced by the research run model",
     "edits": [(SERVICE,
                '    model = str(provider_config.get("model") or "").strip()\n',
                '    model = str(provider_config.get("model") or "research-model").strip()\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ProviderProtocolTests."
         "test_model_identity_records_no_fabricated_model",
     ]},
    # M-AIG11 —— 跳过 provider 槽位 readiness：被禁用的槽位照样发起真实付费调用。
    # 强化版：gate 现在长在 **orchestration service** 上（route 只映射 reason），
    # 因此这条 mutation 直接打破 service 的强制 gate —— 无论调用方是 HTTP、CLI、
    # R36 还是 scheduler 都会重新出现"禁用形同不存在"。
    {"id": "M-AIG11",
     "semantic": "the orchestration service stops enforcing provider readiness",
     "edits": [(SERVICE,
                '    readiness = AIReview.slot_readiness(canonical)\n'
                '    if not readiness["ready"]:\n'
                '        raise AICandidateGenerationError(REASON_PROVIDER_NOT_READY,\n'
                '                                         str(readiness["reason"]))\n',
                '    readiness = AIReview.slot_readiness(canonical)\n'
                '    if False:\n'
                '        raise AICandidateGenerationError(REASON_PROVIDER_NOT_READY,\n'
                '                                         str(readiness["reason"]))\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ProviderReadinessTests."
         "test_disabled_slot_is_blocked_by_the_service_itself",
         "test_r35c_ai_candidate_generation.ProviderReadinessTests."
         "test_unready_slots_are_blocked_with_canonical_reasons",
     ]},
    # M-AIG12 —— 未知槽位不再映射成客户端错误：裸 ValueError 穿透成 5xx。
    {"id": "M-AIG12",
     "semantic": "an unknown provider slot is not mapped to a client error",
     "edits": [(API,
                '        except ValueError:\n'
                '            raise HTTPException(status_code=400,\n'
                '                                detail="unknown_provider_slot") from None\n',
                '        except ValueError:\n'
                '            slot = AIReview.AI_SLOTS[0]\n')],
     "detectors": [
         "test_r35c_ai_candidate_http.AIEndpointTests."
         "test_unknown_provider_slot_is_a_client_error",
     ]},
    # M-AIG13 —— 先在畸形形状上度量：TypeError / AttributeError 穿透 proposal 契约，
    # 变成 5xx 而不是稳定的 fail closed。
    {"id": "M-AIG13",
     "semantic": "a malformed proposal shape raises an unclassified exception",
     "edits": [(PROPOSAL,
                '    raw_parameters = payload.get("parameter_variants")\n'
                '    if raw_parameters is None:\n'
                '        parameters: Mapping[str, Any] = {}\n'
                '    elif isinstance(raw_parameters, Mapping):\n'
                '        parameters = raw_parameters\n'
                '    else:\n'
                '        raise AIProposalError(Reason.INVALID_PROPOSAL_SHAPE,\n'
                '                              "parameter_variants must be an object")\n',
                '    parameters: Mapping[str, Any] = payload.get("parameter_variants") or {}\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ProviderProtocolTests."
         "test_malformed_parameter_variants_fail_closed_not_typeerror",
         "test_r35c_ai_candidate_generation.ProviderProtocolTests."
         "test_malformed_provider_shape_is_a_provider_error_not_a_crash",
     ]},
    # M-AIG14 —— 损坏 research 行不再被翻译：裸 ValueError 穿透成 5xx。
    {"id": "M-AIG14",
     "semantic": "a corrupt research record is not translated at the boundary",
     "edits": [(SERVICE,
                '    except ARR.ResearchPersistenceError as exc:\n',
                '    except ARR.ResearchPersistenceError:\n'
                '        pass\n'
                '    except ARR.ResearchPersistenceError as exc:\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ProviderProtocolTests."
         "test_corrupt_research_record_is_translated_at_the_service_boundary",
     ]},
    # M-AIG15 —— run id 解析退回"先 strip 再 isdigit"：形状非法的输入被悄悄解析成合法
    # id，于是"形状拒绝"与"查无此行"混为一谈（回归会变成假绿）。
    {"id": "M-AIG15",
     "semantic": "a non-canonical run id is silently normalised",
     "edits": [(SERVICE,
                '    if isinstance(research_run_id, str) and _CANONICAL_RUN_ID.fullmatch(research_run_id):\n'
                '        return int(research_run_id)\n',
                '    if isinstance(research_run_id, str) and research_run_id.strip().isdigit():\n'
                '        return int(research_run_id.strip())\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ExactResearchRunTests."
         "test_c1b_non_numeric_or_negative_run_id_fails_closed",
         "test_r35c_ai_candidate_generation.ExactResearchRunTests."
         "test_c1b3_strict_identity_is_checked_before_reading_the_ledger",
     ]},
    # M-AIG16 —— provider slot provenance 的两半同时失守（reviewer 建议强化，不新增 ID）：
    #   * 非 canonical 输入（"AI1" / " mimo "）被原样持久化 ⇒ 同一槽位在事件身份里裂成
    #     多种字符串；未知槽位（"evil-provider"）被接受并真的发起付费调用；
    #   * provenance 干脆不再记录 provider 键。
    {"id": "M-AIG16",
     "semantic": "the provider slot is neither validated nor canonicalised",
     "edits": [(SERVICE,
                '    try:\n'
                '        slot = AIReview.resolve_slot(config.get("slot"))\n'
                '    except ValueError:\n'
                '        # 未知 / 缺失 / 非字符串槽位：fail closed，绝不原样透传。\n'
                '        raise AICandidateGenerationError(REASON_PROVIDER_SLOT_UNKNOWN,\n'
                '                                         str(config.get("slot") or "")) from None\n',
                '    slot = str(config.get("slot") or "").strip()\n'),
               (SERVICE,
                '    provider = str(provider_config.get("slot") or "").strip()\n'
                '    if provider:\n'
                '        identity["provider"] = provider\n',
                '    provider = str(provider_config.get("slot") or "").strip()\n')],
     "detectors": [
         "test_r35c_ai_candidate_generation.ProviderReadinessTests."
         "test_unknown_slot_is_rejected_by_the_service_itself",
         "test_r35c_ai_candidate_generation.ProviderReadinessTests."
         "test_canonical_slot_is_normalised_before_the_provider_call",
         "test_r35c_ai_candidate_generation.ProviderReadinessTests."
         "test_canonicalisation_reuses_the_authority_not_a_second_copy",
         "test_r35c_ai_candidate_generation.CrossModelDedupTests."
         "test_c12c_provider_slot_is_recorded_and_bound_into_the_input_fingerprint",
     ]},
]


def run(*selectors):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *selectors, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=env)


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
    print(f"M-AIG detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and final.returncode == 0 and restored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
