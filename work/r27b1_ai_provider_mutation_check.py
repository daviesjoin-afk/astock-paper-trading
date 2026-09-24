# -*- coding: utf-8 -*-
"""R27-B1 mutation matrix —— M-B1-1 .. M-B1-8。

只覆盖本轮**新的核心 invariant**，刻意不造几十条，也不扩成通用平台：每条 mutation
都必须让**唯一指定的永久回归**变 RED，且 anchor 恰好命中一次；``--non-vacuity``
先跑 baseline，``SyntaxError`` / ``ImportError`` / ``NameError`` 一律计为 FAKE
（改红了不等于证明了业务性质）。

沿用 R27-A 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录，否则 baseline 与 mutant
会共享字节码缓存，整张矩阵静默失效。

**必须串行运行**：每条 mutation 会就地改写 production source，跑完按启动快照做
byte-identical 还原并校验 sha256。

用法：
    python work/r27b1_ai_provider_mutation_check.py
    python work/r27b1_ai_provider_mutation_check.py --only M-B1-1,M-B1-4 --non-vacuity
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

ADAPTER = "backend/ai_research_provider.py"
TRANSPORT = "backend/ai_provider_transport.py"

SUITE = "test_ai_provider_transport"

MUTATIONS = [
    {
        "id": "M-B1-1",
        # 删掉 unknown evidence_id 的拒绝：provider 可以凭空引用不存在的事实，
        # 幻觉 id 会被当成"已知事实"进入研究结论。
        "file": ADAPTER,
        "old": (
            "        if not isinstance(evidence_id, str) or evidence_id not in indexed:\n"
            "            raise ResearchProviderProtocolError(\n"
            "                REASON_UNKNOWN_EVIDENCE_ID,\n"
            '                f"{evidence_id!r} was not supplied as input evidence",\n'
            "            )\n"
        ),
        "new": "        if False:\n            pass\n",
        "test": f"{SUITE}.ResearchProviderTests.test_RPROV_04_unknown_evidence_id_fails_closed",
        "desc": "unknown evidence_id 不再被拒绝（provider 可引用幻觉事实）",
    },
    {
        "id": "M-B1-2",
        # 把所有 relation 强制成 supports：事实对 thesis 的真实关系被抹平，
        # verified + context / contradicts 都会伪装成"支持"。
        "file": ADAPTER,
        "old": "        entries.append((evidence_id, relation))\n",
        "new": '        entries.append((evidence_id, "supports"))\n',
        "test": (
            f"{SUITE}.ResearchProviderTests."
            "test_RPROV_02_verified_context_is_no_supporting_evidence"
        ),
        "desc": "relation 被强制成 supports（context 冒充支持）",
    },
    {
        "id": "M-B1-3",
        # 允许 provider 输出 authority / status 字段：LLM 重新获得声明裁决与
        # 核验结果的权力 —— 本轮要永久根除的那一件事。
        #
        # 刻意**同时**拆掉两道守卫（显式 AUTHORITY_FIELDS 检查 + 严格 allowlist）。
        # 它们互相冗余：只拆一道时另一道仍会拒绝，mutation 会 SURVIVED —— 那说明的
        # 是"这里有两道独立防线"，不是"不变量没有被测到"。要建模"authority 字段被
        # 接受"这个失效状态，就必须把两道都关掉。
        "file": ADAPTER,
        "old": (
            "    forbidden = sorted(set(payload) & AUTHORITY_FIELDS)\n"
            "    if forbidden:\n"
            "        raise ResearchProviderProtocolError(\n"
            "            REASON_INVALID_PROVIDER_RESPONSE,\n"
            '            f"provider tried to declare authority fields {forbidden}",\n'
            "        )\n"
        ),
        "new": "    forbidden = []\n    if False:\n        pass\n",
        "extra": [(
            "    unknown = sorted(set(payload) - _ALLOWED_PROVIDER_FIELDS)\n"
            "    if unknown:\n"
            "        raise ResearchProviderProtocolError(\n"
            "            REASON_INVALID_PROVIDER_RESPONSE, f\"unknown provider fields {unknown}\",\n"
            "        )\n",
            "    unknown = []\n    if False:\n        pass\n",
        )],
        "test": (
            f"{SUITE}.ResearchProviderTests."
            "test_RPROV_06_provider_declaring_status_fails_closed"
        ),
        "desc": "provider 被允许声明 authority/status 字段（两道守卫同时拆除）",
    },
    {
        "id": "M-B1-4",
        # confidence > 1 自动 /100：猜 provider 用的是百分数。
        # 于是 73 被静默当成 0.73，一个协议违规变成"有效自评"。
        "file": ADAPTER,
        "old": (
            "    number = float(value)\n"
            "    if not 0.0 <= number <= 1.0:\n"
            "        raise ResearchProviderProtocolError(\n"
            '            REASON_INVALID_PROVIDER_RESPONSE, "confidence must be within [0, 1]",\n'
            "        )\n"
            "    return number\n"
        ),
        "new": (
            "    number = float(value)\n"
            "    if number > 1.0:\n"
            "        number = number / 100.0\n"
            "    if not 0.0 <= number <= 1.0:\n"
            "        raise ResearchProviderProtocolError(\n"
            '            REASON_INVALID_PROVIDER_RESPONSE, "confidence must be within [0, 1]",\n'
            "        )\n"
            "    return number\n"
        ),
        "test": (
            f"{SUITE}.ResearchProviderTests."
            "test_RPROV_09_confidence_must_be_a_fraction_in_unit_interval"
        ),
        "desc": "confidence >1 被自动 /100（猜测百分数语义）",
    },
    {
        "id": "M-B1-5",
        # 未来 evidence 不再在网络调用前拒绝：先付费调用，再在 hypothesis 构造时
        # 才发现 look-ahead —— 钱已经花了，而且错误发现得太晚。
        "file": ADAPTER,
        "old": "    if offenders:\n",
        "new": "    if False:\n",
        "test": (
            f"{SUITE}.ResearchProviderTests."
            "test_RPROV_10_future_evidence_fails_before_any_network_call"
        ),
        "desc": "未来 evidence 不在 provider 调用前拒绝（先付费再报错）",
    },
    {
        "id": "M-B1-6",
        # 重复 evidence_id 改用 first-wins：同一 id 内容不同时静默保留先到者，
        # 研究结论开始依赖 collection order。
        "file": ADAPTER,
        "old": (
            "        if existing.evidence_ref.fact_state() != event.evidence_ref.fact_state() or \\\n"
            "                _jsonable(existing.payload) != _jsonable(event.payload):\n"
        ),
        "new": "        if False:\n",
        "test": (
            f"{SUITE}.ResearchProviderTests."
            "test_RPROV_12_same_id_with_different_content_fails_closed"
        ),
        "desc": "重复 evidence_id 退化为 first-wins（结果依赖输入顺序）",
    },
    {
        "id": "M-B1-7",
        # transport 接受 JSON list 作为合法响应：协议层不再要求 object，
        # ``[]`` 变成"成功"的 provider 响应。
        "file": TRANSPORT,
        "old": "    if not isinstance(parsed, dict):\n        raise ProviderTransportError(REASON_CONTENT_NOT_OBJECT)\n",
        "new": "    if parsed is None:\n        raise ProviderTransportError(REASON_CONTENT_NOT_OBJECT)\n",
        "test": f"{SUITE}.ProviderTransportTests.test_PROVIDER_07_non_object_content_fails_closed",
        "desc": "transport 接受 JSON list 作为合法 response",
    },
    {
        "id": "M-B1-8",
        # 允许 dict 冒充 typed evidence：入口不再要求 InformationEvent，
        # "source_id=xxx / verification=verified" 这类自由字符串重新可以进入研究链路。
        "file": ADAPTER,
        "old": "        if not isinstance(event, ARC.InformationEvent):\n",
        "new": "        if False:\n",
        "test": (
            f"{SUITE}.ResearchProviderTests."
            "test_RPROV_19_input_must_be_typed_events_not_dicts_or_strings"
        ),
        "desc": "dict / raw string 可以冒充 typed evidence 进入研究链路",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b1_ai_provider_pycache_")
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
    print(f"R27-B1 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
