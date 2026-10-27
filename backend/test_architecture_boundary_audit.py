"""Monotonic coupling budgets; tightening is preferable to loosening."""
import unittest

from tools import coupling_metrics


class CouplingBudgetTests(unittest.TestCase):
    # Measured on PR272 HEAD with the same analyzer.  These are the baseline.
    HEAD_BUDGET = {
        "import_cycles": 5,
        "deferred_imports": 583,
        "private_cross_access": 202,
        "raw_sql_modules": 135,
        "paper_table_fanout_max": 49,
        "cross_domain_edges": 624,
        "max_file_lines": 16367,
        "max_function_lines": 1191,
        "max_function_globals": 56,
        "duplicate_helpers": 307,
        "unsuffixed_module_ratio": 0.4139,
        "import_side_effects": 25,
    }

    # Reviewed growth from the PR272 repair wave.  The new pipeline adds real
    # modules and call sites; the allowance is explicit instead of silently
    # rebasing the whole budget.  No allowance may be increased without a
    # matching architecture note in docs/COUPLING_BUDGET.md.
    FEATURE_ALLOWANCE = {
        "import_cycles": 0,
        "deferred_imports": 43,
        "private_cross_access": 3,
        "raw_sql_modules": 5,
        "paper_table_fanout_max": 1,
        "cross_domain_edges": 25,
        "max_file_lines": 72,
        "max_function_lines": 5,
        "max_function_globals": 1,
        "duplicate_helpers": 5,
        "unsuffixed_module_ratio": 0.0,
        "import_side_effects": 0,
    }

    def ceiling(self, name):
        return self.HEAD_BUDGET[name] + self.FEATURE_ALLOWANCE[name]

    def test_no_metric_regresses(self):
        actual = coupling_metrics.measure_all()
        for name in self.HEAD_BUDGET:
            with self.subTest(metric=name):
                self.assertLessEqual(
                    actual[name], self.ceiling(name),
                    f"{name}: {actual[name]} > {self.ceiling(name)} "
                    f"(HEAD {self.HEAD_BUDGET[name]} + allowance "
                    f"{self.FEATURE_ALLOWANCE[name]})")

    def test_allowances_are_non_negative_and_cover_exactly_the_budget(self):
        self.assertEqual(set(self.HEAD_BUDGET), set(self.FEATURE_ALLOWANCE))
        self.assertTrue(all(value >= 0 for value in self.FEATURE_ALLOWANCE.values()))

    def test_known_cycles_do_not_grow(self):
        actual = coupling_metrics.measure_all()
        self.assertEqual([
            ("evolution_activation", "self_evolution"),
            ("main", "paper_selection"),
            ("market_data_service", "universe"),
            ("strategies", "strategy_plugins"),
            ("tradability_ingestion", "tradability_observation_ledger"),
        ], actual["import_cycle_members"])


if __name__ == "__main__":
    unittest.main()
