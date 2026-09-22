# -*- coding: utf-8 -*-
"""R25 mutation matrix —— M-SIG1..M-SIG8。

只覆盖本轮**新的核心 invariant**（§53）。刻意不造几十条，也不扩成通用平台：
每条 mutation 都必须让**唯一指定的永久回归**变 RED，且 anchor 恰好命中一次；
``--non-vacuity`` 先跑 baseline，``SyntaxError`` / ``ImportError`` / ``NameError``
一律计为 FAKE（改红了不等于证明了业务性质）。

沿用 R23/R24 已修好的 ``PYTHONPYCACHEPREFIX`` 逐次唯一目录，否则 baseline 与
mutant 会共享字节码缓存，整张矩阵静默失效。

**必须串行运行**：每条 mutation 会就地改写 production source，跑完按启动快照做
byte-identical 还原并校验 sha256。

用法：
    python work/r25_mutation_check.py
    python work/r25_mutation_check.py --only M-SIG1,M-SIG4 --non-vacuity
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

PAPER = "backend/paper_trading.py"
SERVICE = "backend/signal_service.py"
CONTRACT = "backend/market_data_contract.py"

SP = "test_signal_pipeline"


def _sig(name: str) -> str:
    """Shortcut for a test in the R25 suite."""
    return f"{SP}.{name}"


MUTATIONS = [
    {
        "id": "M-SIG1",
        "file": PAPER,
        # candidate 绕过 decision 直接 commit：用一个"永远放行"的决策替换真实裁决。
        # 这正是 §24 禁止的"候选 dict 加字段加到变成 DB 行"—— 落库不再表达任何裁决。
        "old": """                decision_result = SIG.decide_signal(
                    passed=passed, reason=reason, evidence=approval_evidence,
                )""",
        "new": """                decision_result = SIG.decide_signal(
                    passed=True, reason=reason, evidence=approval_evidence,
                )""",
        "test": _sig("SignalLedgerTests.test_SIG13_live_signal_path_rejects_a_quote_that_is_not_cross_source_verified"),
        "desc": "candidate 绕过 decision 直接放行（未通过门禁的候选也被 commit）",
    },
    {
        "id": "M-SIG2",
        # 去掉应用层的 stale 检测：把"提交事务里观测到的账户周期"换回候选期捕获的周期，
        # 使 rollover 比较退化为恒等。于是 rollover 后仍会尝试写入旧周期批次 ——
        # 只剩 DB trigger 兜底，且审计里不再有 signal_stale_cycle_context。
        "file": PAPER,
        "old": """                conn, account["id"], cycle_id=account["cycle_id"],
                account_cycle_id=current["cycle_id"], asof_day=day.isoformat())""",
        "new": """                conn, account["id"], cycle_id=account["cycle_id"],
                account_cycle_id=account["cycle_id"], asof_day=day.isoformat())""",
        "test": "test_provenance_inflight_change.SignalCycleRolloverTests.test_RV01_close_signal_rollover_does_not_restamp_old_candidates",
        "desc": "stale 检测被绕过（rollover 后仍尝试写旧周期批次，审计无 stale 事件）",
    },
    {
        "id": "M-SIG3",
        "file": SERVICE,
        # 刷新语句把不可变 provenance 列放进 DO UPDATE SET：
        # 重跑会把历史 signal 的策略版本 / 周期归属改写成今天的事实。
        "old": """    set_clause = ",".join(f"{col}=excluded.{col}" for col in _REFRESHABLE_COLUMNS)""",
        "new": """    set_clause = ",".join(
        f"{col}=excluded.{col}"
        for col in (_REFRESHABLE_COLUMNS + ("strategy_version", "cycle_id"))
    )""",
        "test": _sig("SignalWriterContractTests.test_SIG07_provenance_columns_are_absent_from_the_refresh_set_clause"),
        "desc": "刷新语句改写不可变 provenance（历史 signal 被重新贴标）",
    },
    {
        "id": "M-SIG4",
        "file": CONTRACT,
        # 双源要求退化成"policy 通过即可"：只看 verified，不看 verification_method。
        # 于是 coverage_integrity 也会被当成逐票双源 —— R24 §3 的核心混淆。
        "old": """    return (
        snapshot.verification == VERIFICATION_VERIFIED
        and snapshot.verification_method == VERIFICATION_METHOD_CROSS_SOURCE
    )""",
        "new": """    return snapshot.verification == VERIFICATION_VERIFIED""",
        "test": "test_market_data_boundary.MarketDataContractTests.test_MD15_verified_never_implies_cross_source",
        "desc": "verified 被当成双源（coverage_integrity 误判为独立交叉核验）",
    },
    {
        "id": "M-SIG5",
        "file": SERVICE,
        # 证据来源未知/单源时被乐观升级为双源：把 method 直接硬写成 cross_source。
        # 正是 §50 禁止的"默认 verified / 默认 available"。
        "old": """    method = (
        MDC.VERIFICATION_METHOD_NONE
        if verification == MDC.VERIFICATION_NOT_ATTEMPTED
        else MDC.VERIFICATION_METHOD_CROSS_SOURCE
    )""",
        "new": """    method = MDC.VERIFICATION_METHOD_CROSS_SOURCE""",
        "test": _sig("SignalContractTests.test_SIG04_single_source_and_unavailable_are_not_cross_source"),
        "desc": "单源/未核验证据被默认升级成双源 method",
    },
    {
        "id": "M-SIG6",
        "file": PAPER,
        # bootstrap 绕过统一 writer，自己内联一段 INSERT —— 出现第二个 persistence owner。
        # 这正是 §26 明令禁止的"正常 signal 一套、bootstrap 另一套 writer"。
        "old": """                    committed_payload = SIG.commit_signal(
                        conn,
                        context=bootstrap_context,
                        decision=bootstrap_decision,""",
        "new": """                    conn.execute(
                        "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,"
                        "name,payload,status,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (bootstrap_context.account_id, factor_day.isoformat(), day.isoformat(),
                         code, pick.get("name"), _json(payload), status, reason, _now()),
                    )
                    committed_payload = SIG.commit_signal(
                        conn,
                        context=bootstrap_context,
                        decision=bootstrap_decision,""",
        "test": _sig("SignalArchitectureGuardTests.test_SIGG01_signal_insert_has_exactly_one_production_owner"),
        "desc": "bootstrap 自行 INSERT（出现第二个 signal persistence owner）",
    },
    {
        "id": "M-SIG7",
        "file": PAPER,
        # provider I/O 被搬回写事务：把取行情放回 BEGIN IMMEDIATE 块内。
        # §20 的不变量是"网络必须发生在 writer transaction 之外"。
        "old": """                cooled = evidence.get("cooled") or {""",
        "new": """                quotes = _quotes(sorted(
                    str(item.get("code") or "")
                    for item in precomputed_candidates.get(account["id"], ([], {}))[0]
                ))
                news = _news_for({})
                cooled = evidence.get("cooled") or {""",
        "test": _sig("SignalArchitectureGuardTests.test_SIGG04_no_network_io_inside_the_signal_write_transactions"),
        "desc": "provider I/O 被搬进 signal 写锁内",
    },
    {
        "id": "M-SIG8",
        "file": SERVICE,
        # writer 不再要求 frozen context：允许 provenance 从行数据里进来，
        # 于是"候选批一套戳、落库另一套戳"重新变得可表达。
        "old": """    if context is None:
        raise ValueError("commit_signal requires a frozen SignalWriteContext")""",
        "new": """    if context is None:
        context = row""",
        "test": _sig("SignalWriterContractTests.test_SIG09_commit_requires_a_frozen_context"),
        "desc": "writer 接受非 frozen context（provenance 不再由批次独占）",
    },
    {
        "id": "M-SIG-D1",
        "file": SERVICE,
        # writer 忽略 decision，重新信任调用方 row：裁决字段不再由 SignalDecision
        # 独占，row 里自述的 status/reason 被直接采用。这正是 R25 复核指出的
        # mandatory Decision→Commit contract 被绕过的形状 —— 任何模块（含未来
        # R27 的 AI candidate producer）都能自己拼一行 status="pending" 落库，
        # 完全不经过 Candidate / Evidence / Decision。
        "old": """    leaked = [col for col in _DECISION_OWNED_ROW_COLUMNS if col in row]
    if leaked:
        raise ValueError(
            "commit_signal row must not carry decision-owned columns "
            f"{leaked}: 这些字段由 SignalDecision 独占，调用方提供会让"
            "Candidate→Decision→Commit 边界失效"
        )

    missing = [col for col in _ROW_COLUMNS if col not in row]""",
        "new": """    missing = [col for col in _ROW_COLUMNS if col not in row]""",
        "extra": [(
            """        # 裁决字段由 decision 独占供给。
        "status": decision.status,
        "reason": decision.reason,""",
            """        # 裁决字段改为信任 caller row。
        "status": row.get("status", decision.status),
        "reason": row.get("reason", decision.reason),""",
        )],
        "test": _sig("SignalWriterContractTests.test_SIGW02_commit_rejects_a_row_that_forges_the_decision_status"),
        "desc": "writer 忽略 decision 重新信任 caller row（绕过 Candidate→Decision 边界）",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r25_mutation_pycache_")
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
    ``extra`` — a list of additional ``(old, new)`` pairs — for cases where
    re-opening a bypass genuinely needs two edits (removing the guard *and*
    re-wiring the value it guarded). Each pair is checked for uniqueness too, so
    a multi-anchor mutation is no weaker than a single-anchor one.
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
    print(f"R25 mutations: {len(results) - len(bad)}/{len(results)} CAUGHT; "
          f"survived={sum(1 for _, v in bad if v.startswith('SURVIVED'))}; "
          f"fake={sum(1 for _, v in bad if v == 'FAKE')}; "
          f"other={sum(1 for _, v in bad if not v.startswith('SURVIVED') and v != 'FAKE')}")
    print("restore sha256: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
