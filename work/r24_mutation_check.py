# -*- coding: utf-8 -*-
"""R24 mutation matrix —— M-MD1..M-MD5。

只覆盖本轮**新的核心 invariant**（§33）。刻意不造几十条，也不扩成通用平台：
每条 mutation 都必须让**唯一指定的永久回归**变 RED，且 anchor 恰好命中一次；
``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` / ``NameError``
一律计为 FAKE（改红了不等于证明了业务性质）。

沿用 R23 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录，否则 baseline 与 mutant
会共享字节码缓存，整张矩阵静默失效。

**必须串行运行**：每条 mutation 会就地改写 production source，跑完按启动快照做
byte-identical 还原并校验 sha256。

用法：
    python work/r24_mutation_check.py
    python work/r24_mutation_check.py --only M-MD1 --non-vacuity
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

SVC = "backend/market_data_service.py"
CONTRACT = "backend/market_data_contract.py"
PAPER = "backend/paper_trading.py"

MD = "test_market_data_boundary"

MUTATIONS = [
    {
        "id": "M-MD1", "file": SVC,
        # 只读路径偷偷允许联网：read_snapshot 改成走 provider refresh。
        # 这是整轮存在理由的反面 —— 只读业务路径不得为了回答
        # "当前已知事实是什么" 而同步发起 provider 网络刷新。
        "old": """    deadline = _resolve_now(policy, now)
    snapshot = _load_cached_snapshot(kind)
    return MDC.classify(
        snapshot, policy, now=deadline, access_mode=MDC.ACCESS_READ,
        asof_day=asof_day,
    )""",
        "new": """    deadline = _resolve_now(policy, now)
    rows = dfc_module.fetch_market_snapshot_full(max_age=policy.max_age_seconds)
    snapshot = _load_cached_snapshot(kind) if not rows else MDC.MarketDataSnapshot(
        kind=kind, rows=tuple(rows), observed_at=(rows[0] or {}).get("quote_at"),
        complete=True, verification=MDC.VERIFICATION_VERIFIED,
    )
    return MDC.classify(
        snapshot, policy, now=deadline, access_mode=MDC.ACCESS_READ,
        asof_day=asof_day,
    )""",
        "test": f"{MD}.MarketDataReadPathTests.test_MDR01_read_snapshot_with_cache_never_touches_provider",
        "desc": "read_snapshot 偷偷允许联网（只读路径穿透到 provider）",
    },
    {
        "id": "M-MD2", "file": CONTRACT,
        # stale 被标成 fresh：覆盖不足分支改成放行。这会让页面/决策把陈旧事实
        # 当成可信实时行情。
        "old": """    if not satisfied:
        # 源观测时点不可解析、或横截面新鲜覆盖不足 —— 都不能算 fresh。
        return _reading(
            availability=AVAILABILITY_AVAILABLE,
            freshness=FRESHNESS_UNKNOWN if age is None else FRESHNESS_STALE,
            status=STATUS_STALE, reason=REASON_STALE,
        )""",
        "new": """    if not satisfied and False:
        # 源观测时点不可解析、或横截面新鲜覆盖不足 —— 都不能算 fresh。
        return _reading(
            availability=AVAILABILITY_AVAILABLE,
            freshness=FRESHNESS_UNKNOWN if age is None else FRESHNESS_STALE,
            status=STATUS_STALE, reason=REASON_STALE,
        )""",
        "test": f"{MD}.MarketDataReadPathTests.test_MDR03_stale_read_positive_control",
        "desc": "stale 被标成 fresh（超窗/覆盖不足仍判新鲜）",
    },
    {
        "id": "M-MD3", "file": CONTRACT,
        # disagreement 被静默挑一个源：冲突降级成"可用且已验证"。
        # 正是 §13 禁止的 ``return source_a or source_b`` 式静默 fallback。
        "old": """    if snapshot.verification == VERIFICATION_DISAGREEMENT:
        return _reading(
            availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_UNKNOWN,
            status=STATUS_UNVERIFIED, reason=REASON_CROSS_SOURCE_FAILED,
        )""",
        "new": """    if snapshot.verification == VERIFICATION_DISAGREEMENT and False:
        return _reading(
            availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_UNKNOWN,
            status=STATUS_UNVERIFIED, reason=REASON_CROSS_SOURCE_FAILED,
        )""",
        "test": f"{MD}.MarketDataContractTests.test_MD06_disagreement_is_reported_not_resolved",
        "desc": "provider 冲突被静默当成可用（不报 unverified）",
    },
    {
        "id": "M-MD4", "file": CONTRACT,
        # historical missing 时 fallback current：as-of 校验被绕过，
        # 历史请求可以直接拿 current snapshot 回填（§15 绝对禁止）。
        "old": """        if observed_day > requested:
            return MarketDataReading(""",
        "new": """        if observed_day > requested and False:
            return MarketDataReading(""",
        "test": f"{MD}.MarketDataPointInTimeTests.test_MDPIT01_current_snapshot_never_fills_an_earlier_asof",
        "desc": "historical 请求 fallback 到 current snapshot（PIT 被绕过）",
    },
    {
        "id": "M-MD5", "file": CONTRACT,
        # unavailable 被默认值填充：没有事实时返回一个"空但可用"的 reading。
        # 这正是 §29 禁止的 0 / {} / 默认指数冒充。
        # 必须是**可运行的业务错误**（合法字面量 kind，不是未定义名字）：
        # NameError 那种 RED 什么也证明不了，fake detector 也会计为 FAKE。
        "old": """    if snapshot is None:
        return unavailable_reading(policy, REASON_MISSING, access_mode=access_mode)""",
        "new": """    if snapshot is None:
        snapshot = MarketDataSnapshot(
            kind="full_market_snapshot", rows=(), complete=True,
            verification=VERIFICATION_VERIFIED,
            observed_at=(now.isoformat() if hasattr(now, "isoformat") else None),
        )""",
        "test": f"{MD}.MarketDataReadPathTests.test_MDR02_read_snapshot_without_cache_is_unavailable_not_crash",
        "desc": "unavailable 被默认值填充（没有事实却报可用）",
    },
    # ---- M-MD6..M-MD8：复审修正后新增的不变量 ----
    {
        "id": "M-MD6", "file": CONTRACT,
        # 单源快照冒充多源核验：``verified`` 允许配 ``none`` method。
        # 这正是复审指出的"verification 语义是假的"。
        "old": """    VERIFICATION_VERIFIED: (
        VERIFICATION_METHOD_CROSS_SOURCE, VERIFICATION_METHOD_COVERAGE_INTEGRITY,
    ),""",
        "new": """    VERIFICATION_VERIFIED: (
        VERIFICATION_METHOD_CROSS_SOURCE, VERIFICATION_METHOD_COVERAGE_INTEGRITY,
        VERIFICATION_METHOD_NONE,
    ),""",
        "test": f"{MD}.MarketDataContractTests.test_MD15_verified_never_implies_cross_source",
        "desc": "verified 可以不带核验方法（单源冒充双源）",
    },
    {
        "id": "M-MD7", "file": CONTRACT,
        # 横截面新鲜度只看"最新一行"：3999 条隔夜旧数据 + 1 条新数据被判 fresh。
        # 恢复复审前那个只看 observed_at 的行为。
        "old": """    ratio = fresh_ratio(rows, now, policy.max_age_seconds)
    if ratio is None:
        return False, None
    return ratio >= policy.min_fresh_ratio, ratio""",
        "new": """    return (
        newest_age is not None and newest_age <= policy.max_age_seconds
    ), None""",
        "test": f"{MD}.MarketDataContractTests.test_MD18_cross_section_needs_coverage_not_just_newest_row",
        "desc": "横截面只按最新一行判新鲜（覆盖不足仍报 fresh）",
    },
    {
        "id": "M-MD8", "file": SVC,
        # 全市场快照冒充双源核验：把 coverage_integrity 改成 cross_source。
        "old": """        verification_method=(
            MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY if complete
            else MDC.VERIFICATION_METHOD_NONE
        ),""",
        "new": """        verification_method=(
            MDC.VERIFICATION_METHOD_CROSS_SOURCE if complete
            else MDC.VERIFICATION_METHOD_NONE
        ),""",
        "test": f"{MD}.MarketDataReadPathTests.test_MDR01_read_snapshot_with_cache_never_touches_provider",
        "desc": "单源全市场快照自称 cross_source 核验过",
    },
    {
        "id": "M-MD9", "file": SVC,
        # 丢弃 payload metadata（复审指出的 health 元数据回退）：
        # saved_at / expected_rows 又被压成 None / 0。
        "old": """    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    complete = bool(dfc_module._full_snapshot_payload_is_complete(payload))
    saved_at = payload.get("saved_at")""",
        "new": """    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    complete = bool(dfc_module._full_snapshot_payload_is_complete(payload))
    saved_at = None""",
        "test": f"{MD}.MarketDataMetadataParityTests.test_MDPR01_read_snapshot_keeps_payload_metadata",
        "desc": "只读路径丢弃 saved_at（health 元数据回退）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r24_mutation_pycache_")
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
    """Apply one mutation, requiring its anchor to be **unique**.

    ``replace(..., 1)`` rewrites the first hit; a duplicated anchor would let the
    mutation land elsewhere while still reporting CAUGHT.
    """
    old, new = mutation["old"], mutation["new"]
    count = text.count(old)
    assert count == 1, (
        f'{mutation["id"]}: mutation anchor must be unique; count={count}; '
        f'file={mutation["file"]}'
    )
    return text.replace(old, new, 1)


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
    print(f"R24 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
