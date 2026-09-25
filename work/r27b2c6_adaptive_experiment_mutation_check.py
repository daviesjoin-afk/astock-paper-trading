# -*- coding: utf-8 -*-
"""R27-B2C-6 —— adaptive / experiment owner fact 与 strategy adapter 的 **mutation matrix**。

覆盖 ``M-EXP-01`` ~ ``M-EXP-15``，逐条对应本轮四条要钉死的不变量：

```text
selection candidate 只有一个 production writer（owner 自己）
candidate lifecycle status  ≠ owner verification
run_date                    ≠ revision availability
dataset cutoff              ≠ evaluation availability
evaluation verdict          ≠ owner verification
malformed / 不可证           fail closed（绝不 {} / latest / created_at 兜底）
adapter 只接受已批准的精确类型，且不接受 caller 命名的 identity / as_of
registry 与真实 factory 双向一致
```

────────────── 与 brief 的偏差（记录，不静默） ──────────────

brief 建议的 ``M-EXP-*`` 覆盖到 "learning pit_status verified → owner verified"
（本文件 :data:`M-EXP-10`）与 ``promotable → verification``（:data:`M-EXP-09`）。两条都
**保留**，但落点按真实接缝选择，而不是硬塞：

* ``M-EXP-09`` 不伪造一个 ``promotable`` 字段（本轮**刻意**没有该字段，见 B14），而是把
  ``evaluation_admitted`` 塞进 ``OwnerVerification.attributes`` —— 这正是"把 verdict 当核验
  维度"的可执行形式，且由 ``EXP-14`` 的属性集精确断言打红。
* ``M-EXP-10`` 直接把 ``learning_dataset`` 的 PIT 词表值放进 strategy 归口表，
  ``EXP-18`` 的"两个词表不相交"断言立刻红。

brief 里"没有 safe adapter 时不要假装 EXP-19~28 完成"的条件**不适用**：owner contract 与
adapter 都已真实落地（三个 typed 投影 + 唯一 adapter + registry 双向登记），因此
``EXP-19`` ~ ``EXP-28`` 全部适用，对应 mutation 也全部有真实锚点。

────────────── PYTHONPYCACHEPREFIX 必须唯一 ──────────────

每次 ``run_test`` 都用**唯一**的 ``PYTHONPYCACHEPREFIX``。共享 pycache 会让一次变异后的
编译缓存泄漏到下一次运行，于是"测试被杀死"可能来自上一次的 mutant 而不是这一次 —— 那是
假证据。:func:`self_test_sequence` 就证明这一点。

**必须串行运行**：本 harness 会改写 production source 再还原，并行会互相覆盖。

用法：

```bash
python work/r27b2c6_adaptive_experiment_mutation_check.py
python work/r27b2c6_adaptive_experiment_mutation_check.py --only M-EXP-01
python work/r27b2c6_adaptive_experiment_mutation_check.py --only=M-EXP-01,M-EXP-05
```
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

#: 本 matrix 会改写的 production 文件。
ADVISOR_FILE = "backend/deepseek_advisor.py"
SELECTION_FILE = "backend/adaptive_selection.py"
EVALUATION_FILE = "backend/learning_evaluation.py"
ADAPTER_FILE = "backend/ai_research_strategy_adapter.py"
CONTRACT_FILE = "backend/ai_research_contract.py"

MUTATED_FILES = (ADVISOR_FILE, SELECTION_FILE, EVALUATION_FILE, ADAPTER_FILE, CONTRACT_FILE)

#: 收尾复验基准：矩阵跑完后用它做一次"真的没被动过"的独立检查。
DS_FILE = ADAPTER_FILE

SUITE_OWNER = "test_adaptive_experiment_evidence_ownership"
SUITE_ADAPTER = "test_ai_research_strategy_adapter"


def _owner(name: str) -> str:
    return f"{SUITE_OWNER}.{name}"


def _adapter(name: str) -> str:
    return f"{SUITE_ADAPTER}.{name}"


T_EXP_01 = _owner("SelectionWriterConvergenceTests."
                  "test_EXP_01_selection_candidate_production_writer_is_the_owner_only")
T_EXP_02 = _owner("SelectionWriterConvergenceTests."
                  "test_EXP_02_deepseek_advisor_never_writes_the_selection_ledger")
T_EXP_03 = _owner("SelectionWriterConvergenceTests."
                  "test_EXP_03_ai_proposal_still_cannot_auto_apply")
T_EXP_04 = _owner("CandidateLifecycleIsNotVerificationTests."
                  "test_EXP_04_candidate_lifecycle_status_never_changes_verification")
T_EXP_04B = _owner("CandidateLifecycleIsNotVerificationTests."
                   "test_EXP_04b_unknown_vocabulary_is_unproven_not_verified")
T_EXP_05 = _owner("CandidateIdentityTests."
                  "test_EXP_05_risk_candidate_identity_comes_from_the_owner_revision")
T_EXP_06 = _owner("CandidateIdentityTests."
                  "test_EXP_06_selection_candidate_identity_comes_from_the_owner_revision")
T_EXP_07 = _owner("CandidateIdentityTests."
                  "test_EXP_07_a_revision_change_moves_the_identity_or_creates_a_new_revision")
T_EXP_08 = _owner("CandidatePITTests."
                  "test_EXP_08_updated_at_after_as_of_never_returns_the_current_row")
T_EXP_09 = _owner("CandidatePITTests."
                  "test_EXP_09_run_date_cannot_substitute_for_revision_availability")
T_EXP_09B = _owner("CandidatePITTests."
                   "test_EXP_09b_owner_timezone_normalization_decides_the_availability_day")
T_EXP_10 = _owner("CandidatePITTests."
                  "test_EXP_10_malformed_candidate_content_fails_closed")
T_EXP_11 = _owner("CandidatePITTests."
                  "test_EXP_11_candidate_readers_never_fall_back_to_latest_or_current")
T_EXP_11B = _owner("CandidatePITTests."
                   "test_EXP_11b_missing_normalized_columns_fail_closed")
T_EXP_12 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_12_evaluation_cutoff_is_not_evaluation_availability")
T_EXP_13 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_13_evaluation_produced_later_cannot_be_backfilled_to_the_cutoff_day")
T_EXP_13C = _owner("ExperimentEvaluationFactTests."
                   "test_EXP_13c_unprovable_dataset_freeze_bound_is_unproven_not_recorded")
T_EXP_14 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_14_evaluation_admitted_true_is_not_strategy_verified")
T_EXP_15 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_15_evaluation_admitted_false_is_still_an_owner_verified_fact")
T_EXP_16 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_16_promotable_is_not_the_definition_of_owner_verification")
T_EXP_17 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_17_promotable_false_is_not_source_unusable")
T_EXP_18 = _owner("ExperimentEvaluationFactTests."
                  "test_EXP_18_learning_pit_status_cannot_map_to_owner_verification")
T_EXP_19 = _adapter("ExactTypeBoundaryTests."
                    "test_EXP_19_adapter_accepts_only_exact_approved_owner_types")
T_EXP_19B = _adapter("ExactTypeBoundaryTests."
                     "test_EXP_19b_source_type_reuses_the_existing_family_seam")
T_EXP_20 = _adapter("ExactTypeBoundaryTests."
                    "test_EXP_20_adapter_cannot_take_a_caller_supplied_source_id")
T_EXP_21 = _adapter("ExactTypeBoundaryTests."
                    "test_EXP_21_adapter_cannot_take_a_caller_supplied_as_of")
T_EXP_22 = _adapter("FingerprintTests.test_EXP_22_content_fingerprint_is_deterministic")
T_EXP_23 = _adapter("FingerprintTests."
                    "test_EXP_23_factual_or_revision_change_moves_the_fingerprint")
T_EXP_24 = _adapter("AdapterPurityTests."
                    "test_EXP_24_adapter_has_no_db_network_or_clock_access")
T_EXP_25 = _adapter("StrategyRegistryTests."
                    "test_EXP_25_strategy_research_registry_and_real_factory_agree")
T_EXP_26 = _adapter("StrategyRegistryTests."
                    "test_EXP_26_strategy_adapter_has_zero_production_callers")
T_EXP_27 = _adapter("DeferredLegacyRuntimeTests."
                    "test_EXP_27_candidate_challenge_runtime_is_unchanged_and_deferred")
T_EXP_28 = _adapter("DeferredLegacyRuntimeTests."
                    "test_EXP_28_overfit_watch_runtime_is_unchanged_and_deferred")

#: 点名的永久回归目标：不在本 matrix 的 mutation 里，但必须与 mutation target 一起先证明
#: 在**干净源码**上 GREEN。否则"这些目标也验过"只是句话。
BASELINE_ONLY_TARGETS = (
    T_EXP_05,
    T_EXP_06,
    T_EXP_07,
    T_EXP_12,
    T_EXP_16,
    T_EXP_17,
    T_EXP_19B,
    T_EXP_22,
    T_EXP_24,
    T_EXP_26,
    T_EXP_27,
    T_EXP_28,
    "test_ai_research_evidence_ownership_guard",
    "test_ai_research_contract",
    #: 新接缝必须登记进网络闭集（RG-03 / RG-04 / RG-05）—— 这三条 guard 随接缝集合增长而
    #: 自动扩展，因此把整份 suite 作为 baseline 目标，让"漏登记"在任何 mutation 之前就暴露。
    "test_ai_provider_transport",
)


# --- mutation anchors（逐字节，必须恰好命中一次）--------------------------------
#: ``deepseek_advisor`` 现在通过 owner 的窄接口持久化影子候选。
DS_OWNER_CALL = (
    "                selection.record_shadow_proposal(\n"
    "                    conn,\n"
    "                    run_date=str(\n"
    '                        profile.get("profile_date") or dt.datetime.now(TZ).date().isoformat()\n'
    "                    )[:10],\n"
    "                    account_id=account_id,\n"
    "                    regime=regime,\n"
    "                    model_id=model_id,\n"
    "                    baseline_params=baseline,\n"
    "                    candidate_params=candidate,\n"
    '                    evidence={"source": "DeepSeek", "confidence": item["confidence"],\n'
    '                              "evidence_hash": evidence_hash},\n'
    '                    reason=item["reason"],\n'
    "                    now=now,\n"
    "                )\n"
)

#: selection owner 的核验闭集判据尾部。
SEL_VERIFICATION_TAIL = (
    "    if lifecycle_status not in SELECTION_LIFECYCLE_STATUSES:\n"
    "        return SELECTION_FACT_OWNER_UNPROVEN\n"
    "    if tier not in SELECTION_TIERS:\n"
    "        return SELECTION_FACT_OWNER_UNPROVEN\n"
    "    return SELECTION_FACT_RECORDED\n"
)

#: selection 读侧：可用日由 ``updated_at`` 派生。
SEL_AVAILABILITY = (
    "    available_day = _owner_availability_day(\n"
    '        revision, what="selection candidate updated_at",\n'
    "    )\n"
)

#: selection 读侧的 fail-closed 历史门禁。
SEL_PIT_GATE = (
    "    if available_day > day:\n"
    "        return None\n"
    "    return _selection_projection_from_row(item, revision=revision, available_day=available_day)\n"
)

#: 严格 JSON parse（typed path 不得宽松）。
SEL_STRICT_PARSE = (
    "    try:\n"
    "        parsed = json.loads(value) if isinstance(value, str) else value\n"
    "    except (TypeError, ValueError) as exc:\n"
    '        raise SelectionFactContractError(f"{what} is not parsable JSON: {exc}") from exc\n'
    "    if not isinstance(parsed, dict):\n"
)

#: 显式 ``as_of``（没有默认值）。
SEL_AS_OF = '    day = _business_day(as_of, what="selection candidate fact as_of")\n'

#: 指纹里的 revision identity 字段（必须唯一命中 ``_fingerprint`` 那一段）。
SEL_FINGERPRINT_REVISION = (
    '            "reason": self.reason,\n'
    '            "revision_at": self.revision_at,\n'
    '            "availability_day": self.availability_day,\n'
    '            "created_at": self.created_at,\n'
    '            "fact_verification_status": self.fact_verification_status,\n'
    "        }\n"
    "        encoded = json.dumps(\n"
)

#: evaluation 投影：可用性只来自结果产生瞬间。
EXP_AVAILABILITY = (
    "        result_available_at=available.isoformat(timespec=\"seconds\"),\n"
    "        availability_day=available_day,\n"
    "        fact_verification_status=status,\n"
)

#: evaluation 的核验状态（今天：等价于"冻结边界是否可证"）。
EXP_STATUS = (
    "    if cutoff is None:\n"
    "        status = EXPERIMENT_FACT_OWNER_UNPROVEN\n"
    "    else:\n"
    "        status = EXPERIMENT_FACT_RECORDED\n"
)

#: adapter 的核验 attributes（只放事实性审计维度）。
ADAPTER_ATTRIBUTES = (
    "        attributes={\n"
    '            "record_kind": str(projection.record_kind),\n'
    '            "contract_version": str(projection.version),\n'
    '            "fact_fingerprint": str(projection.content_fingerprint),\n'
    "        },\n"
)

#: adapter 归口表表头。
ADAPTER_OUTCOME_TABLE = (
    "_STRATEGY_OUTCOME_BY_STATUS = {\n"
    "    AR.RISK_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,\n"
)

#: 精确类型判定的收尾（拒绝一切未批准类型）。
ADAPTER_EXACT_KIND = (
    "        return LE.EXPERIMENT_EVALUATION_RECORD_KIND\n"
    "    return None\n"
)

#: 唯一公开 factory 的签名。
ADAPTER_SIGNATURE = (
    "def evidence_ref_from_strategy_projection(projection: Any) -> ARC.ResearchEvidenceRef:\n"
)

#: research contract 的 owner adapter registry。
CONTRACT_REGISTRY = (
    "    EVIDENCE_SOURCE_NEWS,\n"
    "    EVIDENCE_SOURCE_STRATEGY_RESEARCH,\n"
    "})\n"
)


MUTATIONS: list[dict] = [
    {
        "id": "M-EXP-01",
        # 恢复"proposal producer 自己写候选 ledger" —— 这正是 B2C-6 收敛掉的那条 split。
        "file": ADVISOR_FILE,
        "old": DS_OWNER_CALL,
        "new": (
            "                conn.execute(\n"
            '                    """INSERT INTO adaptive_selection_candidates(\n'
            "                       run_date,account_id,regime,model_id,baseline_params,"
            "candidate_params,evidence,status,tier,reason,created_at,updated_at)\n"
            '                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",  # MUTANT\n'
            "                    (str(profile.get(\"profile_date\") or "
            "dt.datetime.now(TZ).date().isoformat())[:10],\n"
            "                     account_id, regime, model_id,\n"
            "                     json.dumps(baseline, ensure_ascii=False), "
            "json.dumps(candidate, ensure_ascii=False),\n"
            '                     json.dumps({"source": "DeepSeek"}, ensure_ascii=False),\n'
            '                     "shadow_proposal", "ai_realtime", item["reason"], now, now),\n'
            "                )\n"
        ),
        "test": T_EXP_02,
        "desc": "deepseek_advisor 恢复对 selection ledger 的直接 INSERT（双 writer 复发）",
    },
    {
        "id": "M-EXP-02",
        # 把 lifecycle status 耦合进核验判据：只有 applied / eligible_auto_adjust 算"核验通过"。
        "file": SELECTION_FILE,
        "old": SEL_VERIFICATION_TAIL,
        "new": (
            '    if lifecycle_status not in {"applied", "eligible_auto_adjust"}:  # MUTANT\n'
            "        return SELECTION_FACT_OWNER_UNPROVEN\n"
            "    if tier not in SELECTION_TIERS:\n"
            "        return SELECTION_FACT_OWNER_UNPROVEN\n"
            "    return SELECTION_FACT_RECORDED\n"
        ),
        "test": T_EXP_04,
        "desc": "candidate lifecycle status 被当作 owner verification（applied ⇒ verified）",
    },
    {
        "id": "M-EXP-03",
        # run_date 代替 revision availability —— 典型 look-ahead。
        "file": SELECTION_FILE,
        "old": SEL_AVAILABILITY,
        "new": (
            "    available_day = _business_day(  # MUTANT —— 用 run_date 当可用性\n"
            '        item.get("run_date"), what="selection candidate run_date",\n'
            "    )\n"
        ),
        "test": T_EXP_09,
        "desc": "candidate as_of 改用 run_date 而不是 revision availability",
    },
    {
        "id": "M-EXP-04",
        # 历史 read 仍然返回当前行：旧 revision 被覆盖后，倒填更早的 as_of。
        "file": SELECTION_FILE,
        "old": SEL_PIT_GATE,
        "new": (
            "    if available_day > day and False:  # MUTANT —— 历史 read 返回当前行\n"
            "        return None\n"
            "    return _selection_projection_from_row("
            "item, revision=revision, available_day=available_day)\n"
        ),
        "test": T_EXP_08,
        "desc": "updated_at > as_of 时仍返回当前行（历史倒填）",
    },
    {
        "id": "M-EXP-05",
        # 坏 JSON 静默变成空对象 —— typed evidence path 上最危险的宽松。
        "file": SELECTION_FILE,
        "old": SEL_STRICT_PARSE,
        "new": (
            "    parsed = _loads(value, {})  # MUTANT —— 坏 JSON 变成空对象\n"
            "    if not isinstance(parsed, dict):\n"
        ),
        "test": T_EXP_10,
        "desc": "malformed candidate JSON 回落到 {} 而不是 fail closed",
    },
    {
        "id": "M-EXP-06",
        # 隐式 latest/current 回落：as_of 缺失时用今天。
        "file": SELECTION_FILE,
        "old": SEL_AS_OF,
        "new": (
            "    day = _business_day(as_of or dt.date.today().isoformat(),"
            ' what="selection candidate fact as_of")  # MUTANT\n'
        ),
        "test": T_EXP_11,
        "desc": "candidate reader 隐式回落到 today/latest",
    },
    {
        "id": "M-EXP-07",
        # dataset cutoff 当成 evaluation availability —— 9/21 跑出来的评估倒填进 9/20。
        "file": EVALUATION_FILE,
        "old": EXP_AVAILABILITY,
        "new": (
            "        result_available_at=available.isoformat(timespec=\"seconds\"),\n"
            "        availability_day=_experiment_day(  # MUTANT —— 用 dataset cutoff\n"
            '            cutoff, what="experiment fact dataset_cutoff"),\n'
            "        fact_verification_status=status,\n"
        ),
        "test": T_EXP_13,
        "desc": "evaluation as_of 使用 dataset cutoff 而不是结果产生瞬间",
    },
    {
        "id": "M-EXP-08",
        # evaluation_admitted 变成核验结论：不满意的评估被降级成 unverified 事实。
        "file": EVALUATION_FILE,
        "old": EXP_STATUS,
        "new": (
            "    if cutoff is None:\n"
            "        status = EXPERIMENT_FACT_OWNER_UNPROVEN\n"
            "    else:\n"
            "        status = (  # MUTANT —— 把 evaluation_admitted 当核验结论\n"
            "            EXPERIMENT_FACT_RECORDED if not blockers_raw\n"
            "            else EXPERIMENT_FACT_OWNER_UNPROVEN\n"
            "        )\n"
        ),
        "test": T_EXP_15,
        "desc": "evaluation_admitted 被读成 owner verification",
    },
    {
        "id": "M-EXP-09",
        # 把评估 verdict 塞进核验 attributes —— 核验维度里出现"结论"。
        "file": ADAPTER_FILE,
        "old": ADAPTER_ATTRIBUTES,
        "new": (
            "        attributes={\n"
            "            # MUTANT —— 把评估 verdict 当成核验维度\n"
            '            "evaluation_admitted": getattr(projection, "evaluation_admitted", None),\n'
            '            "record_kind": str(projection.record_kind),\n'
            '            "contract_version": str(projection.version),\n'
            '            "fact_fingerprint": str(projection.content_fingerprint),\n'
            "        },\n"
        ),
        "test": T_EXP_14,
        "desc": "verdict（admitted）被写进 OwnerVerification.attributes",
    },
    {
        "id": "M-EXP-10",
        # learning dataset 的 PIT 可用性词被当成 owner 核验词。
        "file": ADAPTER_FILE,
        "old": ADAPTER_OUTCOME_TABLE,
        "new": (
            "_STRATEGY_OUTCOME_BY_STATUS = {\n"
            '    "verified": ARC.OWNER_OUTCOME_VERIFIED,  # MUTANT —— PIT 词表借用\n'
            "    AR.RISK_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,\n"
        ),
        "test": T_EXP_18,
        "desc": "learning pit_status=verified 被映射成 owner verified",
    },
    {
        "id": "M-EXP-11",
        # duck typing：只要对象"长得像"投影就放行。
        "file": ADAPTER_FILE,
        "old": ADAPTER_EXACT_KIND,
        "new": (
            "        return LE.EXPERIMENT_EVALUATION_RECORD_KIND\n"
            '    return getattr(projection, "record_kind", None)  # MUTANT —— duck typing\n'
        ),
        "test": T_EXP_19,
        "desc": "adapter 接受 dict / duck-typed / 子类（放弃精确类型判定）",
    },
    {
        "id": "M-EXP-12",
        # caller 可以命名 identity —— 于是同一份事实能被改名成多条。
        "file": ADAPTER_FILE,
        "old": ADAPTER_SIGNATURE,
        "new": (
            "def evidence_ref_from_strategy_projection(  # MUTANT —— caller 可传 source_id\n"
            "    projection: Any, source_id: Any = None,\n"
            ") -> ARC.ResearchEvidenceRef:\n"
        ),
        "test": T_EXP_20,
        "desc": "adapter 接受 caller 传入的 source_id",
    },
    {
        "id": "M-EXP-13",
        # caller 可以声明可用性 —— 于是 PIT 声明脱离 owner 证明。
        "file": ADAPTER_FILE,
        "old": ADAPTER_SIGNATURE,
        "new": (
            "def evidence_ref_from_strategy_projection(  # MUTANT —— caller 可传 as_of\n"
            "    projection: Any, as_of: Any = None,\n"
            ") -> ARC.ResearchEvidenceRef:\n"
        ),
        "test": T_EXP_21,
        "desc": "adapter 接受 caller 传入的 as_of",
    },
    {
        "id": "M-EXP-14",
        # 指纹忽略 revision identity —— 内容变了 identity 不变，冲突检测失效。
        "file": SELECTION_FILE,
        "old": SEL_FINGERPRINT_REVISION,
        "new": (
            '            "reason": self.reason,\n'
            '            "revision_at": "",  # MUTANT —— 忽略 revision identity\n'
            '            "availability_day": self.availability_day,\n'
            '            "created_at": self.created_at,\n'
            '            "fact_verification_status": self.fact_verification_status,\n'
            "        }\n"
            "        encoded = json.dumps(\n"
        ),
        "test": T_EXP_23,
        "desc": "内容指纹忽略 revision identity",
    },
    {
        "id": "M-EXP-15",
        # registry 少登记一个 owner —— "谁能签发证据"不再受边界管辖。
        "file": CONTRACT_FILE,
        "old": CONTRACT_REGISTRY,
        "new": (
            "    EVIDENCE_SOURCE_NEWS,\n"
            "})  # MUTANT —— registry 少登记 strategy_research\n"
        ),
        "test": T_EXP_25,
        "desc": "SUPPORTED_OWNER_ADAPTERS 与真实 factory 漂移",
    },
]


