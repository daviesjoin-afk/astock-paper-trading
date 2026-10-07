"""Reversible semantic M-CEX checks: compile mutants, run real detectors, restore exact bytes."""
from __future__ import annotations
import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = ("test_r36b1_candidate_experiment_execution",)
C = "backend/experiment_contract.py"
R = "backend/candidate_experiment.py"
P = "backend/experiment_pit_validation.py"
E = "backend/experiment_execution_model.py"
S = "backend/experiment_search_contract.py"
D = "backend/experiment_search_repository.py"
V = "backend/experiment_validation_repository.py"
A = "backend/candidate_experiment_service.py"

def case(number, semantic, edits, detector, cls="CandidateContractTests"):
    return {"id": f"M-CEX{number}", "semantic": semantic, "edits": edits,
            "detectors": [f"test_r36b1_candidate_experiment_execution.{cls}.{detector}"]}

search_source = (ROOT / D).read_text(encoding="utf-8")
start = search_source.index("    try:\n        spec = ESC.search_spec_from_projection")
stop = search_source.index("    return {", start)
search_verification = search_source[start:stop]
completion_source = (ROOT / A).read_text(encoding="utf-8")
start = completion_source.index('    if (run["subject_kind"]')
stop = completion_source.index('    conn.execute("BEGIN IMMEDIATE")', start)
completion_verification = completion_source[start:stop]

MUTATIONS = [
 case(1, "legacy identity drift", [(C, '"random_seed": self.random_seed,', '"random_seed": self.random_seed + 1,')], "test_cex01_legacy_identity_frozen"),
 case(2, "candidate content omitted from experiment subject", [(C, '"subject": self.subject.projection(),', '"subject": {"kind": "strategy_candidate", "parent_strategy": self.subject.parent_strategy.projection()},')], "test_cex02_candidate_identity"),
 case(3, "factor ignored", [(R, 'return bool(entry and factor), bool(exit_signal)', 'return bool(entry), bool(exit_signal)')], "test_cex05_factor_gates_entry"),
 case(4, "explicit exit ignored", [(R, 'exit_signal = (EVAL.evaluate(p["exit_ast"], snapshot)\n                       if p["exit_ast"] is not None else not entry)', 'exit_signal = not entry')], "test_cex07_explicit_exit_owns_exit"),
 case(5, "dependencies include only entry", [(P, 'if name in ("entry_ast", "factor_ast", "exit_ast") and ast is not None]', 'if name == "entry_ast" and ast is not None]')], "test_cex09_dependency_union"),
 case(6, "historical symbol filter ignored", [(R, 'for session, rows in members.items()}\n\n\ndef validate_candidate_asof', 'for session, rows in members.items()}\n\n\ndef validate_candidate_asof')], "test_cex10_explicit_symbols_and_pinned_universe"),
 case(7, "future candidate cutoff accepted", [(R, 'instant is None or end_date > candidate.asof\n            or instant.astimezone(PIT.china_tz()).date().isoformat() > candidate.asof', 'False')], "test_cex12_asof_leakage"),
 case(8, "candidate constraints ignored", [(E, 'constraints = candidate_replay.candidate.constraints if candidate_replay is not None else {}', 'constraints = {}')], "test_cex13_constraints"),
 case(9, "plan omitted from search identity", [(S, 'material.update(experiment_plan=self.experiment_plan.projection(),\n                            experiment_plan_fingerprint=self.experiment_plan.fingerprint)', 'pass')], "test_cex15_plan_facts_change_search_identity"),
 case(10, "corrupt search plan accepted", [(D, search_verification, '')], "test_corrupt_plan_with_rehashed_storage_payload_rejected", "CandidateExecutionTests"),
 case(11, "candidate stored as formal parent", [(V, 'extra = {"subject_kind": "strategy_candidate", "subject_json": _canonical(normalized_subject),', 'extra = {"subject_kind": "strategy_version", "subject_json": _canonical(normalized_subject),')], "test_cex18_subject_columns_and_formal_views", "CandidateExecutionTests"),
 case(12, "unpersisted run key completes queue", [(A, '    if conn.in_transaction:\n', '    ESR.record_verified_completion_event(conn, job_id=job_id, search_run_id=ESR.list_job_events(conn, job_id)[-1]["search_run_id"], run_key=run_key)\n    return {}\n    if conn.in_transaction:\n')], "test_cex23_fake_completion_rejected", "CandidateExecutionTests"),
 case(13, "blocked evaluation becomes failed queue work", [(A, '    if output.get("run_key") is not None:', '    if output["validation_evidence"]["status"] == "blocked":\n        ESS.record_job_event(conn, job_id=job_id, event_kind="failed", reason="blocked_evaluation")\n        return output\n    if output.get("run_key") is not None:')], "test_cex24_blocked_is_operationally_completed", "CandidateExecutionTests"),
 case(14, "ordinary completed path reopened", [(D, '    if event_kind == "completed":\n        raise ExperimentSearchRepositoryError("completion_evidence_binding_unavailable")\n', '')], "test_cex23_fake_completion_rejected", "CandidateExecutionTests"),
 case(15, "completion trusts another candidate evidence", [(A, completion_verification, '')], "test_other_candidate_run_cannot_complete", "CandidateExecutionTests"),
 case(16, "candidate spec accepts an external formal AST", [(E, '    if isinstance(spec, EC.CandidateExperimentSpec) and candidate_replay is None:\n        raise ExperimentExecutionUnavailable("candidate_replay_definition_required")\n', '')], "test_cex04_compiler_rejects_tampering_and_ast_substitution"),
 case(17, "universe exclusion manufactures a strategy exit", [(E, '            elif rows:\n                signals[code], exits[code] = candidate_replay.signals(snapshot)\n                signals[code] = signals[code] and code in next_members\n            else:\n                signals[code], exits[code] = False, False', '            elif rows and code in next_members:\n                signals[code], exits[code] = candidate_replay.signals(snapshot)\n            else:\n                signals[code], exits[code] = False, True')], "test_explicit_exit_is_not_replaced_by_universe_exclusion"),
]
# Filter removal is an actual production semantic mutation, with a unique full anchor.
MUTATIONS[5]["edits"] = [(R, 'return {session: [row for row in rows if str(row["code"]).split(".")[0] in symbols]\n            for session, rows in members.items()}', 'return {session: list(rows) for session, rows in members.items()}')]


