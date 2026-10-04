# -*- coding: utf-8 -*-
"""R34-C migration and strict pending-order intent owner regressions."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_schema_migrations as PSM
import portfolio_order_intents as POI


class PortfolioOrderIntentOwnerTests(unittest.TestCase):
    def _base_orders(self, *, row_factory=True):
        conn = sqlite3.connect(":memory:")
        if row_factory:
            conn.row_factory = sqlite3.Row
        columns = (
            "id INTEGER PRIMARY KEY, account_id TEXT, side TEXT, code TEXT, "
            "status TEXT, reason TEXT, created_at TEXT, cycle_id INTEGER, "
            "strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT"
        )
        conn.execute(f"CREATE TABLE paper_orders({columns})")
        conn.execute(f"CREATE TABLE paper_orders_archive({columns})")
        return conn

    def test_rc4_migration_adds_nullable_fields_to_active_and_archive_without_backfill(self):
        conn = self._base_orders()
        try:
            conn.execute(
                "INSERT INTO paper_orders(id,account_id,side,code,status,reason,"
                "created_at,cycle_id) VALUES(1,'s1','sell','AAA','pending_limit',"
                "'RISK_EXIT from prose','2026-10-03T09:30:00+08:00',7)")
            PSM.ensure_order_allocation_provenance(conn)
            for table in ("paper_orders", "paper_orders_archive"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertTrue({"allocation_intent_kind", "portfolio_snapshot_id",
                                 "allocation_plan_id", "allocation_plan_fingerprint",
                                 "allocation_policy_version"}.issubset(columns))
            row = conn.execute(
                "SELECT allocation_intent_kind,portfolio_snapshot_id,allocation_plan_id "
                "FROM paper_orders WHERE id=1").fetchone()
            self.assertEqual((None, None, None), tuple(row))
            self.assertEqual(
                [row[1] for row in conn.execute("PRAGMA table_info(paper_orders)")][-5:],
                [row[1] for row in conn.execute("PRAGMA table_info(paper_orders_archive)")][-5:],
            )
        finally:
            conn.close()

    def test_rc5_legacy_pending_sell_is_unknown_not_risk_exit(self):
        conn = self._base_orders()
        try:
            PSM.ensure_order_allocation_provenance(conn)
            conn.execute(
                "INSERT INTO paper_orders(id,account_id,side,code,status,reason,"
                "created_at,cycle_id) VALUES(1,'s1','sell','AAA','pending_limit',"
                "'RISK_EXIT','2026-10-03T09:30:00+08:00',7)")
            evidence = POI.pending_resource_intents(
                conn, cycle_id=7, asof_day="2026-10-03",
                decision_at="2026-10-03T09:31:00+08:00",
            )
            self.assertEqual("UNAVAILABLE", evidence["status"])
            self.assertEqual([], evidence["intents"])
            self.assertEqual("legacy_or_invalid_pending_intent_kind",
                             evidence["unknown_orders"][0]["reason"])
        finally:
            conn.close()

    def test_rc6_pending_reader_query_error_raises_controlled_evidence_failure(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaisesRegex(
                    POI.PendingIntentEvidenceUnavailable,
                    "pending_order_query_unavailable"):
                POI.pending_resource_intents(
                    conn, cycle_id=7, asof_day="2026-10-03",
                    decision_at="2026-10-03T09:31:00+08:00",
                )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
