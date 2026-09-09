# -*- coding: utf-8 -*-
"""PR-26 生产侧回归：生命周期感知的资金部署。

覆盖三件事：
1. 内置（builtin）策略仍是 standard：预算口径不变，不能因为改造而缩水；
2. Context 给出 pilot 阶段时，``_strategy_pool_budget`` 的部署上限只有
   额度的 25%，且``_allocation_plan`` 给出同样的判断（同一份结果）；
3. ``_allocation_plan`` 与 ``_strategy_pool_budget`` 的阶段/系数必须一致
   ——执行路径与 explainability 不允许各算各的。
"""
import contextlib
import dataclasses
import os
import tempfile
import unittest

import paper_trading as PT
import strategy_runtime as SRT


class AllocationLifecycleProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = PT.DB_PATH
        PT.DB_PATH = os.path.join(self.tmp.name, "paper_trading.sqlite3")
        PT.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, PT, "DB_PATH", self.old_db)
        self.ctx = PT._db()
        self.conn = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.account = {"id": "tq_breakout"}
        self.original_get_context = SRT.get_context
        self.addCleanup(setattr, SRT, "get_context", self.original_get_context)

    def _budget(self, market=None):
        return PT._strategy_pool_budget(
            self.conn, self.account, 300000.0, [], {}, market=market or {"light": "green"},
        )

    def _force_stage(self, stage, scale):
        def patched(conn, strategy_id, *, settings_rev=None):
            context = self.original_get_context(conn, strategy_id, settings_rev=settings_rev)
            runtime = dataclasses.replace(context.allocation_runtime, lifecycle_stage=stage)
            return dataclasses.replace(
                context, lifecycle_stage=stage, capital_scale=scale, allocation_runtime=runtime,
            )

        SRT.get_context = patched

    def test_builtin_strategy_keeps_full_deployment(self):
        budget = self._budget()
        self.assertEqual(budget["lifecycle_stage"], "standard")
        self.assertEqual(budget["capital_scale"], 1.0)
        self.assertAlmostEqual(
            budget["absolute_cap_amount"],
            budget["current_total_amount"] + budget["allowance_amount"],
            places=2,
        )
        self.assertAlmostEqual(budget["lifecycle_waiting_capital"], 0.0, places=2)

    def test_pilot_context_caps_deployment_at_a_quarter(self):
        full = self._budget()
        self._force_stage("pilot", 0.25)
        budget = self._budget()
        self.assertEqual(budget["lifecycle_stage"], "pilot")
        self.assertEqual(budget["capital_scale"], 0.25)
        self.assertAlmostEqual(
            budget["scaled_allowance_amount"], budget["allowance_amount"] * 0.25, places=2
        )
        self.assertLess(budget["absolute_cap_amount"], full["absolute_cap_amount"])
        self.assertGreater(budget["lifecycle_waiting_capital"], 0.0)

    def test_shadow_context_deploys_nothing(self):
        self._force_stage("shadow", 0.0)
        budget = self._budget()
        self.assertEqual(budget["scaled_allowance_amount"], 0.0)
        self.assertAlmostEqual(
            budget["absolute_cap_amount"], budget["current_total_amount"], places=2
        )
        self.assertAlmostEqual(
            budget["lifecycle_waiting_capital"], budget["allowance_amount"], places=2
        )

    def test_plan_and_budget_share_one_lifecycle_source(self):
        self._force_stage("pilot", 0.25)
        plan = PT._allocation_plan(
            self.conn, nav=300000.0, positions=[], quotes={}, market={"light": "green"},
            prices_by_strategy={"tq_breakout": 10.0}, account=self.account,
        )
        row = plan["rows_by_strategy"]["tq_breakout"]
        budget = self._budget()
        self.assertEqual(row["lifecycle_stage"], budget["lifecycle_stage"])
        self.assertEqual(row["capital_scale"], budget["capital_scale"])
        self.assertLessEqual(
            row["deployable_amount"], row["raw_allowance_amount"] * 0.25 + 1e-6
        )
        self.assertEqual(row["allowed"], True)


if __name__ == "__main__":
    unittest.main()
