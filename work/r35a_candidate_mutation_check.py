#!/usr/bin/env python3
"""Reversible R35-A semantic mutations.

Each mutation breaks exactly one candidate-contract boundary and runs the focused
regression that owns it. Source bytes are restored after every case and
SHA256-checked at the end, and the focused baseline is re-run after restore.

Design note: the anchors are semantic (they change a real boundary), not
cosmetic. A mutation that no test can detect would mean the contract is only
documented, not enforced.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BASELINE = (
    "test_r35a_strategy_candidate.CandidateIdentityTests.test_c1_same_canonical_specification_yields_the_same_fingerprint",
    "test_r35a_strategy_candidate.CandidateIdentityTests.test_c2_parameter_mutation_changes_the_fingerprint",
    "test_r35a_strategy_candidate.CandidateIdentityTests.test_c2b_entry_factor_exit_and_parent_checksum_mutations_all_move_identity",
    "test_r35a_strategy_candidate.CandidateIdentityTests.test_c2c_identity_material_carries_no_evaluation_result",
    "test_r35a_strategy_candidate.ExecutablePayloadTests.test_c4_python_source_eval_exec_and_shell_payloads_are_rejected",
    "test_r35a_strategy_candidate.ExecutablePayloadTests.test_c4b_dynamic_field_and_attribute_access_is_rejected",
    "test_r35a_strategy_candidate.ExecutablePayloadTests.test_c4d_unknown_ops_and_oversized_asts_are_rejected",
    "test_r35a_strategy_candidate.MissingProvenanceTests.test_c5_missing_parent_version_or_checksum_is_rejected",
    "test_r35a_strategy_candidate.MissingProvenanceTests.test_c5b_missing_generator_version_asof_and_entry_are_rejected",
    "test_r35a_strategy_candidate.MissingProvenanceTests.test_c5e_undeclared_or_out_of_contract_parameters_are_rejected",
    "test_r35a_strategy_candidate.LedgerTests.test_c6_same_candidate_is_not_duplicated",
    "test_r35a_strategy_candidate.LedgerTests.test_c6b_dedup_keeps_the_proposal_source_evidence",
    "test_r35a_strategy_candidate.LedgerTests.test_c7_candidate_rows_cannot_be_updated_or_deleted",
    "test_r35a_strategy_candidate.LedgerTests.test_c8_persistence_round_trip_reverifies_the_fingerprint",
    "test_r35a_strategy_candidate.ParentPinningTests.test_c3_parent_upgrade_does_not_change_recorded_candidate_provenance",
    "test_r35a_strategy_candidate.ParentPinningTests.test_c10_current_registry_head_cannot_replace_a_stored_pin",
    "test_r35a_strategy_candidate.ParentPinningTests.test_c3b_pin_requires_the_exact_version_and_checksum",
    "test_r35a_strategy_candidate.ParentPinningTests.test_c3d_pinning_never_falls_back_to_the_registry_head",
    "test_r35a_strategy_candidate.ParentPinningTests.test_c3e_parent_metadata_constraints_are_inherited_not_dropped",
    "test_r35a_strategy_candidate.LedgerTests.test_c6d_every_proposal_occurrence_gets_its_own_identity",
    "test_r35a_strategy_candidate.LedgerTests."
    "test_c6e_proposal_event_identity_survives_process_local_identity_reset",
    "test_r35a_strategy_candidate.LedgerTests."
    "test_c6f_unexpected_proposal_id_collision_fails_closed",
    "test_r35a_strategy_candidate.LedgerTests."
    "test_c6g_proposal_identity_is_not_a_content_fingerprint",
    "test_r35a_strategy_candidate.CandidateIdentityTests.test_c2e_declared_parameter_contract_is_part_of_the_fingerprint",
    "test_r35a_strategy_candidate.AuthorityBoundaryTests.test_c9_generator_modules_have_no_promotion_or_execution_dependency",
)

CANDIDATE = "backend/strategy_candidate.py"
GENERATOR = "backend/strategy_generator.py"
SERVICE = "backend/strategy_candidate_service.py"
REPOSITORY = "backend/strategy_candidate_repository.py"
MIGRATIONS = "backend/paper_schema_migrations.py"

MUTATIONS = [
    # M-G1 —— candidate resolver 改成 current parent version。
    {"id": "M-G1",
     "semantic": "candidate provenance re-resolves the parent from the current head",
     "edits": [(SERVICE,
                'record = SR.get_version(str(strategy_id).strip(), int(strategy_version),\n'
                '                                checksum=str(strategy_checksum), conn=conn)',
                'record = SR.get_version(str(strategy_id).strip(), conn=conn)')],
     "detectors": [
         "test_r35a_strategy_candidate.ParentPinningTests."
         "test_c3_parent_upgrade_does_not_change_recorded_candidate_provenance",
         "test_r35a_strategy_candidate.ParentPinningTests."
         "test_c10_current_registry_head_cannot_replace_a_stored_pin",
         "test_r35a_strategy_candidate.ParentPinningTests."
         "test_c3d_pinning_never_falls_back_to_the_registry_head",
     ]},
    # M-G2 —— fingerprint 忽略 parameters（值同时从 entry_spec 与 parameter_spec 抹掉）。
    # 两处锚点缺一不可：候选 id 在 build 路径由局部 material 算出，而
    # verify_candidate_fingerprint 走 fingerprint_material()；只改一处会变成
    # "id 变了但自证失败"，那测的是另一件事。
    {"id": "M-G2",
     "semantic": "the canonical fingerprint ignores parameter values entirely",
     "edits": [
         (CANDIDATE,
          '        material.pop("candidate_id")\n'
          '        material.pop("candidate_fingerprint")\n'
          '        return material',
          '        material.pop("candidate_id")\n'
          '        material.pop("candidate_fingerprint")\n'
          '        material.pop("parameter_spec", None)\n'
          '        def _strip(value):\n'
          '            if isinstance(value, dict):\n'
          '                return {key: _strip(item) for key, item in value.items()\n'
          '                        if key != "value"}\n'
          '            if isinstance(value, list):\n'
          '                return [_strip(item) for item in value]\n'
          '            return value\n'
          '        material["entry_spec"] = _strip(material["entry_spec"])\n'
          '        return material'),
         (CANDIDATE,
          '    fingerprint = _sha(material)\n',
          '    material.pop("parameter_spec", None)\n'
          '    def _strip_material(value):\n'
          '        if isinstance(value, dict):\n'
          '            return {key: _strip_material(item) for key, item in value.items()\n'
          '                    if key != "value"}\n'
          '        if isinstance(value, list):\n'
          '            return [_strip_material(item) for item in value]\n'
          '        return value\n'
          '    material["entry_spec"] = _strip_material(material["entry_spec"])\n'
          '    fingerprint = _sha(material)\n'),
     ],
     "detectors": [
         "test_r35a_strategy_candidate.CandidateIdentityTests."
         "test_c2_parameter_mutation_changes_the_fingerprint",
         "test_r35a_strategy_candidate.CandidateIdentityTests."
         "test_c2e_declared_parameter_contract_is_part_of_the_fingerprint",
     ]},
    # M-G3 —— 每次生成随机 candidate ID（去重权威消失）。
    {"id": "M-G3",
     "semantic": "candidate identity becomes random instead of canonical",
     "edits": [(CANDIDATE,
                '    fingerprint = _sha(material)\n'
                '    return StrategyCandidate(',
                '    import uuid as _uuid\n'
                '    fingerprint = _uuid.uuid4().hex + _uuid.uuid4().hex[:32]\n'
                '    return StrategyCandidate(')],
     "detectors": [
         "test_r35a_strategy_candidate.CandidateIdentityTests."
         "test_c1_same_canonical_specification_yields_the_same_fingerprint",
         "test_r35a_strategy_candidate.LedgerTests.test_c6_same_candidate_is_not_duplicated",
     ]},
    # M-G4 —— 缺失 provenance 时回退到 latest。
    {"id": "M-G4",
     "semantic": "a missing parent checksum falls back to the current version",
     "edits": [(SERVICE,
                '    if not isinstance(strategy_checksum, str) or not SC._SHA256.fullmatch(strategy_checksum):\n'
                '        raise StrategyCandidateUnavailable("explicit_parent_strategy_checksum_required")',
                '    if not isinstance(strategy_checksum, str) or not SC._SHA256.fullmatch(strategy_checksum):\n'
                '        _head = SR.get_version(str(strategy_id).strip(), conn=conn)\n'
                '        strategy_checksum = _head.checksum if _head else ""')],
     "detectors": [
         "test_r35a_strategy_candidate.ParentPinningTests."
         "test_c3b_pin_requires_the_exact_version_and_checksum",
         "test_r35a_strategy_candidate.MissingProvenanceTests."
         "test_c5_missing_parent_version_or_checksum_is_rejected",
     ]},
    # M-G5 —— 允许任意可执行 payload 通过候选契约。
    {"id": "M-G5",
     "semantic": "arbitrary executable candidate payload is accepted",
     "edits": [(CANDIDATE,
                '    try:\n'
                '        normalized = DSL.normalize(ast)\n'
                '    except DSL.StrategyDslValidationError as exc:\n'
                '        raise CandidateValidationError(f"{role}_spec_rejected:{exc}") from exc',
                '    if isinstance(ast, Mapping) and ast.get("op") in ("python", "eval", "exec"):\n'
                '        return dict(ast)\n'
                '    try:\n'
                '        normalized = DSL.normalize(ast)\n'
                '    except DSL.StrategyDslValidationError as exc:\n'
                '        raise CandidateValidationError(f"{role}_spec_rejected:{exc}") from exc')],
     "detectors": [
         "test_r35a_strategy_candidate.ExecutablePayloadTests."
         "test_c4_python_source_eval_exec_and_shell_payloads_are_rejected",
     ]},
    # M-G6 —— 父策略约束被静默丢弃（候选不再是父策略语义）。
    {"id": "M-G6",
     "semantic": "a parameter-only variant silently drops the parent's constraints",
     "edits": [(SERVICE,
                '        constraints=(constraints if constraints is not None\n'
                '                     else metadata.get("constraints")),',
                '        constraints=None,')],
     "detectors": [
         "test_r35a_strategy_candidate.ParentPinningTests."
         "test_c3e_parent_metadata_constraints_are_inherited_not_dropped",
     ]},
    # M-G7 —— 提案身份退化成"内容 + 时间戳"（opaque 事件身份被换回内容指纹）。
    {"id": "M-G7",
     "semantic": "same-timestamp proposals collapse into one identity and lose an event",
     "edits": [(REPOSITORY,
                '    stamp, proposal_id = _proposal_event_identity(created_at)\n',
                '    stamp, _event_id = _proposal_event_identity(created_at)\n'
                '    proposal_id = SC._sha({"candidate_id": candidate.candidate_id,\n'
                '                           "proposal": json.loads(payload),\n'
                '                           "created_at": stamp})\n')],
     "detectors": [
         "test_r35a_strategy_candidate.LedgerTests."
         "test_c6d_every_proposal_occurrence_gets_its_own_identity",
         "test_r35a_strategy_candidate.LedgerTests."
         "test_c6e_proposal_event_identity_survives_process_local_identity_reset",
         "test_r35a_strategy_candidate.LedgerTests."
         "test_c6g_proposal_identity_is_not_a_content_fingerprint",
     ]},
    # M-G8 —— 事件表写回 INSERT OR IGNORE：id 碰撞被静默吞掉，第二次真实提案
    # 假装已经记录。必须由 fail-closed 回归（C6f）判为 RED。
    {"id": "M-G8",
     "semantic": "a proposal id collision is silently swallowed by INSERT OR IGNORE",
     "edits": [(REPOSITORY,
                '        """INSERT INTO strategy_candidate_proposals\n',
                '        """INSERT OR IGNORE INTO strategy_candidate_proposals\n')],
     "detectors": [
         "test_r35a_strategy_candidate.LedgerTests."
         "test_c6f_unexpected_proposal_id_collision_fails_closed",
     ]},
]


def run(*selectors):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run((sys.executable, "-m", "unittest", *selectors, "-q"),
                          cwd=BACKEND, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=env)


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
            mutated = {path: original[path].decode("utf-8") for path in touched}
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
    print(f"M-G detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and not survived and not fake and not timeout
                 and restored and final.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