VERDICT_CAUGHT = "CAUGHT"
VERDICT_SURVIVED = "SURVIVED"
VERDICT_FAKE = "FAKE"
VERDICT_TIMEOUT = "TIMEOUT"

#: baseline 阶段的两种失败。语义不同（没跑完 vs 在干净源码上本来就红），但都让 matrix FAIL。
BASELINE_RED = "BASELINE-RED"
BASELINE_TIMEOUT = "BASELINE-TIMEOUT"

#: 落盘的变异体必须带这个记号，否则"跑了一次没改动的测试"会被记成 CAUGHT。
MUTANT_MARKER = "MUTANT"


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _require(condition: bool, message: str) -> None:
    """本 harness 的 runtime evidence 断言 —— 显式失败，绝不用 ``assert``。

    ``python -O`` 会把 ``assert`` 整条剥掉，于是"证据链守卫在优化解释器下仍然存在"会变成
    一句无法验证的话。:func:`self_test_optimization` 就是这条性质的非空性证明。
    """
    if not condition:
        raise RuntimeError(message)


def _adapt_eol(text: str, original: bytes) -> bytes:
    """按原文件的行尾风格写回 —— 还原必须 byte-identical。"""
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


#: 每次运行使用**唯一**的 pycache 前缀：共享缓存会让上一次 mutant 的编译产物泄漏进来。
PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c6_adaptive_experiment_pycache_")
_SEQ = [0]

