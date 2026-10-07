"""Offline R30 scenario runner reusing canonical R29 owners and execution."""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from dataclasses import replace
from collections.abc import Mapping, Sequence
from typing import Any

try:
    import candidate_experiment as CE
    import experiment_contract as EC
    import experiment_execution_model as EM
    import experiment_pit_validation as PV
    import experiment_validation_runner as R29
    import historical_market_archive as HMA
    import historical_session_calendar as HSC
    import historical_universe_archive as HUA
    import robustness_contract as RC
    import robustness_regimes as RR
    import strategy_dsl_schema as DSL
    import strategy_parameter_schema as SPS
    import strategy_registry as SR
    import tradability_archive as TA
except ImportError:  # pragma: no cover
    from . import candidate_experiment as CE
    from . import experiment_contract as EC
    from . import experiment_execution_model as EM
    from . import experiment_pit_validation as PV
    from . import experiment_validation_runner as R29
    from . import historical_market_archive as HMA
    from . import historical_session_calendar as HSC
    from . import historical_universe_archive as HUA
    from . import robustness_contract as RC
    from . import robustness_regimes as RR
    from . import strategy_dsl_schema as DSL
    from . import strategy_parameter_schema as SPS
    from . import strategy_registry as SR
    from . import tradability_archive as TA

RUNNER_VERSION = RC.REPORT_VERSION
CANDIDATE_RUNNER_VERSION = "r30-candidate-robustness-runner-v1"
R29_RUNNER_VERSION = R29.RUNNER_VERSION
R29_CANDIDATE_RUNNER_VERSION = R29.CANDIDATE_RUNNER_VERSION
TRANSFORM_VERSION = "r30-deterministic-derived-view-v1"
_METRIC_KEYS = ("return", "drawdown", "volatility", "turnover", "trade_count",
                "cost", "exposure", "capacity_proxy", "data_coverage")


