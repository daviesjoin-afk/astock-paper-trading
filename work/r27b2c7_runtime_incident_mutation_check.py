# -*- coding: utf-8 -*-
"""R27-B2C-7 —— runtime / incident owner fact 与 runtime adapter 的 **mutation matrix**。

覆盖 ``M-INC-01`` ~ ``M-INC-15``，逐条对应本轮要钉死的不变量：

```text
runtime lifecycle status      ≠ owner fact verification（含 failed / completed 两侧）
incident severity             ≠ owner verification
started_at                    ≠ terminal result availability
profile_date / market_date     ≠ terminal result availability
mutable 当前行                不得回填被覆盖的历史 revision
malformed detail              fail closed（绝不 {} 兜底）
unknown runtime status        绝不默认 recorded
paper_orders 业务拒绝         不是系统事故，runtime 侧不读它
上一轮 AI research 结论        只是上下文，不得重签成 owner fact
adapter 只接受精确类型，且不接受 caller 命名的 source_id / as_of
指纹必须覆盖 revision identity（同一业务日内也要随内容变化而变）
runtime source registry 与真实 factory 双向一致
```

────────────── 本轮的 mutation 设计原则 ──────────────

每一条变异都**改写真实的 owner / adapter 契约代码**（不是伪造一个不存在的字段），并各自由
一条永久回归捕获。B43 列出的建议清单里有两条在本轮**不存在对应契约**，因此按真实接缝落点，
而不是造一个假锚点：

* "``adaptive_execution_evidence`` 的 status 进入 verification" —— 本轮**刻意没有**为那张表
  发布 typed 契约（``status`` 是自由字符串，见 ``INC-23b``）。因此它不作为 mutation，而是由
  ``INC_23b`` 直接断言"没有闭集常量 / 没有 typed 读入口"。
* "adapter 接受 ``Mapping``" —— 与本轮真正的宽化风险是 ``isinstance``（子类）而非 ``Mapping``
  （后者会以 ``AttributeError`` 失败，属接线错误）。因此 ``M-INC-11`` 变异 ``isinstance``，
  由 ``INC_24`` 的**子类**断言捕获。

────────────── PYTHONPYCACHEPREFIX 必须唯一 ──────────────

每次 ``run_test`` 都用**唯一**的 ``PYTHONPYCACHEPREFIX``。共享 pycache 会让一次变异后的
编译缓存泄漏到下一次运行，于是"测试被杀死"可能来自上一次的 mutant 而不是这一次 —— 那是
假证据。:func:`self_test_sequence` 就证明这一点。

**必须串行运行**：本 harness 会改写 production source 再还原，并行会互相覆盖。

用法：

```bash
python work/r27b2c7_runtime_incident_mutation_check.py
python work/r27b2c7_runtime_incident_mutation_check.py --only M-INC-01
python work/r27b2c7_runtime_incident_mutation_check.py --only=M-INC-01,M-INC-05
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
ADAPTIVE_FILE = "backend/adaptive_engine.py"
PAPER_FILE = "backend/paper_trading.py"
ADAPTER_FILE = "backend/ai_research_runtime_adapter.py"
CONTRACT_FILE = "backend/ai_research_contract.py"

MUTATED_FILES = (ADAPTIVE_FILE, PAPER_FILE, ADAPTER_FILE, CONTRACT_FILE)

#: 收尾复验基准：矩阵跑完后用它做一次"真的没被动过"的独立检查。
DS_FILE = ADAPTER_FILE

SUITE_OWNER = "test_runtime_incident_owner_facts"
SUITE_ADAPTER = "test_ai_research_runtime_adapter"


def _owner(name: str) -> str:
    return f"{SUITE_OWNER}.{name}"


def _adapter(name: str) -> str:
    return f"{SUITE_ADAPTER}.{name}"


T_INC_01 = _owner("RuntimeWriterBoundaryTests."
                  "test_INC_01_adaptive_runs_lifecycle_writer_is_the_owner_only")
T_INC_02 = _owner("RuntimeWriterBoundaryTests."
                  "test_INC_02_paper_jobs_writer_is_the_owner_only")
T_INC_03 = _owner("RuntimeWriterBoundaryTests."
                  "test_INC_03_paper_job_runs_writer_is_the_owner_only")
T_INC_03B = _owner("RuntimeWriterBoundaryTests."
                   "test_INC_03b_adaptive_execution_evidence_writer_is_the_owner_only")
T_INC_03C = _owner("RuntimeWriterBoundaryTests."
                   "test_INC_03c_dynamic_table_name_writers_are_registered_and_scoped")
T_INC_04 = _owner("RuntimeLifecycleVsVerificationTests."
                  "test_INC_04_lifecycle_status_and_fact_verification_are_disjoint")
T_INC_05 = _owner("RuntimeLifecycleVsVerificationTests."
                  "test_INC_05_an_adaptive_failed_run_is_an_owner_verified_fact")
T_INC_06 = _owner("RuntimeLifecycleVsVerificationTests."
                  "test_INC_06_an_adaptive_completed_run_is_also_an_owner_verified_fact")
T_INC_07 = _owner("RuntimeLifecycleVsVerificationTests."
                  "test_INC_07_owner_modules_carry_no_incident_severity_vocabulary")
T_INC_08 = _owner("RuntimeLifecycleVsVerificationTests."
                  "test_INC_08_completed_is_not_a_generic_verified_status")
T_INC_09 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_09_started_at_cannot_be_terminal_result_availability")
T_INC_10 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_10_profile_date_cannot_be_terminal_result_availability")
T_INC_11 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_11_paper_market_date_cannot_be_failure_availability")
T_INC_12 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_12_finished_at_after_as_of_fails_closed")
T_INC_13 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_13_a_mutable_current_row_never_backfills_an_older_revision")
T_INC_14 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_14_malformed_detail_json_fails_closed")
T_INC_15 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_15_unknown_runtime_status_is_never_defaulted_to_recorded")
T_INC_16 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_16_timezone_normalization_uses_the_owner_timezone")
T_INC_17 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_17_a_cross_offset_instant_cannot_be_issued_a_day_early")
T_INC_18 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_18_a_paper_retry_overwrite_cannot_rebuild_the_old_failure")
T_INC_19 = _owner("RuntimePitAvailabilityTests."
                  "test_INC_19_attempt_identity_comes_from_the_owner_not_the_caller")
T_INC_19B = _owner("RuntimePitAvailabilityTests."
                   "test_INC_19b_heartbeat_does_not_move_the_fact_identity")
T_INC_19C = _owner("RuntimePitAvailabilityTests."
                   "test_INC_19c_a_running_row_never_claims_terminal_availability")
T_INC_19D = _owner("RuntimePitAvailabilityTests."
                   "test_INC_19d_a_terminal_row_without_its_terminal_instant_is_refused")
T_INC_19E = _owner("RuntimePitAvailabilityTests."
                   "test_INC_19e_self_inconsistent_timestamps_are_unproven_not_recorded")
T_INC_19F = _owner("RuntimePitAvailabilityTests."
                   "test_INC_19f_as_of_is_mandatory")
T_INC_19G = _owner("RuntimePitAvailabilityTests."
                   "test_INC_19g_killed_rows_have_no_provable_terminal_availability")
T_INC_20 = _owner("RuntimeAuthoritySeparationTests."
                  "test_INC_20_paper_runtime_locks_is_not_an_incident_fact")
T_INC_21 = _owner("RuntimeAuthoritySeparationTests."
                  "test_INC_21_paper_orders_business_rejection_is_not_a_runtime_incident")
T_INC_22 = _owner("RuntimeAuthoritySeparationTests."
                  "test_INC_22_execution_nonfill_still_belongs_to_the_execution_owner")
T_INC_23 = _owner("RuntimeAuthoritySeparationTests."
                  "test_INC_23_previous_research_output_stays_context_only")
T_INC_23B = _owner("RuntimeAuthoritySeparationTests."
                   "test_INC_23b_adaptive_execution_evidence_status_is_not_typed_evidence")
T_INC_24 = _adapter("RuntimeAdapterEntryBoundaryTests."
                    "test_INC_24_the_factory_accepts_only_the_exact_approved_types")
T_INC_25 = _adapter("RuntimeAdapterEntryBoundaryTests."
                    "test_INC_25_the_caller_cannot_supply_a_source_id")
T_INC_26 = _adapter("RuntimeAdapterEntryBoundaryTests."
                    "test_INC_26_the_caller_cannot_supply_an_as_of")
T_INC_26B = _adapter("RuntimeAdapterEntryBoundaryTests."
                     "test_INC_26b_a_failed_run_is_verified_and_an_unproven_row_is_not")
T_INC_27 = _adapter("RuntimeAdapterFingerprintTests."
                    "test_INC_27_the_content_fingerprint_is_deterministic")
T_INC_28 = _adapter("RuntimeAdapterFingerprintTests."
                    "test_INC_28_a_revision_or_content_change_moves_the_fingerprint")
T_INC_29 = _adapter("RuntimeAdapterIoBoundaryTests."
                    "test_INC_29_the_adapter_owns_no_db_no_network_no_clock")
T_INC_29B = _adapter("RuntimeAdapterIoBoundaryTests."
                     "test_INC_29b_the_adapter_has_no_second_authority_vocabulary")
T_INC_30 = _adapter("RuntimeSourceRegistryTests."
                    "test_INC_30_the_runtime_source_registry_matches_the_real_factory")
T_INC_30B = _adapter("RuntimeSourceRegistryTests."
                     "test_INC_30b_the_outcome_mapping_drift_check_reports_both_directions")
T_INC_30C = _adapter("RuntimeSourceRegistryTests."
                     "test_INC_30c_an_unknown_owner_verification_status_is_refused")
T_INC_30D = _adapter("RuntimeSourceRegistryTests."
                     "test_INC_30d_the_research_kind_is_derived_from_the_source_type")
T_INC_31 = _adapter("RuntimeMigrationDeferralTests."
                    "test_INC_31_the_runtime_adapter_has_no_production_callers")
T_INC_32 = _adapter("RuntimeMigrationDeferralTests."
                    "test_INC_32_incident_evidence_remains_deferred_legacy_runtime")

#: 点名的永久回归目标：不在本 matrix 的 mutation 里，但必须与 mutation target 一起先证明
#: 在**干净源码**上 GREEN。否则"这些目标也验过"只是句话。
BASELINE_ONLY_TARGETS = (
    T_INC_01,
    T_INC_02,
    T_INC_03,
    T_INC_03B,
    T_INC_03C,
    T_INC_04,
    T_INC_08,
    T_INC_10,
    T_INC_13,
    T_INC_16,
    T_INC_17,
    T_INC_18,
    T_INC_19,
    T_INC_19B,
    T_INC_19C,
    T_INC_19D,
    T_INC_19E,
    T_INC_19F,
    T_INC_19G,
    T_INC_20,
    T_INC_22,
    T_INC_23B,
    T_INC_27,
    T_INC_29,
    T_INC_29B,
    T_INC_30,
    T_INC_30C,
    T_INC_30D,
    T_INC_31,
    T_INC_32,
)

#: 每条 mutation 都带 ``MUTANT`` 记号，且 ``old`` 必须**恰好命中一次**。
MUTATIONS: list[dict] = [
    {
        "id": "M-INC-01",
        "file": ADAPTER_FILE,
        "desc": "failed lifecycle status 直接映射成 owner verification",
        "old": (
            "    status = str(projection.fact_verification_status)\n"
            "    if status not in _RUNTIME_OUTCOME_BY_STATUS:"
        ),
        "new": (
            "    status = (\n"
            "        AE.ADAPTIVE_RUN_FACT_OWNER_UNPROVEN  # MUTANT\n"
            "        if getattr(projection, \"runtime_status\", None) == \"failed\"\n"
            "        else str(projection.fact_verification_status)\n"
            "    )\n"
            "    if status not in _RUNTIME_OUTCOME_BY_STATUS:"
        ),
        "test": T_INC_26B,
    },
    {
        "id": "M-INC-02",
        "file": ADAPTIVE_FILE,
        "desc": "completed lifecycle 被当成 fact verification 本身",
        "old": (
            '        """**恒为** ``False``：lifecycle status 与 fact verification 是两个维度。"""\n'
            "        return False"
        ),
        "new": (
            '        """**恒为** ``False``：lifecycle status 与 fact verification 是两个维度。"""\n'
            '        return self.runtime_status == "completed"  # MUTANT'
        ),
        "test": T_INC_26B,
    },
    {
        "id": "M-INC-03",
        "file": PAPER_FILE,
        "desc": "as_of 从 finished_at 退化成 market_date",
        "old": (
            "    instant = finished_at if kind == PAPER_JOB_RUN_AVAILABILITY_TERMINAL else started_at\n"
            "    available_day = _paper_runtime_owner_day(\n"
            '        instant, what=f"paper job run {run_key} availability instant",\n'
            "    )"
        ),
        "new": (
            "    instant = _paper_runtime_owner_instant(  # MUTANT\n"
            '        str(item.get("market_date")) + " 00:00:00", what="mutant market_date",\n'
            "    )\n"
            "    available_day = _paper_runtime_owner_day(\n"
            '        instant, what=f"paper job run {run_key} availability instant",\n'
            "    )"
        ),
        "test": T_INC_11,
    },
    {
        "id": "M-INC-04",
        "file": ADAPTIVE_FILE,
        "desc": "as_of 从 finished_at 退化成 started_at",
        "old": (
            "    instant = finished_at if kind == ADAPTIVE_RUN_AVAILABILITY_TERMINAL else started_at\n"
            '    available_day = _runtime_owner_day(instant, what="adaptive run availability instant")'
        ),
        "new": (
            "    instant = started_at  # MUTANT\n"
            '    available_day = _runtime_owner_day(instant, what="adaptive run availability instant")'
        ),
        "test": T_INC_09,
    },
    {
        "id": "M-INC-05",
        "file": ADAPTIVE_FILE,
        "desc": "current revision 允许回填 historical as_of",
        "old": (
            "    if available_day > day:\n"
            "        return None\n"
            "\n"
            "    new_rewards = item.get(\"new_rewards\")"
        ),
        "new": (
            "    if False:  # MUTANT\n"
            "        return None\n"
            "\n"
            "    new_rewards = item.get(\"new_rewards\")"
        ),
        "test": T_INC_12,
    },
    {
        "id": "M-INC-06",
        "file": ADAPTIVE_FILE,
        "desc": "malformed detail JSON 回落成空对象",
        "old": (
            "def _runtime_canonical_detail(value: Any, *, what: str) -> str:\n"
            "    return _runtime_canonical_json(_runtime_strict_mapping(value, what=what), what=what)"
        ),
        "new": (
            "def _runtime_canonical_detail(value: Any, *, what: str) -> str:\n"
            "    try:  # MUTANT\n"
            "        return _runtime_canonical_json(_runtime_strict_mapping(value, what=what), what=what)\n"
            "    except AdaptiveRuntimeFactError:\n"
            '        return "{}"'
        ),
        "test": T_INC_14,
    },
    {
        "id": "M-INC-07",
        "file": ADAPTIVE_FILE,
        "desc": "unknown runtime status 默认成可签发的 terminal",
        "old": (
            "    if runtime_status in ADAPTIVE_RUN_IN_PROGRESS_STATUSES:\n"
            "        return ADAPTIVE_RUN_AVAILABILITY_IN_PROGRESS\n"
            "    return None"
        ),
        "new": (
            "    if runtime_status in ADAPTIVE_RUN_IN_PROGRESS_STATUSES:\n"
            "        return ADAPTIVE_RUN_AVAILABILITY_IN_PROGRESS\n"
            "    return ADAPTIVE_RUN_AVAILABILITY_TERMINAL  # MUTANT"
        ),
        "test": T_INC_15,
    },
    {
        "id": "M-INC-08",
        "file": ADAPTER_FILE,
        "desc": "runtime adapter 直接读取 paper_orders 非成交分布",
        "old": '__all__ = (\n    "evidence_ref_from_runtime_projection",\n)',
        "new": (
            '__all__ = (\n    "evidence_ref_from_runtime_projection",\n)\n'
            "\n"
            "_NONFILL_SQL = (  # MUTANT\n"
            '    "SELECT status,COUNT(*) count FROM paper_orders WHERE status!=\'filled\' "\n'
            '    "GROUP BY status"\n'
            ")"
        ),
        "test": T_INC_21,
    },
    {
        "id": "M-INC-09",
        "file": ADAPTER_FILE,
        "desc": "业务拒绝词汇被分类成 incident factual verification",
        "old": (
            "_RUNTIME_OUTCOME_BY_STATUS = {\n"
            "    AE.ADAPTIVE_RUN_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,"
        ),
        "new": (
            "_RUNTIME_OUTCOME_BY_STATUS = {\n"
            '    "rejected": ARC.OWNER_OUTCOME_VERIFIED,  # MUTANT\n'
            "    AE.ADAPTIVE_RUN_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,"
        ),
        "test": T_INC_30B,
    },
    {
        "id": "M-INC-10",
        "file": ADAPTER_FILE,
        "desc": "previous AI research 结论被重签成 owner fact",
        "old": "import adaptive_engine as AE\nimport ai_research_contract as ARC",
        "new": (
            "import adaptive_engine as AE\n"
            "import ai_research_repository  # MUTANT\n"
            "import ai_research_contract as ARC"
        ),
        "test": T_INC_23,
    },
    {
        "id": "M-INC-11",
        "file": ADAPTER_FILE,
        "desc": "adapter 接受子类（isinstance 宽化）",
        "old": (
            "    if type(projection) is AE.AdaptiveRunFactProjection:\n"
            "        return AE.ADAPTIVE_RUN_RECORD_KIND\n"
            "    if type(projection) is PT.PaperJobRunFactProjection:\n"
            "        return PT.PAPER_JOB_RUN_RECORD_KIND"
        ),
        "new": (
            "    if isinstance(projection, AE.AdaptiveRunFactProjection):\n"
            "        return AE.ADAPTIVE_RUN_RECORD_KIND\n"
            "    if isinstance(projection, PT.PaperJobRunFactProjection):  # MUTANT\n"
            "        return PT.PAPER_JOB_RUN_RECORD_KIND"
        ),
        "test": T_INC_24,
    },
    {
        "id": "M-INC-12",
        "file": ADAPTER_FILE,
        "desc": "caller 可传 source_id",
        "old": "def evidence_ref_from_runtime_projection(projection: Any) -> ARC.ResearchEvidenceRef:",
        "new": (
            "def evidence_ref_from_runtime_projection(  # MUTANT\n"
            "    projection: Any, source_id: Any = None,\n"
            ") -> ARC.ResearchEvidenceRef:"
        ),
        "test": T_INC_25,
    },
    {
        "id": "M-INC-13",
        "file": ADAPTER_FILE,
        "desc": "caller 可传 as_of",
        "old": "def evidence_ref_from_runtime_projection(projection: Any) -> ARC.ResearchEvidenceRef:",
        "new": (
            "def evidence_ref_from_runtime_projection(  # MUTANT\n"
            "    projection: Any, as_of: Any = None,\n"
            ") -> ARC.ResearchEvidenceRef:"
        ),
        "test": T_INC_26,
    },
    {
        "id": "M-INC-14",
        "file": ADAPTIVE_FILE,
        "desc": "指纹忽略 revision identity（同业务日内不再随内容变化）",
        "old": (
            '            "detail": self.detail_canonical,\n'
            '            "started_at": self.started_at,\n'
            '            "finished_at": self.finished_at,'
        ),
        "new": (
            '            "detail": self.detail_canonical,\n'
            '            "started_at": self.started_at,\n'
            '            "finished_at": "MUTANT-IGNORED",'
        ),
        "test": T_INC_28,
    },
    {
        "id": "M-INC-15",
        "file": CONTRACT_FILE,
        "desc": "runtime source registry 漂移（登记与 factory 不再双向一致）",
        "old": (
            "    EVIDENCE_SOURCE_STRATEGY_RESEARCH,\n"
            "    EVIDENCE_SOURCE_RUNTIME_INCIDENT,\n"
            "})"
        ),
        "new": (
            "    EVIDENCE_SOURCE_STRATEGY_RESEARCH,\n"
            "})  # MUTANT"
        ),
        "test": T_INC_30,
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
PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c7_adaptive_experiment_pycache_")
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
    root = tempfile.mkdtemp(prefix="r27b2c7_mutation_semantics_")
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
        (["--only", "M-INC-01"], {"M-INC-01"}),
        (["--only=M-INC-01"], {"M-INC-01"}),
        (["--only", "M-INC-01,M-INC-02"], {"M-INC-01", "M-INC-02"}),
        (["--only=M-INC-01,M-INC-02"], {"M-INC-01", "M-INC-02"}),
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
        ["--onlyy=M-INC-01"],
        ["--only", "--only"],
        ["--only", "M-INC-01", "--only", "M-INC-02"],
    ):
        only, err = _parse_only(argv)
        _require(only is None and isinstance(err, str) and err.startswith("ERROR:"),
                 f"{argv} must be a controlled ERROR, got {(only, err)}")

    # 9b) argv 白名单（main 真正走的入口）：不认识的 token 不是"没有 selector"。
    _require(_parse_argv([]) == (None, None), "empty argv must mean the full matrix")
    for argv, ids in (
        (["--only", "M-INC-01"], {"M-INC-01"}),
        (["--only=M-INC-01"], {"M-INC-01"}),
    ):
        _require(_parse_argv(argv) == (ids, None), f"{argv} must select {ids}")
    _require(_parse_argv(["--only", "UNKNOWN-ID"]) == ({"UNKNOWN-ID"}, None),
             "an unknown id is a selection miss, not a parse error")
    for argv in (
        ["--only"],
        ["--only="],
        ["--onlyy=M-INC-01"],
        ["--onl", "M-INC-01"],
        ["--dry-run"],
        ["foo"],
        ["--only", "M-INC-01", "foo"],
        ["--only", "M-INC-01", "--only", "M-INC-02"],
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
    for argv in (["--only"], ["--only="], ["--onlyy=M-INC-01"], ["--onl", "M-INC-01"],
                 ["--dry-run"], ["foo"], ["--non-vacuity"],
                 ["--only", "M-INC-01", "foo"],
                 ["--only", "M-INC-01", "--only", "M-INC-02"]):
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
    root = tempfile.mkdtemp(prefix="r27b2c7_optimization_probe_")
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
                "(for example: --only M-INC-01,M-INC-05)"
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

    **不做静默忽略**：``--onl M-INC-01`` / ``--dry-run`` / ``foo`` 都不是"没有 selector"，
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

    print(f"R27-B2C-7 mutation matrix: baseline=GREEN; "
          f"{detected}/{len(results)} DETECTED; survived={survived}; fake={fake}; "
          f"timeout={timeout}")
    gate_pass = not bad and restore_ok
    print("gate: baseline=GREEN, survived=0, fake=0, timeout=0, "
          f"restore sha256={'PASS' if restore_ok else 'FAIL'} -> "
          f"{'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
