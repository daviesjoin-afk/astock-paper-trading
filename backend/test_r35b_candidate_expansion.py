# -*- coding: utf-8 -*-
"""R35-B contract regressions: deterministic candidate expansion.

Covers B1…B12 from the R35-B specification:

    B1  search space is deterministic (same space → same fingerprint set)
    B2  declaration order independence (JSON keys / parameter order / alternatives)
    B3  bounded space fails closed (never silently truncated)
    B4  factor variation changes identity
    B5  entry variation changes identity
    B6  exit variation changes identity
    B7  inherited parent semantics are preserved (no future current-parent lookup)
    B8  unsafe structural mutation is rejected
    B9  duplicate semantics across generators dedup (1 candidate row, 2 proposals)
    B10 generation batch provenance (one request → one batch → N proposals)
    B11 batch identity is not candidate identity
    B12 no evaluation / promotion / execution authority in the generator path

The suite is deliberately about the **expansion contract**, not about strategy
quality: nothing here asserts that any candidate is profitable, and the ledger has
no place to put such a claim.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import strategy_candidate as SC
import strategy_candidate_repository as SCRepo
import strategy_candidate_search_space as SS
import strategy_candidate_service as SCV
import strategy_dsl_schema as DSL
import strategy_generator as SG
import strategy_registry as SR

PARENT = "r35b_parent"
CHECKSUM_A = "a" * 64
CHECKSUM_B = "b" * 64


def _parameter(parameter_id, parameter_type, value, minimum, maximum, max_step, *,
               locked=False, risk_direction="neutral", min_evidence=10):
    return {
        "op": "parameter", "parameter_id": parameter_id, "type": parameter_type,
        "value": value, "min": minimum, "max": maximum, "max_step": max_step,
        "locked": locked, "risk_direction": risk_direction, "min_evidence": min_evidence,
    }


def _parent_rule(*, ma_value=20, ma_max=60, ma_step=2, rsi_value=55.0, rsi_step=2.0):
    """A synthetic parameterized parent definition (fixture, not a real strategy)."""
    ma = _parameter("ma_period", "integer", ma_value, 5, ma_max, ma_step,
                    risk_direction="lower_is_riskier")
    return {
        "op": "strategy",
        "rule": {
            "op": "and",
            "args": [
                {"op": "gt", "left": {"op": "field", "name": "close"},
                 "right": {"op": "indicator", "name": "ma", "window": ma}},
                {"op": "lt",
                 "left": {"op": "indicator", "name": "rsi", "window": 14},
                 "right": _parameter("rsi_threshold", "number", rsi_value, 30.0,
                                     80.0, rsi_step)},
            ],
        },
        "parameters": [],
    }


#: entry alternatives: all three are valid bounded DSL boolean expressions.
ENTRY_A = {"op": "gt", "left": {"op": "field", "name": "close"},
           "right": {"op": "const", "value": 10}}
ENTRY_B = {"op": "gt", "left": {"op": "field", "name": "volume"},
           "right": {"op": "const", "value": 1000}}
ENTRY_C = {"op": "gte", "left": {"op": "field", "name": "turnover_rate"},
           "right": {"op": "const", "value": 2}}
#: 一个**声明了可调参数**的 entry 备选：参数只允许落在真正会被使用的 entry 上，
#: 因此要用参数维度做基数测试时，entry 必须自己声明那些参数。
ENTRY_PARAM = {
    "op": "strategy",
    "rule": {"op": "gt", "left": {"op": "field", "name": "close"},
             "right": {"op": "indicator", "name": "ma",
                       "window": _parameter("ma_period", "integer", 20, 5, 60, 2,
                                            risk_direction="lower_is_riskier")}},
    "parameters": [_parameter("rsi_threshold", "number", 55.0, 30.0, 80.0, 2.0)],
}

FACTOR_A = {"op": "gt", "left": {"op": "field", "name": "pe"},
            "right": {"op": "const", "value": 30}}
FACTOR_B = {"op": "lt", "left": {"op": "field", "name": "pb"},
            "right": {"op": "const", "value": 3}}

EXIT_A = {"op": "lt", "left": {"op": "field", "name": "close"},
          "right": {"op": "const", "value": 9}}
EXIT_B = {"op": "cross_below", "left": {"op": "field", "name": "close"},
          "right": {"op": "indicator", "name": "ma", "window": 5}}


def _pin(*, rule=None, version=1, checksum=CHECKSUM_A, asof="2026-10-05",
         constraints=None, factor_spec=None, exit_spec=None):
    return SS.ParentStrategyPin(
        strategy_id=PARENT, strategy_version=version, strategy_checksum=checksum,
        dsl_ast=_parent_rule() if rule is None else rule, asof=asof,
        constraints=constraints, factor_spec=factor_spec, exit_spec=exit_spec)


def _space(*, pin=None, generator_type=SG.PARAMETER_VARIANT_GENERATOR,
           generator_version=SG.PARAMETER_VARIANT_VERSION, variants=None,
           factor_slot=None, entry_slot=None, exit_slot=None, asof="2026-10-05",
           max_candidates=SS.MAX_CANDIDATES_PER_GENERATION_REQUEST, **overrides):
    values = {
        "parent_pin": _pin() if pin is None else pin,
        "generator_type": generator_type,
        "generator_version": generator_version,
        "parameter_variants": {"ma_period": [19, 20, 21]} if variants is None else variants,
        "universe_spec": {"scope_kind": "a_share_all"},
        "intended_market_regime": "momentum",
        "asof": asof,
        "evidence_count": 10,
        "max_candidates": max_candidates,
    }
    if factor_slot is not None:
        values["factor_slot"] = factor_slot
    if entry_slot is not None:
        values["entry_slot"] = entry_slot
    if exit_slot is not None:
        values["exit_slot"] = exit_slot
    values.update(overrides)
    return SS.CandidateSearchSpace(**values)


def _explicit(alternatives):
    return {"kind": SS.EXPLICIT_VARIANT, "alternatives": alternatives}


def _ids(candidates):
    return {item.candidate_id for item in candidates}


class _LedgerFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self._dir.name, "paper.sqlite3"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.conn.close)
        import paper_schema_migrations as PSM
        PSM.ensure_strategy_candidates(self.conn)

    def _seed(self, *, metadata=None, rule=None, strategy_id=PARENT):
        return SR.create_user_definition(
            self.conn, strategy_id, "R35B Parent",
            dsl_ast=_parent_rule() if rule is None else rule,
            metadata=metadata, actor="test")

    def _generate(self, created, **overrides):
        values = {
            "strategy_id": PARENT, "strategy_version": 1,
            "strategy_checksum": created.current_checksum, "asof": "2026-10-05",
            "universe_spec": {"scope_kind": "a_share_all"},
            "intended_market_regime": "momentum", "evidence_count": 10,
        }
        values.update(overrides)
        return SCV.generate_and_record_candidates(self.conn, **values)


class SearchSpaceDeterminismTests(unittest.TestCase):
    """B1 / B2 —— 同一空间 → 同一 candidate 集合；声明顺序不承载语义。"""

    def test_b1_same_search_space_yields_the_same_candidate_set(self):
        first = SG.generate_candidates(_space())
        second = SG.generate_candidates(_space())
        self.assertEqual([item.candidate_id for item in first],
                         [item.candidate_id for item in second])
        self.assertEqual(len(first), 3)
        for candidate in first:
            self.assertTrue(SC.verify_candidate_fingerprint(candidate))
            # candidate_id **就是** canonical fingerprint。
            self.assertEqual(candidate.candidate_id, candidate.candidate_fingerprint)

    def test_b1b_search_space_fingerprint_is_canonical(self):
        self.assertEqual(_space().fingerprint, _space().fingerprint)
        # 空间语义变了，指纹必须变。
        self.assertNotEqual(_space().fingerprint, _space(asof="2026-10-06").fingerprint)
        self.assertNotEqual(_space().fingerprint,
                            _space(variants={"ma_period": [19, 20]}).fingerprint)
        # 预算不是空间语义：同一个空间配不同预算，展开集合相同、指纹相同。
        self.assertEqual(_space().fingerprint, _space(max_candidates=8).fingerprint)

    def test_b2_declaration_order_never_changes_the_candidate_set(self):
        # 参数声明顺序 / JSON key 顺序 / alternative 顺序全部打乱。
        # 显式 entry 备选会替换父策略结构，因此这一格不再声明参数变体（参数由
        # 那些 entry 自己声明才合法）。
        base = _space(variants={},
                      factor_slot=_explicit([FACTOR_A, FACTOR_B]),
                      entry_slot=_explicit([ENTRY_A, ENTRY_B, ENTRY_C]),
                      exit_slot=_explicit([EXIT_A, EXIT_B]),
                      generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
                      generator_version=SG.BOUNDED_COMBINATION_VERSION,
                      max_candidates=100)
        shuffled = _space(
            variants={},
            factor_slot=_explicit([FACTOR_B, FACTOR_A]),
            entry_slot=_explicit([ENTRY_C, ENTRY_A, ENTRY_B]),
            exit_slot=_explicit([EXIT_B, EXIT_A]),
            generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
            generator_version=SG.BOUNDED_COMBINATION_VERSION,
            max_candidates=100)
        self.assertEqual(_ids(SG.generate_candidates(base)),
                         _ids(SG.generate_candidates(shuffled)))
        self.assertEqual(base.fingerprint, shuffled.fingerprint)

    def test_b2c_parameter_declaration_order_never_changes_the_candidate_set(self):
        """参数维度内部的声明顺序同样不承载语义（值本身排序）。"""
        base = _space(variants={"ma_period": [19, 20, 21]})
        shuffled = _space(variants={"ma_period": [21, 19, 20]})
        self.assertEqual(_ids(SG.generate_candidates(base)),
                         _ids(SG.generate_candidates(shuffled)))
        self.assertEqual(base.fingerprint, shuffled.fingerprint)

    def test_b2b_reversed_mapping_keys_do_not_change_identity(self):
        space = _space(variants={}, entry_slot=_explicit([ENTRY_A]))
        reordered = _space(variants={}, entry_slot=_explicit([_reverse_keys(ENTRY_A)]))
        self.assertEqual(space.fingerprint, reordered.fingerprint)
        self.assertEqual(_ids(SG.generate_candidates(space)),
                         _ids(SG.generate_candidates(reordered)))


class BoundedSpaceTests(unittest.TestCase):
    """B3 —— 组合数超限 fail closed；绝不静默截断。"""

    def test_b3_oversized_combination_space_is_rejected_not_truncated(self):
        """超限必须**在生成前**被拒绝，而不是生成前 N 个再截断。

        这里刻意让每一个组合本身都是**合法**的（200 个合法 factor 备选），因此
        "截断"与"拒绝"会产生可观测的不同结果：截断会返回 128 个候选，而契约要求
        直接报错。用非法组合做反例是不合格的探测——那样两种实现都会抛异常。
        """
        factors = [{"op": "gt", "left": {"op": "field", "name": "pe"},
                    "right": {"op": "const", "value": value}}
                   for value in range(1, 201)]
        space = _space(
            variants={}, entry_slot=_explicit([ENTRY_A]),
            factor_slot=_explicit(factors),
            generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
            generator_version=SG.BOUNDED_COMBINATION_VERSION)
        self.assertEqual(200, space.cardinality)
        self.assertGreater(space.cardinality, SS.MAX_CANDIDATES_PER_GENERATION_REQUEST)
        with self.assertRaises(SG.StrategyGeneratorError):
            SG.generate_candidates(space)
        # 显式收紧预算同样必须拒绝（而不是只生成 50 个）。
        tight = _space(
            variants={}, entry_slot=_explicit([ENTRY_A]),
            factor_slot=_explicit(factors), max_candidates=50,
            generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
            generator_version=SG.BOUNDED_COMBINATION_VERSION)
        with self.assertRaises(SG.StrategyGeneratorError):
            SG.generate_candidates(tight)

    def test_b3b_cardinality_is_computable_before_expansion(self):
        """基数必须在生成**之前**就能算出来，否则"拒绝"只能靠跑一遍才知道。"""
        space = _space(variants={"ma_period": [19, 20], "rsi_threshold": [55.0, 57.0]},
                       factor_slot=_explicit([FACTOR_A, FACTOR_B]),
                       entry_slot=_explicit([ENTRY_PARAM]),
                       exit_slot=_explicit([EXIT_A, EXIT_B]),
                       generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
                       generator_version=SG.BOUNDED_COMBINATION_VERSION,
                       max_candidates=100)
        # 参数 2×2 = 4，factor 2，entry 1，exit 2 → 16。
        self.assertEqual(4, space.dimension_cardinality(SS.PARAMETER_DIMENSION))
        self.assertEqual(2, space.dimension_cardinality("factor"))
        self.assertEqual(1, space.dimension_cardinality("entry"))
        self.assertEqual(2, space.dimension_cardinality("exit"))
        self.assertEqual(16, space.cardinality)
        self.assertEqual(16, len(SG.generate_candidates(space)))

    def test_b3c_declared_budget_can_only_tighten_the_contract_ceiling(self):
        with self.assertRaises(SS.SearchSpaceError):
            _space(max_candidates=SS.MAX_CANDIDATES_PER_GENERATION_REQUEST + 1)
        with self.assertRaises(SG.StrategyGeneratorError):
            SG.generate_candidates(_space(
                variants={"ma_period": [19, 20, 21, 22, 23]}, max_candidates=3))

    def test_b3d_a_dimension_the_capability_does_not_expand_must_be_singular(self):
        """能力不展开的维度只能有一个取值。

        否则就是"声明了 3 个 entry 备选、却只用了第 1 个"的静默截断：candidate
        universe 会悄悄依赖遍历顺序。这与超限截断是同一类错误，必须 fail closed。
        """
        with self.assertRaises(SG.StrategyGeneratorError):
            SG.generate_candidates(_space(
                generator_type=SG.FACTOR_VARIANT_GENERATOR,
                generator_version=SG.FACTOR_VARIANT_VERSION,
                variants={}, factor_slot=_explicit([FACTOR_A]),
                entry_slot=_explicit([ENTRY_A, ENTRY_B, ENTRY_C])))
        with self.assertRaises(SG.StrategyGeneratorError):
            SG.generate_candidates(_space(
                generator_type=SG.ENTRY_VARIANT_GENERATOR,
                generator_version=SG.ENTRY_VARIANT_VERSION,
                variants={}, entry_slot=_explicit([ENTRY_A]),
                factor_slot=_explicit([FACTOR_A, FACTOR_B])))
        # 单一取值仍然合法。
        single = SG.generate_candidates(_space(
            generator_type=SG.FACTOR_VARIANT_GENERATOR,
            generator_version=SG.FACTOR_VARIANT_VERSION,
            variants={}, factor_slot=_explicit([FACTOR_A]),
            entry_slot=_explicit([ENTRY_A])))
        self.assertEqual(1, len(single))


class VariantIdentityTests(unittest.TestCase):
    """B4 / B5 / B6 —— 每个 slot 的变体都必须改变 candidate identity。"""

    def test_b4_factor_variation_changes_identity(self):
        candidates = SG.generate_candidates(_space(
            generator_type=SG.FACTOR_VARIANT_GENERATOR,
            generator_version=SG.FACTOR_VARIANT_VERSION,
            variants={}, factor_slot=_explicit([FACTOR_A, FACTOR_B])))
        self.assertEqual(2, len(candidates))
        self.assertEqual(2, len(_ids(candidates)))
        self.assertEqual({json.dumps(FACTOR_A, sort_keys=True),
                          json.dumps(FACTOR_B, sort_keys=True)},
                         {json.dumps(SC._thaw(item.factor_spec), sort_keys=True)
                          for item in candidates})

    def test_b5_entry_variation_changes_identity(self):
        candidates = SG.generate_candidates(_space(
            generator_type=SG.ENTRY_VARIANT_GENERATOR,
            generator_version=SG.ENTRY_VARIANT_VERSION,
            variants={}, entry_slot=_explicit([ENTRY_A, ENTRY_B, ENTRY_C])))
        self.assertEqual(3, len(candidates))
        self.assertEqual(3, len(_ids(candidates)))
        # 每个 alternative 都必须过既有 bounded DSL 契约（canonical 形态）。
        for candidate in candidates:
            DSL.normalize(candidate.entry_spec)

    def test_b6_exit_variation_changes_identity(self):
        inherited = SG.generate_candidates(_space(
            generator_type=SG.EXIT_VARIANT_GENERATOR,
            generator_version=SG.EXIT_VARIANT_VERSION,
            variants={}, exit_slot=_explicit([EXIT_A, EXIT_B])))
        self.assertEqual(2, len(inherited))
        self.assertEqual(2, len(_ids(inherited)))
        # absent 与 explicit 是**不同**语义，因此必须得到不同的候选身份。
        absent = SG.generate_candidates(_space(
            generator_type=SG.EXIT_VARIANT_GENERATOR,
            generator_version=SG.EXIT_VARIANT_VERSION,
            variants={}, exit_slot={"kind": SS.ABSENT}))
        self.assertEqual(1, len(absent))
        self.assertIsNone(absent[0].exit_spec)
        self.assertEqual(set(), _ids(absent) & _ids(inherited))


class InheritanceSemanticsTests(_LedgerFixture):
    """B7 —— 继承语义在生成阶段解析成明确值，绝不回读 future current parent。"""

    def test_b7_inherited_parent_semantics_are_materialized_in_the_candidate(self):
        created = self._seed(metadata={"constraints": {"max_positions": 5,
                                                       "max_exposure_pct": 0.6}})
        result = self._generate(created, parameter_variants={"ma_period": [19]})
        candidate_id = result["candidate_ids"][0]
        read = SCV.get_candidate(self.conn, candidate_id)
        # 继承 = 那一版的最终语义，直接写进 candidate，而不是留一个悬空引用。
        self.assertEqual({"max_positions": 5, "max_exposure_pct": 0.6},
                         read["candidate"]["constraints"])
        self.assertEqual("momentum", read["candidate"]["intended_market_regime"])
        self.assertEqual({"scope_kind": "a_share_all", "boards": [], "symbols": [],
                          "asof_universe_identity": None},
                         read["candidate"]["universe_spec"])
        self.assertNotIn("inherit", json.dumps(read["candidate"]).lower())

        # 父策略后来升级：旧候选的语义与身份都不变。
        SR.save_definition(self.conn, PARENT,
                           {"dsl_ast": _parent_rule(ma_value=45)}, expected_version=1)
        reloaded = SCV.get_candidate(self.conn, candidate_id)
        self.assertEqual(read["candidate"], reloaded["candidate"])
        self.assertEqual({"max_positions": 5, "max_exposure_pct": 0.6},
                         reloaded["candidate"]["constraints"])

    def test_b7b_inherited_factor_and_exit_come_from_the_frozen_pin(self):
        pin = _pin(factor_spec=FACTOR_A, exit_spec=EXIT_A)
        candidates = SG.generate_candidates(_space(pin=pin, variants={}))
        self.assertEqual(1, len(candidates))
        self.assertEqual(SC._sha(DSL.normalize(FACTOR_A)),
                         SC._sha(SC._thaw(candidates[0].factor_spec)))
        self.assertEqual(SC._sha(DSL.normalize(EXIT_A)),
                         SC._sha(SC._thaw(candidates[0].exit_spec)))

    def test_b7c_absent_and_inherit_are_not_the_same_declaration(self):
        """字段"为空"必须语义唯一：``absent`` ≠ ``inherit_parent``。"""
        with self.assertRaises(SS.SearchSpaceError):
            _space(exit_slot={"kind": SS.INHERIT_PARENT, "alternatives": [EXIT_A]})
        with self.assertRaises(SS.SearchSpaceError):
            _space(exit_slot={"kind": SS.ABSENT, "alternatives": [EXIT_A]})
        with self.assertRaises(SS.SearchSpaceError):
            _space(exit_slot={"kind": "inherit"})
        # entry 不能是 absent：候选契约要求 entry_spec 必需。
        with self.assertRaises(SS.SearchSpaceError):
            _space(entry_slot={"kind": SS.ABSENT})


class UnsafeMutationTests(unittest.TestCase):
    """B8 —— 危险结构变异 fail closed；R35-B 不做自由 AST mutation。"""

    def test_b8_arbitrary_executable_payload_is_rejected(self):
        for payload in (
            {"op": "python", "source": "__import__('os').system('id')"},
            {"op": "eval", "expr": "1+1"},
            {"op": "exec", "code": "print(1)"},
            {"op": "import", "module": "os"},
            "__import__('os')",
        ):
            with self.assertRaises(SS.SearchSpaceError):
                _space(entry_slot=_explicit([payload]))

    def test_b8b_dynamic_field_lookup_and_unknown_ops_are_rejected(self):
        with self.assertRaises(SS.SearchSpaceError):
            _space(factor_slot=_explicit([
                {"op": "gt", "left": {"op": "field", "name": "__class__"},
                 "right": {"op": "const", "value": 1}}]))
        with self.assertRaises(SS.SearchSpaceError):
            _space(entry_slot=_explicit([
                {"op": "gt", "left": {"op": "field", "name": "close"},
                 "right": {"op": "const", "value": 1}, "extra": "x"}]))

    def test_b8c_structural_mutation_cannot_smuggle_a_second_parameter_authority(self):
        """factor / exit slot 不得夹带 ``strategy`` 根（第二套参数 authority）。"""
        with self.assertRaises(SS.SearchSpaceError):
            _space(factor_slot=_explicit([_parent_rule()]))
        with self.assertRaises(SS.SearchSpaceError):
            _space(exit_slot=_explicit([_parent_rule()]))

    def test_b8d_generator_never_rewrites_parent_structure_implicitly(self):
        """没有声明 slot 时，候选的 entry 就是父策略那一版的结构（逐字）。"""
        candidates = SG.generate_candidates(_space(variants={}))
        self.assertEqual(1, len(candidates))
        self.assertEqual(DSL.normalize(_parent_rule()),
                         SC._thaw(candidates[0].entry_spec))


class CrossGeneratorDedupTests(_LedgerFixture):
    """B9 / B11 —— 不同 generator 的相同语义仍然 dedup；batch ≠ candidate。"""

    def test_b9_same_semantics_across_generators_dedup_to_one_candidate(self):
        created = self._seed()
        first = self._generate(
            created, generator_type=SG.FACTOR_VARIANT_GENERATOR,
            generator_version=SG.FACTOR_VARIANT_VERSION, parameter_variants={},
            factor_slot=_explicit([FACTOR_A]))
        second = self._generate(
            created, generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
            generator_version=SG.BOUNDED_COMBINATION_VERSION,
            parameter_variants={}, factor_slot=_explicit([FACTOR_A]))
        # 同一份 canonical specification → 同一个 candidate_id。
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])
        # 但两次提案都必须留下事件，且各自记录是哪个能力提出的。
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_proposals").fetchone()[0])
        history = SCRepo.list_proposals(self.conn, first["candidate_ids"][0])
        self.assertEqual({SG.FACTOR_VARIANT_GENERATOR,
                          SG.BOUNDED_COMBINATION_GENERATOR},
                         {item["generator_type"] for item in history})
        self.assertEqual({first["generation_batch_id"], second["generation_batch_id"]},
                         {item["generation_batch_id"] for item in history})

    def test_b11_batch_identity_is_not_candidate_identity(self):
        created = self._seed()
        first = self._generate(created, parameter_variants={"ma_period": [19]})
        second = self._generate(created, parameter_variants={"ma_period": [19]})
        # 两个不同 batch，同一个 candidate。
        self.assertNotEqual(first["generation_batch_id"], second["generation_batch_id"])
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_proposals").fetchone()[0])
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_generation_batches").fetchone()[0])
        # batch identity 不进候选指纹。
        read = SCV.get_candidate(self.conn, first["candidate_ids"][0])
        self.assertNotIn("generation_batch_id", read["candidate"])

    def test_b9b_provenance_variation_does_not_split_candidate_identity(self):
        """B9b —— provenance 变化不得分裂语义相同的 candidate identity。

        candidate 是**内容**身份。"哪个 AI model / 哪条 hypothesis / 哪次研究
        来源 / 哪个 seed 提出了它"是**事件** provenance：它们属于 proposal 事件与
        generation batch。R35-C 接入 AI generator 后，GPT model A 与 model B 提出
        同一份策略时，必须得到**同一个** ``candidate_id``（候选行 1 条、proposal
        事件 2 条），否则同一策略会被重复送进实验 / PIT / robustness，候选数量虚高。

        注意：``generation_input_fingerprint`` **必须**不同 —— 两次请求确实是不同的
        输入事件；变的只是它们落到同一条候选行上。
        """
        created = self._seed()
        shared = {
            "parameter_variants": {"ma_period": [19]},
            "universe_spec": {"scope_kind": "a_share_all"},
            "intended_market_regime": "momentum", "evidence_count": 10,
        }
        batch_a = self._generate(
            created, hypothesis_id="hyp_a", random_seed=1,
            research_provenance={"source_kind": "human", "source_identity": "analyst-a"},
            model_identity={"provider": "openai", "model": "gpt-a", "version": "v1"},
            **shared)
        batch_b = self._generate(
            created, hypothesis_id="hyp_b", random_seed=2,
            research_provenance={"source_kind": "human", "source_identity": "analyst-b"},
            model_identity={"provider": "openai", "model": "gpt-b", "version": "v1"},
            **shared)

        # 语义完全相同 → 同一个 candidate。
        self.assertEqual(batch_a["candidate_ids"], batch_b["candidate_ids"])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])
        # 但每次提案都是独立事件，且各自保留自己的 provenance。
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_proposals").fetchone()[0])
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_generation_batches").fetchone()[0])
        self.assertNotEqual(batch_a["generation_input_fingerprint"],
                            batch_b["generation_input_fingerprint"])

        history = SCRepo.list_proposals(self.conn, batch_a["candidate_ids"][0])
        self.assertEqual(2, len(history))
        self.assertEqual({"hyp_a", "hyp_b"},
                         {item["hypothesis_id"] for item in history})
        self.assertEqual({1, 2}, {item["random_seed"] for item in history})
        self.assertEqual(
            {("analyst-a", "gpt-a"), ("analyst-b", "gpt-b")},
            {(item["research_provenance"]["source_identity"],
              item["model_identity"]["model"]) for item in history})
        self.assertEqual({batch_a["generation_batch_id"], batch_b["generation_batch_id"]},
                         {item["generation_batch_id"] for item in history})

        # v2 candidate 的内容与投影都不再携带这些 provenance。
        read = SCV.get_candidate(self.conn, batch_a["candidate_ids"][0])["candidate"]
        for key in ("hypothesis_id", "research_provenance", "random_seed", "model_identity"):
            self.assertNotIn(key, read, f"v2 candidate 投影不得携带 {key}")


class GenerationBatchTests(_LedgerFixture):
    """B10 —— 同一 batch 的所有 proposal 都能追溯到同一个 generation input。"""

    def test_b10_batch_binds_the_frozen_generation_input(self):
        created = self._seed()
        result = self._generate(
            created, parameter_variants={"ma_period": [19, 20]},
            research_provenance={"source_kind": "human", "source_identity": "analyst-a"},
            hypothesis_id="hyp_1")
        batch = SCV.get_generation_batch(self.conn, result["generation_batch_id"])
        record = batch["generation_batch"]
        # batch 绑定 exact parent pin + search-space 指纹 + generator 契约 + asof。
        self.assertEqual(created.current_checksum, record["parent_strategy_checksum"])
        self.assertEqual(1, record["parent_strategy_version"])
        self.assertEqual("2026-10-05", record["asof"])
        self.assertEqual(SG.GENERATOR_CONTRACT_VERSION,
                         record["generator_contract_version"])
        self.assertEqual(SS.SEARCH_SPACE_CONTRACT_VERSION,
                         record["search_space_contract_version"])
        self.assertEqual(64, len(record["search_space_fingerprint"]))
        self.assertEqual(2, record["candidate_count"])
        self.assertEqual(result["generation_input_fingerprint"],
                         record["generation_input_fingerprint"])
        self.assertEqual(result["search_space_fingerprint"],
                         record["search_space_fingerprint"])
        # 所有 proposal 事件都能追溯到同一个 batch / input fingerprint。
        self.assertEqual(2, len(batch["proposals"]))
        self.assertEqual({result["generation_input_fingerprint"]},
                         {item["input_fingerprint"] for item in batch["proposals"]})
        self.assertEqual({result["generation_batch_id"]},
                         {item["generation_batch_id"] for item in batch["proposals"]})
        self.assertEqual({"hyp_1"}, {item["hypothesis_id"] for item in batch["proposals"]})
        self.assertEqual(
            [{"source_kind": "human", "source_identity": "analyst-a"}] * 2,
            [item["research_provenance"] for item in batch["proposals"]])

    def test_b10b_generation_input_fingerprint_is_content_bound(self):
        created = self._seed()
        base = self._generate(created, parameter_variants={"ma_period": [19]})
        same = self._generate(created, parameter_variants={"ma_period": [19]})
        moved = self._generate(created, parameter_variants={"ma_period": [20]})
        # 同一输入 → 同一 input fingerprint（即使 batch 是不同的事件）。
        self.assertEqual(base["generation_input_fingerprint"],
                         same["generation_input_fingerprint"])
        self.assertNotEqual(base["generation_input_fingerprint"],
                            moved["generation_input_fingerprint"])
        # parent checksum 是输入的一部分：换 pin 就换 input fingerprint。
        # 这里必须用**存在**的另一个 checksum，因此先落一个真实的第二版。
        updated = SR.save_definition(self.conn, PARENT,
                                     {"dsl_ast": _parent_rule(ma_value=21)},
                                     expected_version=1)
        self.assertEqual(2, updated.version)
        other_pin = SCV.generate_and_record_candidates(
            self.conn, strategy_id=PARENT, strategy_version=2,
            strategy_checksum=updated.checksum, asof="2026-10-05",
            parameter_variants={"ma_period": [19]},
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum", evidence_count=10)
        self.assertNotEqual(base["generation_input_fingerprint"],
                            other_pin["generation_input_fingerprint"])

    def test_b10c_batch_rows_are_append_only_and_have_no_latest_pointer(self):
        created = self._seed()
        result = self._generate(created, parameter_variants={"ma_period": [19]})
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE strategy_candidate_generation_batches SET candidate_count=99"
                " WHERE batch_id=?", (result["generation_batch_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM strategy_candidate_generation_batches WHERE batch_id=?",
                (result["generation_batch_id"],))
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(strategy_candidate_generation_batches)")}
        self.assertEqual(set(), {name for name in columns
                                 if name.startswith(("current", "latest"))})
        with self.assertRaises(SCRepo.StrategyCandidateRepositoryError):
            SCRepo.get_generation_batch(self.conn, "short")

    def test_b10d_read_model_publishes_evidence_not_an_implicit_latest(self):
        """列表读模型不得把 append-only 历史压成"最近一条 proposal"。

        ``proposal_id`` 是随机 opaque id，两条事件可以合法拥有完全相同的
        ``created_at``，因此 ``proposals[-1]`` 只是"随机 id 排序靠后"，不是可靠的
        latest。列表要么给全部证据引用，要么用显式 batch id 单独取。
        """
        import inspect
        source = inspect.getsource(SCV)
        # 服务层不得存在任何"取最近一条 proposal"的投影。
        self.assertNotIn("_latest_proposal_summary", source)
        self.assertNotIn("latest_proposal", source)
        created = self._seed()
        # 同一个候选被两个 batch 提出：列表必须给出两条证据，而不是一条"最新"。
        self._generate(created, parameter_variants={"ma_period": [19]},
                       hypothesis_id="hyp_a")
        self._generate(created, parameter_variants={"ma_period": [19]},
                       hypothesis_id="hyp_b")
        listed = SCV.list_candidates_for_parent(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum)
        item = listed["items"][0]
        self.assertEqual(2, item["proposal_evidence"]["proposal_count"])
        self.assertEqual(2, len(item["proposal_evidence"]["proposals"]))
        self.assertEqual(2, len(set(item["proposal_evidence"]["generation_batch_ids"])))
        self.assertNotIn("proposal", item)
        # batch 摘要按显式 id 列出，条数与证据一致。
        self.assertEqual(2, len(listed["generation_batches"]))


class CandidateContractUpgradeTests(_LedgerFixture):
    """R35-B schema v2 —— 候选行是内容身份，generator 能力降到事件 provenance。"""

    def test_candidate_row_has_no_generation_provenance_column(self):
        """v2 候选表**不含任何** generation provenance 列。

        不只是 generator 三件套：``hypothesis_id`` / ``random_seed`` 如果留在表里，
        就会形成"``candidate_json`` 里没有、独立列里有"的两套互相矛盾事实，而
        ``get_candidate`` 只读 ``candidate_json`` —— 那些隐藏值写进去就再也读不出来，
        也清不掉（``INSERT OR IGNORE`` + 幂等只比 json/fingerprint）。
        """
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(strategy_candidates)")}
        self.assertEqual(set(), columns & set(SC.LEGACY_GENERATOR_IDENTITY_KEYS))
        self.assertEqual(set(), columns & set(SC.PROPOSAL_PROVENANCE_KEYS))
        # 候选内容 + 台账元数据仍然在。
        self.assertTrue({"candidate_id", "candidate_fingerprint", "candidate_json",
                         "asof", "parent_strategy_id", "parent_strategy_version",
                         "parent_strategy_checksum", "candidate_schema_version",
                         "candidate_contract_version", "created_at"} <= columns)
        candidate = SC.build_strategy_candidate(
            parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                             "strategy_checksum": CHECKSUM_A},
            asof="2026-10-05", entry_spec=_parent_rule(),
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum")
        self.assertEqual(SC.CANDIDATE_SCHEMA_VERSION, candidate.candidate_schema_version)
        self.assertIsNone(candidate.generator_type)
        # 落库后读回仍然自证。
        SCRepo.append_candidate(self.conn, candidate)
        reloaded = SCRepo.get_candidate(self.conn, candidate.candidate_id)
        self.assertTrue(SC.verify_candidate_fingerprint(reloaded))
        self.assertIsNone(reloaded.generator_type)

    def test_v1_rows_still_verify_under_their_own_material(self):
        """历史 v1 行必须继续自证；绝不把 v1 行"升级"成 v2。"""
        v1 = _v1_candidate()
        self.assertTrue(SC.verify_candidate_fingerprint(v1))
        rebuilt = SC.candidate_from_projection(v1.projection())
        self.assertEqual(v1.candidate_id, rebuilt.candidate_id)
        self.assertEqual(SC.CANDIDATE_SCHEMA_VERSION_V1, rebuilt.candidate_schema_version)
        self.assertTrue(SC.verify_candidate_fingerprint(rebuilt))
        # v2 行与 v1 行在指纹材料上确实不同：generator 三件套只属于 v1 材料。
        self.assertIn("generator_type", rebuilt.fingerprint_material())
        self.assertNotIn("generator_type", self._candidates_v2()[0].fingerprint_material())

    def test_v34_migration_rebuilds_a_v1_table_without_inventing_provenance(self):
        """v34 必须把 v1 形状的表安全重建，并保留历史行逐字不变。"""
        import paper_schema_migrations as PSM
        legacy = sqlite3.connect(os.path.join(self._dir.name, "legacy.sqlite3"))
        self.addCleanup(legacy.close)
        # 先建 R35-A 的 v1 形状（含三个 NOT NULL generator 列）。
        legacy.execute(_V1_CANDIDATE_DDL)
        v1 = _v1_candidate()
        legacy.execute(
            "INSERT INTO strategy_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v1.candidate_id, v1.candidate_fingerprint, SC.CANDIDATE_CONTRACT_VERSION,
             v1.candidate_schema_version, PARENT, 1, CHECKSUM_A, v1.generator_type,
             v1.generator_version, v1.generator_contract_version, None, "2026-10-05",
             None, json.dumps(v1.projection(), sort_keys=True, separators=(",", ":")),
             "2026-10-05T01:00:00+00:00"))
        legacy.commit()
        changes = PSM.ensure_strategy_candidates(legacy)
        self.assertEqual("rebuilt", changes["strategy_candidates"])
        columns = {row[1] for row in legacy.execute(
            "PRAGMA table_info(strategy_candidates)")}
        self.assertEqual(set(), columns & set(SC.LEGACY_GENERATOR_IDENTITY_KEYS))
        row = legacy.execute(
            "SELECT candidate_json FROM strategy_candidates WHERE candidate_id=?",
            (v1.candidate_id,)).fetchone()
        # candidate_json 逐字保留 → v1 材料仍能自证（绝不回填/改写）。
        restored = SC.candidate_from_projection(json.loads(row[0]))
        self.assertTrue(SC.verify_candidate_fingerprint(restored))
        self.assertEqual(v1.candidate_id, restored.candidate_id)
        # 幂等：再跑一次是 no-op。
        self.assertEqual("ok", PSM.ensure_strategy_candidates(legacy)["strategy_candidates"])

    def test_v34_rebuild_is_foreign_key_safe_with_populated_proposals(self):
        """v34 重建必须在**已有 proposal 行且 FK 开启**的 v33 账本上成功。

        ``strategy_candidate_proposals.candidate_id`` 引用候选表，而生产连接
        （``paper_storage`` / ``paper_trading``）开着 ``PRAGMA foreign_keys=ON``：
        直接 DROP 被引用的父表会报 ``FOREIGN KEY constraint failed``，升级后根本
        初始化不了。事务内 ``PRAGMA foreign_keys=OFF`` 是 no-op，所以重建必须走
        引用重写（子表先指向 staged 父表，最后一起 RENAME 回来）。
        """
        import paper_schema_migrations as PSM
        conn = sqlite3.connect(os.path.join(self._dir.name, "fk.sqlite3"))
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(_V1_CANDIDATE_DDL)
        # v33 形状的 proposal 表：带 FK、还没有 generation_batch_id 列。
        conn.execute(PSM.strategy_candidate_proposal_ddl(
            "strategy_candidate_proposals").replace("generation_batch_id TEXT,", ""))
        v1 = _v1_candidate()
        conn.execute(
            "INSERT INTO strategy_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v1.candidate_id, v1.candidate_fingerprint, SC.CANDIDATE_CONTRACT_VERSION,
             v1.candidate_schema_version, PARENT, 1, CHECKSUM_A, v1.generator_type,
             v1.generator_version, v1.generator_contract_version, None, "2026-10-05",
             None, json.dumps(v1.projection(), sort_keys=True, separators=(",", ":")),
             "2026-10-05T01:00:00+00:00"))
        conn.execute(
            "INSERT INTO strategy_candidate_proposals VALUES(?,?,?,?,?)",
            ("p" * 64, v1.candidate_id, "f" * 64, "{}", "2026-10-05T01:00:00+00:00"))
        conn.commit()

        changes = PSM.ensure_strategy_candidates(conn)
        self.assertEqual("rebuilt", changes["strategy_candidates"])
        self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
        # 子表的外键必须重新指回**真名**父表（RENAME 会把记录的父表名一起改写）。
        sql = conn.execute("SELECT sql FROM sqlite_master"
                           " WHERE name='strategy_candidate_proposals'").fetchone()[0]
        self.assertIn('REFERENCES "strategy_candidates"', sql)
        # 事件行与候选行逐字保留；legacy proposal 的 batch 归属保持 NULL。
        self.assertEqual([("p" * 64, v1.candidate_id, None)], conn.execute(
            "SELECT proposal_id,candidate_id,generation_batch_id"
            " FROM strategy_candidate_proposals").fetchall())
        self.assertEqual([(v1.candidate_id,)], conn.execute(
            "SELECT candidate_id FROM strategy_candidates").fetchall())
        # 外键仍然强制（重建没有把它降级成装饰）。
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO strategy_candidate_proposals"
                "(proposal_id,candidate_id,input_fingerprint,proposal_json,created_at)"
                " VALUES(?,?,?,?,?)", ("q" * 64, "z" * 64, "f" * 64, "{}", "x"))
        # 幂等。
        self.assertEqual("ok", PSM.ensure_strategy_candidates(conn)["strategy_candidates"])
        self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_v1_provenance_survives_only_in_candidate_json(self):
        """v1 迁移后：``candidate_json`` 逐字不变、v1 仍自证、顶层 provenance 列被移除。

        v1 的 generator 能力与提案 provenance 继续存在于**原始** ``candidate_json``
        里（v1 指纹材料本来就包含它们），因此历史行照旧自证；而顶层重复的一份
        必须被删掉，否则候选行与候选内容会变成两套互相矛盾的事实。
        """
        import paper_schema_migrations as PSM
        conn = sqlite3.connect(os.path.join(self._dir.name, "v1cols.sqlite3"))
        self.addCleanup(conn.close)
        conn.execute(_V1_CANDIDATE_DDL)
        v1 = _v1_candidate()
        payload = json.dumps(v1.projection(), sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO strategy_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v1.candidate_id, v1.candidate_fingerprint, SC.CANDIDATE_CONTRACT_VERSION,
             v1.candidate_schema_version, PARENT, 1, CHECKSUM_A, v1.generator_type,
             v1.generator_version, v1.generator_contract_version, v1.hypothesis_id,
             "2026-10-05", v1.random_seed, payload, "2026-10-05T01:00:00+00:00"))
        conn.commit()

        self.assertEqual("rebuilt", PSM.ensure_strategy_candidates(conn)["strategy_candidates"])
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(strategy_candidates)")}
        self.assertEqual(set(), columns & set(SC.LEGACY_GENERATOR_IDENTITY_KEYS))
        self.assertEqual(set(), columns & set(SC.PROPOSAL_PROVENANCE_KEYS))
        stored = conn.execute("SELECT candidate_json FROM strategy_candidates"
                              " WHERE candidate_id=?", (v1.candidate_id,)).fetchone()[0]
        # candidate_json 逐字不变。
        self.assertEqual(payload, stored)
        # v1 指纹仍自证。
        self.assertTrue(SC.verify_candidate_fingerprint(
            SC.candidate_from_projection(json.loads(stored))))
        self.assertEqual("ok", PSM.ensure_strategy_candidates(conn)["strategy_candidates"])

    def test_v34_also_rebuilds_the_intermediate_v2_shape(self):
        """本开发分支上跑过早期 v34 的库会停在**中间形态**（generator 列已去、
        ``hypothesis_id`` / ``random_seed`` 还在）—— 正是本轮要消灭的双表示，
        必须走同一条重建路径，否则那些库会永久卡在中间形态。"""
        import paper_schema_migrations as PSM
        conn = sqlite3.connect(os.path.join(self._dir.name, "intermediate.sqlite3"))
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""CREATE TABLE strategy_candidates(
             candidate_id TEXT PRIMARY KEY, candidate_fingerprint TEXT NOT NULL,
             candidate_contract_version TEXT NOT NULL, candidate_schema_version TEXT NOT NULL,
             parent_strategy_id TEXT, parent_strategy_version INTEGER,
             parent_strategy_checksum TEXT, hypothesis_id TEXT, asof TEXT NOT NULL,
             random_seed INTEGER, candidate_json TEXT NOT NULL, created_at TEXT NOT NULL,
             CHECK(candidate_id = candidate_fingerprint), CHECK(length(candidate_id)=64))""")
        conn.execute(PSM.strategy_candidate_proposal_ddl("strategy_candidate_proposals"))
        conn.execute("INSERT INTO strategy_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("a" * 64, "a" * 64, "strategy-candidate-contract-v2",
                      "strategy-candidate-v2", None, None, None, "leftover-hyp",
                      "2026-10-05", 7, "{}", "2026-10-05T01:00:00+00:00"))
        conn.execute("INSERT INTO strategy_candidate_proposals VALUES(?,?,?,?,?,?)",
                     ("p" * 64, "a" * 64, None, "f" * 64, "{}",
                      "2026-10-05T01:00:00+00:00"))
        conn.commit()

        self.assertEqual("rebuilt",
                         PSM.ensure_strategy_candidates(conn)["strategy_candidates"])
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(strategy_candidates)")}
        self.assertEqual(set(), columns & set(SC.PROPOSAL_PROVENANCE_KEYS))
        self.assertEqual(set(), columns & set(SC.LEGACY_GENERATOR_IDENTITY_KEYS))
        self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])
        self.assertEqual("ok", PSM.ensure_strategy_candidates(conn)["strategy_candidates"])

    def test_v2_candidate_payload_carries_no_provenance_and_rejects_smuggling(self):
        """v2 候选的投影与内容里都没有 provenance，且 v2 形状**拒绝**携带它们。

        仅仅"不进指纹"是不够的：如果这些键还留在 ``candidate_json`` 里，就会出现
        ``candidate_id 相同但 candidate_json 不同`` → ``append_candidate`` idempotency
        conflict，所以 ownership 转移必须是形状级的。
        """
        candidate = self._candidates_v2()[0]
        projection = candidate.projection()
        self.assertEqual(set(SC.candidate_projection_keys()), set(projection))
        for key in (*SC.LEGACY_GENERATOR_IDENTITY_KEYS, *SC.PROPOSAL_PROVENANCE_KEYS):
            self.assertNotIn(key, projection)
            self.assertNotIn(key, candidate.fingerprint_material())
        # 有人手工把 provenance 塞回 v2 持久化投影 → 读路径直接拒绝，不静默接受。
        smuggled = dict(projection)
        smuggled["model_identity"] = {"provider": "openai", "model": "gpt-x", "version": "v1"}
        with self.assertRaises(SC.CandidateValidationError):
            SC.candidate_from_projection(smuggled)
        # candidate 契约不接受 provenance 参数：传了必须炸，而不是被静默丢弃。
        with self.assertRaises(TypeError):
            SC.build_strategy_candidate(
                parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                                 "strategy_checksum": CHECKSUM_A},
                asof="2026-10-05", entry_spec=_parent_rule(),
                universe_spec={"scope_kind": "a_share_all"},
                intended_market_regime="momentum",
                hypothesis_id="hyp_x")

    def test_v2_candidate_object_cannot_carry_provenance(self):
        """v2 候选**对象本身**不能承载 generation provenance。

        这条刻意不只测 ``candidate_from_projection()``：要测的是
        ``StrategyCandidate`` 这条形状契约。v2 指纹不看 provenance，所以一个"id 正确
        却挂着 hypothesis / seed"的候选是**可构造**的 —— 而它一旦被写进独立列，就成了
        读不出来、清不掉的隐藏事实。因此不变量放在 dataclass 上，让构造器、
        读回、直接构造 / ``replace()`` 三条入口守同一条。
        """
        import dataclasses
        clean = self._candidates_v2()[0]
        self.assertIsNone(clean.hypothesis_id)
        self.assertIsNone(clean.random_seed)
        self.assertEqual({}, clean.research_provenance)
        self.assertEqual({}, clean.model_identity)
        for key in (*SC.LEGACY_GENERATOR_IDENTITY_KEYS, *SC.PROPOSAL_PROVENANCE_KEYS):
            for value in ("hidden", 123, {"provider": "openai", "model": "gpt-x",
                                          "version": "v1"},
                           {"source_kind": "human"}):
                with self.assertRaises(SC.CandidateValidationError):
                    dataclasses.replace(clean, **{key: value})
        # 直接构造同样拒绝。
        fields = {f.name: getattr(clean, f.name) for f in
                  dataclasses.fields(clean)}
        fields["hypothesis_id"] = "hidden-hyp"
        fields["random_seed"] = 123
        with self.assertRaises(SC.CandidateValidationError):
            SC.StrategyCandidate(**fields)
        # v1 历史行不受影响：它的 provenance 属于自己的指纹材料。
        self.assertTrue(SC.verify_candidate_fingerprint(_v1_candidate()))

    def _candidates_v2(self):
        return SG.generate_candidates(_space(variants={"ma_period": [19]}))


