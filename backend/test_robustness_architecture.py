"""R30 dependency and authority boundary guards."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class RobustnessArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.runner = (BACKEND / "robustness_runner.py").read_text(encoding="utf-8")
        self.api = (BACKEND / "api_adaptive.py").read_text(encoding="utf-8")

    def test_runner_does_not_import_legacy_backtest_or_network_providers(self):
        tree = ast.parse(self.runner)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {
            "backtest", "data_fetcher", "requests", "strategies", "marketdata_transport",
        })

    def test_runner_has_no_wall_clock_latest_lookup_or_owner_writes(self):
        lowered = self.runner.lower()
        for forbidden in ("datetime.now", "datetime.today", "date.today", "latest", "current"):
            self.assertNotIn(forbidden, lowered)
        for forbidden in ("historical_market_bars", "historical_universe_members",
                          "historical_financial_observations"):
            self.assertNotIn(forbidden, lowered)
        self.assertNotRegex(lowered, r"\b(insert|update|delete)\s+into?\b")

    def test_robustness_api_post_is_offline_and_has_no_lifecycle_authority(self):
        start = self.api.index('def create_canonical_robustness_report(')
        route_marker = "\n\n" + "@router" + '.get("/experiments/runs/{run_id}/robustness")'
        end = self.api.index(route_marker, start)
        post = self.api[start:end].lower()
        for forbidden in ("requests.", "httpx.", "data_fetcher", "marketdata_transport",
                          "promote", "lifecycle", "current/latest"):
            self.assertNotIn(forbidden, post)

    def test_runner_reuses_the_single_execution_model_for_aggregate_and_trace(self):
        self.assertIn("import experiment_execution_model as EM", self.runner)
        self.assertIn("EM.simulate_with_trace(", self.runner)
        self.assertNotIn("for index, session in enumerate", self.runner)
        self.assertFalse((BACKEND / "robustness_backtest.py").exists())


if __name__ == "__main__":
    unittest.main()
