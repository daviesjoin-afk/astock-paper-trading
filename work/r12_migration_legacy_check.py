# -*- coding: utf-8 -*-
"""Round-12 迁移在**真实旧 schema** 上的行为验证（只读于真实库，写入只在临时库）。

回答三个问题：
1. 旧 schema（三表无 cycle_id、scans 是 UNIQUE(scan_date,account_id,code)、
   cooldown 是 PK(code,account_id)）跑 v19 后是否变成含周期的契约；
2. 旧行是否逐字保留、cycle_id 是否仍为 NULL；
3. 再跑一次是否 no-op（幂等）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import paper_schema_migrations as PSM  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE rebalance_scans(
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_date TEXT NOT NULL,
    account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    current_qty INTEGER, cost REAL, current_price REAL, unrealized_pnl_pct REAL,
    hold_days INTEGER, quality_score REAL, prev_quality_score REAL,
    quality_change REAL, fund_flow_trend TEXT, consecutive_outflow_days INTEGER,
    action TEXT NOT NULL, action_reason TEXT, planned_sell_ratio REAL DEFAULT 0,
    scan_version TEXT, created_at TEXT NOT NULL,
    UNIQUE(scan_date, account_id, code)
);
CREATE TABLE rebalance_plans(
    id INTEGER PRIMARY KEY AUTOINCREMENT, plan_date TEXT NOT NULL,
    execute_date TEXT, account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    action TEXT NOT NULL, sell_qty INTEGER, sell_ratio REAL, sell_reason TEXT,
    replacement_code TEXT, replacement_name TEXT, replacement_score REAL,
    status TEXT NOT NULL DEFAULT 'planned', open_price REAL, open_pct REAL,
    open_volume_ratio REAL, open_fund_flow REAL, open_verified BOOLEAN DEFAULT 0,
    open_verify_reason TEXT, executed_at TEXT, executed_price REAL,
    executed_qty INTEGER, realized_pnl REAL, plan_version TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE rebalance_cooldown(
    code TEXT NOT NULL, account_id TEXT NOT NULL, sold_date TEXT NOT NULL,
    cooldown_until TEXT NOT NULL, PRIMARY KEY(code, account_id)
);
"""

OLD_SCAN = ("INSERT INTO rebalance_scans(scan_date,account_id,code,name,current_qty,"
            "cost,current_price,unrealized_pnl_pct,hold_days,quality_score,"
            "prev_quality_score,quality_change,fund_flow_trend,consecutive_outflow_days,"
            "action,action_reason,planned_sell_ratio,scan_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
OLD_PLAN = ("INSERT INTO rebalance_plans(plan_date,account_id,code,name,action,sell_qty,"
            "sell_ratio,sell_reason,status,plan_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
OLD_COOL = ("INSERT INTO rebalance_cooldown(code,account_id,sold_date,cooldown_until) "
            "VALUES(?,?,?,?)")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="r12_mig_")
    path = os.path.join(tmp, "legacy.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(OLD_SCAN, ("2026-09-19", "tq_breakout", "600519", "测试股", 100, 10.0,
                            10.0, 0.0, 30, 90.0, 90.0, 0.0, "outflow", 1, "sell",
                            "legacy row", 1.0, "daily-rebalance-v2", "2026-09-19T22:00:00"))
    conn.execute(OLD_PLAN, ("2026-09-19", "tq_breakout", "600519", "测试股", "sell",
                            100, 1.0, "legacy plan", "planned", "daily-rebalance-v2",
                            "2026-09-19T22:00:00", "2026-09-19T22:00:00"))
    conn.execute(OLD_COOL, ("600519", "tq_breakout", "2026-09-18", "2026-09-25"))
    conn.commit()

    before = {
        "scans": conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0],
        "plans": conn.execute("SELECT COUNT(*) FROM rebalance_plans").fetchone()[0],
        "cool": conn.execute("SELECT COUNT(*) FROM rebalance_cooldown").fetchone()[0],
    }
    scan_row_before = conn.execute(
        "SELECT scan_date,account_id,code,name,quality_score,fund_flow_trend,action,"
        "planned_sell_ratio,created_at FROM rebalance_scans").fetchone()
    print(f"旧 schema 行数            = {before}")
    print(f"旧 scans 唯一契约         = {PSM._unique_index_columns(conn, 'rebalance_scans')}")
    print(f"旧 cooldown 主键          = {PSM._primary_key_columns(conn, 'rebalance_cooldown')}")

    changes1 = PSM.ensure_rebalance_state_cycle_ownership(conn)
    conn.commit()
    print(f"\n第一次迁移 changes         = {changes1}")
    after1 = {
        "scans": conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0],
        "plans": conn.execute("SELECT COUNT(*) FROM rebalance_plans").fetchone()[0],
        "cool": conn.execute("SELECT COUNT(*) FROM rebalance_cooldown").fetchone()[0],
    }
    print(f"迁移后行数                = {after1}")
    print(f"新 scans 唯一契约         = {PSM._unique_index_columns(conn, 'rebalance_scans')}")
    print(f"新 cooldown 主键          = {PSM._primary_key_columns(conn, 'rebalance_cooldown')}")
    print(f"scans.cycle_id 列         = {'cycle_id' in PSM.table_columns(conn, 'rebalance_scans')}")
    print(f"plans.cycle_id 列         = {'cycle_id' in PSM.table_columns(conn, 'rebalance_plans')}")
    print(f"cooldown.cycle_id 列      = {'cycle_id' in PSM.table_columns(conn, 'rebalance_cooldown')}")

    legacy_cycles = {
        "scans": conn.execute("SELECT cycle_id FROM rebalance_scans").fetchall(),
        "plans": conn.execute("SELECT cycle_id FROM rebalance_plans").fetchall(),
        "cool": conn.execute("SELECT cycle_id FROM rebalance_cooldown").fetchall(),
    }
    print(f"legacy cycle_id 值        = {legacy_cycles}")
    scan_row_after = conn.execute(
        "SELECT scan_date,account_id,code,name,quality_score,fund_flow_trend,action,"
        "planned_sell_ratio,created_at FROM rebalance_scans").fetchone()
    print(f"legacy scans 其它字段逐字保留 = {scan_row_before == scan_row_after}")

    changes2 = PSM.ensure_rebalance_state_cycle_ownership(conn)
    conn.commit()
    after2 = {
        "scans": conn.execute("SELECT COUNT(*) FROM rebalance_scans").fetchone()[0],
        "plans": conn.execute("SELECT COUNT(*) FROM rebalance_plans").fetchone()[0],
        "cool": conn.execute("SELECT COUNT(*) FROM rebalance_cooldown").fetchone()[0],
    }
    print(f"\n第二次迁移 changes         = {changes2}")
    print(f"第二次迁移后行数           = {after2}")

    checks = {
        "row count preserved": before == after1 == after2,
        "scans UNIQUE includes cycle_id": (
            PSM._unique_index_columns(conn, "rebalance_scans") == PSM.REBALANCE_SCANS_UNIQUE),
        "cooldown PK includes cycle_id": (
            PSM._primary_key_columns(conn, "rebalance_cooldown") == PSM.REBALANCE_COOLDOWN_PK),
        "legacy other fields preserved verbatim": scan_row_before == scan_row_after,
        "legacy cycle_id stays NULL": all(
            row[0] is None for rows in legacy_cycles.values() for row in rows),
        "second run is a no-op": set(changes2.values()) == {"ok"},
    }
    print("\n── VERDICT ──")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    conn.close()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
