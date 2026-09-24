# -*- coding: utf-8 -*-
"""R27-B2C-1 mutation matrix —— M-EXFACT-1 .. M-EXFACT-6。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让唯一指定的永久回归变 RED，
anchor 恰好命中一次；``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` /
``NameError`` 一律计为 FAKE。

本轮的核心不变量分三组：

* **owner 自己的词表与组合**（M-EXFACT-1 / 2 / 6）：接受未知状态、取消"状态×来源"的
  合法组合表、或把 market 的词塞进核验声明，都必须 RED —— 这三条正是"owner-native
  verification 不许退化"的可执行形式。
* **PIT 不许编造**（M-EXFACT-3）：多个业务日时挑一个"代表值"，必须 RED。
* **契约确实委托既有判定**（M-EXFACT-4 / 5）：identity 退化成 order_id、或绕过
  ``verification_from_evidence`` 自己宣布 verified，都必须 RED。

沿用 R27-B2B 的逐次唯一 ``PYTHONPYCACHEPREFIX``，否则 baseline 与 mutant 会共享字节码
缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写 production source，
跑完按启动快照做 byte-identical 还原并校验 sha256。

用法：
    python work/r27b2c1_execution_contract_mutation_check.py
    python work/r27b2c1_execution_contract_mutation_check.py --only M-EXFACT-1,M-EXFACT-6
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

VERIFICATION = "backend/execution_verification.py"

SUITE = "test_execution_fact_contract"


def _case(name: str) -> str:
    """Shortcut for a test in the R27-B2C-1 suite."""
    return f"{SUITE}.{name}"


MUTATIONS = [
    {
        "id": "M-EXFACT-1",
        # 接受契约之外的状态词：核验词表不再是闭集。
        "file": VERIFICATION,
        "old": "    if text_status not in EXECUTION_STATUSES:\n",
        "new": "    if False:  # MUTANT —— 未知状态词被放行\n",
        "test": _case("OwnerNativeVerificationTests."
                      "test_EXFACT_02_verification_contract_rejects_unknown_words_and_impossible_pairs"),
        "desc": "verification_contract 接受 EXECUTION_STATUSES 之外的状态词",
    },
    {
        "id": "M-EXFACT-2",
        # 取消"状态 × 来源"的合法组合表：不可能的核验声明被接受。
        "file": VERIFICATION,
        "old": "    if text_source not in _LEGAL_SOURCES_BY_STATUS.get(text_status, ()):\n",
        "new": "    if False:  # MUTANT —— 合法组合表不再生效\n",
        "test": _case("OwnerNativeVerificationTests."
                      "test_EXFACT_02_verification_contract_rejects_unknown_words_and_impossible_pairs"),
        "desc": "取消状态×来源的合法组合表（accepts verified+absent 等不可能组合）",
    },
    {
        "id": "M-EXFACT-3",
        # 多个业务日时挑一个"代表值"：编造一条它没有的 PIT 事实。
        "file": VERIFICATION,
        "old": (
            "    if len(distinct) == 1:\n"
            "        value = distinct[0]\n"
        ),
        "new": (
            "    if distinct:  # MUTANT —— 多个取值时挑一个当代表\n"
            "        value = distinct[0]\n"
        ),
        "test": _case("PitHonestyTests."
                      "test_EXFACT_04_no_owner_recorded_business_day_is_reported_unknown"),
        "desc": "多个业务日/观测时点时挑一个代表值（编造 PIT 事实）",
    },
    {
        "id": "M-EXFACT-4",
        # identity 退化成 order_id：逐次执行事实身份消失。
        "file": VERIFICATION,
        "old": (
            "    if len(keys) == 1:\n"
            "        return keys[0], IDENTITY_KIND_FILL_EVENT_KEY\n"
        ),
        "new": (
            "    if False:  # MUTANT —— 单条 event_key 不再被当作逐次身份\n"
            "        return keys[0], IDENTITY_KIND_FILL_EVENT_KEY\n"
        ),
        "test": _case("OwnerNativeVerificationTests."
                      "test_EXFACT_01_a_filled_fact_publishes_identity_day_and_verification"),
        "desc": "identity 退化成集合/order_id（逐次执行事实身份消失）",
    },
    {
        "id": "M-EXFACT-5",
        # 绕过既有判定，自己宣布 verified：出现第二份核验实现。
        "file": VERIFICATION,
        "old": "    verdict = verification_from_evidence(evidence, fill_rows_present=fill_rows_present)\n",
        "new": (
            "    verdict = {  # MUTANT —— 绕过唯一判定，自己宣布 verified\n"
            "        \"execution_status\": EXECUTION_STATUS_VERIFIED,\n"
            "        \"execution_evidence_source\": EVIDENCE_SOURCE_LEDGER,\n"
            "    }\n"
        ),
        "test": _case("SingleSourceTests."
                      "test_EXFACT_10_the_verdict_still_has_exactly_one_implementation"),
        "desc": "fact_projection 绕过 verification_from_evidence，自己宣布 verified",
    },
    {
        "id": "M-EXFACT-6",
        # 把 market 的词塞进核验声明：owner-native 词表被污染。
        "file": VERIFICATION,
        "old": (
            "        \"verification_source\": text_source,\n"
            "        \"is_verified\": is_verified_status(text_status),\n"
        ),
        "new": (
            "        \"verification_source\": text_source,\n"
            "        \"verification_method\": \"cross_source\",  # MUTANT —— market 词\n"
            "        \"is_verified\": is_verified_status(text_status),\n"
        ),
        "test": _case("OwnerNativeVerificationTests."
                      "test_EXFACT_03_the_contract_is_owner_native_not_market_vocabulary"),
        "desc": "核验声明里混入 market 的 verification_method / cross_source",
    },
    {
        "id": "M-EXFACT-7",
        # 入口不再要求真正 typed evidence：伪对象也能签发"owner projection"。
        "file": VERIFICATION,
        "old": "    if type(evidence) is not EE.ExecutionEvidence:\n",
        "new": "    if False:  # MUTANT —— 伪对象可以冒充 owner 证据\n",
        "test": _case("InputBoundaryTests."
                      "test_EXFACT_15_duck_typed_evidence_cannot_be_published_as_an_owner_projection"),
        "desc": "fact_projection 接受任意 duck-typed 伪对象",
    },
    {
        "id": "M-EXFACT-8",
        # 只校验词表、不做整份声明的精确相等：version / is_verified 可以被改。
        "file": VERIFICATION,
        "old": "        if declared != canonical:\n",
        "new": "        if False:  # MUTANT —— 非 canonical 声明被接受\n",
        "test": _case("InputBoundaryTests."
                      "test_EXFACT_16_non_canonical_verification_statement_is_rejected"),
        "desc": "核验声明不做精确相等校验（伪 version / 相反 is_verified 可通过）",
    },
    {
        "id": "M-EXFACT-9",
        # 缺 order id 时不再 fail closed：拼出 order:None 这种占位身份。
        "file": VERIFICATION,
        "old": "    if isinstance(order_id, bool) or order_id is None:\n",
        "new": "    if False:  # MUTANT —— 缺 order id 时拼占位身份\n",
        "test": _case("InputBoundaryTests."
                      "test_EXFACT_17_absent_order_id_never_becomes_a_placeholder_identity"),
        "desc": "缺 order id 时不再 fail closed（产出 order:None 占位身份）",
    },
    {
        "id": "M-EXFACT-10",
        # 取消 PIT 字段的格式校验：banana / 无时区时间戳都能变成 typed PIT fact。
        "file": VERIFICATION,
        "old": (
            "        if validator is not None and not validator(value):\n"
        ),
        "new": "        if False:  # MUTANT —— 脏值可以变成 known\n",
        "test": _case("InputBoundaryTests."
                      "test_EXFACT_18_day_and_instant_values_are_format_validated"),
        "desc": "取消 business_day / observed_at 的格式校验（banana 也能成为 known）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c1_execution_contract_pycache_")
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
    print(f"R27-B2C-1 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
