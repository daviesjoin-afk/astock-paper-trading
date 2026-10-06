# -*- coding: utf-8 -*-
"""R35-A contract regressions: ``StrategyCandidate`` + constrained generator.

Covers C1…C10 from the R35-A specification:

    C1  deterministic candidate identity
    C2  semantic mutation changes identity
    C3  parent pinning survives a later parent upgrade
    C4  arbitrary executable source is rejected (fail closed)
    C5  missing provenance fails closed (no latest/current fallback)
    C6  dedup on canonical candidate identity (candidate) / every occurrence is a
        new event (proposal)
    C7  append-only identity (no update-into-another-candidate)
    C8  persistence round trip re-verifies the fingerprint
    C9  no promotion / execution authority in the generator production path
    C10 current-state leakage cannot rebind an existing candidate

The suite is deliberately about the **contract**, not about strategy quality:
nothing here asserts that any candidate is profitable, and the ledger has no
place to put such a claim.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from collections.abc import Mapping
from unittest import mock

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

PARENT = "r35a_parent"
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
    """A synthetic parameterized parent definition (fixture, not a real strategy).

    参数声明遵循既有 DSL 约定：**在 rule 里内联使用的** parameter 节点（``ma`` 的
    window、RSI 阈值）只内联声明一次，不再重复出现在 ``parameters`` 列表里；
    ``parameters`` 列表只承载其余声明。重复声明会被 DSL schema 拒绝。
    """
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
        "parameters": [
            _parameter("volume_multiplier", "number", 1.5, 1.0, 3.0, 0.2),
        ],
    }


def _wide_rule():
    """A two-parameter parent wide enough to exceed the declared variant ceiling."""
    return {
        "op": "strategy",
        "rule": {
            "op": "and",
            "args": [
                {"op": "gt", "left": {"op": "field", "name": "close"},
                 "right": {"op": "indicator", "name": "ma",
                           "window": _parameter("ma_period", "integer", 30, 5, 60, 40)}},
                {"op": "gt", "left": {"op": "field", "name": "volume"},
                 "right": {"op": "mul",
                           "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                           "right": _parameter("volume_multiplier", "number", 1.5,
                                               1.0, 3.0, 2.0)}},
            ],
        },
        "parameters": [],
    }


def _pin(*, rule=None, version=1, checksum=CHECKSUM_A, asof="2026-10-05"):
    return SS.ParentStrategyPin(
        strategy_id=PARENT, strategy_version=version, strategy_checksum=checksum,
        dsl_ast=_parent_rule() if rule is None else rule, asof=asof)


def _search_space(*, pin=None, variants=None, adjustments=None, asof="2026-10-05",
                  generator_type=SG.PARAMETER_VARIANT_GENERATOR,
                  generator_version=SG.PARAMETER_VARIANT_VERSION, **overrides):
    """One explicit R35-B search space with R35-A's defaults."""
    values = {
        "parent_pin": _pin() if pin is None else pin,
        "generator_type": generator_type,
        "generator_version": generator_version,
        "parameter_variants": ({"ma_period": [19, 20, 21]} if adjustments is None
                               else adjustments) if variants is None else variants,
        "universe_spec": {"scope_kind": "a_share_all"},
        "intended_market_regime": "momentum",
        "asof": asof,
        "evidence_count": 10,
    }
    values.update(overrides)
    return SS.CandidateSearchSpace(**values)


def _seed_registry(conn, *, rule=None, strategy_id=PARENT, name="R35A Parent"):
    return SR.create_user_definition(
        conn, strategy_id, name, dsl_ast=_parent_rule() if rule is None else rule,
        actor="test")


def _propose(conn, candidate, *, input_fingerprint, created_at=None,
             generation_batch_id=None):
    """Record one proposal event with the capability identity it belongs to.

    R35-B：proposal 是事件，事件必须说清"是哪个 generator 能力、哪一次请求提出的"。
    因此能力身份是**必填**参数，而不是从 candidate 上猜（候选已经不带它了）。
    """
    return SCRepo.record_proposal(
        conn, candidate, input_fingerprint=input_fingerprint,
        generator_type=SG.PARAMETER_VARIANT_GENERATOR,
        generator_version=SG.PARAMETER_VARIANT_VERSION,
        generator_contract_version=SG.GENERATOR_CONTRACT_VERSION,
        generation_batch_id=generation_batch_id, created_at=created_at)


class _LedgerFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self._dir.name, "paper.sqlite3"))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.conn.close)
        # DDL owner 是 paper_schema_migrations（migration v33/v34 调用同一个函数）。
        import paper_schema_migrations as PSM
        PSM.ensure_strategy_candidates(self.conn)

    def _candidates(self, **kwargs):
        return SG.generate_candidates(_search_space(**kwargs))


