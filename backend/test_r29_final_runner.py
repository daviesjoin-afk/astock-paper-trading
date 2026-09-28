"""Fail-closed R29 runner/replay and dependency architecture contracts."""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import experiment_pit_validation as PV
import experiment_validation_runner as RUNNER
import historical_market_archive as HMA
import historical_session_calendar as HSC
import historical_universe_archive as HUA
import strategy_registry as SR
import tradability_archive as TA
import walk_forward_validation as WFV
import test_experiment_pit_validation as FIXTURES
import test_experiment_execution_model as EXEC_FIXTURES


def _field(name):
    return {"op": "gt", "left": {"op": "field", "name": name},
            "right": {"op": "const", "value": 0}}


class R29DependencyTests(unittest.TestCase):
    def test_price_only_has_no_fundamental_dependency(self):
        deps = PV.strategy_dsl_dependencies({"op": "strategy", "rule": _field("close")})
        self.assertEqual(["close"], deps["price_fields"])
        self.assertEqual([], deps["financial_fields"])
        self.assertEqual([], deps["fund_flow_fields"])

    def test_financial_and_flow_dependencies_are_explicit(self):
        financial = PV.strategy_dsl_dependencies({"op": "strategy", "rule": _field("roe")})
        flow = PV.strategy_dsl_dependencies({"op": "strategy", "rule": _field("main_net_inflow")})
        self.assertEqual(["roe"], financial["financial_fields"])
        self.assertEqual(["main_net_inflow"], flow["fund_flow_fields"])


