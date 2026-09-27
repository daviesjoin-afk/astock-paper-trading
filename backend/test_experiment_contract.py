"""Permanent regression tests for the R28-A experiment contract."""
from __future__ import annotations

import ast
import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import experiment_contract as EC  # noqa: E402
import learning_evaluation  # noqa: E402


def _spec(**changes):
    values = {
        "strategy": EC.StrategyIdentity("trend_pullback", 2, "a" * 64),
        "code_revision": "b" * 40,
        "dataset_fingerprint": "c" * 64,
        "universe_fingerprint": "d" * 64,
        "tradability_fingerprint": "e" * 64,
        "market_data_fingerprint": "f" * 64,
        "parameter_set": {"lookback": 20, "risk": {"stop": 0.08}},
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
        "asof_policy": {"policy_id": "pit-close-v1", "cutoff": "2026-03-31T15:00:00+08:00"},
        "execution_assumptions": {
            "execution_profile_version": "execution-profile-v3",
            "fill_assumptions": {"fill_price": "next_open", "priority": "price_time"},
            "t_plus_one_semantics": "sell_after_next_session",
            "price_limit_semantics": "exchange_limit_rules-v1",
            "partial_fill_semantics": "record_unfilled_remainder",
            "capacity_assumptions": {"participation_rate": 0.05},
        },
        "cost_model": {
            "commission_rate": 0.0001,
            "minimum_commission": 0,
            "stamp_duty_rate": 0.0005,
            "slippage_model": "fixed-rate-v1",
            "version": "cn-equity-cost-v1",
        },
        "random_seed": 17,
    }
    values.update(changes)
    return EC.ExperimentSpec(**values)


def _completed(**changes):
    values = {
        "experiment_fingerprint": _spec().fingerprint,
        "status": "completed",
        "total_return": 0.12,
        "max_drawdown": -0.08,
        "volatility": 0.2,
        "turnover": 3.5,
        "trade_count": 12,
        "total_cost": 125.5,
        "data_coverage": 0.98,
    }
    values.update(changes)
    return EC.ExperimentResult(**values)


