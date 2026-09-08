# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as paper
import strategy_registry as registry


class StrategyVersioningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temporary.name, "paper.sqlite3")
        self.patches = (
            mock.patch.object(paper, "DB_PATH", self.path),
            mock.patch.object(paper, "_benchmark_close", return_value=None),
            mock.patch.object(paper, "_RUNNER_BOOT_RECOVERED", False),
        )
        for patcher in self.patches:
            patcher.start()
        paper.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def test_active_save_appends_v2_and_v1_remains_immutable(self):
        original = registry.get_version("tq_breakout", 1, conn=self.conn)
        version_two = registry.save_definition(
            self.conn, "tq_breakout", {"description": "version two"},
            expected_version=1, actor="test", change_note="test append",
        )
        self.conn.commit()

        self.assertEqual(version_two.version, 2)
        self.assertNotEqual(version_two.checksum, original.checksum)
        self.assertEqual(
            registry.get_version("tq_breakout", 1, conn=self.conn), original,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute(
                "UPDATE paper_strategy_versions SET description='changed' "
                "WHERE strategy_id='tq_breakout' AND version=1"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute(
                "DELETE FROM paper_strategy_versions "
                "WHERE strategy_id='tq_breakout' AND version=1"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "require a new strategy version"):
            self.conn.execute(
                "UPDATE strategy_definitions SET name='in-place edit' "
                "WHERE id='tq_breakout'"
            )

    def test_existing_cycle_and_legacy_order_stay_resolvable_as_v1_after_v2(self):
        cycle_id = self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id='tq_breakout'"
        ).fetchone()[0]
        pinned = registry.stamp_for_account(
            self.conn, "tq_breakout", cycle_id=cycle_id,
        )
        self.assertEqual(pinned[1], 1)
        self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,qty,status,risk_payload,created_at,
                   strategy_id,strategy_version,strategy_checksum)
               VALUES('tq_breakout','buy','000001',100,'filled','{}','2026-09-08',?,?,?)""",
            pinned,
        )
        registry.save_definition(
            self.conn, "tq_breakout", {"description": "new head"}, expected_version=1,
        )
        stamped = dict(self.conn.execute(
            "SELECT account_id,strategy_id,strategy_version,strategy_checksum "
            "FROM paper_orders ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertEqual(registry.resolve_record_version(self.conn, stamped).version, 1)
        self.assertEqual(registry.stamp_for_account(self.conn, "tq_breakout")[1], 1)

        legacy = {"account_id": "tq_breakout", "strategy_id": None,
                  "strategy_version": None, "strategy_checksum": None}
        self.assertEqual(registry.resolve_record_version(self.conn, legacy).version, 1)

    def test_invalid_and_mutated_evidence_stamps_fail_closed(self):
        stamp = registry.stamp_for_account(self.conn, "tq_breakout")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid strategy version stamp"):
            self.conn.execute(
                """INSERT INTO paper_risk_decisions(
                       account_id,side,decision,payload,created_at,strategy_id)
                   VALUES('tq_breakout','buy','blocked','{}','2026-09-08','tq_breakout')"""
            )
        self.conn.execute(
            """INSERT INTO paper_audit(
                   account_id,event,created_at,strategy_id,strategy_version,strategy_checksum)
               VALUES('tq_breakout','test','2026-09-08',?,?,?)""",
            stamp,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute(
                "UPDATE paper_audit SET strategy_version=2 WHERE event='test'"
            )
        with self.assertRaisesRegex(ValueError, "partial strategy version stamp"):
            registry.resolve_record_version(self.conn, {
                "account_id": "tq_breakout", "strategy_id": "tq_breakout",
                "strategy_version": None, "strategy_checksum": None,
            })

    def test_clone_copies_only_explicit_source_definition_with_provenance(self):
        registry.save_definition(
            self.conn, "tq_breakout", {"description": "source v2"}, expected_version=1,
        )
        clone = registry.clone_definition(
            self.conn, "tq_breakout", 1, "user_breakout_clone",
            name="Breakout clone", actor="test",
        )
        version = registry.get_version("user_breakout_clone", 1, conn=self.conn)
        self.assertEqual((clone.origin, clone.status, clone.current_version), ("user", "draft", 1))
        self.assertEqual(version.cloned_from_strategy_id, "tq_breakout")
        self.assertEqual(version.cloned_from_version, 1)
        self.assertEqual(version.definition["description"], "")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM paper_accounts WHERE id='user_breakout_clone'"
            ).fetchone()[0], 0,
        )

    def test_live_and_archive_column_order_remains_identical(self):
        def columns(table):
            return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")]

        self.assertEqual(columns("paper_orders"), columns("paper_orders_archive"))
        self.assertEqual(columns("paper_signals"), columns("paper_signals_archive"))

        stamp = registry.stamp_for_account(self.conn, "tq_breakout")
        self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,qty,status,risk_payload,created_at,
                   strategy_id,strategy_version,strategy_checksum)
               VALUES('tq_breakout','buy','000001',100,'filled','{}','2026-09-08',?,?,?)""",
            stamp,
        )
        self.conn.execute("INSERT INTO paper_orders_archive SELECT * FROM paper_orders")
        archived = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum FROM paper_orders_archive"
        ).fetchone()
        self.assertEqual(tuple(archived), stamp)


if __name__ == "__main__":
    unittest.main()
