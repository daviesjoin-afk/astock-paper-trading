# -*- coding: utf-8 -*-
import sqlite3
import unittest

import pandas as pd

import strategy_plugins as plugins
import strategy_registry as SR


class StrategyPluginContractTests(unittest.TestCase):
    def test_builtin_plugins_cover_exactly_the_five_registered_active_strategies(self):
        expected = {spec.id for spec in SR.BUILTIN_STRATEGIES}
        self.assertEqual(set(plugins.plugin_ids()), expected)
        for strategy_id in expected:
            plugin = plugins.get_plugin(strategy_id)
            self.assertTrue(plugin.factor_inputs)
            self.assertEqual(plugin.entry_key, strategy_id)
            self.assertEqual(plugin.exit_key, strategy_id)

    def test_builtin_candidate_bindings_match_current_paper_account_contract(self):
        expected = {
            "tq_breakout": "one_to_two",
            "trend_pullback": "bottom_reversal",
            "sector_rotation": "sentiment_pioneer",
            "reported_profit_breakout": "reported_profit_breakout",
            "main_force_top10": "main_force_top10",
        }
        self.assertEqual(
            {strategy_id: plugins.get_plugin(strategy_id).selector_id for strategy_id in expected},
            expected,
        )

    def test_manifest_uses_authoritative_version_parameter_and_lifecycle_runtime(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        SR.ensure_schema(conn)
        for spec in SR.BUILTIN_STRATEGIES:
            manifest = plugins.get_plugin(spec.id).manifest(conn=conn)
            self.assertEqual(manifest["strategy_id"], spec.id)
            self.assertEqual(manifest["name"], spec.name)
            self.assertEqual(manifest["version"], 1)
            self.assertTrue(manifest["checksum"])
            self.assertEqual(manifest["parameter_schema"]["version"], "strategy-parameter-schema-v1")
            self.assertEqual(manifest["lifecycle_stage"], "standard")
            self.assertTrue(manifest["enabled"])

    def test_builtin_entry_and_exit_contracts_remain_strategy_specific(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        SR.ensure_schema(conn)
        entries = {}
        exits = {}
        for strategy_id in plugins.plugin_ids():
            plugin = plugins.get_plugin(strategy_id)
            entries[strategy_id] = plugin.entry_contract(conn)
            exits[strategy_id] = plugin.exit_contract(conn)
            self.assertEqual(entries[strategy_id]["strategy_id"], strategy_id)
            self.assertEqual(exits[strategy_id]["strategy_id"], strategy_id)
            self.assertTrue(entries[strategy_id]["opening_event"])
            self.assertTrue(exits[strategy_id]["intraday_downside"])
            self.assertIn("risk_profile", entries[strategy_id])
            self.assertIn("risk_profile", exits[strategy_id])
        self.assertNotEqual(
            entries["tq_breakout"]["opening_event"],
            entries["trend_pullback"]["opening_event"],
        )
        self.assertNotEqual(
            exits["sector_rotation"]["intraday_downside"],
            exits["main_force_top10"]["intraday_downside"],
        )

    def test_custom_plugin_runs_by_registration_without_core_dispatch_change(self):
        seen = []

        def runner(table, *, topn=10, **_kwargs):
            seen.append((list(table.index), topn))
            return {
                "strategy": "test_plugin_strategy",
                "count": min(len(table), topn),
                "picks": [{"code": str(code)} for code in list(table.index)[:topn]],
            }

        plugin = plugins.StrategyPlugin(
            "test_plugin_strategy",
            "test_plugin_strategy",
            ("factor_a",),
            candidate_runner=runner,
        )
        plugins.register_plugin(plugin)
        self.addCleanup(plugins.unregister_plugin, plugin.strategy_id)
        table = pd.DataFrame({"factor_a": [1.0, 2.0]}, index=["000001", "000002"])
        result = plugins.select_candidates(plugin.strategy_id, table, topn=1)
        self.assertEqual(result["strategy"], plugin.strategy_id)
        self.assertEqual(result["picks"], [{"code": "000001"}])
        self.assertEqual(seen, [(["000001", "000002"], 1)])

    def test_candidate_output_contract_fails_closed_on_malformed_plugin(self):
        plugin = plugins.StrategyPlugin(
            "bad_plugin_strategy",
            "bad_plugin_strategy",
            ("factor_a",),
            candidate_runner=lambda _table, **_kwargs: {"strategy": "bad_plugin_strategy"},
        )
        plugins.register_plugin(plugin)
        self.addCleanup(plugins.unregister_plugin, plugin.strategy_id)
        with self.assertRaisesRegex(ValueError, "candidate output missing key"):
            plugins.select_candidates(plugin.strategy_id, pd.DataFrame({"factor_a": [1.0]}))

    def test_lifecycle_transition_uses_registry_entrypoint(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        SR.ensure_schema(conn)
        plugin = plugins.get_plugin("tq_breakout")
        paused = plugin.transition(conn, "paused", reason="plugin contract test", actor="test")
        self.assertEqual(paused.status, "paused")
        self.assertFalse(paused.supports_new_cycle)
        active = plugin.transition(conn, "active", reason="plugin contract test", actor="test")
        self.assertEqual(active.status, "active")
        self.assertTrue(active.supports_new_cycle)

    def test_native_builtin_parameter_validation_rejects_undeclared_adjustments(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        SR.ensure_schema(conn)
        plugin = plugins.get_plugin("main_force_top10")
        empty = plugin.validate_parameters(conn, {}, evidence_count=100)
        self.assertEqual(empty["changed"], {})
        with self.assertRaisesRegex(ValueError, "no executable DSL parameter schema"):
            plugin.validate_parameters(conn, {"invented": 1.0}, evidence_count=100)


if __name__ == "__main__":
    unittest.main()
