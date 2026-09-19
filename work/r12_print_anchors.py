# -*- coding: utf-8 -*-
"""只读：打印 Round-12 变异锚点所在行（供人工核对，不修改任何文件）。"""
import io
import sys

TARGETS = (
    "backend/rebalance_scanner.py",
    "backend/api_adaptive.py",
    "backend/paper_schema_migrations.py",
)

NEEDLES = (
    "cycle_id=?",
    "cycle_id, today.isoformat()",
    "AND cycle_id=?",
    "WHERE id=? AND cycle_id=?",
    "cursor.rowcount",
    "current_cycle_id != requested_cycle_id",
    "PSM.ensure_rebalance_state_cycle_ownership(conn)",
    "UNIQUE(cycle_id, scan_date, account_id, code)",
    "PRIMARY KEY(cycle_id, code, account_id)",
    "cycle_id = rebalance_scanner.resolve_cycle_id(conn)",
    "cycle_id, plan_date",
    "cycle_id=cycle_id",
    "REBALANCE_SCANS_UNIQUE",
    "REBALANCE_COOLDOWN_PK",
    "def _require_cycle_id",
    "if cycle_id is None:",
)

for path in TARGETS:
    lines = io.open(path, encoding="utf-8").read().split("\n")
    print(f"===== {path} =====")
    for index, line in enumerate(lines, 1):
        if any(needle in line for needle in NEEDLES):
            print(f"{index:5d} | {line}")
    print()

sys.exit(0)