class CandidateIdentityTests(_LedgerFixture):
    """C1 / C2 —— canonical identity is deterministic and semantics-sensitive."""

    def test_c1_same_canonical_specification_yields_the_same_fingerprint(self):
        first = self._candidates()
        second = self._candidates()
        self.assertEqual([item.candidate_id for item in first],
                         [item.candidate_id for item in second])
        self.assertEqual(len(first), 3)
        for candidate in first:
            self.assertTrue(SC.verify_candidate_fingerprint(candidate))
            # candidate_id **就是** canonical fingerprint。
            self.assertEqual(candidate.candidate_id, candidate.candidate_fingerprint)

    def test_c1b_key_order_and_presentation_never_change_the_fingerprint(self):
        """只有 **JSON key 顺序**是展示材料；list 顺序是 DSL 语义的一部分。

        ``and`` 的 args 顺序被反转是真实的语义差异（既有 DSL 不排序 args），
        因此**必须**改变指纹；而每个对象的 key 插入顺序不承载语义。
        """
        pin = _pin()
        reordered = _reverse_mapping_keys(_thaw(pin.dsl_ast))
        other = SG.ParentStrategyPin(
            strategy_id=PARENT, strategy_version=1, strategy_checksum=CHECKSUM_A,
            dsl_ast=reordered, asof="2026-10-05")
        self.assertEqual([item.candidate_id for item in self._candidates(pin=pin)],
                         [item.candidate_id for item in self._candidates(pin=other)])

        # 反例：真正的语义变化（args 顺序）必须改变身份。
        flipped = _thaw(pin.dsl_ast)
        flipped["rule"]["args"] = list(reversed(flipped["rule"]["args"]))
        self.assertNotEqual(
            [item.candidate_id for item in self._candidates(pin=pin)],
            [item.candidate_id for item in self._candidates(
                pin=_pin(rule=flipped))])

    def test_c2_parameter_mutation_changes_the_fingerprint(self):
        base = {item.candidate_id for item in self._candidates()}
        # 22 不在默认网格 {19,20,21} 里，且相对父值 20 仍在 max_step=1 内。
        moved = {item.candidate_id for item in self._candidates(
            adjustments={"ma_period": [22]})}
        self.assertEqual(set(), base & moved)
        self.assertTrue(moved)

    def test_c2b_entry_factor_exit_and_parent_checksum_mutations_all_move_identity(self):
        reference = self._candidates()[0].candidate_id

        def fingerprint(*, entry=None, factor=None, exit_spec=None, checksum=CHECKSUM_A):
            candidate = SC.build_strategy_candidate(
                parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                                 "strategy_checksum": checksum},
                asof="2026-10-05",
                entry_spec=_parent_rule() if entry is None else entry,
                universe_spec={"scope_kind": "a_share_all"},
                intended_market_regime="momentum",
                factor_spec=factor,
                exit_spec=exit_spec,
                research_provenance={"source_kind": "human"},
            )
            return candidate.candidate_id

        # reference uses the same material as the first generated variant only if
        # the parameter values coincide; compare *changes* instead of absolutes.
        baseline = fingerprint()
        self.assertNotEqual(baseline, fingerprint(entry=_parent_rule(ma_value=19)))
        self.assertNotEqual(baseline, fingerprint(checksum=CHECKSUM_B))
        self.assertNotEqual(baseline, fingerprint(
            factor={"op": "gt", "left": {"op": "field", "name": "volume"},
                    "right": {"op": "const", "value": 1000}}))
        self.assertNotEqual(baseline, fingerprint(
            exit_spec={"op": "lt", "left": {"op": "field", "name": "close"},
                       "right": {"op": "const", "value": 9}}))
        self.assertNotEqual(baseline, reference)

    def test_c2c_identity_material_carries_no_evaluation_result(self):
        """Sharpe / 收益 / 回撤 / 胜率 / promotion 结果不属于 candidate identity。"""
        for forbidden in ("sharpe", "max_drawdown", "win_rate", "promotion_result"):
            with self.assertRaises(SC.CandidateValidationError):
                SC.build_strategy_candidate(
                    parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                                     "strategy_checksum": CHECKSUM_A},
                    asof="2026-10-05",
                    entry_spec=_parent_rule(),
                    universe_spec={"scope_kind": "a_share_all"},
                    intended_market_regime="momentum",
                    research_provenance={"source_kind": "human"},
                    constraints={forbidden: 1},
                )

    def test_c2d_dsl_schema_version_is_part_of_the_fingerprint(self):
        self.assertTrue(hasattr(DSL, "DSL_SCHEMA_VERSION"))
        candidate = self._candidates()[0]
        self.assertEqual(DSL.DSL_SCHEMA_VERSION, candidate.strategy_schema_version)
        material = candidate.fingerprint_material()
        self.assertIn("strategy_schema_version", material)
        # R35-B：generator 能力身份**不是**候选内容的一部分（它是 proposal 事件 /
        # batch 的 provenance）。同一份 specification 由不同能力提出必须是同一个
        # candidate_id，因此材料里不允许出现这三件套。
        for key in SC.LEGACY_GENERATOR_IDENTITY_KEYS:
            self.assertNotIn(key, material)

    def test_c2e_declared_parameter_contract_is_part_of_the_fingerprint(self):
        """``volume_multiplier`` 只声明在 ``parameters`` 列表里（不在 rule 内联）。

        因此它的值变化**必须**进入指纹——无论经由 ``parameter_spec`` 还是经由
        ``entry_spec``。把任一处的参数值从身份材料里抹掉都会让两个参数不同的
        候选得到同一个身份。
        """
        base = {item.candidate_id for item in self._candidates(
            adjustments={"ma_period": [19]})}
        moved = {item.candidate_id for item in self._candidates(
            adjustments={"ma_period": [19], "volume_multiplier": [1.6]})}
        self.assertEqual(set(), base & moved)
        first = self._candidates(adjustments={"ma_period": [19]})[0]
        second = self._candidates(
            adjustments={"ma_period": [19], "volume_multiplier": [1.6]})[0]
        self.assertNotEqual(first.parameter_spec, second.parameter_spec)
        self.assertNotEqual(first.candidate_id, second.candidate_id)


