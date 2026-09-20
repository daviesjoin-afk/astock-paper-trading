# -*- coding: utf-8 -*-
"""R18 负向变异矩阵（M-RPL1 ~ M-RPL14）。

每条变异都对应一条 R18 / 架构契约，必须让**对应**契约测试变红 —— 否则门禁是空的。

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

    M-RPL1  candidate query 恢复 today..next_day range   -> RPL-P1
    M-RPL2  删掉 signal_date<=asof                       -> RP2-04
    M-RPL3  latest review 删掉 review_date<=asof         -> RPL-P4
    M-RPL4  slot context 改回 _active_cycle()            -> RPL-P5/P6 或 Guard 9g
    M-RPL5  apply borrow 改回 _active_cycle()            -> RPL-P5
    M-RPL6  rollback borrow 改回 _active_cycle()         -> RPL-P6
    M-RPL7  donor position read 改回 current_positions   -> Guard 9h/RPL-P5
    M-RPL8  candidate score 权重改变                     -> RD-1
    M-RPL9  T+1 lock 移除                                -> RD-6
    M-RPL10 min-hold gate 移除                           -> RD-7
    M-RPL11 urgent threshold >= 改 >                     -> 阈值边界
    M-RPL12 net edge 忽略 execution buffer               -> 阈值边界
    M-RPL13 pure module reverse-import paper_trading     -> Guard 9a
    M-RPL14 tomorrow candidate 重新触发 today sell       -> RPL-P1

用法（仓库根目录）::

    python work/r18_mutation_check.py
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
PURE_MODULE = "test_paper_replacement_decision"
PROV_MODULE = "test_replacement_asof_provenance"
GUARD_MODULE = "test_paper_trading_architecture_guard"
REPLACEMENT_FILE = "backend/paper_replacement_decision.py"
EVIDENCE_FILE = "backend/paper_replacement_evidence.py"
PAPER_TRADING_FILE = "backend/paper_trading.py"


def b(text: str) -> bytes:
    return text.encode("utf-8")


def _adapt_eol(text: str, original: bytes) -> bytes:
    """把锚点里的 ``\\n`` 换成目标文件实际的行尾（``paper_trading.py`` 是 CRLF）。

    否则锚点在字节层面永远匹配不到 —— 变异会以"锚点未找到"失败，或者更糟：
    匹配到一半造成错位改写。
    """
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


#: 每次测试用一份全新的字节码缓存目录，杜绝"同长度变异复用旧 .pyc"的假阴性。
PYCACHE_ROOT = tempfile.mkdtemp(prefix="r18_mutation_pycache_")
_PYCACHE_SEQ = [0]


def run_test(target: str) -> subprocess.CompletedProcess:
    _PYCACHE_SEQ[0] += 1
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{_PYCACHE_SEQ[0]:03d}")
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, timeout=1800, env=env,
    )


MUTATIONS = [
    {
        "id": "M-RPL1",
        "file": EVIDENCE_FILE,
        # 核心业务变异：把等式改回 today..next_day range（明天的候选能卖掉今天的仓）。
        # 注意必须同时提供 next-day 参数，否则 ``>=D AND <=D`` 仍是"只有今天"，
        # 变异会退化成空门禁。
        "old": '            "   AND intended_date=? AND signal_date<=?"\n'
               '            " ORDER BY COALESCE(t_score,0) DESC,COALESCE(rank_score,0) DESC,id DESC",\n'
               '            (account_id, *active, asof, asof),\n',
        "new": '            "   AND intended_date>=? AND intended_date<=?"  # mutation\n'
               '            " ORDER BY COALESCE(t_score,0) DESC,COALESCE(rank_score,0) DESC,id DESC",\n'
               '            (account_id, *active, asof, asof[:8] + str(int(asof[8:10]) + 1).zfill(2)),\n',
        "test": f"{PROV_MODULE}.ProductionAsOfRegression."
                "test_rpl_p1_tomorrow_candidate_cannot_sell_today_holding",
        "desc": "candidate query 恢复 today..next_day range（明天候选卖掉今天持仓）",
    },
    {
        "id": "M-RPL2",
        "file": EVIDENCE_FILE,
        # 反转 asof 上界（保持参数个数不变，避免退化成 sqlite3 参数错误被 except 吞掉）。
        "old": '            "   AND intended_date=? AND signal_date<=?"\n',
        "new": '            "   AND intended_date=? AND signal_date>=?"  # mutation\n',
        "test": f"{PROV_MODULE}.CandidateEvidenceTests."
                "test_rp2_04_future_signal_date_is_excluded",
        "desc": "signal_date 上界被反转（未来 signal 被采用）",
    },
    {
        "id": "M-RPL3",
        "file": EVIDENCE_FILE,
        "old": '            " WHERE cycle_id=? AND account_id=? AND code=? AND review_date<=?"\n',
        "new": '            " WHERE cycle_id=? AND account_id=? AND code=?"\n',
        "test": f"{PROV_MODULE}.ProductionAsOfRegression."
                "test_rpl_p4_future_review_cannot_create_slot_upgrade_ready",
        "desc": "历史 review 删掉 review_date <= asof 上界（读到未来 review）",
    },
    {
        "id": "M-RPL4",
        "file": PAPER_TRADING_FILE,
        "old": '    resolved_cycle_id = int(cycle_id)\n    count_budget = _dynamic_position_limits(conn)\n',
        "new": '    resolved_cycle_id = int(_active_cycle(conn)["id"])  # mutation\n'
               '    count_budget = _dynamic_position_limits(conn)\n',
        "test": f"{GUARD_MODULE}.ReplacementIsAsOfAndCycleBound."
                "test_guard9g_slot_context_never_resolves_the_active_cycle",
        "desc": "slot context 改回 _active_cycle() 自行解析周期",
    },
    {
        "id": "M-RPL5",
        "file": PAPER_TRADING_FILE,
        "old": '    resolved_cycle_id = int(cycle_id)\n    budget = _dynamic_position_limits(conn)\n',
        "new": '    resolved_cycle_id = int(_active_cycle(conn)["id"])  # mutation\n'
               '    budget = _dynamic_position_limits(conn)\n',
        "test": f"{PROV_MODULE}.ProductionSlotLifecycleProvenance."
                "test_rpl_p5_borrow_never_touches_another_cycles_allocation",
        "desc": "apply borrow 改回 _active_cycle()（越界写别的周期席位）",
    },
    {
        "id": "M-RPL6",
        "file": PAPER_TRADING_FILE,
        "old": '    resolved_cycle_id = int(cycle_id)\n'
               '    version_text = str(borrow.get("allocation_version") or "slots-v0")\n',
        "new": '    resolved_cycle_id = int(_active_cycle(conn)["id"])  # mutation\n'
               '    version_text = str(borrow.get("allocation_version") or "slots-v0")\n',
        "test": f"{PROV_MODULE}.ProductionSlotLifecycleProvenance."
                "test_rpl_p6_rollback_never_touches_another_cycles_allocation",
        "desc": "rollback borrow 改回 _active_cycle()（越界回滚别的周期）",
    },
    {
        "id": "M-RPL7",
        "file": PAPER_TRADING_FILE,
        "old": '        1 for item in PPRM.positions_for_cycle(conn, resolved_cycle_id)\n',
        "new": '        1 for item in PPRM.current_positions(conn)  # mutation\n',
        "test": f"{PROV_MODULE}.ProductionSlotLifecycleProvenance."
                "test_rpl_p5c_donor_count_is_read_from_the_explicit_cycle",
        "desc": "donor 持仓读改回 current_positions（不再固定显式周期）",
    },
    {
        "id": "M-RPL8",
        "file": REPLACEMENT_FILE,
        "old": '        _score100(entry.get("score"), 0.0) * 0.45\n'
               '        + _score100(signal.get("t_score"), 0.0) * 0.35\n'
               '        + _score100(signal.get("rank_score"), 0.0) * 0.20,\n',
        "new": '        _score100(entry.get("score"), 0.0) * 0.50  # mutation\n'
               '        + _score100(signal.get("t_score"), 0.0) * 0.30\n'
               '        + _score100(signal.get("rank_score"), 0.0) * 0.20,\n',
        "test": f"{PURE_MODULE}.ScoringEquivalenceTests."
                "test_rd1_formula_is_entry_45_t_35_rank_20",
        "desc": "candidate score 权重被改（0.45/0.35/0.20 漂移）",
    },
    {
        "id": "M-RPL9",
        "file": REPLACEMENT_FILE,
        "old": '    if int(_num(weakest.get("available_qty"))) < policy.lot_size:\n'
               '        state = "t1_locked"\n',
        "new": '    if False:  # mutation: T+1 lock 移除\n'
               '        state = "t1_locked"\n',
        "test": f"{PURE_MODULE}.SlotUpgradeStateTests.test_rd6_t1_locked_wins_over_everything",
        "desc": "T+1 锁定被移除（强候选可绕过）",
    },
    {
        "id": "M-RPL10",
        "file": REPLACEMENT_FILE,
        "old": '    elif upgrade_ready and int(_num(weakest.get("hold_days"))) < min_hold_days and not urgent:\n',
        "new": '    elif False:  # mutation: min-hold gate 移除\n',
        "test": f"{PURE_MODULE}.SlotUpgradeStateTests.test_rd7_min_hold_produces_observe",
        "desc": "最短观察期闸门被移除",
    },
    {
        "id": "M-RPL11",
        "file": REPLACEMENT_FILE,
        "old": '    urgent = bool(\n'
               '        candidate_score >= policy.upgrade_min_candidate_score\n'
               '        and edge >= policy.upgrade_min_edge\n',
        "new": '    urgent = bool(\n'
               '        candidate_score > policy.upgrade_min_candidate_score  # mutation\n'
               '        and edge >= policy.upgrade_min_edge\n',
        "test": f"{PURE_MODULE}.ThresholdBoundaryTests."
                "test_upgrade_min_candidate_score_is_inclusive",
        "desc": "urgent 阈值 >= 被改成 >（candidate == 75 不再紧急）",
    },
    {
        "id": "M-RPL12",
        "file": REPLACEMENT_FILE,
        "old": '    net_edge = edge - policy.execution_buffer\n',
        "new": '    net_edge = edge  # mutation: 忽略执行缓冲\n',
        "test": f"{PURE_MODULE}.ThresholdBoundaryTests."
                "test_full_cap_edge_uses_net_edge_inclusive",
        "desc": "净优势忽略执行缓冲",
    },
    {
        "id": "M-RPL13",
        "file": REPLACEMENT_FILE,
        "old": 'import json\nfrom dataclasses import dataclass\n',
        "new": 'import json\nfrom dataclasses import dataclass\n\n'
               'import paper_trading  # mutation: 反向依赖\n',
        "test": f"{GUARD_MODULE}.ReplacementIsAsOfAndCycleBound."
                "test_guard9a_pure_replacement_module_has_zero_project_imports",
        "desc": "pure module 反向 import paper_trading",
    },
    {
        "id": "M-RPL14",
        "file": PAPER_TRADING_FILE,
        # adapter 层绕过：把 asof 换成"下一个工作日"，于是明天的候选又成为今天的替补。
        "old": '    rows = PREPL.load_replacement_candidates(\n'
               '        conn, account_id=account_id, asof_day=_date(day).isoformat(), statuses=statuses,\n'
               '    )\n',
        "new": '    rows = PREPL.load_replacement_candidates(  # mutation\n'
               '        conn, account_id=account_id, statuses=statuses,\n'
               '        asof_day=_next_weekday(_date(day)).isoformat(),\n'
               '    )\n',
        "test": f"{PROV_MODULE}.ProductionAsOfRegression."
                "test_rpl_p1_tomorrow_candidate_cannot_sell_today_holding",
        "desc": "adapter 把 asof 换成 next weekday（明天候选重新可卖今天持仓）",
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
            print(base.stdout[-4000:], base.stderr[-4000:])
            return 2
    print(f"BASELINE: {PURE_MODULE} + {PROV_MODULE} + {GUARD_MODULE} all green\n")

    results = []
    for mut in MUTATIONS:
        path = os.path.join(ROOT, mut["file"])
        original = open(path, "rb").read()
        before_sha = sha256(original)
        try:
            old, new = _adapt_eol(mut["old"], original), _adapt_eol(mut["new"], original)
            count = original.count(old)
            if count != 1:
                raise AssertionError(
                    f"变异锚点未找到: {mut['id']} in {mut['file']} (count={count})")
            mutated = original.replace(old, new, 1)
            if mutated == original:
                raise AssertionError(f"变异没有改变字节: {mut['id']}")
            open(path, "wb").write(mutated)

            target = mut["test"]
            method = target.rsplit(".", 1)[-1]
            proc = run_test(target.rsplit(".", 1)[0])
            combined = proc.stdout + proc.stderr
            contract_failure = (
                f"FAIL: {method}" in combined or f"ERROR: {method}" in combined
            )
            caught = proc.returncode != 0 and contract_failure
            print(f"[{mut['id']}] {mut['desc']}")
            print(f"    -> {'RED' if caught else 'SUSPECT'} "
                  f"(rc={proc.returncode}, contract_failure={contract_failure})")
            if not caught:
                print(combined[-2500:])
            results.append((mut["id"], caught, mut["desc"]))
        finally:
            open(path, "wb").write(original)
            restored = sha256(open(path, "rb").read())
            if restored != before_sha:
                print(f"!!!! {mut['id']} 还原失败：{before_sha} != {restored}")
                os._exit(3)

    print("\n===== R18 mutation summary =====")
    for mid, caught, desc in results:
        print(f"  {mid:<8} {'RED' if caught else 'SUSPECT':<8} {desc}")
    bad = [mid for mid, caught, _ in results if not caught]
    if bad:
        print(f"\nRESULT: {len(bad)} mutation(s) NOT confirmed -> DO NOT TRUST GATE")
        return 1
    print(f"\nRESULT: {len(results)}/{len(results)} mutations RED, "
          "all files restored byte-identical")
    return 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(PYCACHE_ROOT, ignore_errors=True)
    raise SystemExit(code)
