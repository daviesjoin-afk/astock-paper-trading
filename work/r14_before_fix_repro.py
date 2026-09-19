# -*- coding: utf-8 -*-
"""R14 before-fix reproduction：旧周期 peak_price / take_stage 泄漏进新周期并真实改变卖出决策。

在**未修改**的生产代码（baseline ``4cb871c``）上确定性复现四个事实：

* R14-C1  旧周期 mirror 的 ``peak_price`` / ``take_stage`` 进入当前周期的持仓读数；
* R14-C2  旧 peak 真实改变 trailing-stop 决策（exit_class none -> trailing_stop）；
* R14-C3  旧 take_stage 真实压制新周期的阶梯止盈（sell_ratio > 0 -> 0）；
* R14-C4  same-cycle full exit -> re-entry 的 episode 继承（若基线恰好被
          ``_sync_positions`` 的全量重建救掉，则如实报告 NOT REPRODUCIBLE）。

全部使用临时 SQLite + 真实生产 schema（``PT.init_db``）+ 真实生产读模型与
``_sell_plan``；行情/输入全部打桩（``_completed_kline`` → None），不访问
``data_cache``、不访问网络、不触碰真实账本。确定性阈值一律走 ``spec_override``，
**不改任何生产风险参数**。

注意：本脚本是 **before-fix 证据**，断言的是*缺陷存在*。修复合入后它会在
R14-C1 处按预期失败（peak=成本锚、take_stage=None，旧镜像被忽略）—— 这正是
before/after 证明。修复后的永久回归契约由 ``backend/test_position_risk_state.py``
（PRS-1..15）持有。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
from unittest import mock

BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "backend"))
sys.path.insert(0, BACKEND)

import paper_position_read_model as PPRM  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
ASOF = dt.date(2026, 9, 17)

# 只覆盖确定性阈值（不改生产参数语义：hard_stop/trail 与账户默认一致，
# take_profit 抬到不触发的位置、hold_max 放宽，使断言只反映 peak/take_stage 的影响）。
SPEC_C2 = {"hard_stop": -0.05, "trail_after": 0.04, "trail_stop": 0.05,
           "take_profit": [(0.50, 0.30)], "hold_max": 8}
SPEC_C3 = {"hard_stop": -0.05, "trail_after": 0.04, "trail_stop": 0.05,
           "take_profit": [(0.05, 0.30), (0.10, 0.30)], "hold_max": 8}


def _ledger():
    """真实生产 schema 的临时账本（与 test_authoritative_position_consumers 同范式）。"""
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "paper.sqlite3")
    patches = [
        mock.patch.object(PT, "DB_PATH", path),
        mock.patch.object(PT, "_benchmark_close", return_value=None),
        mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        mock.patch.object(PT, "_completed_kline", return_value=None),
    ]
    try:
        for patcher in patches:
            patcher.start()
        PT.init_db()
    except Exception:
        for patcher in reversed(patches):
            patcher.stop()
        tmp.cleanup()
        raise
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return tmp, patches, conn


def _done(tmp, patches, conn):
    conn.close()
    for patcher in reversed(patches):
        patcher.stop()
    tmp.cleanup()


def _cycle(conn, key, status):
    cur = conn.execute(
        "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
        "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
        (key, status, 1000000.0, "shared_pool", "2026-09-01 09:30:00",
         "2026-09-01 09:30:00", "2026-09-01 09:30:00"))
    return int(cur.lastrowid)


def _lot(conn, cycle_id, remaining_qty, cost, acquired_at="2026-09-15 09:31:00"):
    conn.execute(
        "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
        "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
        "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, ACCOUNT, CODE, "贵州茅台", "白酒", 100, int(remaining_qty), float(cost),
         acquired_at, "2026-09-16", "stock_t1", None, 1, 1))


def _mirror(conn, peak_price, take_stage):
    """旧周期 / 旧 episode 留在 paper_positions 的镜像行（无 cycle_id，无法证明归属）。"""
    conn.execute(
        "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
        "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ACCOUNT, CODE, "贵州茅台", "白酒", 100, 100.0, "2026-09-10", "2026-09-11",
         "stock_t1", float(peak_price), int(take_stage)))


def _sell(conn, quote, spec):
    rows = PPRM.current_positions(conn)
    assert len(rows) == 1, "expected exactly one lot-backed position, got %r" % (rows,)
    ratio, reason, next_stage, detail = PT._sell_plan(
        rows[0], quote, ASOF, {}, spec_override=spec)
    return ratio, reason, next_stage, detail


def case_c1():
    """R14-C1：cycle 8 的 peak/take_stage 进入 cycle 9 的持仓读数。"""
    tmp, patches, conn = _ledger()
    try:
        c8 = _cycle(conn, "r14-cycle-8", "ended")
        c9 = _cycle(conn, "r14-cycle-9", "running")
        assert c8 < c9, "cycle identity ordering broken"
        # cycle 9 的权威 lot（qty/cost 的唯一事实来源）；cycle 8 不再 active。
        _lot(conn, c9, 100, 100.0)
        # 无周期身份的镜像行（升级前/旧周期遗留）。
        _mirror(conn, 130.0, 2)
        conn.commit()
        rows = PPRM.current_positions(conn)
        assert len(rows) == 1, rows
        item = rows[0]
        ok_qty = item["qty"] == 100
        ok_cost = abs(item["cost"] - 100.0) < 1e-9
        leaked_peak = item["peak_price"]
        leaked_stage = item["take_stage"]
        reproduced = leaked_peak == 130.0 and leaked_stage == 2
        print("R14-C1  qty=%s (want 100, from lot)  cost=%s (want 100, from lot)"
              % (item["qty"], item["cost"]))
        print("R14-C1  peak_price=%s  take_stage=%s  (lot episode says fresh: "
              "peak=cost=100 / stage=0)" % (leaked_peak, leaked_stage))
        assert ok_qty and ok_cost, "lot authority unexpectedly broken: %r" % (item,)
        assert reproduced, (
            "R14-C1 NOT REPRODUCED: stale mirror did not leak "
            "(peak=%r stage=%r)" % (leaked_peak, leaked_stage))
        print("R14-C1 REPRODUCED: stale cycle-8 mirror entered the cycle-9 position read")
        return True
    finally:
        _done(tmp, patches, conn)


def case_c2():
    """R14-C2：同一 lot、同一 quote，仅 stale peak 使 none -> trailing_stop。"""
    # 世界 A（stale）：cycle 8 mirror peak=130 残留。
    tmp, patches, conn = _ledger()
    try:
        c9 = _cycle(conn, "r14-cycle-9", "running")
        _lot(conn, c9, 100, 100.0)
        _mirror(conn, 130.0, 0)
        conn.commit()
        s_ratio, s_reason, _stage, s_detail = _sell(
            conn, {"price": 110.0, "high": 110.0}, SPEC_C2)
    finally:
        _done(tmp, patches, conn)
    # 世界 B（fresh）：全新周期 episode，无任何镜像（读模型默认 peak=cost=100）。
    tmp, patches, conn = _ledger()
    try:
        c9 = _cycle(conn, "r14-cycle-9", "running")
        _lot(conn, c9, 100, 100.0)
        conn.commit()
        f_ratio, f_reason, _stage, f_detail = _sell(
            conn, {"price": 110.0, "high": 110.0}, SPEC_C2)
    finally:
        _done(tmp, patches, conn)

    print("R14-C2  fresh   : ratio=%s exit=%s drawdown=%s%%  (%s)"
          % (f_ratio, f_detail["exit_class"], f_detail["drawdown_pct"], f_reason or "-"))
    print("R14-C2  stale   : ratio=%s exit=%s drawdown=%s%%  (%s)"
          % (s_ratio, s_detail["exit_class"], s_detail["drawdown_pct"], s_reason))
    print("R14-C2  same cycle-9 lot (cost=100), same quote (price=high=110), "
          "only the mirror peak differs (130 vs none)")
    assert f_ratio == 0.0 and f_detail["exit_class"] == "none", (
        "fresh world unexpectedly sells: %r" % (f_detail,))
    assert s_ratio > 0 and s_detail["exit_class"] == "trailing_stop", (
        "stale peak did not create a trailing stop: %r" % (s_detail,))
    print("R14-C2 REPRODUCED: stale peak alone flips exit none -> trailing_stop "
          "(sell_ratio 0 -> %s)" % s_ratio)
    return True


def case_c3():
    """R14-C3：同一 lot、同一 quote，仅 stale take_stage 压制新周期止盈。"""
    # 世界 A（stale）：take_stage=2 —— 两档都被认为已消费。
    tmp, patches, conn = _ledger()
    try:
        c9 = _cycle(conn, "r14-cycle-9", "running")
        _lot(conn, c9, 100, 100.0)
        _mirror(conn, 106.0, 2)  # peak=106 保持 drawdown=0，隔离 take_stage 变量
        conn.commit()
        s_ratio, _reason, s_stage, s_detail = _sell(
            conn, {"price": 106.0, "high": 106.0}, SPEC_C3)
    finally:
        _done(tmp, patches, conn)
    # 世界 B（fresh）：新 episode，stage 0 —— 应消费第一档。
    tmp, patches, conn = _ledger()
    try:
        c9 = _cycle(conn, "r14-cycle-9", "running")
        _lot(conn, c9, 100, 100.0)
        conn.commit()
        f_ratio, _reason, f_stage, f_detail = _sell(
            conn, {"price": 106.0, "high": 106.0}, SPEC_C3)
    finally:
        _done(tmp, patches, conn)

    print("R14-C3  fresh (stage 0): ratio=%s next_stage=%s exit=%s"
          % (f_ratio, f_stage, f_detail["exit_class"]))
    print("R14-C3  stale (stage 2): ratio=%s next_stage=%s exit=%s"
          % (s_ratio, s_stage, s_detail["exit_class"]))
    print("R14-C3  same cycle-9 lot (cost=100), same quote (price=high=106, ret=+6%), "
          "take_profit=[(0.05,0.30),(0.10,0.30)]")
    assert f_ratio > 0 and f_stage == 1, (
        "fresh world did not consume stage 0: ratio=%r stage=%r" % (f_ratio, f_stage))
    assert s_ratio == 0.0 and s_stage == 2, (
        "stale stage did not suppress the take profit: ratio=%r stage=%r"
        % (s_ratio, s_stage))
    print("R14-C3 REPRODUCED: stale take_stage suppresses the new cycle's tactical "
          "take profit (sell_ratio %s -> 0)" % f_ratio)
    return True


def case_c4():
    """R14-C4：same-cycle full exit -> re-entry 的 episode 继承。"""
    tmp, patches, conn = _ledger()
    try:
        c9 = _cycle(conn, "r14-cycle-9", "running")
        # episode A：建仓 100 股后**全部卖出**（remaining_qty=0）。
        _lot(conn, c9, 0, 100.0)
        # episode A 的风险状态仍留在无 episode 身份的镜像里。
        _mirror(conn, 130.0, 1)
        conn.commit()
        held = PPRM.current_positions(conn)
        assert held == [], "a fully exited lot must not be reported as held: %r" % (held,)
        # 生产生命周期：_sync_positions 全量重建（DELETE-then-INSERT）——
        # 全清仓后镜像行随 lot 聚合一起消失（基线的"意外自救"）。
        PT._sync_positions(conn, ACCOUNT)
        # episode B：同一周期同账户同代码重新买入（0 -> >0）。
        _lot(conn, c9, 100, 110.0, acquired_at="2026-09-17 09:31:00")
        conn.commit()
        rows = PPRM.current_positions(conn)
        assert len(rows) == 1, rows
        item = rows[0]
        fresh_peak = item["peak_price"]
        fresh_stage = item["take_stage"]
        production_path_resets = abs(fresh_peak - 110.0) < 1e-9 and fresh_stage == 0
        # 权威层暴露：镜像行本身没有 episode 身份（PK=account_id+code），
        # 只要同一 key 上残留 episode A 的行，读模型就会原样继承。
        _mirror(conn, 130.0, 2)
        conn.commit()
        rows2 = PPRM.current_positions(conn)
        inherited = (rows2[0]["peak_price"] == 130.0 and rows2[0]["take_stage"] == 2)
        print("R14-C4  production path after full exit + _sync_positions: "
              "peak=%s stage=%s -> %s" % (
                  fresh_peak, fresh_stage,
                  "fresh (rescued by the incidental DELETE-then-INSERT rebuild)"
                  if production_path_resets else "INHERITED"))
        print("R14-C4  same key with a leftover episode-A mirror row: "
              "peak=%s stage=%s -> %s" % (
                  rows2[0]["peak_price"], rows2[0]["take_stage"],
                  "INHERITED (the read model has no episode identity)" if inherited
                  else "fresh"))
        assert production_path_resets, (
            "production lifecycle unexpectedly leaks episode state: %r" % (item,))
        assert inherited, "expected the read model to be unable to distinguish episodes"
        print("R14-C4 NOT REPRODUCIBLE ON BASE: the end-to-end production path is "
              "rescued by _sync_positions' incidental full rebuild, not by any "
              "episode identity. The read model itself provably cannot distinguish "
              "position episodes (same (account_id, code) key inherits any leftover "
              "row), so the new cycle-owned design must own the episode reset "
              "(PRS-9 / PRS-10).")
        return True
    finally:
        _done(tmp, patches, conn)


def main():
    print("R14 before-fix reproduction (unmodified production code, temp ledger only)")
    case_c1()
    case_c2()
    case_c3()
    case_c4()
    print("\nSUMMARY")
    print("  R14-C1 stale peak crosses cycle:              REPRODUCED")
    print("  R14-C2 stale peak changes trailing-stop:      REPRODUCED")
    print("  R14-C3 stale take_stage changes take-profit:  REPRODUCED")
    print("  R14-C4 same-cycle re-entry inheritance:       NOT REPRODUCIBLE ON BASE")
    print("      (authority-level exposure documented; new design must own the reset)")


if __name__ == "__main__":
    main()
