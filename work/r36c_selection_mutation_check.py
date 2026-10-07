"""Reversible semantic M-SEL checks: compile mutants, run real detectors, restore bytes."""
from __future__ import annotations
import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = ("test_r36c_candidate_selection",)

CS = "backend/candidate_selection.py"
CR = "backend/candidate_selection_repository.py"
CV = "backend/candidate_selection_service.py"
SC = "backend/experiment_search_contract.py"
SS = "backend/experiment_search_service.py"
PSM = "backend/paper_schema_migrations.py"
CR2 = "backend/candidate_robustness_service.py"


def case(number, semantic, edits, detector, cls="PureEvaluationTests"):
    return {"id": f"M-SEL{number}", "semantic": semantic, "edits": edits,
            "detectors": [f"test_r36c_candidate_selection.{cls}.{detector}"]}


MUTATIONS = [
    case(1, "selection policy removed from search identity",
         [(SC, '            material.update(selection_policy=self.selection_policy.projection(),\n                            selection_policy_fingerprint=self.selection_policy.fingerprint)',
               '            material.update(selection_policy_fingerprint=self.selection_policy.fingerprint)')],
         "test_sel05_v3_binds_selection_policy", "ContractV3Tests"),
    case(2, "selection policy contaminates the experiment plan identity",
         [(SC, '        value = self.plan.projection()\n        value["plan_contract_version"] = EXPERIMENT_PLAN_CONTRACT_VERSION_V2',
               '        value = self.plan.projection()\n        value["plan_contract_version"] = EXPERIMENT_PLAN_CONTRACT_VERSION_V2\n        value["selection_leak"] = self.robustness_policy.fingerprint')],
         "test_sel06b_plan_v2_projection_has_no_selection_leak", "ContractV3Tests"),
    case(3, "search v3 rejected by B2 robustness",
         [(CR2, '    if spec.search_contract_version not in _ROBUSTNESS_EXECUTABLE_SEARCH_CONTRACTS:',
                '    if spec.search_contract_version != ESC.SEARCH_CONTRACT_VERSION_V2:')],
         "test_sel08_v3_still_executes_b1_b2_and_selects", "SelectionServiceTests"),
    case(4, "selection falls back to a recent R29 run",
         [(CV, '        run = validation_repository.get_run(run_key=run_key)\n        if run is None or run.get("run_key") != run_key:\n            raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")',
               '        runs = validation_repository.recent_runs()\n        run = next((item for item in runs if item.get("run_key") == run_key), None)\n        if run is None:\n            raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")')],
         "test_sel04_selection_uses_exact_r29_run_key", "SelectionServiceTests"),
    case(5, "selection falls back to a recent R30 report",
         [(CV, '        report = robustness_repository.get_report_by_key(report_key)\n        if report is None or report.get("report_key") != report_key:\n            raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")',
               '        reports = robustness_repository.recent_reports()\n        report = next((item for item in reports if item.get("report_key") == report_key), None)\n        if report is None:\n            raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")')],
         "test_sel05_selection_uses_exact_r30_report_key", "SelectionServiceTests"),
    case(6, "operational PIT failure becomes elimination",
         [(CV, '    if state != "completed":\n        # failed / queued / claimed / cancelled are operational states, never performance.\n        raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")',
               '    if state is None:\n        raise CandidateSelectionUnavailable("selection_operational_evidence_incomplete")')],
         "test_sel11_operational_pit_failure_blocks_selection", "SelectionServiceTests"),
    case(7, "operational R30 failure becomes elimination",
         [(CV, '    if state != "completed":\n        raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")',
               '    if state is None:\n        raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")')],
         "test_sel14_operational_robustness_failure_blocks_selection", "SelectionServiceTests"),
    case(8, "R29 blocked candidate incorrectly requires R30",
         [(CV, '        if not ready:\n            # Canonical R29 blocked / not-completed is real evidence: ineligible, no R30.\n            evidence.append(CS.CandidateSelectionEvidence(**common))\n            continue',
               '        if not ready:\n            _exact_robustness_binding(conn, search_run_id=search_run_id, candidate_id=candidate_id)\n            evidence.append(CS.CandidateSelectionEvidence(**common))\n            continue')],
         "test_sel12_canonical_blocked_pit_is_selectable_as_ineligible", "SelectionServiceTests"),
    case(9, "READY R29 candidate can be selected without R30",
         [(CV, '    job = _find_job(conn, search_run_id=search_run_id, candidate_id=candidate_id,\n                    stage=ESC.JOB_STAGE_ROBUSTNESS)\n    if job is None:\n        raise CandidateSelectionUnavailable("selection_robustness_stage_incomplete")',
               '    job = _find_job(conn, search_run_id=search_run_id, candidate_id=candidate_id,\n                    stage=ESC.JOB_STAGE_ROBUSTNESS)\n    if job is None:\n        return None, None')],
         "test_sel13_ready_without_robustness_job_blocks_selection", "SelectionServiceTests"),
    case(10, "another candidate robustness report is accepted",
         [(CV, '            RRUN.verify_candidate_robustness_report(\n                report=report["report"], baseline_run=run, spec=candidate_spec,\n                plan=robustness_plan, candidate_replay=replay)',
               '            pass')],
         "test_sel15_foreign_robustness_report_rejected", "SelectionServiceTests"),
    case(11, "missing metric coerced to zero",
         [(CS, '        value = getattr(evidence, feature_name)\n        if value is None:\n            reasons.append("selection_metric_unavailable")\n            continue',
               '        value = getattr(evidence, feature_name)\n        if value is None:\n            value = 0')],
         "test_sel16_missing_metric_is_not_zero"),
    case(12, "objective direction inverted",
         [(CS, '    "baseline_return": MAXIMIZE,', '    "baseline_return": MINIMIZE,')],
         "test_sel19_fixed_objective_directions"),
    case(13, "Pareto requires strict improvement on every objective",
         [(CS, '            if left < right:\n                return False\n            if left > right:\n                strictly_better = True',
               '            if left < right:\n                return False\n            if left > right:\n                pass')],
         "test_sel20_pareto_simple_dominance"),
    case(14, "candidate_id order becomes semantic ranking",
         [(CS, '        for candidate in front:\n            fronts[candidate.candidate_id] = level',
               '        for rank, candidate in enumerate(front):\n            fronts[candidate.candidate_id] = level + rank')],
         "test_sel22_exact_ties_same_front"),
    case(15, "ineligible candidate enters the Pareto set",
         [(CS, '    eligible = [item for item in rows if not eligibility[item.candidate_id]]',
               '    eligible = list(rows)'),
          (CS, '        if blocking:\n            front = None', '        if False:\n            front = None')],
         "test_sel25_ineligible_has_no_front"),
    case(16, "advance/retain fronts ignored",
         [(CS, '    if front <= policy.advance_through_front:\n        return "advance", True',
               '    if front <= 0:\n        return "advance", True')],
         "test_sel24_advance_retain_eliminate"),
    case(17, "weighted scalar score introduced as selection authority",
         [(CS, '        "candidates": candidates,\n    }', '        "candidates": candidates,\n        "weighted_score": 0.6 * (rows[0].baseline_return or 0),\n    }')],
         "test_sel26b_no_winner_tokens_as_fields", "ArchitectureTests"),
    case(18, "evidence set omitted from report identity",
         [(CS, '        "evidence_set_fingerprint": evidence_set_fingerprint,', '        "evidence_set_fingerprint": "0" * 64,')],
         "test_sel27_evidence_set_fingerprint_exact"),
    case(19, "same search accepts a conflicting second report",
         [(CR, '        if (decoded["payload_fingerprint"] != payload["payload_fingerprint"]\n                or decoded["selection_report_key"] != payload["selection_report_key"]):\n            raise SelectionPersistenceError("selection_report_conflict")',
               '        if False:\n            raise SelectionPersistenceError("selection_report_conflict")')],
         "test_sel32_same_search_different_report_conflict", "RepositoryTests"),
    case(20, "selection repository becomes mutable",
         [(PSM, '    for action in ("UPDATE", "DELETE"):', '    for action in ():')],
         "test_sel29_append_only", "RepositoryTests"),
    case(25, "enabled robustness gate fails open when the count is unavailable",
         [(CS, '        count = getattr(evidence, feature_name)\n        if count is None:\n            reasons.append("selection_metric_unavailable")\n        elif count > 0:\n            reasons.append(reason)',
               '        count = getattr(evidence, feature_name)\n        if (count or 0) > 0:\n            reasons.append(reason)')],
         "test_enabled_gate_with_none_count_is_ineligible", "FailClosedGateTests"),
    case(26, "ledger accepts candidates that do not match the bound evidence",
         [(CR, '    report_ids = sorted(item["candidate_id"] for item in candidates)\n    binding_ids = sorted(item["candidate_id"] for item in record["evidence_binding"])\n    if report_ids != binding_ids or len(set(report_ids)) != len(report_ids):\n        raise SelectionPersistenceError("corrupt_selection_report")',
               '    report_ids = sorted(item["candidate_id"] for item in candidates)\n    binding_ids = sorted(item["candidate_id"] for item in record["evidence_binding"])\n    if False:\n        raise SelectionPersistenceError("corrupt_selection_report")')],
         "test_candidates_must_match_evidence_binding", "LedgerCrossCheckTests"),
    case(27, "v3 search accepts a bare v1 experiment plan",
         [(SC, '            if not isinstance(self.experiment_plan, ExperimentSearchPlanV2):\n                raise SearchContractError("search_run_robustness_policy_unavailable")', '            pass')],
         "test_sel03c_v3_requires_robustness_plan", "ContractV3Tests"),
    case(21, "selection writes the candidate ledger",
         [(CV, '    evidence = _collect_evidence(conn, run=run, spec=spec,\n                                 validation_repository=validation_repository,\n                                 robustness_repository=robustness_repository)',
               '    conn.execute("UPDATE strategy_candidates SET candidate_json=candidate_json")\n    evidence = _collect_evidence(conn, run=run, spec=spec,\n                                 validation_repository=validation_repository,\n                                 robustness_repository=robustness_repository)')],
         "test_sel36_no_promotion_or_lifecycle_writes", "SelectionServiceTests"),
    case(22, "selection writes lifecycle/promotion",
         [(CV, 'import candidate_selection as CS\n', 'import candidate_selection as CS\nimport strategy_promotion as _SP\n')],
         "test_sel36b_no_promotion_imports", "ArchitectureTests"),
    case(23, "concurrent identical selection creates duplicate/conflict",
         [(CR, '            if winner is not None:\n                return self._resolve_existing(winner, payload)\n            raise SelectionPersistenceError("selection_report_conflict") from exc',
               '            raise SelectionPersistenceError("selection_report_conflict") from exc')],
         "test_sel23b_concurrent_identical_append_is_idempotent", "RepositoryTests"),
    case(24, "fresh bootstrap omits the selection table",
         [(PSM, '    existed = bool(table_columns(conn, "experiment_search_selection_reports"))\n    conn.execute(experiment_search_selection_report_ddl())',
                '    existed = bool(table_columns(conn, "experiment_search_selection_reports"))\n    return {"experiment_search_selection_reports": "skipped"}\n    conn.execute(experiment_search_selection_report_ddl())')],
         "test_sel34b_bootstrap_ddl_matches_migration_ddl", "MigrationTests"),
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
            mutated = {path: original[path].decode("utf-8").replace("\r\n", "\n")
                       for path in touched}
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
    restored = all(hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path]
                   for path in paths)
    final = run(*BASELINE)
    print(f"M-SEL detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and final.returncode == 0 and restored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
