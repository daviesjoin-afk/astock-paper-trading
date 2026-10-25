# -*- coding: utf-8 -*-
"""三策略名册与五变三切换脚本的契约测试。

锁住四件事：

1. 名册只有三个正式策略，预算 20% / 50% / 30% 合计恰好 100%；
2. 两个旧策略只退出独立账户，不搬移持仓、成本或资金，且**新开仓**口径给出理由；
3. 名册是纯声明：不 import 数据库、网络、执行或注册表真相；
4. ``scripts/three_strategy_switch.py`` 默认只读；``--apply`` 只改生命周期，
   账户/资金/订单计数不变，重复执行幂等。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sqlite3
import tempfile
import unittest

import paper_account_specs as ACS
import strategy_lifecycle as SL
import strategy_registry as SR
import strategy_roster as ROSTER

BACKEND = pathlib.Path(__file__).resolve().parent
SCRIPT_PATH = BACKEND.parent / "scripts" / "three_strategy_switch.py"

FORBIDDEN_IMPORT_ROOTS = {
    "sqlite3", "requests", "urllib", "http", "socket", "subprocess", "threading",
    "asyncio", "paper_trading", "paper_storage", "paper_repository", "strategy_registry",
    "strategy_runtime", "strategy_service", "order_intent", "execution_planner",
    "execution_dispatch", "user_strategy_participation", "main", "api_paper",
}


def _load_script():
    spec = importlib.util.spec_from_file_location("three_strategy_switch", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StrategyRosterContractTests(unittest.TestCase):
    def test_exactly_three_formal_roles_with_100_percent_budget(self):
        self.assertEqual(ROSTER.formal_strategy_ids(),
                         ("tq_breakout", "trend_pullback", "reported_profit_breakout"))
        shares = ROSTER.budget_shares()
        self.assertAlmostEqual(sum(shares.values()), 1.0, places=9)
        self.assertEqual(shares, {"tq_breakout": 0.20, "trend_pullback": 0.50,
                                  "reported_profit_breakout": 0.30})
        roles = {item.role for item in ROSTER.formal_roles()}
        self.assertEqual(roles, {ROSTER.AGGRESSIVE, ROSTER.BALANCED, ROSTER.CONSERVATIVE})

    def test_roles_are_ordered_aggressive_balanced_conservative(self):
        names = [item.name for item in ROSTER.formal_roles()]
        self.assertEqual(names, ["激进 · AI 增强做 T", "稳健 · 趋势波段", "保守 · 质量防守"])

    def test_only_the_aggressive_role_runs_intraday_t(self):
        self.assertEqual([item.strategy_id for item in ROSTER.formal_roles() if item.intraday_t],
                         ["tq_breakout"])

    def test_conservative_role_holds_least_risk_of_the_three(self):
        roles = {item.role: item for item in ROSTER.formal_roles()}
        conservative = roles[ROSTER.CONSERVATIVE]
        for other in (roles[ROSTER.AGGRESSIVE], roles[ROSTER.BALANCED]):
            self.assertLess(conservative.within_budget_exposure, other.within_budget_exposure)
            self.assertLess(conservative.within_budget_single, other.within_budget_single)
            self.assertLess(conservative.single_trade_risk, other.single_trade_risk)
            self.assertLess(conservative.drawdown_stop, other.drawdown_stop)

    def test_exit_only_set_is_disjoint_and_never_overlaps_formal_roles(self):
        self.assertEqual(ROSTER.exit_only_ids(), ("sector_rotation", "main_force_top10"))
        self.assertFalse(set(ROSTER.exit_only_ids()) & set(ROSTER.formal_strategy_ids()))
        for strategy_id in ROSTER.exit_only_ids():
            self.assertTrue(ROSTER.is_exit_only(strategy_id))
            self.assertIsNone(ROSTER.role_for(strategy_id))
        for strategy_id in ROSTER.formal_strategy_ids():
            self.assertTrue(ROSTER.is_formal(strategy_id))
            self.assertFalse(ROSTER.is_exit_only(strategy_id))

    def test_entry_block_reason_only_for_exit_only_ids(self):
        for strategy_id in ROSTER.formal_strategy_ids():
            self.assertIsNone(ROSTER.entry_block_reason(strategy_id))
        for strategy_id in ROSTER.exit_only_ids():
            reason = ROSTER.entry_block_reason(strategy_id)
            self.assertIn("退出独立资金账户", reason)
        self.assertIsNone(ROSTER.entry_block_reason("user_strategy_xyz"))
        self.assertIsNone(ROSTER.entry_block_reason(None))

    def test_nav_limits_scale_budget_by_within_budget_ratio(self):
        conservative = ROSTER.role_for("reported_profit_breakout")
        limits = conservative.nav_limits()
        self.assertAlmostEqual(limits["exposure_cap_nav"], 0.30 * 0.35, places=9)
        self.assertAlmostEqual(limits["single_name_cap_nav"], 0.30 * 0.12, places=9)

    def test_pool_budget_example_is_a_display_not_a_cash_injection(self):
        example = ROSTER.pool_budget_example(300000)
        budgets = {row["strategy_id"]: row["budget"] for row in example["strategies"]}
        self.assertEqual(budgets, {"tq_breakout": 60000.0, "trend_pullback": 150000.0,
                                   "reported_profit_breakout": 90000.0})
        self.assertAlmostEqual(example["max_holding_total"], 42000 + 97500 + 31500, places=2)
        self.assertIn("不是新增现金", example["note"])

    def test_allocation_declaration_uses_percentage_and_requires_explicit_date(self):
        declaration = ROSTER.allocation_declaration("trend_pullback", effective_date="2026-10-26")
        allocation = declaration["params"]["adaptive_allocation"]
        self.assertEqual(allocation["weight_pct"], 50.0)
        self.assertEqual(allocation["status"], "active")
        self.assertEqual(declaration["effective_date"], "2026-10-26")
        with self.assertRaises(ValueError):
            ROSTER.allocation_declaration("sector_rotation", effective_date="2026-10-26")
        with self.assertRaises(ValueError):
            ROSTER.allocation_declaration("tq_breakout", effective_date="")

    def test_factor_policy_states_its_own_boundary(self):
        policy = ROSTER.factor_policy()
        self.assertEqual(set(policy), {"prioritize", "reduce", "not_admitted", "boundary"})
        self.assertIn("13 个有效日期", policy["boundary"])
        self.assertTrue(any("mom_short" in item for item in policy["reduce"]))
        self.assertTrue(any("新闻人气" in item for item in policy["not_admitted"]))

    def test_spec_drift_reports_every_unapplied_target(self):
        drift = ROSTER.spec_drift(ACS.ACCOUNT_SPECS, ACS.RISK_PROFILES)
        fields = {(row["strategy_id"], row["field"]) for row in drift}
        self.assertIn(("reported_profit_breakout", "max_exposure"), fields)
        self.assertIn(("trend_pullback", "risk_profile.drawdown"), fields)
        self.assertNotIn("sector_rotation", {row["strategy_id"] for row in drift})
        missing = ROSTER.spec_drift({}, {})
        self.assertEqual(len(missing), 3)

    def test_roster_module_is_pure_declaration(self):
        tree = ast.parse((BACKEND / "strategy_roster.py").read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots - {"__future__", "dataclasses"}, set())
        self.assertFalse(roots & FORBIDDEN_IMPORT_ROOTS)


class ThreeStrategySwitchScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "paper.sqlite3")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        SR.ensure_schema(self.conn)
        self.conn.execute(
            """CREATE TABLE paper_accounts(
                   id TEXT PRIMARY KEY, name TEXT, status TEXT, cycle_id INTEGER,
                   initial_cash REAL, cash REAL, params TEXT)""")
        self.conn.execute(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT, capital REAL,"
            " enabled_strategies TEXT)")
        self.conn.execute(
            "INSERT INTO paper_cycles VALUES(?,?,?,?)",
            (1, "running", 300000.0, '["tq_breakout","trend_pullback","sector_rotation",'
                                    '"reported_profit_breakout","main_force_top10"]'))
        for strategy_id in ("tq_breakout", "trend_pullback", "sector_rotation",
                            "reported_profit_breakout", "main_force_top10"):
            self.conn.execute(
                "INSERT INTO paper_accounts VALUES(?,?,?,?,?,?,?)",
                (strategy_id, strategy_id, "running", 1, 60000.0, 50000.0, "{}"))
        self.conn.commit()
        self.module = _load_script()

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def _state(self, strategy_id):
        version = SR.get_version(strategy_id, conn=self.conn)
        return SL.get_state(self.conn, strategy_id, version.version,
                            checksum=version.checksum)["state"]

    def test_default_audit_is_read_only_and_plans_retirement(self):
        before = SR.active_ids(conn=self.conn)
        report = self.module.audit(self.path)
        self.assertEqual(report["mode"], "read_only")
        self.assertEqual(set(report["strategies"]), set(self.module.ROSTER_IDS))
        self.assertEqual(SR.active_ids(conn=self.conn), before)
        actions = {row["strategy_id"]: row["action"] for row in report["retirement_plan"]}
        self.assertEqual(actions, {"sector_rotation": "transition_to_retiring",
                                   "main_force_top10": "transition_to_retiring"})
        self.assertTrue(any("20/50/30" in item for item in report["warnings"]))

    def test_confirmation_must_name_exactly_the_two_exit_only_ids(self):
        for confirmed in ([], ["sector_rotation"], ["sector_rotation", "trend_pullback"],
                          ["sector_rotation", "main_force_top10", "trend_pullback"]):
            with self.subTest(confirmed=confirmed):
                with self.assertRaises(SystemExit):
                    self.module.apply_retirement(self.path, confirmed)
        self.assertEqual(self._state("sector_rotation"), "paper")

    def test_apply_retires_only_the_two_and_leaves_ledger_untouched(self):
        before = self.module.audit(self.path)["counts"]
        result = self.module.apply_retirement(
            self.path, ["sector_rotation", "main_force_top10"])
        self.assertTrue(result["verified"]["ok"], result["verified"])
        self.assertEqual(self._state("sector_rotation"), "retiring")
        self.assertEqual(self._state("main_force_top10"), "retiring")
        for strategy_id in ("tq_breakout", "trend_pullback", "reported_profit_breakout"):
            self.assertEqual(self._state(strategy_id), "paper")
        self.assertEqual(SR.active_ids(conn=self.conn),
                         ("tq_breakout", "trend_pullback", "reported_profit_breakout"))
        after = self.module.audit(self.path)["counts"]
        self.assertEqual(after, before)
        self.assertEqual(after["paper_accounts"], 5)
        self.assertEqual(after["cash_total"], 250000.0)

    def test_apply_is_idempotent(self):
        self.module.apply_retirement(self.path, ["sector_rotation", "main_force_top10"])
        again = self.module.apply_retirement(self.path, ["sector_rotation", "main_force_top10"])
        results = {row["strategy_id"]: row["result"] for row in again["steps"]}
        self.assertEqual(results, {"sector_rotation": "already_retiring",
                                   "main_force_top10": "already_retiring"})
        self.assertTrue(again["verified"]["ok"])

    def test_unavailable_lifecycle_state_rolls_back_everything(self):
        self.conn.execute(
            "DELETE FROM strategy_lifecycle_state WHERE strategy_id='main_force_top10'")
        self.conn.commit()
        with self.assertRaises(SystemExit):
            self.module.apply_retirement(self.path, ["sector_rotation", "main_force_top10"])
        self.assertEqual(self._state("sector_rotation"), "paper")
        self.assertIn("sector_rotation", SR.active_ids(conn=self.conn))


if __name__ == "__main__":
    unittest.main()
