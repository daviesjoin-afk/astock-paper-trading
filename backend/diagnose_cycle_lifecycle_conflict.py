#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only diagnostic for the "running cycle + archive marker" P0.

It answers, from database facts alone, why a strict portfolio read is UNKNOWN
and whether the archive marker is a removable orphan or a real authority.

It NEVER writes: the database is opened with ``mode=ro``. Anything that would
require a mutation is instead simulated on an in-memory copy so the operator can
see whether a proposed repair would actually restore the read.

Usage:
    python scripts/diagnose_cycle_lifecycle_conflict.py --db <paper_trading.sqlite3>
    python scripts/diagnose_cycle_lifecycle_conflict.py --db <db> --cycle-id 259
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import paper_portfolio_read_model as PRM  # noqa: E402

ACTIVE_STATUSES = ("draft", "running", "paused")
ARCHIVE_FORMAT = "compact-ledger-v2"


def _connect_ro(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _has_table(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _count(conn, table, where="", params=()) -> int:
    if not _has_table(conn, table):
        return 0
    try:
        sql = f'SELECT COUNT(1) FROM "{table}"' + (f" WHERE {where}" if where else "")
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return 0


def resolve_cycle(conn, requested):
    if requested is not None:
        row = conn.execute(
            "SELECT * FROM paper_cycles WHERE id=?", (int(requested),)
        ).fetchone()
        return dict(row) if row else None
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT * FROM paper_cycles WHERE status IN ({marks}) ORDER BY id DESC LIMIT 1",
        ACTIVE_STATUSES,
    ).fetchone()
    return dict(row) if row else None


def archive_rows(conn, cycle_id):
    if not _has_table(conn, "paper_archives"):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM paper_archives WHERE cycle_id=? ORDER BY id", (cycle_id,)
    )]


def audit_trail(conn, cycle_key):
    if not _has_table(conn, "paper_audit"):
        return []
    cols = _columns(conn, "paper_audit")
    if "detail" not in cols:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT id,event,detail,created_at FROM paper_audit "
        "WHERE event IN ('cycle_created','cycle_archived','accounts_running',"
        "'accounts_paused') AND detail LIKE ? ORDER BY id",
        (f"%{cycle_key}%",),
    )]


def lot_lineage(conn, cycle_id):
    """Per-lot: is a verified BUY fill reachable, i.e. is quantity provable?"""
    if not _has_table(conn, "paper_position_lots"):
        return {"total": 0, "linked": 0, "unlinked": 0, "unresolved": 0, "rows": []}
    rows = [dict(r) for r in conn.execute(
        "SELECT id,account_id,code,qty,remaining_qty,source_order_id "
        "FROM paper_position_lots WHERE cycle_id=? ORDER BY id", (cycle_id,)
    )]
    linked = unlinked = unresolved = 0
    detail = []
    for lot in rows:
        if lot.get("source_order_id") is None:
            unlinked += 1
            if int(lot.get("remaining_qty") or 0) > 0:
                unresolved += 1
            detail.append({**lot, "lineage": "NO_SOURCE_ORDER"})
            continue
        linked += 1
        detail.append({**lot, "lineage": "linked"})
    return {"total": len(rows), "linked": linked, "unlinked": unlinked,
            "unresolved_open": unresolved, "rows": detail}


def live_facts(conn, cycle_id):
    """Cycle-owned rows that survive an archive purge for this cycle.

    ``paper_accounts`` is deliberately excluded from this decisive set: its
    ``cycle_id`` is a *mutable rebinding* that ``_create_cycle`` rewrites onto
    the new cycle, and it is not in the archive purge list. Counting it would
    make a genuine orphan marker look like live activity forever. It is
    reported separately as a binding indicator instead.
    """
    out = {}
    for table in ("paper_orders", "paper_fills", "paper_position_lots", "paper_nav"):
        if not _has_table(conn, table):
            out[table] = None
            continue
        if "cycle_id" not in _columns(conn, table):
            if table == "paper_fills":
                out[table] = _count(
                    conn, "paper_fills",
                    "order_id IN (SELECT id FROM paper_orders WHERE cycle_id=?)",
                    (cycle_id,),
                )
                continue
            out[table] = None
            continue
        out[table] = _count(conn, table, "cycle_id=?", (cycle_id,))
    return out


