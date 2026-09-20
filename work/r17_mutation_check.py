# -*- coding: utf-8 -*-
"""R17 负向变异矩阵（M-PR1 ~ M-PR15）。

每条变异都对应一条 R17 / 架构契约，必须让**对应**契约测试变红 —— 否则门禁是空的。

流程（每条变异，严格逐字节备份/还原）::

    1. 读取目标文件字节 + sha256；
    2. 断言变异锚点存在（not found = 立即失败，绝不静默跳过）；
    3. 注入变异（bytes 级替换，只替换第一处）；
    4. 用仓库真实测试套件跑对应契约测试（``python -m unittest``）；
    5. 断言 RED：退出码非 0，且输出中目标测试以 FAIL:/ERROR: 出现
       （non-vacuity —— 失败必须来自对应契约，而不是 import/收集错误）；
    6. finally 逐字节还原，sha256 必须与原文件一致。

**每次注入都必须用独立冷字节码缓存**（``PYTHONPYCACHEPREFIX``）：CPython 判定
``.pyc`` 是否有效只看源文件 mtime（秒级）+ 字节长度，两条**字节长度相同**的变异
在同一秒内可能让第二条复用第一条的旧字节码，注入没执行却"绿" —— 假阴性。

变异矩阵::

    M-PR1   resolver 改回 latest account+code signal    -> RISK-REVIEW-P1
    M-PR2   resolver 不验证 order.cycle_id              -> RP-04
    M-PR3   resolver 不验证 execution_verified          -> RP-07
    M-PR4   resolver 不验证 order account/code          -> RP-05
    M-PR5   resolver 不检查 signal_date <= asof         -> RISK-REVIEW-P2
    M-PR6   missing opened_order_id 时回落 latest       -> RISK-REVIEW-P3
    M-PR7   signal 身份不匹配仍被接受                   -> RP-11
    M-PR8   新仓使用 raw trend 而非中性 50              -> PR-02
    M-PR9   T+1 锁定仍允许集中换仓                      -> PR-05
    M-PR10  trend_pullback 确认闸门被移除               -> PR-09
    M-PR11  score == 淘汰线 的 <= 改成 <                -> 阈值边界
    M-PR12  paper_position_review 反向 import           -> Guard 8a
    M-PR13  _save_position_review 缺 review_date 时兜底 -> Guard 8h
    M-PR14  归档后的 episode signal 不再解析            -> RP-15
    M-PR15  signal 身份校验（含归档行）被移除           -> RP-16

用法（仓库根目录）::

    python work/r17_mutation_check.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
PURE_MODULE = "test_paper_position_review"
PROV_MODULE = "test_position_review_provenance"
GUARD_MODULE = "test_paper_trading_architecture_guard"
REVIEW_FILE = "backend/paper_position_review.py"
EVIDENCE_FILE = "backend/paper_position_review_evidence.py"
PAPER_TRADING_FILE = "backend/paper_trading.py"


def b(text: str) -> bytes:
    return text.encode("utf-8")


#: 每次测试用一份全新的字节码缓存目录，杜绝"同长度变异复用旧 .pyc"的假阴性。
PYCACHE_ROOT = tempfile.mkdtemp(prefix="r17_mutation_pycache_")
_PYCACHE_SEQ = [0]


def run_test(target: str) -> subprocess.CompletedProcess:
    _PYCACHE_SEQ[0] += 1
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{_PYCACHE_SEQ[0]:03d}")
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, timeout=600, env=env,
    )


MUTATIONS = [
    {
        "id": "M-PR1",
        "file": EVIDENCE_FILE,
        # 核心业务变异：把 opened_order_id → 精确 signal 变回"最近一条 signal"。
        "old": '    signal, signal_source = _load_signal(conn, signal_id)\n'
               '    if signal is None:\n'
               '        return _unknown("signal_not_found", opened_order_id=order_id, signal_id=signal_id)\n',
        "new": '    signal = conn.execute(\n'
               '        "SELECT id,account_id,code,signal_date,rank_score,t_score,payload"\n'
               '        "  FROM paper_signals WHERE account_id=? AND code=?"\n'
               '        " ORDER BY signal_date DESC,id DESC LIMIT 1",\n'
               '        (expected_account, expected_code),\n'
               '    ).fetchone()  # mutation: 最近一条 signal\n'
               '    signal_source = "paper_signals"\n'
               '    if signal is None:\n'
               '        return _unknown("signal_not_found", opened_order_id=order_id, signal_id=signal_id)\n',
        "test": f"{PROV_MODULE}.ProductionProvenanceRegression."
                "test_risk_review_p1_later_signal_does_not_change_model_score",
        "desc": "resolver 改回 latest account+code signal（episode provenance 丢失）",
    },
    {
        "id": "M-PR2",
        "file": EVIDENCE_FILE,
        "old": '    order_cycle = _row_value(order, "cycle_id")\n'
               '    if order_cycle is None or int(order_cycle) != cycle_id:\n',
        "new": '    order_cycle = _row_value(order, "cycle_id")\n'
               '    if False:  # mutation: 不验证 order.cycle_id\n',
        "test": f"{PROV_MODULE}.ResolverContractTests.test_rp04_wrong_cycle_order_rejected",
        "desc": "resolver 不验证 order.cycle_id（可解析到别的周期的订单）",
    },
    {
        "id": "M-PR3",
        "file": EVIDENCE_FILE,
        "old": '    if not EV.is_verified_row(order):\n',
        "new": '    if False:  # mutation: 不验证 execution_verified\n',
        "test": f"{PROV_MODULE}.ResolverContractTests.test_rp07_unverified_order_rejected",
        "desc": "resolver 不验证 order execution_verified（未证明成交也算建仓证据）",
    },
    {
        "id": "M-PR4",
        "file": EVIDENCE_FILE,
        "old": '    if (str(_row_value(order, "account_id") or "") != expected_account\n'
               '            or str(_row_value(order, "code") or "") != expected_code):\n',
        "new": '    if False:  # mutation: 不验证 order account/code\n',
        "test": f"{PROV_MODULE}.ResolverContractTests.test_rp05_wrong_account_order_rejected",
        "desc": "resolver 不验证 order account/code（身份错配被接受）",
    },
    {
        "id": "M-PR5",
        "file": EVIDENCE_FILE,
        # 未来 signal 泄漏：去掉 asof 上界。
        "old": '    if not signal_date or not asof or signal_date > asof:\n',
        "new": '    if False:  # mutation: 不检查 signal_date <= asof\n',
        "test": f"{PROV_MODULE}.ProductionProvenanceRegression."
                "test_risk_review_p2b_episode_signal_after_asof_stays_unknown",
        "desc": "resolver 不检查 signal_date <= asof（未来 signal 泄漏）",
    },
    {
        "id": "M-PR6",
        "file": EVIDENCE_FILE,
        "old": '    if opened_order_id is None or str(opened_order_id).strip() == "":\n'
               '        return _unknown("missing_opened_order_id")\n',
        # missing provenance 时回落 latest：unknown 被洗成 latest guess。
        "new": '    if opened_order_id is None or str(opened_order_id).strip() == "":\n'
               '        _latest = conn.execute(\n'
               '            "SELECT id,account_id,code,signal_date,rank_score,t_score,payload"\n'
               '            "  FROM paper_signals WHERE account_id=? AND code=?"\n'
               '            " ORDER BY signal_date DESC,id DESC LIMIT 1",\n'
               '            (expected_account, expected_code)).fetchone()\n'
               '        if _latest is None:\n'
               '            return _unknown("missing_opened_order_id")\n'
               '        return {"status": "verified", "reason": None, "opened_order_id": None,\n'
               '                "signal_id": _row_value(_latest, "id"),\n'
               '                "signal_date": _normalize_day(_row_value(_latest, "signal_date")),\n'
               '                "signal": dict(_latest), "provenance_version": ENTRY_PROVENANCE_VERSION}\n',
        "test": f"{PROV_MODULE}.ProductionProvenanceRegression."
                "test_risk_review_p3b_unknown_is_not_latest_guess",
        "desc": "missing opened_order_id 时回落 latest signal（unknown 被洗白）",
    },
    {
        "id": "M-PR7",
        "file": EVIDENCE_FILE,
        "old": '    if (str(_row_value(signal, "account_id") or "") != expected_account\n'
               '            or str(_row_value(signal, "code") or "") != expected_code):\n'
               '        return _unknown("signal_identity_mismatch", opened_order_id=order_id,\n'
               '                        signal_id=signal_id)\n',
        "new": '    # mutation: signal 身份不匹配仍被接受\n',
        "test": f"{PROV_MODULE}.ResolverContractTests."
                "test_rp11_signal_identity_mismatch_is_unknown",
        "desc": "signal account/code 不匹配仍被接受",
    },
    {
        "id": "M-PR8",
        "file": REVIEW_FILE,
        "old": '    trend_for_score = 50.0 if hold_days < 1 else _num(trend_score, 50.0)\n',
        "new": '    trend_for_score = _num(trend_score, 50.0)  # mutation: 新仓不中性化\n',
        "test": f"{PURE_MODULE}.ScoringEquivalenceTests."
                "test_pr02_new_position_neutralises_trend",
        "desc": "新仓使用 raw trend 而不是中性 50（新仓评分漂移）",
    },
    {
        "id": "M-PR9",
        "file": REVIEW_FILE,
        "old": '    if int(position.get("available_qty") or 0) < pf.lot_size:\n'
               '        return "t1_locked", "T+1 可卖份额不足，等待可卖后再评估"\n',
        "new": '    # mutation: T+1 锁定仍允许集中换仓\n',
        "test": f"{PURE_MODULE}.ConcentrationDecisionTests.test_pr05_t1_lock_blocks_rotation",
        "desc": "T+1 锁定仍允许 concentration exit",
    },
    {
        "id": "M-PR10",
        "file": REVIEW_FILE,
        "old": '    if position.get("account_id") == "trend_pullback" and score <= pf.exit_score:\n'
               '        if not bool(review.get("quality_exit_confirmed")):\n'
               '            return "watch", (\n'
               '                f"趋势持仓评分 {score:.1f} 低于淘汰线，但尚无连续观察确认；"\n'
               '                "保留观察，等待下一完整窗口或结构破坏"\n'
               '            )\n',
        "new": '    # mutation: trend_pullback 确认闸门被移除\n',
        "test": f"{PURE_MODULE}.ConcentrationDecisionTests."
                "test_pr09_trend_pullback_needs_confirmation",
        "desc": "trend_pullback 连续观察确认闸门被移除",
    },
    {
        "id": "M-PR11",
        "file": REVIEW_FILE,
        "old": '    if score <= pf.exit_score:\n'
               '        return "consolidation_exit", f"持仓质量评分 {score:.1f} 低于淘汰线 {pf.exit_score:.0f}"\n',
        "new": '    if score < pf.exit_score:  # mutation: <= 变 <\n'
               '        return "consolidation_exit", f"持仓质量评分 {score:.1f} 低于淘汰线 {pf.exit_score:.0f}"\n',
        "test": f"{PURE_MODULE}.ThresholdBoundaryTests.test_exit_threshold_is_inclusive",
        "desc": "淘汰线边界 <= 被改成 <（score == 38 不再淘汰）",
    },
    {
        "id": "M-PR12",
        "file": REVIEW_FILE,
        "old": 'from dataclasses import dataclass\nfrom typing import Any\n',
        "new": 'from dataclasses import dataclass\nfrom typing import Any\n\n'
               'import paper_trading  # mutation: 反向依赖\n',
        "test": f"{GUARD_MODULE}.PositionReviewIsProvenanceBound."
                "test_guard8a_pure_review_module_has_zero_project_imports",
        "desc": "paper_position_review 反向 import paper_trading",
    },
    {
        "id": "M-PR13",
        "file": PAPER_TRADING_FILE,
        "old": '    review_date = review.get("review_date")\n'
               '    if review_date is None or str(review_date).strip() == "":\n'
               '        raise ValueError("_save_position_review 需要显式 review_date（不允许 wall-clock 回退）")\n',
        # 恢复 wall-clock 兜底：历史 as-of 复核日期会被机器今天顶掉。
        "new": '    review_date = review.get("review_date") or dt.date.today()\n',
        "test": f"{GUARD_MODULE}.PositionReviewIsProvenanceBound."
                "test_guard8h_save_position_review_has_no_wall_clock_fallback",
        "desc": "_save_position_review 缺 review_date 时回落机器今天",
    },
    {
        "id": "M-PR14",
        "file": EVIDENCE_FILE,
        "old": 'SIGNAL_SOURCES = ("paper_signals", "paper_signals_archive")\n',
        "new": 'SIGNAL_SOURCES = ("paper_signals",)  # mutation: 归档后的 episode signal 失去 provenance\n',
        "test": f"{PROV_MODULE}.ResolverContractTests."
                "test_rp15_archived_entry_signal_still_resolves_by_exact_id",
        "desc": "分批建仓归档后的 entry signal 不再被解析（真实分数退化成中性 50）",
    },
    {
        "id": "M-PR15",
        "file": EVIDENCE_FILE,
        "old": '    if (str(_row_value(signal, "account_id") or "") != expected_account\n'
               '            or str(_row_value(signal, "code") or "") != expected_code):\n'
               '        return _unknown("signal_identity_mismatch", opened_order_id=order_id,\n'
               '                        signal_id=signal_id)\n',
        "new": '    # mutation: 归档/活跃 signal 的身份不再校验\n',
        "test": f"{PROV_MODULE}.ResolverContractTests."
                "test_rp16_archived_signal_keeps_identity_and_asof_checks",
        "desc": "signal 身份不匹配（含归档行）仍被接受为 provenance",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print(f"repo root: {ROOT}")
    for module in (PURE_MODULE, PROV_MODULE, GUARD_MODULE):
        base = run_test(module)
        if base.returncode != 0:
            print(f"BASELINE FAILED —— {module} 必须全绿，终止。")
            print(base.stdout[-2000:], base.stderr[-2000:])
            return 2
    print(f"BASELINE: {PURE_MODULE} + {PROV_MODULE} + {GUARD_MODULE} all green\n")

    results = []
    for mut in MUTATIONS:
        path = os.path.join(ROOT, mut["file"])
        with open(path, "rb") as fh:
            original = fh.read()
        original_hash = sha256(original)
        try:
            candidates = []
            for style in ("\n", "\r\n"):
                if style == "\r\n" and b"\r\n" not in original:
                    continue
                old_b = b(mut["old"].replace("\n", style))
                if old_b in original:
                    candidates.append((old_b, b(mut["new"].replace("\n", style))))
            if not candidates:
                raise AssertionError(f"变异锚点未找到: {mut['id']} in {mut['file']}")
            old_b, new_b = candidates[0]
            injected = original.replace(old_b, new_b, 1)
            if injected == original:
                raise AssertionError(f"变异未产生任何字节变化: {mut['id']}")
            with open(path, "wb") as fh:
                fh.write(injected)

            proc = run_test(mut["test"])
            output = (proc.stdout or "") + (proc.stderr or "")
            red = proc.returncode != 0
            method = mut["test"].split(".")[-1]
            marker = f"FAIL: {method}" in output or f"ERROR: {method}" in output
            status = "RED" if (red and marker) else "SUSPECT"
            results.append((mut["id"], mut["desc"], status, mut["test"]))
            print(f"[{mut['id']}] {mut['desc']}")
            print(f"    -> {status} (rc={proc.returncode}, contract_failure={marker})")
        finally:
            with open(path, "wb") as fh:
                fh.write(original)
            with open(path, "rb") as fh:
                restored = fh.read()
            assert sha256(restored) == original_hash, f"还原后 sha256 不一致: {mut['file']}"

    print("\n===== R17 mutation summary =====")
    failed = [r for r in results if r[2] != "RED"]
    for mid, desc, status, _test in results:
        print(f"  {mid:<8} {status:<8} {desc}")
    if failed:
        print(f"\nRESULT: {len(failed)} mutation(s) NOT confirmed -> DO NOT TRUST GATE")
        return 1
    print(f"\nRESULT: {len(results)}/{len(results)} mutations RED, "
          "all files restored byte-identical")
    return 0


if __name__ == "__main__":
    try:
        _code = main()
    finally:
        shutil.rmtree(PYCACHE_ROOT, ignore_errors=True)
    raise SystemExit(_code)
