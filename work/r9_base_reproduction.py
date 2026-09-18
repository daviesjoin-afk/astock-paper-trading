# -*- coding: utf-8 -*-
"""Round-9 §2/§22 base reproduction.

Proves that on the UNMODIFIED base commit a legacy ``paper_positions`` mirror row
is rematerialized into the CURRENT active cycle as a ``source_order_id IS NULL``
lot, purely as a side effect of an ordinary position read.

Usage:
    python work/r9_base_reproduction.py          # expect RED on base, GREEN after fix

Uses the real production schema (``PT.init_db``) and the real production primitives
(``_position_rows`` / ``_sync_positions`` / ``_shared_account_exposure``), because
the invariant under test is held by production code, not by a hand-written fixture.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest import mock

BACKEND = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
sys.path.insert(0, BACKEND)

import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
NAME = "测试股"


class Repro:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.patchers = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for p in self.patchers:
            p.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle8 = int(
            self.conn.execute("SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone()[0]
        )

    def close(self):
        self.conn.close()
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    # ── fixtures ────────────────────────────────────────────────────────────
    def add_cycle(self, status="running", started_at="2026-09-20 09:30:00"):
        cur = self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,updated_at,started_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (f"c-r9-{started_at[:10]}", status, 100000.0, "shared_pool", started_at, started_at,
             started_at if status == "running" else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_lot(self, cycle_id, qty, available_date="2026-09-01"):
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,"
            "cost,acquired_at,available_date,asset_type,cost_fee_included,is_t_base,source_order_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ACCOUNT, CODE, NAME, "测试", qty, qty, 10.0,
             "2026-08-31 10:00:00", available_date, "stock_t1", 1, 1, 4242),
        )
        self.conn.commit()

    def active_cycle(self):
        return int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])

    def lot_count(self, cycle_id, only_null_source=False):
        sql = "SELECT COUNT(*) FROM paper_position_lots WHERE cycle_id=?"
        if only_null_source:
            sql += " AND source_order_id IS NULL"
        return int(self.conn.execute(sql, (cycle_id,)).fetchone()[0])

    def mirror_rows(self):
        return self.conn.execute("SELECT account_id,code,qty FROM paper_positions").fetchall()

    def lot_fingerprint(self):
        """Full identity of the lot table: (id, cycle, qty, source_order_id)."""
        return [
            (r["id"], r["cycle_id"], r["remaining_qty"], r["source_order_id"])
            for r in self.conn.execute(
                "SELECT id,cycle_id,remaining_qty,source_order_id FROM paper_position_lots ORDER BY id"
            )
        ]


def scenario_new_cycle():
    """§2: cycle 8 lot + mirror, then cycle 9 activated with no lots of its own."""
    r = Repro()
    try:
        print(f"  [setup] cycle8={r.cycle8} active={r.active_cycle()}")
        r.add_lot(r.cycle8, 100)
        PT._sync_positions(r.conn)               # mirror written the normal way
        r.conn.commit()
        print(f"  [setup] cycle8 lots={r.lot_count(r.cycle8)} mirror={[tuple(m) for m in r.mirror_rows()]}")

        c9 = r.add_cycle()
        r.conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?", (c9, ACCOUNT))
        r.conn.commit()
        print(f"  [setup] active cycle={r.active_cycle()} (must be {c9}); cycle9 lots={r.lot_count(c9)}")

        before9, before8 = r.lot_count(c9), r.lot_count(r.cycle8)

        # ── THE CALL UNDER TEST ──
        PT._position_rows(r.conn, readonly=False)

        after9, after9_null, after8 = r.lot_count(c9), r.lot_count(c9, True), r.lot_count(r.cycle8)
        print(f"\n  cycle 9 lots: {before9} -> {after9}   (source_order_id IS NULL: {after9_null})")
        print(f"  cycle 8 lots: {before8} -> {after8}   (must be unchanged)")
        for row in r.conn.execute(
            "SELECT cycle_id,code,qty,source_order_id,acquired_at FROM paper_position_lots WHERE cycle_id=?", (c9,)
        ):
            print(f"    NEW LOT -> cycle={row['cycle_id']} {row['code']} qty={row['qty']} "
                  f"source_order_id={row['source_order_id']} acquired_at={row['acquired_at']}")
        return after9 > before9, after8 != before8
    finally:
        r.close()


def scenario_repeated_reads():
    """§20 LP2: repeated _position_rows calls must not change lot state."""
    r = Repro()
    try:
        r.add_lot(r.cycle8, 100)
        PT._sync_positions(r.conn)
        r.conn.commit()
        c9 = r.add_cycle()
        r.conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?", (c9, ACCOUNT))
        r.conn.commit()

        PT._position_rows(r.conn, readonly=False)
        first = r.lot_fingerprint()
        PT._position_rows(r.conn, readonly=False)
        PT._position_rows(r.conn, readonly=False)
        second = r.lot_fingerprint()
        print(f"  reads 1 vs 3 identical: {first == second}   (lots={len(first)} -> {len(second)})")
        return first == second
    finally:
        r.close()


def scenario_exposure_read():
    """§13: _shared_account_exposure must not create lots."""
    r = Repro()
    try:
        r.add_lot(r.cycle8, 100)
        PT._sync_positions(r.conn)
        r.conn.commit()
        c9 = r.add_cycle()
        r.conn.execute("UPDATE paper_accounts SET cycle_id=? WHERE id=?", (c9, ACCOUNT))
        r.conn.commit()
        before = r.lot_fingerprint()
        try:
            PT._shared_account_exposure(r.conn, {})
        except Exception as exc:                      # noqa: BLE001
            print(f"  (exposure raised {type(exc).__name__}; still checking lot state)")
        after = r.lot_fingerprint()
        print(f"  exposure read lot state unchanged: {before == after}   (lots={len(before)} -> {len(after)})")
        return before == after
    finally:
        r.close()


def main():
    print("=== §2  new-cycle stale mirror ===")
    leaked, cycle8_changed = scenario_new_cycle()
    print("\n=== §20 LP2  repeated reads ===")
    stable = scenario_repeated_reads()
    print("\n=== §13  exposure read ===")
    exposure_clean = scenario_exposure_read()

    print("\n=== VERDICT ===")
    print(f"  active cycle received inferred lot : {leaked}")
    print(f"  older cycle mutated                : {cycle8_changed}")
    print(f"  repeated reads stable              : {stable}")
    print(f"  exposure read created no lot       : {exposure_clean}")
    if leaked:
        print("\nBUG REPRODUCED / RED — legacy mirror rematerialized into the active cycle")
        return 1
    print("\nGREEN — no inferred lot in the active cycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