def account_binding(conn, cycle_id):
    """How many paper_accounts currently point at this cycle (rebinding signal)."""
    if not _has_table(conn, "paper_accounts") or \
            "cycle_id" not in _columns(conn, "paper_accounts"):
        return None
    return _count(conn, "paper_accounts", "cycle_id=?", (cycle_id,))



def snapshot_summary(raw):
    try:
        snap = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(snap, dict):
        return None
    orders = snap.get("paper_orders") or []
    return {
        "format": snap.get("_archive_format"),
        "order_ids": sorted(
            int(o["id"]) for o in orders
            if isinstance(o, dict) and o.get("id") is not None
        ),
        "table_counts": snap.get("_table_counts") or {},
    }


def read_status(conn, cycle_id, asof_day):
    ctx = PRM.PortfolioReadContext(cycle_id=cycle_id, asof_day=asof_day)
    archived = PRM._cycle_is_archived(conn, ctx)
    lots, qty = PRM.bounded_lots_with_status(conn, ctx)
    cash_v, cash = PRM.cash(conn, ctx)
    pnl_v, pnl = PRM.realized_pnl(conn, ctx)
    return {
        "archived": archived, "quantity": qty, "cash": cash, "realized_pnl": pnl,
        "lots_visible": len(lots),
        "all_verified": qty == PRM.STATUS_VERIFIED
        and cash == PRM.STATUS_VERIFIED and pnl == PRM.STATUS_VERIFIED,
    }


def simulate_without_marker(db_path, cycle_id, asof_day):
    """Copy the ledger in memory, drop ONE archive row, and re-measure.

    This is how we learn whether removing the marker is even sufficient, without
    touching the real database.
    """
    src = _connect_ro(db_path)
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    try:
        src.backup(mem)
        mem.execute(
            "DELETE FROM paper_archives WHERE cycle_id=? AND id IN "
            "(SELECT id FROM paper_archives WHERE cycle_id=? ORDER BY id LIMIT 1)",
            (cycle_id, cycle_id),
        )
        mem.commit()
        ctx = PRM.PortfolioReadContext(cycle_id=cycle_id, asof_day=asof_day)
        lots, qty = PRM.bounded_lots_with_status(mem, ctx)
        cash_v, cash = PRM.cash(mem, ctx)
        pnl_v, pnl = PRM.realized_pnl(mem, ctx)
        return {
            "archived": PRM._cycle_is_archived(mem, ctx),
            "quantity": qty, "cash": cash, "realized_pnl": pnl,
            "lots_visible": len(lots),
            "all_verified": qty == PRM.STATUS_VERIFIED
            and cash == PRM.STATUS_VERIFIED and pnl == PRM.STATUS_VERIFIED,
        }
    finally:
        mem.close()
        src.close()


def snapshot_authority(conn, snap):
    """Do the archived order ids exist anywhere else in the ledger?

    Section 8 requires proving an "orphan" marker carries no legitimate
    historical authority. If the snapshot names orders that survive in neither
    ``paper_orders`` nor ``paper_orders_archive``, the marker is the only record
    of that history and must not be treated as a removable artifact.
    """
    if not snap or not snap.get("order_ids"):
        return {"exclusive_order_ids": [], "exclusive": False}
    ids = list(snap["order_ids"])
    placeholders = ",".join("?" for _ in ids)
    known: set[int] = set()
    for table in ("paper_orders", "paper_orders_archive"):
        if not _has_table(conn, table):
            continue
        try:
            for row in conn.execute(
                f"SELECT id FROM \"{table}\" WHERE id IN ({placeholders})", tuple(ids)
            ):
                known.add(int(row[0]))
        except sqlite3.Error:
            continue
    exclusive = sorted(set(ids) - known)
    return {"exclusive_order_ids": exclusive, "exclusive": bool(exclusive)}