class ParentPinningTests(_LedgerFixture):
    """C3 / C10 —— the pin is immutable; a later parent version cannot rebind it."""

    def test_c3_parent_upgrade_does_not_change_recorded_candidate_provenance(self):
        created = _seed_registry(self.conn)
        self.assertEqual(1, created.current_version)
        pin_v1 = SCV.pin_parent_strategy(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05")
        candidates = SG.generate_candidates(_search_space(pin=pin_v1))
        stored = SCRepo.append_candidate(self.conn, candidates[0])

        # 父策略后来升级到 v2（不可变新版本）。
        updated = SR.save_definition(
            self.conn, PARENT, {"dsl_ast": _parent_rule(ma_value=30)},
            expected_version=1)
        self.assertEqual(2, updated.version)

        reloaded = SCRepo.get_candidate(self.conn, stored.candidate_id)
        self.assertEqual(1, reloaded.parent_strategy_version)
        self.assertEqual(created.current_checksum, reloaded.parent_strategy_checksum)
        self.assertEqual(reloaded.candidate_id, stored.candidate_id)
        self.assertTrue(SC.verify_candidate_fingerprint(reloaded))

    def test_c10_current_registry_head_cannot_replace_a_stored_pin(self):
        created = _seed_registry(self.conn)
        pin = SCV.pin_parent_strategy(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05")
        candidate = SC.build_strategy_candidate(
            parent_identity=pin.identity,
            asof="2026-10-05",
            entry_spec=_thaw(pin.dsl_ast),
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum",
            research_provenance={"source_kind": "human"})
        SCRepo.append_candidate(self.conn, candidate)
        SR.save_definition(self.conn, PARENT,
                           {"dsl_ast": _parent_rule(ma_value=45)}, expected_version=1)

        head = SR.get_version(PARENT, conn=self.conn)
        self.assertEqual(2, head.version)
        reloaded = SCRepo.get_candidate(self.conn, candidate.candidate_id)
        self.assertNotEqual(head.checksum, reloaded.parent_strategy_checksum)
        self.assertEqual(pin.identity, reloaded.identity())

    def test_c3e_parent_metadata_constraints_are_inherited_not_dropped(self):
        """父策略那一版声明的 constraints 必须被候选继承，而不是变成空集。

        空集不是"无约束"：它会让下游实验读到一份并非父策略语义的候选（仓位 /
        敞口 / 权重上限被悄悄丢掉）。

        R35-B 收紧为 "inherit, or only tighten"：显式 override 既不能丢掉父策略
        已有的边界（丢掉 = 放宽），也不能把任何边界放宽。放宽仓位 / 敞口 / 权重上限
        是风险放大动作，必须走正式 risk evidence gate，不属于 candidate generator。
        """
        inherited = {"max_positions": 5, "max_exposure_pct": 0.6}
        created = SR.create_user_definition(
            self.conn, PARENT, "R35A Parent", dsl_ast=_parent_rule(),
            metadata={"constraints": inherited}, actor="test")
        result = SCV.generate_and_record_candidates(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05",
            parameter_adjustments={"ma_period": [19]},
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum", evidence_count=10)
        read = SCV.get_candidate(self.conn, result["candidate_ids"][0])
        self.assertEqual(inherited, read["candidate"]["constraints"])

        # 收紧是允许的（两个边界都给，且都不放宽）。
        tightened = {"max_positions": 2, "max_exposure_pct": 0.4}
        overridden = SCV.generate_and_record_candidates(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05",
            parameter_adjustments={"ma_period": [19]},
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum", evidence_count=10,
            constraints=tightened)
        read_override = SCV.get_candidate(self.conn, overridden["candidate_ids"][0])
        self.assertEqual(tightened, read_override["candidate"]["constraints"])

        # 丢掉父策略的边界（= 放宽）与放宽任一上限都必须 fail closed。
        for expanding in ({"max_positions": 2},
                          {"max_positions": 9, "max_exposure_pct": 0.6},
                          {"max_positions": 5, "max_exposure_pct": 0.9}):
            with self.assertRaises(SS.SearchSpaceError):
                SCV.generate_and_record_candidates(
                    self.conn, strategy_id=PARENT, strategy_version=1,
                    strategy_checksum=created.current_checksum, asof="2026-10-05",
                    parameter_adjustments={"ma_period": [19]},
                    universe_spec={"scope_kind": "a_share_all"},
                    intended_market_regime="momentum", evidence_count=10,
                    constraints=expanding)

    def test_c3d_pinning_never_falls_back_to_the_registry_head(self):
        """只给 strategy_id 时**必须**拒绝，绝不用 current head 补齐身份。

        这条断言独立于"C3 升级后 provenance 不变"：即使读路径老实读 persisted
        pin，只要 pin 的建立允许回退到 head，候选就已经被绑到了错误的版本上。
        """
        created = _seed_registry(self.conn)
        SR.save_definition(self.conn, PARENT, {"dsl_ast": _parent_rule(ma_value=45)},
                           expected_version=1)
        head = SR.get_version(PARENT, conn=self.conn)
        self.assertEqual(2, head.version)
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=1,
                                    strategy_checksum="", asof="2026-10-05")
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=1,
                                    strategy_checksum=None, asof="2026-10-05")
        # 显式给出 v1 checksum 时读到的仍然是 v1，而不是 head v2。
        pin = SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=1,
                                      strategy_checksum=created.current_checksum,
                                      asof="2026-10-05")
        self.assertEqual(1, pin.strategy_version)
        self.assertEqual(created.current_checksum, pin.strategy_checksum)
        self.assertNotEqual(head.checksum, pin.strategy_checksum)

    def test_c3b_pin_requires_the_exact_version_and_checksum(self):
        created = _seed_registry(self.conn)
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            # 版本存在但 checksum 不符 → registry raise → fail closed。
            SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=1,
                                    strategy_checksum=CHECKSUM_B, asof="2026-10-05")
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=99,
                                    strategy_checksum=created.current_checksum,
                                    asof="2026-10-05")
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=1,
                                    strategy_checksum="not-a-digest", asof="2026-10-05")

    def test_c3c_parent_without_declarative_dsl_cannot_be_a_generator_basis(self):
        created = _seed_registry(self.conn)
        self.conn.execute("DROP TRIGGER trg_strategy_versions_no_update")
        self.conn.execute("DROP TRIGGER trg_strategy_definition_version_guard")
        self.conn.execute(
            "UPDATE paper_strategy_versions SET definition_json=? WHERE strategy_id=? AND version=1",
            (json.dumps({"dsl_ast": None, "description": "", "implementation_key": "x",
                         "metadata": {}, "name": "R35A Parent"}, sort_keys=True,
                        separators=(",", ":")), PARENT))
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.pin_parent_strategy(self.conn, strategy_id=PARENT, strategy_version=1,
                                    strategy_checksum=created.current_checksum,
                                    asof="2026-10-05")


