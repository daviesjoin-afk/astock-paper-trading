# -*- coding: utf-8 -*-
"""Contracts for the explicit, evidence-only legacy cycle recovery tool."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
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


class CliCyclePlanMismatchTests(unittest.TestCase):
    """LEG-CYCLE-CLI-01..04: the CLI must never apply a different cycle than requested.

    Regression for the review finding on ``--apply``: the operator states the
    cycle under review via ``--cycle-id``, so a reviewed plan for another cycle
    must fail closed before any writer transaction, trigger change or UPDATE.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ledger.sqlite")
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
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
            CREATE TABLE paper_orders_archive(id INTEGER PRIMARY KEY, cycle_id INTEGER);
            CREATE TABLE paper_fills(
                id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT,
                side TEXT, code TEXT, qty INTEGER, price REAL, amount REAL,
                fees REAL, fill_date TEXT
            );
            CREATE TABLE paper_position_lots(
                id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT,
                code TEXT, qty INTEGER, remaining_qty INTEGER, source_order_id INTEGER
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
        """)
        RECOVERY.PSM._ensure_order_cycle_provenance_guards(conn)
        # Two legacy orders, each with a durable lot proving a *different*
        # cycle. Cycle 9 therefore has a non-empty provable set, which is what
        # makes "requested cycle 8 + reviewed cycle-9 plan" a real mutation
        # instead of a harmless no-op.
        insert_guard = "trg_paper_orders_cycle_provenance_insert"
        guard_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (insert_guard,),
        ).fetchone()[0]
        conn.execute(f'DROP TRIGGER "{insert_guard}"')
        try:
            conn.execute(
                "INSERT INTO paper_orders VALUES(10,'acct','buy','600519',100,"
                "'2026-08-20 09:30:00','2026-08-20 09:30:00','filled',NULL,NULL,"
                "'verified',1)"
            )
            conn.execute(
                "INSERT INTO paper_orders VALUES(11,'acct','buy','000001',200,"
                "'2026-09-21 09:30:00','2026-09-21 09:30:00','filled',NULL,NULL,"
                "'verified',1)"
            )
        finally:
            conn.execute(guard_sql)
        conn.execute(
            "INSERT INTO paper_fills VALUES(10,10,'acct','buy','600519',100,"
            "10.0,1000.0,1.0,'2026-08-20')"
        )
        conn.execute(
            "INSERT INTO paper_fills VALUES(11,11,'acct','buy','000001',200,"
            "5.0,1000.0,1.0,'2026-09-21')"
        )
        conn.execute(
            "INSERT INTO paper_position_lots VALUES(1,8,'acct','600519',100,100,10)"
        )
        conn.execute(
            "INSERT INTO paper_position_lots VALUES(2,9,'acct','000001',200,200,11)"
        )
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _fingerprints(self):
        """Fingerprint every table the repair must leave untouched.

        ``_table_fingerprint`` intentionally omits ``paper_orders.cycle_id``
        because that is the one column apply is allowed to change, so it cannot
        by itself prove "no cycle was reassigned". The cycle-id-inclusive digest
        below closes that gap for the mismatch (no-mutation) case.
        """
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            fingerprints = {
                table: RECOVERY._table_fingerprint(conn, table)
                for table in ("paper_orders", "paper_fills", "paper_position_lots",
                              "paper_accounts", "paper_cycles")
            }
            digest = hashlib.sha256()
            for row in conn.execute("SELECT id,cycle_id FROM paper_orders ORDER BY id"):
                digest.update(f"{int(row[0])}:{row[1]}\n".encode("utf-8"))
            fingerprints["paper_orders_including_cycle_id"] = digest.hexdigest()
            return fingerprints
        finally:
            conn.close()

    def _cycle_ids(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            return [
                (int(row[0]), row[1])
                for row in conn.execute("SELECT id, cycle_id FROM paper_orders ORDER BY id")
            ]
        finally:
            conn.close()

    def _triggers(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            return sorted(
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
                )
            )
        finally:
            conn.close()

    def _write_plan(self, cycle_id):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            plan = RECOVERY.build_plan(conn, cycle_id)
        finally:
            conn.close()
        path = os.path.join(self.tmp.name, f"plan-{cycle_id}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(RECOVERY._canonical_json(plan) + "\n")
        return path, plan

    def test_cli_01_requested_cycle_mismatching_plan_is_refused(self):
        """LEG-CYCLE-CLI-01: --cycle-id 8 with a reviewed cycle-9 plan fails closed."""
        plan_path, plan = self._write_plan(9)
        self.assertEqual(plan["cycle_id"], 9)
        self.assertEqual(plan["proven_count_for_requested_cycle"], 1,
                         "cycle-9 plan must carry a real change to be meaningful")
        fingerprints_before = self._fingerprints()
        triggers_before = self._triggers()

        status = RECOVERY.main([
            "--db", self.db_path, "--cycle-id", "8",
            "--apply", "--plan", plan_path,
        ])

        self.assertNotEqual(status, 0, "mismatched requested cycle must fail closed")
        self.assertEqual(self._cycle_ids(), [(10, None), (11, None)],
                         "paper_orders must be unchanged on mismatch")
        self.assertEqual(triggers_before, self._triggers(),
                         "triggers must be unchanged on mismatch")
        self.assertEqual(fingerprints_before, self._fingerprints(),
                         "protected ledger fingerprints must be identical on mismatch")

    def test_cli_02_matching_cycle_enters_apply(self):
        """LEG-CYCLE-CLI-02: requested cycle == plan cycle proceeds normally."""
        plan_path, _ = self._write_plan(8)
        status = RECOVERY.main([
            "--db", self.db_path, "--cycle-id", "8",
            "--apply", "--plan", plan_path,
        ])
        self.assertEqual(status, 0)
        self.assertEqual(self._cycle_ids(), [(10, 8), (11, None)],
                         "only the requested cycle's order may be repaired")

    def test_cli_03_mismatch_never_calls_apply_plan(self):
        """LEG-CYCLE-CLI-03: apply_plan is not reached at all on mismatch."""
        plan_path, _ = self._write_plan(9)
        with mock.patch.object(RECOVERY, "apply_plan") as spy:
            status = RECOVERY.main([
                "--db", self.db_path, "--cycle-id", "8",
                "--apply", "--plan", plan_path,
            ])
        self.assertNotEqual(status, 0)
        spy.assert_not_called()
        self.assertEqual(self._cycle_ids(), [(10, None), (11, None)])

    def test_cli_04_mismatch_leaves_database_bit_identical(self):
        """LEG-CYCLE-CLI-04: every table fingerprint is identical after refusal."""
        plan_path, _ = self._write_plan(9)
        before = self._fingerprints()
        RECOVERY.main([
            "--db", self.db_path, "--cycle-id", "8",
            "--apply", "--plan", plan_path,
        ])
        self.assertEqual(before, self._fingerprints())

    def test_cli_mismatch_message_names_both_cycles(self):
        plan_path, _ = self._write_plan(9)
        with self.assertRaisesRegex(
            RECOVERY.RecoveryError,
            r"requested=8 plan=9",
        ):
            RECOVERY._main([
                "--db", self.db_path, "--cycle-id", "8",
                "--apply", "--plan", plan_path,
            ])


if __name__ == "__main__":
    unittest.main()
