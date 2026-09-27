"""Fail-closed R29 runner/replay and dependency architecture contracts."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import experiment_pit_validation as PV
import experiment_validation_runner as RUNNER
import strategy_registry as SR
import test_experiment_pit_validation as FIXTURES


def _field(name):
    return {"op": "gt", "left": {"op": "field", "name": name},
            "right": {"op": "const", "value": 0}}


class R29DependencyTests(unittest.TestCase):
    def test_price_only_has_no_fundamental_dependency(self):
        deps = PV.strategy_dsl_dependencies({"op": "strategy", "rule": _field("close")})
        self.assertEqual(["close"], deps["price_fields"])
        self.assertEqual([], deps["financial_fields"])
        self.assertEqual([], deps["fund_flow_fields"])

    def test_financial_and_flow_dependencies_are_explicit(self):
        financial = PV.strategy_dsl_dependencies({"op": "strategy", "rule": _field("roe")})
        flow = PV.strategy_dsl_dependencies({"op": "strategy", "rule": _field("main_net_inflow")})
        self.assertEqual(["roe"], financial["financial_fields"])
        self.assertEqual(["main_net_inflow"], flow["fund_flow_fields"])


class R29RunnerTests(unittest.TestCase):
    def test_strategy_version_mismatch_is_unavailable(self):
        spec = FIXTURES._spec()
        version = SR.StrategyVersion(
            strategy_id=spec.strategy.strategy_id, version=spec.strategy.version + 1,
            checksum=spec.strategy.checksum,
            definition={"dsl_ast": {"op": "strategy", "rule": {"op": "const", "value": True}}},
            created_at="2026-01-01T00:00:00+00:00", created_by="test",
        )
        output = RUNNER.run_validation(
            spec, runner_code_revision=spec.code_revision, strategy_version=version,
            dataset_manifest=None, samples=(), walk_forward_config=None,
            session_calendar=None, market_archive_repository=None,
            market_archive_fingerprint=spec.market_data_fingerprint,
            universe_archive_repository=None, universe_archive_fingerprint=spec.universe_fingerprint,
            tradability_repository=None,
        )
        self.assertEqual("strategy_identity_mismatch", output["result"]["failure_reason"])

    def test_code_revision_mismatch_is_unavailable_without_metrics(self):
        spec = FIXTURES._spec()
        output = RUNNER.run_validation(
            spec, runner_code_revision="f" * 40, strategy_version=None,
            dataset_manifest=None, samples=(), walk_forward_config=None,
            session_calendar=None, market_archive_repository=None,
            market_archive_fingerprint=spec.market_data_fingerprint,
            universe_archive_repository=None, universe_archive_fingerprint=spec.universe_fingerprint,
            tradability_repository=None,
        )
        self.assertEqual("unavailable", output["result"]["status"])
        self.assertEqual("code_revision_mismatch", output["result"]["failure_reason"])
        self.assertTrue(all(value is None for value in output["result"]["metrics"].values()))

    def test_null_dsl_does_not_resolve_current_python_strategy(self):
        spec = FIXTURES._spec()
        version = SR.StrategyVersion(
            strategy_id=spec.strategy.strategy_id, version=spec.strategy.version,
            checksum=spec.strategy.checksum, definition={"implementation_key": "trend_pullback"},
            created_at="2026-01-01T00:00:00+00:00", created_by="test",
        )
        output = RUNNER.run_validation(
            spec, runner_code_revision=spec.code_revision, strategy_version=version,
            dataset_manifest=None, samples=(), walk_forward_config=None,
            session_calendar=None, market_archive_repository=None,
            market_archive_fingerprint=spec.market_data_fingerprint,
            universe_archive_repository=None, universe_archive_fingerprint=spec.universe_fingerprint,
            tradability_repository=None,
        )
        self.assertEqual("strategy_replay_definition_unavailable",
                         output["result"]["failure_reason"])

    def test_canonical_modules_have_no_legacy_runtime_or_network_imports(self):
        paths = ("experiment_pit_validation.py", "experiment_validation_runner.py",
                 "experiment_validation_repository.py", "experiment_execution_model.py")
        forbidden = {"backtest", "data_fetcher", "strategies", "requests", "marketdata_transport"}
        for name in paths:
            tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(item.name.split(".")[0] for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertFalse(imports & forbidden, (name, imports & forbidden))


if __name__ == "__main__":
    unittest.main()