class ExecutablePayloadTests(_LedgerFixture):
    """C4 —— arbitrary executable strategy payload is rejected by the schema."""

    def _rejected(self, spec):
        with self.assertRaises(SC.CandidateValidationError):
            SC.build_strategy_candidate(
                parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                                 "strategy_checksum": CHECKSUM_A},
                asof="2026-10-05",
                entry_spec=spec, universe_spec={"scope_kind": "a_share_all"},
                intended_market_regime="momentum",
                research_provenance={"source_kind": "human"})

    def test_c4_python_source_eval_exec_and_shell_payloads_are_rejected(self):
        # 这些不是"子串过滤"挡下来的：DSL schema 里**没有**这些 op，任何自由
        # 源码形状都无法通过封闭节点校验。
        self._rejected({"op": "python", "source": "__import__('os').system('id')"})
        self._rejected({"op": "eval", "expr": "1+1"})
        self._rejected({"op": "exec", "code": "print(1)"})
        self._rejected({"op": "call", "name": "subprocess.run", "args": ["ls"]})
        self._rejected({"op": "shell", "command": "rm -rf /"})
        self._rejected({"op": "import", "module": "os"})
        self._rejected("__import__('os')")
        self._rejected({"op": "gt", "left": {"op": "source", "code": "1"},
                        "right": {"op": "const", "value": 1}})

    def test_c4b_dynamic_field_and_attribute_access_is_rejected(self):
        self._rejected({"op": "gt", "left": {"op": "field", "name": "__class__"},
                        "right": {"op": "const", "value": 1}})
        self._rejected({"op": "gt", "left": {"op": "field", "name": "os.system"},
                        "right": {"op": "const", "value": 1}})

    def test_c4c_parameter_spec_cannot_be_smuggled_into_a_non_entry_rule(self):
        with self.assertRaises(SC.CandidateValidationError):
            SC.build_strategy_candidate(
                parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                                 "strategy_checksum": CHECKSUM_A},
                asof="2026-10-05",
                entry_spec=_parent_rule(),
                exit_spec=_parent_rule(),  # 第二个 parameter authority
                universe_spec={"scope_kind": "a_share_all"},
                intended_market_regime="momentum",
                research_provenance={"source_kind": "human"})

    def test_c4d_unknown_ops_and_oversized_asts_are_rejected(self):
        self._rejected({"op": "gt", "left": {"op": "field", "name": "close"},
                        "right": {"op": "const", "value": 1}, "extra": "x"})
        nested = {"op": "const", "value": 1}
        for _ in range(DSL.MAX_AST_DEPTH + 2):
            nested = {"op": "not", "arg": nested}
        self._rejected(nested)


