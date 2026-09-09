# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_dsl as dsl
import strategy_registry as registry


def _sample_rule():
    # close > ma20 AND ma20 > ma60 AND volume > volume_ma5*1.8
    return {
        "op": "and",
        "args": [
            {"op": "gt", "left": {"op": "field", "name": "close"},
             "right": {"op": "indicator", "name": "ma", "window": 20}},
            {"op": "gt", "left": {"op": "indicator", "name": "ma", "window": 20},
             "right": {"op": "indicator", "name": "ma", "window": 60}},
            {"op": "gt", "left": {"op": "field", "name": "volume"},
             "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                       "right": {"op": "const", "value": 1.8}}},
        ],
    }


class StrategyDslTests(unittest.TestCase):
    def setUp(self):
        close = [float(i) for i in range(1, 61)]
        self.snapshot = {
            "close": close,
            "open": close,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "volume": [100.0] * 59 + [300.0],
            "financials": {"roe": 16.0},
            "fund_flow": {"main_net_inflow_pct": 3.2},
        }

    def test_declarative_rule_is_deterministic_against_offline_snapshot(self):
        rule = _sample_rule()
        before = copy.deepcopy((rule, self.snapshot))
        self.assertTrue(dsl.evaluate(rule, self.snapshot))
        self.assertEqual(dsl.evaluate(rule, self.snapshot), dsl.evaluate(rule, self.snapshot))
        self.assertEqual(before, (rule, self.snapshot))

    def test_cross_and_allowlisted_financial_flow_fields(self):
        cross = {
            "op": "and", "args": [
                {"op": "cross_above", "left": {"op": "field", "name": "close"},
                 "right": {"op": "indicator", "name": "ma", "window": 5}},
                {"op": "gte", "left": {"op": "field", "name": "roe"},
                 "right": {"op": "const", "value": 15}},
                {"op": "gt", "left": {"op": "field", "name": "main_net_inflow_pct"},
                 "right": {"op": "const", "value": 0}},
            ],
        }
        snapshot = dict(self.snapshot)
        snapshot["close"] = [10.0, 9.0, 9.0, 9.0, 9.0, 12.0]
        snapshot["volume"] = [100.0] * 6
        self.assertTrue(dsl.evaluate(cross, snapshot))

    def test_rejects_code_like_nodes_unknown_fields_and_resource_limits(self):
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "unsupported DSL op"):
            dsl.normalize({"op": "python", "source": "__import__('os')"})
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "not allowlisted"):
            dsl.normalize({"op": "field", "name": "__class__"})
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "window"):
            dsl.normalize({"op": "indicator", "name": "ma", "window": 251})
        nested = {"op": "const", "value": 1}
        for _ in range(dsl.MAX_AST_DEPTH):
            nested = {"op": "not", "arg": nested}
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "max depth"):
            dsl.normalize(nested)

    def test_rejects_ill_typed_expressions_and_fails_closed_through_not(self):
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "root must be a boolean"):
            dsl.normalize({"op": "const", "value": 1})
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "requires scalar operands"):
            dsl.normalize({"op": "gt", "left": {"op": "and", "args": [_sample_rule(), _sample_rule()]},
                           "right": {"op": "const", "value": 1}})
        with self.assertRaisesRegex(dsl.StrategyDslValidationError, "boolean arg"):
            dsl.normalize({"op": "not", "arg": {"op": "field", "name": "close"}})
        missing = {"op": "not", "arg": {"op": "gt", "left": {"op": "field", "name": "roe"},
                                              "right": {"op": "const", "value": 10}}}
        self.assertFalse(dsl.evaluate(missing, {"close": [10.0]}))

    def test_canonical_serialization_and_checksum_ignore_input_key_order(self):
        left = {"left": {"name": "close", "op": "field"}, "op": "gt",
                "right": {"value": 10, "op": "const"}}
        right = {"right": {"op": "const", "value": 10}, "op": "gt",
                 "left": {"op": "field", "name": "close"}}
        self.assertEqual(dsl.canonical_json(left), dsl.canonical_json(right))
        self.assertEqual(dsl.checksum(left), dsl.checksum(right))

    def test_definition_version_stores_real_immutable_dsl_ast(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = sqlite3.connect(os.path.join(directory, "paper.sqlite3"))
            try:
                created = registry.create_user_definition(
                    conn, "user_dsl_alpha", "DSL Alpha", dsl_ast=_sample_rule(), actor="test",
                )
                original = registry.get_version(created.id, 1, conn=conn)
                self.assertEqual(original.definition["dsl_ast"], dsl.normalize(_sample_rule()))
                updated = registry.save_definition(
                    conn, created.id,
                    {"dsl_ast": {"op": "lt", "left": {"op": "indicator", "name": "rsi", "window": 14},
                                 "right": {"op": "const", "value": 50}}},
                    expected_version=1,
                )
                self.assertEqual(updated.version, 2)
                self.assertEqual(registry.get_version(created.id, 1, conn=conn), original)
            finally:
                conn.close()

    def test_legacy_snapshot_without_null_dsl_is_a_noop_save(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = sqlite3.connect(os.path.join(directory, "paper.sqlite3"))
            try:
                registry.ensure_schema(conn)
                current = registry.get_version("tq_breakout", 1, conn=conn)
                legacy = dict(current.definition)
                legacy.pop("dsl_ast")
                encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                legacy_checksum = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                conn.execute("DROP TRIGGER trg_strategy_versions_no_update")
                conn.execute("DROP TRIGGER trg_strategy_definition_version_guard")
                conn.execute("UPDATE paper_strategy_versions SET definition_json=?,checksum=? WHERE strategy_id=? AND version=1",
                             (encoded, legacy_checksum, "tq_breakout"))
                conn.execute("UPDATE paper_strategy_version_heads SET current_checksum=? WHERE strategy_id=?",
                             (legacy_checksum, "tq_breakout"))
                conn.execute("UPDATE strategy_definitions SET current_checksum=? WHERE id=?",
                             (legacy_checksum, "tq_breakout"))
                unchanged = registry.save_definition(conn, "tq_breakout", {})
                self.assertEqual(unchanged.version, 1)
                self.assertEqual(len(registry.list_versions("tq_breakout", conn=conn)), 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
