"""Reversible semantic M-CRB checks: compile mutants, run real detectors, restore exact bytes."""
from __future__ import annotations
import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = ("test_r36b2_candidate_robustness_execution",)

RC = "backend/robustness_contract.py"
RR = "backend/robustness_runner.py"
SC = "backend/experiment_search_contract.py"
SR = "backend/experiment_search_repository.py"
SS = "backend/experiment_search_service.py"
CR = "backend/candidate_robustness_service.py"
CE = "backend/candidate_experiment.py"
SP = "backend/strategy_parameter_schema.py"
XR = "backend/experiment_validation_runner.py"
EC = "backend/experiment_contract.py"
RREP = "backend/robustness_repository.py"


def case(number, semantic, edits, detector, cls="ExecutionTests"):
    return {"id": f"M-CRB{number}", "semantic": semantic, "edits": edits,
            "detectors": [f"test_r36b2_candidate_robustness_execution.{cls}.{detector}"]}


MUTATIONS = [
    case(1, "candidate baseline incorrectly accepted as parent StrategyVersion",
         [(RR, '    if run.get("subject_kind") != "strategy_candidate":\n        raise RobustnessBaselineError("baseline_subject_kind_mismatch")\n', '')],
         "test_security_formal_run_cannot_be_candidate_baseline"),
    case(2, "R29 blocked baseline gets robustness job",
         [(CR, '        if baseline is None:\n            continue  # R30 prerequisite unavailable (blocked/unavailable/failed): no job.\n', '')],
         "test_crb08_blocked_pit_gets_no_robustness_job", "DeclarationTests"),
    case(3, "robustness policy removed from search identity",
         [(SC, '        value["robustness_policy"] = self.robustness_policy.projection()\n        value["robustness_policy_fingerprint"] = self.robustness_policy.fingerprint\n        return value',
               '        value["robustness_policy_fingerprint"] = self.robustness_policy.fingerprint\n        return value')],
         "test_crb05_plan_v2_search_identity", "PlanV2Tests"),
    case(4, "runtime replaces pinned robustness policy",
         [(RC, '    def bind(self, baseline_run_key: str,\n             baseline_experiment_fingerprint: str) -> RobustnessPlan:\n        """Bind one exact baseline identity; the plan keeps the existing authority."""\n        return RobustnessPlan(\n            baseline_run_key=baseline_run_key,\n            baseline_experiment_fingerprint=baseline_experiment_fingerprint,\n            random_seed=self.random_seed,',
               '    def bind(self, baseline_run_key: str,\n             baseline_experiment_fingerprint: str) -> RobustnessPlan:\n        """Bind one exact baseline identity; the plan keeps the existing authority."""\n        return RobustnessPlan(\n            baseline_run_key=baseline_run_key,\n            baseline_experiment_fingerprint=baseline_experiment_fingerprint,\n            random_seed=self.random_seed + 1,')],
         "test_crb03_policy_bind", "PolicyTests"),
    case(5, "stage barrier removed",
         [(CR, '        if not ESC.is_terminal_state(state):\n            raise CandidateRobustnessUnavailable("pit_stage_not_terminal")\n', '')],
         "test_crb07_stage_barrier", "DeclarationTests"),
    case(6, "robustness job drops baseline_run_key from identity",
         [(SC, '            "baseline_run_key": self.baseline_run_key,\n            "baseline_experiment_fingerprint": self.baseline_experiment_fingerprint,\n            "robustness_policy_fingerprint": self.robustness_policy_fingerprint,\n            "robustness_plan_fingerprint": self.robustness_plan_fingerprint,\n            "job_contract_version": self.job_contract_version,\n        }\n        return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()',
          '            "baseline_run_key": self.baseline_run_key,\n            "robustness_policy_fingerprint": self.robustness_policy_fingerprint,\n            "robustness_plan_fingerprint": self.robustness_plan_fingerprint,\n            "job_contract_version": self.job_contract_version,\n        }\n        return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()')],
         "test_robustness_job_id_binds_baseline_and_plan", "PlanV2Tests"),
    case(7, "candidate universe filter removed",
         [(CE, '    symbols = set(scope["symbols"])\n    # Archive codes may include exchange suffixes; canonical candidate symbols are six digits.\n    return {session: [row for row in rows if str(row["code"]).split(".")[0] in symbols]\n            for session, rows in members.items()}',
               '    symbols = set(scope["symbols"])\n    return {session: list(rows) for session, rows in members.items()}')],
         "test_crb16b_runner_applies_candidate_scope_before_stress"),
    case(8, "universe stress applied before candidate scope",
         [(CE, '    symbols = set(scope["symbols"])\n    # Archive codes may include exchange suffixes; canonical candidate symbols are six digits.\n    return {session: [row for row in rows if str(row["code"]).split(".")[0] in symbols]\n            for session, rows in members.items()}',
               '    symbols = set(scope["symbols"])\n    # Archive codes may include exchange suffixes; canonical candidate symbols are six digits.\n    return {session: list(rows) for session, rows in members.items()}')],
         "test_crb16_universe_perturbation_after_candidate_filter"),
    case(9, "candidate factor/exit ignored",
         [(CE, '        factor = p["factor_ast"] is None or EVAL.evaluate(p["factor_ast"], snapshot)\n        exit_signal = (EVAL.evaluate(p["exit_ast"], snapshot)\n                       if p["exit_ast"] is not None else not entry)',
               '        factor = True\n        exit_signal = not entry')],
         "test_crb17_factor_exit_retained"),
    case(10, "candidate constraints ignored",
         [("backend/experiment_execution_model.py", '    constraints = candidate_replay.candidate.constraints if candidate_replay is not None else {}', '    constraints = {}')],
         "test_crb18_constraints_retained"),
    case(11, "financial feature caller injection accepted",
         [(CR, '        required_fields = dependencies["financial_fields"]', '        required_fields = []')],
         "test_crb11c_financial_dependency_fails_closed"),
    case(12, "parameter stress creates/mutates canonical candidate",
         [(CE, '        return replace(self, entry_ast_override=_freeze(entry_ast))',
               '        from dataclasses import replace as _r\n        return replace(self.candidate, entry_spec=_freeze(entry_ast))')],
         "test_crb21_parameter_stress_does_not_create_candidate"),
    case(13, "date stress skips R29 PIT revalidation",
         [(RR, '            if changes_date_range:\n                if expands_baseline and extended_session_calendar is None:\n                    raise ValueError("date_expansion_owner_calendar_missing")',
               '            if False:\n                if expands_baseline and extended_session_calendar is None:\n                    raise ValueError("date_expansion_owner_calendar_missing")')],
         "test_crb26b_date_stress_runs_r29_through_runner"),
    case(14, "date stress crosses candidate asof",
         [(CE, '    if (instant is None or end_date > candidate.asof\n            or instant.astimezone(PIT.china_tz()).date().isoformat() > candidate.asof):\n        raise ValueError("candidate_asof_leakage")',
               '    if False:\n        raise ValueError("candidate_asof_leakage")')],
         "test_crb27_candidate_asof_cannot_be_crossed"),
    case(15, "fake robustness report completes job",
         [(SR, '    if not ESC.is_search_identity(report_key):\n        raise ExperimentSearchRepositoryError("invalid_robustness_report_key")\n', '')],
         "test_crb15b_invalid_report_key_primitive_rejects"),
    case(16, "other candidate report completes job",
         [(CR, '    report = robustness_repository.get_report_by_key(report_key)\n    if report is None or report["report_key"] != report_key:\n        raise CandidateRobustnessUnavailable("robustness_report_not_found")',
               '    report = robustness_repository.get_report_by_key(report_key)\n    if report is None:\n        recent = robustness_repository.recent_reports()\n        report = recent[0] if recent else None\n    if report is None:\n        raise CandidateRobustnessUnavailable("robustness_report_not_found")')],
         "test_crb30_31_fake_report_rejected"),
    case(17, "scenario unavailable maps queue failed",
         [(RR, '            result = RC.case_result(scenario_fingerprint=scenario["scenario_fingerprint"],\n                                    status="unavailable", metrics=None,\n                                    baseline_delta=None, reason_code=reason)',
               '            raise EM.ExperimentExecutionUnavailable(reason)')],
         "test_crb32_33_unavailable_or_failed_case_still_completed"),
    case(18, "infrastructure failure maps completed",
         [(CR, '    except Exception:\n        _executor_failure(conn, job_id=job_id, created_at=created_at, actor=actor)\n        raise', '    except Exception:\n        raise')],
         "test_crb34_infrastructure_failure_is_retryable"),
    case(19, "formal R30 fingerprint drifts",
         [(RR, '        "runner_version": runner_version, "baseline_identity": baseline_identity,',
               '        "runner_version": "r30-drifted", "baseline_identity": baseline_identity,')],
         "test_formal_r30_identity_frozen", "FormalRegressionTests"),
    case(21, "forged nested baseline provenance is accepted",
         [(RREP, '        if _sha(report["baseline_spec"]) != report["baseline_experiment_fingerprint"]:\n            raise RobustnessPersistenceError("corrupt_robustness_report")\n', '')],
         "test_crb15e_ledger_rejects_forged_baseline_spec"),
    case(22, "nested baseline identity need not match the top level",
         [(CR, '    if (baseline_identity.get("run_key") != report["baseline_run_key"]', '    if (False and baseline_identity.get("run_key") != report["baseline_run_key"]')],
         "test_crb15c_forged_nested_provenance_rejected"),
    case(20, "legacy plan-v1 silently receives default robustness policy",
         [(SC, '        plan = ExperimentSearchPlan(plan_contract_version=EXPERIMENT_PLAN_CONTRACT_VERSION, **args)\n        if plan.projection() != value:\n            raise ValueError("noncanonical plan")\n        return plan',
               '        plan = ExperimentSearchPlan(plan_contract_version=EXPERIMENT_PLAN_CONTRACT_VERSION, **args)\n        if plan.projection() != value:\n            raise ValueError("noncanonical plan")\n        return ExperimentSearchPlanV2.from_v1(\n            plan, RC.RobustnessPolicy(random_seed=0, regime_policy={\n                "policy_version": "r30-regime-v1", "benchmark_symbol": "000001.SH",\n                "trend_window_sessions": 2, "bull_threshold": 0.01, "bear_threshold": 0.01,\n                "volatility_window_sessions": 2, "high_vol_threshold": 0.02,\n                "low_vol_threshold": 0.005}))')],
         "test_crb04_plan_v1_identity_unchanged", "PlanV2Tests"),
]


