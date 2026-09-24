# -*- coding: utf-8 -*-
"""R27-B2C-2 mutation matrix —— M-RVERIFY-1 .. M-RVERIFY-7。

只覆盖本轮**新的高风险 invariant**。每条 mutation 都必须让唯一指定的永久回归变 RED，
anchor 恰好命中一次；``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` /
``NameError`` 一律计为 FAKE（接线错误是假杀，不能算 caught）。

本轮的核心不变量分四组：

* **判据解耦**（M-RVERIFY-1 / 2）：把 ``HypothesisEvidence.is_verified`` 改回比较 market
  的 ``verified`` 字面量、或让 owner 的 ``is_verified=False`` 被当成 True，都必须 RED
  —— 这两条正是"research 不再依赖 market 状态词"的可执行形式。
* **冲突检测 owner-neutral**（M-RVERIFY-3 / 4）：``fact_state`` 忽略 owner verification
  attributes、或 market 的 ``verification_method`` 不再参与冲突，都必须 RED。后者是 R24
  的永久不变量（``coverage_integrity`` 的 ``verified`` 不是逐票双源）。
* **不可变性**（M-RVERIFY-5）：owner verification 的嵌套 attributes 变成可变必须 RED。
* **market 语义仍归 R24**（M-RVERIFY-6 / 7）：非 market 事实又被强制要求
  ``verification_method``、或 market 的 ``cross_source_verified`` 不再委托 R24，都必须 RED。

沿用 R27-B2C-1 的逐次唯一 ``PYTHONCACHEPREFIX``，否则 baseline 与 mutant 会共享字节码
缓存，整张矩阵静默失效。**必须串行运行**：每条 mutation 就地改写 production source，
跑完按启动快照做 byte-identical 还原并校验 sha256。

用法：
    python work/r27b2c2_owner_native_verification_mutation_check.py
    python work/r27b2c2_owner_native_verification_mutation_check.py --only M-RVERIFY-1
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

SUITE = "test_ai_research_contract"

#: 本轮新测试组。mutation 的 anchor 全部落在契约的生产代码里。
GROUP = f"{SUITE}.OwnerNativeVerificationTests"


def _case(name: str) -> str:
    """Shortcut for a test in the B2C-2 group."""
    return f"{GROUP}.{name}"


MUTATIONS = [
    {
        "id": "M-RVERIFY-1",
        # 判据改回 market 字符串比较：owner-native 结论被绕过。
        "file": CONTRACT,
        "old": "        return self.ref.is_verified\n",
        "new": (
            "        return self.ref.verification == MDC.VERIFICATION_VERIFIED  "
            "# MUTANT —— 重新比较 market 字面量\n"
        ),
        "test": _case("test_RVERIFY_02_verified_judgement_does_not_depend_on_market_vocabulary"),
        "desc": "HypothesisEvidence.is_verified 改回 verification == MDC.VERIFICATION_VERIFIED",
    },
    {
        "id": "M-RVERIFY-2",
        # owner 说"没通过核验"却被当成通过：三态被压成常量。
        "file": CONTRACT,
        "old": "        return self.outcome == OWNER_OUTCOME_VERIFIED\n",
        "new": "        return True  # MUTANT —— owner 的未通过结论被当成通过\n",
        "test": _case("test_RVERIFY_03_owner_is_verified_false_cannot_support_regardless_of_spelling"),
        "desc": "OwnerVerification.is_verified 忽略 owner 的 false 结论",
    },
    {
        "id": "M-RVERIFY-3",
        # fact_state 丢掉 owner attributes：status 相同但 method 不同被误判成同一条事实。
        "file": CONTRACT,
        "old": "            self.owner_verification.canonical(),\n",
        "new": (
            "            (self.owner_verification.outcome, "
            "self.owner_verification.status),  # MUTANT —— attributes 不再参与\n"
        ),
        "test": _case("test_RVERIFY_06_market_verification_method_remains_part_of_conflict_state"),
        "desc": "fact_state 忽略 owner verification attributes（method 差异被压平）",
    },
    {
        "id": "M-RVERIFY-4",
        # canonical form 只比 status：attributes 差异不再算冲突。
        "file": CONTRACT,
        "old": "        return (self.outcome, self.status, _canonical_attributes(self.attributes))\n",
        "new": (
            "        return (self.outcome, self.status, ())  "
            "# MUTANT —— canonical form 丢掉 attributes\n"
        ),
        "test": _case("test_RVERIFY_06_market_verification_method_remains_part_of_conflict_state"),
        "desc": "OwnerVerification.canonical 丢掉 owner-specific attributes",
    },
    {
        "id": "M-RVERIFY-5",
        # 嵌套 attributes 变成可变：拿到合法 ref 的人可以改写 owner 的核验声明。
        "file": CONTRACT,
        "old": (
            "        object.__setattr__(\n"
            "            self, \"attributes\",\n"
            "            _deep_freeze(self.attributes, what=\"owner verification attributes\"),\n"
            "        )\n"
        ),
        "new": (
            "        object.__setattr__(\n"
            "            self, \"attributes\",\n"
            "            dict(self.attributes),  # MUTANT —— 只做浅拷贝，嵌套可变\n"
            "        )\n"
        ),
        "test": _case("test_RVERIFY_04_owner_verification_is_deeply_immutable"),
        "desc": "owner verification attributes 不再深冻结（嵌套可变）",
    },
    {
        "id": "M-RVERIFY-6",
        # 非 market 事实又被强制要求 market method：把 market 语义强加给别的 owner。
        "file": CONTRACT,
        "old": (
            "        if self.source_type != EVIDENCE_SOURCE_MARKET_DATA:\n"
            "            return None\n"
            "        return self.owner_verification.attributes.get(\"verification_method\")\n"
        ),
        "new": (
            "        return self.owner_verification.attributes.get(\n"
            "            \"verification_method\", MDC.VERIFICATION_METHOD_NONE,\n"
            "        )  # MUTANT —— 用 market 词代表非 market owner\n"
        ),
        "test": _case("test_RVERIFY_08_non_market_verification_does_not_require_a_market_method"),
        "desc": "非 market 事实被填上 MDC.VERIFICATION_METHOD_NONE",
    },
    {
        "id": "M-RVERIFY-7",
        # 双源判据不再委托 R24：本层自行比较 verified 字面量。
        "file": CONTRACT,
        "old": (
            "        if self.source_type != EVIDENCE_SOURCE_MARKET_DATA:\n"
            "            return False\n"
            "        return bool(self.owner_verification.attributes.get(\"cross_source_verified\"))\n"
        ),
        "new": (
            "        return self.owner_verification.status == \"verified\"  "
            "# MUTANT —— 自行比较 verified 字面量\n"
        ),
        "test": _case("test_RVERIFY_07_market_cross_source_semantics_stay_delegated_to_r24"),
        "desc": "market cross_source_verified 不再委托 R24，改为比较 verified 字面量",
    },
    {
        "id": "M-RVERIFY-8",
        # 显式穷尽映射退回 catch-all else：R24 未来的新状态被静默分类。
        "file": CONTRACT,
        "old": "    outcome = _MARKET_OUTCOME_BY_VERIFICATION[verification]\n",
        "new": (
            "    if verification == MDC.VERIFICATION_VERIFIED:  # MUTANT —— 退回 catch-all\n"
            "        outcome = OWNER_OUTCOME_VERIFIED\n"
            "    else:\n"
            "        outcome = OWNER_OUTCOME_UNVERIFIED\n"
        ),
        "test": _case("test_RVERIFY_11_market_outcome_mapping_is_exhaustive_and_fail_closed"),
        "desc": "显式穷尽映射退回 catch-all else（新状态被静默归为 unverified）",
    },
    {
        "id": "M-RVERIFY-9",
        # 取消词表穷尽检查：R24 新增合法状态不再 fail closed。
        "file": CONTRACT,
        "old": (
            "    problems = _market_outcome_mapping_problems(\n"
            "        _MARKET_OUTCOME_BY_VERIFICATION, MDC.VERIFICATIONS,\n"
            "    )\n"
            "    if problems:\n"
        ),
        "new": (
            "    problems = []  # MUTANT —— 词表漂移不再被检查\n"
            "    if problems:\n"
        ),
        "test": _case("test_RVERIFY_11_market_outcome_mapping_is_exhaustive_and_fail_closed"),
        "desc": "取消 market outcome 映射的穷尽性检查（R24 新状态静默通过）",
    },
]


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2c2_owner_native_pycache_")
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
    print(f"R27-B2C-2 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
