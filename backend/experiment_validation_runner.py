"""Offline canonical PIT validation runner for one exact ExperimentSpec."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

try:
    import experiment_contract as EC
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
    strategy_version: SR.StrategyVersion | None,
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
) -> dict[str, Any]:
    """Validate and, when ready, replay a strategy using only injected immutable owners.

    Build identity and every owner identity are explicit arguments. No current/latest
    selection, provider calls, filesystem discovery, or git subprocesses occur here.
    """
    if not isinstance(spec, EC.ExperimentSpec):
        raise ValueError("spec must be an ExperimentSpec")
    if (runner_code_revision != spec.code_revision
            or not isinstance(runner_code_revision, str)):
        result = _unavailable(spec, "code_revision_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if not isinstance(strategy_version, SR.StrategyVersion):
        result = _unavailable(spec, "strategy_identity_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if not (strategy_version.strategy_id == spec.strategy.strategy_id
            and strategy_version.version == spec.strategy.version
            and strategy_version.checksum == spec.strategy.checksum):
        result = _unavailable(spec, "strategy_identity_mismatch")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    ast = strategy_version.definition.get("dsl_ast") if isinstance(strategy_version.definition, Mapping) else None
    if ast is None:
        result = _unavailable(spec, "strategy_replay_definition_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    try:
        normalized_ast = DSL.normalize(ast)
        dependencies = PV.strategy_dsl_dependencies(normalized_ast)
    except (TypeError, ValueError):
        result = _unavailable(spec, "strategy_replay_definition_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if dependencies["other_fields"] or dependencies["fund_flow_fields"]:
        result = _unavailable(spec, "strategy_input_owner_unavailable")
        return {"status": "unavailable", "result": result.projection(),
                "validation_evidence": None, "folds": [], "run_key": None}
    if dependencies["financial_fields"] and (
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

    evidence = PV.build_pit_validation_evidence(
        spec, strategy_version=strategy_version, dataset_manifest=dataset_manifest,
        tradability_repository=tradability_repository, fundamental_records=fundamental_records,
        financial_feature_repository=financial_feature_repository,
        financial_archive_fingerprint=financial_archive_fingerprint,
        samples=samples, walk_forward_config=walk_forward_config,
        session_calendar=session_calendar, market_archive_repository=market_archive_repository,
        market_archive_fingerprint=market_archive_fingerprint,
        universe_archive_repository=universe_archive_repository,
        universe_archive_fingerprint=universe_archive_fingerprint,
    )
    folds = list(evidence.walk_forward.get("windows") or ())
    if evidence.status != "ready":
        result = _unavailable(spec, evidence.reason_codes[0])
        run_status = "blocked"
        members = {}
    else:
        try:
            members = _universe_rows(universe_archive_repository, universe_archive_fingerprint,
                                     session_calendar.sessions)
            bars = market_archive_repository.read_bars(
                market_archive_fingerprint, start=spec.start_date, end=spec.end_date,
                symbols=sorted({row["code"] for values in members.values() for row in values}),
            )
            execution_requests = {
                (str(member["code"]), session): f"{session}T09:30:00+08:00"
                for session in session_calendar.sessions
                for member in members.get(session, ())
                if isinstance(member, Mapping) and member.get("code")
            }
            execution_facts = tradability_repository.evidence_many(execution_requests)
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
            metrics = EM.simulate(spec, ast=normalized_ast, sessions=session_calendar.sessions,
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
        except Exception:
            result = EC.ExperimentResult(status="failed", experiment_fingerprint=spec.fingerprint,
                                         failure_reason="validation_execution_failed")
            run_status = "failed"

    owner_identities = {
        "calendar_fingerprint": session_calendar.calendar_fingerprint if session_calendar else None,
        "universe_archive_fingerprint": universe_archive_fingerprint,
        "tradability_evidence_fingerprint": _tradability_fingerprint(
            members, session_calendar.sessions if session_calendar else (), tradability_repository),
        "market_archive_fingerprint": market_archive_fingerprint,
        "financial_archive_fingerprint": financial_archive_fingerprint,
        "dataset_fingerprint": spec.dataset_fingerprint,
        "strategy_version": spec.strategy.projection(),
        "validation_evidence_fingerprint": evidence.validation_evidence_fingerprint,
    }
    run_key = EVR.ExperimentValidationRepository.build_run_key(
        spec.fingerprint, owner_identities, RUNNER_VERSION,
    )
    output = {"status": run_status, "run_key": run_key,
              "owner_identities": owner_identities,
              "validation_evidence": evidence.projection(),
              "validation_evidence_fingerprint": evidence.validation_evidence_fingerprint,
              "result": _serialize_result(result), "folds": folds}
    if validation_repository is not None:
        if not created_at:
            raise ValueError("created_at must be injected by the application boundary")
        output["run"] = validation_repository.append_run(
            run_key=run_key, experiment_fingerprint=spec.fingerprint,
            strategy_id=spec.strategy.strategy_id, strategy_version=spec.strategy.version,
            strategy_checksum=spec.strategy.checksum,
            calendar_fingerprint=owner_identities["calendar_fingerprint"],
            universe_archive_fingerprint=universe_archive_fingerprint,
            financial_archive_fingerprint=financial_archive_fingerprint,
            tradability_evidence_fingerprint=owner_identities["tradability_evidence_fingerprint"],
            market_archive_fingerprint=market_archive_fingerprint,
            dataset_fingerprint=spec.dataset_fingerprint,
            validation_evidence=evidence.projection(), result=result.projection(), folds=folds,
            runner_version=RUNNER_VERSION, runner_code_revision=runner_code_revision,
            created_at=created_at,
        )
    return output
