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
import paper_entry_decision as PED
import paper_risk_market_inputs as PRMI


BACKEND = Path(__file__).resolve().parent


class ImportBoundaryTests(unittest.TestCase):
    def test_extracted_decisions_cannot_acquire_data_or_import_project_modules(self):
        project_modules = {path.stem for path in BACKEND.glob("*.py")}
        for name in ("paper_candidate_projection", "paper_entry_decision", "paper_candidate_live_fields"):
            allowed = {"paper_candidate_live_fields"} if name == "paper_candidate_projection" else set()
            if name == "paper_entry_decision":
                allowed = {"paper_sizing"}  # Legacy slice facade → stdlib-only quantity owner.
            tree = ast.parse((BACKEND / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".")[0] for alias in node.names}
                    self.assertFalse((roots & project_modules) - allowed, name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[0], project_modules - allowed, name)
                if isinstance(node, ast.Call):
                    called = ast.unparse(node.func)
                    self.assertNotIn(called.split(".")[-1], {
                        "open", "read_csv", "read_sql", "connect", "execute",
                        "urlopen", "request", "now", "today", "time",
                    }, f"{name}: {called}")

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

    def test_risk_acquisition_preserves_inputs_and_optional_failure(self):
        """新闻/资金流与报价并行后，只保留仍然有意义的次序约束。

        市场背景必须先取（空持仓时会短路整条链）；``news_meta`` 必须在新闻
        之后读取；报价仍在调用线程执行。其余两条支路互不依赖，因此不再断言
        它们与报价的先后。
        """
        calls = []
        ports = PRMI.RiskMarketInputs(
            market_context=lambda day: calls.append("market") or {},
            quotes=lambda codes, **kwargs: calls.append(("quotes", codes)) or {"quote": True},
            news=lambda names: calls.append("news") or [],
            news_meta=lambda: calls.append("news_meta") or {"current": True},
            trajectories=mock.Mock(side_effect=RuntimeError("optional source down")),
        )
        result = PRMI.load(day="2026-09-01", positions=[{"code": "000002", "name": "sample"}], candidate_codes={"000001"}, ports=ports)
        self.assertEqual(calls[0], "market")
        self.assertIn(("quotes", ["000001", "000002"]), calls)
        self.assertIn("news", calls)
        self.assertEqual(calls[-1], "news_meta")
        self.assertEqual(result["quote_map"], {"quote": True})
        self.assertEqual(result["flow_trajectory_map"], {
            "000002": {"status": "source_unavailable", "code": "000002",
                       "error": "RuntimeError", "score_applied": False},
        })
        ports.trajectories.assert_called_once_with(["000002"], asof_day="2026-09-01")
        self.assertEqual(replace(ports, trajectories=lambda *args, **kwargs: {}).news, ports.news)


if __name__ == "__main__":
    unittest.main()