class RobustnessBaselineError(ValueError):
    """A requested baseline is not a canonical completed R29 run."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _verify_common_baseline(run: Mapping[str, Any], spec, plan: RC.RobustnessPlan):
    """Subject-independent baseline proof shared by formal and candidate paths."""
    if not isinstance(run, Mapping) or not run:
        raise RobustnessBaselineError("canonical_baseline_run_not_found")
    if run.get("run_key") != plan.baseline_run_key:
        raise RobustnessBaselineError("baseline_run_identity_mismatch")
    if run.get("validation_status") != "ready":
        raise RobustnessBaselineError("baseline_validation_not_ready")
    if run.get("runner_version") not in (R29_RUNNER_VERSION, R29_CANDIDATE_RUNNER_VERSION):
        raise RobustnessBaselineError("baseline_runner_version_unknown")
    if run.get("runner_code_revision") != spec.code_revision:
        raise RobustnessBaselineError("baseline_code_revision_mismatch")
    if run.get("experiment_fingerprint") != spec.fingerprint:
        raise RobustnessBaselineError("baseline_experiment_fingerprint_mismatch")
    if plan.baseline_experiment_fingerprint != spec.fingerprint:
        raise RobustnessBaselineError("plan_baseline_experiment_mismatch")
    result = run.get("result")
    if not isinstance(result, Mapping) or result.get("status") != "completed":
        raise RobustnessBaselineError("baseline_result_not_completed")
    if result.get("experiment_fingerprint") != spec.fingerprint:
        raise RobustnessBaselineError("baseline_result_experiment_mismatch")
    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        raise RobustnessBaselineError("baseline_result_metrics_missing")
    try:
        canonical_result = EC.ExperimentResult(
            experiment_fingerprint=spec.fingerprint, status="completed",
            total_return=metrics.get("return"), max_drawdown=metrics.get("drawdown"),
            volatility=metrics.get("volatility"), turnover=metrics.get("turnover"),
            trade_count=metrics.get("trade_count"), total_cost=metrics.get("cost"),
            exposure=metrics.get("exposure"), capacity_proxy=metrics.get("capacity_proxy"),
            data_coverage=metrics.get("data_coverage"),
            regime_breakdown=metrics.get("regime_breakdown"),
            result_fingerprint=result.get("result_fingerprint"),
        )
    except (TypeError, ValueError) as exc:
        raise RobustnessBaselineError("baseline_result_fingerprint_mismatch") from exc
    if result.get("result_fingerprint") != canonical_result.result_fingerprint:
        raise RobustnessBaselineError("baseline_result_fingerprint_mismatch")
    expected_owners = {
        "calendar_fingerprint": spec.parameter_set.get("validation_calendar_fingerprint"),
        "universe_archive_fingerprint": spec.universe_fingerprint,
        "financial_archive_fingerprint": spec.parameter_set.get("financial_archive_fingerprint"),
        "market_archive_fingerprint": spec.market_data_fingerprint,
        "dataset_fingerprint": spec.dataset_fingerprint,
    }
    for name, expected in expected_owners.items():
        if run.get(name) != expected:
            raise RobustnessBaselineError("baseline_owner_identity_mismatch")
    for name in ("calendar_fingerprint", "universe_archive_fingerprint",
                 "tradability_evidence_fingerprint", "market_archive_fingerprint",
                 "dataset_fingerprint"):
        if not run.get(name):
            raise RobustnessBaselineError("baseline_owner_identity_missing")
    if bool(run.get("financial_archive_fingerprint")) != bool(
            spec.parameter_set.get("financial_archive_fingerprint")):
        raise RobustnessBaselineError("baseline_owner_identity_mismatch")
    validation_evidence = run.get("validation_evidence")
    if (not isinstance(validation_evidence, Mapping)
            or validation_evidence.get("status") != "ready"
            or validation_evidence.get("experiment_fingerprint") != spec.fingerprint):
        raise RobustnessBaselineError("baseline_validation_evidence_missing")
    return canonical_result, validation_evidence


def _verify_formal_baseline(run: Mapping[str, Any], spec: EC.ExperimentSpec,
                            plan: RC.RobustnessPlan,
                            strategy: SR.StrategyVersion) -> dict[str, Any]:
    """Formal StrategyVersion path. Returned baseline_identity shape is frozen."""
    canonical_result, validation_evidence = _verify_common_baseline(run, spec, plan)
    if run.get("runner_version") != R29_RUNNER_VERSION:
        raise RobustnessBaselineError("baseline_runner_version_unknown")
    if (run.get("strategy_id") != strategy.strategy_id
            or run.get("strategy_version") != strategy.version
            or run.get("strategy_checksum") != strategy.checksum
            or spec.strategy.strategy_id != strategy.strategy_id
            or spec.strategy.version != strategy.version
            or spec.strategy.checksum != strategy.checksum):
        raise RobustnessBaselineError("baseline_strategy_identity_mismatch")
    owner_projection = {
        "calendar_fingerprint": run["calendar_fingerprint"],
        "universe_archive_fingerprint": run["universe_archive_fingerprint"],
        "tradability_evidence_fingerprint": run["tradability_evidence_fingerprint"],
        "market_archive_fingerprint": run["market_archive_fingerprint"],
        "financial_archive_fingerprint": run.get("financial_archive_fingerprint"),
        "dataset_fingerprint": run["dataset_fingerprint"],
        "strategy_version": spec.strategy.projection(),
        "validation_evidence_fingerprint": _sha(validation_evidence),
    }
    expected_run_key = R29.EVR.ExperimentValidationRepository.build_run_key(
        spec.fingerprint, owner_projection, R29_RUNNER_VERSION)
    if expected_run_key != run.get("run_key"):
        raise RobustnessBaselineError("baseline_run_key_mismatch")
    return {
        "run_key": run["run_key"], "experiment_fingerprint": spec.fingerprint,
        "result_fingerprint": canonical_result.result_fingerprint,
        "runner_version": run["runner_version"],
        "code_revision": run["runner_code_revision"],
        "strategy_id": run["strategy_id"], "strategy_version": run["strategy_version"],
        "strategy_checksum": run["strategy_checksum"],
        **{name: run.get(name) for name in (
            "calendar_fingerprint", "universe_archive_fingerprint",
            "financial_archive_fingerprint", "tradability_evidence_fingerprint",
            "market_archive_fingerprint", "dataset_fingerprint")},
    }


def _verify_candidate_baseline(run: Mapping[str, Any], spec: EC.CandidateExperimentSpec,
                               plan: RC.RobustnessPlan,
                               candidate_replay: CE.CandidateReplayDefinition) -> dict[str, Any]:
    """Candidate StrategyCandidate path: the parent StrategyVersion must not masquerade."""
    canonical_result, validation_evidence = _verify_common_baseline(run, spec, plan)
    if run.get("runner_version") != R29_CANDIDATE_RUNNER_VERSION:
        raise RobustnessBaselineError("baseline_runner_version_unknown")
    if run.get("subject_kind") != "strategy_candidate":
        raise RobustnessBaselineError("baseline_subject_kind_mismatch")
    subject = candidate_replay.subject
    if run.get("candidate_id") != subject.candidate_id:
        raise RobustnessBaselineError("baseline_candidate_identity_mismatch")
    if run.get("subject") != subject.projection():
        raise RobustnessBaselineError("baseline_candidate_identity_mismatch")
    if any(run.get(name) is not None for name in
           ("strategy_id", "strategy_version", "strategy_checksum")):
        raise RobustnessBaselineError("baseline_candidate_masquerades_as_strategy_version")
    parent = subject.parent_strategy
    if (run.get("parent_strategy_id") != parent.strategy_id
            or run.get("parent_strategy_version") != parent.version
            or run.get("parent_strategy_checksum") != parent.checksum):
        raise RobustnessBaselineError("baseline_parent_identity_mismatch")
    if run.get("experiment_plan_fingerprint") != spec.parameter_set.get("experiment_plan_fingerprint"):
        raise RobustnessBaselineError("baseline_experiment_plan_mismatch")
    owner_projection = {
        "calendar_fingerprint": run["calendar_fingerprint"],
        "universe_archive_fingerprint": run["universe_archive_fingerprint"],
        "tradability_evidence_fingerprint": run["tradability_evidence_fingerprint"],
        "market_archive_fingerprint": run["market_archive_fingerprint"],
        "financial_archive_fingerprint": run.get("financial_archive_fingerprint"),
        "dataset_fingerprint": run["dataset_fingerprint"],
        "experiment_subject": run["subject"],
        "experiment_plan_fingerprint": run["experiment_plan_fingerprint"],
        "validation_evidence_fingerprint": _sha(validation_evidence),
    }
    expected_run_key = R29.EVR.ExperimentValidationRepository.build_run_key(
        spec.fingerprint, owner_projection, R29_CANDIDATE_RUNNER_VERSION)
    if expected_run_key != run.get("run_key"):
        raise RobustnessBaselineError("baseline_run_key_mismatch")
    return {
        "run_key": run["run_key"], "experiment_fingerprint": spec.fingerprint,
        "result_fingerprint": canonical_result.result_fingerprint,
        "runner_version": run["runner_version"],
        "code_revision": run["runner_code_revision"],
        "experiment_subject": run["subject"],
        "experiment_plan_fingerprint": run["experiment_plan_fingerprint"],
        **{name: run.get(name) for name in (
            "calendar_fingerprint", "universe_archive_fingerprint",
            "financial_archive_fingerprint", "tradability_evidence_fingerprint",
            "market_archive_fingerprint", "dataset_fingerprint")},
    }


def _verify_baseline(run: Mapping[str, Any], spec: EC.ExperimentSpec,
                     plan: RC.RobustnessPlan, strategy: SR.StrategyVersion) -> dict[str, Any]:
    """Backward-compatible formal entry point used by the existing R30 callers."""
    return _verify_formal_baseline(run, spec, plan, strategy)


def _deterministic_fraction(seed: int, *parts: str) -> float:
    material = "\0".join((str(seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / 2**64


def _bar_is_masked(bar: Mapping[str, Any], *, seed: int, fraction: float,
                   field_scope: str) -> bool:
    return _deterministic_fraction(seed, str(bar["code"]), str(bar["session"]),
                                    field_scope) < fraction


def _drop_universe_members(members_by_session: Mapping[str, Sequence[Mapping[str, Any]]],
                           *, seed: int, fraction: float):
    symbols = sorted({str(row["code"]) for rows in members_by_session.values()
                      for row in rows if isinstance(row, Mapping) and row.get("code")})
    dropped = {code for code in symbols if _deterministic_fraction(seed, code) < fraction}
    kept = {session: [row for row in rows if str(row.get("code")) not in dropped]
            for session, rows in members_by_session.items()}
    return kept, sorted(dropped)


def _metric_projection(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {"return": metrics.get("total_return"), "drawdown": metrics.get("max_drawdown"),
            "volatility": metrics.get("volatility"), "turnover": metrics.get("turnover"),
            "trade_count": metrics.get("trade_count"), "cost": metrics.get("total_cost"),
            "exposure": metrics.get("exposure"), "capacity_proxy": metrics.get("capacity_proxy"),
            "data_coverage": metrics.get("data_coverage")}


def _apply_parameter_stress(ast: Mapping[str, Any], parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Delegate to the single shared parameter-mutation authority."""
    return SPS.apply_parameter_stress(ast, parameters)