def classify(conn, cycle, archives, lineage, facts, sim, snap):
    """Return (classification, reasons). Never invents a cause."""
    reasons = []
    status = str(cycle.get("status") or "")
    if not archives:
        reasons.append("no archive row for this cycle")
        return "NO_ARCHIVE_CONFLICT", reasons

    if status in ACTIVE_STATUSES:
        reasons.append(f"cycle status={status} but {len(archives)} archive row(s) exist")
    if len(archives) > 1:
        reasons.append(f"{len(archives)} archive rows for one cycle -> duplicated archive")
    for a in archives:
        if str(a.get("cycle_key") or "") != str(cycle.get("cycle_key") or ""):
            reasons.append(
                f"archive id={a.get('id')} cycle_key={a.get('cycle_key')!r} "
                f"!= paper_cycles.cycle_key={cycle.get('cycle_key')!r}"
            )

    if not sim["all_verified"]:
        reasons.append(
            "removing the marker is NOT sufficient: read is still "
            f"quantity={sim['quantity']} cash={sim['cash']} pnl={sim['realized_pnl']}"
        )
    if lineage["unlinked"]:
        reasons.append(
            f"{lineage['unlinked']} lot(s) have source_order_id IS NULL "
            f"({lineage['unresolved_open']} still open) -> lineage unprovable"
        )

    if len(archives) > 1:
        return "DUPLICATED_ARCHIVE", reasons
    if any(str(a.get("cycle_key") or "") != str(cycle.get("cycle_key") or "")
           for a in archives):
        return "AMBIGUOUS CYCLE LIFECYCLE CONFLICT", reasons

    # Single archive row whose key matches. Decide orphan vs authority.
    live_total = sum(v for v in facts.values() if v)
    authority = snapshot_authority(conn, snap)
    if authority["exclusive"]:
        reasons.append(
            f"archive snapshot holds {len(authority['exclusive_order_ids'])} order id(s) "
            f"present in no live/archive table ({authority['exclusive_order_ids'][:8]}) "
            "-> the marker carries exclusive history and must not be removed"
        )
        return "AMBIGUOUS CYCLE LIFECYCLE CONFLICT", reasons
    if live_total == 0:
        reasons.append("live cycle-owned tables are empty; no post-archive facts")
        reasons.append(
            "candidate only: uniqueness must still be proven (and the snapshot's "
            "authority ruled out) before any repair plan is written"
        )
        return "PROVEN_ORPHAN_CANDIDATE", reasons

    reasons.append(
        "live cycle-owned tables still hold facts, so the archive may be a real "
        "authority or the cycle was reopened -> cannot be proven from facts alone"
    )
    return "AMBIGUOUS CYCLE LIFECYCLE CONFLICT", reasons



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="paper_trading.sqlite3 (opened read-only)")
    parser.add_argument("--cycle-id", type=int, default=None,
                        help="defaults to the active cycle (draft/running/paused, highest id)")
    parser.add_argument("--asof", default=None, help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args(argv)

    asof_day = (dt.date.fromisoformat(args.asof) if args.asof else dt.date.today())
    conn = _connect_ro(args.db)
    try:
        print(f"db     : {Path(args.db).resolve()}")
        print(f"asof   : {asof_day.isoformat()}")
        print(f"integrity: {conn.execute('PRAGMA quick_check').fetchone()[0]}")
        print()

        cycle = resolve_cycle(conn, args.cycle_id)
        if cycle is None:
            print("no cycle found for the requested/active selection")
            return 2
        cycle_id = int(cycle["id"])
        print(f"== cycle {cycle_id} ({cycle.get('cycle_key')}) status={cycle.get('status')} ==")
        print()

        archives = archive_rows(conn, cycle_id)
        print(f"archive rows for this cycle: {len(archives)}")
        for a in archives:
            summ = snapshot_summary(a.get("snapshot"))
            print(f"  id={a.get('id')} key={a.get('cycle_key')!r} created_at={a.get('created_at')}")
            print(f"     reason={str(a.get('reason'))[:80]!r}")
            if summ:
                print(f"     snapshot format={summ['format']!r} "
                      f"archived_order_ids={summ['order_ids'][:12]}"
                      f"{' ...' if len(summ['order_ids']) > 12 else ''}")
        print()

        print("== paper_cycles status histogram ==")
        for r in conn.execute(
            "SELECT status, COUNT(1) n FROM paper_cycles GROUP BY status ORDER BY n DESC"
        ):
            print(f"  {r[0]}: {r[1]}")
        print()

        active = [dict(r) for r in conn.execute(
            f"SELECT id,cycle_key,status FROM paper_cycles WHERE status IN "
            f"({','.join('?' for _ in ACTIVE_STATUSES)}) ORDER BY id", ACTIVE_STATUSES)]
        print(f"== active-cycle candidates ({len(active)}) ==")
        for r in active:
            n = len(archive_rows(conn, int(r["id"])))
            flag = "  <-- CONFLICT: archive row exists" if n else ""
            print(f"  id={r['id']} {r['status']:<8} {r['cycle_key']}{flag}")
        print()

        lineage = lot_lineage(conn, cycle_id)
        print("== durable-lot lineage (quantity provability) ==")
        print(f"  lots={lineage['total']} linked={lineage['linked']} "
              f"unlinked={lineage['unlinked']} open-unresolved={lineage['unresolved_open']}")
        print()

        facts = live_facts(conn, cycle_id)
        print("== cycle-owned facts surviving an archive purge ==")
        for k, v in facts.items():
            print(f"  {k}: {v if v is not None else 'n/a'}")
        binding = account_binding(conn, cycle_id)
        print(f"  paper_accounts bound to this cycle: "
              f"{binding if binding is not None else 'n/a'}")
        print("  (note: paper_accounts.cycle_id is a mutable rebinding and is NOT")
        print("   counted as live proof; it is not in the archive purge list)")
        print()

        print("== audit trail (this cycle_key) ==")
        trail = audit_trail(conn, cycle.get("cycle_key"))
        if trail:
            for r in trail:
                print(f"  {r['id']} {r['event']} | {str(r['detail'])[:80]} | {r['created_at']}")
        else:
            print("  (none)")
        print()

        current = read_status(conn, cycle_id, asof_day)
        print("== strict portfolio read (as-is) ==")
        print(f"  archived={current['archived']} lots_visible={current['lots_visible']}")
        print(f"  quantity={current['quantity']} cash={current['cash']} "
              f"realized_pnl={current['realized_pnl']}")
        print(f"  FULLY VERIFIED = {current['all_verified']}")
        print()

        print("== simulation: same ledger with ONE archive row removed ==")
        sim = simulate_without_marker(args.db, cycle_id, asof_day)
        print(f"  archived={sim['archived']} lots_visible={sim['lots_visible']}")
        print(f"  quantity={sim['quantity']} cash={sim['cash']} "
              f"realized_pnl={sim['realized_pnl']}")
        print(f"  FULLY VERIFIED = {sim['all_verified']}")
        print(f"  => marker removal alone is {'SUFFICIENT' if sim['all_verified'] else 'NOT SUFFICIENT'}")
        print()

        snap = snapshot_summary(archives[0].get("snapshot")) if archives else None
        verdict, reasons = classify(conn, cycle, archives, lineage, facts, sim, snap)

        print("== classification ==")
        print(f"  {verdict}")
        for r in reasons:
            print(f"    - {r}")
        print()

        print("== next step ==")
        if verdict == "NO_ARCHIVE_CONFLICT":
            print("  No marker conflict. Investigate lineage / other fail-closed gates.")
        elif verdict == "PROVEN_ORPHAN_CANDIDATE":
            print("  A single matching archive row over an empty live ledger is a candidate")
            print("  orphan, but uniqueness must still be proven (see section 8) before any")
            print("  repair plan is written. Do NOT delete the row by hand.")
        else:
            print("  Not uniquely provable from facts. Per the recovery contract, stop and")
            print("  report AMBIGUOUS CYCLE LIFECYCLE CONFLICT. Do not delete the marker,")
            print("  do not flip cycle status, and do not relax the strict reader.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
