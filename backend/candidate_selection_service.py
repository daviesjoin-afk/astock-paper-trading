"""R36-C selection orchestration: exact R29/R30 evidence closure -> pure selection.

Chain:

    exact search run (v3, pinned selection policy)
        -> exact candidate pool (search_spec.candidate_ids, each re-self-verified)
        -> exact PIT job + exact completed event -> exact R29 run
        -> exact robustness job + exact completed event -> exact R30 report
        -> verify_candidate_robustness_report (existing canonical binding helper)
        -> pure candidate_selection.evaluate_selection()
        -> append one canonical selection report

Operational failure or missing execution evidence never becomes candidate elimination:
the whole selection is refused. This module adds no promotion, lifecycle, candidate
generation or AI authority.
"""
from __future__ import annotations

import candidate_experiment as CE
import candidate_selection as CS
import candidate_selection_repository as CSR
import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_validation_repository as EVR
import robustness_repository as RREP
import robustness_runner as RRUN
import strategy_candidate as SC
import strategy_candidate_repository as SCR


class CandidateSelectionUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _exact_search_v3(conn, search_run_id):
    """Read one exact v3 search run with a pinned selection policy."""
    run = ESR.get_search_run(conn, search_run_id)
    if run is None:
        raise CandidateSelectionUnavailable("search_run_not_found")
    spec = ESC.search_spec_from_projection(run["search_spec"])
    if spec.search_contract_version != ESC.SEARCH_CONTRACT_VERSION_V3:
        raise CandidateSelectionUnavailable("search_run_selection_policy_unavailable")
    if not isinstance(spec.selection_policy, CS.CandidateSelectionPolicy):
        raise CandidateSelectionUnavailable("search_run_selection_policy_unavailable")
    if spec.selection_policy.fingerprint != spec.selection_policy_fingerprint:
        raise CandidateSelectionUnavailable("selection_policy_fingerprint_mismatch")
    return run, spec


def _exact_pit_binding(conn, *, search_run_id, candidate_id):
    """Exact PIT job + its terminal event. Operational non-completion refuses selection."""
    job = _find_job(conn, search_run_id=search_run_id, candidate_id=candidate_id,
                    stage=ESC.JOB_STAGE_PIT_VALIDATION)
    if job is None:
        raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")
    events = ESR.list_job_events(conn, job["job_id"])
    state = events[-1]["event_kind"] if events else None
    if state != "completed":
        # failed / queued / claimed / cancelled are operational states, never performance.
        raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")
    last_event = events[-1]
    if (last_event["evidence_owner"] != "experiment_validation_run"
            or not last_event["evidence_id"]):
        raise CandidateSelectionUnavailable("corrupt_search_job")
    return job, last_event["evidence_id"]


def _exact_robustness_binding(conn, *, search_run_id, candidate_id):
    job = _find_job(conn, search_run_id=search_run_id, candidate_id=candidate_id,
                    stage=ESC.JOB_STAGE_ROBUSTNESS)
    if job is None:
        raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")
    events = ESR.list_job_events(conn, job["job_id"])
    state = events[-1]["event_kind"] if events else None
    if state != "completed":
        raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")
    last_event = events[-1]
    if (last_event["evidence_owner"] != "robustness_report"
            or not last_event["evidence_id"]):
        raise CandidateSelectionUnavailable("corrupt_search_job")
    return job, last_event["evidence_id"]


def _find_job(conn, *, search_run_id, candidate_id, stage):
    for job in ESR.list_search_jobs(conn, search_run_id):
        if job["stage"] == stage and job["candidate_id"] == candidate_id:
            return job
    return None


def _baseline_features(run):
    result = run["result"]
    if not isinstance(result, dict):
        raise CandidateSelectionUnavailable("selection_metric_unavailable")
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        raise CandidateSelectionUnavailable("selection_metric_unavailable")
    drawdown = metrics.get("drawdown")
    return {
        "baseline_return": metrics.get("return"),
        "baseline_drawdown_abs": None if drawdown is None else abs(drawdown),
        "baseline_turnover": metrics.get("turnover"),
        "baseline_trade_count": metrics.get("trade_count"),
        "baseline_data_coverage": metrics.get("data_coverage"),
    }


def _robustness_features(report):
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise CandidateSelectionUnavailable("corrupt_robustness_report")
    returns, drawdowns, degradations = [], [], []
    for case in cases:
        if not isinstance(case, dict):
            raise CandidateSelectionUnavailable("corrupt_robustness_report")
        result = case.get("result")
        if not isinstance(result, dict):
            raise CandidateSelectionUnavailable("corrupt_robustness_report")
        if result.get("status") != "completed":
            continue  # unavailable / failed only feed the gate counts, never worst metrics.
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            raise CandidateSelectionUnavailable("corrupt_robustness_report")
        if metrics.get("return") is None or metrics.get("drawdown") is None:
            raise CandidateSelectionUnavailable("selection_metric_unavailable")
        returns.append(metrics["return"])
        drawdowns.append(abs(metrics["drawdown"]))
        delta = result.get("baseline_delta")
        if isinstance(delta, dict) and delta.get("return") is not None:
            degradations.append(max(0, -delta["return"]))
    unavailable = report.get("unavailable_cases")
    failed = report.get("failed_cases")
    breaches = report.get("threshold_breaches")
    fragilities = report.get("observed_fragilities")
    for name, value in (("unavailable_cases", unavailable), ("failed_cases", failed),
                        ("threshold_breaches", breaches), ("observed_fragilities", fragilities)):
        if not isinstance(value, list):
            raise CandidateSelectionUnavailable("corrupt_robustness_report")
    return {
        "robustness_worst_return": min(returns) if returns else None,
        "robustness_worst_drawdown_abs": max(drawdowns) if drawdowns else None,
        "robustness_max_return_degradation": max(degradations) if degradations else None,
        "robustness_unavailable_count": len(unavailable),
        "robustness_failed_count": len(failed),
        "robustness_threshold_breach_count": len(breaches),
        "robustness_fragility_count": len(fragilities),
    }


