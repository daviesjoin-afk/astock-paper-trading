"""R36-B2 candidate robustness orchestration; existing R30 remains the only runner.

Chain:

    exact search run (plan-v2)
        -> exact READY R29 candidate baseline (from the PIT completed event)
        -> pinned RobustnessPolicy.bind(exact baseline) -> canonical RobustnessPlan
        -> existing robustness_runner
        -> canonical robustness report (existing robustness_repository)
        -> exact report_key -> robustness job completed

This module adds no selection authority: it never scores, ranks, selects or promotes.
"""
from __future__ import annotations

import candidate_experiment as CE
import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_search_service as ESS
import experiment_validation_repository as EVR
import experiment_validation_runner as R29
import robustness_repository as RREP
import robustness_runner as RRUN
import strategy_candidate_repository as SCR


class CandidateRobustnessUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _exact_search_plan_v2(conn, search_run_id):
    run = ESR.get_search_run(conn, search_run_id)
    if run is None:
        raise CandidateRobustnessUnavailable("search_run_not_found")
    spec = ESC.search_spec_from_projection(run["search_spec"])
    if spec.search_contract_version != ESC.SEARCH_CONTRACT_VERSION_V2:
        raise CandidateRobustnessUnavailable("search_run_robustness_policy_unavailable")
    if not isinstance(spec.experiment_plan, ESC.ExperimentSearchPlanV2):
        raise CandidateRobustnessUnavailable("search_run_robustness_policy_unavailable")
    plan = spec.experiment_plan
    # The policy fingerprint must self-verify against its own projection.
    if plan.robustness_policy.fingerprint != plan.robustness_policy_fingerprint:
        raise CandidateRobustnessUnavailable("robustness_policy_fingerprint_mismatch")
    return run, spec, plan


def _exact_ready_candidate_baseline(validation_repository, *, run_key, candidate_id, plan):
    """Re-read exact R29 evidence and return it only when it is READY + completed.

    A canonical blocked/unavailable/failed R29 baseline returns ``None``: that is an
    R30 prerequisite that is unavailable, not a corruption and not a candidate
    elimination (R36-C owns the meaning). Identity corruption still raises.
    """
    run = validation_repository.get_run(run_key=run_key)
    if run is None or run.get("run_key") != run_key:
        raise CandidateRobustnessUnavailable("baseline_evidence_not_found")
    if (run.get("subject_kind") != "strategy_candidate"
            or run.get("candidate_id") != candidate_id):
        raise CandidateRobustnessUnavailable("baseline_subject_not_this_candidate")
    if run.get("experiment_plan_fingerprint") != plan.fingerprint:
        raise CandidateRobustnessUnavailable("baseline_experiment_plan_mismatch")
    if run.get("runner_version") != R29.CANDIDATE_RUNNER_VERSION:
        raise CandidateRobustnessUnavailable("baseline_runner_version_unknown")
    if (run.get("validation_status") != "ready"
            or not isinstance(run.get("result"), dict)
            or run["result"].get("status") != "completed"):
        return None
    return run


def _completed_pit_baseline_run_keys(conn, search_run_id):
    """Exact ``evidence_id`` of each completed PIT job; never a latest/recent lookup."""
    result = {}
    for job in ESR.list_search_jobs(conn, search_run_id):
        if job["stage"] != ESC.JOB_STAGE_PIT_VALIDATION:
            continue
        events = ESR.list_job_events(conn, job["job_id"])
        if not events or events[-1]["event_kind"] != "completed":
            continue
        if events[-1]["evidence_owner"] != "experiment_validation_run":
            raise CandidateRobustnessUnavailable("corrupt_search_job")
        result[job["candidate_id"]] = events[-1]["evidence_id"]
    return result


