# -*- coding: utf-8 -*-
"""Read-only audit of the local data_cache DBs for rebalance state (no copy taken)."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import adaptive_engine as AE  # noqa: E402
import data_paths  # noqa: E402

print("data_dir =", data_paths.data_dir())
for label, path in (("paper", AE.PAPER_DB_PATH), ("adaptive", AE.DB_PATH)):
    print("=" * 60)
    print(f"{label}: {path}")
    print("  exists =", os.path.exists(path))
    if not os.path.exists(path):
        continue
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("rebalance_scans", "rebalance_plans", "rebalance_cooldown"):
            if table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table:22s} rows={count}")
            else:
                print(f"  {table:22s} ABSENT")
        if "rebalance_scans" in tables:
            for row in conn.execute(
                    "SELECT id,scan_date,account_id,code,quality_score,action,created_at"
                    " FROM rebalance_scans ORDER BY id"):
                print("    scan:", tuple(row))
        if "rebalance_plans" in tables:
            for row in conn.execute(
                    "SELECT id,plan_date,account_id,code,status,sell_qty,created_at"
                    " FROM rebalance_plans ORDER BY id"):
                print("    plan:", tuple(row))
    finally:
        conn.close()
