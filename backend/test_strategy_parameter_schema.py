import copy
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import self_evolution as evolution
import strategy_dsl as dsl
import strategy_registry as registry
import strategy_runtime as runtime
from strategy_parameter_schema import (
    StrategyParameterAdjustmentError,
    StrategyParameterSchema,
)


def _parameter(parameter_id, parameter_type, value, minimum, maximum, max_step,
               *, locked=False, risk_direction="neutral", min_evidence=10):
    return {
        "op": "parameter", "parameter_id": parameter_id, "type": parameter_type,
        "value": value, "min": minimum, "max": maximum, "max_step": max_step,
        "locked": locked, "risk_direction": risk_direction,
        "min_evidence": min_evidence,
    }


def _parameterized_rule():
    ma = _parameter("ma_period", "integer", 20, 5, 60, 1,
                    risk_direction="lower_is_riskier")
    return {
        "op": "strategy",
        "rule": {
            "op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "indicator", "name": "ma", "window": ma},
        },
        "parameters": [
            _parameter("rsi_threshold", "number", 55.0, 30.0, 80.0, 2.0),
            _parameter("volume_multiplier", "number", 1.5, 1.0, 3.0, 0.2),
            _parameter("atr_stop_multiplier", "number", 2.0, 1.0, 4.0, 0.25),
            _parameter("holding_days", "integer", 5, 1, 20, 1),
            _parameter("entry_threshold", "number", 0.75, 0.5, 0.95, 0.03),
            _parameter("risk_per_trade", "number", 0.01, 0.002, 0.02, 0.002,
                       risk_direction="higher_is_riskier"),
            _parameter("entry_slices", "integer", 2, 1, 4, 1),
        ],
    }


class StrategyParameterSchemaTests(unittest.TestCase):
    def setUp(self):
        self.ast = _parameterized_rule()
        self.schema = StrategyParameterSchema.from_dsl(self.ast)

    def test_compiles_all_first_release_parameter_kinds(self):
        self.assertEqual("strategy-parameter-schema-v1", self.schema.version)
        self.assertEqual(
            {"ma_period", "rsi_threshold", "volume_multiplier", "atr_stop_multiplier",
             "holding_days", "entry_threshold", "risk_per_trade", "entry_slices"},
            {item.parameter_id for item in self.schema.parameters},
        )
        self.assertEqual(self.schema.editable, tuple(item.parameter_id for item in self.schema.parameters))

    def test_ma20_to_ma19_changes_the_same_deterministic_candidate_snapshot(self):
        snapshot = {
            "close": [120.0] + [10.0] * 18 + [15.0],
            "high": [121.0] + [11.0] * 18 + [16.0],
            "low": [119.0] + [9.0] * 18 + [14.0],
            "volume": [100.0] * 20,
        }
        before = dsl.evaluate(self.ast, snapshot)
        applied = self.schema.apply(self.ast, {"ma_period": 19}, evidence_count=10)
        after = dsl.evaluate(applied.dsl_ast, snapshot)

        self.assertFalse(before)
        self.assertTrue(after)
        self.assertEqual({"ma_period": {"old": 20, "new": 19}}, applied.changed)
        self.assertEqual(self.schema.structure_checksum, applied.structure_checksum)

    def test_only_declared_values_can_change(self):
        altered = copy.deepcopy(self.ast)
        altered["rule"]["op"] = "lte"
        with self.assertRaisesRegex(StrategyParameterAdjustmentError, "structure changed"):
            self.schema.apply(altered, {"ma_period": 19}, evidence_count=10)
        with self.assertRaisesRegex(StrategyParameterAdjustmentError, "undeclared"):
            self.schema.apply(self.ast, {"new_condition": 1}, evidence_count=10)

    def test_locked_bounds_step_and_evidence_are_all_enforced(self):
        locked = copy.deepcopy(self.ast)
        locked["parameters"][0]["locked"] = True
        locked_schema = StrategyParameterSchema.from_dsl(locked)
        with self.assertRaisesRegex(StrategyParameterAdjustmentError, "locked"):
            locked_schema.apply(locked, {"rsi_threshold": 56}, evidence_count=10)
        with self.assertRaisesRegex(StrategyParameterAdjustmentError, "max_step"):
            self.schema.apply(self.ast, {"ma_period": 18}, evidence_count=10)
        with self.assertRaisesRegex(StrategyParameterAdjustmentError, "insufficient evidence"):
            self.schema.apply(self.ast, {"ma_period": 19}, evidence_count=9)
        with self.assertRaisesRegex(StrategyParameterAdjustmentError, "outside bounds"):
            self.schema.apply(self.ast, {"ma_period": 61}, evidence_count=10)


class StrategyParameterRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "strategy.sqlite3"))
        self.conn.row_factory = sqlite3.Row
        created = registry.create_user_definition(
            self.conn, "parameterized_alpha", "Parameterized alpha",
            dsl_ast=_parameterized_rule(), actor="test",
        )
        registry.transition(self.conn, created.id, "validated", actor="test")
        registry.transition(self.conn, created.id, "active", actor="test")
        runtime.clear_cache()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_evolution_entrypoint_creates_a_real_strategy_definition_version(self):
        before = runtime.get_context(self.conn, "parameterized_alpha", settings_rev="1")
        # ma_period 声明 lower_is_riskier → 20 → 21 是收紧方向，快速放行。
        result = evolution.adjust_strategy_dsl_parameters(
            self.conn, "parameterized_alpha", {"ma_period": 21}, evidence_count=10,
        )
        after = runtime.get_context(self.conn, "parameterized_alpha", settings_rev="1")

        self.assertTrue(result["adjusted"])
        self.assertEqual(2, result["version"])
        self.assertEqual(1, before.version)
        self.assertEqual(2, after.version)
        self.assertEqual(21, after.parameter_schema.parameters[0].value)
        self.assertEqual(20, registry.get_version("parameterized_alpha", 1, conn=self.conn)
                         .definition["dsl_ast"]["rule"]["right"]["window"]["value"])

    def test_evolution_cannot_expand_a_risk_direction_parameter(self):
        """PR-33：AI/自进化路径放大风险参数必须先过非对称门（默认无权放大）。"""
        with self.assertRaises(ValueError) as ctx:
            evolution.adjust_strategy_dsl_parameters(
                self.conn, "parameterized_alpha", {"ma_period": 19}, evidence_count=10,
            )
        self.assertIn("风险放大证据不足", str(ctx.exception))
        # 未生效：版本仍然是 1。
        self.assertEqual(1, runtime.get_context(
            self.conn, "parameterized_alpha", settings_rev="1").version)

    def test_expansion_lands_only_with_evidence_observation_and_challenger_win(self):
        import asymmetric_risk as AR

        AR.register_proposal(self.conn, "parameterized_alpha", "risk_per_trade",
                             0.01, 0.012, evidence_count=30, actor="test")
        self.conn.execute(
            "UPDATE risk_expansion_proposals SET proposed_at=? WHERE strategy_id=?",
            ((dt.datetime.now() - dt.timedelta(days=12)).isoformat(timespec="seconds"),
             "parameterized_alpha"),
        )
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            # 观察期已满，但没有 Challenger 胜出 → 仍然拒绝。
            evolution.adjust_strategy_dsl_parameters(
                self.conn, "parameterized_alpha", {"risk_per_trade": 0.012},
                evidence_count=30,
            )
        self.assertIn("Challenger 胜出", str(ctx.exception))
        result = evolution.adjust_strategy_dsl_parameters(
            self.conn, "parameterized_alpha", {"risk_per_trade": 0.012},
            evidence_count=30, challenger_win=True,
        )
        self.assertTrue(result["adjusted"])
        statuses = [row[0] for row in self.conn.execute(
            "SELECT status FROM risk_expansion_proposals WHERE strategy_id=?",
            ("parameterized_alpha",)).fetchall()]
        self.assertEqual(["promoted"], statuses)


if __name__ == "__main__":
    unittest.main()
