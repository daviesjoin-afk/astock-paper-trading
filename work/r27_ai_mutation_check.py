# -*- coding: utf-8 -*-
"""R27-A mutation matrix —— M-AI1..M-AI5。

只覆盖本轮**新的核心 invariant**。刻意不造几十条，也不扩成通用平台：每条
mutation 都必须让**唯一指定的永久回归**变 RED，且 anchor 恰好命中一次；
``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` / ``NameError``
一律计为 FAKE（改红了不等于证明了业务性质）。

沿用 R23/R24/R25 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录，否则 baseline
与 mutant 会共享字节码缓存，整张矩阵静默失效。

**必须串行运行**：每条 mutation 会就地改写 production source，跑完按启动快照做
byte-identical 还原并校验 sha256。

用法：
    python work/r27_ai_mutation_check.py
    python work/r27_ai_mutation_check.py --only M-AI1,M-AI4 --non-vacuity
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

CONTRACT = "backend/ai_research_contract.py"
RISK = "backend/paper_risk_service.py"

ARC = "test_ai_research_contract"


def _arc(name: str) -> str:
    """Shortcut for a test in the R27-A suite."""
    return f"{ARC}.{name}"


MUTATIONS = [
    {
        "id": "M-AI1",
        # 去掉 future-evidence check：假设可以引用**晚于自己业务日**的事实。
        # 这正是 PIT 的 look-ahead 漏洞 —— 历史研究用未来信息解释过去。
        "file": CONTRACT,
        "old": "        if self.look_ahead_refs:\n",
        "new": "        if False:\n",
        "test": _arc(
            "AiResearchContractTests."
            "test_AI03_hypothesis_referencing_future_evidence_is_rejected"
        ),
        "desc": "future-evidence check 被移除（历史假设可引用未来事实）",
    },
    {
        "id": "M-AI2",
        # 单源证据被乐观升级为 supporting：`single_source` 在 standing 映射里
        # 被当成 `verified`。于是"只有单源、没通过核验"的事实也能支撑结论 ——
        # 正是 §五 禁止的 unverified → verified 自动升级。
        "file": CONTRACT,
        "old": "    MDC.VERIFICATION_SINGLE_SOURCE: STANDING_DEGRADED,\n",
        "new": "    MDC.VERIFICATION_SINGLE_SOURCE: STANDING_SUPPORTING,\n",
        "test": _arc(
            "AiResearchContractTests."
            "test_AI02_stale_or_unverified_evidence_keeps_its_original_state"
        ),
        "desc": "单源证据被升级成 supporting（unverified → verified）",
    },
    {
        "id": "M-AI3",
        # 允许空证据假设：没有 evidence 也返回 `supported` —— 默认批准。
        # §五 明令："如果 evidence 不足，不要默认批准。"
        "file": CONTRACT,
        "old": (
            "    if not evidence_refs:\n"
            "        return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_NO_EVIDENCE\n"
        ),
        "new": (
            "    if not evidence_refs:\n"
            "        return HYPOTHESIS_SUPPORTED, None\n"
        ),
        "test": _arc(
            "AiResearchContractTests."
            "test_AI04_hypothesis_without_evidence_is_insufficient_not_approved"
        ),
        "desc": "空证据假设被默认批准（insufficient_evidence → supported）",
    },
    {
        "id": "M-AI4",
        # authority 反向 import AI 研究层：风控服务把 AI 研究契约当成依赖。
        # §六 明令禁止 —— AI 必须是纯消费者，不得反向进入现有 authority。
        "file": RISK,
        "old": "import execution_planner as EP\n",
        "new": "import ai_research_contract as ARC\nimport execution_planner as EP\n",
        "test": _arc(
            "AiResearchArchitectureGuardTests."
            "test_AIG02_no_authority_module_imports_the_ai_research_layer"
        ),
        "desc": "authority（paper_risk_service）反向 import AI 研究层",
    },
    {
        "id": "M-AI5",
        # 给 evidence source 闭集加一个 AI 自产类别：AI 自己的文本于是变成了
        # 可引用的"事实来源"。这是让数据冒充另一类 authority 的起点，也是
        # §52 的核心禁止项。
        "file": CONTRACT,
        "old": "    EVIDENCE_SOURCE_NEWS,\n)\n",
        "new": "    EVIDENCE_SOURCE_NEWS, \"llm_output\",\n)\n",
        "test": _arc(
            "AiResearchContractTests."
            "test_AI08_evidence_refs_are_frozen_deduped_and_ai_text_is_not_a_source"
        ),
        "desc": "AI 自产文本被加入 evidence source 闭集（AI 输出冒充事实）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27_ai_mutation_pycache_")
_SEQ = [0]

#: 变异体必须因**契约断言**失败。接线错误是假杀，不能计为 CAUGHT。
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
    """Static + behavioural assertion that run caches never collapse to one dir."""
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
    """Apply one mutation, requiring **every** anchor to be unique.

    ``replace(..., 1)`` rewrites the first hit; a duplicated anchor would let the
    mutation land elsewhere while still reporting CAUGHT. A mutation may carry
    ``extra`` — a list of additional ``(old, new)`` pairs — each checked for
    uniqueness too, so a multi-anchor mutation is no weaker than a single-anchor one.
    """
    pairs = [(mutation["old"], mutation["new"])]
    for extra in mutation.get("extra", ()):
        pairs.append((extra[0], extra[1]))
    for old, new in pairs:
        count = text.count(old)
        assert count == 1, (
            f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
            f'file={mutation["file"]}; anchor={old[:60]!r}'
        )
        text = text.replace(old, new, 1)
    return text


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
    print(f"R27-A mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