def declare_candidate_robustness_jobs(conn, *, search_run_id, validation_repository,
                                      created_at=None):
    """Atomically declare one robustness job per eligible exact R29 baseline.

    Requires every PIT job to be terminal; re-reads each completed PIT job's exact
    ``evidence_id`` from the validation repository; binds the pinned policy to the exact
    baseline; and appends all jobs + queued events in one transaction (all or nothing).
    Idempotent for the same identity; a different baseline/plan for the same
    (search, candidate, robustness) is a hard conflict.
    """
    if conn.in_transaction:
        raise CandidateRobustnessUnavailable("search_write_transaction_must_be_closed")
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateRobustnessUnavailable("canonical_validation_repository_required")
    run, spec, plan = _exact_search_plan_v2(conn, search_run_id)
    # Acquire the write lock BEFORE reading existing jobs so two concurrent
    # declarations for the same search cannot both observe an empty set and then
    # collide on the unique (search_run_id, candidate_id, stage) constraint.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Stage barrier: every PIT job must be terminal (completed/cancelled) first.
        for job in ESR.list_search_jobs(conn, run["search_run_id"]):
            if job["stage"] != ESC.JOB_STAGE_PIT_VALIDATION:
                continue
            events = ESR.list_job_events(conn, job["job_id"])
            state = events[-1]["event_kind"] if events else None
            if not ESC.is_terminal_state(state):
                raise CandidateRobustnessUnavailable("pit_stage_not_terminal")
        existing = {}
        for job in ESR.list_search_jobs(conn, run["search_run_id"]):
            if job["stage"] == ESC.JOB_STAGE_ROBUSTNESS:
                existing[job["candidate_id"]] = job
        completed = _completed_pit_baseline_run_keys(conn, run["search_run_id"])
        declarations = []
        for candidate_id in spec.candidate_ids:
            run_key = completed.get(candidate_id)
            if run_key is None:
                continue  # cancelled PIT or no completed evidence: no robustness job.
            baseline = _exact_ready_candidate_baseline(
                validation_repository, run_key=run_key, candidate_id=candidate_id, plan=plan)
            if baseline is None:
                continue  # R30 prerequisite unavailable (blocked/unavailable/failed): no job.
            robustness_plan = plan.robustness_policy.bind(
                baseline["run_key"], baseline["experiment_fingerprint"])
            job = ESC.RobustnessSearchJobSpec(
                search_run_id=run["search_run_id"], candidate_id=candidate_id,
                baseline_run_key=baseline["run_key"],
                baseline_experiment_fingerprint=baseline["experiment_fingerprint"],
                robustness_policy_fingerprint=plan.robustness_policy.fingerprint,
                robustness_plan_fingerprint=robustness_plan.fingerprint)
            prior = existing.get(candidate_id)
            if prior is not None:
                if prior["job_id"] != job.job_id:
                    raise CandidateRobustnessUnavailable("robustness_job_identity_conflict")
                continue
            declarations.append(job)
        for job in declarations:
            ESR.record_job(conn, job=job, created_at=created_at)
            ESR.record_job_event(conn, job_id=job.job_id,
                                 search_run_id=run["search_run_id"],
                                 event_kind="queued", created_at=created_at)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"search_run_id": run["search_run_id"],
            "job_ids": [job.job_id for job in declarations],
            "jobs_created": len(declarations),
            "queued_events_created": len(declarations)}


def _robustness_job_inputs(conn, job_id):
    row = ESR.get_search_job(conn, job_id)
    if row is None:
        raise CandidateRobustnessUnavailable("search_job_not_found")
    if (row["stage"] != ESC.JOB_STAGE_ROBUSTNESS
            or row["job_contract_version"] != ESC.ROBUSTNESS_JOB_CONTRACT_VERSION):
        raise CandidateRobustnessUnavailable("corrupt_search_job")
    job = ESC.RobustnessSearchJobSpec(
        search_run_id=row["search_run_id"], candidate_id=row["candidate_id"],
        baseline_run_key=row["job"]["baseline_run_key"],
        baseline_experiment_fingerprint=row["job"]["baseline_experiment_fingerprint"],
        robustness_policy_fingerprint=row["job"]["robustness_policy_fingerprint"],
        robustness_plan_fingerprint=row["job"]["robustness_plan_fingerprint"])
    if job.job_id != job_id or job.projection() != row["job"]:
        raise CandidateRobustnessUnavailable("corrupt_search_job")
    run, spec, plan = _exact_search_plan_v2(conn, job.search_run_id)
    if job.candidate_id not in spec.candidate_ids:
        raise CandidateRobustnessUnavailable("corrupt_search_job")
    return job, plan, run