#: 接线级错误的指纹。命中即 FAKE —— 测试是因"代码根本没接好"而失败，不是业务断言。
BROKEN_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError"
    r"|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError"
    r"|_FailedTest"
    r"|TypeError: .*takes .* positional argument"
    r"|is not defined|local variable .* referenced before assignment)",
    re.MULTILINE,
)


def _next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def run_test(target: str, seq: int | None = None) -> subprocess.CompletedProcess:
    if seq is None:
        seq = _next_seq()
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{seq:03d}")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900, env=env,
    )


class _ShortCircuit(RuntimeError):
    """Raised by the self-test's subprocess stub."""


def self_test_sequence() -> None:
    seen = [_next_seq() for _ in range(5)]
    _require(len(set(seen)) == len(seen), f"sequence not unique: {seen}")
    _require(seen == sorted(seen), f"sequence not increasing: {seen}")
    dirs: list[str] = []
    original = subprocess.run
    try:
        def _capture(args, **kwargs):
            dirs.append(kwargs["env"]["PYTHONPYCACHEPREFIX"])
            raise _ShortCircuit
        subprocess.run = _capture  # type: ignore[assignment]
        for _ in range(3):
            try:
                run_test("unittest")
            except _ShortCircuit:
                pass
    finally:
        subprocess.run = original  # type: ignore[assignment]
    _require(len(dirs) == 3, f"expected 3 invocations, got {dirs}")
    _require(len(set(dirs)) == 3, f"invocations share a cache dir: {dirs}")


