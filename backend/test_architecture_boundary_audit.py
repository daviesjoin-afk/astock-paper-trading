"""Monotonic coupling budgets; tightening is preferable to loosening."""
import unittest

from tools import coupling_metrics


class CouplingBudgetTests(unittest.TestCase):
    # PR272 baseline, with boundaries tightened after each completed slice.
    HEAD_BUDGET = {
        "import_cycles": 0,
        "deferred_imports": 583,
        "private_cross_access": 201,
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
    # 2026-10-28（A8 收尾：最后两组环）：`universe.json` 的读取下沉到
    # `data_universe_store`，行情回退与 `universe` 都向它取同一份事实；
    # `strategy_plugins` 不再 import `strategies`，改由算法 owner 在组装期
    # `bind_default_runner` 绑定。实测：环 2 → **0**，私有跨模块访问 202 → 201
    # （少掉 `S._run_strategy_legacy` 这一处），延迟 import 621 → 618，
    # 跨域边 666 → 664。因此按实测收紧额度：延迟 39 → 35、跨域 42 → 40，
    # 基线里 `import_cycles` 与 `private_cross_access` 直接收紧到实测值。
    # 顶层 import 副作用 23 → 24，来自组装期那一次 `bind_default_runner` 调用，
    # 仍在 PR272 基线 25 以内，不是新增的副作用类别。
    #
    # 2026-10-28（行情源预算/TTL 配置）：新增基础模块 `env_utils`，把 5 处各自
    # 解析环境变量的副本收敛成 1 个实现（duplicate_helpers 314 → 313，回到额度
    # 内）。`data_fetcher`(data) 与 `paper_live_tradability`(paper) 各新增一条
    # 跨域边，共 +2（664 → 666）；其余三个调用方都在 `other` 域，不新增边。
    # 这是"共享实现替代复制"的代价，不是新业务耦合。
    #
    # 同日（A1 第一组：共享工具下沉）：`manual_orders` / `dashboard_queries` /
    # `execution_planner` 把时钟/JSON/数值工具改为直接 import 各自的 owner
    # （paper_common / paper_numeric），不再经 `paper_trading` 的全局桥转发。
    # 三个 vertical 的依赖字段共 92 → 80（-12），代价是 +5 条显式跨域边
    # （666 → 673；paper_risk_evidence / paper_risk_service 等早已这样做）。
    # 显式依赖比隐藏的 globals 桥更可核对，故按实测收紧额度外的增量记录。
    #
    # 同日（低频状态跨进程缓存）：`paper_live_tradability` 新增 `data_paths`
    # 与 `marketdata_cache` 两条跨域边（paper → data / other），用于把停牌报表
    # 落到 data_cache 并复用 single-flight 原子发布。664 → 668。额度 42 → 44。
    FEATURE_ALLOWANCE = {
        "import_cycles": 0,
        "deferred_imports": 35,
        "private_cross_access": 0,
        "raw_sql_modules": 6,
        "paper_table_fanout_max": 1,
        "cross_domain_edges": 49,
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

    def test_no_backend_import_cycles(self):
        """2026-10-28：已知闭环全部关闭，现在不允许任何新环出现。"""
        actual = coupling_metrics.measure_all()
        self.assertEqual([], actual["import_cycle_members"])
        self.assertEqual(0, actual["import_cycles"])


if __name__ == "__main__":
    unittest.main()