class MissingProvenanceTests(_LedgerFixture):
    """C5 —— missing required facts fail closed; nothing is silently backfilled."""

    def _build(self, **overrides):
        values = {
            "parent_identity": {"strategy_id": PARENT, "strategy_version": 1,
                                "strategy_checksum": CHECKSUM_A},
            "asof": "2026-10-05",
            "entry_spec": _parent_rule(),
            "universe_spec": {"scope_kind": "a_share_all"},
            "intended_market_regime": "momentum",
            "research_provenance": {"source_kind": "human"},
        }
        values.update(overrides)
        return SC.build_strategy_candidate(**values)

    def test_c5_missing_parent_version_or_checksum_is_rejected(self):
        with self.assertRaises(SC.CandidateValidationError):
            self._build(parent_identity={"strategy_id": PARENT})
        with self.assertRaises(SC.CandidateValidationError):
            self._build(parent_identity={"strategy_id": PARENT, "strategy_version": 1})
        with self.assertRaises(SC.CandidateValidationError):
            self._build(parent_identity={"strategy_id": PARENT, "strategy_version": 0,
                                         "strategy_checksum": CHECKSUM_A})
        with self.assertRaises(SC.CandidateValidationError):
            self._build(parent_identity={"strategy_id": PARENT, "strategy_version": 1,
                                         "strategy_checksum": "abc"})

    def test_c5b_missing_asof_and_entry_are_rejected(self):
        with self.assertRaises(SC.CandidateValidationError):
            self._build(asof=None)
        with self.assertRaises(SC.CandidateValidationError):
            self._build(asof="today")
        with self.assertRaises(SC.CandidateValidationError):
            self._build(entry_spec=None)

    def test_c5c_search_space_requires_explicit_asof_universe_and_regime(self):
        with self.assertRaises(SC.CandidateValidationError):
            _search_space(asof=None)
        with self.assertRaises(SS.SearchSpaceError):
            _search_space(intended_market_regime="")
        with self.assertRaises(SS.SearchSpaceError):
            _search_space(universe_spec=None)
        with self.assertRaises(SS.SearchSpaceError):
            # 没有显式 pin 就没有生成基础。
            _search_space(parent_pin=None)
        with self.assertRaises(SS.SearchSpaceError):
            # 能力身份必须显式给出（空值不是"用默认"）。
            _search_space(generator_type="")
        # 能力必须在**已注册**的封闭词汇里：registry 的 authority 在生成域。
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(generator_type="generic")
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(generator_version="v9")

    def test_c5d_service_requires_explicit_universe_and_regime(self):
        created = _seed_registry(self.conn)
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.generate_and_record_candidates(
                self.conn, strategy_id=PARENT, strategy_version=1,
                strategy_checksum=created.current_checksum, asof="2026-10-05",
                parameter_adjustments={"ma_period": [19]},
                universe_spec=None, intended_market_regime="momentum",
                evidence_count=10)

    def test_c5e_undeclared_or_out_of_contract_parameters_are_rejected(self):
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(adjustments={"undeclared_knob": [1, 2]})
        # max_step 是父策略自己的参数契约：一次跨 5 超过 max_step=1。
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(adjustments={"ma_period": [25]})
        # 越界（max=60）。
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(adjustments={"ma_period": [90]})
        # 证据不足时既有 owner 会拒绝，生成域原样 fail closed。
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(evidence_count=0)

    def test_c5f_locked_parameters_cannot_be_varied(self):
        rule = _parent_rule()
        # 把已内联的 RSI 阈值标记为 locked：参数契约由既有 owner 裁决。
        rule["rule"]["args"][1]["right"] = _parameter(
            "rsi_threshold", "number", 55.0, 30.0, 80.0, 2.0, locked=True)
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(pin=_pin(rule=rule), adjustments={"rsi_threshold": [57.0]})

    def test_c5g_variant_space_is_bounded_by_the_input_contract(self):
        with self.assertRaises(SG.StrategyGeneratorError):
            self._candidates(pin=_pin(rule=_wide_rule()),
                             adjustments={"ma_period": list(range(5, 61)),
                                          "volume_multiplier": [1.0, 2.0, 3.0]})


