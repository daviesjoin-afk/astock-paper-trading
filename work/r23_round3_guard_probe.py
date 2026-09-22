# -*- coding: utf-8 -*-
"""验证 paper_signals INSERT guard 的 account-cycle 一致性（本地证据）。"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import paper_trading as PT
import strategy_registry as SR

tmp = tempfile.mkdtemp()
PT.DB_PATH = os.path.join(tmp, "p.sqlite3")
PT.init_db()
conn = sqlite3.connect(PT.DB_PATH)
conn.row_factory = sqlite3.Row
acc = conn.execute(
    "SELECT id,cycle_id FROM paper_accounts WHERE cycle_id IS NOT NULL LIMIT 1").fetchone()
print("account", acc["id"], "cycle", acc["cycle_id"])
conn.execute(
    "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,updated_at)"
    " VALUES('x','running',1000,'shared_pool',datetime('now'),datetime('now'))")
other = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
print("other cycle", other)
stamp = SR.stamp_for_account(conn, acc["id"], cycle_id=acc["cycle_id"])

SQL = (
    "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,name,payload,"
    "status,reason,created_at,strategy_id,strategy_version,strategy_checksum,cycle_id)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")


def ins(cycle, code="600901"):
    conn.execute(SQL, (acc["id"], "2026-09-07", "2026-09-07", code, "t", "{}",
                       "pending", "", "2026-09-07T15:00:00") + tuple(stamp) + (cycle,))


ins(acc["cycle_id"])
conn.commit()
print("own cycle       : OK")
for label, cycle in (("ghost cycle", 99999), ("foreign cycle", other)):
    try:
        ins(cycle, code=f"6009{cycle % 100:02d}")
        conn.commit()
        print(f"{label:15}: ACCEPTED (BAD)")
    except sqlite3.IntegrityError as exc:
        print(f"{label:15}: rejected -> {exc}")
# action-scoped row（无 account）仍允许 NULL 语义
try:
    conn.execute(SQL, (None, "2026-09-07", "2026-09-07", "600999", "t", "{}",
                       "pending", "", "2026-09-07T15:00:00") + tuple(stamp) + (None,))
    conn.commit()
    print("account-less row: accepted (guard 只约束 account-scoped)")
except sqlite3.IntegrityError as exc:
    print("account-less row: rejected ->", exc)