def run(*selectors):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *selectors, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env)


def main() -> int:
    paths = sorted({ROOT / edit[0] for case_ in MUTATIONS for edit in case_["edits"]})
    original = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run(*BASELINE)
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-4000:])
        return 1
    detected = survived = fake = timeout = 0
    modified_paths = set()
    try:
        for case_ in MUTATIONS:
            touched = {ROOT / edit[0] for edit in case_["edits"]}
            mutated = {path: original[path].decode("utf-8").replace("\r\n", "\n") for path in touched}
            ok = True
            for relative, before, after in case_["edits"]:
                path = ROOT / relative
                count = mutated[path].count(before)
                if count != 1:
                    fake += 1
                    print(f"{case_['id']} FAKE anchor_count={count} in {relative}")
                    ok = False
                    break
                mutated[path] = mutated[path].replace(before, after, 1)
            if not ok:
                continue
            for path, source in mutated.items():
                ast.parse(source, filename=str(path))
                path.write_text(source, encoding="utf-8", newline="")
                modified_paths.add(path)
            try:
                result = run(*case_["detectors"])
            except subprocess.TimeoutExpired:
                timeout += 1
                print(f"{case_['id']} TIMEOUT")
            else:
                if result.returncode:
                    detected += 1
                    print(f"{case_['id']} DETECTED ({case_['semantic']})")
                else:
                    survived += 1
                    print(f"{case_['id']} SURVIVED ({case_['semantic']})")
                    print((result.stdout + result.stderr)[-1200:])
            for path in tuple(modified_paths):
                path.write_bytes(original[path])
                modified_paths.discard(path)
    finally:
        for path in tuple(modified_paths):
            path.write_bytes(original[path])
            modified_paths.discard(path)
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path] for path in paths)
    final = run(*BASELINE)
    print(f"M-CRB detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and final.returncode == 0 and restored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