def _scenario_sessions(scenario: Mapping[str, Any], spec: EC.ExperimentSpec,
                       sessions: Sequence[str]) -> list[str]:
    start, end = _baseline_session_bounds(spec, sessions)
    parameters = scenario["parameters"]
    if scenario["category"] == "start_date":
        start += parameters["shift_sessions"]
    elif scenario["category"] == "end_date":
        end += parameters["shift_sessions"]
    if start < 0 or end >= len(sessions) or start > end:
        raise ValueError("date_perturbation_outside_archive_coverage")
    return list(sessions[start:end + 1])


def _baseline_session_bounds(spec: EC.ExperimentSpec,
                             sessions: Sequence[str]) -> tuple[int, int]:
    """Anchor a declared date interval to its first/last owner sessions."""
    if not sessions or list(sessions) != sorted(set(sessions)):
        raise ValueError("historical_session_calendar_invalid")
    start = bisect_left(sessions, spec.start_date)
    end = bisect_right(sessions, spec.end_date) - 1
    if start >= len(sessions) or end < 0 or start > end:
        raise ValueError("date_perturbation_baseline_sessions_unavailable")
    return start, end


def _revalidate_scenario_date_range(*, sessions: Sequence[str], spec,
                               baseline_identity: Mapping[str, Any],
                               market_archive_repository: HMA.HistoricalMarketArchiveRepository,
                               universe_archive_repository: HUA.HistoricalUniverseArchiveRepository,
                               tradability_repository: TA.TradabilityArchiveRepository,
                               strategy_version: SR.StrategyVersion | None = None,
                               candidate_replay: CE.CandidateReplayDefinition | None = None,
                               validation_context: Mapping[str, Any] | None,
                               replay_capture: Mapping[str, Any],
                               scenario_fingerprint: str | None = None) -> dict[str, Any]:
    """Re-run R29 PIT validation for a date-stressed range.

    Date stress may introduce future data, a different universe, tradability or WFV
    maturity, so it must never skip PIT revalidation. Candidate path calls R29 with
    the exact candidate subject (never the parent StrategyVersion).
    """
    candidate_path = candidate_replay is not None
    if not isinstance(validation_context, Mapping):
        raise ValueError("date_range_pit_context_missing")
    config = validation_context.get("walk_forward_config")
    manifest = validation_context.get("dataset_manifest")
    samples = validation_context.get("samples")
    if config is None or manifest is None or samples is None:
        raise ValueError("date_range_pit_context_missing")
    exact_calendar = HSC.issue_from_market_archive(
        market_archive_repository, archive_fingerprint=spec.market_data_fingerprint,
        benchmark_symbol=validation_context["benchmark_symbol"],
        start=sessions[0], end=sessions[-1])
    ranged_members = R29._universe_rows(
        universe_archive_repository, spec.universe_fingerprint, sessions)
    if candidate_path:
        try:
            ranged_members = CE.filter_candidate_members(
                candidate_replay, ranged_members, spec.universe_fingerprint)
        except CE.CandidateUniverseUnavailable as exc:
            raise ValueError(str(exc)) from exc
    ranged_capture = R29.PV.tradability_replay_projection(
        ranged_members, sessions, tradability_repository, captured=replay_capture)
    if not ranged_capture["capture_complete"]:
        raise ValueError("date_range_tradability_snapshot_unavailable")
    ranged_tradability_fingerprint = ranged_capture["fingerprint"]
    common = dict(
        start_date=sessions[0], end_date=sessions[-1],
        tradability_fingerprint=ranged_tradability_fingerprint,
        parameter_set={**spec.parameter_set,
                       "validation_calendar_fingerprint": exact_calendar.calendar_fingerprint})
    if candidate_path:
        derived_spec = replace(spec, robustness_scenario_fingerprint=scenario_fingerprint,
                               **{key: value for key, value in common.items()
                                  if key != "parameter_set"},
                               parameter_set={**common["parameter_set"],
                                              "robustness_scenario_fingerprint": scenario_fingerprint})
        proof = R29.run_validation(
            derived_spec, runner_code_revision=derived_spec.code_revision,
            strategy_candidate=candidate_replay.candidate, dataset_manifest=manifest,
            samples=samples, walk_forward_config=config, session_calendar=exact_calendar,
            market_archive_repository=market_archive_repository,
            market_archive_fingerprint=derived_spec.market_data_fingerprint,
            universe_archive_repository=universe_archive_repository,
            universe_archive_fingerprint=derived_spec.universe_fingerprint,
            tradability_repository=tradability_repository,
            tradability_replay_capture=ranged_capture,
            financial_feature_repository=validation_context.get("financial_feature_repository"),
            financial_archive_fingerprint=baseline_identity.get("financial_archive_fingerprint"),
        )
        expected_experiment_fingerprint = derived_spec.fingerprint
    else:
        ranged_spec = replace(spec, **common)
        proof = R29.run_validation(
            ranged_spec, runner_code_revision=ranged_spec.code_revision,
            strategy_version=strategy_version, dataset_manifest=manifest, samples=samples,
            walk_forward_config=config, session_calendar=exact_calendar,
            market_archive_repository=market_archive_repository,
            market_archive_fingerprint=ranged_spec.market_data_fingerprint,
            universe_archive_repository=universe_archive_repository,
            universe_archive_fingerprint=ranged_spec.universe_fingerprint,
            tradability_repository=tradability_repository,
            tradability_replay_capture=replay_capture,
            financial_feature_repository=validation_context.get("financial_feature_repository"),
            financial_archive_fingerprint=baseline_identity.get("financial_archive_fingerprint"),
        )
        expected_experiment_fingerprint = ranged_spec.fingerprint
    owner_identities = proof.get("owner_identities") or {}
    if (proof.get("status") != "ready"
            or not isinstance(proof.get("result"), Mapping)
            or proof["result"].get("status") != "completed"
            or owner_identities.get("calendar_fingerprint") != exact_calendar.calendar_fingerprint
            or owner_identities.get("market_archive_fingerprint")
                != baseline_identity.get("market_archive_fingerprint")
            or owner_identities.get("universe_archive_fingerprint")
                != baseline_identity.get("universe_archive_fingerprint")
            or owner_identities.get("dataset_fingerprint") != baseline_identity.get("dataset_fingerprint")
            or owner_identities.get("financial_archive_fingerprint")
                != baseline_identity.get("financial_archive_fingerprint")
            or not owner_identities.get("tradability_evidence_fingerprint")):
        raise ValueError("date_range_pit_revalidation_unavailable")
    if owner_identities["tradability_evidence_fingerprint"] != ranged_tradability_fingerprint:
        raise ValueError("date_range_tradability_identity_mismatch")
    return {"experiment_fingerprint": expected_experiment_fingerprint,
            "calendar_fingerprint": exact_calendar.calendar_fingerprint,
            "tradability_evidence_fingerprint": ranged_tradability_fingerprint,
            "validation_evidence_fingerprint": proof.get("validation_evidence_fingerprint"),
            "run_key": proof.get("run_key"), "runner_version": proof.get("runner_version")}


