# -*- coding: utf-8 -*-
"""R27-B2C-3 mutation matrix —— M-EXECREF-1 .. M-EXECREF-11。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让唯一指定的永久回归变 RED，
anchor 恰好命中一次；``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` /
``NameError`` 一律计为 FAKE（接线错误是假杀，不能算 caught）。

本轮的核心不变量分四组：

* **两个问题必须分开**（M-EXECREF-1 / 2 / 3）：``partial + ledger`` 与
  ``not_executed + ledger`` 是**可信的事实结论**，把它们降级成 ``unverified`` 必须 RED；
  ``unknown + ledger`` 被升级成 ``verified`` 同样必须 RED。这三条分别对应
  "把部分成交当没发生"、"把确认未执行当不可信"、"把无结论当结论"。
* **来源级别的不可用不能混进事实级别的未核验**（M-EXECREF-4 / 9）：``evidence_inconsistent``
  必须归 ``source_unusable``；映射一旦退回 catch-all（只按 status 分派），来源级别的区分
  立刻消失，必须 RED。
* **PIT 与 identity 不得被伪造**（M-EXECREF-5 / 6）：业务日 unknown 时 fallback 到
  ``observed_at`` 的日期必须 RED；``source_id`` 丢掉 owner 的
  ``identity_kind`` / ``identity`` 也必须 RED。
* **内容指纹与结构性边界**（M-EXECREF-7 / 8 / 10 / 11）：指纹被常量化 → RED；
  adapter 接受 duck-typed 投影 → RED；出现第三个未批准的私有签发调用 → RED；
  取消与 owner 契约的双向穷尽检查（新组合静默通过）→ RED。

沿用 R27-B2C-1 / B2C-2 的逐次唯一 ``PYTHONCACHEPREFIX``，否则 baseline 与 mutant 会共享
字节码缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写 production source，
跑完按启动快照做 byte-identical 还原并校验 sha256。

用法：
    python work/r27b2c3_execution_adapter_mutation_check.py
    python work/r27b2c3_execution_adapter_mutation_check.py --only M-EXECREF-1
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

ADAPTER = "backend/ai_research_execution_adapter.py"

SUITE = "test_ai_research_execution_adapter"
GUARD_SUITE = "test_ai_research_evidence_ownership_guard"


def _case(name: str) -> str:
    """Adapter 测试组里的一个用例。"""
    return f"{SUITE}.{name}"


OWNER_OUTCOME = f"{SUITE}.OwnerOutcomeMappingTests"
ISSUANCE = f"{SUITE}.IssuanceShapeTests"
MAPPING = f"{SUITE}.MappingExhaustivenessTests"
FINGERPRINT = f"{SUITE}.ContentFingerprintTests"
GUARD = f"{GUARD_SUITE}.EvidenceFactoryBoundaryTests"

MUTATIONS = [
    {
        "id": "M-EXECREF-1",
        # partial + ledger 被降级：真实发生的部分成交变成"不可信事实"。
        "file": ADAPTER,
        "old": (
            "    (\n"
            "        EV.EXECUTION_STATUS_PARTIAL,\n"
            "        EV.EVIDENCE_SOURCE_LEDGER,\n"
            "    ): ARC.OWNER_OUTCOME_VERIFIED,\n"
        ),
        "new": (
            "    (\n"
            "        EV.EXECUTION_STATUS_PARTIAL,\n"
            "        EV.EVIDENCE_SOURCE_LEDGER,\n"
            "    ): ARC.OWNER_OUTCOME_UNVERIFIED,  # MUTANT —— 部分成交被降到不可信\n"
        ),
        "test": f"{OWNER_OUTCOME}."
                "test_EXEC_REF_08_partial_ledger_is_a_trustworthy_partial_fill_fact",
        "desc": "partial + ledger 被映射成 unverified",
    },
    {
        "id": "M-EXECREF-2",
        # not_executed + ledger 被降级：确认未执行不再是可信事实。
        "file": ADAPTER,
        "old": (
            "    (\n"
            "        EV.EXECUTION_STATUS_NOT_EXECUTED,\n"
            "        EV.EVIDENCE_SOURCE_LEDGER,\n"
            "    ): ARC.OWNER_OUTCOME_VERIFIED,\n"
        ),
        "new": (
            "    (\n"
            "        EV.EXECUTION_STATUS_NOT_EXECUTED,\n"
            "        EV.EVIDENCE_SOURCE_LEDGER,\n"
            "    ): ARC.OWNER_OUTCOME_UNVERIFIED,  # MUTANT —— 确认未执行被降到不可信\n"
        ),
        "test": f"{OWNER_OUTCOME}."
                "test_EXEC_REF_09_not_executed_ledger_is_a_trustworthy_non_execution_fact",
        "desc": "not_executed + ledger 被映射成 unverified",
    },
    {
        "id": "M-EXECREF-3",
        # unknown + ledger 被升级：没有结论被当成有结论。
        "file": ADAPTER,
        "old": (
            "    (\n"
            "        EV.EXECUTION_STATUS_UNKNOWN,\n"
            "        EV.EVIDENCE_SOURCE_LEDGER,\n"
            "    ): ARC.OWNER_OUTCOME_UNVERIFIED,\n"
        ),
        "new": (
            "    (\n"
            "        EV.EXECUTION_STATUS_UNKNOWN,\n"
            "        EV.EVIDENCE_SOURCE_LEDGER,\n"
            "    ): ARC.OWNER_OUTCOME_VERIFIED,  # MUTANT —— 无结论被升级\n"
        ),
        "test": f"{OWNER_OUTCOME}."
                "test_EXEC_REF_10_unknown_ledger_cannot_be_upgraded_by_a_supporting_relation",
        "desc": "unknown + ledger 被升级成 verified",
    },
    {
        "id": "M-EXECREF-4",
        # 来源不可用被当成"事实已核验"：evidence_unavailable 与 evidence_not_verified 混同。
        "file": ADAPTER,
        "old": (
            "    (\n"
            "        EV.EXECUTION_STATUS_VERIFIED,\n"
            "        EV.EVIDENCE_SOURCE_INCONSISTENT,\n"
            "    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,\n"
        ),
        "new": (
            "    (\n"
            "        EV.EXECUTION_STATUS_VERIFIED,\n"
            "        EV.EVIDENCE_SOURCE_INCONSISTENT,\n"
            "    ): ARC.OWNER_OUTCOME_VERIFIED,  # MUTANT —— 矛盾证据被当成可信\n"
        ),
        "test": f"{OWNER_OUTCOME}."
                "test_EXEC_REF_11_unusable_evidence_sources_stay_separate_from_unverified",
        "desc": "evidence_inconsistent 被当成 verified，而非 source_unusable",
    },
    {
        "id": "M-EXECREF-5",
        # PIT：业务日 unknown 时用 observed_at 的日期冒充 owner 记录的业务日。
        "file": ADAPTER,
        "old": "    if not business_day.is_known:\n",
        "new": (
            "    if not business_day.is_known and str(projection.observed_at.maybe() or \"\"):\n"
            "        # MUTANT —— 用 observed_at 的日期冒充业务日\n"
            "        business_day = EV.EE.EvidenceField.known(\n"
            "            \"business_day\", str(projection.observed_at.maybe())[:10],\n"
            "        )\n"
            "    if not business_day.is_known:\n"
        ),
        "test": f"{ISSUANCE}."
                "test_EXEC_REF_06_unknown_business_day_fails_closed_without_any_fallback",
        "desc": "business_day unknown 时 fallback 到 observed_at 的日期",
    },
    {
        "id": "M-EXECREF-6",
        # identity 不再由 owner 投影派生：调用方自己的 order id 冒充身份。
        "file": ADAPTER,
        "old": "        source_id=f\"{projection.identity_kind}|{projection.identity}\",\n",
        "new": "        source_id=str(projection.order_id),  # MUTANT —— 丢掉 owner identity\n",
        "test": f"{ISSUANCE}."
                "test_EXEC_REF_04_source_id_is_derived_from_owner_identity_only",
        "desc": "source_id 不再包含 owner 的 identity_kind / identity",
    },
    {
        "id": "M-EXECREF-7",
        # 内容指纹被常量化：同 identity 下内容变了会被静默去重。
        "file": ADAPTER,
        "old": "    return hashlib.sha256(encoded.encode(\"utf-8\")).hexdigest()\n",
        "new": "    return hashlib.sha256(b\"execution\").hexdigest()  # MUTANT —— 指纹常量化\n",
        "test": f"{FINGERPRINT}."
                "test_EXEC_REF_16_same_identity_with_same_content_is_safely_deduped",
        "desc": "content fingerprint 被常量化（内容差异不再可见）",
    },
    {
        "id": "M-EXECREF-8",
        # adapter 接受 duck-typed 投影：owner contract 边界形同虚设。
        "file": ADAPTER,
        "old": "    if type(projection) is not EV.ExecutionFactProjection:\n",
        "new": "    if False:  # MUTANT —— 接受 duck-typed 投影\n",
        "test": f"{ISSUANCE}."
                "test_EXEC_REF_02_dict_fake_and_subclass_projections_are_all_rejected",
        "desc": "adapter 接受 duck-typed projection",
    },
    {
        "id": "M-EXECREF-9",
        # status × source 映射退回 catch-all：来源级别的区分彻底消失。
        "file": ADAPTER,
        "old": "        outcome=_EXECUTION_OUTCOME_BY_VERIFICATION[key],\n",
        "new": (
            "        outcome=(  # MUTANT —— 退回只按 status 的 catch-all\n"
            "            ARC.OWNER_OUTCOME_VERIFIED\n"
            "            if status == EV.EXECUTION_STATUS_VERIFIED\n"
            "            else ARC.OWNER_OUTCOME_UNVERIFIED\n"
            "        ),\n"
        ),
        "test": f"{OWNER_OUTCOME}."
                "test_EXEC_REF_11_unusable_evidence_sources_stay_separate_from_unverified",
        "desc": "status×source 映射改成 catch-all（来源级别语义被压平）",
    },
    {
        "id": "M-EXECREF-10",
        # 出现第三个未批准的私有签发调用。
        "file": ADAPTER,
        "old": "def _owner_legal_pairs() -> frozenset:\n",
        "new": (
            "def _unapproved_third_caller():  # MUTANT —— 未批准的第三个签发调用\n"
            "    return ARC._issue_evidence_ref()\n"
            "\n"
            "\n"
            "def _owner_legal_pairs() -> frozenset:\n"
        ),
        "test": f"{GUARD}."
                "test_EVIDENCE_03_private_issuer_callers_are_an_exact_allowlist",
        "desc": "出现第三个未批准的 private issuer caller",
    },
    {
        "id": "M-EXECREF-11",
        # 取消与 owner 契约的双向穷尽检查：owner 新增组合时静默通过。
        "file": ADAPTER,
        "old": "    if problems:\n",
        "new": "    if False:  # MUTANT —— 词表漂移不再被检查\n",
        "test": f"{MAPPING}."
                "test_EXEC_REF_12_mapping_is_exhaustive_against_the_owner_contract",
        "desc": "取消 outcome 映射与 owner 合法组合的双向穷尽检查",
    },
]


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c3_execution_adapter_pycache_")
_SEQ = [0]

#: 变异体必须因**业务断言**失败。接线错误是假杀，不能计为 CAUGHT。
BROKEN_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError"
    r"|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError"
    r"|_FailedTest|AttributeError: module"
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
    assert len(set(seen)) == len(seen), f"sequence not unique: {seen}"
    assert seen == sorted(seen), f"sequence not increasing: {seen}"
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
    assert len(dirs) == 3, f"expected 3 invocations, got {dirs}"
    assert len(set(dirs)) == 3, f"invocations share a cache dir: {dirs}"


def assert_no_leftover(mutation: dict) -> None:
    path = os.path.join(ROOT, mutation["file"])
    with open(path, encoding="utf-8") as handle:
        if "MUTANT" in handle.read():
            raise RuntimeError(f'{mutation["id"]}: leftover mutant in {mutation["file"]}')


def _apply(text: str, mutation: dict) -> str:
    count = text.count(mutation["old"])
    assert count == 1, (
        f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
        f'file={mutation["file"]}; anchor={mutation["old"][:60]!r}'
    )
    return text.replace(mutation["old"], mutation["new"], 1)


def _is_fake_kill(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stdout or "") + (result.stderr or "")
    return bool(BROKEN_RE.search(blob))


def run_mutation(mutation: dict, *, non_vacuity: bool) -> str:
    """Return ``CAUGHT`` / ``SURVIVED`` / ``FAKE`` / ``BASELINE-RED``."""
    path = os.path.join(ROOT, mutation["file"])
    with open(path, "rb") as handle:
        original = handle.read()
    before = sha256(original)
    text = original.decode("utf-8").replace("\r\n", "\n")

    if non_vacuity:
        baseline = run_test(mutation["test"])
        if baseline.returncode != 0:
            return f"BASELINE-RED({baseline.returncode})"

    mutated = _apply(text, mutation)
    try:
        with open(path, "wb") as handle:
            handle.write(_adapt_eol(mutated, original))
        result = run_test(mutation["test"])
        if result.returncode == 0:
            return "SURVIVED"
        if _is_fake_kill(result):
            return "FAKE"
        return "CAUGHT"
    finally:
        with open(path, "wb") as handle:
            handle.write(original)
        with open(path, "rb") as handle:
            after = sha256(handle.read())
        if after != before:
            raise RuntimeError(f'{mutation["id"]}: restore sha256 mismatch')
        assert_no_leftover(mutation)


def main() -> int:
    print(f"repo root: {ROOT}")
    argv = sys.argv[1:]
    only: set[str] | None = None
    if "--only" in argv:
        only = {item for item in argv[argv.index("--only") + 1].split(",") if item}
    non_vacuity = "--non-vacuity" in argv

    self_test_sequence()
    print("runner self-test: PASS (unique, increasing pycache sequence)")

    selected = [m for m in MUTATIONS if only is None or m["id"] in only]
    results: list[tuple[str, str]] = []
    for mutation in selected:
        verdict = run_mutation(mutation, non_vacuity=non_vacuity)
        results.append((mutation["id"], verdict))
        print(f'{mutation["id"]} {mutation["desc"]}: {verdict}', flush=True)

    bad = [(mid, v) for mid, v in results if v != "CAUGHT"]
    for mid, verdict in bad:
        print(f"NOT-CAUGHT {mid}: {verdict}")
    print(f"R27-B2C-3 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
