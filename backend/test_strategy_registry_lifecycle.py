# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry


class StrategyDefinitionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "paper.sqlite3")
        self.conn = sqlite3.connect(self.path)
        registry.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def test_builtin_migration_is_idempotent_and_preserves_state(self):
        rows = registry.list_definitions(conn=self.conn)
        self.assertEqual([row.id for row in rows], [
            "tq_breakout", "trend_pullback", "sector_rotation",
            "reported_profit_breakout", "main_force_top10",
        ])
        self.assertTrue(all(row.origin == "builtin" for row in rows))
        self.conn.execute(
            "UPDATE strategy_definitions SET lifecycle_status='paused',supports_new_cycle=0 "
            "WHERE id='tq_breakout'"
        )
        registry.ensure_schema(self.conn)
        self.assertEqual(registry.get("tq_breakout", conn=self.conn).status, "paused")

    def test_user_definition_can_be_queried_and_follows_lifecycle(self):
        created = registry.create_user_definition(
            self.conn, "user_momentum", "User Momentum", actor="unit-test",
        )
        self.assertEqual((created.origin, created.status), ("user", "draft"))
        validated = registry.transition(
            self.conn, created.id, "validated", expected_status="draft",
            reason="offline checks passed", actor="unit-test",
        )
        active = registry.transition(
            self.conn, created.id, "active", expected_status="validated",
            actor="unit-test",
        )
        self.assertEqual(validated.status, "validated")
        self.assertFalse(active.supports_new_cycle)
        self.assertNotIn(created.id, registry.active_ids(conn=self.conn))
        self.assertEqual(len(registry.lifecycle_events(self.conn, created.id)), 3)

    def test_invalid_transition_is_rejected(self):
        registry.create_user_definition(self.conn, "user_value", "User Value")
        with self.assertRaisesRegex(ValueError, "invalid lifecycle transition"):
            registry.transition(self.conn, "user_value", "active")

    def test_catalogue_migration_does_not_rewrite_historical_ids(self):
        self.conn.execute("CREATE TABLE paper_accounts(id TEXT PRIMARY KEY, name TEXT)")
        self.conn.execute("CREATE TABLE historical_events(strategy_id TEXT, payload TEXT)")
        self.conn.execute("INSERT INTO paper_accounts VALUES('legacy_alpha','Legacy')")
        self.conn.execute("INSERT INTO historical_events VALUES('legacy_alpha','{}')")
        before_account = self.conn.execute("SELECT id FROM paper_accounts").fetchall()
        before_history = self.conn.execute("SELECT strategy_id FROM historical_events").fetchall()
        registry.ensure_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT id FROM paper_accounts").fetchall(), before_account)
        self.assertEqual(
            self.conn.execute("SELECT strategy_id FROM historical_events").fetchall(), before_history,
        )

    def test_filters_and_archived_visibility(self):
        registry.create_user_definition(self.conn, "user_archived", "Archived")
        registry.transition(self.conn, "user_archived", "archived")
        visible = registry.list_definitions(
            conn=self.conn, origins=("user",), include_archived=False,
        )
        self.assertEqual(visible, ())
        archived = registry.list_definitions(
            conn=self.conn, statuses=("archived",),
        )
        self.assertEqual([row.id for row in archived], ["user_archived"])

    def test_query_endpoint_returns_database_definitions(self):
        import main

        registry.create_user_definition(self.conn, "user_api", "API Strategy")
        self.conn.commit()
        original_path = main.P.DB_PATH
        try:
            main.P.DB_PATH = self.path
            payload = main.strategy_definitions(
                origin="user", status=None, include_archived=True,
            )
        finally:
            main.P.DB_PATH = original_path
        self.assertEqual([row["id"] for row in payload["strategies"]], ["user_api"])
        self.assertEqual(payload["strategies"][0]["status"], "draft")
        self.assertEqual(payload["origins"], ["builtin", "user"])


if __name__ == "__main__":
    unittest.main()