class AuthorityBoundaryTests(unittest.TestCase):
    """B12 —— R35-B 生产模块不得拿到 evaluation / promotion / execution 权限。"""

    #: R35-B 的纯生成域：连"取连接"的权限都没有。
    PURE_MODULES = ("strategy_candidate.py", "strategy_generator.py",
                    "strategy_candidate_search_space.py",
                    "strategy_candidate_repository.py")
    SERVICE_MODULE = "strategy_candidate_service.py"
    PRODUCTION_MODULES = PURE_MODULES + (SERVICE_MODULE,)

    #: 出现即意味着生成域拿到了它明确不该有的权限。
    FORBIDDEN_IMPORTS = frozenset({
        # evaluation / validation
        "backtest", "backtest_engine", "learning_evaluation", "experiment",
        "tradability_archive", "tradability_shadow", "strategy_health",
        "factor_quality_shadow", "shadow_runtime",
        # promotion / lifecycle
        "strategy_lifecycle", "strategy_promotion", "strategy_champion",
        "strategy_retirement", "evolution_apply", "asymmetric_risk",
        # execution / allocation
        "execution_planner", "execution_dispatch", "execution_lifecycle",
        "order_intent", "manual_orders", "paper_allocation",
        "portfolio_allocation_policy", "portfolio_runtime", "main", "fastapi",
        "api_strategies",
    })

    def _roots(self, name):
        with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=name)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_b12_generator_path_has_no_evaluation_promotion_or_execution_dependency(self):
        for name in self.PRODUCTION_MODULES:
            leaked = sorted(self._roots(name) & self.FORBIDDEN_IMPORTS)
            self.assertEqual([], leaked, f"{name} 获得了越权依赖：{leaked}")

    def test_b12b_no_scoring_ranking_or_winner_selection_in_the_generator_path(self):
        for name in self.PRODUCTION_MODULES:
            with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source, filename=name)
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            calls |= {node.func.id for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            for forbidden in ("promote", "transition", "submit_order", "place_order",
                              "commit_fill", "rank", "score", "sort_by_score",
                              "select_winner", "best_candidate"):
                self.assertNotIn(forbidden, calls, f"{name} 调用了 {forbidden}")

    def test_b12c_search_space_module_is_a_pure_contract(self):
        roots = self._roots("strategy_candidate_search_space.py")
        for forbidden in ("paper_trading", "paper_storage", "sqlite3", "fastapi"):
            self.assertNotIn(forbidden, roots)

    def test_b12d_no_runtime_create_or_alter_table_in_the_generator_path(self):
        for name in self.PRODUCTION_MODULES:
            with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
                source = handle.read().upper()
            for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE"):
                self.assertNotIn(forbidden, source, f"{name} 在运行时建表/改表")

    #: 生成/展开域：绝不允许机器时钟参与任何事实。
    CLOCK_FREE_MODULES = ("strategy_candidate.py", "strategy_generator.py",
                          "strategy_candidate_search_space.py",
                          "strategy_candidate_service.py")

    def test_b12e_no_implicit_current_state_lookup(self):
        """生成/展开路径不得读取"当前"父策略 / 当前日期 / 当前组合。

        以 AST 为准：docstring 里写"没有 ``datetime.now()``"是**说明**，不是调用；
        只有真实代码节点才算违规。
        """
        for name in self.CLOCK_FREE_MODULES:
            with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=name)
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc is not None:
                        docstrings.add(doc)
            attributes = {node.attr for node in ast.walk(tree)
                          if isinstance(node, ast.Attribute)}
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            literals = {node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value not in docstrings}
            self.assertNotIn("now", attributes, f"{name} 读了机器时钟")
            self.assertNotIn("today", attributes, f"{name} 读了机器时钟")
            self.assertNotIn("datetime", names, f"{name} 引用了机器时钟")
            for forbidden in ("MAX(version)", "get_latest", "get_current",
                              "current_strategy", "latest_strategy"):
                self.assertNotIn(forbidden, literals, f"{name} 有 current/latest 读路径")

    def test_b12e2_repository_clock_is_confined_to_persistence_metadata(self):
        """台账层的机器时钟只允许给 ``created_at`` 打时间戳，绝不进候选身份。"""
        with open(os.path.join(BACKEND, "strategy_candidate_repository.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename="strategy_candidate_repository.py")
        clocks = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and node.attr in ("now", "today")]
        self.assertTrue(clocks, "created_at 需要一个明确的时间戳来源")
        candidate = SG.generate_candidates(_space(variants={"ma_period": [19]}))[0]
        self.assertNotIn("created_at", candidate.fingerprint_material())
        self.assertNotIn("generation_batch_id", candidate.fingerprint_material())

    def test_b12f_generator_dispatch_is_a_registry_not_a_branching_chain(self):
        with open(os.path.join(BACKEND, "strategy_generator.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename="strategy_generator.py")
        # generator_type 的派发必须是 registry 查表：模块里不得出现按能力名比较的
        # if/elif 链（那样每加一个能力就多一条分支）。
        comparisons = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(isinstance(item, ast.Constant) and isinstance(item.value, str)
                    and item.value in SG.GENERATOR_CAPABILITIES
                    for item in [node.left, *node.comparators])
        ]
        self.assertEqual([], comparisons)
        self.assertIn("GENERATOR_CAPABILITIES", dir(SG))


#: R35-A（v33）候选表的原始形状，用来证明 v34 重建保留了历史行。
_V1_CANDIDATE_DDL = """
CREATE TABLE IF NOT EXISTS strategy_candidates(
    candidate_id TEXT PRIMARY KEY,
    candidate_fingerprint TEXT NOT NULL,
    candidate_contract_version TEXT NOT NULL,
    candidate_schema_version TEXT NOT NULL,
    parent_strategy_id TEXT,
    parent_strategy_version INTEGER,
    parent_strategy_checksum TEXT,
    generator_type TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    generator_contract_version TEXT NOT NULL,
    hypothesis_id TEXT,
    asof TEXT NOT NULL,
    random_seed INTEGER,
    candidate_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(candidate_id = candidate_fingerprint),
    CHECK(length(candidate_id)=64)
)
"""


def _v1_candidate():
    """Build one R35-A (schema v1) candidate under the historical material shape."""
    entry = DSL.normalize(_parent_rule())
    material = {
        "candidate_schema_version": SC.CANDIDATE_SCHEMA_VERSION_V1,
        "parent_strategy_id": PARENT,
        "parent_strategy_version": 1,
        "parent_strategy_checksum": CHECKSUM_A,
        "generator_type": SG.PARAMETER_VARIANT_GENERATOR,
        "generator_version": SG.PARAMETER_VARIANT_VERSION,
        "generator_contract_version": SG.GENERATOR_CONTRACT_VERSION_V1,
        "hypothesis_id": None,
        "research_provenance": {"source_kind": "human"},
        "strategy_schema_version": DSL.DSL_SCHEMA_VERSION,
        "factor_spec": None,
        "entry_spec": entry,
        "exit_spec": None,
        "parameter_spec": {},
        "universe_spec": {"scope_kind": "a_share_all", "boards": [], "symbols": [],
                          "asof_universe_identity": None},
        "intended_market_regime": "momentum",
        "constraints": {},
        "asof": "2026-10-05",
        "random_seed": None,
        "model_identity": {},
    }
    fingerprint = SC._sha(material)
    return SC.StrategyCandidate(
        candidate_id=fingerprint, candidate_fingerprint=fingerprint,
        parent_strategy_id=PARENT, parent_strategy_version=1,
        parent_strategy_checksum=CHECKSUM_A,
        hypothesis_id=None, research_provenance=SC._freeze({"source_kind": "human"}),
        strategy_schema_version=DSL.DSL_SCHEMA_VERSION, factor_spec=None,
        entry_spec=SC._freeze(entry), exit_spec=None, parameter_spec=SC._freeze({}),
        universe_spec=SC._freeze(material["universe_spec"]),
        intended_market_regime="momentum", constraints=SC._freeze({}),
        asof="2026-10-05", random_seed=None, model_identity=SC._freeze({}),
        candidate_schema_version=SC.CANDIDATE_SCHEMA_VERSION_V1,
        generator_type=SG.PARAMETER_VARIANT_GENERATOR,
        generator_version=SG.PARAMETER_VARIANT_VERSION,
        generator_contract_version=SG.GENERATOR_CONTRACT_VERSION_V1)


def _reverse_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
