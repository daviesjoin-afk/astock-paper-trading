# -*- coding: utf-8 -*-
"""P1-01：周度复盘只产出 advisory，不得改写账户参数或参数版本。

这条回归锁住的是一条**写入通道**而不是文案：过去 ``_weekly_review`` 会在
样本充足时直接 ``PAC.write_params`` 并插入 ``paper_parameter_versions``，
绕过 ``evolution_apply`` 的双 AI 共识、人工确认与版本/回滚门禁。周报仍要
保留绩效统计、``paper_reviews`` 历史和报告文件，但参数应用只能走人工确认。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_trading as PT  # noqa: E402

DAY = dt.date(2026, 11, 6)  # 周五，ISO 周内
CHECKSUM = "a" * 64


class WeeklyReviewAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.old_db_path = PT.DB_PATH
        self.old_report_dir = PT.REPORT_DIR
        PT.DB_PATH = os.path.join(self.tmp_dir, "paper.sqlite3")
        PT.REPORT_DIR = os.path.join(self.tmp_dir, "reports")
        PT.init_db()
        self._seed_account()

    def tearDown(self):
        PT.DB_PATH = self.old_db_path
        PT.REPORT_DIR = self.old_report_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _seed_account(self, *, params=None):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO paper_accounts"
                "(id,name,source_strategy,status,initial_cash,cash,cycle_days,"
                " max_positions,max_weight,max_exposure,version,created_at,updated_at,params,cycle_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                ("weekly_acc", "周报复盘账户", "tq_breakout", "running",
                 300000.0, 300000.0, 30, 5, 0.3, 0.8, "v3.0",
                 "2026-10-01 09:00:00", "2026-10-01 09:00:00",
                 json.dumps(params or {"entry_score_delta": 0.0})),
            )
            # 订单有 cycle provenance 触发器：必须存在一个真实周期。
            conn.execute(
                "INSERT OR REPLACE INTO paper_cycles(id,cycle_key,status,capital,"
                " risk_profile,started_at,created_at,updated_at)"
                " VALUES(1,?,?,?,?,?,?,?)",
                ("cycle-weekly", "running", 300000.0, "conservative",
                 "2026-10-01 09:00:00", "2026-10-01 09:00:00",
                 "2026-10-01 09:00:00"),
            )
            # filled 卖单的策略版本戳触发器要求完整版本链。
            conn.execute(
                "INSERT OR REPLACE INTO strategy_definitions"
                "(id,name,origin,lifecycle_status,implementation_key,supports_new_cycle,"
                " created_at,updated_at,current_version,current_checksum)"
                " VALUES(?,?,?,?,?,1,?,?,1,?)",
                ("weekly_acc", "周报复盘策略", "builtin", "active", "builtin",
                 "2026-10-01 09:00:00", "2026-10-01 09:00:00", CHECKSUM),
            )
            # filled 卖单的策略版本戳触发器要求一条真实版本记录。
            conn.execute(
                "INSERT OR REPLACE INTO paper_strategy_versions"
                "(strategy_id,version,checksum,definition_json,name,implementation_key,"
                " created_at,created_by)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("weekly_acc", 1, CHECKSUM, "{}", "周报复盘策略",
                 "builtin", "2026-10-01 09:00:00", "test"),
            )

    def _set_performance(self, *, navs, closed, signals, execution_verified=True):
        """构造一周净值序列、已核验平仓数与信号数。"""
        with PT._db(immediate=True) as conn:
            for i, nav in enumerate(navs):
                conn.execute(
                    "INSERT OR REPLACE INTO paper_nav(account_id,nav_date,cash,market_value,nav,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    ("weekly_acc", f"2026-11-{i+1:02d}", nav, 0.0, nav,
                     f"2026-11-{i+1:02d} 15:00:00"),
                )
            for i in range(closed):
                conn.execute(
                    "INSERT INTO paper_orders(account_id,side,code,qty,planned_price,"
                    " status,filled_price,created_at,execution_verified,execution_status,"
                    " cycle_id,strategy_id,strategy_version,strategy_checksum,risk_payload)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,1,?,1,?,'{}')",
                    ("weekly_acc", "sell", f"6000{i:02d}", 100, 10.0, "filled", 10.0,
                     "2026-11-03 10:00:00", 1 if execution_verified else 0,
                     "verified" if execution_verified else "unverified",
                     "weekly_acc", CHECKSUM),
                )
            for i in range(signals):
                conn.execute(
                    "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,payload,status,"
                    " cycle_id,strategy_id,strategy_version,strategy_checksum,created_at)"
                    " VALUES(?,?,?,?,?,?,1,?,1,?,?)",
                    ("weekly_acc", "2026-11-03", "2026-11-03", f"6001{i:02d}",
                     "{}", "filled", "weekly_acc", CHECKSUM,
                     "2026-11-03 09:00:00"),
                )

    def _snapshot(self):
        """周报前后，所有账户参数与所有参数版本行都必须逐字不变。

        只断言目标账户会漏掉内置账户被改参的情况；周报遍历
        ``SELECT * FROM paper_accounts``，因此这里也用全表快照。
        """
        with PT._db() as conn:
            params = [tuple(r) for r in conn.execute(
                "SELECT id, params, version, updated_at FROM paper_accounts ORDER BY id"
            )]
            versions = [tuple(r) for r in conn.execute(
                "SELECT id, cycle_id, account_id, version, style, params, reason,"
                " effective_date, created_at FROM paper_parameter_versions ORDER BY id"
            )]
            reviews = conn.execute(
                "SELECT week_key, report, recommendation FROM paper_reviews"
                " WHERE account_id='weekly_acc'"
            ).fetchall()
        return (
            tuple(params),
            tuple(versions),
            [tuple(r) for r in reviews],
        )

    def _run_review(self):
        with PT._db(immediate=True) as conn:
            return PT._weekly_review(conn, DAY)

    def test_negative_return_with_enough_samples_never_writes_params(self):
        self._set_performance(navs=[300000, 290000, 280000], closed=8, signals=12)
        before = self._snapshot()
        result = self._run_review()
        after = self._snapshot()
        self.assertEqual(before[0], after[0], "负收益场景不应改写账户参数")
        self.assertEqual(before[1], after[1], "负收益场景不应新增参数版本")
        self.assertEqual(len(after[2]), 1, "周报仍须写入 paper_reviews")
        own = next(x for x in result if x["account_id"] == "weekly_acc")
        self.assertIn("不自动生效", own["recommendation"][-1])

    def test_positive_return_with_enough_samples_never_writes_params(self):
        self._set_performance(navs=[300000, 310000, 320000], closed=8, signals=12)
        before = self._snapshot()
        self._run_review()
        after = self._snapshot()
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertEqual(len(after[2]), 1)
        self.assertTrue(any("advisory" in (r[1] + r[2]) for r in after[2]))

    def test_drawdown_scenario_never_writes_params(self):
        self._set_performance(navs=[300000, 330000, 260000], closed=8, signals=12)
        before = self._snapshot()
        self._run_review()
        after = self._snapshot()
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertTrue(any("回撤" in (r[1] + r[2]) for r in after[2]))

    def test_insufficient_samples_still_writes_report_without_param_change(self):
        self._set_performance(navs=[300000, 290000], closed=2, signals=3)
        before = self._snapshot()
        self._run_review()
        after = self._snapshot()
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertTrue(any("样本不足" in (r[1] + r[2]) for r in after[2]))

    def test_repeated_run_is_idempotent_and_never_accumulates_versions(self):
        self._set_performance(navs=[300000, 290000, 280000], closed=8, signals=12)
        before = self._snapshot()
        self._run_review()
        self._run_review()
        after = self._snapshot()
        self.assertEqual(before[0], after[0], "重复执行不应改写参数")

        self.assertEqual(len(after[2]), 1, "同周复盘应幂等覆盖而不是追加")

    def test_unverified_closes_do_not_trigger_a_param_suggestion(self):
        self._set_performance(navs=[300000, 290000, 280000], closed=8,
                              signals=12, execution_verified=False)
        before = self._snapshot()
        self._run_review()
        after = self._snapshot()
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertTrue(any("样本不足" in (r[1] + r[2]) for r in after[2]))


if __name__ == "__main__":
    unittest.main()
