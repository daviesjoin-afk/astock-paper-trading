# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry
import paper_trading as paper


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

    def test_old_pr01_definition_table_is_readable_before_version_migration(self):
        self.conn.execute("DROP TABLE strategy_definitions")
        self.conn.execute(
            """CREATE TABLE strategy_definitions (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL, implementation_key TEXT NOT NULL,
                supports_new_cycle INTEGER NOT NULL, description TEXT NOT NULL,
                metadata TEXT NOT NULL, sort_order INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """INSERT INTO strategy_definitions VALUES(
                'legacy_strategy','Legacy','builtin','active','legacy_strategy',1,
                '','{}',1,'2026-09-08','2026-09-08'
            )"""
        )
        self.assertEqual(
            registry.list_definitions(conn=self.conn)[0].current_version, None,
        )

    def test_user_definition_can_be_queried_and_follows_lifecycle(self):
        created = registry.create_user_definition(
            self.conn, "user_momentum", "User Momentum", actor="unit-test",
            dsl_ast={"op": "gt", "left": {"op": "field", "name": "close"},
                     "right": {"op": "indicator", "name": "ma", "window": 20}},
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
        self.assertTrue(active.supports_new_cycle)
        self.assertIn(created.id, registry.active_ids(conn=self.conn))
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

    def test_bootstrap_fallback_applies_filters_and_rejects_invalid_values(self):
        missing_path = os.path.join(self.directory.name, "missing.sqlite3")
        self.assertEqual(
            registry.list_definitions(db_path=missing_path, origins=("user",)), (),
        )
        with self.assertRaisesRegex(ValueError, "invalid strategy origin"):
            registry.list_definitions(db_path=missing_path, origins=("invalid",))

    def test_non_object_metadata_is_rejected_before_persisting(self):
        with self.assertRaisesRegex(ValueError, "metadata must be an object"):
            registry.create_user_definition(
                self.conn, "user_bad_metadata", "Bad Metadata", metadata=["not", "an", "object"],
            )

    def test_paused_definition_is_excluded_from_a_new_cycle_without_restart(self):
        paper_path = os.path.join(self.directory.name, "paper-cycle.sqlite3")
        with (
            mock.patch.object(paper, "DB_PATH", paper_path),
            mock.patch.object(paper, "_benchmark_close", return_value=None),
            mock.patch.object(paper, "_RUNNER_BOOT_RECOVERED", False),
        ):
            paper.init_db()
            conn = sqlite3.connect(paper_path)
            try:
                registry.transition(
                    conn, "tq_breakout", "paused", expected_status="active", actor="test",
                )
                conn.commit()
            finally:
                conn.close()
            paper.reset_cycle(100000, include_dashboard=False)
            conn = sqlite3.connect(paper_path)
            try:
                enabled = conn.execute(
                    "SELECT enabled_strategies FROM paper_cycles ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertNotIn("tq_breakout", enabled)

    def test_query_endpoint_returns_database_definitions(self):
        # PR-52：策略定义的唯一 API 是 ``/api/strategies``（Strategy Admin）。
        # 旧的 ``/api/strategy-definitions`` 与生命周期别名一并删除，因此这里
        # 改为断言 canonical 路由同样返回数据库里的定义与生命周期状态。
        import api_strategies as API

        registry.create_user_definition(self.conn, "user_api", "API Strategy")
        self.conn.commit()
        original_path = paper.DB_PATH
        try:
            paper.DB_PATH = self.path
            payload = API.list_strategies(origin="user", include_archived=True)
            everything = API.list_strategies(include_archived=True)
        finally:
            paper.DB_PATH = original_path
        self.assertEqual([row["id"] for row in payload["items"]], ["user_api"])
        self.assertEqual(payload["items"][0]["status"], "draft")
        self.assertEqual(payload["items"][0]["origin"], "user")
        # 内置与自定义来自同一份注册表（旧端点用 origins 字段表达同一事实）。
        ids = {row["id"] for row in everything["items"]}
        self.assertIn("trend_pullback", ids)
        self.assertEqual(everything["summary"]["user"], 1)
        self.assertGreaterEqual(everything["summary"]["builtin"], 5)


if __name__ == "__main__":
    unittest.main()
