# -*- coding: utf-8 -*-
import json
import sqlite3
import unittest

from api_settings import _planned_end
import runtime_settings as settings


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
        self.assertEqual(current["simulation"]["enabled_strategies"], list(settings.STRATEGIES))
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

    def test_invalid_duration_and_empty_strategy_set_are_rejected(self):
        with self.assertRaises(ValueError):
            settings.validate({"cycle_duration_days": 45})
        with self.assertRaises(ValueError):
            settings.validate({"enabled_strategies": []})
        with self.assertRaises(ValueError):
            settings.validate({"enabled_strategies": ["not-a-strategy"]})

    def test_strategy_overrides_are_bounded(self):
        value = settings.validate({"strategy_overrides": {"tq_breakout": {"style": "strong", "max_positions": 6, "max_weight_pct": 36, "max_exposure_pct": 96}}})
        self.assertEqual(value["strategy_overrides"]["tq_breakout"]["max_positions"], 6)
        with self.assertRaises(ValueError):
            settings.validate({"strategy_overrides": {"tq_breakout": {"max_weight_pct": 50}}})

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