def prepare_candidate_robustness(conn, *, job_id, validation_repository):
    """Re-read exact job + search policy + R29 baseline and rebuild the exact plan."""
    job, plan, run = _robustness_job_inputs(conn, job_id)
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateRobustnessUnavailable("canonical_validation_repository_required")
    baseline = _exact_ready_candidate_baseline(
        validation_repository, run_key=job.baseline_run_key,
        candidate_id=job.candidate_id, plan=plan)
    if baseline["experiment_fingerprint"] != job.baseline_experiment_fingerprint:
        raise CandidateRobustnessUnavailable("baseline_experiment_fingerprint_mismatch")
    if plan.robustness_policy.fingerprint != job.robustness_policy_fingerprint:
        raise CandidateRobustnessUnavailable("robustness_policy_fingerprint_mismatch")
    robustness_plan = plan.robustness_policy.bind(
        baseline["run_key"], baseline["experiment_fingerprint"])
    if robustness_plan.fingerprint != job.robustness_plan_fingerprint:
        raise CandidateRobustnessUnavailable("robustness_plan_fingerprint_mismatch")
    candidate = SCR.get_candidate(conn, job.candidate_id)
    if candidate is None:
        raise CandidateRobustnessUnavailable("candidate_identity_mismatch")
    replay = CE.compile_candidate_replay(candidate)
    spec = CE.build_candidate_experiment_spec(
        candidate, replay, plan, baseline["tradability_evidence_fingerprint"])
    if spec.fingerprint != baseline["experiment_fingerprint"]:
        raise CandidateRobustnessUnavailable("baseline_experiment_fingerprint_mismatch")
    return {"job": job, "plan": plan, "robustness_plan": robustness_plan,
            "run": run, "baseline": baseline, "candidate": candidate, "replay": replay,
            "spec": spec}


def _executor_failure(conn, *, job_id, created_at, actor):
    ESS.fail_claimed_job(conn, job_id=job_id, reason="candidate_robustness_executor_failure",
                         created_at=created_at, actor=actor)


def run_candidate_robustness(conn, *, job_id, validation_repository, session_calendar,
                             market_archive_repository, universe_archive_repository,
                             tradability_repository, robustness_repository,
                             dataset_manifest, samples, created_at, actor=None,
                             financial_feature_repository=None, extended_session_calendar=None,
                             date_range_validation_context=None):
    """Claimed -> run existing R30 -> append canonical report -> verified completion."""
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateRobustnessUnavailable("canonical_validation_repository_required")
    if not isinstance(robustness_repository, RREP.RobustnessRepository):
        raise CandidateRobustnessUnavailable("canonical_robustness_repository_required")
    if conn.in_transaction or validation_repository.conn.in_transaction:
        raise CandidateRobustnessUnavailable("search_write_transaction_must_be_closed")
    events = ESR.list_job_events(conn, job_id)
    if not events or events[-1]["event_kind"] != "claimed":
        raise CandidateRobustnessUnavailable("search_job_must_be_claimed")
    try:
        prepared = prepare_candidate_robustness(conn, job_id=job_id,
                                                validation_repository=validation_repository)
        # Required financial fields come from the canonical replay union, never the caller.
        dependencies = R29.PV.replay_dsl_dependencies(prepared["replay"])
        required_fields = dependencies["financial_fields"]
        financial_by_pair = R29.build_replay_financial_features(
            spec=prepared["spec"], samples=samples, financial_fields=required_fields,
            financial_feature_repository=financial_feature_repository,
            financial_archive_fingerprint=prepared["plan"].financial_archive_fingerprint)
        report = RRUN.run_robustness(
            baseline_run=prepared["baseline"], spec=prepared["spec"],
            plan=prepared["robustness_plan"], candidate_replay=prepared["replay"],
            session_calendar=session_calendar,
            market_archive_repository=market_archive_repository,
            universe_archive_repository=universe_archive_repository,
            tradability_repository=tradability_repository,
            financial_features=financial_by_pair, required_financial_fields=required_fields,
            extended_session_calendar=extended_session_calendar,
            date_range_validation_context=date_range_validation_context,
            created_at=created_at)
        stored = robustness_repository.append_report(report)
    except Exception:
        _executor_failure(conn, job_id=job_id, created_at=created_at, actor=actor)
        raise
    output = {"report_key": stored["report_key"],
              "report_fingerprint": stored["report_fingerprint"],
              "report": stored["report"]}
    output["completion"] = complete_candidate_robustness_job(
        conn, job_id=job_id, report_key=stored["report_key"],
        validation_repository=validation_repository,
        robustness_repository=robustness_repository,
        created_at=created_at, actor=actor)
    return output


