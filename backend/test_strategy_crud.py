# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry


DSL = {
    "op": "gt", "left": {"op": "field", "name": "close"},
    "right": {"op": "indicator", "name": "ma", "window": 20},
}


class StrategyCrudTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.directory.name, "paper.sqlite3"))
        registry.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def test_user_draft_validates_activates_and_appears_in_active_ids(self):
        created = registry.create_user_definition(self.conn, "user_crud_alpha", "CRUD alpha", dsl_ast=DSL)
        readiness = registry.runtime_readiness(self.conn, created.id)
        self.assertTrue(readiness["runtime_ready"])
        registry.transition(self.conn, created.id, "validated", expected_status="draft")
        active = registry.transition(self.conn, created.id, "active", expected_status="validated")
        self.assertTrue(active.supports_new_cycle)
        self.assertIn(created.id, registry.active_ids(conn=self.conn))

    def test_user_without_dsl_cannot_activate(self):
        created = registry.create_user_definition(self.conn, "user_no_dsl", "No DSL")
        self.assertFalse(registry.runtime_readiness(self.conn, created.id)["runtime_ready"])
        registry.transition(self.conn, created.id, "validated", expected_status="draft")
        with self.assertRaisesRegex(ValueError, "runtime is not ready"):
            registry.transition(self.conn, created.id, "active", expected_status="validated")

    def test_unused_draft_can_be_hard_deleted_but_history_forces_archive(self):
        draft = registry.create_user_definition(self.conn, "user_delete_me", "Delete me", dsl_ast=DSL)
        self.assertTrue(registry.hard_delete_unused_draft(self.conn, draft.id)["deleted"])
        self.assertIsNone(registry.get(draft.id, conn=self.conn))

        used = registry.create_user_definition(self.conn, "user_keep_me", "Keep me", dsl_ast=DSL)
        self.conn.execute("CREATE TABLE paper_signals(account_id TEXT, strategy_id TEXT)")
        self.conn.execute("INSERT INTO paper_signals VALUES(?,?)", (used.id, used.id))
        with self.assertRaisesRegex(ValueError, "historical references"):
            registry.hard_delete_unused_draft(self.conn, used.id)
        archived = registry.archive_definition(self.conn, used.id)
        self.assertEqual(archived.status, "archived")

    def test_dsl_edit_appends_immutable_version(self):
        created = registry.create_user_definition(self.conn, "user_edit_dsl", "Edit DSL", dsl_ast=DSL)
        original = registry.get_version(created.id, 1, conn=self.conn)
        updated = registry.save_definition(
            self.conn, created.id,
            {"dsl_ast": {"op": "lt", "left": {"op": "field", "name": "close"},
                         "right": {"op": "indicator", "name": "ema", "window": 20}}},
            expected_version=1,
        )
        self.assertEqual(updated.version, 2)
        self.assertEqual(registry.get_version(created.id, 1, conn=self.conn), original)


if __name__ == "__main__":
    unittest.main()
