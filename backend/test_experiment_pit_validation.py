"""Permanent R29-A tests for the canonical PIT validation gate."""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import experiment_contract as EC  # noqa: E402
import experiment_pit_validation as PV  # noqa: E402
import learning_dataset as LD  # noqa: E402
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


def _calendar_provenance(sessions=None, *, spec=None, complete=True, session_count=None):
    sessions = list(_sessions() if sessions is None else sessions)
    spec = spec or _spec()
    in_range = [day for day in sessions if spec.start_date <= day <= spec.end_date]
    return {
        "kind": "historical_session_calendar",
        "source": "fixture-exchange-calendar-v1",
        "coverage_start": spec.start_date,
        "coverage_end": spec.end_date,
        "range_complete": complete,
        "session_count": len(in_range) if session_count is None else session_count,
    }


def _samples():
    return [
        WFV.ValidationSample(
            sample_key=f"sample-{day}", code="600000",
            decision_session=f"2026-01-0{day}",
            decision_at=f"2026-01-0{day}T09:30:00+08:00",
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
            "record": {
                "report_period": "2025-12-31", "published_at": "2026-03-20",
                "net_profit": 10,
            },
            "sample_keys": [sample.sample_key for sample in _samples()],
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
        evidence = _evaluate(
            universe_rows=None, universe_source=None,
            session_calendar_provenance=_calendar_provenance(),
        )
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])
        self.assertNotEqual("session_calendar_unproven",
                            evidence.dimensions["historical_universe"]["provenance_status"])

    def test_R29_04_current_universe_source_cannot_pass_historical_completeness(self):
        rows, source = _universe("current_snapshot")
        evidence = _evaluate(
            universe_rows=rows, universe_source=source,
            session_calendar_provenance=_calendar_provenance(),
        )
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])
        self.assertEqual(0, evidence.dimensions["historical_universe"]["coverage"]["sessions_proven"])

    def test_R29_04b_caller_universe_claim_is_not_an_archive_owner(self):
        rows, source = _universe()
        detail, _members, _report = PV._universe(
            _spec(), rows, source, _sessions(), session_calendar_complete=True,
        )
        self.assertEqual("blocked", detail["status"])

    def test_R29_05_caller_calendar_claim_cannot_prove_historical_universe(self):
        rows, source = _universe()
        evidence = _evaluate(
            universe_rows=rows, universe_source=source,
            session_calendar_provenance=_calendar_provenance(),
        )
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])

    def test_R29_06_missing_tradability_evidence_blocks(self):
        evidence = _evaluate(session_calendar_provenance=_calendar_provenance())
        self.assertEqual("blocked", evidence.dimensions["historical_tradability"]["status"])
        self.assertGreater(evidence.data_coverage["tradability"]["unknown"], 0)
        isolated, _coverage = PV._tradability(
            _spec(), {day: [{"code": "600000"}] for day in _sessions()},
            _sessions(), None, universe_complete=True,
        )
        self.assertEqual("blocked", isolated["status"])

    def test_R29_07_unknown_st_is_not_non_st(self):
        conn, repo = _tradability_repo(unknown_st=True)
        try:
            evidence = _evaluate(
                tradability_repository=repo,
                session_calendar_provenance=_calendar_provenance(),
            )
            self.assertEqual("blocked", evidence.dimensions["historical_tradability"]["status"])
            self.assertGreater(evidence.data_coverage["tradability"]["unknown"], 0)
        finally:
            conn.close()

    def test_R29_07b_complete_requested_tradability_facts_still_require_calendar_owner(self):
        conn, repo = _tradability_repo()
        try:
            evidence = _evaluate(
                tradability_repository=repo,
                session_calendar_provenance=_calendar_provenance(),
            )
            self.assertEqual("blocked", evidence.dimensions["historical_tradability"]["status"])
            self.assertIsNone(evidence.data_coverage["tradability"]["ratio"])
        finally:
            conn.close()

    def test_R29_tradability_gate_uses_close_and_execution_facts_from_one_capture(self):
        conn = sqlite3.connect(":memory:")
        repo = TA.TradabilityArchiveRepository(conn)
        repo.ensure_schema()
        sessions = _sessions()
        members = {day: [{"code": "600000"}] for day in sessions}
        common = dict(code="600000", is_listed=True, listing_date="2020-01-01",
            delisting_date=None, is_st=False, is_suspended=False, suspension_reason=None,
            has_market_quote=True, has_trade_volume=True, is_price_limit_locked=False,
            price_limit_direction=None)
        try:
            for day in sessions:
                repo.save(TA.TradabilityEvidence(
                    **common, session_date=day, source="open-A",
                    observed_at=f"{day}T09:00:00+08:00",
                    effective_at=f"{day}T09:00:00+08:00"))
            first = sessions[0]
            repo.save(TA.TradabilityEvidence(
                **common, session_date=first, source="close-B",
                observed_at=f"{first}T14:00:00+08:00",
                effective_at=f"{first}T14:00:00+08:00"))
            captured = PV.tradability_replay_projection(members, sessions, repo)
            spec = replace(_spec(), tradability_fingerprint=captured["fingerprint"])
            original_capture = repo.evidence_snapshot_many

            def capture_then_append(requests):
                snapshot = original_capture(requests)
                repo.save(TA.TradabilityEvidence(
                    **common, session_date=first, source="post-capture-C",
                    observed_at=f"{first}T09:20:00+08:00",
                    effective_at=f"{first}T09:15:00+08:00"))
                return snapshot

            with mock.patch.object(repo, "evidence_snapshot_many", side_effect=capture_then_append):
                detail, coverage = PV._tradability(
                    spec, members, sessions, repo, universe_complete=True)

            self.assertTrue(coverage["identity_matches"])
            self.assertEqual(0, coverage["execution_unknown"])
            self.assertEqual("post-capture-C", repo.evidence_at(
                "600000", first, f"{first}T09:30:00+08:00").source)
            self.assertEqual("proven", detail["status"])
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
        evidence = _evaluate(fundamental_records=[{
            "record": {"report_period": "2025-12-31", "net_profit": 3},
            "sample_keys": ["sample-1"],
        }])
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])

    def test_R29_11_future_publication_is_invisible(self):
        evidence = _evaluate(fundamental_records=[{
            "record": {
                "report_period": "2025-12-31", "published_at": "2026-04-01", "net_profit": 3,
            },
            "sample_keys": ["sample-1"],
        }])
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])

    def test_R29_12_missing_publication_is_blocked_in_strict_mode(self):
        evidence = _evaluate(fundamental_records=[{
            "record": {"report_period": "2025-12-31", "eps": 0.1},
            "sample_keys": ["sample-1"],
        }])
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])

    def test_R29_12b_invalid_publication_metadata_has_its_own_bucket(self):
        evidence = _evaluate(fundamental_records=[{
            "record": {
                "report_period": "2025-12-31", "published_at": "not-a-date", "eps": 0.1,
            },
            "sample_keys": ["sample-1"],
        }])
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])

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
        evidence = _evaluate(
            samples=samples,
            session_calendar_provenance=_calendar_provenance(),
        )
        self.assertEqual("blocked", evidence.dimensions["walk_forward"]["status"])
        self.assertEqual("walk_forward_session_calendar_unproven",
                         evidence.dimensions["walk_forward"]["reason_code"])
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
        evidence = _evaluate(session_calendar_provenance=_calendar_provenance())
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
                session_calendar_provenance=_calendar_provenance(),
            )
            self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])
            self.assertEqual(9, evidence.data_coverage["tradability"]["requested"])
            self.assertEqual(9, evidence.data_coverage["tradability"]["available"])
            self.assertIsNone(evidence.data_coverage["tradability"]["ratio"])
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
            evidence = _evaluate(
                tradability_repository=repo,
                session_calendar_provenance=_calendar_provenance(),
            )
            coverage = evidence.data_coverage["tradability"]
            self.assertEqual("blocked", evidence.dimensions["historical_tradability"]["status"])
            self.assertEqual(6, coverage["blocked"])
            self.assertEqual(0, coverage["available"])
            self.assertEqual(0, coverage["unknown"])
            self.assertIsNone(coverage["ratio"])
        finally:
            conn.close()

    def test_R29_28_walk_forward_sessions_and_samples_stay_inside_spec_range(self):
        spec = _spec(start_date="2026-01-02", end_date="2026-01-05")
        evidence = _evaluate(
            spec=spec, authoritative_sessions=_sessions(), samples=_samples(),
            session_calendar_provenance=_calendar_provenance(_sessions(), spec=spec),
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
        sample_evidence = _evaluate(
            samples=[*_samples(), invalid_sample],
            session_calendar_provenance=_calendar_provenance(),
        )
        self.assertEqual("blocked", sample_evidence.dimensions["walk_forward"]["status"])
        self.assertEqual(1, sample_evidence.walk_forward["label_coverage"]["samples_with_invalid_session"])

    def test_R29_30_short_calendar_without_full_range_provenance_blocks(self):
        evidence = _evaluate(authoritative_sessions=_sessions())
        self.assertEqual("blocked", evidence.dimensions["walk_forward"]["status"])
        self.assertEqual("walk_forward_session_calendar_unproven",
                         evidence.dimensions["walk_forward"]["reason_code"])
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])
        self.assertIsNone(evidence.data_coverage["session_calendar"]["ratio"])

        incomplete = _calendar_provenance(
            _sessions(), complete=False, session_count=len(_sessions()),
        )
        evidence = _evaluate(
            authoritative_sessions=_sessions(), session_calendar_provenance=incomplete,
        )
        self.assertEqual("blocked", evidence.dimensions["walk_forward"]["status"])

    def test_R29_31_financial_observations_use_linked_decision_session(self):
        samples = _samples()
        early_filed = [{
            "record": {
                "report_period": "2025-12-31", "published_at": "2025-12-31",
                "net_profit": 10,
            },
            "sample_keys": [sample.sample_key for sample in samples],
        }]
        evidence = _evaluate(fundamental_records=early_filed)
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])

        late_filed = [{
            "record": {
                "report_period": "2025-12-31", "published_at": "2026-03-20",
                "net_profit": 10,
            },
            "sample_keys": [sample.sample_key for sample in samples],
        }]
        evidence = _evaluate(fundamental_records=late_filed)
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])

        unlinked = _evaluate(fundamental_records=[{
            "record": late_filed[0]["record"], "sample_keys": ["missing-sample"],
        }])
        self.assertEqual("blocked", unlinked.dimensions["fundamental_pit"]["status"])
        self.assertEqual("financial_feature_evidence_missing",
                         unlinked.dimensions["fundamental_pit"]["reason_code"])

    def test_R29_32_caller_calendar_claim_never_creates_authoritative_proof(self):
        forged_claim = _calendar_provenance(
            _sessions(), complete=True, session_count=6,
        )
        evidence = _evaluate(
            authoritative_sessions=_sessions(),
            session_calendar_provenance=forged_claim,
        )
        coverage = evidence.data_coverage["session_calendar"]
        self.assertTrue(coverage["caller_claim_supplied"])
        self.assertEqual("blocked", coverage["status"])
        self.assertEqual("historical_session_calendar_owner_unavailable", coverage["authority"])
        self.assertEqual("blocked", evidence.dimensions["historical_universe"]["status"])
        self.assertEqual("blocked", evidence.dimensions["walk_forward"]["status"])
        self.assertIsNone(coverage["ratio"])

    def test_R29_33_same_day_future_financial_publication_is_not_visible(self):
        sample = WFV.ValidationSample(
            sample_key="morning-decision", code="600000",
            decision_session="2026-01-05", decision_at="2026-01-05T09:30:00+08:00",
            label_available_at="2026-01-05T15:00:00+08:00", target=0.01,
            pit_status=WFV.PIT_VERIFIED,
        )
        evidence = _evaluate(
            samples=[sample],
            fundamental_records=[{
                "record": {
                    "report_period": "2025-12-31",
                    "published_at": "2026-01-05T14:00:00+08:00", "net_profit": 10,
                },
                "sample_keys": [sample.sample_key],
            }],
        )
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])

        date_only = _evaluate(
            samples=[sample],
            fundamental_records=[{
                "record": {
                    "report_period": "2025-12-31", "published_at": "2026-01-05",
                    "net_profit": 10,
                },
                "sample_keys": [sample.sample_key],
            }],
        )
        self.assertEqual("financial_feature_evidence_missing",
                         date_only.dimensions["fundamental_pit"]["reason_code"])
        self.assertEqual("blocked", date_only.dimensions["fundamental_pit"]["status"])

        canonical = LD.CanonicalSample(
            sample_key="canonical-morning", source="fixture", source_version="v1",
            code="600000", strategy_id="trend_pullback", model_family="test",
            feature_asof="2026-01-05",
            feature_available_at="2026-01-05T09:30:00+08:00",
            label_start_date="2026-01-05", label_end_date="2026-01-06",
            label_available_at="2026-01-06T15:00:00+08:00", horizon=1,
            horizon_semantics="close_to_close_v1", features={}, target=0.01,
            pit_status=WFV.PIT_VERIFIED,
        )
        canonical_evidence = _evaluate(
            samples=[canonical],
            fundamental_records=[{
                "record": {
                    "report_period": "2025-12-31",
                    "published_at": "2026-01-05T14:00:00+08:00", "net_profit": 10,
                },
                "sample_keys": [canonical.sample_key],
            }],
        )
        self.assertEqual("financial_feature_evidence_missing",
                         canonical_evidence.dimensions["fundamental_pit"]["reason_code"])

    def test_R29_34_fundamental_pit_requires_exact_decision_instant(self):
        for decision_at in (None, "2026-01-05"):
            with self.subTest(decision_at=decision_at):
                sample = WFV.ValidationSample(
                    sample_key="no-exact-instant", code="600000",
                    decision_session="2026-01-05", decision_at=decision_at,
                    label_available_at="2026-01-05T15:00:00+08:00", target=0.01,
                    pit_status=WFV.PIT_VERIFIED,
                )
                evidence = _evaluate(
                    samples=[sample],
                    fundamental_records=[{
                        "record": {
                            "report_period": "2025-12-31",
                            "published_at": "2025-12-31", "net_profit": 10,
                        },
                        "sample_keys": [sample.sample_key],
                    }],
                )
                self.assertEqual("financial_feature_evidence_missing",
                                 evidence.dimensions["fundamental_pit"]["reason_code"])
                self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])

    def test_R29_35_decision_instant_is_normalized_to_utc(self):
        self.assertEqual("2026-01-01T07:30:00+00:00",
                         PV._decision_instant("2026-01-01T15:30:00+08:00"))

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
            ("G-R29-10 walk-forward", "build_walk_forward_folds"),
        )
        for label, call in reused:
            with self.subTest(label=label):
                self.assertIn(call, calls)
        with self.subTest(label="G-R29-09 financial owner visibility"):
            import financial_feature_evidence as financial_owner
            owner_calls = {node.func.attr for node in ast.walk(ast.parse(
                __import__("inspect").getsource(financial_owner)))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            self.assertIn("financial_visibility", owner_calls)
        with self.subTest(label="G-R29-08 tradability owner evaluator"):
            self.assertIn("TA.TradabilityArchiveRepository", source)
            self.assertIn("coverage_projection", source)
        with self.subTest(label="G-R29-11 no promotion state"):
            self.assertFalse({"approved", "promotable", "champion"} & {
                node.value.lower() for node in ast.walk(tree) if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            })


if __name__ == "__main__":
    unittest.main()