def _collect_evidence(conn, *, run, spec, validation_repository, robustness_repository):
    """Prove evidence closure for every candidate in the exact search."""
    search_run_id = run["search_run_id"]
    evidence = []
    for candidate_id in spec.candidate_ids:
        try:
            candidate = SCR.get_candidate(conn, candidate_id)
        except (SC.CandidateValidationError, SCR.StrategyCandidateRepositoryError) as exc:
            raise CandidateSelectionUnavailable("candidate_identity_mismatch") from exc
        if candidate is None or candidate.candidate_id != candidate_id:
            raise CandidateSelectionUnavailable("candidate_identity_mismatch")
        if not SC.verify_candidate_fingerprint(candidate):
            raise CandidateSelectionUnavailable("candidate_identity_mismatch")
        _, run_key = _exact_pit_binding(conn, search_run_id=search_run_id,
                                        candidate_id=candidate_id)
        run = validation_repository.get_run(run_key=run_key)
        if run is None or run.get("run_key") != run_key:
            raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")
        if (run.get("subject_kind") != "strategy_candidate"
                or run.get("candidate_id") != candidate_id):
            raise CandidateSelectionUnavailable("corrupt_validation_run")
        result = run.get("result")
        if not isinstance(result, dict) or not result.get("result_fingerprint"):
            raise CandidateSelectionUnavailable("corrupt_validation_run")
        ready = (run.get("validation_status") == "ready"
                 and result.get("status") == "completed")
        common = {
            "candidate_id": candidate_id,
            "pit_run_key": run_key,
            "pit_result_fingerprint": result["result_fingerprint"],
            "pit_validation_status": run.get("validation_status"),
            "pit_result_status": result.get("status"),
        }
        if not ready:
            # Canonical R29 blocked / not-completed is real evidence: ineligible, no R30.
            evidence.append(CS.CandidateSelectionEvidence(**common))
            continue
        _, report_key = _exact_robustness_binding(
            conn, search_run_id=search_run_id, candidate_id=candidate_id)
        report = robustness_repository.get_report_by_key(report_key)
        if report is None or report.get("report_key") != report_key:
            raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")
        plan = spec.experiment_plan
        robustness_plan = plan.robustness_policy.bind(run_key, run["experiment_fingerprint"])
        replay = CE.compile_candidate_replay(candidate)
        candidate_spec = CE.build_candidate_experiment_spec(
            candidate, replay, plan, run["tradability_evidence_fingerprint"])
        try:
            RRUN.verify_candidate_robustness_report(
                report=report["report"], baseline_run=run, spec=candidate_spec,
                plan=robustness_plan, candidate_replay=replay)
        except RRUN.RobustnessBaselineError as exc:
            raise CandidateSelectionUnavailable("robustness_report_binding_mismatch") from exc
        features = _baseline_features(run)
        features.update(_robustness_features(report["report"]))
        evidence.append(CS.CandidateSelectionEvidence(
            **common, robustness_report_key=report_key,
            robustness_report_fingerprint=report["report_fingerprint"], **features))
    return evidence


def _evidence_binding(evidence):
    return [item.evidence_binding_projection() for item in evidence]


def select_search_candidates(conn, *, search_run_id, validation_repository,
                             robustness_repository, selection_repository,
                             created_at=None):
    """Exact search -> evidence closure -> pure selection -> canonical report append."""
    if conn.in_transaction:
        raise CandidateSelectionUnavailable("search_write_transaction_must_be_closed")
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateSelectionUnavailable("canonical_validation_repository_required")
    if not isinstance(robustness_repository, RREP.RobustnessRepository):
        raise CandidateSelectionUnavailable("canonical_robustness_repository_required")
    if not isinstance(selection_repository, CSR.CandidateSelectionRepository):
        raise CandidateSelectionUnavailable("canonical_selection_repository_required")
    run, spec = _exact_search_v3(conn, search_run_id)
    # Idempotent fast path: an exact canonical report for this search already exists.
    existing = selection_repository.get_selection_report_for_search(run["search_run_id"])
    if existing is not None:
        return {"status": "ok", "selection_report_key": existing["selection_report_key"],
                "report_fingerprint": existing["report"]["report_fingerprint"],
                "report": existing["report"]}
    # Read every owner fact outside any write transaction, then compute purely.
    evidence = _collect_evidence(conn, run=run, spec=spec,
                                 validation_repository=validation_repository,
                                 robustness_repository=robustness_repository)
    report = CS.evaluate_selection(
        search_run_id=run["search_run_id"],
        search_input_fingerprint=run["search_input_fingerprint"],
        policy=spec.selection_policy, evidence=evidence)
    stored = selection_repository.append_report(
        report, evidence_binding=_evidence_binding(evidence), created_at=created_at)
    return {"status": "ok", "selection_report_key": stored["selection_report_key"],
            "report_fingerprint": stored["report"]["report_fingerprint"],
            "report": stored["report"]}


__all__ = [
    "CandidateSelectionUnavailable",
    "select_search_candidates",
]
