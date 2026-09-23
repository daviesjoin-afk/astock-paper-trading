# -*- coding: utf-8 -*-
"""Contracts for the read-only cycle-lifecycle diagnostic.

The diagnostic exists to answer section 7's question — *why* a running cycle has
an archive marker, and whether the marker is a removable orphan — from database
facts alone, without writing to the ledger. These tests pin the verdicts so the
tool cannot start guessing, and pin the read-only guarantee.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

SCRIPT = BACKEND / "diagnose_cycle_lifecycle_conflict.py"
SPEC = importlib.util.spec_from_file_location("cycle_lifecycle_diag", SCRIPT)
DIAG = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = DIAG
SPEC.loader.exec_module(DIAG)

import paper_schema_migrations as PSM  # noqa: E402

DAY = dt.date(2026, 9, 22)
SNAPSHOT = json.dumps({"_archive_format": "compact-ledger-v2", "paper_orders": []})

DDL = """
CREATE TABLE paper_cycles(
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL, capital REAL NOT NULL, risk_profile TEXT NOT NULL,
    started_at TEXT, ended_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    duration_days INTEGER, enabled_strategies TEXT
);
CREATE TABLE paper_archives(
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, cycle_key TEXT,
    reason TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE paper_orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, side TEXT, code TEXT,
    qty INTEGER, name TEXT, planned_price REAL, filled_price REAL, amount REAL,
    fees REAL, status TEXT, reason TEXT, risk_payload TEXT, created_at TEXT,
    executed_at TEXT, order_type TEXT, origin TEXT, strategy_id TEXT,
    strategy_version TEXT, strategy_checksum TEXT, cycle_id INTEGER,
    execution_status TEXT, execution_verified INTEGER
);
CREATE TABLE paper_orders_archive(id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER);
CREATE TABLE paper_fills(
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, account_id TEXT,
    side TEXT, code TEXT, qty INTEGER, price REAL, amount REAL, fees REAL,
    fill_date TEXT, quote_at TEXT, assumption TEXT
);
CREATE TABLE paper_position_lots(
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL, account_id TEXT,
    code TEXT, name TEXT, industry TEXT, qty INTEGER, remaining_qty INTEGER,
    cost REAL, acquired_at TEXT, available_date TEXT, asset_type TEXT,
    source_order_id INTEGER, cost_fee_included INTEGER, is_t_base INTEGER
);
CREATE TABLE paper_accounts(
    id TEXT PRIMARY KEY, name TEXT, status TEXT, initial_cash REAL, cash REAL,
    cycle_id INTEGER, updated_at TEXT, source_strategy TEXT
);
CREATE TABLE paper_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event TEXT NOT NULL,
    detail TEXT, created_at TEXT NOT NULL
);
"""

CYCLE = 259
ACCOUNT = "diag_acct"


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.sqlite3")
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(DDL)
        PSM._ensure_order_cycle_provenance_guards(conn)
        stamp = "2026-09-20 09:00:00"
        conn.execute(
            "INSERT INTO paper_cycles(id,cycle_key,status,capital,risk_profile,"
            "created_at,updated_at,started_at,duration_days,enabled_strategies) "
            "VALUES(?,?,?,?,?,?,?,?,0,'[]')",
            (CYCLE, "cycle-259", "running", 100000.0, "shared_pool",
             stamp, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO paper_accounts(id,name,status,initial_cash,cash,cycle_id,"
            "updated_at,source_strategy) VALUES(?,?,'running',100000.0,90000.0,?,?,?)",
            (ACCOUNT, "诊断账户", CYCLE, stamp, "trend"),
        )
        conn.execute(
            "INSERT INTO paper_audit(account_id,event,detail,created_at) "
            "VALUES(NULL,'cycle_created',?,'2026-09-20 09:00:00')",
            ("cycle-259：共享模拟资金池 100000.00 元",),
        )
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _conn(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _add_order_fill_lot(self, *, source_order_id=10):
        """A verified BUY order + fill, with a durable lot optionally linked."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO paper_orders(id,account_id,side,code,name,qty,"
                "planned_price,filled_price,amount,fees,status,reason,risk_payload,"
                "created_at,executed_at,order_type,origin,strategy_id,"
                "strategy_version,strategy_checksum,cycle_id,execution_status,"
                "execution_verified) VALUES(10,?,'buy','600519','诊断股',100,10.0,"
                "10.0,1000.0,1.0,'filled','probe','{}','2026-09-21 09:30:00',"
                "'2026-09-21 09:30:00','market','seed','diag','v1','csum',?,"
                "'verified',1)", (ACCOUNT, CYCLE),
            )
            conn.execute(
                "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,"
                "amount,fees,fill_date,quote_at,assumption) "
                "VALUES(10,?,'buy','600519',100,10.0,1000.0,1.0,'2026-09-21',"
                "'2026-09-21 09:30:00','probe')", (ACCOUNT,),
            )
            conn.execute(
                "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,"
                "industry,qty,remaining_qty,cost,acquired_at,available_date,"
                "asset_type,source_order_id,cost_fee_included,is_t_base) "
                "VALUES(?,?,?,'诊断股','测试',100,100,10.0,'2026-09-21 10:00:00',"
                "'2026-09-22','stock_t1',?,1,1)",
                (CYCLE, ACCOUNT, "600519", source_order_id),
            )
        finally:
            conn.close()

    def _add_archive(self, *, cycle_key="cycle-259"):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,"
                "created_at) VALUES(?,?,?,?,?)",
                (CYCLE, cycle_key, "probe", SNAPSHOT, "2026-09-21 15:00:00"),
            )
        finally:
            conn.close()

    # ─── verdicts ───

    def test_no_archive_row_reports_no_conflict(self):
        self._add_order_fill_lot()
        conn = self._conn()
        try:
            cycle = DIAG.resolve_cycle(conn, CYCLE)
            archives = DIAG.archive_rows(conn, CYCLE)
            lineage = DIAG.lot_lineage(conn, CYCLE)
            facts = DIAG.live_facts(conn, CYCLE)
            sim = DIAG.simulate_without_marker(self.db, CYCLE, DAY)
            verdict, reasons = DIAG.classify(conn, cycle, archives, lineage, facts, sim, None)
        finally:
            conn.close()
        self.assertEqual(verdict, "NO_ARCHIVE_CONFLICT")
        self.assertTrue(any("no archive row" in r for r in reasons))

    def test_matching_archive_row_over_live_facts_is_ambiguous(self):
        """The real P0 shape: cannot be proven from facts alone."""
        self._add_order_fill_lot()
        self._add_archive()
        conn = self._conn()
        try:
            cycle = DIAG.resolve_cycle(conn, CYCLE)
            archives = DIAG.archive_rows(conn, CYCLE)
            lineage = DIAG.lot_lineage(conn, CYCLE)
            facts = DIAG.live_facts(conn, CYCLE)
            sim = DIAG.simulate_without_marker(self.db, CYCLE, DAY)
            verdict, reasons = DIAG.classify(conn, cycle, archives, lineage, facts, sim, None)
        finally:
            conn.close()
        self.assertEqual(verdict, "AMBIGUOUS CYCLE LIFECYCLE CONFLICT")
        self.assertTrue(any("still hold facts" in r for r in reasons))

    def test_matching_archive_row_over_empty_ledger_is_orphan_candidate(self):
        """Single matching marker over an empty live ledger.

        Note this is deliberately *only* a candidate: an empty ledger has no
        fills, so the strict reader still cannot prove quantity==0 and stays
        unknown. What distinguishes this shape from the ambiguous one is that no
        cycle-owned fact survives — not that the read becomes fully verified.
        """
        self._add_archive()
        conn = self._conn()
        try:
            cycle = DIAG.resolve_cycle(conn, CYCLE)
            archives = DIAG.archive_rows(conn, CYCLE)
            lineage = DIAG.lot_lineage(conn, CYCLE)
            facts = DIAG.live_facts(conn, CYCLE)
            sim = DIAG.simulate_without_marker(self.db, CYCLE, DAY)
            verdict, reasons = DIAG.classify(conn, cycle, archives, lineage, facts,
                                             sim, None)
        finally:
            conn.close()
        self.assertEqual(verdict, "PROVEN_ORPHAN_CANDIDATE")
        self.assertEqual(sum(v for v in facts.values() if v), 0,
                         "orphan candidate requires no surviving cycle-owned facts")
        self.assertFalse(sim["archived"],
                         "removing the marker must clear the archived suppression")
        self.assertTrue(any("tables are empty" in r for r in reasons))
        # It must remain a *candidate*: the contract forbids auto-repair.
        self.assertTrue(any("must still be proven" in r for r in reasons))

    def test_cycle_key_mismatch_is_ambiguous_even_when_empty(self):
        self._add_archive(cycle_key="cycle-WRONG")
        conn = self._conn()
        try:
            cycle = DIAG.resolve_cycle(conn, CYCLE)
            archives = DIAG.archive_rows(conn, CYCLE)
            lineage = DIAG.lot_lineage(conn, CYCLE)
            facts = DIAG.live_facts(conn, CYCLE)
            sim = DIAG.simulate_without_marker(self.db, CYCLE, DAY)
            verdict, reasons = DIAG.classify(conn, cycle, archives, lineage, facts, sim, None)
        finally:
            conn.close()
        self.assertEqual(verdict, "AMBIGUOUS CYCLE LIFECYCLE CONFLICT")
        self.assertTrue(any("cycle_key" in r for r in reasons))

    def test_duplicate_archive_rows_are_duplicated_archive(self):
        self._add_archive()
        self._add_archive()
        conn = self._conn()
        try:
            cycle = DIAG.resolve_cycle(conn, CYCLE)
            archives = DIAG.archive_rows(conn, CYCLE)
            lineage = DIAG.lot_lineage(conn, CYCLE)
            facts = DIAG.live_facts(conn, CYCLE)
            sim = DIAG.simulate_without_marker(self.db, CYCLE, DAY)
            verdict, reasons = DIAG.classify(conn, cycle, archives, lineage, facts, sim, None)
        finally:
            conn.close()
        self.assertEqual(verdict, "DUPLICATED_ARCHIVE")

    def test_marker_holding_exclusive_history_is_never_an_orphan(self):
        """Section 8: a snapshot naming orders that survive nowhere is authority.

        Even over an otherwise empty live ledger, such a marker must stay
        ambiguous — removing it would destroy the only record of that history.
        """
        self._add_archive()
        # Rewrite the snapshot so it names an order id that exists nowhere else.
        conn = self._conn()
        try:
            snapshot = json.dumps({
                "_archive_format": "compact-ledger-v2",
                "paper_orders": [{"id": 424242, "account_id": ACCOUNT,
                                  "side": "buy", "code": "600519", "qty": 100}],
            })
            conn.execute("UPDATE paper_archives SET snapshot=? WHERE cycle_id=?",
                         (snapshot, CYCLE))
        finally:
            conn.close()

        conn = self._conn()
        try:
            cycle = DIAG.resolve_cycle(conn, CYCLE)
            archives = DIAG.archive_rows(conn, CYCLE)
            lineage = DIAG.lot_lineage(conn, CYCLE)
            facts = DIAG.live_facts(conn, CYCLE)
            # Mirror main(): the snapshot summary is an input to classify().
            snap = DIAG.snapshot_summary(archives[0].get("snapshot"))
            sim = DIAG.simulate_without_marker(self.db, CYCLE, DAY)
            verdict, reasons = DIAG.classify(conn, cycle, archives, lineage, facts,
                                             sim, snap)
        finally:
            conn.close()
        self.assertEqual(verdict, "AMBIGUOUS CYCLE LIFECYCLE CONFLICT")
        self.assertTrue(any("exclusive history" in r for r in reasons))
        self.assertEqual(sum(v for v in facts.values() if v), 0,
                         "the ledger really is empty; authority alone must block removal")

    # ─── the tool must not be able to modify the ledger ───

    def test_diagnostic_never_writes_to_the_database(self):
        """Main must leave the file byte-identical, including the marker."""
        self._add_order_fill_lot()
        self._add_archive()

        def fingerprint():
            import hashlib
            h = hashlib.sha256()
            with open(self.db, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        before = fingerprint()
        rc = DIAG.main(["--db", self.db, "--cycle-id", str(CYCLE),
                        "--asof", DAY.isoformat()])
        self.assertEqual(rc, 0)
        self.assertEqual(before, fingerprint(),
                         "diagnostic must be strictly read-only")

        conn = self._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(1) FROM paper_archives WHERE cycle_id=?",
                             (CYCLE,)).fetchone()[0], 1,
                "the marker must survive the diagnostic",
            )
        finally:
            conn.close()

    def test_missing_schema_does_not_crash(self):
        """A minimal/partial ledger degrades instead of raising."""
        bare = os.path.join(self.tmp.name, "bare.sqlite3")
        conn = sqlite3.connect(bare, isolation_level=None)
        conn.execute("CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT)")
        conn.execute("INSERT INTO paper_cycles VALUES(1,'c','running')")
        conn.commit()
        conn.close()
        ro = DIAG._connect_ro(bare)
        try:
            self.assertEqual(DIAG.resolve_cycle(ro, 1)["id"], 1)
            self.assertEqual(DIAG.archive_rows(ro, 1), [])
            self.assertEqual(DIAG.lot_lineage(ro, 1)["total"], 0)
        finally:
            ro.close()


if __name__ == "__main__":
    unittest.main()
