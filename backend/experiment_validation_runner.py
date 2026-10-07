"""Offline canonical PIT validation runner for one exact ExperimentSpec."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

try:
    import experiment_contract as EC
    import candidate_experiment as CE
    import experiment_execution_model as EM
    import experiment_pit_validation as PV
    import financial_feature_evidence as FFE
    import historical_market_archive as HMA
    import historical_session_calendar as HSC
    import historical_universe_archive as HUA
    import point_in_time as PIT
    import strategy_dsl_schema as DSL
    import strategy_registry as SR
    import tradability_archive as TA
    import walk_forward_validation as WFV
    import experiment_validation_repository as EVR
except ImportError:  # pragma: no cover
    from . import experiment_contract as EC
    from . import candidate_experiment as CE
    from . import experiment_execution_model as EM
    from . import experiment_pit_validation as PV
    from . import financial_feature_evidence as FFE
    from . import historical_market_archive as HMA
    from . import historical_session_calendar as HSC
    from . import historical_universe_archive as HUA
    from . import point_in_time as PIT
    from . import strategy_dsl_schema as DSL
    from . import strategy_registry as SR
    from . import tradability_archive as TA
    from . import walk_forward_validation as WFV
    from . import experiment_validation_repository as EVR

RUNNER_VERSION = "r29-pit-validation-runner-v1"
CANDIDATE_RUNNER_VERSION = "r29-candidate-pit-validation-runner-v1"


def _unavailable(spec: EC.ExperimentSpec, code: str) -> EC.ExperimentResult:
    return EC.ExperimentResult(status="unavailable", experiment_fingerprint=spec.fingerprint,
                               failure_reason=code)


def _sha(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _universe_rows(repository: HUA.HistoricalUniverseArchiveRepository,
                   fingerprint: str, sessions: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        instant = PIT.bar_available_at(session)
        visible = repository.membership_rows(fingerprint, asof=instant.isoformat()) if instant else []
        projection = PIT.historical_universe(
            visible, session,
            source=repository.source_projection(fingerprint), drop_unproven=True,
        )
        if not projection.get("passed"):
            raise ValueError("historical_universe_unproven")
        result[session] = list(projection.get("members") or ())
    return result


def _tradability_fingerprint(members: Mapping[str, Sequence[Any]], sessions: Sequence[str],
                             repository: Any) -> str:
    return PV.tradability_replay_projection(members, sessions, repository)["fingerprint"]


def _serialize_result(result: EC.ExperimentResult) -> dict[str, Any]:
    return result.projection()


def run_validation(
    spec: EC.ExperimentSpec, *, runner_code_revision: str,
    strategy_version: SR.StrategyVersion | None = None,
    strategy_candidate: Any = None,
    dataset_manifest: Mapping[str, Any] | None,
    samples: Sequence[Any], walk_forward_config: WFV.WalkForwardConfig,
    session_calendar: HSC.HistoricalSessionCalendar | None,
    market_archive_repository: HMA.HistoricalMarketArchiveRepository,
    market_archive_fingerprint: str,
    universe_archive_repository: HUA.HistoricalUniverseArchiveRepository,
    universe_archive_fingerprint: str,
    tradability_repository: TA.TradabilityArchiveRepository,
    financial_feature_repository: FFE.FinancialFeatureEvidenceRepository | None = None,
    financial_archive_fingerprint: str | None = None,
    fundamental_records: Sequence[Any] = (),
    validation_repository: EVR.ExperimentValidationRepository | None = None,
    created_at: str | None = None,
    tradability_replay_capture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and, when ready, replay a strategy using only injected immutable owners.

    Build identity and every owner identity are explicit arguments. No current/latest
    selection, provider calls, filesystem discovery, or git subprocesses occur here.
    """
    if not isinstance(spec, (EC.ExperimentSpec, EC.CandidateExperimentSpec)):
        raise ValueError("spec must be an ExperimentSpec")
    if (runner_code_revision != spec.code_revision
            or not isinstance(runner_code_revision, str)):
        result = _unavailable(spec, "code_revision_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    candidate_path = isinstance(spec, EC.CandidateExperimentSpec)
    candidate_replay = None
    if candidate_path:
        if (not isinstance(walk_forward_config, WFV.WalkForwardConfig)
                or walk_forward_config.fingerprint != spec.parameter_set.get("walk_forward_config_fingerprint")
                or not isinstance(spec.parameter_set.get("experiment_plan_fingerprint"), str)
                or len(spec.parameter_set["experiment_plan_fingerprint"]) != 64):
            return {"status": "unavailable", "result": _unavailable(spec, "candidate_experiment_plan_mismatch").projection(),
                    "validation_evidence": None, "folds": [], "run_key": None}
        try:
            candidate_replay = CE.compile_candidate_replay(strategy_candidate)
            if strategy_version is not None or candidate_replay.subject != spec.subject:
                raise ValueError("candidate_identity_mismatch")
            CE.validate_candidate_asof(strategy_candidate, spec.end_date, spec.asof_policy["cutoff"])
        except (TypeError, ValueError) as exc:
            code = "candidate_asof_leakage" if str(exc) == "candidate_asof_leakage" else "candidate_identity_mismatch"
            return {"status": "unavailable", "result": _unavailable(spec, code).projection(),
                    "validation_evidence": None, "folds": [], "run_key": None}
    if not candidate_path and (not isinstance(strategy_version, SR.StrategyVersion) or strategy_candidate is not None):
        result = _unavailable(spec, "strategy_identity_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if not candidate_path and not (strategy_version.strategy_id == spec.strategy.strategy_id
            and strategy_version.version == spec.strategy.version
            and strategy_version.checksum == spec.strategy.checksum):
        result = _unavailable(spec, "strategy_identity_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    ast = (strategy_version.definition.get("dsl_ast") if not candidate_path
           and isinstance(strategy_version.definition, Mapping) else None)
    if ast is None and not candidate_path:
        result = _unavailable(spec, "strategy_replay_definition_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    try:
        normalized_ast = DSL.normalize(ast) if not candidate_path else None
        dependencies = (PV.replay_dsl_dependencies(candidate_replay) if candidate_path
                        else PV.strategy_dsl_dependencies(normalized_ast))
    except (TypeError, ValueError):
        result = _unavailable(spec, "strategy_replay_definition_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if dependencies["other_fields"] or dependencies["fund_flow_fields"]:
        result = _unavailable(spec, "strategy_input_owner_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if not candidate_path and dependencies["financial_fields"] and (
            financial_feature_repository is None or not financial_archive_fingerprint
            or spec.parameter_set.get("financial_archive_fingerprint") != financial_archive_fingerprint):
        result = _unavailable(spec, "financial_feature_evidence_missing")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if (not isinstance(session_calendar, HSC.HistoricalSessionCalendar)
            or session_calendar.calendar_fingerprint != spec.parameter_set.get("validation_calendar_fingerprint")):
        result = _unavailable(spec, "historical_session_calendar_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if dataset_manifest is None or dataset_manifest.get("dataset_fingerprint") != spec.dataset_fingerprint:
        result = _unavailable(spec, "dataset_identity_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}

    tradability_capture: dict[str, Any] = {}
    evidence = PV.build_pit_validation_evidence(
        spec, strategy_version=strategy_version, candidate_replay=candidate_replay, dataset_manifest=dataset_manifest,
        tradability_repository=tradability_repository, fundamental_records=fundamental_records,
        financial_feature_repository=financial_feature_repository,
        financial_archive_fingerprint=financial_archive_fingerprint,
        samples=samples, walk_forward_config=walk_forward_config,
        session_calendar=session_calendar, market_archive_repository=market_archive_repository,
        market_archive_fingerprint=market_archive_fingerprint,
        universe_archive_repository=universe_archive_repository,
        universe_archive_fingerprint=universe_archive_fingerprint,
        tradability_replay_capture=tradability_replay_capture,
        tradability_capture_out=tradability_capture,
    )
    folds = list(EC._thaw_json(evidence.walk_forward.get("windows") or ()))
    members = tradability_capture.get("members_by_session", {})
    if evidence.status != "ready":
        result = _unavailable(spec, evidence.reason_codes[0])
        run_status = "blocked"
    else:
        try:
            bars = market_archive_repository.read_bars(
                market_archive_fingerprint, start=spec.start_date, end=spec.end_date,
                symbols=sorted({row["code"] for values in members.values() for row in values}),
            )
            execution_facts = tradability_capture["execution_facts"]
            financial_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
            if dependencies["financial_fields"]:
                for sample in samples:
                    sample_key = getattr(sample, "sample_key", None)
                    code = getattr(sample, "code", None)
                    session = (getattr(sample, "feature_asof", None)
                               or getattr(sample, "decision_session", None))
                    if not sample_key or not code or not session:
                        continue
                    for name in dependencies["financial_fields"]:
                        item = financial_feature_repository.resolve_for_dataset_sample(
                            dataset_fingerprint=spec.dataset_fingerprint,
                            sample_key=str(sample_key), feature_name=name,
                            financial_archive_fingerprint=financial_archive_fingerprint,
                        ).projection()
                        if item.get("verification") != "proven":
                            continue
                        pair = (str(code), str(session))
                        entry = financial_by_pair.setdefault(pair, {"decision_at": item.get("decision_at")})
                        if entry.get("decision_at") != item.get("decision_at"):
                            raise EM.ExperimentExecutionUnavailable("financial_feature_decision_mismatch")
                        prior = entry.get(name)
                        if prior is not None and prior != item.get("feature_value"):
                            raise EM.ExperimentExecutionUnavailable("financial_feature_value_mismatch")
                        entry[name] = item.get("feature_value")
            replay_args = {"candidate_replay": candidate_replay} if candidate_path else {"ast": normalized_ast}
            metrics = EM.simulate(spec, **replay_args, sessions=session_calendar.sessions,
                                  members_by_session=members, bars=bars,
                                  tradability_repository=tradability_repository,
                                  tradability_evidence=execution_facts,
                                  financial_features=financial_by_pair,
                                  required_financial_fields=dependencies["financial_fields"])
            result = EC.ExperimentResult(
                experiment_fingerprint=spec.fingerprint, status="completed",
                total_return=metrics["total_return"], max_drawdown=metrics["max_drawdown"],
                volatility=metrics["volatility"], turnover=metrics["turnover"],
                trade_count=metrics["trade_count"], total_cost=metrics["total_cost"],
                exposure=metrics["exposure"], capacity_proxy=metrics["capacity_proxy"],
                data_coverage=metrics["data_coverage"], regime_breakdown=metrics["regime_breakdown"],
            )
            run_status = "ready"
        except EM.ExperimentExecutionUnavailable as exc:
            result = _unavailable(spec, exc.reason)
            run_status = "blocked"
        except Exception as exc:
            if candidate_path and isinstance(exc, (OSError, sqlite3.Error)):
                raise  # Infrastructure failure is retryable queue work, not canonical evaluation evidence.
            result = EC.ExperimentResult(status="failed", experiment_fingerprint=spec.fingerprint,
                                         failure_reason="validation_execution_failed")
            run_status = "failed"

    runner_version = CANDIDATE_RUNNER_VERSION if candidate_path else RUNNER_VERSION
    owner_identities = {
        "calendar_fingerprint": session_calendar.calendar_fingerprint if session_calendar else None,
        "universe_archive_fingerprint": universe_archive_fingerprint,
        "tradability_evidence_fingerprint": tradability_capture.get("fingerprint"),
        "market_archive_fingerprint": market_archive_fingerprint,
        "financial_archive_fingerprint": financial_archive_fingerprint,
        "dataset_fingerprint": spec.dataset_fingerprint,
        ("experiment_subject" if candidate_path else "strategy_version"):
            spec.subject.projection() if candidate_path else spec.strategy.projection(),
        "validation_evidence_fingerprint": evidence.validation_evidence_fingerprint,
    }
    if candidate_path:
        owner_identities["experiment_plan_fingerprint"] = spec.parameter_set["experiment_plan_fingerprint"]
    run_key = EVR.ExperimentValidationRepository.build_run_key(
        spec.fingerprint, owner_identities, runner_version,
    )
    output = {"status": run_status, "run_key": run_key,
              "owner_identities": owner_identities,
              "validation_evidence": evidence.projection(),
              "validation_evidence_fingerprint": evidence.validation_evidence_fingerprint,
              "result": _serialize_result(result), "folds": folds}
    if validation_repository is not None:
        if not created_at:
            raise ValueError("created_at must be injected by the application boundary")
        subject_args = {"subject": spec.subject.projection(),
                        "experiment_plan_fingerprint": spec.parameter_set["experiment_plan_fingerprint"]} if candidate_path else {
            "strategy_id": spec.strategy.strategy_id, "strategy_version": spec.strategy.version,
            "strategy_checksum": spec.strategy.checksum}
        output["run"] = validation_repository.append_run(
            run_key=run_key, experiment_fingerprint=spec.fingerprint,
            **subject_args,
            calendar_fingerprint=owner_identities["calendar_fingerprint"],
            universe_archive_fingerprint=universe_archive_fingerprint,
            financial_archive_fingerprint=financial_archive_fingerprint,
            tradability_evidence_fingerprint=owner_identities["tradability_evidence_fingerprint"],
            market_archive_fingerprint=market_archive_fingerprint,
            dataset_fingerprint=spec.dataset_fingerprint,
            validation_evidence=evidence.projection(), result=result.projection(), folds=folds,
            runner_version=runner_version, runner_code_revision=runner_code_revision,
            created_at=created_at,
        )
    return output