#: ``-O`` 探针：在优化解释器里复算 harness 的硬守卫，输出一行 JSON 报告。
_OPTIMIZATION_PROBE = '''
"""在普通 / ``-O`` 解释器下复算 harness 的硬守卫。"""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("_harness_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ANCHOR = "    return a + b\\n"


def outcome(call):
    try:
        call()
    except RuntimeError:
        return "RuntimeError"
    except AssertionError:
        return "AssertionError"
    return "NO-ERROR"


def apply_anchor(text):
    return module._apply(
        text, {"id": "PROBE", "file": "probe.py", "old": ANCHOR, "new": ""}
    )


print(json.dumps({
    "optimized": not __debug__,
    "implementation": sys.implementation.name,
    "results": [
        outcome(lambda: apply_anchor("def add(a, b):\\n    return a * b\\n")),
        outcome(lambda: apply_anchor("def add(a, b):\\n" + ANCHOR + ANCHOR)),
        outcome(lambda: apply_anchor("def add(a, b):\\n" + ANCHOR)),
        outcome(lambda: module._require(False, "probe: hard guard must survive -O")),
    ],
}))
'''


def _cli_probe(argv: list[str], timeout: int = 300) -> tuple[int, str]:
    """真实跑一次 CLI —— 只用于**参数解析阶段就退出**的用例，不触碰 production source。"""
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _assert_cli_rejected(argv: list[str]) -> None:
    """真实 CLI 上，一个非法 argv 必须 exit 2 + 受控 ERROR，且不进入任何执行阶段。

    ``selected:`` 只在参数与选择都通过之后才打印，因此它的缺席直接证明这次调用没有走到
    selection / baseline / mutation —— 也就不会改写 production source。
    """
    code, blob = _cli_probe(argv)
    _require(code == 2, f"{argv}: expected exit 2, got {code}: {blob[:200]}")
    _require("Traceback" not in blob, f"{argv}: must not raise a bare traceback: {blob[:200]}")
    _require("ERROR:" in blob, f"{argv}: expected a controlled ERROR: {blob[:200]}")
    _require("selected:" not in blob, f"{argv}: must not reach the mutation phase")


