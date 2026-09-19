# -*- coding: utf-8 -*-
"""R14 负向变异矩阵（M-PRS1~10）。

流程（每条变异，严格逐字节备份/还原）::

    1. 读取目标文件字节 + sha256；
    2. 断言变异锚点存在（not found = 立即失败，绝不静默跳过）；
    3. 注入变异（bytes 级替换，只替换第一处）；
    4. 用仓库真实测试套件跑对应契约测试（``python -m unittest``）；
    5. 断言 RED：退出码非 0，且输出中目标测试以 FAIL:/ERROR: 出现
       （non-vacuity —— 失败必须来自对应风险契约，而不是 import/收集错误）；
    6. finally 逐字节还原，sha256 必须与原文件一致。

变异矩阵（每条都对应一个 PRS 风险契约）::

    M-PRS1  读模型回落 paper_positions 投影           -> PRS-1
    M-PRS2  缺失 state 时 peak 不锚定成本             -> PRS-2
    M-PRS3  未知 take_stage 被当成已知                -> PRS-3
    M-PRS4  peak 写入跨周期（WHERE 丢 cycle_id）      -> PRS-5
    M-PRS5  delete 原语丢 WHERE（跨周期全表擦除）     -> PRS-5c
    M-PRS6  full exit 分支永不删除状态                -> PRS-7
    M-PRS7  加仓重置 episode（peak/stage 全丢）       -> PRS-8
    M-PRS8  re-entry 继承残留行（REPLACE->IGNORE）    -> PRS-9b
    M-PRS9  读路径创建状态行（纯读不变量被破坏）      -> PRS-10
    M-PRS10 v20 守卫不安装（cycle 必填/不可变丢失）   -> migration

用法（仓库根目录）::

    .venv/Scripts/python.exe work/r14_mutation_check.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
TEST_MODULE = "test_position_risk_state"


def b(text: str) -> bytes:
    return text.encode("utf-8")


def inject_variants(text: str):
    """兼容 LF / CRLF：返回 (old, new) 两种换行风格的候选对。"""
    pairs = [(text, text)]
    if "\n" in text:
        pairs.append((text.replace("\n", "\r\n"), text.replace("\n", "\r\n")))
    return pairs


MUTATIONS = [
    {
        "id": "M-PRS1",
        "file": "backend/paper_position_read_model.py",
        "old": '"SELECT * FROM paper_position_risk_state WHERE cycle_id=?"',
        "new": '"SELECT account_id,code,peak_price,take_stage FROM paper_positions'
               ' WHERE ? IS NOT NULL"',
        "test": f"{TEST_MODULE}.CycleOwnedReadPath."
                "test_PRS1_stale_cycle_state_does_not_enter_current_positions",
        "desc": "读模型回落 paper_positions 投影（投影重获执行权威）",
    },
    {
        "id": "M-PRS2",
        "file": "backend/paper_portfolio.py",
        "old": 'item["peak_price"] = item["cost"]',
        "new": 'item["peak_price"] = item["cost"] * 1.375',
        "test": f"{TEST_MODULE}.StalePeakCannotDriveTrailingStop."
                "test_PRS2_missing_state_anchors_peak_to_cost_no_trailing_stop",
        "desc": "缺失 state 时 peak 不锚定成本（凭空抬高峰值）",
    },
    {
        "id": "M-PRS3",
        "file": "backend/paper_trading.py",
        "old": "stage_known = raw_stage is not None",
        "new": "stage_known = True",
        "test": f"{TEST_MODULE}.StaleTakeStageCannotDriveTakeProfit."
                "test_PRS3_missing_state_skips_staged_take_profit",
        "desc": "未知 take_stage 被当成已知（猜档位多卖）",
    },
    {
        "id": "M-PRS4",
        "file": "backend/paper_trading.py",
        "old": '    conn.execute(\n'
               '        "UPDATE paper_position_risk_state SET peak_price=MAX(peak_price,?),updated_at=?"\n'
               '        " WHERE cycle_id=? AND account_id=? AND code=?",\n'
               '        (float(peak_price), _now(), cycle_id, account_id, code),\n'
               '    )\n',
        "new": '    conn.execute(\n'
               '        "UPDATE paper_position_risk_state SET peak_price=MAX(peak_price,?),updated_at=?"\n'
               '        " WHERE account_id=? AND code=?",\n'
               '        (float(peak_price), _now(), account_id, code),\n'
               '    )\n',
        "test": f"{TEST_MODULE}.WritesAreCycleScoped."
                "test_PRS5_peak_and_stage_writes_only_touch_their_cycle",
        "desc": "peak 写入跨周期（WHERE 丢 cycle_id）",
    },
    {
        "id": "M-PRS5",
        "file": "backend/paper_trading.py",
        "old": '    conn.execute(\n'
               '        "DELETE FROM paper_position_risk_state WHERE cycle_id=? AND account_id=? AND code=?",\n'
               '        (cycle_id, account_id, code),\n'
               '    )\n',
        "new": '    conn.execute(\n'
               '        "DELETE FROM paper_position_risk_state",\n'
               '    )\n',
        "test": f"{TEST_MODULE}.WritesAreCycleScoped."
                "test_PRS5c_delete_is_cycle_scoped",
        "desc": "delete 原语丢 WHERE（跨周期全表擦除）",
    },
    {
        "id": "M-PRS6",
        "file": "backend/paper_trading.py",
        "old": "if position_closed:",
        "new": "if position_closed and False:",
        "test": f"{TEST_MODULE}.EpisodeLifecycle."
                "test_PRS7_full_exit_clears_state_via_sell_finalization",
        "desc": "full exit 分支永不删除状态（episode 无法关闭）",
    },
    {
        "id": "M-PRS7",
        "file": "backend/paper_trading.py",
        "old": "    if prior_qty <= 0:",
        "new": "    if True or prior_qty <= 0:",
        "test": f"{TEST_MODULE}.EpisodeLifecycle."
                "test_PRS8_add_on_preserves_stage_and_raises_peak",
        "desc": "加仓被当成新 episode（stage 重置 / episode 起点重写）",
    },
    {
        "id": "M-PRS8",
        "file": "backend/paper_trading.py",
        "old": "INSERT OR REPLACE INTO paper_position_risk_state(",
        "new": "INSERT OR IGNORE INTO paper_position_risk_state(",
        "test": f"{TEST_MODULE}.EpisodeLifecycle."
                "test_PRS9b_init_replaces_any_leftover_stale_row",
        "desc": "re-entry 继承残留行（防御性 REPLACE 被弱化）",
    },
    {
        "id": "M-PRS9",
        "file": "backend/paper_position_read_model.py",
        "old": "    return PP.aggregate_positions(",
        "new": "    for l in lots:\n"
               "        conn.execute(\n"
               '            "INSERT OR IGNORE INTO paper_position_risk_state(cycle_id,account_id,code,"\n'
               '            "peak_price,take_stage,initialized_at,updated_at)"\n'
               '            " VALUES(?,?,?,0,0,\'1970-01-01 00:00:00\',\'1970-01-01 00:00:00\')",\n'
               '            (cycle_id, l["account_id"], l["code"]),\n'
               "        )\n"
               "    return PP.aggregate_positions(",
        "test": f"{TEST_MODULE}.ReadPathIsPureRead."
                "test_PRS10_current_positions_never_creates_state",
        "desc": "读路径创建状态行（纯读不变量被破坏）",
    },
    {
        "id": "M-PRS10",
        "file": "backend/paper_schema_migrations.py",
        "old": "    _ensure_position_risk_state_guards(conn)\n    return changes\n",
        "new": "    return changes\n",
        "test": f"{TEST_MODULE}.V20Migration."
                "test_migration_state_row_requires_real_cycle",
        "desc": "v20 守卫不安装（cycle 必填 / 归属不可变丢失）",
    },
]


def run_test(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, timeout=300,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print(f"repo root: {ROOT}")
    base = run_test(TEST_MODULE)
    if base.returncode != 0:
        print("BASELINE FAILED —— 变异前测试套件必须全绿，终止。")
        print(base.stdout[-2000:], base.stderr[-2000:])
        return 2
    print(f"BASELINE: {TEST_MODULE} all green\n")

    results = []
    for mut in MUTATIONS:
        path = os.path.join(ROOT, mut["file"])
        original = open(path, "rb").read()
        original_hash = sha256(original)
        try:
            # 构造字节级 old/new（old 必须原样存在；new 与 old 同换行风格）。
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
            restored = open(path, "rb").read()
            assert sha256(restored) == original_hash, \
                f"还原后 sha256 不一致: {mut['file']}"

    print("\n===== R14 mutation summary =====")
    failed = [r for r in results if r[2] != "RED"]
    for mid, desc, status, _test in results:
        print(f"  {mid:<8} {status:<8} {desc}")
    if failed:
        print(f"\nRESULT: {len(failed)} mutation(s) NOT confirmed -> DO NOT TRUST GATE")
        return 1
    print(f"\nRESULT: {len(results)}/{len(results)} mutations RED, all files restored byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
