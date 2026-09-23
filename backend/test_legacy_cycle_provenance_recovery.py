# -*- coding: utf-8 -*-
"""Contracts for the explicit, evidence-only legacy cycle recovery tool."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "backend" / "recover_legacy_order_cycle_provenance.py"
SPEC = importlib.util.spec_from_file_location("legacy_cycle_recovery", SCRIPT)
RECOVERY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = RECOVERY
SPEC.loader.exec_module(RECOVERY)


class LegacyCycleProvenanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE paper_cycles(
                id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT,
                started_at TEXT, ended_at TEXT
            );
            CREATE TABLE paper_orders(
                id INTEGER PRIMARY KEY, account_id TEXT, side TEXT, code TEXT,
                qty INTEGER, created_at TEXT, executed_at TEXT, status TEXT,
                signal_id INTEGER, cycle_id INTEGER, execution_status TEXT,
                execution_verified INTEGER
            );
            CREATE TABLE paper_orders_archive(
                id INTEGER PRIMARY KEY, cycle_id INTEGER
            );
            CREATE TABLE paper_fills(
                id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT,
                side TEXT, code TEXT, qty INTEGER, price REAL, amount REAL,
                fees REAL, fill_date TEXT
            );
            CREATE TABLE paper_position_lots(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT,
                code TEXT, qty INTEGER, remaining_qty INTEGER,
                source_order_id INTEGER
            );
            CREATE TABLE paper_signals(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT, code TEXT
            );
            CREATE TABLE paper_capital_reservations(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, order_key TEXT,
                account_id TEXT, code TEXT, side TEXT, status TEXT
            );
            CREATE TABLE paper_risk_decisions(id INTEGER PRIMARY KEY, detail TEXT);
            CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, cycle_id INTEGER, cash REAL);
            CREATE TABLE paper_nav(id INTEGER PRIMARY KEY, account_id TEXT, cash REAL);
            CREATE TABLE paper_archives(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, cycle_key TEXT,
                snapshot TEXT, created_at TEXT
            );
            INSERT INTO paper_cycles VALUES
                (8,'cycle-8','running','2026-08-19 09:00:00',NULL),
                (9,'cycle-9','running','2026-09-20 09:00:00',NULL);
            INSERT INTO paper_accounts VALUES ('acct',9,75000.0);
            INSERT INTO paper_risk_decisions VALUES (1,'unchanged');
            INSERT INTO paper_nav VALUES (1,'acct',1234.5);
        """)
        RECOVERY.PSM._ensure_order_cycle_provenance_guards(self.conn)

    def tearDown(self):
        self.conn.close()

    def _order(self, *, order_id=10, cycle_id=None, side="buy", signal_id=None,
               code="600519", qty=100, created="2026-08-20 09:30:00"):
        insert_guard = "trg_paper_orders_cycle_provenance_insert"
        guard_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (insert_guard,),
        ).fetchone()[0]
        self.conn.execute(f'DROP TRIGGER "{insert_guard}"')
        try:
            self.conn.execute(
                "INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, "acct", side, code, qty, created, created, "filled",
                 signal_id, cycle_id, "verified", 1),
            )
        finally:
            self.conn.execute(guard_sql)
        self.conn.execute(
            "INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?)",
            (order_id, order_id, "acct", side, code, qty, 10.0, qty * 10.0,
             1.0, created[:10]),
        )

    def _lot(self, *, lot_id=1, cycle_id=8, order_id=10, code="600519", qty=100):
        self.conn.execute(
            "INSERT INTO paper_position_lots VALUES(?,?,?,?,?,?,?)",
            (lot_id, cycle_id, "acct", code, qty, qty, order_id),
        )

    def _related_lot_without_source_order(self, *, lot_id=1, cycle_id=8,
                                          code="600519", qty=100):
        self.conn.execute(
            "INSERT INTO paper_position_lots VALUES(?,?,?,?,?,?,?)",
            (lot_id, cycle_id, "acct", code, qty, qty, None),
        )

    def test_lot_source_proves_historical_cycle_not_current_account_binding(self):
        self._order()
        self._lot()
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["proven_count_for_requested_cycle"], 1)
        self.assertEqual(plan["proven"][0]["new_cycle_id"], 8)
        self.assertEqual(plan["proven"][0]["evidence"][0]["type"],
                         "durable_lot_source_order")

    def test_time_alone_is_unprovable_and_does_not_use_account_cycle(self):
        self._order()
        self._related_lot_without_source_order()
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["proven"], [])
        self.assertEqual(plan["unprovable"][0]["classification"], "UNPROVABLE")

    def test_conflicting_direct_evidence_is_ambiguous(self):
        self._order(signal_id=4)
        self._lot(cycle_id=8)
        self.conn.execute(
            "INSERT INTO paper_signals VALUES(?,?,?,?)", (4, 9, "acct", "600519")
        )
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["proven"], [])
        self.assertEqual(plan["ambiguous"][0]["order_id"], 10)

    def test_pruned_optional_signal_does_not_conflict_with_lot_proof(self):
        self._order(signal_id=404)
        self._lot()
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["proven_count_for_requested_cycle"], 1)
        self.assertEqual(plan["ambiguous"], [])

    def test_repair_scope_excludes_unrelated_legacy_orders(self):
        self._order()
        self._order(order_id=11, code="000001")
        self._lot()
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["runtime_relevant_order_ids"], [10])
        unrelated = next(item for item in plan["irrelevant"]
                         if item["order_id"] == 11)
        self.assertEqual(unrelated["classification"], "IRRELEVANT")

    def test_invalid_fill_identity_cannot_prove_a_lot_source(self):
        self._order()
        self._lot()
        self.conn.execute("UPDATE paper_fills SET side='sell' WHERE order_id=10")
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["proven"], [])
        self.assertIn("fill_order_side_mismatch", plan["ambiguous"][0]["conflicts"])

    def test_archive_snapshot_is_direct_cycle_owned_evidence(self):
        self._order()
        self._related_lot_without_source_order()
        archived_order = {
            "id": 10, "account_id": "acct", "side": "buy", "code": "600519",
            "qty": 100, "created_at": "2026-08-20 09:30:00",
            "executed_at": "2026-08-20 09:30:00", "status": "filled",
            "signal_id": None, "order_type": "market", "origin": "strategy",
        }
        snapshot = json.dumps({
            "_archive_format": "compact-ledger-v2",
            "paper_orders": [archived_order],
        })
        self.conn.execute(
            "INSERT INTO paper_archives VALUES(?,?,?,?,?)",
            (1, 8, "cycle-8", snapshot, "2026-08-21 15:00:00"),
        )
        plan = RECOVERY.build_plan(self.conn, 8)
        self.assertEqual(plan["proven"][0]["evidence"][0]["type"],
                         "cycle_owned_archive_snapshot")

    def test_apply_changes_only_proven_cycle_ids_and_is_idempotent(self):
        self._order()
        self._lot()
        plan = RECOVERY.build_plan(self.conn, 8)
        before_fills = RECOVERY._table_fingerprint(self.conn, "paper_fills")
        before_lots = RECOVERY._table_fingerprint(self.conn, "paper_position_lots")
        self.assertEqual(RECOVERY.apply_plan(self.conn, plan), 1)
        self.assertEqual(self.conn.execute(
            "SELECT cycle_id FROM paper_orders WHERE id=10"
        ).fetchone()[0], 8)
        self.assertEqual(before_fills,
                         RECOVERY._table_fingerprint(self.conn, "paper_fills"))
        self.assertEqual(before_lots,
                         RECOVERY._table_fingerprint(self.conn, "paper_position_lots"))
        self.assertEqual(RECOVERY.apply_plan(self.conn, plan), 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE paper_orders SET cycle_id=9 WHERE id=10")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO paper_orders VALUES(11,'acct','buy','600519',100,"
                "'2026-08-20','2026-08-20','filled',NULL,NULL,'verified',1)"
            )

    def test_plan_tamper_or_database_drift_is_rejected(self):
        self._order()
        self._lot()
        plan = RECOVERY.build_plan(self.conn, 8)
        self.conn.execute("UPDATE paper_fills SET amount=999 WHERE order_id=10")
        with self.assertRaisesRegex(RECOVERY.RecoveryError, "changed since"):
            RECOVERY.apply_plan(self.conn, plan)

    def test_trigger_restore_failure_rolls_back_the_entire_apply(self):
        self._order()
        self._lot()
        plan = RECOVERY.build_plan(self.conn, 8)
        original = RECOVERY.PSM._ensure_order_cycle_provenance_guards
        calls = 0

        def fail_on_restore(conn):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated trigger restore failure")
            return original(conn)

        with mock.patch.object(
            RECOVERY.PSM, "_ensure_order_cycle_provenance_guards", fail_on_restore
        ):
            with self.assertRaisesRegex(RuntimeError, "restore failure"):
                RECOVERY.apply_plan(self.conn, plan)
        self.assertIsNone(self.conn.execute(
            "SELECT cycle_id FROM paper_orders WHERE id=10"
        ).fetchone()[0])
        self.assertEqual(RECOVERY._trigger_sql(self.conn),
                         RECOVERY._canonical_trigger_sql())


if __name__ == "__main__":
    unittest.main()