def run(*selectors):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *selectors, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600, env=env)


def main() -> int:
    paths = sorted({ROOT / edit[0] for case in MUTATIONS for edit in case["edits"]})
    original = {path: path.read_bytes() for path in paths}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in original.items()}
    baseline = run(*BASELINE)
    if baseline.returncode:
        print("baseline = RED")
        print((baseline.stdout + baseline.stderr)[-4000:])
        return 1

    detected = survived = fake = timeout = 0
    modified_paths: set[Path] = set()
    try:
        for case in MUTATIONS:
            touched = {ROOT / edit[0] for edit in case["edits"]}
            mutated = {path: original[path].decode("utf-8").replace("\r\n", "\n") for path in touched}
            for relative, before, after in case["edits"]:
                path = ROOT / relative
                count = mutated[path].count(before)
                if count != 1:
                    fake += 1
                    print(f"{case['id']} FAKE anchor_count={count} in {relative}")
                    break
                mutated[path] = mutated[path].replace(before, after, 1)
            else:
                for path, source in mutated.items():
                    ast.parse(source, filename=str(path))
                    path.write_text(source, encoding="utf-8", newline="")
                    modified_paths.add(path)
                try:
                    result = run(*case["detectors"])
                except subprocess.TimeoutExpired:
                    timeout += 1
                    print(f"{case['id']} TIMEOUT")
                else:
                    if result.returncode:
                        detected += 1
                        print(f"{case['id']} DETECTED ({case['semantic']})")
                    else:
                        survived += 1
                        print(f"{case['id']} SURVIVED ({case['semantic']})")
                        print((result.stdout + result.stderr)[-1500:])
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
    print(f"M-CEX detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and final.returncode == 0 and restored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