def _execution_stress(category: str, parameters: Mapping[str, Any], spec: EC.ExperimentSpec):
    stress: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    if category == "cost":
        costs = dict(spec.cost_model)
        values = {
            "commission_rate": ("commission_multiplier", "commission_rate"),
            "minimum_commission": ("minimum_commission_multiplier", "minimum_commission"),
            "stamp_duty_rate": ("stamp_duty_multiplier", "stamp_duty_rate"),
        }
        for target, (multiplier_key, source_key) in values.items():
            if multiplier_key in parameters:
                baseline, multiplier = costs[source_key], parameters[multiplier_key]
                stressed = baseline * multiplier
                costs[target] = stressed
                evidence[target] = {"baseline": baseline, "multiplier": multiplier,
                                    "stressed": stressed}
        stress["cost_model"] = costs
    elif category == "slippage":
        if spec.cost_model.get("slippage_model") != "fixed-rate-v1":
            raise ValueError("slippage_model_unsupported")
        baseline = spec.cost_model.get("slippage_parameters", {}).get("rate")
        multiplier = parameters["multiplier"]
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
            raise ValueError("slippage_baseline_unavailable")
        evidence["slippage_rate"] = {"model": "fixed-rate-v1", "baseline": baseline,
                                     "multiplier": multiplier,
                                     "stressed": baseline * multiplier}
        stress["slippage_multiplier"] = multiplier
    elif category == "execution_delay":
        stress["execution_delay_sessions"] = parameters["execution_delay_sessions"]
    elif category == "signal_delay":
        stress["signal_delay_sessions"] = parameters["signal_delay_sessions"]
    elif category == "liquidity":
        stress["liquidity_multiplier"] = parameters["liquidity_multiplier"]
        evidence["effective_volume_multiplier"] = parameters["liquidity_multiplier"]
    return stress, evidence


