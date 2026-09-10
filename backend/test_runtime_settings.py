# -*- coding: utf-8 -*-
import json
import sqlite3
import unittest

from api_settings import _planned_end
import runtime_settings as settings
import strategy_registry as registry


def _rule():
    return {"op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "const", "value": 1}}


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, enabled_strategies TEXT, duration_days INTEGER)")
        settings.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_defaults_match_five_strategy_baseline(self):
        current = settings.read(self.conn)
        self.assertEqual(current["simulation"]["default_starting_capital"], 300000.0)
        self.assertEqual(current["simulation"]["cycle_duration_days"], 0)
        self.assertEqual(current["simulation"]["enabled_strategies"], list(registry.active_ids(conn=self.conn)))
        self.assertEqual(current["risk"]["shared_pool_position_limit"], 15)
        self.assertEqual(current["risk"]["shared_pool_exposure_cap"], 0.82)

    def test_update_is_atomic_and_audited(self):
        updated = settings.update(self.conn, {
            "default_starting_capital": 100000,
            "cycle_duration_days": 30,
            "enabled_strategies": ["tq_breakout", "main_force_top10"],
        }, actor="test")
        self.assertEqual(updated["simulation"]["cycle_duration_days"], 30)
        self.assertEqual(updated["simulation"]["enabled_strategies"], ["tq_breakout", "main_force_top10"])
        rows = settings.audit(self.conn)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["updated_by"], "test")

    def test_invalid_duration_and_unknown_strategy_are_rejected(self):
        with self.assertRaises(ValueError):
            settings.validate({"cycle_duration_days": 45}, conn=self.conn)
        with self.assertRaises(ValueError):
            settings.validate({"enabled_strategies": ["not-a-strategy"]}, conn=self.conn)

    def test_explicit_empty_enabled_strategies_is_a_valid_idle_cycle(self):
        """PR-47：显式空启用集合 = 零策略 idle 周期，读取时不再回落全集。"""
        settings.update(self.conn, {"enabled_strategies": []}, actor="test")
        self.assertEqual([], settings.enabled_strategies(self.conn))
        self.assertEqual([], settings.read(self.conn)["simulation"]["enabled_strategies"])
        # 缺失/未配置（旧行）仍回落 eligible 全集。
        self.conn.execute("DELETE FROM paper_runtime_settings WHERE key='enabled_strategies'")
        self.assertEqual(
            list(registry.active_ids(conn=self.conn)),
            settings.enabled_strategies(self.conn),
        )

    def test_strategy_overrides_are_bounded(self):
        value = settings.validate({"strategy_overrides": {"tq_breakout": {"style": "strong", "max_positions": 6, "max_weight_pct": 36, "max_exposure_pct": 96}}}, conn=self.conn)
        self.assertEqual(value["strategy_overrides"]["tq_breakout"]["max_positions"], 6)
        with self.assertRaises(ValueError):
            settings.validate({"strategy_overrides": {"tq_breakout": {"max_weight_pct": 50}}}, conn=self.conn)

    def test_active_custom_strategy_is_setting_eligible_and_archive_is_not(self):
        created = registry.create_user_definition(
            self.conn, "custom_settings_probe", "Custom settings", dsl_ast=_rule(), actor="test",
        )
        registry.transition(self.conn, created.id, "validated", actor="test")
        registry.transition(self.conn, created.id, "active", actor="test")
        updated = settings.update(self.conn, {"enabled_strategies": [created.id]}, actor="test")
        self.assertEqual(updated["simulation"]["enabled_strategies"], [created.id])
        self.assertIn(created.id, updated["strategy"]["strategy_overrides"])
        registry.archive_definition(self.conn, created.id, actor="test")
        with self.assertRaisesRegex(ValueError, "未知策略"):
            settings.validate({"enabled_strategies": [created.id]}, conn=self.conn)

    def test_planned_end_counts_weekdays_for_trading_day_duration(self):
        self.assertEqual(_planned_end("2026-09-04 10:00:00", 1), "2026-09-07")
        self.assertIsNone(_planned_end("2026-09-04 10:00:00", 0))

    def test_audit_does_not_create_secret_material(self):
        settings.update(self.conn, {"single_position_max_amount": 12000}, actor="test")
        payload = json.dumps(settings.audit(self.conn), ensure_ascii=False)
        self.assertNotIn("api_key", payload.lower())
        self.assertNotIn("token", payload.lower())


if __name__ == "__main__":
    unittest.main()
