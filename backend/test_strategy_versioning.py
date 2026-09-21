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


    def _try_insert(self, sql, params=()):
        try:
            self.conn.execute(sql, params)
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def test_db_strat_1_signal_all_null_rejected(self):
        self.assertFalse(self._try_insert(
            "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,"
            "payload,status,created_at) VALUES(?,?,?,?,?,?,?)",
            ("tq_breakout", "2026-09-10", "2026-09-10", "600001",
             "{}", "pending", "2026-09-10 09:00:00"),
        ))

    def test_db_strat_guard_scope_matrix(self):
        cycle_id = self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id='tq_breakout'"
        ).fetchone()[0]
        account = "tq_breakout"
        order_sql = (
            "INSERT INTO paper_orders(account_id,side,code,qty,status,risk_payload,"
            "created_at,order_type,origin,cycle_id,strategy_id,strategy_version,"
            "strategy_checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        decision_sql = (
            "INSERT INTO paper_risk_decisions(account_id,side,decision,payload,created_at,"
            "strategy_id,strategy_version,strategy_checksum) VALUES(?,?,?,?,?,?,?,?)"
        )
        audit_sql = (
            "INSERT INTO paper_audit(account_id,event,created_at,strategy_id,"
            "strategy_version,strategy_checksum) VALUES(?,?,?,?,?,?)"
        )
        # DB-STRAT-2: BUY all-NULL rejected.
        self.assertFalse(self._try_insert(
            order_sql, (account, "buy", "600001", 100, "pending_execution", "{}",
                        "2026-09-10 09:00:00", "market", "strategy", cycle_id,
                        None, None, None)))
        # DB-STRAT-3: SELL pending_execution with explicit cycle allows unknown.
        self.assertTrue(self._try_insert(
            order_sql, (account, "sell", "600001", 100, "pending_execution", "{}",
                        "2026-09-10 09:00:00", "market", "strategy", cycle_id,
                        None, None, None)))
        # DB-STRAT-4: SELL without cycle cannot use unknown.
        self.assertFalse(self._try_insert(
            order_sql, (account, "sell", "600002", 100, "pending_execution", "{}",
                        "2026-09-10 09:00:00", "market", "strategy", None,
                        None, None, None)))
        # DB-STRAT-5 / 6: SELL risk decision allows unknown; BUY does not.
        self.assertTrue(self._try_insert(
            decision_sql, (account, "sell", "filled", "{}", "2026-09-10 10:00:00",
                           None, None, None)))
        self.assertFalse(self._try_insert(
            decision_sql, (account, "buy", "blocked", "{}", "2026-09-10 10:00:00",
                           None, None, None)))
        # DB-STRAT-7 / 8: only explicit causal SELL audit events allow unknown.
        self.assertTrue(self._try_insert(
            audit_sql, (account, "sell_filled", "2026-09-10 10:00:00", None, None, None)))
        self.assertTrue(self._try_insert(
            audit_sql, (account, "protective_exit_recovery_watch",
                        "2026-09-10 10:00:00", None, None, None)))
        self.assertTrue(self._try_insert(
            audit_sql, (account, "quality_rotation",
                        "2026-09-10 10:00:00", None, None, None)))
        self.assertTrue(self._try_insert(
            audit_sql, (account, "concentration_rotation",
                        "2026-09-10 10:00:00", None, None, None)))
        self.assertTrue(self._try_insert(
            audit_sql, (account, "permission_scope_exit",
                        "2026-09-10 10:00:00", None, None, None)))
        self.assertFalse(self._try_insert(
            audit_sql, (account, "some_unrelated_event", "2026-09-10 10:00:00",
                        None, None, None)))
        # DB-STRAT-9: partial stamps remain rejected even on allowed shapes.
        self.assertFalse(self._try_insert(
            order_sql, (account, "sell", "600003", 100, "pending_execution", "{}",
                        "2026-09-10 09:00:00", "market", "strategy", cycle_id,
                        account, None, None)))
        self.assertFalse(self._try_insert(
            decision_sql, (account, "sell", "filled", "{}", "2026-09-10 10:00:00",
                           account, None, None)))
        # DB-STRAT-10: complete but forged stamps remain rejected.
        self.assertFalse(self._try_insert(
            audit_sql, (account, "sell_filled", "2026-09-10 10:00:00",
                        account, 1, "forged-checksum")))

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
                   strategy_id,strategy_version,strategy_checksum,cycle_id)
               VALUES('tq_breakout','buy','000001',100,'filled','{}','2026-09-08',?,?,?,?)""",
            pinned + (cycle_id,),
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
        cycle_id = self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id='tq_breakout'"
        ).fetchone()[0]
        self.conn.execute(
            """INSERT INTO paper_orders(
                   account_id,side,code,qty,status,risk_payload,created_at,
                   strategy_id,strategy_version,strategy_checksum,cycle_id)
               VALUES('tq_breakout','buy','000001',100,'filled','{}','2026-09-08',?,?,?,?)""",
            stamp + (cycle_id,),
        )
        self.conn.execute("INSERT INTO paper_orders_archive SELECT * FROM paper_orders")
        archived = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum FROM paper_orders_archive"
        ).fetchone()
        self.assertEqual(tuple(archived), stamp)

    def test_pr01_metadata_is_normalized_during_version_migration(self):
        path = os.path.join(self.temporary.name, "pr01.sqlite3")
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """CREATE TABLE strategy_definitions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL, implementation_key TEXT NOT NULL,
                    supports_new_cycle INTEGER NOT NULL, description TEXT NOT NULL,
                    metadata TEXT NOT NULL, sort_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO strategy_definitions VALUES(
                    'user_legacy','Legacy','user','draft','user_legacy',0,'',
                    '{"risk": 1}',1000,'2026-09-08','2026-09-08'
                )"""
            )
            registry.ensure_schema(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT metadata,current_version FROM strategy_definitions WHERE id='user_legacy'"
                ).fetchone(),
                ('{"risk":1}', 1),
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