def complete_candidate_robustness_job(conn, *, job_id, report_key, validation_repository,
                                      robustness_repository, created_at=None, actor=None):
    """Independent re-read + rebind, then a short queue transaction. Crash-safe/idempotent."""
    if conn.in_transaction:
        raise CandidateRobustnessUnavailable("search_write_transaction_must_be_closed")
    if not isinstance(validation_repository, EVR.ExperimentValidationRepository):
        raise CandidateRobustnessUnavailable("canonical_validation_repository_required")
    if not isinstance(robustness_repository, RREP.RobustnessRepository):
        raise CandidateRobustnessUnavailable("canonical_robustness_repository_required")
    report = robustness_repository.get_report_by_key(report_key)
    if report is None or report["report_key"] != report_key:
        raise CandidateRobustnessUnavailable("robustness_report_not_found")
    job, plan, run = _robustness_job_inputs(conn, job_id)
    # Report <-> job exact identity.
    if job.stage != ESC.JOB_STAGE_ROBUSTNESS:
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    baseline_identity = report["report"].get("baseline_identity")
    if not isinstance(baseline_identity, dict):
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    subject = baseline_identity.get("experiment_subject")
    if (not isinstance(subject, dict) or subject.get("kind") != "strategy_candidate"
            or subject.get("candidate_id") != job.candidate_id):
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    if (job.baseline_run_key != report["baseline_run_key"]
            or job.baseline_experiment_fingerprint != report["baseline_experiment_fingerprint"]
            or job.robustness_plan_fingerprint != report["plan_fingerprint"]
            or report["report"].get("runner_version") != RRUN.CANDIDATE_RUNNER_VERSION
            or report["report"].get("baseline_result_fingerprint")
                != report["baseline_result_fingerprint"]):
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    # The embedded provenance must agree with the top-level report identity.
    if (baseline_identity.get("run_key") != report["baseline_run_key"]
            or baseline_identity.get("experiment_fingerprint")
                != report["baseline_experiment_fingerprint"]
            or baseline_identity.get("result_fingerprint")
                != report["baseline_result_fingerprint"]):
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    # Re-read the exact R29 baseline independently.
    baseline = _exact_ready_candidate_baseline(
        validation_repository, run_key=report["baseline_run_key"],
        candidate_id=job.candidate_id, plan=plan)
    if baseline is None:
        raise CandidateRobustnessUnavailable("baseline_validation_not_ready")
    if (baseline["experiment_fingerprint"] != report["baseline_experiment_fingerprint"]
            or baseline["result"].get("result_fingerprint")
                != report["baseline_result_fingerprint"]
            or baseline["run_key"] != report["baseline_run_key"]):
        raise CandidateRobustnessUnavailable("baseline_evidence_identity_mismatch")
    # The full embedded subject must equal the exact re-read R29 baseline subject;
    # a producer cannot swap the candidate fingerprint / parent pin / schema / replay
    # fingerprint and still complete this job.
    if subject != baseline.get("subject"):
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    baseline_spec = report["report"].get("baseline_spec")
    if (not isinstance(baseline_spec, dict)
            or baseline_spec.get("subject") != baseline.get("subject")):
        raise CandidateRobustnessUnavailable("robustness_report_identity_mismatch")
    # Re-bind the pinned policy and require all three plan fingerprints to agree.
    expected_plan = plan.robustness_policy.bind(
        baseline["run_key"], baseline["experiment_fingerprint"])
    if (expected_plan.fingerprint != job.robustness_plan_fingerprint
            or expected_plan.fingerprint != report["plan_fingerprint"]):
        raise CandidateRobustnessUnavailable("robustness_plan_fingerprint_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        events = ESR.list_job_events(conn, job_id)
        if not events:
            raise CandidateRobustnessUnavailable("search_job_must_be_claimed")
        latest = events[-1]
        if latest["event_kind"] == "completed":
            if (latest["evidence_owner"] != "robustness_report"
                    or latest["evidence_id"] != report_key):
                raise CandidateRobustnessUnavailable("completion_evidence_conflict")
        elif latest["event_kind"] == "claimed":
            ESR.record_verified_robustness_completion_event(
                conn, job_id=job_id, search_run_id=job.search_run_id,
                report_key=report_key, created_at=created_at, actor=actor)
        else:
            raise CandidateRobustnessUnavailable("search_job_must_be_claimed")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"job_id": job_id, "event_kind": "completed",
            "evidence_owner": "robustness_report", "evidence_id": report_key}
