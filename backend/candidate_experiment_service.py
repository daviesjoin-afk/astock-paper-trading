"""Exact candidate PIT orchestration; R29 remains the sole evaluation authority."""
from __future__ import annotations

import candidate_experiment as CE
import experiment_contract as EC
import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_search_service as ESS
import experiment_validation_repository as EVR
import experiment_validation_runner as RUNNER
import historical_session_calendar as HSC
import experiment_pit_validation as PV
import strategy_candidate_repository as SCR


class CandidateExperimentUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _job_inputs(conn, job_id):
    try:
        job = ESR.get_search_job(conn, job_id)
    except ESR.ExperimentSearchRepositoryError as exc:
        raise CandidateExperimentUnavailable("corrupt_search_job") from exc
    if job is None:
        raise CandidateExperimentUnavailable("search_job_not_found")
    if (not isinstance(job["job"], dict) or job["stage"] != ESC.JOB_STAGE_PIT_VALIDATION
            or job["job_contract_version"] != ESC.SEARCH_JOB_CONTRACT_VERSION):
        raise CandidateExperimentUnavailable("corrupt_search_job")
    job = ESC.SearchJobSpec(job["search_run_id"], job["candidate_id"],
                            job["stage"], job["job_contract_version"])
    run = ESR.get_search_run(conn, job.search_run_id)
    if run is None:
        raise CandidateExperimentUnavailable("search_run_not_found")
    spec = ESC.search_spec_from_projection(run["search_spec"])
    if spec.experiment_plan is None:
        raise CandidateExperimentUnavailable("search_run_experiment_plan_unavailable")
    if job.candidate_id not in spec.candidate_ids:
        raise CandidateExperimentUnavailable("corrupt_search_job")
    candidate = SCR.get_candidate(conn, job.candidate_id)
    if candidate is None:
        raise CandidateExperimentUnavailable("candidate_identity_mismatch")
    replay = CE.compile_candidate_replay(candidate)
    return job, spec.experiment_plan, candidate, replay


def prepare_candidate_experiment(conn, *, job_id, session_calendar, universe_archive_repository,
                                 tradability_repository):
    job, plan, candidate, replay = _job_inputs(conn, job_id)
    if (not isinstance(session_calendar, HSC.HistoricalSessionCalendar)
            or session_calendar.calendar_fingerprint != plan.session_calendar_fingerprint):
        raise CandidateExperimentUnavailable("historical_session_calendar_unavailable")
    if session_calendar.coverage_start != plan.start_date or session_calendar.coverage_end != plan.end_date:
        raise CandidateExperimentUnavailable("historical_session_calendar_unavailable")
    members = RUNNER._universe_rows(universe_archive_repository, plan.universe_archive_fingerprint,
                                    session_calendar.sessions)
    try:
        members = CE.filter_candidate_members(replay, members, plan.universe_archive_fingerprint)
    except CE.CandidateUniverseUnavailable:
        # No requested universe can be proven. R29 independently records the canonical blocked reason.
        members = {}
    capture = PV.tradability_replay_projection(members, session_calendar.sessions, tradability_repository)
    spec = CE.build_candidate_experiment_spec(candidate, replay, plan, capture["fingerprint"])
    return {"job": job, "plan": plan, "candidate": candidate, "replay": replay,
            "spec": spec, "tradability_replay_capture": capture}


