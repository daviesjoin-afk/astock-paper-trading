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
