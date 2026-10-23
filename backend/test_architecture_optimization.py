"""Behavior and import-boundary regressions for the overall optimization."""
import ast
from dataclasses import replace
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import pandas as pd

import paper_candidate_projection as PCP
import execution_planner as EP
import paper_entry_decision as PED
import paper_numeric as PNum
import paper_risk_market_inputs as PRMI


BACKEND = Path(__file__).resolve().parent


class ImportBoundaryTests(unittest.TestCase):
    # 候选投影只允许纯依赖：实时字段覆盖、严格数值规约（仅标准库）以及交易
    # 规则里的纯谓语。``limit_pct`` 之类会懒加载因子模块的入口不在允许范围，
    # 因此这里同时校验导入的函数名，而不只是模块名。
    PURE_PROJECTION_DEPENDENCIES = {
        "paper_candidate_live_fields", "paper_numeric", "paper_trading_rules",
    }
    PURE_RULE_NAMES = {"asset_type", "is_st_or_delisting", "security_scope"}

    def test_extracted_decisions_cannot_acquire_data_or_import_project_modules(self):
        project_modules = {path.stem for path in BACKEND.glob("*.py")}
        for name in ("paper_candidate_projection", "paper_entry_decision", "paper_candidate_live_fields"):
            allowed = (self.PURE_PROJECTION_DEPENDENCIES
                       if name == "paper_candidate_projection" else set())
            tree = ast.parse((BACKEND / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".")[0] for alias in node.names}
                    self.assertFalse((roots & project_modules) - allowed, name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    self.assertNotIn(root, project_modules - allowed, name)
                    if name == "paper_candidate_projection" and root == "paper_trading_rules":
                        self.assertLessEqual(
                            {alias.name for alias in node.names}, self.PURE_RULE_NAMES,
                        )
                if isinstance(node, ast.Call):
                    called = ast.unparse(node.func)
                    self.assertNotIn(called.split(".")[-1], {
                        "open", "read_csv", "read_sql", "connect", "execute",
                        "urlopen", "request", "now", "today", "time",
                    }, f"{name}: {called}")

    def test_entry_assessment_model_stays_pure_and_keeps_one_spec_owner(self):
        """入场评分模型不读库、不读时钟，账户声明仍只有主编排一个解析口。"""
        import paper_entry_assessment as PEA
        import paper_trading as PT

        self.assertIs(PT._strategy_entry_assessment, PEA._strategy_entry_assessment)
        self.assertIs(PT._clip01, PEA._clip01)
        self.assertIs(PEA._SPEC_RESOLVER, PT._spec_for)
        for account_id in ("trend_pullback", "tq_breakout", "unknown_account"):
            self.assertEqual(PEA._spec_for(account_id), PT._spec_for(account_id))

        tree = ast.parse((BACKEND / "paper_entry_assessment.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
                self.assertFalse(roots & {"sqlite3", "paper_trading", "data_fetcher"}, roots)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0],
                                 {"sqlite3", "paper_trading", "data_fetcher"})
            if isinstance(node, ast.Call):
                self.assertNotIn(
                    ast.unparse(node.func).split(".")[-1],
                    {"open", "read_csv", "read_sql", "connect", "execute",
                     "urlopen", "request", "now", "today", "time"},
                )

    def test_runtime_fact_owner_only_executes_reads(self):
        tree = ast.parse((BACKEND / "adaptive_runtime_facts.py").read_text(encoding="utf-8"))
        queries = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"execute", "executemany", "executescript"}:
                    self.assertEqual(node.func.attr, "execute")
                    self.assertIsInstance(node.args[0], ast.Constant)
                    query = node.args[0].value
                    self.assertIsInstance(query, str)
                    self.assertTrue(query.lstrip().upper().startswith("SELECT "))
                    queries.append(query)
        self.assertTrue(queries, "guard must cover the extracted reader")

    def run_isolated(self, code):
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=BACKEND,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_route_registration_does_not_load_research_implementations(self):
        self.run_isolated(
            "import sys; import api_adaptive, api_settings; "
            "assert 'adaptive_engine' not in sys.modules; "
            "assert 'self_evolution' not in sys.modules; "
            "assert 'learning_evaluation' not in sys.modules; "
            "assert len(api_adaptive.router.routes) > 0; "
            "assert len(api_settings.router.routes) > 0"
        )

    def test_first_route_use_preserves_legacy_aliases_and_loads_once(self):
        self.run_isolated(
            "import sys, importlib; import api_adaptive, api_settings; "
            "assert 'adaptive_engine' not in sys.modules; "
            "a = api_adaptive.adaptive; "
            "assert a is importlib.import_module('adaptive_engine'); "
            "assert api_adaptive.adaptive is a; assert api_settings.adaptive is a; "
            "assert api_adaptive.SE is importlib.import_module('self_evolution')"
        )

    def test_verticals_and_read_owner_can_be_imported_without_engines(self):
        self.run_isolated(
            "import sys; import execution_planner, manual_orders, dashboard_queries, adaptive_runtime_facts; "
            "assert 'paper_trading' not in sys.modules; assert 'adaptive_engine' not in sys.modules; "
            "assert execution_planner.policy_for('tq_breakout').chase_lane == 'momentum'"
        )

    def test_verticals_have_no_literal_reverse_engine_imports(self):
        for name in ("execution_planner", "manual_orders", "dashboard_queries", "adaptive_runtime_facts", "evolution_apply"):
            tree = ast.parse((BACKEND / f"{name}.py").read_text(encoding="utf-8"))
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            self.assertFalse(roots & {"paper_trading", "adaptive_engine"}, name)

    def test_bound_capabilities_follow_current_runtime_overrides(self):
        import paper_trading as P
        import execution_planner as EP
        import dashboard_queries as DQ
        import manual_orders as MO
        with mock.patch.object(P, "_now", return_value="late-bound"):
            self.assertEqual(EP._pt()._now(), "late-bound")
            self.assertEqual(MO._dependencies()._now(), "late-bound")
        with mock.patch.object(P, "schedule_status", return_value={"late": True}):
            self.assertEqual(DQ._dependencies().schedule_status(), {"late": True})
        for deps in (EP._pt(), DQ._dependencies(), MO._dependencies()):
            for value in vars(deps).values():
                self.assertIsNot(value, P)

    def test_execution_rejects_missing_dependencies_without_importing_engine(self):
        self.run_isolated(
            "import sys; import execution_planner as E; "
            "\ntry: E._pt()\nexcept RuntimeError: pass\nelse: raise AssertionError('expected explicit binding')\n"
            "assert 'paper_trading' not in sys.modules"
        )

    def test_runtime_fact_types_keep_the_legacy_identity(self):
        import adaptive_engine as A
        import adaptive_runtime_facts as F
        self.assertIs(A.AdaptiveRunFactProjection, F.AdaptiveRunFactProjection)
        sentinel = object()
        with mock.patch.object(F, "adaptive_run_fact", return_value=sentinel) as read:
            self.assertIs(A.adaptive_run_fact("conn", 1, as_of="2026-09-01"), sentinel)
        read.assert_called_once_with("conn", 1, as_of="2026-09-01")

    def test_lazy_route_alias_overrides_remain_supported(self):
        module = importlib.import_module("api_adaptive")
        fake = mock.Mock()
        fake.ai_settings_schema.return_value = {"settings": "injected"}
        with mock.patch.object(module, "adaptive", fake):
            self.assertEqual(module.ai_settings(), {"settings": "injected"})

    def test_overview_loads_the_engine_in_worker_not_request(self):
        module = importlib.import_module("api_adaptive")
        fake = mock.Mock()
        fake.overview.return_value = {"ready": True}
        with mock.patch.dict(module._overview_cache, {"running": False}, clear=True), \
                mock.patch.object(module.threading, "Thread") as thread, \
                mock.patch.object(module, "_implementation", return_value=fake) as resolve, \
                mock.patch.object(module, "_store_overview_snapshot"):
            module._schedule_overview_refresh()
            resolve.assert_not_called()
            thread.return_value.start.assert_called_once()
            thread.call_args.kwargs["target"]()
            resolve.assert_called_once_with("adaptive")
            self.assertEqual(module._overview_cache["data"], {"ready": True})