def self_test_semantics() -> None:
    """在临时目录里自证分类语义（stub 掉真实 runner，不触碰任何 production source）。

    覆盖：anchor 唯一性、BASELINE-RED / BASELINE-TIMEOUT 且不进入 mutation、CAUGHT、
    SURVIVED、FAKE（四类接线错误）、TIMEOUT、restore sha256 硬失败、byte-identical 还原、
    mutant 记号残留检测、``--only`` 选择器与 argv 白名单的全部参数边界。
    """
    root = tempfile.mkdtemp(prefix="r27b2c6_mutation_semantics_")
    rel = "semantics_target.py"
    path = os.path.join(root, rel)
    original = "def add(a, b):\n    return a + b\n"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(original)

    def result(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)

    def source_bytes() -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    mutation = {
        "id": "SELF-1", "file": rel,
        "old": "    return a + b\n", "new": "    return a - b  # MUTANT\n",
        "test": "test_semantics.Fake.test_add", "desc": "self-test semantic mutant",
    }
    extra_target = "test_semantics.Fake.test_extra"

    # 0) anchor 唯一性是证据链的硬不变量：0 次命中与多次命中都必须硬失败，
    #    绝不允许落到 replace(..., 1) 上（那会让"测试被杀"归因到一个没发生的改写）。
    for label, text in (
        ("count=0", "def add(a, b):\n    return a * b\n"),
        ("count=2", "def add(a, b):\n    return a + b\n    return a + b\n"),
    ):
        try:
            _apply(text, {"id": "SELF-ANCHOR", "file": rel,
                          "old": mutation["old"], "new": ""})
        except RuntimeError as exc:
            _require("anchor must be unique" in str(exc) and label in str(exc), exc)
        else:
            raise RuntimeError(f"{label}: non-unique anchor did not hard-fail")

    # 1) baseline RED → 整体失败，且 production source 一个字节都不被触碰。
    seen: list[str] = []

    def red_baseline(target: str, seq: int | None = None):
        seen.append(target)
        return result(1, "", "AssertionError: expected 2 got 3")

    _require(run_baselines([mutation], runner=red_baseline) == 1, "baseline RED must fail")
    _require(seen == [mutation["test"]], f"baseline must run exactly the deduped target: {seen}")
    _require(source_bytes() == original.encode("utf-8"),
             "baseline phase must not touch the source")

    # 2) baseline TIMEOUT → 同样整体失败、mutation 阶段不启动。
    seen.clear()

    def timeout_baseline(target: str, seq: int | None = None):
        seen.append(target)
        raise subprocess.TimeoutExpired(cmd=target, timeout=900)

    _require(run_baselines([mutation], runner=timeout_baseline) == 1,
             "baseline TIMEOUT must fail the matrix")
    _require(seen == [mutation["test"]],
             f"baseline TIMEOUT must stop after the first target: {seen}")
    _require(source_bytes() == original.encode("utf-8"),
             "baseline TIMEOUT must not touch the source")

    # 2b) baseline-only 的永久回归目标必须一起跑、且与 mutation target 去重。
    seen.clear()

    def green_baseline(target: str, seq: int | None = None):
        seen.append(target)
        return result(0)

    _require(run_baselines([mutation], runner=green_baseline,
                           extra=(mutation["test"], extra_target)) == 0,
             "baseline with extras must pass")
    _require(seen == [mutation["test"], extra_target],
             f"baseline extras must be appended and deduped: {seen}")

    # 3/4/5) baseline GREEN 之后的分类：CAUGHT / SURVIVED / FAKE。
    def runner_for(code: int, out: str = "", err: str = ""):
        def _run(target: str, seq: int | None = None):
            return result(code, out, err)
        return _run

    _require(run_mutation(mutation, root=root, runner=runner_for(1)) == VERDICT_CAUGHT,
             "returncode 1 with a business assertion failure must be CAUGHT")
    _require(source_bytes() == original.encode("utf-8"), "bytes must be restored exactly")
    _require(run_mutation(mutation, root=root, runner=runner_for(0)) == VERDICT_SURVIVED,
             "returncode 0 must be SURVIVED")
    _require(source_bytes() == original.encode("utf-8"), "bytes must be restored exactly")
    for err in ("SyntaxError: invalid syntax", "ImportError: no module named x",
                "NameError: name 'x' is not defined", "_FailedTest: collection failure"):
        verdict = run_mutation(mutation, root=root, runner=runner_for(1, err=err))
        _require(verdict == VERDICT_FAKE, f"{err} must be FAKE, got {verdict}")

    # 6) 超时是独立分类：不能算 CAUGHT，也不能算 FAKE，且必须仍然完整还原源码。
    def timeout_runner(target: str, seq: int | None = None):
        raise subprocess.TimeoutExpired(cmd=target, timeout=900)

    _require(run_mutation(mutation, root=root, runner=timeout_runner) == VERDICT_TIMEOUT,
             "TimeoutExpired must classify as TIMEOUT, not CAUGHT/FAKE")
    _require(source_bytes() == original.encode("utf-8"),
             "TIMEOUT must still restore the source byte-identically")
    _require(MUTANT_MARKER not in source_bytes().decode("utf-8"),
             "TIMEOUT must not leave the mutant on disk")

    # 6b) mutant 记号残留必须被 hard fail（"还原了"与"还原成什么"是两件事）。
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(original + "# MUTANT\n")
    try:
        assert_no_leftover(path, mutation["id"])
    except RuntimeError as exc:
        _require("leftover mutant" in str(exc), exc)
    else:
        raise RuntimeError("leftover mutant did not hard-fail")
    with open(path, "wb") as handle:
        handle.write(original.encode("utf-8"))

    # 7) restore 不一致 → 硬失败（人为给一个错误的启动快照 sha）。
    try:
        _restore_and_verify(path, original.encode("utf-8"), "0" * 64, mutation["id"])
    except RuntimeError as exc:
        _require("restore sha256 mismatch" in str(exc), exc)
    else:
        raise RuntimeError("restore mismatch did not hard-fail")

    # 8) 非空性：BROKEN_RE 必须真的能区分接线错误与业务断言失败。
    _require(_is_fake_kill(result(1, err="SyntaxError: invalid syntax")),
             "BROKEN_RE failed to flag a wiring error")
    _require(not _is_fake_kill(result(1, err="AssertionError: 2 != 3")),
             "BROKEN_RE must not flag a business assertion failure")

    # 9) --only 的选择语义（helper 层）：只有三态，绝不能把未知拼写解释成"跑全量 matrix"。
    _require(_parse_only([]) == (None, None), "no selector must mean the full matrix")
    for argv, ids in (
        (["--only", "M-EXP-01"], {"M-EXP-01"}),
        (["--only=M-EXP-01"], {"M-EXP-01"}),
        (["--only", "M-EXP-01,M-EXP-02"], {"M-EXP-01", "M-EXP-02"}),
        (["--only=M-EXP-01,M-EXP-02"], {"M-EXP-01", "M-EXP-02"}),
    ):
        _require(_parse_only(argv) == (ids, None), f"{argv} must select {ids}")
    #    未知 id 的解析本身是成功的 —— 由 main 的 "no mutation selected" 统一处理。
    _require(_parse_only(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--only", ""],
        ["--only", ","],
        ["--only=,"],
        ["--onlyy=M-EXP-01"],
        ["--only", "--only"],
        ["--only", "M-EXP-01", "--only", "M-EXP-02"],
    ):
        only, err = _parse_only(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 9b) argv 白名单（main 真正走的入口）：不认识的 token 不是"没有 selector"。
    _require(_parse_argv([]) == (None, None), "empty argv must mean the full matrix")
    for argv, ids in (
        (["--only", "M-EXP-01"], {"M-EXP-01"}),
        (["--only=M-EXP-01"], {"M-EXP-01"}),
    ):
        _require(_parse_argv(argv) == (ids, None), f"{argv} must select {ids}")
    _require(_parse_argv(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--onlyy=M-EXP-01"],
        ["--onl", "M-EXP-01"],
        ["--dry-run"],
        ["foo"],
        ["--only", "M-EXP-01", "foo"],
        ["--only", "M-EXP-01", "--only", "M-EXP-02"],
        ["--non-vacuity"],
    ):
        only, err = _parse_argv(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 10) 同一条边界在**真实 CLI** 上：exit 2、受控 ERROR、不抛裸 traceback、
    #     不进选择/baseline/mutation 阶段，且 production source 逐字节不变。
    guarded = {}
    for name in MUTATED_FILES:
        with open(os.path.join(ROOT, name), "rb") as handle:
            guarded[name] = sha256(handle.read())
    for argv in (["--only"], ["--only="], ["--onlyy=M-EXP-01"], ["--onl", "M-EXP-01"],
                 ["--dry-run"], ["foo"], ["--non-vacuity"],
                 ["--only", "M-EXP-01", "foo"],
                 ["--only", "M-EXP-01", "--only", "M-EXP-02"]):
        _assert_cli_rejected(argv)
    for name, before in guarded.items():
        with open(os.path.join(ROOT, name), "rb") as handle:
            _require(sha256(handle.read()) == before,
                     f"{name} 被一次被拒的 CLI 调用改动了")
    #    --only=<ids> 必须真的走到选择阶段（不是被解析层拒掉）：未知 id → 空选择。
    code, blob = _cli_probe(["--only=NO-SUCH-MUTATION"])
    _require(code == 2, f"--only=<ids>: expected exit 2, got {code}: {blob[:200]}")
    _require("no mutation selected" in blob,
             f"--only=<ids> must reach the selection stage: {blob[:200]}")


def self_test_optimization() -> None:
    """证明 harness 的硬守卫在 ``python -O`` 下**仍然存在**。

    ``assert`` 会被 ``-O`` 整条剥除。本 harness 的证据链守卫（anchor 唯一性、restore
    sha256、分类语义）一律走 :func:`_require`，这个 self-test 就是它的非空性证明。
    """
    root = tempfile.mkdtemp(prefix="r27b2c6_optimization_probe_")
    probe = os.path.join(root, "optimization_probe.py")
    with open(probe, "w", encoding="utf-8", newline="") as handle:
        handle.write(_OPTIMIZATION_PROBE)

    expected = ["RuntimeError", "RuntimeError", "NO-ERROR", "RuntimeError"]
    env = {key: value for key, value in os.environ.items() if key != "PYTHONOPTIMIZE"}
    for flags, want_optimized in (([], False), (["-O"], True)):
        proc = subprocess.run(
            [sys.executable, *flags, probe, os.path.abspath(__file__)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, timeout=300, env=env,
        )
        label = " ".join(flags) or "(default)"
        blob = (proc.stdout or "") + (proc.stderr or "")
        _require(proc.returncode == 0, f"optimization probe {label} failed: {blob[:400]}")
        try:
            report = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"optimization probe {label} produced no report: {blob[:400]}") from exc
        _require(report.get("optimized") is want_optimized,
                 f"python {label} did not run in the expected mode: {report}")
        _require(report.get("results") == expected,
                 f"hard guards differ under python {label}: {report}")


def assert_no_leftover(path: str, mutation_id: str) -> None:
    with open(path, encoding="utf-8") as handle:
        if MUTANT_MARKER in handle.read():
            raise RuntimeError(f"{mutation_id}: leftover mutant in {path}")


def _restore_and_verify(path: str, original: bytes, before: str, mutation_id: str) -> None:
    """按启动快照 byte-identical 还原，并校验 sha256 —— 不一致即硬失败。"""
    with open(path, "wb") as handle:
        handle.write(original)
    with open(path, "rb") as handle:
        after = sha256(handle.read())
    if after != before:
        raise RuntimeError(f"{mutation_id}: restore sha256 mismatch")
    assert_no_leftover(path, mutation_id)


def _verify_untouched(path: str, original: bytes, before: str) -> None:
    """整张矩阵跑完后，production file 必须逐字节等于启动快照。"""
    with open(path, "rb") as handle:
        current = handle.read()
    if current != original or sha256(current) != before:
        raise RuntimeError(
            f"{path}: 矩阵结束后与启动快照不一致 "
            f"({sha256(current)} != {before})"
        )


def _targets_for(selected, extra=()) -> list[str]:
    """baseline 的目标集：selected mutations 的去重 target + 点名的永久回归目标。"""
    targets: list[str] = []
    for mutation in selected:
        if mutation["test"] not in targets:
            targets.append(mutation["test"])
    for target in extra:
        if target not in targets:
            targets.append(target)
    return targets


def run_baselines(selected, *, runner=run_test, extra=()) -> int:
    """在触碰任何 production source **之前**，先证明全部目标永久回归都是 GREEN。

    baseline 是 matrix 自身的强制前提，不是可选观察项：某个目标若在干净源码上本来就红，
    它的所有 mutation 都会因 ``returncode != 0`` 被记成 CAUGHT —— 那是假证据。
    """
    targets = _targets_for(selected, extra)
    if not targets:
        print("baseline: no target selected", flush=True)
        return 1
    red: list[str] = []
    timed_out: list[str] = []
    for target in targets:
        try:
            result = runner(target)
        except subprocess.TimeoutExpired:
            print(f"baseline {target}: {BASELINE_TIMEOUT}", flush=True)
            timed_out.append(target)
            continue
        if result.returncode == 0:
            print(f"baseline {target}: GREEN", flush=True)
        else:
            print(f"baseline {target}: {BASELINE_RED}({result.returncode})", flush=True)
            red.append(target)
    if timed_out or red:
        if timed_out:
            print(
                f"baseline: TIMEOUT —— {len(timed_out)}/{len(targets)} 个目标未跑完：{timed_out}",
                flush=True,
            )
        if red:
            print(
                f"baseline: RED —— {len(red)}/{len(targets)} 个目标在干净源码上非 GREEN：{red}",
                flush=True,
            )
        print("mutation 阶段不启动（baseline 是强制前提）", flush=True)
        return 1
    print(f"baseline: GREEN（{len(targets)} 个目标全部先于 mutation 验证）", flush=True)
    return 0


def run_mutation(mutation: dict, *, root: str = ROOT, runner=run_test) -> str:
    """Return ``CAUGHT`` / ``SURVIVED`` / ``FAKE`` / ``TIMEOUT``。

    无论走哪条路径，``finally`` 都按启动快照 byte-identical 还原并校验 sha256。
    """
    path = os.path.join(root, mutation["file"])
    with open(path, "rb") as handle:
        original = handle.read()
    before = sha256(original)
    text = original.decode("utf-8").replace("\r\n", "\n")

    mutated = _apply(text, mutation)
    try:
        with open(path, "wb") as handle:
            handle.write(_adapt_eol(mutated, original))
        #: 变异体必须真的落盘：写失败 / 锚点漂移却继续跑测试，会把一次空转记成 CAUGHT。
        with open(path, encoding="utf-8") as handle:
            on_disk = handle.read()
        _require(MUTANT_MARKER in on_disk,
                 f'{mutation["id"]}: mutant marker missing on disk')
        try:
            result = runner(mutation["test"])
        except subprocess.TimeoutExpired:
            return VERDICT_TIMEOUT
        if result.returncode == 0:
            return VERDICT_SURVIVED
        if _is_fake_kill(result):
            return VERDICT_FAKE
        return VERDICT_CAUGHT
    finally:
        _restore_and_verify(path, original, before, mutation["id"])


def _apply(text: str, mutation: dict) -> str:
    """应用 mutation；anchor 必须**恰好命中一次**，否则硬失败。

    ``count == 0`` 会让 ``str.replace`` 静默返回原文 —— mutation 从未落盘，随后那条测试
    "被杀死" 就另有原因，是假证据；``count > 1`` 会让 ``replace(..., 1)`` 只改第一处，
    改的不是被证明的那一处。两种都必须硬失败，且在 ``python -O`` 下同样硬失败。
    """
    count = text.count(mutation["old"])
    _require(count == 1, (
        f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
        f'file={mutation["file"]}; anchor={mutation["old"][:60]!r}'
    ))
    return text.replace(mutation["old"], mutation["new"], 1)


def _is_fake_kill(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stdout or "") + (result.stderr or "")
    return bool(BROKEN_RE.search(blob))


def _parse_only(argv: list[str]) -> tuple[set[str] | None, str | None]:
    """解析 ``--only`` 选择器；返回 ``(selected_ids, error_message)``，两者互斥。

    只有三种结果：无 selector → ``(None, None)``；合法 → ``(ids, None)``；
    形态存在但非法 → ``(None, "ERROR: ...")`` → 调用方 exit 2。

    理由：操作者请求 targeted mutation 时，实际覆盖范围绝不能因为 CLI 拼写问题被静默放大。
    """
    matches = [item for item in argv if item.startswith("--only")]
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            f"ERROR: --only 只能出现一次（收到 {len(matches)} 个：{matches}）。"
            "重复选择器不是'取并集'，因此拒绝而不是猜。"
        )
    token = matches[0]
    if token == "--only":
        index = argv.index(token)
        if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
            return None, (
                "ERROR: --only requires a comma-separated mutation id list "
                "(for example: --only M-EXP-01,M-EXP-05)"
            )
        raw = argv[index + 1]
    elif token.startswith("--only="):
        raw = token[len("--only="):]
    else:
        return None, (
            f"ERROR: unrecognized selector argument {token!r}; 受支持的形式只有 "
            "--only <ids> 与 --only=<ids>。未知拼写不会被当作'没有 selector'来处理。"
        )
    ids = {item for item in raw.split(",") if item}
    if not ids:
        return None, f"ERROR: --only 需要非空的逗号分隔 id 列表，收到 {raw!r}"
    return ids, None


def _parse_argv(argv: list[str]) -> tuple[set[str] | None, str | None]:
    """argv 白名单 + ``--only`` 选择器；返回 ``(selected_ids, error_message)``。

    **不做静默忽略**：``--onl M-EXP-01`` / ``--dry-run`` / ``foo`` 都不是"没有 selector"，
    而是参数错误 —— 理由与 ``--only`` 那条完全相同。
    """
    if "--non-vacuity" in argv:
        return None, (
            "ERROR: --non-vacuity 已删除。baseline 现在是 matrix 的强制前提，"
            "无条件先于任何 mutation 运行，没有开关。"
        )
    unknown: list[str] = []
    expects_value = False
    for token in argv:
        if expects_value:
            # ``--only`` 的取值 token：它就是 mutation id，形态交给 _parse_only 判。
            expects_value = False
        elif token == "--only":
            expects_value = True
        elif token.startswith("--only"):
            continue
        else:
            unknown.append(token)
    if unknown:
        return None, (
            f"ERROR: unrecognized argument(s) {unknown}；受支持的形式只有 "
            "--only <ids> 与 --only=<ids>（以及被显式拒绝的 --non-vacuity）。"
            "未知 token 不会被当作'没有 selector'而跑全量 matrix。"
        )
    return _parse_only(argv)


def main() -> int:
    print(f"repo root: {ROOT}")
    only, parse_error = _parse_argv(sys.argv[1:])
    if parse_error:
        print(parse_error, flush=True)
        return 2

    selected = [m for m in MUTATIONS if only is None or m["id"] in only]
    if not selected:
        print("no mutation selected", flush=True)
        return 2
    print(f'selected: {len(selected)}/{len(MUTATIONS)} mutation(s): '
          f'{[m["id"] for m in selected]}', flush=True)

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")
    self_test_semantics()
    print("semantics self-test: PASS (anchor uniqueness / BASELINE-RED / BASELINE-TIMEOUT / "
          "CAUGHT / SURVIVED / FAKE / TIMEOUT / restore / --only)")
    self_test_optimization()
    print("optimization self-test: PASS (hard guards survive python -O)")

    #: 启动快照：整张矩阵的还原基准。矩阵结束后再整体复验一次。
    snapshots: dict[str, bytes] = {}
    for name in MUTATED_FILES:
        with open(os.path.join(ROOT, name), "rb") as handle:
            snapshots[name] = handle.read()
        print(f"startup snapshot: {name} sha256={sha256(snapshots[name])}", flush=True)

    if run_baselines(selected, extra=BASELINE_ONLY_TARGETS) != 0:
        print("mutation matrix: FAILED —— baseline 非 GREEN，mutation 阶段未启动", flush=True)
        return 1

    results: list[tuple[str, str]] = []
    for mutation in selected:
        verdict = run_mutation(mutation)
        results.append((mutation["id"], verdict))
        print(f'{mutation["id"]} {mutation["desc"]}: {verdict}', flush=True)

    bad = [(mid, v) for mid, v in results if v != VERDICT_CAUGHT]
    for mid, verdict in bad:
        print(f"NOT-CAUGHT {mid}: {verdict}")
    detected = sum(1 for _, v in results if v == VERDICT_CAUGHT)
    survived = sum(1 for _, v in results if v == VERDICT_SURVIVED)
    fake = sum(1 for _, v in results if v == VERDICT_FAKE)
    timeout = sum(1 for _, v in results if v == VERDICT_TIMEOUT)

    restore_ok = True
    for name, blob in snapshots.items():
        try:
            _verify_untouched(os.path.join(ROOT, name), blob, sha256(blob))
        except RuntimeError as exc:
            print(f"restore: FAIL —— {exc}", flush=True)
            restore_ok = False

    print(f"R27-B2C-6 mutation matrix: baseline=GREEN; "
          f"{detected}/{len(results)} DETECTED; survived={survived}; fake={fake}; "
          f"timeout={timeout}")
    gate_pass = not bad and restore_ok
    print("gate: baseline=GREEN, survived=0, fake=0, timeout=0, "
          f"restore sha256={'PASS' if restore_ok else 'FAIL'} -> "
          f"{'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