class ExperimentSpecTests(unittest.TestCase):
    def test_EXP_01_same_complete_spec_has_same_fingerprint(self):
        self.assertEqual(_spec().fingerprint, _spec().fingerprint)

    def test_EXP_02_mapping_insertion_order_does_not_change_fingerprint(self):
        first = _spec(parameter_set={"a": 1, "b": {"x": 2, "y": 3}})
        second = _spec(parameter_set={"b": {"y": 3, "x": 2}, "a": 1})
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_EXP_03_strategy_checksum_changes_fingerprint(self):
        self.assertNotEqual(_spec().fingerprint, _spec(strategy=EC.StrategyIdentity("trend_pullback", 2, "1" * 64)).fingerprint)

    def test_EXP_04_code_revision_changes_fingerprint(self):
        self.assertNotEqual(_spec().fingerprint, _spec(code_revision="2" * 40).fingerprint)

    def test_EXP_05_dataset_identity_changes_fingerprint(self):
        self.assertNotEqual(_spec().fingerprint, _spec(dataset_fingerprint="1" * 64).fingerprint)

    def test_EXP_06_universe_tradability_and_market_data_change_fingerprint(self):
        baseline = _spec().fingerprint
        for field in ("universe_fingerprint", "tradability_fingerprint", "market_data_fingerprint"):
            with self.subTest(field=field):
                self.assertNotEqual(baseline, _spec(**{field: "1" * 64}).fingerprint)

    def test_EXP_07_parameter_change_changes_fingerprint(self):
        self.assertNotEqual(_spec().fingerprint, _spec(parameter_set={"lookback": 21}).fingerprint)

    def test_EXP_08_date_range_change_changes_fingerprint(self):
        self.assertNotEqual(_spec().fingerprint, _spec(end_date="2026-04-01").fingerprint)

    def test_EXP_09_asof_policy_change_changes_fingerprint(self):
        self.assertNotEqual(
            _spec().fingerprint,
            _spec(asof_policy={"policy_id": "pit-close-v1", "cutoff": "2026-03-30T15:00:00+08:00"}).fingerprint,
        )

    def test_EXP_10_execution_assumptions_change_fingerprint(self):
        changed = dict(_spec().execution_assumptions)
        changed["t_plus_one_semantics"] = "same_session"
        self.assertNotEqual(_spec().fingerprint, _spec(execution_assumptions=changed).fingerprint)

    def test_EXP_11_cost_model_change_fingerprint(self):
        changed = dict(_spec().cost_model)
        changed["commission_rate"] = 0.0002
        self.assertNotEqual(_spec().fingerprint, _spec(cost_model=changed).fingerprint)

    def test_EXP_12_random_seed_change_fingerprint(self):
        self.assertNotEqual(_spec().fingerprint, _spec(random_seed=18).fingerprint)

    def test_EXP_13_missing_or_unstable_identity_fails_closed(self):
        with self.assertRaises((TypeError, ValueError)):
            _spec(universe_fingerprint=None)
        with self.assertRaises(ValueError):
            _spec(universe_fingerprint="latest")

    def test_EXP_14_invalid_or_reversed_date_range_fails_closed(self):
        with self.assertRaises(ValueError):
            _spec(start_date="2026-04-01", end_date="2026-03-31")
        with self.assertRaises(ValueError):
            _spec(start_date=None)

    def test_EXP_15_non_finite_nested_values_fail_closed(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _spec(parameter_set={"invalid": value})

    def test_EXP_16_nested_mutable_input_is_frozen(self):
        params = {"nested": [1, {"value": 2}]}
        spec = _spec(parameter_set=params)
        fingerprint = spec.fingerprint
        params["nested"][1]["value"] = 99
        params["later"] = True
        self.assertEqual(fingerprint, spec.fingerprint)
        self.assertEqual((1, {"value": 2}), spec.parameter_set["nested"])
        with self.assertRaises(TypeError):
            spec.parameter_set["later"] = True

    def test_EXP_17_contract_requires_explicit_pit_execution_and_cost_inputs(self):
        with self.assertRaises(ValueError):
            _spec(asof_policy={"policy_id": "pit-close-v1"})
        with self.assertRaises(ValueError):
            _spec(asof_policy={"policy_id": "pit-close-v1", "cutoff": "2026-03-31T15:00:00"})
        with self.assertRaises(ValueError):
            _spec(execution_assumptions={"execution_profile_version": "v1"})
        with self.assertRaises(ValueError):
            _spec(cost_model={"commission_rate": 0})

    def test_EXP_28_equivalent_numeric_spellings_have_one_identity(self):
        first = _spec(parameter_set={"integer": 1, "negative_zero": -0.0})
        second = _spec(parameter_set={"integer": 1.0, "negative_zero": 0}, cost_model={
            "commission_rate": 0.0001,
            "minimum_commission": 0.0,
            "stamp_duty_rate": 0.0005,
            "slippage_model": "fixed-rate-v1",
            "version": "cn-equity-cost-v1",
        })
        self.assertEqual(first.fingerprint, second.fingerprint)


class ExperimentResultTests(unittest.TestCase):
    def test_EXP_18_completed_result_requires_core_metrics(self):
        with self.assertRaises(ValueError):
            _completed(data_coverage=None)

    def test_EXP_19_failed_or_unavailable_keeps_unknown_metrics_unknown(self):
        for status in ("failed", "unavailable"):
            result = EC.ExperimentResult(
                experiment_fingerprint=_spec().fingerprint,
                status=status,
                failure_reason="source_unavailable",
            )
            self.assertIsNone(result.total_return)
            self.assertIsNone(result.trade_count)
            self.assertIsNone(result.projection()["metrics"]["return"])
            with self.assertRaises(ValueError):
                EC.ExperimentResult(
                    experiment_fingerprint=_spec().fingerprint,
                    status=status,
                    failure_reason="source_unavailable",
                    total_return=0,
                )
        with self.assertRaises(ValueError):
            EC.ExperimentResult(
                experiment_fingerprint=_spec().fingerprint,
                status="failed",
                failure_reason="Runner crashed with an unstable free-form message",
            )

    def test_EXP_20_result_binds_exact_experiment_fingerprint(self):
        result = _completed()
        self.assertEqual(_spec().fingerprint, result.projection()["experiment_fingerprint"])
        result.assert_for(_spec())
        with self.assertRaises(ValueError):
            result.assert_for(_spec(random_seed=18))
        self.assertNotEqual(result.result_fingerprint, _completed(experiment_fingerprint="1" * 64).result_fingerprint)

    def test_EXP_21_statuses_exclude_approval_and_promotion(self):
        self.assertEqual({"completed", "failed", "unavailable"}, EC.RESULT_STATUSES)
        for forbidden in ("approved", "promotable", "champion", "production", "verified_strategy"):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                _completed(status=forbidden)

    def test_EXP_22_numeric_metrics_are_finite_and_canonical(self):
        for value in (float("nan"), float("inf"), float("-inf"), "N/A", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _completed(total_return=value)
        for field, value in (("trade_count", 1.5), ("trade_count", True),
                             ("turnover", -1), ("total_cost", -1), ("data_coverage", 1.01)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _completed(**{field: value})

    def test_EXP_23_result_fingerprint_covers_all_result_fields(self):
        result = _completed(regime_breakdown={"bull": {"return": 0.2}})
        reordered = _completed(regime_breakdown={"bull": {"return": 0.2}})
        self.assertEqual(result.result_fingerprint, reordered.result_fingerprint)
        self.assertNotEqual(result.result_fingerprint, _completed(total_return=0.13).result_fingerprint)
        with self.assertRaises(ValueError):
            _completed(result_fingerprint="0" * 64)

    def test_EXP_24_regime_breakdown_is_deeply_frozen(self):
        data = {"bull": [{"count": 2}]}
        result = _completed(regime_breakdown=data)
        fingerprint = result.result_fingerprint
        data["bull"][0]["count"] = 9
        self.assertEqual(fingerprint, result.result_fingerprint)

    def test_EXP_29_nested_identity_labels_are_normalized_before_storage(self):
        spec = _spec(
            asof_policy={"policy_id": " pit-close-v1 ", "cutoff": " 2026-03-31T15:00:00+08:00 "},
            execution_assumptions={
                "execution_profile_version": " execution-profile-v3 ",
                "fill_assumptions": {"fill_price": "next_open", "priority": "price_time"},
                "t_plus_one_semantics": " sell_after_next_session ",
                "price_limit_semantics": " exchange_limit_rules-v1 ",
                "partial_fill_semantics": " record_unfilled_remainder ",
                "capacity_assumptions": {"participation_rate": 0.05},
            },
            cost_model={
                "commission_rate": 0.0001,
                "minimum_commission": 0,
                "stamp_duty_rate": 0.0005,
                "slippage_model": " fixed-rate-v1 ",
                "version": " cn-equity-cost-v1 ",
            },
        )
        self.assertEqual("pit-close-v1", spec.asof_policy["policy_id"])
        self.assertEqual("execution-profile-v3", spec.execution_assumptions["execution_profile_version"])
        self.assertEqual("sell_after_next_session", spec.execution_assumptions["t_plus_one_semantics"])
        self.assertEqual("fixed-rate-v1", spec.cost_model["slippage_model"])
        self.assertEqual(_spec().fingerprint, spec.fingerprint)

    def test_EXP_30_explicit_falsy_result_fingerprints_fail_closed(self):
        for value in (None, False, 0, "", {}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _completed(result_fingerprint=value)


class ExperimentArchitectureTests(unittest.TestCase):
    def test_EXP_25_contract_has_only_pure_standard_library_imports(self):
        path = os.path.join(BACKEND, "experiment_contract.py")
        with open(path, encoding="utf-8") as source_file:
            tree = ast.parse(source_file.read())
        allowed = {"__future__", "dataclasses", "datetime", "hashlib", "json", "math", "re", "types", "typing"}
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertLessEqual(roots, allowed)
        forbidden_calls = {"now", "today", "utcnow", "system", "popen", "run", "Popen"}
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(calls & forbidden_calls)

    def test_EXP_26_existing_evaluation_projection_remains_the_owner_fact(self):
        self.assertTrue(hasattr(learning_evaluation, "ExperimentEvaluationProjection"))
        self.assertIsNot(learning_evaluation.ExperimentEvaluationProjection, EC.ExperimentResult)

    def test_EXP_27_legacy_backtest_is_not_wired_to_the_contract(self):
        contract_path = os.path.join(BACKEND, "experiment_contract.py")
        with open(contract_path, encoding="utf-8") as source_file:
            contract = ast.parse(source_file.read())
        roots = set()
        for node in ast.walk(contract):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue({"backtest", "data_fetcher", "strategies", "paper_trading"}.isdisjoint(roots))
        self.assertTrue(callable(__import__("backtest").run_backtest))


if __name__ == "__main__":
    unittest.main()