class R29RunnerTests(unittest.TestCase):
    def test_strategy_version_mismatch_is_unavailable(self):
        spec = FIXTURES._spec()
        version = SR.StrategyVersion(
            strategy_id=spec.strategy.strategy_id, version=spec.strategy.version + 1,
            checksum=spec.strategy.checksum,
            definition={"dsl_ast": {"op": "strategy", "rule": {"op": "const", "value": True}}},
            created_at="2026-01-01T00:00:00+00:00", created_by="test",
        )
        output = RUNNER.run_validation(
            spec, runner_code_revision=spec.code_revision, strategy_version=version,
            dataset_manifest=None, samples=(), walk_forward_config=None,
            session_calendar=None, market_archive_repository=None,
            market_archive_fingerprint=spec.market_data_fingerprint,
            universe_archive_repository=None, universe_archive_fingerprint=spec.universe_fingerprint,
            tradability_repository=None,
        )
        self.assertEqual("strategy_identity_mismatch", output["result"]["failure_reason"])

    def test_code_revision_mismatch_is_unavailable_without_metrics(self):
        spec = FIXTURES._spec()
        output = RUNNER.run_validation(
            spec, runner_code_revision="f" * 40, strategy_version=None,
            dataset_manifest=None, samples=(), walk_forward_config=None,
            session_calendar=None, market_archive_repository=None,
            market_archive_fingerprint=spec.market_data_fingerprint,
            universe_archive_repository=None, universe_archive_fingerprint=spec.universe_fingerprint,
            tradability_repository=None,
        )
        self.assertEqual("unavailable", output["result"]["status"])
        self.assertEqual("code_revision_mismatch", output["result"]["failure_reason"])
        self.assertTrue(all(value is None for value in output["result"]["metrics"].values()))

    def test_null_dsl_does_not_resolve_current_python_strategy(self):
        spec = FIXTURES._spec()
        version = SR.StrategyVersion(
            strategy_id=spec.strategy.strategy_id, version=spec.strategy.version,
            checksum=spec.strategy.checksum, definition={"implementation_key": "trend_pullback"},
            created_at="2026-01-01T00:00:00+00:00", created_by="test",
        )
        output = RUNNER.run_validation(
            spec, runner_code_revision=spec.code_revision, strategy_version=version,
            dataset_manifest=None, samples=(), walk_forward_config=None,
            session_calendar=None, market_archive_repository=None,
            market_archive_fingerprint=spec.market_data_fingerprint,
            universe_archive_repository=None, universe_archive_fingerprint=spec.universe_fingerprint,
            tradability_repository=None,
        )
        self.assertEqual("strategy_replay_definition_unavailable",
                         output["result"]["failure_reason"])

    def test_runner_requests_open_evidence_for_suspended_member_without_a_bar(self):
        code = "000001.SH"
        first, second = "2026-01-05", "2026-01-06"
        market_conn = sqlite3.connect(":memory:")
        universe_conn = sqlite3.connect(":memory:")
        tradability_conn = sqlite3.connect(":memory:")
        try:
            market = HMA.HistoricalMarketArchiveRepository(market_conn)
            market_manifest = market.import_raw_market_archive([{
                "code": code, "session": first, "open": 10, "high": 11,
                "low": 9, "close": 10, "volume": 1000, "amount": 10000,
            }], source="runner-fixture", source_revision="1", adjustment="raw")
            universe = HUA.HistoricalUniverseArchiveRepository(universe_conn)
            universe_manifest = universe.import_historical_security_master([{
                "code": code, "listed_from": "2020-01-01", "delisted_at": None,
                "security_type": "equity", "exchange": "SH",
                "observed_at": "2020-01-01T09:00:00+08:00",
            }], coverage_start="2020-01-01", coverage_end=second,
                source="runner-fixture", source_revision="1")
            tradability = TA.TradabilityArchiveRepository(tradability_conn)
            tradability.ensure_schema()
            for session, suspended in ((first, False), (second, True)):
                tradability.save(TA.TradabilityEvidence(
                    code=code, session_date=session, is_listed=True,
                    listing_date="2020-01-01", delisting_date=None, is_st=False,
                    is_suspended=suspended,
                    suspension_reason="fixture" if suspended else None,
                    has_market_quote=not suspended, has_trade_volume=not suspended,
                    is_price_limit_locked=False, price_limit_direction=None,
                    source="runner-fixture", observed_at=f"{session}T09:00:00+08:00",
                    effective_at=f"{session}T09:00:00+08:00"))

            calendar_fingerprint = "a" * 64
            initial = EXEC_FIXTURES._spec()
            spec = replace(initial,
                market_data_fingerprint=market_manifest.archive_fingerprint,
                universe_fingerprint=universe_manifest.universe_archive_fingerprint,
                parameter_set={**initial.parameter_set,
                               "validation_calendar_fingerprint": calendar_fingerprint})
            calendar = HSC.HistoricalSessionCalendar(
                calendar_fingerprint=calendar_fingerprint,
                source_archive_fingerprint=market_manifest.archive_fingerprint,
                benchmark_symbol="000001.SH", coverage_start=first, coverage_end=second,
                sessions=(first, second), session_count=2, content_hash="b" * 64,
                verification_status="fixture-owner-issued")
            version = SR.StrategyVersion(
                strategy_id=spec.strategy.strategy_id, version=spec.strategy.version,
                checksum=spec.strategy.checksum,
                definition={"dsl_ast": {"op": "strategy", "rule": {
                    "op": "gt", "left": {"op": "field", "name": "close"},
                    "right": {"op": "const", "value": 0}}}},
                created_at="2026-01-01T00:00:00+00:00", created_by="test")
            evidence = SimpleNamespace(
                status="ready", reason_codes=(), walk_forward={"windows": []},
                validation_evidence_fingerprint="c" * 64,
                projection=lambda: {"status": "ready"})
            captured_identity = {}

            def ready_with_captured_facts(*args, **kwargs):
                members = RUNNER._universe_rows(
                    universe, universe_manifest.universe_archive_fingerprint, calendar.sessions)
                captured = PV.tradability_replay_projection(members, calendar.sessions, tradability)
                captured["members_by_session"] = members
                kwargs["tradability_capture_out"].update(captured)
                captured_identity["fingerprint"] = captured["fingerprint"]
                tradability.save(TA.TradabilityEvidence(
                    code=code, session_date=second, is_listed=True,
                    listing_date="2020-01-01", delisting_date=None, is_st=False,
                    is_suspended=False, suspension_reason=None, has_market_quote=True,
                    has_trade_volume=True, is_price_limit_locked=False,
                    price_limit_direction=None, source="late-revision",
                    observed_at=f"{second}T09:20:00+08:00",
                    effective_at=f"{second}T09:15:00+08:00"))
                return evidence

            with mock.patch.object(PV, "build_pit_validation_evidence",
                                   side_effect=ready_with_captured_facts):
                output = RUNNER.run_validation(
                    spec, runner_code_revision=spec.code_revision,
                    strategy_version=version,
                    dataset_manifest={"dataset_fingerprint": spec.dataset_fingerprint},
                    samples=(), walk_forward_config=WFV.WalkForwardConfig(
                        min_train_sessions=1, validation_sessions=1, test_sessions=1),
                    session_calendar=calendar, market_archive_repository=market,
                    market_archive_fingerprint=market_manifest.archive_fingerprint,
                    universe_archive_repository=universe,
                    universe_archive_fingerprint=universe_manifest.universe_archive_fingerprint,
                    tradability_repository=tradability)

            self.assertEqual("ready", output["status"])
            self.assertEqual("completed", output["result"]["status"])
            self.assertEqual(0, output["result"]["metrics"]["trade_count"])
            self.assertEqual(captured_identity["fingerprint"],
                             output["owner_identities"]["tradability_evidence_fingerprint"])
            self.assertEqual("late-revision", tradability.evidence_at(
                code, second, f"{second}T09:30:00+08:00").source)
        finally:
            market_conn.close()
            universe_conn.close()
            tradability_conn.close()

    def test_canonical_modules_have_no_legacy_runtime_or_network_imports(self):
        paths = ("experiment_pit_validation.py", "experiment_validation_runner.py",
                 "experiment_validation_repository.py", "experiment_execution_model.py")
        forbidden = {"backtest", "data_fetcher", "strategies", "requests", "marketdata_transport"}
        for name in paths:
            tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(item.name.split(".")[0] for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertFalse(imports & forbidden, (name, imports & forbidden))


if __name__ == "__main__":
    unittest.main()
