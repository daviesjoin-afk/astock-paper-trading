# -*- coding: utf-8 -*-
"""R14 Round-2 before-fix 复现探针（Blocker A：full exit 未闭环）。

Round-1 的 PR #170 把 risk-state 清理只写进了风控扫描分支，评审指出另外两条
生产 SELL 路径同样能把最后一股权威 lot 卖光：

    R14-R2-C1  ``execution_planner.commit_fill`` SELL（manual / deferred 委托）
                卖光 100 股之后 ``paper_position_lots`` 归零，但
                ``paper_position_risk_state`` 行仍然存在 → episode 未结束。

    R14-R2-C2  ``_intraday_sell``（日内高抛）
                ``available == LOT_SIZE == 100`` ⇒ ``qty = max(100, int(100*0.3/100)*100)
                = 100``，即"一手仓的高抛就是整仓清空"；同样残留状态行。

两个探针都**驱动真实生产代码**（``EP.commit_fill`` / ``PT._intraday_sell``），
不手工调用任何 delete 原语 —— 要复现的正是"生产链路没有做这件事"。
risk-state 行用 SQL 直接落库（夹具），因此本脚本在修复前/修复后的两个 revision
上都能运行（不依赖 ``init_position_risk_state`` / ``initialize_episode`` 任一 API）。

预期::

    修复前（base 94dee6b）：两个探针都 REPRODUCED
    修复后            ：两个探针都 NOT REPRODUCED

用法（仓库根目录；会自建临时 SQLite，绝不碰生产库）::

    .venv/Scripts/python.exe work/r14_round2_before_fix_repro.py
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_planner as EP  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600901"
NAME = "测试股"
DAY = dt.date(2026, 9, 5)
SELL_PRICE = 13.0


def _seed_state_row(conn, cycle_id, code, peak_price, take_stage=1):
    """夹具：直接写一条同周期运行时风险状态行（版本无关）。"""
    conn.execute(
        "INSERT OR REPLACE INTO paper_position_risk_state(cycle_id,account_id,code,"
        "peak_price,take_stage,opened_order_id,initialized_at,updated_at)"
        " VALUES(?,?,?,?,?,NULL,?,?)",
        (cycle_id, ACCOUNT, code, peak_price, take_stage,
         "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
    )
    conn.commit()


def _state_present(conn, cycle_id, code):
    return conn.execute(
        "SELECT COUNT(*) FROM paper_position_risk_state"
        " WHERE cycle_id=? AND account_id=? AND code=?",
        (cycle_id, ACCOUNT, code),
    ).fetchone()[0] > 0


def _remaining(conn, cycle_id, code):
    return int(conn.execute(
        "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
        " WHERE cycle_id=? AND account_id=? AND code=?",
        (cycle_id, ACCOUNT, code),
    ).fetchone()[0])


def probe_execution_planner_full_sell():
    """R14-R2-C1：``EP.commit_fill`` SELL 卖光全部 lot 后的 episode 收尾。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = os.path.join(tmp.name, "paper.sqlite3")
        with mock.patch.object(PT, "DB_PATH", path), \
                mock.patch.object(PT, "_benchmark_close", return_value=None), \
                mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
            PT.init_db()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cycle = int(conn.execute(
                "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,"
                "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
                "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cycle, ACCOUNT, CODE, NAME, "测试", 100, 100, 10.0,
                 "2026-09-01 10:00:00", "2026-09-02", "stock_t1", 1, 1),
            )
            _seed_state_row(conn, cycle, CODE, 12.0, take_stage=1)
            stamp = PT._strategy_stamp(conn, ACCOUNT)
            cur = conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "status,reason,risk_payload,created_at,origin,strategy_id,"
                "strategy_version,strategy_checksum,cycle_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ACCOUNT, "sell", CODE, NAME, 100, SELL_PRICE, "pending_limit",
                 "manual_sell", "{}", "2026-09-05 10:00:00", "manual", *stamp, cycle),
            )
            order = int(cur.lastrowid)
            conn.commit()

            with mock.patch.object(PT, "_completed_kline", return_value=None):
                EP.commit_fill(
                    conn, account={"id": ACCOUNT},
                    plan={"side": "sell", "code": CODE, "qty": 100,
                          "fill_price": SELL_PRICE, "amount": 100 * SELL_PRICE,
                          "fees": 5.0, "quote_at": None, "risk": {}},
                    order_id=order, asof_day=DAY, reserved=True,
                    action="manual_filled", reason="探针：手动全仓卖出",
                )
            conn.commit()

            remaining = _remaining(conn, cycle, CODE)
            state = _state_present(conn, cycle, CODE)
            reproduced = remaining == 0 and state
            print(f"  R14-R2-C1 execution_planner full SELL: remaining_lots={remaining}"
                  f" risk_state_present={state}")
            return reproduced
        finally:
            conn.close()
    finally:
        tmp.cleanup()


def probe_intraday_full_sell():
    """R14-R2-C2：``_intraday_sell`` 在 available==100 时的高抛整仓清空。"""
    import test_paper_risk_exit_production_path as RISK

    inner = RISK.TestPaperRiskExitProductionPath(
        "test_A_normal_running_account_full_risk_exit_pipeline")
    inner.setUp()
    conn = None
    try:
        conn = sqlite3.connect(PT.DB_PATH)
        conn.row_factory = sqlite3.Row
        inner._insert_lot(ACCOUNT, inner.code, 100, 10.0)
        conn.execute("UPDATE paper_accounts SET mode='intraday_t' WHERE id=?", (ACCOUNT,))
        cycle = int(conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0])
        _seed_state_row(conn, cycle, inner.code, 10.0, take_stage=0)
        inner.quotes_map[inner.code] = {
            "code": inner.code, "name": f"测试股_{inner.code}", "price": 11.0,
            "high": 11.5, "low": 10.8, "pct": 0.0, "prev_close": 11.0,
            "amount": 100000.0, "volume": 10000.0, "turnover": 1.0,
            "quote_source": "live",
            "quote_at": f"{inner.day.isoformat()} 10:30:00",
            "quote_validation": "cross_source_checked",
        }
        account = dict(conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone())
        active = PT._active_cycle(conn)
        positions = PT._position_rows(conn, ACCOUNT, inner.day)
        with mock.patch.object(PT, "_completed_kline", return_value=None):
            action, reason = PT._intraday_sell(
                conn, account, dict(positions[0]), dict(inner.quotes_map[inner.code]),
                inner.day, PT._risk_profile(account, conn=conn), active,
            )
        conn.commit()

        remaining = _remaining(conn, cycle, inner.code)
        state = _state_present(conn, cycle, inner.code)
        sold = int((action or {}).get("qty") or 0)
        reproduced = sold == 100 and remaining == 0 and state
        print(f"  R14-R2-C2 intraday 100-share full SELL: sold={sold}"
              f" remaining_lots={remaining} risk_state_present={state}"
              f" reason={reason!r}")
        return reproduced
    finally:
        if conn is not None:
            conn.close()
        inner.tearDown()


def main() -> int:
    import subprocess
    head = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"probe revision: {head}")
    print("R14 Round-2 before-fix reproduction (Blocker A: full-exit lifecycle)")

    results = {
        "R14-R2-C1": probe_execution_planner_full_sell(),
        "R14-R2-C2": probe_intraday_full_sell(),
    }
    print("\n===== summary =====")
    for key, value in results.items():
        print(f"  {key}: {'REPRODUCED' if value else 'NOT REPRODUCED'}")
    reproduced = sum(1 for value in results.values() if value)
    print(f"\nRESULT: {reproduced}/{len(results)} defect(s) reproduced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