class LedgerTests(_LedgerFixture):
    """C6 / C7 / C8 —— dedup, append-only identity, persistence round trip."""

    def test_c6_same_candidate_is_not_duplicated(self):
        candidates = self._candidates()
        for candidate in candidates:
            SCRepo.append_candidate(self.conn, candidate)
        first_count = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0]
        self.assertEqual(len(candidates), first_count)
        # 同一个 generator 再提一次：身份不变，行数不增。
        for candidate in self._candidates():
            SCRepo.append_candidate(self.conn, candidate)
        self.assertEqual(first_count, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])
        self.assertEqual(
            len(candidates),
            len({row[0] for row in self.conn.execute(
                "SELECT candidate_id FROM strategy_candidates").fetchall()}))

    def test_c6b_dedup_keeps_the_proposal_source_evidence(self):
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        first = _propose(self.conn, candidate, input_fingerprint="1" * 64,
                         created_at="2026-10-05T01:00:00+00:00")
        second = _propose(self.conn, candidate, input_fingerprint="2" * 64,
                          created_at="2026-10-06T01:00:00+00:00")
        self.assertNotEqual(first, second)
        history = SCRepo.list_proposals(self.conn, candidate.candidate_id)
        self.assertEqual(2, len(history))
        self.assertEqual({"1" * 64, "2" * 64},
                         {item["input_fingerprint"] for item in history})
        # 候选行本身仍然只有一行，且指纹未变。
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates").fetchone()[0])

    def test_c6d_every_proposal_occurrence_gets_its_own_identity(self):
        """同一秒内对同一候选、同一输入提出两次，是两条独立历史记录。

        提案是**事件**而不是内容的函数：如果 proposal id 由"内容 + 时间戳"决定，
        第二条会被静默吞掉，append-only 台账就丢了一次提案。
        """
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        first = _propose(self.conn, candidate, input_fingerprint="1" * 64,
                         created_at="2026-10-05T01:00:00+00:00")
        second = _propose(self.conn, candidate, input_fingerprint="1" * 64,
                          created_at="2026-10-05T01:00:00+00:00")
        self.assertNotEqual(first, second)
        self.assertEqual(2, len(SCRepo.list_proposals(self.conn, candidate.candidate_id)))

    def test_c6e_proposal_event_identity_survives_process_local_identity_reset(self):
        """C6e —— proposal event identity 不依赖任何 process-local 权威。

        场景：同一个 candidate、同一个 input_fingerprint、**完全相同的 proposal
        payload 与完全相同的 ``created_at``**，连续提出两次；中间把进程级状态
        清空（模拟进程重启 / 多 worker 各自从 1 开始）。

        契约：proposal_id 是**事件身份**，不是内容指纹。即使时间戳一模一样，
        两次也必须是两个不同的 id、两行记录。测试刻意固定 ``created_at``，
        不 sleep、不指望系统时钟产生不同微秒。
        """
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        created_at = "2026-10-05T01:00:00+00:00"
        first = _propose(self.conn, candidate, input_fingerprint="1" * 64,
                         created_at=created_at)
        # 进程级状态复位：任何 process-local 计数器 / 缓存都从头开始。
        importlib.reload(SCRepo)
        self.assertIs(SCRepo, sys.modules["strategy_candidate_repository"])
        second = _propose(self.conn, candidate, input_fingerprint="1" * 64,
                          created_at=created_at)
        self.assertNotEqual(first, second)
        self.assertEqual(64, len(first))
        self.assertEqual(64, len(second))
        self.assertTrue(SC._SHA256.fullmatch(first))
        self.assertTrue(SC._SHA256.fullmatch(second))
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_proposals").fetchone()[0])
        history = SCRepo.list_proposals(self.conn, candidate.candidate_id)
        self.assertEqual(2, len(history))
        self.assertEqual({first, second}, {item["proposal_id"] for item in history})
        # created_at 只是事件时间戳：两条记录可以合法地完全相同。
        self.assertEqual({created_at}, {item["created_at"] for item in history})

    def test_c6f_unexpected_proposal_id_collision_fails_closed(self):
        """C6f —— event identity 冲突必须 fail closed，不能静默假装成功。

        人为把 event identity generator 钉成同一个 id：第二次写入必须是 RED
        （``sqlite3.IntegrityError``），绝不能 ``INSERT OR IGNORE`` → 返回旧
        proposal → 假装第二次提案已经记录。
        """
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        created_at = "2026-10-05T01:00:00+00:00"
        with mock.patch.object(SCRepo, "_proposal_event_identity",
                               return_value=(created_at, "e" * 64)):
            _propose(self.conn, candidate, input_fingerprint="1" * 64,
                     created_at=created_at)
            with self.assertRaises(sqlite3.IntegrityError):
                _propose(self.conn, candidate, input_fingerprint="2" * 64,
                         created_at=created_at)
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM strategy_candidate_proposals").fetchone()[0])
        history = SCRepo.list_proposals(self.conn, candidate.candidate_id)
        self.assertEqual(["1" * 64], [item["input_fingerprint"] for item in history])

    def test_c6g_proposal_identity_is_not_a_content_fingerprint(self):
        """proposal_id 是 opaque 事件 id，不是 proposal 内容指纹。

        结构上也必须是两套契约：candidate 行按 canonical fingerprint 去重
        （``INSERT OR IGNORE`` 保留），proposal 行是事件追加（**没有** ``INSERT OR
        IGNORE``），且身份不再依赖 process-local 计数器。
        """
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        created_at = "2026-10-05T01:00:00+00:00"
        proposal_id = _propose(self.conn, candidate, input_fingerprint="1" * 64,
                               created_at=created_at)
        row = self.conn.execute(
            "SELECT proposal_json,created_at FROM strategy_candidate_proposals"
            " WHERE proposal_id=?", (proposal_id,)).fetchone()
        content_fingerprint = SC._sha({"candidate_id": candidate.candidate_id,
                                       "proposal": json.loads(str(row[0])),
                                       "created_at": str(row[1])})
        self.assertNotEqual(content_fingerprint, proposal_id)
        with open(os.path.join(BACKEND, "strategy_candidate_repository.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        # process-local identity authority 已彻底移除。
        self.assertNotIn("itertools", source)
        self.assertNotIn("_PROPOSAL_SEQUENCE", source)
        self.assertNotIn("event_sequence", source)
        # 事件表禁止 INSERT OR IGNORE（碰撞必须报错，不能静默吞事件）。
        self.assertNotIn("INSERT OR IGNORE INTO strategy_candidate_proposals", source)
        # candidate 表的内容去重权威保持不变。
        self.assertIn("INSERT OR IGNORE INTO strategy_candidates", source)

    def test_c6c_dedup_authority_is_the_fingerprint_not_the_name_or_time(self):
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate, created_at="2026-10-05T01:00:00+00:00")
        SCRepo.append_candidate(self.conn, candidate, created_at="2027-01-01T09:00:00+00:00")
        row = self.conn.execute(
            "SELECT candidate_id,created_at FROM strategy_candidates").fetchone()
        self.assertEqual(candidate.candidate_id, row[0])
        # 后一次提案没有覆盖第一次的时间戳：展示材料不构成去重权威。
        self.assertEqual("2026-10-05T01:00:00+00:00", row[1])

    def test_c7_candidate_rows_cannot_be_updated_or_deleted(self):
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE strategy_candidates SET candidate_json='{}'"
                              " WHERE candidate_id=?", (candidate.candidate_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM strategy_candidates WHERE candidate_id=?",
                              (candidate.candidate_id,))

    def test_c7b_same_id_with_different_content_is_a_conflict_not_an_overwrite(self):
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        self.conn.execute("DROP TRIGGER strategy_candidates_no_update")
        # 直接改内容但保留 id：读回时必须冲突（指纹自证失败）。
        self.conn.execute("UPDATE strategy_candidates SET candidate_json=? WHERE candidate_id=?",
                          (json.dumps({**candidate.projection(), "asof": "2099-01-01"},
                                      sort_keys=True, separators=(",", ":")),
                           candidate.candidate_id))
        with self.assertRaises(SCRepo.StrategyCandidateRepositoryError):
            SCRepo.get_candidate(self.conn, candidate.candidate_id)

    def test_c8_persistence_round_trip_reverifies_the_fingerprint(self):
        created = _seed_registry(self.conn)
        result = SCV.generate_and_record_candidates(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05",
            parameter_adjustments={"ma_period": [19, 21]},
            universe_spec={"scope_kind": "a_share_board" and "a_share_boards",
                           "boards": ["main_board"]},
            intended_market_regime="momentum", evidence_count=10,
            hypothesis_id="hyp_1",
            research_provenance={"source_kind": "human", "source_identity": "analyst-a"})
        self.assertEqual(2, result["candidate_count"])
        for candidate_id in result["candidate_ids"]:
            read = SCV.get_candidate(self.conn, candidate_id)
            projection = read["candidate"]
            rebuilt = SC.candidate_from_projection(projection)
            self.assertTrue(SC.verify_candidate_fingerprint(rebuilt))
            self.assertEqual(candidate_id, rebuilt.candidate_id)
            self.assertEqual(1, rebuilt.parent_strategy_version)
            self.assertEqual(created.current_checksum, rebuilt.parent_strategy_checksum)
            self.assertEqual("CANDIDATE", read["status"])
            # 生成路径不发布任何评估 / 晋级结论。
            self.assertIsNone(read["evaluation"])
            self.assertIsNone(read["promotion"])
            self.assertTrue(read["proposals"])

    def test_c8b_schema_itself_refuses_an_id_that_is_not_the_fingerprint(self):
        """身份就是指纹：连 schema 都不接受 ``candidate_id != fingerprint``。"""
        candidate = self._candidates()[0]
        SCRepo.append_candidate(self.conn, candidate)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE strategy_candidates SET candidate_fingerprint=? WHERE candidate_id=?",
                ("c" * 64, candidate.candidate_id))
        self.conn.execute("DROP TRIGGER strategy_candidates_no_update")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE strategy_candidates SET candidate_id=? WHERE candidate_id=?",
                ("c" * 64, candidate.candidate_id))
        # 原行完好无损：被拒绝的写入没有留下半个候选。
        self.assertTrue(SC.verify_candidate_fingerprint(
            SCRepo.get_candidate(self.conn, candidate.candidate_id)))

    def test_c8c_explicit_candidate_id_is_required(self):
        with self.assertRaises(SCRepo.StrategyCandidateRepositoryError):
            SCRepo.get_candidate(self.conn, "")
        with self.assertRaises(SCRepo.StrategyCandidateRepositoryError):
            SCRepo.get_candidate(self.conn, "short")

    def test_c8d_unknown_candidate_is_absent_not_the_latest_one(self):
        self._candidates()
        self.assertIsNone(SCRepo.get_candidate(self.conn, "d" * 64))
        with self.assertRaises(SCV.StrategyCandidateUnavailable):
            SCV.get_candidate(self.conn, "d" * 64)


