# -*- coding: utf-8 -*-
"""R27-A mutation matrix —— M-AI1..M-AI6。

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
            "AiResearchFactTests."
            "test_AI03_hypothesis_referencing_future_evidence_is_rejected"
        ),
        "desc": "future-evidence check 被移除（历史假设可引用未来事实）",
    },
    {
        "id": "M-AI2",
        # 把"未经核验"当成"通过核验"：is_verified 恒为 True。
        # 于是 single_source / not_attempted 的 supports 也能让假设 supported ——
        # P1-1 那一类"事实可信度被凭空抬高"的回归。
        "file": CONTRACT,
        "old": (
            "    def is_verified(self) -> bool:\n"
            "        \"\"\"这条事实本身是否通过 owner 的核验（fact level）。\"\"\"\n"
            "        return self.ref.verification == MDC.VERIFICATION_VERIFIED\n"
        ),
        "new": (
            "    def is_verified(self) -> bool:\n"
            "        \"\"\"这条事实本身是否通过 owner 的核验（fact level）。\"\"\"\n"
            "        return True\n"
        ),
        "test": _arc(
            "AiResearchRelationTests."
            "test_AI12_unverified_fact_with_supports_is_insufficient"
        ),
        "desc": "未核验事实被当作已核验（unverified + supports → supported）",
    },
    {
        "id": "M-AI3",
        # 把 relation 从"显式声明"退化成"由 verification 决定"：
        # 校验时把任何 relation 强制重写成 supports。这正是 P1 禁止的语义绑定 ——
        # verified 只能回答"事实是否可信"，不能回答"是否支持 thesis"。
        "file": CONTRACT,
        "old": "        object.__setattr__(self, \"relation\", relation)\n",
        "new": "        object.__setattr__(self, \"relation\", RELATION_SUPPORTS)\n",
        "test": _arc(
            "AiResearchRelationTests."
            "test_AI09_verified_fact_with_context_relation_is_not_supported"
        ),
        "desc": "relation 被强制成 supports（verified 事实自动支持 thesis）",
    },
    {
        "id": "M-AI4",
        # identity 不再由 R24 投影派生：丢掉观测时点，只留 policy。
        # 于是**不同的两份快照**塌成同一个 identity —— 去重与冲突检测认不出它们，
        # duplicate / conflict 语义随之失效。这正是本轮 P1 修正要守住的性质。
        "file": CONTRACT,
        "old": '    return f"{policy}@{stamp}", as_of\n',
        "new": "    return policy, as_of\n",
        "test": _arc(
            "AiResearchTypedEvidenceTests."
            "test_AI_TYPED_02_identity_is_derived_from_the_owner_projection"
        ),
        "desc": "identity 不再由投影派生（不同快照塌成同一 identity）",
    },
    {
        "id": "M-AI5",
        # 冲突检测退化为 first-wins：不再比较同一 identity 的事实状态。
        # 于是一条 verified 后跟一条 disagreement 会产生 supported，
        # 反过来则 unsupported —— 研究结论依赖 collection order。
        "file": CONTRACT,
        "old": (
            "        states = [entry.ref.fact_state() for entry in group]\n"
            "        if any(state != states[0] for state in states):\n"
        ),
        "new": (
            "        states = [entry.ref.fact_state() for entry in group]\n"
            "        if False:\n"
        ),
        "test": _arc(
            "AiResearchRelationTests."
            "test_AI14_conflicting_duplicate_evidence_fails_closed_order_independently"
        ),
        "desc": "冲突 duplicate 退化为 first-wins（结果依赖输入顺序）",
    },
    {
        "id": "M-AI6",
        # deep freeze 退回浅冻结：嵌套 dict / list 仍可被外部改写。
        # 调用方保留的原始对象于是能篡改"已冻结"的研究内容。
        "file": CONTRACT,
        "old": (
            "    if isinstance(value, Mapping):\n"
            "        return MappingProxyType(\n"
            "            {key: _deep_freeze(item, what=what) for key, item in value.items()}\n"
            "        )\n"
        ),
        "new": (
            "    if isinstance(value, Mapping):\n"
            "        return MappingProxyType(dict(value))\n"
        ),
        "test": _arc(
            "AiResearchImmutabilityTests.test_AI17_nested_payload_is_deeply_frozen"
        ),
        "desc": "deep freeze 退回浅冻结（嵌套 payload 可被外部改写）",
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
