# -*- coding: utf-8 -*-
"""Guard 14l/14m/14n/14o/14p 的非空性证明（本地证据，不提交）。

对每条 guard 施加一处真实违规改动，确认它**真的变红**；再确认基线为绿。
不做 byte-identical 恢复的只有本脚本自己临时改写的片段（finally 里还原）。
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
RESOLVER = os.path.join(BACKEND, "strategy_selection_resolver.py")
SELECTION = os.path.join(BACKEND, "paper_selection.py")
PAPER = os.path.join(BACKEND, "paper_trading.py")

GUARD = "test_paper_trading_architecture_guard.SelectionProvenanceIsVersionPinned"

CASES = [
    {
        "guard": f"{GUARD}.test_guard14l_signal_cycle_requires_a_keyword_only_explicit_cycle",
        "file": RESOLVER,
        "old": "def signal_cycle_provenance(conn, account_id, *, cycle_id):",
        "new": "def signal_cycle_provenance(conn, account_id, cycle_id=None):",
        "desc": "cycle_id 变回可选位置参数（keyword-only 契约被绕过）",
    },
    {
        "guard": f"{GUARD}.test_guard14m_signal_cycle_body_never_reads_paper_accounts",
        "file": RESOLVER,
        "old": """    requested = SP.canonical_cycle_id(cycle_id)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)
    if stamp is None:
        raise SignalCycleUnprovable(
            account_id, requested, "cycle has no pinned immutable strategy version"
        )
    return requested, tuple(stamp)""",
        "new": """    row = conn.execute(
        "SELECT cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
    ).fetchone()
    requested = SP.canonical_cycle_id(row[0] if row is not None else None)
    if requested is None:
        raise SignalCycleUnprovable(account_id, None, "no durable cycle")
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)
    if stamp is None:
        raise SignalCycleUnprovable(
            account_id, requested, "cycle has no pinned immutable strategy version"
        )
    return requested, tuple(stamp)""",
        "desc": "signal_cycle_provenance 函数体重新读 paper_accounts",
    },
    {
        "guard": f"{GUARD}.test_guard14n_generate_signals_passes_a_frozen_explicit_cycle",
        "file": PAPER,
        "old": """            account_context, context_error = SRES.signal_write_context_or_error(
                conn, account["id"], cycle_id=account["cycle_id"],
                account_cycle_id=current["cycle_id"], asof_day=day.isoformat())""",
        "new": """            account_context, context_error = SRES.signal_write_context_or_error(
                conn, account["id"], cycle_id=current["cycle_id"],
                account_cycle_id=current["cycle_id"], asof_day=day.isoformat())""",
        "desc": "generate_signals 丢掉候选构建前的捕获周期，改用提交时观测值",
    },
    {
        "guard": f"{GUARD}.test_guard14o_bootstrap_signals_passes_an_explicit_cycle",
        "file": PAPER,
        "old": """                bootstrap_context, bootstrap_error = SRES.signal_write_context_or_error(
                    conn, account["id"], cycle_id=cycle["id"],
                    account_cycle_id=account["cycle_id"], asof_day=day.isoformat())""",
        "new": """                bootstrap_context, bootstrap_error = SRES.signal_write_context_or_error(
                    conn, account["id"], cycle_id=cycle["id"],
                    account_cycle_id=cycle["id"], asof_day=day.isoformat())""",
        "desc": "bootstrap 不再用候选构建前的捕获归属校验（rollover 检测失效）",
    },
    {
        "guard": f"{GUARD}.test_guard14p_research_head_is_pinned_before_the_run_computes",
        "file": SELECTION,
        # 语法完全合法的错误顺序：pin 挪进 try 体内、_run_one 之后。Python 能正常
        # parse/import，Guard 14p 的**顺序**断言必须 FAIL（不是靠 SyntaxError）。
        "old": """            pin = _pin_research_version(item["strategy_id"])
            try:
                result = _run_one(item["model_id"], topn)""",
        "new": """            try:
                result = _run_one(item["model_id"], topn)
                pin = _pin_research_version(item["strategy_id"])""",
        "desc": "pin 移到 _run_one 之后（合法语法；静态顺序检查必须抓到）",
    },
]

FIXED_GUARDS = [
    f"{GUARD}.test_guard14l_signal_cycle_requires_a_keyword_only_explicit_cycle",
    f"{GUARD}.test_guard14m_signal_cycle_body_never_reads_paper_accounts",
    f"{GUARD}.test_guard14n_generate_signals_passes_a_frozen_explicit_cycle",
    f"{GUARD}.test_guard14o_bootstrap_signals_passes_an_explicit_cycle",
    f"{GUARD}.test_guard14p_research_head_is_pinned_before_the_run_computes",
]

#: 接线错误不是「业务断言失败」。mutant 让测试因这些原因变红时，被改的实现根本
#: 没跑到被测契约，所以计 FAKE（不算非空性证据）。
BROKEN_RE = re.compile(
    r"(SyntaxError|IndentationError|TabError"
    r"|ImportError|ModuleNotFoundError"
    r"|NameError|UnboundLocalError"
    r"|_FailedTest|AttributeError: module"
    r"|is not defined|local variable .* referenced before assignment)",
    re.MULTILINE,
)


def run(target: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="r23_guard_pycache_")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "unittest", target],
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env)


def _is_fake(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stdout or "") + (result.stderr or "")
    return bool(BROKEN_RE.search(blob))


def main() -> int:
    print("=== baseline ===")
    ok_all = True
    for name in FIXED_GUARDS:
        r = run(name)
        status = "GREEN" if r.returncode == 0 else "RED"
        if r.returncode != 0:
            ok_all = False
        print(f"  {status} {name.split('.')[-1]}")
    if not ok_all:
        print("VERDICT: baseline not green — 非空性检查没有意义")
        return 1

    print("\n=== non-vacuity ===")
    bad = []
    fake = []
    for case in CASES:
        with open(case["file"], "rb") as handle:
            original = handle.read()
        before = hashlib.sha256(original).hexdigest()
        text = original.decode("utf-8")
        if text.count(case["old"]) != 1:
            print(f'  SKIP {case["desc"]}: anchor count={text.count(case["old"])}')
            bad.append(case["guard"].split(".")[-1])
            continue
        try:
            with open(case["file"], "wb") as handle:
                handle.write(text.replace(case["old"], case["new"], 1).encode("utf-8"))
            result = run(case["guard"])
            if result.returncode == 0:
                verdict = "SURVIVED"
            elif _is_fake(result):
                verdict = "FAKE"
                fake.append(case["guard"].split(".")[-1])
            else:
                verdict = "CAUGHT"
        finally:
            with open(case["file"], "wb") as handle:
                handle.write(original)
        after = hashlib.sha256(open(case["file"], "rb").read()).hexdigest()
        assert before == after, f'{case["guard"]}: restore mismatch'
        print(f'  {verdict} {case["guard"].split(".")[-1]}  ({case["desc"]})')
        if verdict != "CAUGHT":
            bad.append(case["guard"].split(".")[-1])

    caught = len(CASES) - len(bad)
    print(f"\nGuard 14 non-vacuity: caught={caught}/{len(CASES)} "
          f"survived={len(bad) - len(fake)} fake={len(fake)}")
    print("restore sha256: PASS")
    if fake:
        print(f"FAKE（接线错误，不算非空性证据）: {fake}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