class ExtractedDecisionTests(unittest.TestCase):
    def test_candidate_lane_reservation_preserves_order_and_limit(self):
        regular = [{"code": str(i)} for i in range(40)]
        hot = [{"code": "hot" + str(i), "candidate_status": "ths_hot_lane"} for i in range(9)]
        original = regular + hot
        result = PCP.select_candidate_lanes(original)
        self.assertEqual(result, regular[:24] + hot[:6])
        self.assertEqual(len(original), 49)

    def test_risk_flag_codes_mirror_the_trading_rule_reasons(self):
        universe = [
            {"code": "600001", "name": "普通甲"},
            {"code": "600002", "name": "普通乙", "risk_flag": 1},
            {"code": "600003", "name": "ST测试"},
            {"code": "600004", "name": "退市风险"},
            {"code": "600005"},
        ]
        self.assertEqual(PCP.risk_flag_codes(universe), {"600002", "600003", "600004"})
        self.assertEqual(PCP.risk_flag_codes(None), set())

    def test_runtime_filter_keeps_order_and_only_drops_unexecutable_rows(self):
        picks = [
            {"code": "600001", "price": 10.0},
            {"code": "600002", "price": 0.0},
            {"code": "600003", "price": "--"},
            {"code": "600004"},
            {"code": "600005", "price": 5.0},
        ]
        kept = PCP.filter_executable_candidates(picks, mode="swing", excluded_codes={"600005"})
        self.assertEqual([pick["code"] for pick in kept], ["600001"])
        # An expensive one-lot candidate is never deleted at selection time.
        expensive = [{"code": "600009", "price": 1200.0}]
        self.assertEqual(
            PCP.filter_executable_candidates(expensive, mode="swing"),
            expensive,
        )

    def test_intraday_t_filter_keeps_only_t_plus_one_assets(self):
        picks = [
            {"code": "600001", "name": "主板股", "price": 10.0},
            {"code": "510300", "name": "沪深300ETF", "price": 4.0},
            {"code": "159915", "name": "创业板ETF", "price": 2.0},
        ]
        kept = PCP.filter_executable_candidates(picks, mode="intraday_t")
        self.assertEqual([pick["code"] for pick in kept], ["600001"])
        self.assertEqual(len(PCP.filter_executable_candidates(picks, mode="swing")), 3)

    def test_live_overlay_preserves_factors_and_missing_value_behavior(self):
        table = pd.DataFrame({"price": [1., 2.], "pct": [3., 4.], "ma20": [5., 6.]}, index=["a", "b"])
        live = {"a": {"price": 10., "pct": "--", "quote_at": "2026-09-01 10:00:00"},
                "outside": {"price": 99.}}
        result = PCP.overlay_live_fields(table, live, "2026-08-31")
        self.assertEqual(result.loc["a", "price"], 10.)
        self.assertEqual(result.loc["a", "pct"], 3.)
        self.assertEqual(result.loc["a", "ma20"], 5.)
        self.assertEqual(list(result.index), ["a", "b"])
        self.assertEqual(result.loc["a", "quote_source"], "live_snapshot")
        self.assertEqual(result.loc["a", "historical_factor_date"], "2026-08-31")

    def economics(self, **overrides):
        kwargs = dict(
            reasons=[], qty=100, fill_price=10.0, price=10.0, pending_cash=0.0,
            shared_cash=900000.0, minimum_order_amount=0.0, lot_size=100,
            economics_policy=None, min_cost_edge=None, sizing={},
            fee_estimator=EP.estimate_execution_fees,
            sell_price_estimator=EP.estimated_fill_price, num=PNum.num,
        )
        kwargs.update(overrides)
        return PED.assess_order_economics(**kwargs)

    def test_zero_cash_is_a_hard_veto_while_a_short_balance_stays_soft(self):
        empty = self.economics(shared_cash=0.0)
        self.assertEqual(empty["soft_reasons"], [])
        self.assertEqual(empty["reasons"], ["共享资金池暂无可用现金"])
        short = self.economics(shared_cash=500.0)
        self.assertEqual(len(short["reasons"]), 1)
        self.assertEqual(short["soft_reasons"], short["reasons"])

    def test_risk_only_binding_is_terminal_but_budget_bindings_are_waitable(self):
        terminal = self.economics(qty=50, sizing={"constraint_shares": {"risk": 0.0}})
        self.assertEqual(terminal["soft_reasons"], [])
        self.assertIn("结构性不足", terminal["reasons"][0])
        waitable = self.economics(qty=50, sizing={"constraint_shares": {"cash": 50.0}})
        self.assertEqual(waitable["soft_reasons"], waitable["reasons"])
        self.assertIn("可用现金", waitable["reasons"][0])

    def test_dust_order_floor_uses_the_dynamic_minimum_amount(self):
        result = self.economics(minimum_order_amount=6000.0)
        self.assertEqual(result["soft_reasons"], result["reasons"])
        self.assertIn("低于动态最小建仓金额", result["reasons"][0])

    def test_economics_policy_requires_edge_to_cover_the_round_trip_cost(self):
        policy = {"min_effective_order_amount": 2000.0, "min_expected_edge_pct": 0.003}
        too_small = self.economics(economics_policy=policy)
        self.assertIn("短线有效委托至少", too_small["reasons"][0])
        expensive = self.economics(
            economics_policy={"min_effective_order_amount": 0.0, "min_expected_edge_pct": 0.003},
            price=9.0,
        )
        self.assertIn("未覆盖估算往返成本", expensive["reasons"][0])
        self.assertGreater(expensive["sizing"]["estimated_round_trip_cost"], 0.0)

    def test_one_lot_arithmetic_is_reported_without_touching_the_input_sizing(self):
        sizing = {"binding_constraints": ["cash"]}
        result = self.economics(qty=300, sizing=sizing)
        self.assertEqual(result["amount"], 3000.0)
        self.assertEqual(result["fees"], 0.3)
        self.assertEqual(result["sizing"]["one_lot_amount"], 1000.0)
        self.assertEqual(result["sizing"]["qty_final"], 300)
        self.assertEqual(result["sizing"]["cash_available"], 900000.0)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(sizing, {"binding_constraints": ["cash"]})

    def test_slice_continuation_preserves_plan_and_current_count(self):
        payload = {"entry_slice_policy": 2, "entry_slices": {"plan": [100, 200, 300], "filled": 1, "slices": 2}}
        before = json.dumps(payload, sort_keys=True)
        qty, state, sizing, detail = PED.prepare_entry_slice(
            payload=payload, account={}, qty=500, asof_day="2026-09-01",
            continuation=True, existing_plan=[100, 200, 300], lot_size=100,
            plan_builder=mock.Mock(side_effect=AssertionError("existing plan must be preserved")),
        )
        self.assertEqual(qty, 200)
        self.assertEqual(sizing["entry_slice_index"], 2)
        self.assertEqual(detail["slices"], 3)
        self.assertEqual(state["slices"], 2)
        self.assertEqual(before, json.dumps(payload, sort_keys=True))

    def test_invalid_slice_policy_keeps_single_slice_fallback(self):
        result = PED.prepare_entry_slice(
            payload={"entry_slice_policy": "invalid"}, account={}, qty=500,
            asof_day="2026-09-01", continuation=False, existing_plan=[], lot_size=100,
            plan_builder=mock.Mock(side_effect=AssertionError("single slice does not build a plan")),
        )
        self.assertEqual(result, (500, None, {}, None))

    def test_empty_risk_scope_does_not_acquire_external_evidence(self):
        ports = PRMI.RiskMarketInputs(
            market_context=mock.Mock(return_value={"light": "green"}),
            quotes=mock.Mock(), news=mock.Mock(), news_meta=mock.Mock(return_value={}), trajectories=mock.Mock(),
        )
        result = PRMI.load(day="2026-09-01", positions=[], candidate_codes={"000001"}, ports=ports)
        self.assertEqual(result["quote_map"], {})
        ports.quotes.assert_not_called()
        ports.news.assert_not_called()
        ports.trajectories.assert_not_called()

    def test_risk_acquisition_preserves_order_and_optional_failure(self):
        calls = []
        ports = PRMI.RiskMarketInputs(
            market_context=lambda day: calls.append("market") or {},
            quotes=lambda codes, **kwargs: calls.append(("quotes", codes)) or {"quote": True},
            news=lambda names: calls.append("news") or [],
            news_meta=lambda: calls.append("news_meta") or {"current": True},
            trajectories=mock.Mock(side_effect=RuntimeError("optional source down")),
        )
        result = PRMI.load(day="2026-09-01", positions=[{"code": "000002", "name": "sample"}], candidate_codes={"000001"}, ports=ports)
        self.assertEqual(calls, ["market", ("quotes", ["000001", "000002"]), "news", "news_meta"])
        self.assertEqual(result["quote_map"], {"quote": True})
        self.assertEqual(result["flow_trajectory_map"], {})
        self.assertEqual(replace(ports, trajectories=lambda *args, **kwargs: {}).news, ports.news)


if __name__ == "__main__":
    unittest.main()
