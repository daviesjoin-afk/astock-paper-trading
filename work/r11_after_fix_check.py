# -*- coding: utf-8 -*-
"""Round-11 after-fix 验证：两个物理分离的库，scan 必须落在 paper DB。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import adaptive_engine as AE  # noqa: E402
import api_adaptive as API  # noqa: E402
import paper_trading as PT  # noqa: E402


def tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    finally:
        c.close()


def main():
    tmp = tempfile.mkdtemp(prefix="r11_after_")
    paper = os.path.join(tmp, "paper.sqlite3")
    adap = os.path.join(tmp, "adaptive.sqlite3")

    with mock.patch.object(PT, "DB_PATH", paper), \
            mock.patch.object(PT, "_benchmark_close", return_value=None), \
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True):
        PT.init_db()
    with mock.patch.object(AE, "DB_PATH", adap), \
            mock.patch.object(AE, "CACHE_DIR", tmp):
        with AE._connect():
            pass

    c = sqlite3.connect(paper)
    c.row_factory = sqlite3.Row
    c.execute("UPDATE paper_accounts SET status='running' WHERE id='tq_breakout'")
    cycle = c.execute(
        "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
        " ORDER BY id DESC LIMIT 1").fetchone()[0]
    c.execute(
        "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
        "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
        "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cycle, "tq_breakout", "600519", "测试股", "测试", 100, 100, 10.0,
         "2026-08-31 10:00:00", "2026-09-01", "stock_t1", 1, 1, 4242))
    c.commit()
    c.close()

    quotes = {"600519": {"code": "600519", "price": 10.0, "pct": 0.0, "super_net": 0.0}}
    with mock.patch.object(AE, "DB_PATH", adap), \
            mock.patch.object(AE, "PAPER_DB_PATH", paper), \
            mock.patch.object(AE, "CACHE_DIR", tmp), \
            mock.patch.object(API, "_fetch_rebalance_quotes",
                              return_value=(quotes, {"source": "fixture"})):
        res = API.run_rebalance_scan(confirmed=True)
        print("scan OK:", res.get("total_positions"), "positions,",
              res.get("plans_created"), "plans")
        status = API.rebalance_status()
        print("status recent_scans:", len(status["recent_scans"]))
        plans = API.get_rebalance_plans(status="all")
        print("plans:", len(plans["plans"]))
        verify = API.verify_rebalance_plans(confirmed=True)
        print("verify:", verify.get("message") or f"{len(verify.get('plans', []))} plans")

    pt = tables(paper)
    at = tables(adap)
    print()
    print("paper DB rebalance_*   :", sorted(t for t in pt if t.startswith("rebalance")))
    print("adaptive DB rebalance_*:", sorted(t for t in at if t.startswith("rebalance")))
    c = sqlite3.connect(paper)
    print("paper rebalance_scans rows:", c.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0])
    c.close()


if __name__ == "__main__":
    main()
