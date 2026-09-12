# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_plugins as PLUGINS
import synthetic_demo_strategy as DEMO


class SyntheticDemoStrategyTests(unittest.TestCase):
    def tearDown(self):
        PLUGINS.unregister_plugin(DEMO.DEMO_STRATEGY_ID)

    def test_demo_is_deterministic_and_selects_expected_candidates(self):
        first = DEMO.run_demo()
        second = DEMO.run_demo()
        self.assertEqual(first, second)
        self.assertEqual(first["data_source"], "synthetic")
        self.assertEqual(first["data_date"], "2026-01-15")
        self.assertEqual(first["candidates"]["count"], 2)
        self.assertEqual(
            [row["code"] for row in first["candidates"]["picks"]],
            ["SYNTH_A", "SYNTH_B"],
        )
        self.assertEqual(
            [row["score"] for row in first["candidates"]["picks"]],
            [0.83, 0.765],
        )

    def test_demo_uses_real_plugin_registry_and_cleans_up(self):
        self.assertNotIn(DEMO.DEMO_STRATEGY_ID, PLUGINS.plugin_ids())
        payload = DEMO.run_demo()
        self.assertNotIn(DEMO.DEMO_STRATEGY_ID, PLUGINS.plugin_ids())
        self.assertEqual(payload["manifest"]["strategy_id"], DEMO.DEMO_STRATEGY_ID)
        self.assertEqual(payload["manifest"]["selector_id"], DEMO.DEMO_SELECTOR_ID)
        self.assertEqual(payload["manifest"]["origin"], "user")
        self.assertEqual(payload["manifest"]["status"], "draft")
        self.assertFalse(payload["manifest"]["enabled"])

    def test_demo_exercises_entry_risk_and_exit_contracts(self):
        payload = DEMO.run_demo()
        self.assertTrue(payload["entry"]["opening_event"])
        self.assertTrue(payload["entry"]["entry_economics"])
        self.assertIn("hard_limits", payload["risk"])
        self.assertIn("soft_limits", payload["risk"])
        self.assertTrue(payload["exit"]["intraday_downside"])
        self.assertIsInstance(payload["exit"]["recovery"], dict)

    def test_demo_output_contains_only_synthetic_inputs_not_environment_dump(self):
        payload = DEMO.run_demo()
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("account_id", serialized)
        self.assertNotIn("paper_trading.sqlite3", serialized)
        self.assertNotIn(os.getcwd().lower(), serialized)
        self.assertTrue(all(str(row["code"]).startswith("SYNTH_") for row in payload["factors"]))

    def test_missing_factor_fails_closed(self):
        table = DEMO.synthetic_factor_table().drop(columns=["quality"])
        plugin = DEMO.build_demo_plugin()
        with self.assertRaisesRegex(ValueError, "missing column: quality"):
            plugin.select_candidates(table, topn=2)


if __name__ == "__main__":
    unittest.main()
