# -*- coding: utf-8 -*-
"""Undo the Round-12 probe's contamination of the local data_cache ledger.

Round-11's production audit recorded that ``rebalance_scans`` / ``rebalance_plans``
/ ``rebalance_cooldown`` were **absent** from the local ``data_cache`` pair (the
endpoint had never successfully run).  A Round-12 probe run accidentally targeted
this live ledger instead of its temporary fixture, creating the three tables and
4 ``rebalance_scans`` rows.

This script prints the rows it is about to remove, then removes them, then drops
the three tables — restoring the recorded pre-probe state exactly.  Run with
``--check`` to only print.
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import adaptive_engine as AE  # noqa: E402

TABLES = ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")


def main():
    check = "--check" in sys.argv
    path = AE.PAPER_DB_PATH
    print(f"target = {path}")
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        present = [t for t in TABLES if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()]
        print(f"present rebalance tables = {present}")
        for table in present:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            print(f"  {table}: {len(rows)} row(s)")
            for row in rows:
                print("    ", dict(row))
        if check:
            print("--check: nothing written")
            return 0
        if not present:
            print("nothing to do")
            return 0
        for table in present:
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
        remaining = [t for t in TABLES if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()]
        print(f"after restore: present rebalance tables = {remaining}")
        print("RESTORE OK" if not remaining else "RESTORE INCOMPLETE")
        return 0 if not remaining else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
