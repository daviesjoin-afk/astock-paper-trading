import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry
import strategy_runtime as runtime


def _breakout_rule():
    return {"op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "indicator", "name": "ma", "window": 20}}


class StrategyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "runtime.sqlite3"))
        self.conn.row_factory = sqlite3.Row
        strategy = registry.create_user_definition(
            self.conn, "runtime_breakout", "Runtime breakout", dsl_ast=_breakout_rule(), actor="test",
        )
        registry.transition(self.conn, strategy.id, "validated", actor="test")
        registry.transition(self.conn, strategy.id, "active", actor="test")
        # 上下文缓存是模块级的（同 id+版本+revision 复用），用例之间必须清掉，
        # 否则上一个用例的生命周期状态会串到下一个用例。
        runtime.clear_cache()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_context_pins_version_and_cache_key_includes_settings_revision(self):
        first = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        same = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        changed = runtime.get_context(self.conn, "runtime_breakout", settings_rev="2")
        self.assertIs(first, same)
        self.assertIsNot(first, changed)
        self.assertEqual(first.version, 1)
        self.assertEqual(first.compiled_dsl, _breakout_rule())
        self.assertEqual(first.execution_profile["family"], "trend")
        self.assertEqual(first.allocation_runtime.strategy_id, "runtime_breakout")

    def test_user_strategy_starts_in_pilot_not_full_budget(self):
        """PR-26 验收：新用户策略 active 也在 pilot，资金系数 0.25。"""
        context = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        self.assertEqual(context.lifecycle_stage, "pilot")
        self.assertEqual(context.capital_scale, 0.25)
        self.assertEqual(context.allocation_runtime.lifecycle_stage, "pilot")
        self.assertEqual(context.evolution_control.lifecycle_stage, "pilot")

    def test_metadata_lifecycle_stage_overrides_derivation(self):
        registry.save_definition(
            self.conn, "runtime_breakout",
            {"metadata": {"lifecycle_stage": "standard"}},
        )
        runtime.clear_cache()
        context = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        self.assertEqual(context.lifecycle_stage, "standard")
        self.assertEqual(context.capital_scale, 1.0)

    def test_paused_and_archived_are_quarantined(self):
        registry.transition(self.conn, "runtime_breakout", "paused", actor="test")
        runtime.clear_cache()
        context = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        self.assertEqual(context.lifecycle_stage, "quarantined")
        self.assertEqual(context.capital_scale, 0.0)

    def test_builtin_active_strategy_deploys_at_full_scale(self):
        builtin = registry.get("tq_breakout", conn=self.conn)
        if builtin is None:
            self.skipTest("builtin registry not seeded")
        context = runtime.get_context(self.conn, "tq_breakout", settings_rev="1")
        self.assertEqual(context.lifecycle_stage, "standard")
        self.assertEqual(context.capital_scale, 1.0)

    def test_draft_strategy_is_shadow_only(self):
        registry.create_user_definition(
            self.conn, "runtime_draft", "Runtime draft", dsl_ast=_breakout_rule(), actor="test",
        )
        context = runtime.get_context(self.conn, "runtime_draft", settings_rev="1")
        self.assertEqual(context.lifecycle_stage, "shadow")
        self.assertEqual(context.capital_scale, 0.0)

    def test_dsl_version_change_produces_new_context_contract(self):
        before = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        registry.save_definition(self.conn, "runtime_breakout", {"dsl_ast": {
            "op": "gt", "left": {"op": "field", "name": "volume"},
            "right": {"op": "const", "value": 100},
        }})
        after = runtime.get_context(self.conn, "runtime_breakout", settings_rev="1")
        self.assertEqual(before.version, 1)
        self.assertEqual(after.version, 2)
        self.assertNotEqual(before.checksum, after.checksum)


if __name__ == "__main__":
    unittest.main()
