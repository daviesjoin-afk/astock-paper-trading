# -*- coding: utf-8 -*-
"""R20 负向变异矩阵（M-EXE1 ~ M-EXE18）。

每条变异都对应一条 R20 契约，必须让对应契约测试变红 —— 否则门禁是空的。

流程（每条变异，严格逐字节备份/还原）::

    1. 读取目标文件字节 + sha256；
    2. 断言变异锚点存在（not found = 立即失败，绝不静默跳过）；
    3. 注入变异（bytes 级替换，只替换第一处）；
    4. 用仓库真实测试套件跑对应契约测试；
    5. 断言 RED：退出码非 0，且输出中目标方法以 FAIL:/ERROR: 出现；
    6. finally 逐字节还原，sha256 必须与原文件一致。

用法（仓库根目录）::

    python work/r20_mutation_check.py
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
SELL_MODULE = "test_sell_fill_commit_convergence"
GUARD_MODULE = "test_paper_trading_architecture_guard"
PAPER_TRADING_FILE = "backend/paper_trading.py"
PLANNER_FILE = "backend/execution_planner.py"


def _adapt_eol(text: str, original: bytes) -> bytes:
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r20_mutation_pycache_")
_PYCACHE_SEQ = [0]


def run_test(target: str) -> subprocess.CompletedProcess:
    _PYCACHE_SEQ[0] += 1
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{_PYCACHE_SEQ[0]:03d}")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800, env=env,
    )


MUTATIONS = [
    {
        "id": "M-EXE1",
        "file": PAPER_TRADING_FILE,
        "old": "                EP.commit_fill(\n",
        "new": "                _r20_disabled_commit(  # mutation: no commit\n",
        "test": f"{SELL_MODULE}.RiskSellCommitConvergence."
                "test_sf1_risk_sell_commits_through_execution_planner",
        "desc": "risk SELL 不再经过 EP.commit_fill",
    },
    {
        "id": "M-EXE2",
        "file": PAPER_TRADING_FILE,
        "old": "        pnl = EP.commit_fill(\n",
        "new": "        pnl = _r20_disabled_commit(  # mutation: no commit\n",
        "test": f"{SELL_MODULE}.IntradaySellCommitConvergence."
                "test_sf2_intraday_sell_commits_through_execution_planner",
        "desc": "intraday SELL 不再经过 EP.commit_fill",
    },
    {
        "id": "M-EXE3",
        "file": PLANNER_FILE,
        "old": "        consumed, cost_amount = PT._consume_available_lots(\n"
               "            conn, account_id, code, qty, asof_day, cycle_id=order_cycle_id,\n"
               "        )\n",
        "new": "        consumed, cost_amount = qty, 0.0  # mutation: no FIFO consumption\n",
        "test": f"{SELL_MODULE}.DirectSellCommitConvergence."
                "test_sf8_realized_pnl_formula_unchanged",
        "desc": "commit_fill SELL 删除 lot consumption",
    },
    {
        "id": "M-EXE4",
        "file": PLANNER_FILE,
        "old": "            conn, account_id, code, qty, asof_day, cycle_id=order_cycle_id,\n",
        "new": "            conn, account_id, code, qty, asof_day,  # mutation: drop cycle\n",
        "test": f"{SELL_MODULE}.DirectSellCommitConvergence."
                "test_sf9_sell_consumes_the_order_cycle_only",
        "desc": "commit_fill SELL lot consume 去掉 cycle_id",
    },
    {
        "id": "M-EXE5",
        "file": PLANNER_FILE,
        "old": "    if not provenance.is_proven:\n"
               "        raise PT.OrderCycleProvenanceUnknown(\n"
               "            order_id, provenance.status,\n"
               "            f\"{side} 成交被拒绝：订单周期归属不可证明\",\n"
               "        )\n",
        "new": "    if not provenance.is_proven:\n"
               "        provenance = PT.OrderCycleProvenance(\n"
               "            True, True, PT._active_cycle_id_readonly(conn), PT.ORDER_CYCLE_PROVEN,\n"
               "        )  # mutation: fallback to active cycle\n",
        "test": f"{SELL_MODULE}.DirectSellCommitConvergence."
                "test_sf9b_unknown_order_cycle_fails_closed",
        "desc": "删除 order provenance guard（unknown 回退 active cycle）",
    },
    {
        "id": "M-EXE6",
        "file": PLANNER_FILE,
        "old": "    _assert_order_identity(conn, order_id=order_id, account_id=account_id,\n"
               "                           code=code, side=side)\n",
        "new": "    pass  # mutation: order identity guard removed\n",
        "test": f"{SELL_MODULE}.DirectSellCommitConvergence."
                "test_sf10_order_identity_mismatch_fails_closed",
        "desc": "删除 order identity guard",
    },
    {
        "id": "M-EXE7",
        "file": PLANNER_FILE,
        "old": "        PT._credit_shared_cash(conn, amount - fees, account_id)\n",
        "new": "        pass  # mutation: no sell cash credit\n",
        "test": f"{SELL_MODULE}.RiskSellCommitConvergence."
                "test_sf1_risk_sell_commits_through_execution_planner",
        "desc": "删除 SELL cash credit",
    },
    {
        "id": "M-EXE8",
        "file": PLANNER_FILE,
        "old": "        PPRS.finalize_sell(\n"
               "            conn, cycle_id=order_cycle_id, account_id=account_id, code=code,\n"
               "            next_take_stage=sell_next_take_stage,\n"
               "        )\n",
        "new": "        pass  # mutation: no episode finalize\n",
        "test": f"{SELL_MODULE}.RiskSellCommitConvergence."
                "test_sf5_risk_full_sell_closes_episode",
        "desc": "删除 PPRS.finalize_sell",
    },
    {
        "id": "M-EXE9",
        "file": PLANNER_FILE,
        "old": "            next_take_stage=sell_next_take_stage,\n",
        "new": "            next_take_stage=None,  # mutation: drop stage fact\n",
        "test": f"{SELL_MODULE}.RiskSellCommitConvergence."
                "test_sf4_risk_partial_sell_advances_take_stage",
        "desc": "丢掉 sell_next_take_stage",
    },
    {
        "id": "M-EXE10",
        "file": PLANNER_FILE,
        "old": "    conn.execute(\n"
               "        \"\"\"INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption)\n"
               "           VALUES(?,?,?,?,?,?,?,?,?,?,?)\"\"\",\n"
               "        (order_id, account_id, side, code, qty, fill_price, amount, fees,\n"
               "         PT._date(asof_day).isoformat(), plan.get(\"quote_at\"), assumption),\n"
               "    )\n",
        "new": "    pass  # mutation: no paper_fills INSERT\n",
        "test": f"{SELL_MODULE}.RiskSellCommitConvergence."
                "test_sf1_risk_sell_commits_through_execution_planner",
        "desc": "删除 paper_fills INSERT",
    },
    {
        "id": "M-EXE11",
        "file": PLANNER_FILE,
        "old": "    EV.stamp_order(conn, order_id)\n",
        "new": "    pass  # mutation: no execution verification stamp\n",
        "test": f"{SELL_MODULE}.RiskSellCommitConvergence."
                "test_sf1_risk_sell_commits_through_execution_planner",
        "desc": "删除 EV.stamp_order",
    },
    {
        "id": "M-EXE12",
        "file": PAPER_TRADING_FILE,
        "old": "            savepoint = f\"risk_pos_{position['account_id']}_{position['code']}\"\n",
        "new": "            conn.execute(\"INSERT INTO paper_fills(order_id) VALUES(0)\")  # mutation\n"
               "            savepoint = f\"risk_pos_{position['account_id']}_{position['code']}\"\n",
        "test": f"{GUARD_MODULE}.SellFillCommitConvergenceIsBounded."
                "test_guard11a_risk_sell_delegates_to_commit_fill",
        "desc": "risk SELL 重新直接 INSERT paper_fills",
    },
    {
        "id": "M-EXE13",
        "file": PAPER_TRADING_FILE,
        "old": "    savepoint = f\"intraday_sell_{account['id']}_{position['code']}\"\n",
        "new": "    conn.execute(\"INSERT INTO paper_fills(order_id) VALUES(0)\")  # mutation\n"
               "    savepoint = f\"intraday_sell_{account['id']}_{position['code']}\"\n",
        "test": f"{GUARD_MODULE}.SellFillCommitConvergenceIsBounded."
                "test_guard11b_intraday_sell_delegates_to_commit_fill",
        "desc": "intraday SELL 重新直接 INSERT paper_fills",
    },
    {
        "id": "M-EXE14",
        "file": PAPER_TRADING_FILE,
        "old": "        conn.execute(f\"ROLLBACK TO SAVEPOINT {savepoint}\")\n"
               "        conn.execute(f\"RELEASE SAVEPOINT {savepoint}\")\n"
               "        if _lease_lost(exc):\n"
               "            raise\n"
               "        return None, f\"高抛执行失败，可重试：{type(exc).__name__}: {exc}\"\n",
        "new": "        pass  # mutation: no rollback\n"
               "        conn.execute(f\"RELEASE SAVEPOINT {savepoint}\")\n"
               "        if _lease_lost(exc):\n"
               "            raise\n"
               "        return None, f\"高抛执行失败，可重试：{type(exc).__name__}: {exc}\"\n",
        "test": f"{SELL_MODULE}.IntradaySellCommitConvergence."
                "test_sf11_intraday_commit_failure_rolls_back_atomically",
        "desc": "失败路径取消 SAVEPOINT rollback",
    },
    {
        "id": "M-EXE15",
        "file": PAPER_TRADING_FILE,
        "old": "            sell_next_take_stage=None,\n"
               "        )\n"
               "        conn.execute(f\"RELEASE SAVEPOINT {savepoint}\")\n",
        "new": "            sell_next_take_stage=None,\n"
               "        )\n"
               "        _risk_log(conn, account[\"id\"], position[\"code\"], \"sell\", audit_action, \"dup\", payload)\n"
               "        conn.execute(f\"RELEASE SAVEPOINT {savepoint}\")\n",
        "test": f"{SELL_MODULE}.IntradaySellCommitConvergence."
                "test_sf12_intraday_log_and_audit_exactly_once",
        "desc": "caller 成功后重复 _risk_log",
    },
    {
        "id": "M-EXE16",
        "file": PAPER_TRADING_FILE,
        "old": "            sell_next_take_stage=None,\n"
               "        )\n"
               "        conn.execute(f\"RELEASE SAVEPOINT {savepoint}\")\n",
        "new": "            sell_next_take_stage=None,\n"
               "        )\n"
               "        _audit(conn, account[\"id\"], audit_action, \"dup\")\n"
               "        conn.execute(f\"RELEASE SAVEPOINT {savepoint}\")\n",
        "test": f"{SELL_MODULE}.IntradaySellCommitConvergence."
                "test_sf12_intraday_log_and_audit_exactly_once",
        "desc": "caller 成功后重复 _audit",
    },
    {
        "id": "M-EXE17",
        "file": PLANNER_FILE,
        "old": "        realized_pnl = amount - cost_amount - fees\n",
        "new": "        realized_pnl = amount - cost_amount + fees  # mutation\n",
        "test": f"{SELL_MODULE}.DirectSellCommitConvergence."
                "test_sf8_realized_pnl_formula_unchanged",
        "desc": "realized_pnl 改成 amount-cost+fees",
    },
    {
        "id": "M-EXE18",
        "file": PAPER_TRADING_FILE,
        "old": "            sell_next_take_stage=None,\n",
        "new": "            sell_next_take_stage=0,  # mutation: reset partial stage\n",
        "test": f"{SELL_MODULE}.IntradaySellCommitConvergence."
                "test_sf6_intraday_partial_sell_preserves_take_stage",
        "desc": "partial intraday SELL 错误重置 take_stage",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print(f"repo root: {ROOT}")
    for module in (SELL_MODULE, GUARD_MODULE):
        base = run_test(module)
        if base.returncode != 0:
            print(f"BASELINE FAILED —— {module} 必须全绿，终止。")
            print(base.stdout[-5000:], base.stderr[-5000:])
            return 2
    print(f"BASELINE: {SELL_MODULE} + {GUARD_MODULE} all green\n")

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
                print(combined[-3000:])
            results.append((mut["id"], caught, mut["desc"]))
        finally:
            open(path, "wb").write(original)
            restored = sha256(open(path, "rb").read())
            if restored != before_sha:
                print(f"!!!! {mut['id']} 还原失败：{before_sha} != {restored}")
                os._exit(3)

    print("\n===== R20 mutation summary =====")
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