class AuthorityBoundaryTests(_LedgerFixture):
    """C9 —— the generator production path owns no promotion / execution authority."""

    #: 纯域/证据模块：连"取连接"的权限都没有，因此**不得** import paper_trading。
    PURE_MODULES = ("strategy_candidate.py", "strategy_generator.py",
                    "strategy_candidate_repository.py")
    #: application service 是连接 owner（与 strategy_health_service /
    #: strategy_retirement_service 同一模式），允许 paper_trading 作为连接出口。
    SERVICE_MODULE = "strategy_candidate_service.py"
    PRODUCTION_MODULES = PURE_MODULES + (SERVICE_MODULE,)

    #: 这些模块出现即意味着生成域拿到了它明确不该有的权限。
    FORBIDDEN_IMPORTS = frozenset({
        "execution_planner", "execution_dispatch", "execution_lifecycle",
        "order_intent", "strategy_lifecycle", "strategy_promotion", "strategy_champion",
        "evolution_apply", "manual_orders", "api_strategies", "fastapi", "main",
    })

    def _tree(self, name):
        with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
            return ast.parse(handle.read(), filename=name)

    def _roots(self, name):
        roots = set()
        for node in ast.walk(self._tree(name)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_c9_generator_modules_have_no_promotion_or_execution_dependency(self):
        for name in self.PRODUCTION_MODULES:
            leaked = sorted(self._roots(name) & self.FORBIDDEN_IMPORTS)
            self.assertEqual([], leaked,
                             f"{name} 获得了 promotion/execution 依赖：{leaked}")

    def test_c9b_pure_candidate_domain_never_takes_a_connection(self):
        """纯域只接受**调用方传入**的 conn，不自己开库、不 import paper_trading。

        ``strategy_candidate_repository`` 是持久化层，允许 import sqlite3（它只按
        显式 candidate ID 读写自己的两张表）；``strategy_candidate`` 与
        ``strategy_generator`` 是纯契约，连 sqlite3 都不该出现。
        """
        for name in ("strategy_candidate.py", "strategy_generator.py"):
            roots = self._roots(name)
            for forbidden in ("paper_trading", "paper_storage", "sqlite3"):
                self.assertNotIn(forbidden, roots, f"{name} -> {forbidden}")
        repository = self._roots("strategy_candidate_repository.py")
        for forbidden in ("paper_trading", "paper_storage"):
            self.assertNotIn(forbidden, repository,
                             f"strategy_candidate_repository.py -> {forbidden}")

    def test_c9c_generator_never_writes_a_lifecycle_state(self):
        """以 AST 为准（docstring 里说明"不 import lifecycle"不算违规）。"""
        for name in self.PRODUCTION_MODULES:
            tree = self._tree(name)
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            calls |= {node.func.id for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            for forbidden in ("transition", "allows_formal_cycle", "activate",
                              "promote", "submit_order", "place_order", "commit_fill"):
                self.assertNotIn(forbidden, calls, f"{name} 调用了 {forbidden}")
            constants = {node.value for node in ast.walk(tree)
                         if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            self.assertNotIn("PRODUCTION_SIM", constants, name)

    def test_c9d_candidate_ledger_holds_no_evaluation_or_promotion_columns(self):
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(strategy_candidates)")}
        self.assertEqual(set(), columns & SC.FORBIDDEN_EVALUATION_KEYS)
        for forbidden in ("sharpe", "return", "drawdown", "win_rate", "promotion"):
            self.assertFalse([name for name in columns if forbidden in name.lower()],
                             forbidden)

    def test_c9e_repository_touches_only_its_own_tables(self):
        tree = self._tree("strategy_candidate_repository.py")
        tables = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for word in node.value.replace("\n", " ").split():
                if word in ("strategy_candidates", "strategy_candidate_proposals"):
                    tables.add(word)
        self.assertEqual({"strategy_candidates", "strategy_candidate_proposals"}, tables)

    def test_c9f_no_runtime_create_or_alter_table_in_the_generator_path(self):
        for name in self.PRODUCTION_MODULES:
            with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
                source = handle.read().upper()
            for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE"):
                self.assertNotIn(forbidden, source, f"{name} 在运行时建表/改表")


class DeterminismTests(_LedgerFixture):
    """Generator determinism and immutability of its inputs."""

    def test_same_input_same_candidates_in_the_same_order(self):
        first = self._candidates()
        second = self._candidates()
        self.assertEqual([item.candidate_id for item in first],
                         [item.candidate_id for item in second])
        self.assertEqual([item.projection() for item in first],
                         [item.projection() for item in second])

    def test_generation_does_not_mutate_the_pinned_parent(self):
        pin = _pin()
        before = json.dumps(pin.projection(), sort_keys=True)
        self._candidates(pin=pin)
        self.assertEqual(before, json.dumps(pin.projection(), sort_keys=True))

    def test_variant_order_is_input_order_not_a_search(self):
        ascending = self._candidates(adjustments={"ma_period": [19, 20, 21]})
        shuffled = self._candidates(adjustments={"ma_period": [21, 19, 20]})
        # 身份集合相同（同一组规格），顺序差异不制造新候选。
        self.assertEqual({item.candidate_id for item in ascending},
                         {item.candidate_id for item in shuffled})

    def test_duplicate_declared_values_collapse_to_one_candidate(self):
        candidates = self._candidates(adjustments={"ma_period": [19, 19, 20]})
        self.assertEqual(2, len(candidates))
        self.assertEqual(2, len({item.candidate_id for item in candidates}))

    def test_input_fingerprint_is_stable_and_content_bound(self):
        """search-space fingerprint 是 canonical 的：同一空间 → 同一指纹。"""
        first = _search_space()
        second = _search_space()
        self.assertEqual(first.fingerprint, second.fingerprint)
        moved = _search_space(asof="2026-10-06")
        self.assertNotEqual(first.fingerprint, moved.fingerprint)

    def test_ledger_metadata_never_enters_the_fingerprint(self):
        candidate = self._candidates()[0]
        self.assertNotIn("created_at", candidate.fingerprint_material())
        self.assertNotIn("row_id", candidate.fingerprint_material())
        self.assertEqual(
            hashlib.sha256(json.dumps(candidate.fingerprint_material(), sort_keys=True,
                                      separators=(",", ":"), ensure_ascii=False)
                           .encode("utf-8")).hexdigest(),
            candidate.candidate_fingerprint)


class ReadModelTests(_LedgerFixture):
    """The read model publishes backend candidate facts and nothing more."""

    def test_read_model_exposes_only_candidate_facts(self):
        created = _seed_registry(self.conn)
        result = SCV.generate_and_record_candidates(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05",
            parameter_adjustments={"ma_period": [19]},
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum", evidence_count=10)
        candidate_id = result["candidate_ids"][0]
        read = SCV.get_candidate(self.conn, candidate_id)
        self.assertEqual({"authority", "candidate", "persistence", "status",
                          "parent_strategy_pin", "proposals", "evaluation",
                          "promotion"}, set(read))
        self.assertEqual(
            set(SC.candidate_projection_keys()) | {"candidate_schema_version"},
            set(read["candidate"]))
        # created_at 是持久化事实，不属于候选身份。
        self.assertIn("created_at", read["persistence"])
        self.assertNotIn("created_at", read["candidate"])
        for forbidden in ("sharpe", "annual_return", "max_drawdown", "win_rate",
                          "promotion_result"):
            self.assertNotIn(forbidden, json.dumps(read).lower())

    def test_listing_is_bound_to_one_exact_parent_pin(self):
        created = _seed_registry(self.conn)
        SCV.generate_and_record_candidates(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum, asof="2026-10-05",
            parameter_adjustments={"ma_period": [19]},
            universe_spec={"scope_kind": "a_share_all"},
            intended_market_regime="momentum", evidence_count=10)
        listed = SCV.list_candidates_for_parent(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=created.current_checksum)
        self.assertEqual(1, len(listed["items"]))
        self.assertEqual(created.current_checksum,
                         listed["parent_strategy_pin"]["strategy_checksum"])
        self.assertIn("created_at", listed["items"][0]["persistence"])
        self.assertEqual(created.current_checksum,
                         listed["items"][0]["candidate"]["parent_strategy_checksum"])
        other = SCV.list_candidates_for_parent(
            self.conn, strategy_id=PARENT, strategy_version=1,
            strategy_checksum=CHECKSUM_B)
        self.assertEqual([], other["items"])


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _reverse_mapping_keys(value):
    """只反转**对象 key 顺序**，保留 list 顺序（list 顺序是语义）。"""
    if isinstance(value, Mapping):
        return {key: _reverse_mapping_keys(value[key])
                for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_mapping_keys(item) for item in value]
    return value


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
