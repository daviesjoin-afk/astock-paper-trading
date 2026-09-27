"""Permanent R29-A tests for the canonical PIT validation gate."""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import unittest
from pathlib import Path

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import experiment_contract as EC  # noqa: E402
import experiment_pit_validation as PV  # noqa: E402
import strategy_registry as SR  # noqa: E402
import tradability_archive as TA  # noqa: E402
import walk_forward_validation as WFV  # noqa: E402


def _spec(**changes):
    values = {
        "strategy": EC.StrategyIdentity("trend_pullback", 2, "a" * 64),
        "code_revision": "b" * 40,
        "dataset_fingerprint": "c" * 64,
        "universe_fingerprint": "d" * 64,
        "tradability_fingerprint": "e" * 64,
        "market_data_fingerprint": "f" * 64,
        "parameter_set": {"lookback": 20},
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
        "asof_policy": {"policy_id": "pit-close-v1", "cutoff": "2026-03-31T15:00:00+08:00"},
        "execution_assumptions": {
            "execution_profile_version": "execution-profile-v3",
            "fill_assumptions": {"fill_price": "next_open"},
            "t_plus_one_semantics": "sell_after_next_session",
            "price_limit_semantics": "exchange-v1",
            "partial_fill_semantics": "preserve_remainder",
            "capacity_assumptions": {"participation_rate": 0.05},
        },
        "cost_model": {
            "commission_rate": 0.0001,
            "minimum_commission": 5,
            "stamp_duty_rate": 0.0005,
            "slippage_model": "fixed-rate-v1",
            "slippage_parameters": {"rate": 0.001},
            "version": "cost-v1",
        },
        "random_seed": 17,
    }
    values.update(changes)
    return EC.ExperimentSpec(**values)


def _strategy(spec=None, **changes):
    spec = spec or _spec()
    return SR.StrategyVersion(
        strategy_id=changes.get("strategy_id", spec.strategy.strategy_id),
        version=changes.get("version", spec.strategy.version),
        checksum=changes.get("checksum", spec.strategy.checksum),
        definition={"implementation_key": "trend_pullback"},
        created_at="2026-01-01T00:00:00+00:00",
        created_by="test",
    )


def _sessions():
    return [f"2026-01-0{day}" for day in range(1, 7)]


def _samples():
    return [
        WFV.ValidationSample(
            sample_key=f"sample-{day}", code="600000",
            decision_session=f"2026-01-0{day}",
            label_available_at=f"2026-01-0{day}T15:00:00+08:00",
            target=0.01 * day, pit_status=WFV.PIT_VERIFIED,
        )
        for day in range(1, 7)
    ]


def _universe(source_kind="historical_archive"):
    rows = [{"code": "600000", "list_date": "2020-01-01"}]
    source = {
        "kind": source_kind,
        "historical_membership_complete": True,
        "historical_membership_asof": "2026-03-31",
        "source": "fixture",
    }
    return rows, source


def _tradability_repo(*, unknown_st=False):
    conn = sqlite3.connect(":memory:")
    repo = TA.TradabilityArchiveRepository(conn)
    repo.ensure_schema()
    for day in _sessions():
        repo.save(TA.TradabilityEvidence(
            code="600000", session_date=day, is_listed=True,
            listing_date="2020-01-01", delisting_date=None,
            is_st=None if unknown_st else False, is_suspended=False,
            suspension_reason=None, has_market_quote=True, has_trade_volume=True,
            is_price_limit_locked=False, price_limit_direction=None,
            source="fixture", observed_at=f"{day}T15:00:00+08:00",
            effective_at=f"{day}T09:30:00+08:00",
        ))
    return conn, repo


