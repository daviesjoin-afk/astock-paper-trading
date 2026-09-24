# -*- coding: utf-8 -*-
"""R27-B2A mutation matrix —— M-B2A-1 .. M-B2A-8。

只覆盖本轮**新的高风险 invariant**，刻意不造几十条，也不扩成通用平台：每条 mutation
都必须让**唯一指定的永久回归**变 RED，且 anchor 恰好命中一次；``--non-vacuity`` 先跑
baseline，``SyntaxError`` / ``ImportError`` / ``NameError`` 一律计为 FAKE
（改红了不等于证明了业务性质）。

本轮的核心不变量有三组，mutation 也按这三组设计：

* **派生值不能被改写**（M-B2A-2 / 3）：``status`` / ``authority`` / ``is_authoritative``
  只能来自 typed hypothesis。改坏之后由 schema 的 ``CHECK`` 直接拦下 —— 那是生产防线
  真的生效，不是接线错误。
* **数据库不重新解释 evidence**（M-B2A-4 / 5）：丢字段、或把
  ``cross_source_verified`` 重算成 ``verification == "verified"``，必须立刻 RED。
* **append-only 与内容指纹**（M-B2A-6 / 8）：静默 upsert 必须被 append-only guard 抓到；
  ``record_hash`` 必须真的覆盖研究内容，而不是"稳定但空洞"。

沿用 R27-B1 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录，否则 baseline 与 mutant 会
共享字节码缓存，整张矩阵静默失效。

**必须串行运行**：每条 mutation 会就地改写 production source，跑完按启动快照做
byte-identical 还原并校验 sha256。

用法：
    python work/r27b2a_research_persistence_mutation_check.py
    python work/r27b2a_research_persistence_mutation_check.py --only M-B2A-1,M-B2A-7 --non-vacuity
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

REPOSITORY = "backend/ai_research_repository.py"

SUITE = "test_ai_research_repository"


def _rep(name: str) -> str:
    """Shortcut for a test in the R27-B2A suite."""
    return f"{SUITE}.{name}"


MUTATIONS = [
    {
        "id": "M-B2A-1",
        # 允许 dict / 假对象冒充 typed hypothesis：调用方重新获得自述
        # status / authority 的能力 —— 本轮要永久根除的那件事。
        "file": REPOSITORY,
        "old": "    if not isinstance(hypothesis, ARC.ResearchHypothesis):\n",
        "new": "    if False:\n",
        "test": _rep("ResearchPersistenceTypedInputTests."
                     "test_RPERSIST_02_only_typed_hypotheses_are_accepted"),
        "desc": "dict / 假对象可以冒充 typed ResearchHypothesis",
    },
    {
        "id": "M-B2A-2",
        # is_authoritative 恒写 1：研究产物自称权威。
        # schema 的 ``CHECK(is_authoritative = 0)`` 会在写入时直接拒绝。
        "file": REPOSITORY,
        "old": '        "is_authoritative": 1 if hypothesis.is_authoritative else 0,\n',
        "new": '        "is_authoritative": 1,\n',
        "test": _rep("ResearchPersistenceDerivedValueTests."
                     "test_RPERSIST_03_supported_is_derived_from_the_typed_hypothesis"),
        "desc": "is_authoritative 恒定写成 1（研究记录自称权威）",
    },
    {
        "id": "M-B2A-3",
        # authority 固定写成 signal：持久化层自己给 research 产物换一个归属。
        # ``CHECK(authority = 'research')`` 直接拒绝。
        "file": REPOSITORY,
        "old": '        "authority": hypothesis.authority,\n',
        "new": '        "authority": "signal",\n',
        "test": _rep("ResearchPersistenceDerivedValueTests."
                     "test_RPERSIST_03_supported_is_derived_from_the_typed_hypothesis"),
        "desc": "authority 被固定写成 signal（研究记录冒充裁决归属）",
    },
    {
        "id": "M-B2A-4",
        # 落库时丢掉 evidence 的 verification_method：两个正交维度只剩一个，
        # "verified 是通过哪一套 policy 得到的"永久丢失。
        "file": REPOSITORY,
        "old": (
            "    projection = hypothesis.projection()\n"
            "    hypothesis_json = _canonical_json(projection)\n"
        ),
        "new": (
            "    projection = hypothesis.projection()\n"
            "    for _item in projection.get(\"evidence\") or ():\n"
            "        _item.pop(\"verification_method\", None)\n"
            "    hypothesis_json = _canonical_json(projection)\n"
        ),
        "test": _rep("ResearchPersistenceEvidenceTests."
                     "test_RPERSIST_08_coverage_integrity_verified_is_not_cross_source"),
        "desc": "持久化时丢掉 verification_method（核验维度永久丢失）",
    },
    {
        "id": "M-B2A-5",
        # 把 cross_source_verified 重算成 verification == "verified"：
        # coverage_integrity 的 verified 被升级成"逐票双源"。R24 的永久不变量。
        "file": REPOSITORY,
        "old": (
            "    projection = hypothesis.projection()\n"
            "    hypothesis_json = _canonical_json(projection)\n"
        ),
        "new": (
            "    projection = hypothesis.projection()\n"
            "    for _item in projection.get(\"evidence\") or ():\n"
            "        _item[\"cross_source_verified\"] = "
            "_item.get(\"verification\") == \"verified\"\n"
            "    hypothesis_json = _canonical_json(projection)\n"
        ),
        "test": _rep("ResearchPersistenceEvidenceTests."
                     "test_RPERSIST_08_coverage_integrity_verified_is_not_cross_source"),
        "desc": "cross_source_verified 被重算（verified 冒充逐票双源）",
    },
    {
        "id": "M-B2A-6",
        # INSERT 退化成 INSERT OR REPLACE：append-only 台账出现静默 upsert 语义。
        # 由 append-only guard 抓 —— 改数据语句不得引用 canonical 表。
        "file": REPOSITORY,
        "old": (
            '        f"INSERT INTO {TABLE}({_WRITE_LIST}) VALUES({_WRITE_PLACEHOLDERS})",\n'
        ),
        "new": (
            '        f"INSERT OR REPLACE INTO {TABLE}({_WRITE_LIST}) "\n'
            '        f"VALUES({_WRITE_PLACEHOLDERS})",\n'
        ),
        "test": _rep("ResearchPersistenceBoundaryGuardTests."
                     "test_RPERSIST_24_no_update_delete_replace_or_upsert_on_the_canonical_table"),
        "desc": "canonical INSERT 退化成 INSERT OR REPLACE（静默 upsert）",
    },
    {
        "id": "M-B2A-7",
        # 损坏 JSON 时 ``except → {}``：数据损坏被伪装成"这里没有研究结论"。
        "file": REPOSITORY,
        "old": (
            "def _json_object(raw: Any, *, what: str) -> dict:\n"
            "    try:\n"
            "        parsed = json.loads(raw)\n"
            "    except (TypeError, ValueError):\n"
            "        raise ResearchPersistenceError(\n"
            '            REASON_CORRUPT_RECORD, f"{what} is not valid JSON",\n'
            "        ) from None\n"
        ),
        "new": (
            "def _json_object(raw: Any, *, what: str) -> dict:\n"
            "    try:\n"
            "        parsed = json.loads(raw)\n"
            "    except (TypeError, ValueError):\n"
            "        return {}\n"
        ),
        "test": _rep("ResearchPersistenceCorruptReadTests."
                     "test_RPERSIST_15_corrupt_hypothesis_json_fails_closed"),
        "desc": "损坏 hypothesis JSON 被静默替换成 {}（fail open）",
    },
    {
        "id": "M-B2A-8",
        # record_hash 不再覆盖 hypothesis 内容：hash 依然"稳定"，但 thesis / relation /
        # verification_method 的变化都不会改变它 —— 一个证明不了研究产物的指纹。
        "file": REPOSITORY,
        "old": '        "hypothesis": projection,\n',
        "new": "",
        "test": _rep("ResearchPersistenceAppendOnlyTests."
                     "test_RPERSIST_12_record_hash_covers_the_research_content"),
        "desc": "record_hash 不再覆盖研究内容（指纹稳定但空洞）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r27b2a_research_persistence_pycache_")
_SEQ = [0]

#: 变异体必须因**业务断言**失败。接线错误是假杀，不能计为 CAUGHT。
#: 注意 ``sqlite3.IntegrityError`` **不在**此列：CHECK 约束命中是本轮期望的生产防线，
#: 不是接线错误。
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
    print(f"R27-B2A mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
