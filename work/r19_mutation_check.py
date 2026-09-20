# -*- coding: utf-8 -*-
"""R19 负向变异矩阵（M-ENT1 ~ M-ENT23）。

每条变异都对应一条 R19 契约，必须让**对应**契约测试变红 —— 否则门禁是空的。

流程（每条变异，严格逐字节备份/还原）::

    1. 读取目标文件字节 + sha256；
    2. 断言变异锚点存在（not found = 立即失败，绝不静默跳过）；
    3. 注入变异（bytes 级替换，只替换第一处）；
    4. 用仓库真实测试套件跑对应契约测试（``python -m unittest``）；
    5. 断言 RED：退出码非 0，且输出中目标方法以 FAIL:/ERROR: 出现
       （non-vacuity —— 失败必须来自对应契约，而不是 import/收集错误）；
    6. finally 逐字节还原，sha256 必须与原文件一致。

**每条注入都用独立冷字节码缓存**（``PYTHONPYCACHEPREFIX``）：CPython 判定
``.pyc`` 是否有效只看源文件 mtime（秒级）+ 字节长度，两条**字节长度相同**的
变异在同一秒内可能让第二条复用第一条的旧字节码，注入没执行却"绿" —— 假阴性。

变异矩阵::

    M-ENT1  _strategy_pool_weights 删掉 asof_day          -> EC-3
    M-ENT2  _risk_profile 删掉 asof_day（rows path）      -> EC-1
    M-ENT3  participants 改回 current active cycle        -> EC-7
    M-ENT4  cluster factors 删掉 asof_day                 -> EC-5
    M-ENT5  cluster factors 删掉 cycle_id                 -> Guard 10e
    M-ENT6  _buy_order strategy_budget 删掉 cycle/asof    -> Guard 10g
    M-ENT7  _buy_order allocation_plan 删掉 cycle/asof    -> Guard 10g
    M-ENT8  _intraday_buyback budget 删掉 cycle/asof      -> Guard 10g / EC-9
    M-ENT9  _swing_scale_in budget 删掉 cycle/asof        -> Guard 10g / EC-10
    M-ENT10 normal _buy_order 绕回 direct fill            -> SB-1 / Guard 10i
    M-ENT11 EP reservation expected_cycle_id 被删         -> Guard 10m
    M-ENT12 new reservation INSERT 改回 active_cycle_fn   -> SB-3 / Guard 10l
    M-ENT13 mismatch catch 误 release 冲突 reservation    -> SB-4 / Guard 10k
    M-ENT14 normal BUY 重新 direct INSERT paper_fills     -> Guard 10i
    M-ENT15 normal BUY 重新 direct _record_lot            -> Guard 10i
    M-ENT16 slice 1 错误把 signal 标 filled               -> SB-14
    M-ENT17 participants 改回 active cycle                 -> EC-7
    M-ENT18 adaptive allocation 删除 asof                  -> EC-3
    M-ENT19 risk profile 删除 cycle_id                     -> Guard 10c
    M-ENT20 cycle-pinned risk profile 改回 current head    -> EC-15
    M-ENT21 strict cycle resolver 改回 stamp fallback      -> EC-17
    M-ENT22 cluster DSL 改回 current runtime context       -> EC-18
    M-ENT23 runtime version fields 改回 current context    -> EC-19

用法（仓库根目录）::

    python work/r19_mutation_check.py
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
CAPITAL_MODULE = "test_entry_capital_asof"
BUY_MODULE = "test_strategy_buy_commit_convergence"
GUARD_MODULE = "test_paper_trading_architecture_guard"
PAPER_TRADING_FILE = "backend/paper_trading.py"
STRATEGY_RUNTIME_FILE = "backend/strategy_runtime.py"
RISK_ENFORCEMENT_FILE = "backend/strategy_risk_enforcement.py"
MANUAL_FILE = "backend/manual_orders.py"
RESERVATION_FILE = "backend/paper_capital_reservations.py"
PLANNER_FILE = "backend/execution_planner.py"


def _adapt_eol(text: str, original: bytes) -> bytes:
    """把锚点里的 ``\\n`` 换成目标文件实际的行尾（``paper_trading.py`` 是 CRLF）。"""
    if original.count(b"\r\n") > 0:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("utf-8")


PYCACHE_ROOT = tempfile.mkdtemp(prefix="r19_mutation_pycache_")
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
    # ── capital planning provenance ──────────────────────────────────────
    {
        "id": "M-ENT1",
        "file": PAPER_TRADING_FILE,
        "old": "                and _runtime_parameter_active(\n"
               "                    alloc.get(\"effective_date\"), asof_day=asof_day,\n"
               "                    status=alloc.get(\"status\"))):\n",
        "new": "                and _runtime_parameter_active(\n"
               "                    alloc.get(\"effective_date\"),\n"
               "                    status=alloc.get(\"status\"))):  # mutation\n",
        "test": f"{CAPITAL_MODULE}.AdaptiveAllocationIsAsOfBound."
                "test_ec3_future_adaptive_allocation_is_ignored",
        "desc": "_strategy_pool_weights 删除 asof_day 传递（未来权重改写历史）",
    },
    {
        "id": "M-ENT2",
        "file": PAPER_TRADING_FILE,
        "old": "    profiles = {\n"
               "        row.get(\"id\"): _risk_profile(row, asof_day=asof_day, "
               "conn=conn, cycle_id=cycle_id)\n"
               "        for row in rows if row.get(\"id\")\n"
               "    }\n",
        "new": "    profiles = {\n"
               "        row.get(\"id\"): _risk_profile(row, conn=conn, cycle_id=cycle_id)"
               "  # mutation\n"
               "        for row in rows if row.get(\"id\")\n"
               "    }\n",
        "test": f"{CAPITAL_MODULE}.AdaptiveRiskIsAsOfBound."
                "test_ec1_future_adaptive_risk_is_ignored",
        "desc": "_risk_profile 的 rows path 删除 asof_day（未来 risk overlay 泄漏）",
    },
    {
        "id": "M-ENT3",
        "file": PAPER_TRADING_FILE,
        "old": "    rows = rows if rows is not None else _shared_account_rows(conn, cycle_id)\n",
        "new": "    rows = rows if rows is not None else _shared_account_rows(conn)  # mutation\n",
        "test": f"{CAPITAL_MODULE}.ParticipantRowsFollowTheCycle."
                "test_ec7_participants_come_from_the_explicit_cycle_only",
        "desc": "参与者账户改回 current active cycle",
    },
    {
        "id": "M-ENT4",
        "file": PAPER_TRADING_FILE,
        "old": "    clusters, cluster_factors = _strategy_cluster_factors(\n"
               "        conn, asof_day, account_ids=list(weights), cycle_id=cycle_id,\n"
               "    )\n",
        "new": "    clusters, cluster_factors = _strategy_cluster_factors(\n"
               "        conn, account_ids=list(weights), cycle_id=cycle_id,  # mutation\n"
               "    )\n",
        "test": f"{CAPITAL_MODULE}.ClusterEvidenceIsAsOfBound."
                "test_ec5_future_cluster_signal_is_ignored",
        "desc": "cluster factors 删除 as-of（未来 signal 改变历史簇结构）",
    },
    {
        "id": "M-ENT5",
        "file": PAPER_TRADING_FILE,
        "old": "    clusters, cluster_factors = _strategy_cluster_factors(\n"
               "        conn, asof_day, account_ids=list(weights), cycle_id=cycle_id,\n"
               "    )\n",
        "new": "    clusters, cluster_factors = _strategy_cluster_factors(\n"
               "        conn, asof_day, account_ids=list(weights),  # mutation\n"
               "    )\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10e_cluster_evidence_follows_cycle_and_asof",
        "desc": "cluster factors 删除 cycle_id（簇证据跨周期）",
    },
    {
        "id": "M-ENT6",
        "file": PAPER_TRADING_FILE,
        "old": "    strategy_budget = _strategy_pool_budget(\n"
               "        conn, account, nav, positions, all_quotes, market=market,\n"
               "        cycle_id=current_cycle[\"id\"], asof_day=asof_day,\n"
               "    )\n",
        "new": "    strategy_budget = _strategy_pool_budget(\n"
               "        conn, account, nav, positions, all_quotes, market=market,  # mutation\n"
               "    )\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10g_production_buy_callers_pass_cycle_and_asof",
        "desc": "_buy_order 的 strategy_budget 删除 cycle/as-of",
    },
    {
        "id": "M-ENT7",
        "file": PAPER_TRADING_FILE,
        "old": "            account=account,\n"
               "            cycle_id=current_cycle[\"id\"], asof_day=asof_day,\n"
               "        )\n",
        "new": "            account=account,  # mutation\n"
               "        )\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10g_production_buy_callers_pass_cycle_and_asof",
        "desc": "_buy_order 的 allocation_plan 删除 cycle/as-of",
    },
    {
        "id": "M-ENT8",
        "file": PAPER_TRADING_FILE,
        "old": "    strategy_budget = _strategy_pool_budget(\n"
               "        conn, account, nav, shared_positions, quotes, market=market,\n"
               "        cycle_id=cycle[\"id\"], asof_day=asof_day,\n"
               "    )\n",
        "new": "    strategy_budget = _strategy_pool_budget(\n"
               "        conn, account, nav, shared_positions, quotes, market=market,  # mutation\n"
               "    )\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10g_production_buy_callers_pass_cycle_and_asof",
        "desc": "_intraday_buyback 的预算删除 cycle/as-of",
    },
    {
        "id": "M-ENT9",
        "file": PAPER_TRADING_FILE,
        "old": "    strategy_budget = _strategy_pool_budget(\n"
               "        conn, account, nav, positions, quotes, market=market,\n"
               "        cycle_id=cycle[\"id\"], asof_day=asof_day,\n"
               "    )\n",
        "new": "    strategy_budget = _strategy_pool_budget(\n"
               "        conn, account, nav, positions, quotes, market=market,  # mutation\n"
               "    )\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10g_production_buy_callers_pass_cycle_and_asof",
        "desc": "_swing_scale_in 的预算删除 cycle/as-of",
    },
    # ── commit 收敛 ───────────────────────────────────────────────────────
    {
        "id": "M-ENT10",
        "file": PAPER_TRADING_FILE,
        # 必须整体替换整个调用：只替换首行会把参数留在原地变成语法错误，
        # 于是失败来自 import 而不是契约（假阴性）。
        "old": "    failure = MO.commit_strategy_entry_fill(\n"
               "        conn, account=account, signal=signal, quote=quote, payload=payload,\n"
               "        slice_state=slice_state, asof_day=asof_day, order_id=order_id,\n"
               "        plan={\"code\": code, \"name\": signal.get(\"name\"),\n"
               "              \"industry\": signal.get(\"industry\"), \"qty\": qty,\n"
               "              \"fill_price\": fill_price, \"amount\": amount, \"fees\": fees},\n"
               "        decision_name=decision_name, reason=reason, risk=risk,\n"
               "        cycle_id=current_cycle[\"id\"],\n"
               "    )\n",
        "new": "    failure = None  # mutation: bypass the planner primitive\n",
        "test": f"{BUY_MODULE}.NormalBuyConvergence."
                "test_normal_buy_goes_through_the_planner_and_keeps_parity",
        "desc": "普通 _buy_order 绕过 EP.commit_fill（改回 direct fill 路径）",
    },
    {
        "id": "M-ENT11",
        "file": PLANNER_FILE,
        "old": "            ok, reserve_reason = PT._reserve_shared_capital(\n"
               "                conn, order_id, account_id, code, amount, fees,\n"
               "                expected_cycle_id=order_cycle_id,\n"
               "            )\n",
        "new": "            ok, reserve_reason = PT._reserve_shared_capital(\n"
               "                conn, order_id, account_id, code, amount, fees,  # mutation\n"
               "            )\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10m_production_reservations_carry_the_order_cycle",
        "desc": "execution_planner 的预占删除 expected_cycle_id",
    },
    {
        "id": "M-ENT12",
        "file": RESERVATION_FILE,
        # 注意目标必须是 **Guard 10l**（源码契约），不能是 SB-3：SB-3 的 fixture
        # 里预占行已经存在，``reserve_shared_capital`` 走的是 UPDATE 分支，
        # INSERT 分支根本不会执行 —— 用它当目标会变成空门禁。
        "old": "    if expected_cycle_id is not None:\n"
               "        reservation_cycle_id = int(expected_cycle_id)\n"
               "    else:\n"
               "        reservation_cycle_id = int(active_cycle_fn(conn)[\"id\"])\n",
        "new": "    reservation_cycle_id = int(active_cycle_fn(conn)[\"id\"])  # mutation\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10l_new_reservation_uses_the_expected_order_cycle",
        "desc": "新预占 INSERT 改回 active_cycle_fn（订单/预占周期被拆开）",
    },
    {
        "id": "M-ENT13",
        "file": MANUAL_FILE,
        "old": "        _risk_log(conn, account[\"id\"], code, \"buy\", \"risk_rejected\", conflict, risk)\n"
               "        return {\n"
               "            \"filled\": False, \"deferred\": True, \"status\": \"risk_rejected\",\n",
        "new": "        _finish_capital_reservation(conn, order_id, \"released\")  # mutation\n"
               "        _risk_log(conn, account[\"id\"], code, \"buy\", \"risk_rejected\", conflict, risk)\n"
               "        return {\n"
               "            \"filled\": False, \"deferred\": True, \"status\": \"risk_rejected\",\n",
        "test": f"{BUY_MODULE}.WrongCycleReservationFailsClosed."
                "test_wrong_cycle_reservation_is_rejected_and_untouched",
        "desc": "周期冲突时误 release 冲突的 reservation",
    },
    {
        "id": "M-ENT14",
        "file": PAPER_TRADING_FILE,
        # 注入在 ``_buy_order`` 内（Guard 10i 的作用域）且是**语法合法**的死代码：
        # 变异本身不改变运行行为，只改变源码形状 —— 这正是源码级门禁要钉的东西。
        "old": "    if failure is not None:\n        return failure\n",
        "new": "    if failure is not None:\n        return failure\n"
               "    conn.execute(\"INSERT INTO paper_fills(order_id,account_id,side,code,\"\n"
               "                 \"qty,price,amount,fees,fill_date,quote_at,assumption) \"\n"
               "                 \"VALUES(?,?,?,?,?,?,?,?,?,?,?)\",  # mutation\n"
               "                 (order_id, account[\"id\"], \"buy\", code, 0, 0.0, 0.0,\n"
               "                  0.0, \"1970-01-01\", None, \"mutation\"))\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10i_normal_buy_order_has_no_direct_ledger_writes",
        "desc": "normal _buy_order 重新 direct INSERT paper_fills",
    },
    {
        "id": "M-ENT15",
        "file": PAPER_TRADING_FILE,
        "old": "    if failure is not None:\n        return failure\n",
        "new": "    if failure is not None:\n        return failure\n"
               "    _record_lot(conn, account, signal, 0, 0.0, asof_day, order_id)  # mutation\n",
        "test": f"{GUARD_MODULE}.EntryCapitalPlanningIsBounded."
                "test_guard10i_normal_buy_order_has_no_direct_ledger_writes",
        "desc": "normal _buy_order 重新 direct _record_lot",
    },
    {
        "id": "M-ENT16",
        "file": MANUAL_FILE,
        "old": "            if not slice_done:\n",
        "new": "            if False:  # mutation: 中间片也把 signal 标 filled\n",
        "test": f"{BUY_MODULE}.SliceSemanticsPreserved."
                "test_sb14_intermediate_slice_stays_deferred",
        "desc": "第 1 片成交后错误把 signal 标 filled（剩余片丢失）",
    },
    # ── 审查反馈：三条 P2 的回归钉 ────────────────────────────────────────
    {
        "id": "M-ENT17",
        "file": PAPER_TRADING_FILE,
        "old": "    if not rows and cycle_id is None:\n",
        "new": "    if not rows:  # mutation: 显式周期也注入调用方账户\n",
        "test": f"{CAPITAL_MODULE}.ExplicitEmptyCycleHasNoCapital."
                "test_ec13_idle_cycle_does_not_fall_back_to_the_caller_account",
        "desc": "显式 idle 周期重新注入调用方账户（零策略周期凭空有预算）",
    },
    {
        "id": "M-ENT18",
        "file": PAPER_TRADING_FILE,
        "old": "    elif conn is not None and SRE.compiled_profile_is_asof_provable("
               "conn, account_id, asof_day):\n",
        "new": "    elif conn is not None:  # mutation: 无条件融合当前版本编译画像\n",
        "test": f"{CAPITAL_MODULE}.CompiledProfileIsAsOfBound."
                "test_ec12_future_strategy_version_does_not_change_history",
        "desc": "编译风险画像无条件融合（回放日之后创建的版本改写历史）",
    },
    {
        "id": "M-ENT19",
        "file": MANUAL_FILE,
        "old": "    foreign_reservation = _is_reservation_cycle_mismatch("
               "exc, ReservationCycleMismatch)\n"
               "    detail[\"reservation_released\"] = not foreign_reservation\n"
               "    if not foreign_reservation:\n",
        "new": "    foreign_reservation = False  # mutation: 无条件释放冲突预占\n"
               "    detail[\"reservation_released\"] = True\n"
               "    if True:\n",
        "test": f"{CAPITAL_MODULE}.ForeignReservationIsNeverReleased."
                "test_ec14_terminalizer_skips_release_for_a_foreign_reservation",
        "desc": "手动终态化路径无条件 release 周期冲突的预占",
    },
    {
        "id": "M-ENT20",
        "file": PAPER_TRADING_FILE,
        "old": "    if conn is not None and cycle_id is not None:\n"
               "        compiled = SRE.compiled_profile_for_cycle("
               "conn, account_id, cycle_id=cycle_id)\n"
               "    elif conn is not None and SRE.compiled_profile_is_asof_provable("
               "conn, account_id, asof_day):\n"
               "        compiled = SRE.compiled_profile_for(conn, account_id)\n",
        "new": "    if conn is not None and SRE.compiled_profile_is_asof_provable("
               "conn, account_id, asof_day):\n"
               "        compiled = SRE.compiled_profile_for(conn, account_id)"
               "  # mutation: current/latest head\n",
        "test": f"{CAPITAL_MODULE}.CyclePinnedStrategyVersionIsUsed."
                "test_ec15_cycle_pinned_version_beats_a_later_current_head",
        "desc": "cycle-pinned 版本查询退回 current/latest head",
    },
    {
        "id": "M-ENT21",
        "file": RISK_ENFORCEMENT_FILE,
        "old": "        record = SR.cycle_version_for_account(\n"
               "            conn, str(account_id), cycle_id=int(cycle_id))\n"
               "        if record is None:\n"
               "            return composite_compiled_profile()\n"
               "        return _compiled_from_version(record)\n",
        "new": "        strategy_id, version, checksum = SR.stamp_for_account(\n"
               "            conn, str(account_id), cycle_id=int(cycle_id))\n"
               "        record = SR.get_version(\n"
               "            str(strategy_id), int(version), checksum=str(checksum), conn=conn)\n"
               "        if record is None:\n"
               "            return composite_compiled_profile()\n"
               "        return _compiled_from_version(record)\n",
        "test": f"{CAPITAL_MODULE}.CyclePinnedStrategyVersionIsUsed."
                "test_ec17_missing_cycle_pin_never_falls_back_to_current_head",
        "desc": "strict cycle resolver 退回 legacy/current-head stamp fallback",
    },
    {
        "id": "M-ENT22",
        "file": PAPER_TRADING_FILE,
        "old": "                if cycle_id is None:\n"
               "                    dsl_ast = SRT.get_context(conn, account_id).compiled_dsl\n"
               "                else:\n"
               "                    dsl_ast = SRE.compiled_dsl_for_cycle(\n"
               "                        conn, account_id, cycle_id=cycle_id)\n",
        "new": "                dsl_ast = SRT.get_context(conn, account_id).compiled_dsl\n"
               "                # mutation: current head DSL\n",
        "test": f"{CAPITAL_MODULE}.CyclePinnedStrategyVersionIsUsed."
                "test_ec18_cluster_dsl_uses_cycle_pinned_version",
        "desc": "cluster DSL 退回 current runtime context",
    },
    {
        "id": "M-ENT23",
        "file": STRATEGY_RUNTIME_FILE,
        "old": "            profile = (profiles or {}).get(account_id) or {}\n"
               "            compiled_audit = profile.get(\"compiled_risk_profile\") or {}\n",
        "new": "            context_runtime = get_context(conn, account_id).allocation_runtime\n"
               "            profile = {\"max_exposure\": context_runtime.own_exposure_cap_pct}\n"
               "            compiled_audit = {\"max_positions\": context_runtime.max_positions}\n",
        "test": f"{CAPITAL_MODULE}.CyclePinnedStrategyVersionIsUsed."
                "test_ec19_runtime_cap_uses_cycle_pinned_version",
        "desc": "runtime 版本派生字段退回 current strategy context",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print(f"repo root: {ROOT}")
    for module in (CAPITAL_MODULE, BUY_MODULE, GUARD_MODULE):
        base = run_test(module)
        if base.returncode != 0:
            print(f"BASELINE FAILED —— {module} 必须全绿，终止。")
            print(base.stdout[-4000:], base.stderr[-4000:])
            return 2
    print(f"BASELINE: {CAPITAL_MODULE} + {BUY_MODULE} + {GUARD_MODULE} all green\n")

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

    print("\n===== R19 mutation summary =====")
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