def _evaluate(**changes):
    spec = changes.pop("spec", _spec())
    values = {
        "strategy_version": _strategy(spec),
        "dataset_manifest": {"dataset_fingerprint": spec.dataset_fingerprint},
        "universe_rows": _universe()[0],
        "universe_source": _universe()[1],
        "tradability_repository": None,
        "fundamental_records": [{
            "report_period": "2025-12-31", "published_at": "2026-03-20",
            "net_profit": 10,
        }],
        "samples": _samples(),
        "walk_forward_config": WFV.WalkForwardConfig(
            min_train_sessions=2, validation_sessions=1, test_sessions=1,
        ),
        "authoritative_sessions": _sessions(),
    }
    values.update(changes)
    return PV.build_pit_validation_evidence(spec, **values)


class PITValidationTests(unittest.TestCase):
    def test_R29_01_valid_explicit_pinned_strategy_identity_is_proven(self):
        evidence = _evaluate()
        self.assertEqual("proven", evidence.dimensions["strategy_version"]["status"])

    def test_R29_02_current_strategy_head_cannot_substitute_pinned_identity(self):
        evidence = _evaluate(strategy_version=None)
        self.assertEqual("blocked", evidence.dimensions["strategy_version"]["status"])
        self.assertIn("strategy_identity_mismatch", evidence.reason_codes)

    def test_R29_03_universe_sha_alone_does_not_prove_history(self):
        evidence = _evaluate(universe_rows=None, universe_source=None)
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])

    def test_R29_04_current_universe_source_cannot_pass_historical_completeness(self):
        rows, source = _universe("current_snapshot")
        evidence = _evaluate(universe_rows=rows, universe_source=source)
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])

    def test_R29_05_complete_historical_archive_can_pass_universe_gate(self):
        rows, source = _universe()
        evidence = _evaluate(universe_rows=rows, universe_source=source)
        self.assertEqual("proven", evidence.dimensions["historical_universe"]["status"])

    def test_R29_06_missing_tradability_evidence_blocks(self):
        evidence = _evaluate()
        self.assertEqual("blocked", evidence.dimensions["historical_tradability"]["status"])
        self.assertGreater(evidence.data_coverage["tradability"]["unknown"], 0)

    def test_R29_07_unknown_st_is_not_non_st(self):
        conn, repo = _tradability_repo(unknown_st=True)
        try:
            evidence = _evaluate(tradability_repository=repo)
            self.assertEqual("blocked", evidence.dimensions["historical_tradability"]["status"])
            self.assertGreater(evidence.data_coverage["tradability"]["unknown"], 0)
        finally:
            conn.close()

    def test_R29_07b_complete_requested_tradability_facts_are_reported(self):
        conn, repo = _tradability_repo()
        try:
            evidence = _evaluate(tradability_repository=repo)
            self.assertEqual("proven", evidence.dimensions["historical_tradability"]["status"])
            self.assertEqual(6, evidence.data_coverage["tradability"]["available"])
            self.assertEqual(1.0, evidence.data_coverage["tradability"]["ratio"])
        finally:
            conn.close()

    def test_R29_08_current_market_snapshot_cannot_prove_historical_pit(self):
        evidence = _evaluate(market_snapshot={"as_of": "2026-03-31", "complete": True})
        self.assertEqual("blocked", evidence.dimensions["market_data_pit"]["status"])
        self.assertEqual("historical_market_data_unavailable",
                         evidence.dimensions["market_data_pit"]["reason_code"])

    def test_R29_09_historical_validation_does_not_refresh_or_network(self):
        source = Path(BACKEND, "experiment_pit_validation.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("refresh_snapshot", calls)
        self.assertNotIn("fetch", calls)
        self.assertIn("build_pit_validation_evidence", names)

    def test_R29_10_report_period_cannot_substitute_publication_time(self):
        evidence = _evaluate(fundamental_records=[{"report_period": "2025-12-31", "net_profit": 3}])
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])

    def test_R29_11_future_publication_is_invisible(self):
        evidence = _evaluate(fundamental_records=[{
            "report_period": "2025-12-31", "published_at": "2026-04-01", "net_profit": 3,
        }])
        self.assertEqual(1, evidence.data_coverage["fundamental"]["future"])
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])

    def test_R29_12_missing_publication_is_blocked_in_strict_mode(self):
        evidence = _evaluate(fundamental_records=[{"report_period": "2025-12-31", "eps": 0.1}])
        self.assertEqual(1, evidence.data_coverage["fundamental"]["publication_unproven"])

    def test_R29_12b_invalid_publication_metadata_has_its_own_bucket(self):
        evidence = _evaluate(fundamental_records=[{
            "report_period": "2025-12-31", "published_at": "not-a-date", "eps": 0.1,
        }])
        self.assertEqual(1, evidence.data_coverage["fundamental"]["invalid"])

    def test_R29_13_dataset_fingerprint_mismatch_blocks(self):
        evidence = _evaluate(dataset_manifest={"dataset_fingerprint": "1" * 64})
        self.assertEqual("dataset_identity_mismatch", evidence.dimensions["dataset"]["reason_code"])

    def test_R29_14_latest_dataset_fallback_is_forbidden(self):
        evidence = _evaluate(dataset_manifest=None)
        self.assertEqual("blocked", evidence.dimensions["dataset"]["status"])

    def test_R29_15_execution_assumptions_come_only_from_spec(self):
        spec = _spec(execution_assumptions={
            "execution_profile_version": "pinned-v99", "fill_assumptions": {"mode": "explicit"},
            "t_plus_one_semantics": "pinned", "price_limit_semantics": "pinned",
            "partial_fill_semantics": "pinned", "capacity_assumptions": {"ratio": 0.01},
        })
        evidence = _evaluate(spec=spec)
        self.assertEqual("pinned-v99", evidence.dimensions["execution_model"]["declared_identity"]["execution_profile_version"])

    def test_R29_16_cost_model_comes_only_from_spec(self):
        spec = _spec(cost_model={
            "commission_rate": 0.002, "minimum_commission": 8,
            "stamp_duty_rate": 0.001, "slippage_model": "fixed-rate-v1",
            "slippage_parameters": {"rate": 0.003}, "version": "pinned-cost-v2",
        })
        evidence = _evaluate(spec=spec)
        self.assertEqual("pinned-cost-v2", evidence.dimensions["cost_model"]["declared_identity"]["version"])

    def test_R29_17_walk_forward_requires_explicit_sessions(self):
        evidence = _evaluate(authoritative_sessions=None)
        self.assertEqual("walk_forward_explicit_sessions_required",
                         evidence.dimensions["walk_forward"]["reason_code"])
        evidence = _evaluate(authoritative_sessions=_sessions())
        self.assertEqual("explicit_sessions", evidence.walk_forward["timeline_source"])

    def test_R29_18_walk_forward_test_label_maturity_uses_asof(self):
        samples = _samples()
        samples[-1] = WFV.ValidationSample(
            sample_key="future-test", code="600000", decision_session="2026-01-06",
            label_available_at="2026-04-01T15:00:00+08:00", target=0.1,
            pit_status=WFV.PIT_VERIFIED,
        )
        evidence = _evaluate(samples=samples)
        self.assertEqual("proven", evidence.dimensions["walk_forward"]["status"])
        self.assertIn("walk_forward_window_not_matured", evidence.pit_warnings)
        self.assertTrue(any(
            window["status"] == "not_ready" and window["reason"] == "window_not_matured"
            for window in evidence.walk_forward["windows"]
        ))

    def test_R29_19_purge_semantics_remain_owned_by_walk_forward_validator(self):
        result = WFV.build_walk_forward_folds(
            _samples(), WFV.WalkForwardConfig(2, 1, 1),
            sessions=_sessions(), asof="2026-03-31T15:00:00+08:00",
        )
        self.assertEqual("explicit_sessions", result["report"]["timeline_source"])
        self.assertIn("label_not_available_before_fold", result["report"]["exclusion_reasons"])

    def test_R29_20_blocked_required_dimension_blocks_whole_validation(self):
        evidence = _evaluate()
        self.assertEqual("blocked", evidence.status)
        self.assertIn("historical_market_data_unavailable", evidence.reason_codes)

    def test_R29_21_blocked_evidence_has_no_zero_performance_metrics(self):
        projection = _evaluate().projection()
        self.assertNotIn("metrics", projection)
        self.assertNotIn("return", projection)

    def test_R29_22_identity_is_separate_from_provenance(self):
        evidence = _evaluate()
        market = evidence.dimensions["market_data_pit"]
        self.assertEqual("f" * 64, market["declared_identity"]["market_data_fingerprint"])
        self.assertEqual("declared_identity_only", market["provenance_status"])
        self.assertEqual("blocked", market["status"])

    def test_R29_23_unknown_coverage_remains_none(self):
        evidence = _evaluate(authoritative_sessions=[])
        self.assertIsNone(evidence.data_coverage["tradability"]["ratio"])
        self.assertIsNone(evidence.data_coverage["market_data"])

    def test_R29_26_universe_and_tradability_follow_each_historical_session(self):
        rows = [
            {"code": "600000", "list_date": "2020-01-01", "delist_date": "2026-01-04"},
            {"code": "000001", "list_date": "2020-01-01"},
        ]
        conn = sqlite3.connect(":memory:")
        repo = TA.TradabilityArchiveRepository(conn)
        repo.ensure_schema()
        for day in _sessions():
            for code in ("600000", "000001"):
                repo.save(TA.TradabilityEvidence(
                    code=code, session_date=day, is_listed=True,
                    listing_date="2020-01-01", delisting_date="2026-01-04" if code == "600000" else None,
                    is_st=False, is_suspended=False, suspension_reason=None,
                    has_market_quote=True, has_trade_volume=True,
                    is_price_limit_locked=False, price_limit_direction=None,
                    source="fixture", observed_at=f"{day}T15:00:00+08:00",
                    effective_at=f"{day}T09:30:00+08:00",
                ))
        try:
            evidence = _evaluate(
                universe_rows=rows, tradability_repository=repo,
                authoritative_sessions=_sessions(),
            )
            self.assertEqual("proven", evidence.dimensions["historical_universe"]["status"])
            self.assertEqual(9, evidence.data_coverage["tradability"]["requested"])
            self.assertEqual(9, evidence.data_coverage["tradability"]["available"])
        finally:
            conn.close()

    def test_R29_27_complete_suspension_facts_are_blocked_not_unknown(self):
        conn = sqlite3.connect(":memory:")
        repo = TA.TradabilityArchiveRepository(conn)
        repo.ensure_schema()
        for day in _sessions():
            repo.save(TA.TradabilityEvidence(
                code="600000", session_date=day, is_listed=True,
                listing_date="2020-01-01", delisting_date=None,
                is_st=False, is_suspended=True, suspension_reason="fixture",
                has_market_quote=False, has_trade_volume=False,
                is_price_limit_locked=False, price_limit_direction=None,
                source="fixture", observed_at=f"{day}T15:00:00+08:00",
                effective_at=f"{day}T09:30:00+08:00",
            ))
        try:
            evidence = _evaluate(tradability_repository=repo)
            coverage = evidence.data_coverage["tradability"]
            self.assertEqual("proven", evidence.dimensions["historical_tradability"]["status"])
            self.assertEqual(6, coverage["blocked"])
            self.assertEqual(0, coverage["available"])
            self.assertEqual(0, coverage["unknown"])
            self.assertEqual(1.0, coverage["ratio"])
            self.assertLessEqual(coverage["ratio"], 1.0)
        finally:
            conn.close()

    def test_R29_28_walk_forward_sessions_and_samples_stay_inside_spec_range(self):
        spec = _spec(start_date="2026-01-02", end_date="2026-01-05")
        evidence = _evaluate(
            spec=spec, authoritative_sessions=_sessions(), samples=_samples(),
        )
        self.assertEqual(2, evidence.walk_forward["sessions"]["excluded_outside_experiment_range"])
        self.assertEqual(2, evidence.walk_forward["label_coverage"]["samples_excluded_outside_experiment_range"])
        for window in evidence.walk_forward["windows"]:
            for period_name in ("train_period", "validation_period", "oos_period"):
                period = window[period_name]
                if period["start"] is not None:
                    self.assertGreaterEqual(period["start"], spec.start_date)
                if period["end"] is not None:
                    self.assertLessEqual(period["end"], spec.end_date)

    def test_R29_29_invalid_session_or_sample_date_blocks(self):
        calendar_evidence = _evaluate(
            authoritative_sessions=[*_sessions(), "2026-01-07 trailing-garbage"],
        )
        self.assertEqual("blocked", calendar_evidence.dimensions["walk_forward"]["status"])
        self.assertEqual(1, calendar_evidence.walk_forward["sessions"]["invalid"])

        invalid_sample = WFV.ValidationSample(
            sample_key="invalid-date", code="600000", decision_session="bad-date",
            label_available_at="2026-01-07T15:00:00+08:00", target=0.1,
            pit_status=WFV.PIT_VERIFIED,
        )
        sample_evidence = _evaluate(samples=[*_samples(), invalid_sample])
        self.assertEqual("blocked", sample_evidence.dimensions["walk_forward"]["status"])
        self.assertIn("walk_forward_sample_session_invalid", sample_evidence.reason_codes)
        self.assertEqual(1, sample_evidence.walk_forward["label_coverage"]["samples_with_invalid_session"])

    def test_R29_24_no_clock_or_latest_fallback(self):
        source = Path(BACKEND, "experiment_pit_validation.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        clock_calls = {node.func.attr for node in ast.walk(tree)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr in {"now", "today", "utcnow"}}
        self.assertFalse(clock_calls)
        self.assertNotIn("latest", source.lower())

    def test_R29_25_validation_contract_has_no_promotion_authority(self):
        source = Path(BACKEND, "experiment_pit_validation.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertEqual(["PITValidationEvidence"], [node.name for node in classes])
        self.assertNotIn("approved", source.lower())
        self.assertNotIn("promotable", source.lower())

    def test_G_R29_01_to_11_architecture_boundaries(self):
        source = Path(BACKEND, "experiment_pit_validation.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        forbidden_calls = (
            ("G-R29-03 refresh", "refresh_snapshot"),
            ("G-R29-04 current strategy head", "get_version"),
        )
        for label, call in forbidden_calls:
            with self.subTest(label=label):
                self.assertNotIn(call, calls)
        forbidden_imports = (
            ("G-R29-01 backtest", "backtest"),
            ("G-R29-02 data_fetcher", "data_fetcher"),
            ("G-R29-05 current runtime settings", "execution_profiles"),
            ("G-R29-05 paper constants", "paper_trading_rules"),
        )
        for label, imported in forbidden_imports:
            with self.subTest(label=label):
                self.assertNotIn(imported, imports)
        with self.subTest(label="G-R29-06 wall clock"):
            self.assertFalse(
                any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"now", "today", "utcnow"} for node in ast.walk(tree))
            )
        reused = (
            ("G-R29-07 universe", "historical_universe"),
            ("G-R29-08 tradability", "tradability_at"),
            ("G-R29-09 financial", "financial_visibility"),
            ("G-R29-10 walk-forward", "build_walk_forward_folds"),
        )
        for label, call in reused:
            with self.subTest(label=label):
                self.assertIn(call, calls)
        with self.subTest(label="G-R29-11 no promotion state"):
            self.assertFalse({"approved", "promotable", "champion"} & {
                node.value.lower() for node in ast.walk(tree) if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            })


if __name__ == "__main__":
    unittest.main()