def _resolve_subject(*, spec, plan, baseline_run, strategy_version, candidate_replay):
    """Return (subject_kind, baseline_identity, ast, deps, candidate_path, replay)."""
    candidate_path = isinstance(spec, EC.CandidateExperimentSpec)
    if candidate_path:
        if not isinstance(candidate_replay, CE.CandidateReplayDefinition):
            raise RobustnessBaselineError("candidate_replay_definition_required")
        if candidate_replay.subject != spec.subject:
            raise RobustnessBaselineError("candidate_identity_mismatch")
        baseline_identity = _verify_candidate_baseline(baseline_run, spec, plan, candidate_replay)
        dependencies = PV.replay_dsl_dependencies(candidate_replay)
        return ("strategy_candidate", baseline_identity, None, dependencies, True, candidate_replay)
    if not isinstance(strategy_version, SR.StrategyVersion):
        raise RobustnessBaselineError("strategy_identity_mismatch")
    baseline_identity = _verify_formal_baseline(baseline_run, spec, plan, strategy_version)
    dependencies = PV.strategy_dsl_dependencies(strategy_version.definition.get("dsl_ast"))
    return ("strategy_version", baseline_identity, strategy_version, dependencies, False, None)


def run_robustness(*, baseline_run: Mapping[str, Any], spec,
                   plan: RC.RobustnessPlan,
                   strategy_version: SR.StrategyVersion | None = None,
                   candidate_replay: CE.CandidateReplayDefinition | None = None,
                   session_calendar: HSC.HistoricalSessionCalendar,
                   market_archive_repository: HMA.HistoricalMarketArchiveRepository,
                   universe_archive_repository: HUA.HistoricalUniverseArchiveRepository,
                   tradability_repository: TA.TradabilityArchiveRepository,
                   financial_features: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
                   required_financial_fields: Sequence[str] = (),
                   extended_session_calendar: HSC.HistoricalSessionCalendar | None = None,
                   date_range_validation_context: Mapping[str, Any] | None = None,
                   created_at: str | None = None) -> dict[str, Any]:
    """Run bounded deterministic stress cases against exact canonical R29 inputs.

    Formal ``StrategyVersion`` and ``StrategyCandidate`` share the same execution loop
    and evidence contract; only the subject verifier, replay source, universe scope and
    runner/report version differ.
    """
    subject_kind, baseline_identity, strategy_version, dependencies, candidate_path, candidate_replay = (
        _resolve_subject(spec=spec, plan=plan, baseline_run=baseline_run,
                         strategy_version=strategy_version, candidate_replay=candidate_replay))
    runner_version = CANDIDATE_RUNNER_VERSION if candidate_path else RUNNER_VERSION
    if (not isinstance(session_calendar, HSC.HistoricalSessionCalendar)
            or session_calendar.calendar_fingerprint != baseline_identity["calendar_fingerprint"]):
        raise RobustnessBaselineError("baseline_calendar_unavailable")
    baseline_sessions = list(session_calendar.sessions)
    market_manifest = market_archive_repository.get_manifest(spec.market_data_fingerprint)
    if market_manifest is None or market_manifest.adjustment != "raw":
        raise RobustnessBaselineError("baseline_market_archive_unavailable")
    coverage_calendar = extended_session_calendar or session_calendar
    if (not isinstance(coverage_calendar, HSC.HistoricalSessionCalendar)
            or coverage_calendar.source_archive_fingerprint != spec.market_data_fingerprint
            or coverage_calendar.benchmark_symbol != session_calendar.benchmark_symbol
            or not set(session_calendar.sessions).issubset(coverage_calendar.sessions)):
        raise RobustnessBaselineError("extended_calendar_owner_mismatch")
    issued_calendar = HSC.issue_from_market_archive(
        market_archive_repository, archive_fingerprint=spec.market_data_fingerprint,
        benchmark_symbol=session_calendar.benchmark_symbol,
        start=coverage_calendar.coverage_start, end=coverage_calendar.coverage_end)
    if (coverage_calendar.calendar_fingerprint != issued_calendar.calendar_fingerprint
            or tuple(coverage_calendar.sessions) != issued_calendar.sessions):
        raise RobustnessBaselineError("extended_calendar_not_owner_issued")
    sessions = list(coverage_calendar.sessions)
    members = R29._universe_rows(universe_archive_repository, spec.universe_fingerprint, sessions)
    if candidate_path:
        # Candidate scope FIRST, then every perturbation (incl. universe stress).
        members = CE.filter_candidate_members(candidate_replay, members, spec.universe_fingerprint)
    replay_capture = R29.PV.tradability_replay_projection(
        members, sessions, tradability_repository)
    baseline_members = {session: members[session] for session in baseline_sessions}
    baseline_capture = R29.PV.tradability_replay_projection(
        baseline_members, baseline_sessions, tradability_repository, captured=replay_capture)
    archive_tradability_fingerprint = baseline_capture["fingerprint"]
    if (not replay_capture["capture_complete"]
            or not baseline_capture["capture_complete"]
            or archive_tradability_fingerprint != baseline_identity["tradability_evidence_fingerprint"]
            or archive_tradability_fingerprint != spec.tradability_fingerprint):
        raise RobustnessBaselineError("baseline_tradability_identity_mismatch")
    symbols = sorted({row["code"] for rows in members.values() for row in rows})
    bars = market_archive_repository.read_bars(spec.market_data_fingerprint,
        start=coverage_calendar.coverage_start, end=coverage_calendar.coverage_end, symbols=symbols)
    regime_policy = plan.projection()["regime_policy"]
    benchmark_bars = market_archive_repository.read_bars(spec.market_data_fingerprint,
        start=coverage_calendar.coverage_start, end=coverage_calendar.coverage_end,
        symbols=[regime_policy["benchmark_symbol"]])
    regime_labels = RR.classify_sessions(benchmark_bars, sessions, regime_policy)
    execution_facts = replay_capture["execution_facts"]
    ast = (None if candidate_path
           else DSL.normalize(strategy_version.definition["dsl_ast"]))
    canonical_replay = candidate_replay
    baseline_result = baseline_run["result"]["metrics"]
    baseline_metrics = {key: baseline_result.get(key) for key in _METRIC_KEYS}
    cases = []
    fragilities: set[str] = set()
    sensitivity: dict[str, list[dict[str, Any]]] = {}
    for scenario in plan.scenarios():
        category, parameters = scenario["category"], scenario["parameters"]
        case_evidence = {
            "scenario_fingerprint": scenario["scenario_fingerprint"], "category": category,
            "parameters": parameters, "baseline_owner_identities": baseline_identity,
            "transform_version": TRANSFORM_VERSION, "input_coverage": {
                "calendar_fingerprint": coverage_calendar.calendar_fingerprint,
                "market_archive_fingerprint": spec.market_data_fingerprint,
                "session_count": len(sessions), "symbol_count": len(symbols)},
            "status": "available", "reason_code": None,
            "affected_sessions": [], "affected_symbols": [],
            "masked_observations": 0, "execution_changes": {},
        }
        if candidate_path:
            case_evidence["candidate_id"] = spec.subject.candidate_id
            case_evidence["candidate_replay_fingerprint"] = canonical_replay.replay_fingerprint
        result = None
        try:
            scenario_sessions = _scenario_sessions(scenario, spec, sessions)
            baseline_start, baseline_end = _baseline_session_bounds(spec, sessions)
            declared_baseline_sessions = sessions[baseline_start:baseline_end + 1]
            expands_baseline = (scenario_sessions[0] < declared_baseline_sessions[0]
                                or scenario_sessions[-1] > declared_baseline_sessions[-1])
            changes_date_range = scenario_sessions != declared_baseline_sessions
            if changes_date_range:
                if expands_baseline and extended_session_calendar is None:
                    raise ValueError("date_expansion_owner_calendar_missing")
                case_evidence["date_range_pit_proof"] = _revalidate_scenario_date_range(
                    sessions=scenario_sessions, spec=spec,
                    baseline_identity=baseline_identity,
                    market_archive_repository=market_archive_repository,
                    universe_archive_repository=universe_archive_repository,
                    tradability_repository=tradability_repository,
                    strategy_version=strategy_version,
                    candidate_replay=canonical_replay if candidate_path else None,
                    validation_context=date_range_validation_context,
                    replay_capture=replay_capture,
                    scenario_fingerprint=scenario["scenario_fingerprint"])
            scenario_members = {session: list(members.get(session, ())) for session in scenario_sessions}
            scenario_bars = [row for row in bars if row["session"] in scenario_sessions]
            expected_bar_count = len(scenario_bars)
            scenario_ast = ast
            scenario_replay = canonical_replay
            stress, execution_changes = _execution_stress(category, parameters, spec)
            case_evidence["execution_changes"] = execution_changes
            if category == "parameter":
                derived_entry = SPS.apply_parameter_stress(
                    canonical_replay.candidate.entry_spec if candidate_path else ast, parameters)
                if candidate_path:
                    scenario_replay = canonical_replay.derive_entry_ast(derived_entry)
                    case_evidence["execution_changes"] = {
                        "path": parameters["path"], "operation": parameters["operation"],
                        "value": parameters["value"], "derived_ast": derived_entry,
                        "derived_entry_fingerprint": scenario_replay.derived_entry_fingerprint,
                        "derived_replay_fingerprint": scenario_replay.scenario_replay_fingerprint,
                        "candidate_replay_fingerprint": canonical_replay.replay_fingerprint}
                else:
                    scenario_ast = derived_entry
                    case_evidence["execution_changes"] = {
                        "path": parameters["path"], "operation": parameters["operation"],
                        "value": parameters["value"], "derived_ast": scenario_ast}
            elif category == "data_missingness":
                fraction = parameters["missing_fraction"]
                kept = []
                for bar in scenario_bars:
                    if _bar_is_masked(bar, seed=scenario["seed"], fraction=fraction,
                                      field_scope=parameters["field_scope"]):
                        case_evidence["affected_sessions"].append(bar["session"])
                        case_evidence["affected_symbols"].append(bar["code"])
                    else:
                        kept.append(bar)
                case_evidence["masked_observations"] = len(scenario_bars) - len(kept)
                scenario_bars = kept
                if case_evidence["masked_observations"]:
                    raise EM.ExperimentExecutionUnavailable("stress_required_market_bar_missing")
            elif category == "universe":
                fraction = parameters["drop_fraction"]
                scenario_members, dropped = _drop_universe_members(
                    scenario_members, seed=scenario["seed"], fraction=fraction)
                if not any(scenario_members.values()):
                    raise ValueError("universe_perturbation_empty")
                case_evidence["affected_symbols"] = dropped
                case_evidence["affected_sessions"] = list(scenario_sessions)
            scenario_requests = {(str(row["code"]), session): f"{session}T09:30:00+08:00"
                                 for session in scenario_sessions
                                 for row in scenario_members.get(session, ())}
            scenario_facts = {key: execution_facts.get(key) for key in scenario_requests}
            replay_kwargs = ({"candidate_replay": scenario_replay} if candidate_path
                             else {"ast": scenario_ast})
            metrics_raw = EM.simulate_with_trace(
                spec=spec, **replay_kwargs, sessions=scenario_sessions,
                members_by_session=scenario_members, bars=scenario_bars,
                tradability_repository=tradability_repository,
                tradability_evidence=scenario_facts,
                financial_features=financial_features,
                required_financial_fields=required_financial_fields,
                stress=stress)
            metrics = _metric_projection(metrics_raw)
            metrics["data_coverage"] = (len(scenario_bars) / expected_bar_count
                                         if expected_bar_count else None)
            delta = {key: (None if metrics[key] is None or baseline_metrics.get(key) is None
                           else metrics[key] - baseline_metrics[key])
                     for key in metrics if key != "trade_count"}
            delta["trade_count"] = (None if metrics.get("trade_count") is None
                                    or baseline_metrics.get("trade_count") is None
                                    else metrics["trade_count"] - baseline_metrics["trade_count"])
            result = RC.case_result(scenario_fingerprint=scenario["scenario_fingerprint"],
                                    status="completed", metrics=metrics,
                                    baseline_delta=delta)
            trace = metrics_raw["trace"]
            if category in {"cost", "slippage", "liquidity", "parameter"}:
                sensitivity.setdefault(category, []).append({
                    "scenario_fingerprint": scenario["scenario_fingerprint"],
                    "parameters": parameters, "metrics": metrics, "baseline_delta": delta})
            if (category == "slippage" and plan.max_return_degradation is not None
                    and delta.get("return") is not None
                    and -delta["return"] > plan.max_return_degradation):
                fragilities.add("high_slippage_sensitive")
            if (category == "liquidity" and delta.get("capacity_proxy") is not None
                    and delta["capacity_proxy"] < 0):
                fragilities.add("liquidity_sensitive")
            case_evidence["regime_breakdown"] = RR.summarize_trace(trace, regime_labels)
        except (ValueError, KeyError, TypeError, EM.ExperimentExecutionUnavailable) as exc:
            reason = getattr(exc, "reason", None) or (str(exc) if str(exc).isidentifier()
                                                         else "scenario_inputs_unavailable")
            case_evidence["status"] = "unavailable"
            case_evidence["reason_code"] = reason
            result = RC.case_result(scenario_fingerprint=scenario["scenario_fingerprint"],
                                    status="unavailable", metrics=None,
                                    baseline_delta=None, reason_code=reason)
            if category == "data_missingness":
                fragilities.add("data_gap_unavailable")
        except Exception:
            case_evidence["status"] = "available"
            case_evidence["reason_code"] = "robustness_execution_failed"
            result = RC.case_result(scenario_fingerprint=scenario["scenario_fingerprint"],
                                    status="failed", metrics=None,
                                    baseline_delta=None, reason_code="robustness_execution_failed")
        case_evidence["affected_sessions"] = sorted(set(case_evidence["affected_sessions"]))
        case_evidence["affected_symbols"] = sorted(set(case_evidence["affected_symbols"]))
        cases.append({"scenario": scenario, "evidence": case_evidence, "result": result})
    baseline_sessions = list(session_calendar.sessions)
    baseline_members = {session: members[session] for session in baseline_sessions}
    baseline_bars = [row for row in bars if row["session"] in baseline_sessions]
    baseline_facts = {key: fact for key, fact in execution_facts.items()
                       if key[1] in baseline_sessions}
    baseline_replay_kwargs = ({"candidate_replay": canonical_replay} if candidate_path
                              else {"ast": ast})
    baseline_trace_result = EM.simulate_with_trace(
        spec=spec, **baseline_replay_kwargs, sessions=baseline_sessions,
        members_by_session=baseline_members, bars=baseline_bars,
        tradability_repository=tradability_repository,
        tradability_evidence=baseline_facts, financial_features=financial_features,
        required_financial_fields=required_financial_fields)
    replayed_baseline = _metric_projection(baseline_trace_result)
    if any(replayed_baseline.get(key) != baseline_metrics.get(key)
           for key in _METRIC_KEYS if key != "trade_count") or (
               replayed_baseline.get("trade_count") != baseline_metrics.get("trade_count")):
        raise RobustnessBaselineError("baseline_replay_not_bit_identical")
    regime_analysis = RR.summarize_trace(baseline_trace_result["trace"], regime_labels)
    unavailable = [case["scenario"]["scenario_fingerprint"] for case in cases
                   if case["result"]["status"] == "unavailable"]
    failed = [case["scenario"]["scenario_fingerprint"] for case in cases
              if case["result"]["status"] == "failed"]
    threshold_breaches = []
    for case in cases:
        delta = case["result"].get("baseline_delta")
        if not isinstance(delta, Mapping):
            continue
        limit = plan.max_drawdown_limit
        drawdown = case["result"].get("metrics", {}).get("drawdown")
        if limit is not None and drawdown is not None and abs(drawdown) > limit:
            threshold_breaches.append({"dimension": "drawdown",
                "scenario_fingerprint": case["scenario"]["scenario_fingerprint"],
                "observed": drawdown, "threshold": limit})
        limit = plan.max_return_degradation
        if limit is not None and delta.get("return") is not None and -delta["return"] > limit:
            threshold_breaches.append({"dimension": "return_degradation",
                "scenario_fingerprint": case["scenario"]["scenario_fingerprint"],
                "observed": -delta["return"], "threshold": limit})
    if any(regime_analysis.get("trend", {}).get(label, {}).get("return") is not None
           and regime_analysis["trend"][label]["return"] < 0 for label in ("bear",)):
        fragilities.add("bear_regime_weakness")
    if any(case["scenario"]["category"] == "parameter"
           and case["result"]["status"] == "completed"
           and case["result"]["baseline_delta"].get("return") not in (None, 0)
           for case in cases):
        fragilities.add("parameter_sensitive")
    report_core = {
        "runner_version": runner_version, "baseline_identity": baseline_identity,
        "baseline_spec": spec.projection(), "baseline_run_key": baseline_identity["run_key"],
        "baseline_experiment_fingerprint": spec.fingerprint,
        "baseline_result_fingerprint": baseline_identity["result_fingerprint"],
        "plan_fingerprint": plan.fingerprint, "plan": plan.projection(),
        "cases": cases, "regime_analysis": regime_analysis,
        "sensitivity_analysis": sensitivity,
        "data_coverage": {"sessions": len(sessions), "symbols": len(symbols),
                          "bars": len(bars), "masked_observations": sum(
                              case["evidence"]["masked_observations"] for case in cases)},
        "observed_fragilities": sorted(fragilities), "unavailable_cases": unavailable,
        "failed_cases": failed, "threshold_breaches": threshold_breaches,
    }
    fingerprint_cases = [{"scenario": case["scenario"], "evidence": case["evidence"],
                          "result": case["result"]} for case in cases]
    fingerprint = RC.report_fingerprint(
        baseline_identity={**baseline_identity, "spec": spec.projection()},
        plan_fingerprint=plan.fingerprint, cases=fingerprint_cases,
        report_version=runner_version)
    return {"report_fingerprint": fingerprint,
            "report_key": _sha({"baseline_run_key": baseline_identity["run_key"],
                "baseline_result_fingerprint": baseline_identity["result_fingerprint"],
                "plan_fingerprint": plan.fingerprint, "runner_version": runner_version}),
            "report_version": runner_version, **report_core,
            "created_at": created_at}