def complete_candidate_pit_job(conn, *, job_id, run_key, validation_repository,
                                created_at=None, actor=None):
    """Independent exact row verification, then a short queue transaction; crash-safe and idempotent."""
    if conn.in_transaction:
        raise CandidateExperimentUnavailable("search_write_transaction_must_be_closed")
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateExperimentUnavailable("canonical_validation_repository_required")
    run = validation_repository.get_run(run_key=run_key)
    if run is None or run["run_key"] != run_key:
        raise CandidateExperimentUnavailable("validation_evidence_not_found")
    job, plan, candidate, replay = _job_inputs(conn, job_id)
    spec = CE.build_candidate_experiment_spec(candidate, replay, plan, run["tradability_evidence_fingerprint"])
    if (run["subject_kind"] != "strategy_candidate" or run["subject"] != replay.subject.projection()
            or run["experiment_fingerprint"] != spec.fingerprint
            or run["experiment_plan_fingerprint"] != plan.fingerprint
            or run["runner_version"] != RUNNER.CANDIDATE_RUNNER_VERSION
            or run["runner_code_revision"] != plan.code_revision
            or any(run[key] != value for key, value in {
                "dataset_fingerprint": plan.dataset_fingerprint,
                "market_archive_fingerprint": plan.market_archive_fingerprint,
                "universe_archive_fingerprint": plan.universe_archive_fingerprint,
                "calendar_fingerprint": plan.session_calendar_fingerprint,
                "financial_archive_fingerprint": plan.financial_archive_fingerprint}.items())):
        raise CandidateExperimentUnavailable("validation_evidence_identity_mismatch")
    evidence = run["validation_evidence"]
    if (evidence["experiment_fingerprint"] != spec.fingerprint
            or evidence["dimensions"].get("experiment_subject", {}).get("declared_identity") != spec.subject.projection()
            or run["result"]["experiment_fingerprint"] != spec.fingerprint):
        raise CandidateExperimentUnavailable("validation_evidence_identity_mismatch")
    owners = {"calendar_fingerprint": run["calendar_fingerprint"],
              "universe_archive_fingerprint": run["universe_archive_fingerprint"],
              "tradability_evidence_fingerprint": run["tradability_evidence_fingerprint"],
              "market_archive_fingerprint": run["market_archive_fingerprint"],
              "financial_archive_fingerprint": run["financial_archive_fingerprint"],
              "dataset_fingerprint": run["dataset_fingerprint"], "experiment_subject": run["subject"],
              "experiment_plan_fingerprint": run["experiment_plan_fingerprint"],
              "validation_evidence_fingerprint": EC._digest(evidence)}
    if EVR.ExperimentValidationRepository.build_run_key(spec.fingerprint, owners, run["runner_version"]) != run_key:
        raise CandidateExperimentUnavailable("validation_evidence_identity_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        events = ESR.list_job_events(conn, job_id)
        if not events:
            raise CandidateExperimentUnavailable("search_job_must_be_claimed")
        latest = events[-1]
        if latest["event_kind"] == "completed":
            if latest["evidence_owner"] != "experiment_validation_run" or latest["evidence_id"] != run_key:
                raise CandidateExperimentUnavailable("completion_evidence_conflict")
        elif latest["event_kind"] == "claimed":
            ESR.record_verified_completion_event(conn, job_id=job_id, search_run_id=job.search_run_id,
                                                 run_key=run_key, created_at=created_at, actor=actor)
        else:
            raise CandidateExperimentUnavailable("search_job_must_be_claimed")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"job_id": job_id, "event_kind": "completed", "evidence_owner": "experiment_validation_run",
            "evidence_id": run_key}


def _fail_job(conn, *, job_id, reason, created_at, actor):
    # B1 and B2 share one generic queue-failure authority in experiment_search_service.
    ESS.fail_claimed_job(conn, job_id=job_id, reason=reason,
                         created_at=created_at, actor=actor)


def run_candidate_pit_validation(conn, *, job_id, validation_repository, session_calendar,
                                 market_archive_repository, universe_archive_repository,
                                 tradability_repository, dataset_manifest, samples,
                                 created_at, actor=None, financial_feature_repository=None):
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateExperimentUnavailable("canonical_validation_repository_required")
    if conn.in_transaction or validation_repository.conn.in_transaction:
        raise CandidateExperimentUnavailable("search_write_transaction_must_be_closed")
    events = ESR.list_job_events(conn, job_id)
    if not events or events[-1]["event_kind"] != "claimed":
        raise CandidateExperimentUnavailable("search_job_must_be_claimed")
    try:
        prepared = prepare_candidate_experiment(conn, job_id=job_id, session_calendar=session_calendar,
            universe_archive_repository=universe_archive_repository, tradability_repository=tradability_repository)
        plan = prepared["plan"]
        output = RUNNER.run_validation(prepared["spec"], runner_code_revision=plan.code_revision,
            strategy_candidate=prepared["candidate"], dataset_manifest=dataset_manifest, samples=samples,
            walk_forward_config=plan.walk_forward_config, session_calendar=session_calendar,
            market_archive_repository=market_archive_repository, market_archive_fingerprint=plan.market_archive_fingerprint,
            universe_archive_repository=universe_archive_repository,
            universe_archive_fingerprint=plan.universe_archive_fingerprint, tradability_repository=tradability_repository,
            financial_feature_repository=financial_feature_repository,
            financial_archive_fingerprint=plan.financial_archive_fingerprint,
            tradability_replay_capture=prepared["tradability_replay_capture"],
            validation_repository=validation_repository, created_at=created_at)
    except Exception:
        _fail_job(conn, job_id=job_id, reason="candidate_pit_executor_failure", created_at=created_at, actor=actor)
        raise
    if output.get("run_key") is not None:
        output["completion"] = complete_candidate_pit_job(conn, job_id=job_id, run_key=output["run_key"],
            validation_repository=validation_repository, created_at=created_at, actor=actor)
    else:
        _fail_job(conn, job_id=job_id, reason=output["result"]["failure_reason"], created_at=created_at, actor=actor)
    return output
