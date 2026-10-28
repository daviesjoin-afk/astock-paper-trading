"""Monotonic coupling budgets; tightening is preferable to loosening."""
import unittest

from tools import coupling_metrics


class CouplingBudgetTests(unittest.TestCase):
    # PR272 baseline, with boundaries tightened after each completed slice.
    HEAD_BUDGET = {
        "import_cycles": 2,
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
    #
    # 2026-10-27（选股应用/共享资金/前端去环）：selection_application 是新的
    # lower application owner，跨域边由它的实际依赖产生；其余额度按当前实测
    # 收紧，不重设基线。详见 docs/COUPLING_BUDGET.md 的当日切片。
    #
    # 2026-10-27（手单能力桥）：把 6 个有独立 owner 的模块对象从 paper_trading 的
    # 全局桥移到 manual_orders 的直接 import。业务依赖没有增加，但同一条依赖
    # 现在由**两个消费者**各自声明，跨域边因此 +5（666）。这是"声明位置更诚实"
    # 的代价，不是新增耦合；其余额度不变。
    #
    # 2026-10-28（A5 任务生命周期）：`paper_jobs` / `paper_job_runs` 的 9 处写入
    # 收进 `paper_job_lifecycle`，SQL 从主文件搬出后"含 execute 的模块"多了一个
    # （141）。这是归属明确化的代价，不是新增写入面：`paper_trading` 里对这两张表
    # 的 INSERT/UPDATE/DELETE 已归零（`test_paper_job_lifecycle` 有机械守卫）。
    FEATURE_ALLOWANCE = {
        "import_cycles": 0,
        "deferred_imports": 39,
        "private_cross_access": 0,
        "raw_sql_modules": 6,
        "paper_table_fanout_max": 1,
        "cross_domain_edges": 42,
        "max_file_lines": 0,
        "max_function_lines": 4,
        "max_function_globals": 1,
        "duplicate_helpers": 6,
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
            ("market_data_service", "universe"),
            ("strategies", "strategy_plugins"),
        ], actual["import_cycle_members"])


if __name__ == "__main__":
    unittest.main()
