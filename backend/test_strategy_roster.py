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
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import paper_account_specs as ACS
import paper_trading as PT
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

    def test_spec_drift_classifies_every_difference(self):
        """目标与声明之间的每一条差异都必须有归属，不允许"待办"悄悄堆着。"""
        drift = ROSTER.spec_drift(ACS.ACCOUNT_SPECS, ACS.RISK_PROFILES)
        open_rows = [row for row in drift if row["status"] == "open"]
        self.assertEqual(open_rows, [])
        fields = {(row["strategy_id"], row["field"]) for row in drift}
        self.assertIn(("reported_profit_breakout", "max_exposure"), fields)
        self.assertIn(("trend_pullback", "risk_profile.drawdown"), fields)
        self.assertNotIn("sector_rotation", {row["strategy_id"] for row in drift})
        for row in drift:
            self.assertTrue(row["reason"], row)
        missing = ROSTER.spec_drift({}, {})
        self.assertEqual(len(missing), 3)
        self.assertTrue(all(row["status"] == "open" for row in missing))

    def test_conservative_declaration_was_actually_tightened(self):
        """保守化必须落成数字，而不是只改名字。"""
        spec = ACS.ACCOUNT_SPECS["reported_profit_breakout"]
        profile = ACS.RISK_PROFILES[spec["risk_profile"]]
        self.assertEqual(profile["max_weight"], 0.12)
        self.assertEqual(profile["single_risk"], 0.006)
        self.assertLessEqual(spec["max_open_runup_pct"], 0.012)
        self.assertLessEqual(spec["gap_q2"][1], 0.03)
        self.assertLessEqual(spec["entry_pct_high"], 4.0)
        # 全池熔断字段必须保持原值：它们不是单策略止损。
        self.assertEqual(profile["daily_loss"], 0.035)
        self.assertEqual(profile["drawdown"], 0.11)

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
                   initial_cash REAL, cash REAL, params TEXT, version TEXT, style TEXT,
                   updated_at TEXT)""")
        self.conn.execute(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT, capital REAL,"
            " enabled_strategies TEXT)")
        self.conn.execute(
            """CREATE TABLE paper_parameter_versions(
                   id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, account_id TEXT,
                   version TEXT, style TEXT, params TEXT, reason TEXT,
                   effective_date TEXT, created_at TEXT)""")
        self.conn.execute(
            """CREATE TABLE paper_audit(
                   id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event TEXT,
                   detail TEXT, created_at TEXT)""")
        self.conn.execute(
            "INSERT INTO paper_cycles VALUES(?,?,?,?)",
            (1, "running", 300000.0, '["tq_breakout","trend_pullback","sector_rotation",'
                                    '"reported_profit_breakout","main_force_top10"]'))
        for strategy_id in ("tq_breakout", "trend_pullback", "sector_rotation",
                            "reported_profit_breakout", "main_force_top10"):
            self.conn.execute(
                "INSERT INTO paper_accounts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (strategy_id, strategy_id, "running", 1, 60000.0, 50000.0, "{}",
                 "v3.0", "trend", "2026-10-01 09:00:00"))
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

    def test_budget_weights_are_appended_and_verified(self):
        self.conn.execute(
            "UPDATE paper_accounts SET params=? WHERE id='trend_pullback'",
            (json.dumps({"adaptive_allocation": {"status": "active", "weight_pct": 26.07},
                         "other_cap": 0.5}),))
        self.conn.commit()
        result = self.module.write_budget_weights(self.path, effective_date="2026-10-26")
        self.assertTrue(result["verified"]["ok"], result["verified"])
        weights = {row["strategy_id"]: row["weight_pct"] for row in result["steps"]}
        self.assertEqual(weights, {"tq_breakout": 20.0, "trend_pullback": 50.0,
                                   "reported_profit_breakout": 30.0})
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            params = json.loads(conn.execute(
                "SELECT params FROM paper_accounts WHERE id='trend_pullback'").fetchone()[0])
            self.assertEqual(params["other_cap"], 0.5)
            self.assertEqual(params["adaptive_allocation"]["weight_pct"], 50.0)
            self.assertEqual(params["adaptive_allocation_previous"]["weight_pct"], 26.07)
            rows = conn.execute(
                "SELECT account_id,params,created_at FROM paper_parameter_versions"
                " ORDER BY id").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertIn("+08:00", rows[0]["created_at"])
            audits = conn.execute(
                "SELECT COUNT(*) FROM paper_audit WHERE event='roster_budget_declared'").fetchone()[0]
            self.assertEqual(audits, 3)
        finally:
            conn.close()
        again = self.module.write_budget_weights(self.path, effective_date="2026-10-26")
        self.assertEqual({row["result"] for row in again["steps"]}, {"already_declared"})

    def test_budget_weights_never_touch_exit_only_accounts(self):
        self.module.write_budget_weights(self.path, effective_date="2026-10-26")
        conn = sqlite3.connect(self.path)
        try:
            for strategy_id in ("sector_rotation", "main_force_top10"):
                raw = conn.execute("SELECT params FROM paper_accounts WHERE id=?",
                                   (strategy_id,)).fetchone()[0]
                self.assertEqual(json.loads(raw), {})
        finally:
            conn.close()


class CycleBudgetSeedingTests(unittest.TestCase):
    """新周期开局按名册写入初始预算 20/50/30，并且真的被分配口径读到。

    这是"初始预算固定、后续由自进化调整"的分界点：开局由名册写死，
    之后的 ``paper_dynamic_allocation`` / ``evolution_apply`` 才在边界内改动。
    """

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="roster-budget-")
        self.addCleanup(folder.cleanup)
        patch = mock.patch.object(PT, "DB_PATH", os.path.join(folder.name, "paper.sqlite3"))
        patch.start()
        self.addCleanup(patch.stop)
        PT.init_db()
        self.summary, self.cycle = PT.start_new_cycle(capital=300_000, include_dashboard=False)

    def test_initial_budget_is_written_for_the_three_formal_strategies(self):
        with PT._db() as conn:
            params = {row["id"]: json.loads(row["params"] or "{}")
                      for row in conn.execute("SELECT id,params FROM paper_accounts")}
        expected = {"tq_breakout": 20.0, "trend_pullback": 50.0,
                    "reported_profit_breakout": 30.0}
        for strategy_id, weight in expected.items():
            allocation = params[strategy_id]["adaptive_allocation"]
            self.assertEqual(allocation["weight_pct"], weight)
            self.assertEqual(allocation["status"], "active")
            self.assertEqual(allocation["roster_version"], ROSTER.ROSTER_VERSION)
            self.assertEqual(allocation["declared_policy"], ROSTER.BUDGET_POLICY_VERSION)
        for strategy_id in ROSTER.exit_only_ids():
            self.assertEqual(params[strategy_id], {})

    def test_parameter_history_carries_an_aware_decision_instant(self):
        with PT._db() as conn:
            rows = conn.execute(
                "SELECT account_id,params,created_at,effective_date"
                " FROM paper_parameter_versions WHERE cycle_id=?",
                (self.cycle["id"],)).fetchall()
        by_account = {row["account_id"]: row for row in rows}
        seeded = by_account["trend_pullback"]
        self.assertEqual(json.loads(seeded["params"])["adaptive_allocation"]["weight_pct"], 50.0)
        self.assertIsNotNone(
            __import__("datetime").datetime.fromisoformat(seeded["created_at"]).tzinfo)
        self.assertEqual(json.loads(by_account["sector_rotation"]["params"]), {})

    def test_canonical_resolution_reads_the_seeded_budget(self):
        import datetime as dt

        import portfolio_allocation_weights as PAW
        eligible = set(ROSTER.formal_strategy_ids())
        decision_at = dt.datetime.now().astimezone().replace(microsecond=0) + dt.timedelta(seconds=5)
        with PT._db() as conn:
            rows = PAW.read_canonical_allocation_weight_owner_rows(
                conn, eligible_account_ids=eligible, cycle_id=self.cycle["id"])
            pins = []
            for strategy_id in sorted(eligible):
                version = SR.get_version(strategy_id, conn=conn)
                pins.append({"account_id": strategy_id, "strategy_id": strategy_id,
                             "strategy_version": version.version,
                             "strategy_checksum": version.checksum})
        resolved = PAW.resolve_canonical_allocation_weights(
            rows, eligible_account_ids=eligible, cycle_id=self.cycle["id"],
            asof_day=decision_at.date().isoformat(),
            decision_at=decision_at.isoformat(), strategy_pins=pins)
        self.assertEqual(resolved["weights"], {"tq_breakout": 0.2, "trend_pullback": 0.5,
                                              "reported_profit_breakout": 0.3})


if __name__ == "__main__":
    unittest.main()