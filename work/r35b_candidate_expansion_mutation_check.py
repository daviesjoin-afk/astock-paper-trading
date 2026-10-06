#!/usr/bin/env python3
"""Reversible R35-B semantic mutations.

Each mutation breaks exactly one **expansion-contract** boundary and runs the focused
regression that owns it. Source bytes are restored after every case and SHA256-checked
at the end, and the focused baseline is re-run after restore.

Design note: the anchors are semantic (they change a real boundary), not cosmetic. A
mutation that no test can detect would mean the contract is only documented, not
enforced. Only mutations that map onto a real production boundary are listed — the
count is not a goal.
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
    "test_r35b_candidate_expansion.SearchSpaceDeterminismTests.test_b1_same_search_space_yields_the_same_candidate_set",
    "test_r35b_candidate_expansion.SearchSpaceDeterminismTests.test_b2_declaration_order_never_changes_the_candidate_set",
    "test_r35b_candidate_expansion.BoundedSpaceTests.test_b3_oversized_combination_space_is_rejected_not_truncated",
    "test_r35b_candidate_expansion.BoundedSpaceTests.test_b3b_cardinality_is_computable_before_expansion",
    "test_r35b_candidate_expansion.BoundedSpaceTests.test_b3c_declared_budget_can_only_tighten_the_contract_ceiling",
    "test_r35b_candidate_expansion.BoundedSpaceTests.test_b3d_a_dimension_the_capability_does_not_expand_must_be_singular",
    "test_r35b_candidate_expansion.VariantIdentityTests.test_b4_factor_variation_changes_identity",
    "test_r35b_candidate_expansion.VariantIdentityTests.test_b5_entry_variation_changes_identity",
    "test_r35b_candidate_expansion.VariantIdentityTests.test_b6_exit_variation_changes_identity",
    "test_r35b_candidate_expansion.InheritanceSemanticsTests.test_b7_inherited_parent_semantics_are_materialized_in_the_candidate",
    "test_r35b_candidate_expansion.InheritanceSemanticsTests.test_b7b_inherited_factor_and_exit_come_from_the_frozen_pin",
    "test_r35b_candidate_expansion.UnsafeMutationTests.test_b8_arbitrary_executable_payload_is_rejected",
    "test_r35b_candidate_expansion.CrossGeneratorDedupTests.test_b9_same_semantics_across_generators_dedup_to_one_candidate",
    "test_r35b_candidate_expansion.GenerationBatchTests.test_b10_batch_binds_the_frozen_generation_input",
    "test_r35b_candidate_expansion.GenerationBatchTests.test_b10b_generation_input_fingerprint_is_content_bound",
    "test_r35b_candidate_expansion.CrossGeneratorDedupTests.test_b11_batch_identity_is_not_candidate_identity",
    "test_r35b_candidate_expansion.AuthorityBoundaryTests.test_b12_generator_path_has_no_evaluation_promotion_or_execution_dependency",
    "test_r35b_candidate_expansion.AuthorityBoundaryTests.test_b12f_generator_dispatch_is_a_registry_not_a_branching_chain",
)

CANDIDATE = "backend/strategy_candidate.py"
GENERATOR = "backend/strategy_generator.py"
SEARCH_SPACE = "backend/strategy_candidate_search_space.py"
SERVICE = "backend/strategy_candidate_service.py"

MUTATIONS = [
    # M-B1 —— 静默截断：组合空间超限时只生成前 N 个；能力不展开的维度只取第一个
    # 取值。两者是同一类错误 —— candidate universe 会依赖遍历顺序。
    {"id": "M-B1",
     "semantic": "an oversized or partially-declared space is silently truncated",
     "edits": [(GENERATOR,
                '    if cardinality > search_space.max_candidates:\n'
                '        raise StrategyGeneratorError('
                '"candidate_space_exceeds_the_declared_maximum")\n',
                '    if cardinality > search_space.max_candidates:\n'
                '        cardinality = search_space.max_candidates\n'),
               (GENERATOR,
                '        if declared_arity != 1:\n'
                '            raise StrategyGeneratorError(\n'
                '                f"dimension_is_not_expanded_by_this_generator:{dimension}")\n',
                '        if declared_arity != 1:\n'
                '            pass\n'),
               (GENERATOR,
                '    return tuple(produced[key] for key in sorted(produced))\n',
                '    return tuple(produced[key] for key in sorted(produced))[\n'
                '        :search_space.max_candidates]\n')],
     "detectors": [
         "test_r35b_candidate_expansion.BoundedSpaceTests."
         "test_b3_oversized_combination_space_is_rejected_not_truncated",
         "test_r35b_candidate_expansion.BoundedSpaceTests."
         "test_b3b_cardinality_is_computable_before_expansion",
         "test_r35b_candidate_expansion.BoundedSpaceTests."
         "test_b3d_a_dimension_the_capability_does_not_expand_must_be_singular",
     ]},
    # M-B2 —— candidate fingerprint 忽略 factor（结构变体不再改变身份）。
    {"id": "M-B2",
     "semantic": "the canonical fingerprint ignores the factor slot",
     "edits": [(CANDIDATE,
                '        "factor_spec": factor,\n',
                '        "factor_spec": None,\n')],
     "detectors": [
         "test_r35b_candidate_expansion.VariantIdentityTests."
         "test_b4_factor_variation_changes_identity",
     ]},
    # M-B3 —— candidate fingerprint 忽略 exit。
    {"id": "M-B3",
     "semantic": "the canonical fingerprint ignores the exit slot",
     "edits": [(CANDIDATE,
                '        "exit_spec": exit_rule,\n',
                '        "exit_spec": None,\n')],
     "detectors": [
         "test_r35b_candidate_expansion.VariantIdentityTests."
         "test_b6_exit_variation_changes_identity",
     ]},
    # M-B4 —— inherit 时不再使用 pin 上冻结的 slot 语义（继承退化）。
    # 纯生成域**没有** registry 连接，所以真实的"回读 current"不可能发生；这里
    # 制造的是同一类越界：继承语义不再来自 exact pinned parent 的冻结事实。
    {"id": "M-B4",
     "semantic": "an inherited slot no longer resolves to the pinned parent's semantics",
     "edits": [(SEARCH_SPACE,
                '            slot, inherited = self.exit_slot, (\n'
                '                None if self.parent_pin.exit_spec is None\n'
                '                else _thaw(self.parent_pin.exit_spec))\n',
                '            slot, inherited = self.exit_slot, None\n')],
     "detectors": [
         "test_r35b_candidate_expansion.InheritanceSemanticsTests."
         "test_b7b_inherited_factor_and_exit_come_from_the_frozen_pin",
     ]},
    # M-B5 —— 不同 generator 的相同语义制造两个 candidate id（去重权威分裂）。
    {"id": "M-B5",
     "semantic": "different generators produce two candidate ids for one specification",
     "edits": [(CANDIDATE,
                '        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,\n'
                '        "parent_strategy_id": parent_id,\n',
                '        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,\n'
                '        "generator_contract_version": '
                'generator_contract_version,\n'
                '        "parent_strategy_id": parent_id,\n')],
     "detectors": [
         "test_r35b_candidate_expansion.CrossGeneratorDedupTests."
         "test_b9_same_semantics_across_generators_dedup_to_one_candidate",
     ]},
    # M-B6 —— generation input fingerprint 不再绑定 frozen input（parent pin +
    # search-space 指纹）。§20 要求 batch 能回答"这一批候选是从什么输入生成的"，
    # 丢掉这两项之后不同输入会得到同一个 input fingerprint。
    {"id": "M-B6",
     "semantic": "the generation input fingerprint stops binding the frozen input",
     "edits": [(SERVICE,
                '        "search_space_contract_version": '
                'search_space.search_space_contract_version,\n'
                '        "search_space_fingerprint": search_space.fingerprint,\n'
                '        "parent_pin": dict(search_space.parent_pin.identity),\n',
                '        "search_space_contract_version": '
                'search_space.search_space_contract_version,\n')],
     "detectors": [
         "test_r35b_candidate_expansion.GenerationBatchTests."
         "test_b10b_generation_input_fingerprint_is_content_bound",
         "test_r35b_candidate_expansion.GenerationBatchTests."
         "test_b10_batch_binds_the_frozen_generation_input",
     ]},
    # M-B7 —— 允许 arbitrary AST mutation 通过 slot 校验。
    {"id": "M-B7",
     "semantic": "an arbitrary AST is accepted as a slot alternative",
     "edits": [(SEARCH_SPACE,
                '        try:\n'
                '            normalized_ast = SC._rule_spec(ast, role, '
                'allow_strategy_root=(role == "entry"))\n'
                '        except SC.CandidateValidationError as exc:\n'
                '            raise SearchSpaceError(f"{role}_alternative_rejected:{exc}") '
                'from exc\n',
                '        if isinstance(ast, Mapping) and ast.get("op") in (\n'
                '                "python", "eval", "exec", "import", "call"):\n'
                '            normalized.append(_canonical(dict(ast)))\n'
                '            continue\n'
                '        try:\n'
                '            normalized_ast = SC._rule_spec(ast, role, '
                'allow_strategy_root=(role == "entry"))\n'
                '        except SC.CandidateValidationError as exc:\n'
                '            raise SearchSpaceError(f"{role}_alternative_rejected:{exc}") '
                'from exc\n')],
     "detectors": [
         "test_r35b_candidate_expansion.UnsafeMutationTests."
         "test_b8_arbitrary_executable_payload_is_rejected",
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
    print(f"M-B detected = {detected}/{len(MUTATIONS)}")
    print(f"survived = {survived}; fake = {fake}; timeout = {timeout}")
    print(f"restore SHA256 = {'PASS' if restored else 'FAIL'}")
    print(f"baseline after restore = {'GREEN' if final.returncode == 0 else 'RED'}")
    return 0 if (detected == len(MUTATIONS) and not survived and not fake and not timeout
                 and restored and final.returncode == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
