# -*- coding: utf-8 -*-
"""Constrained strategy generator boundary（R35-A）.

本模块拥有**唯一**的生成权限：``GeneratorInput → StrategyCandidate``。

它明确**不拥有**：

* promotion / lifecycle 权限 —— 不 import ``strategy_lifecycle`` / ``strategy_promotion``，
  不写任何 lifecycle 状态；
* execution 权限 —— 不 import ``paper_trading`` / ``execution_*`` / ``order_intent``，
  不下单、不成交、不碰正式账本；
* "当前状态"读取权 —— 没有 DB 连接、没有 registry、没有 ``datetime.now()`` 业务
  as-of、没有 latest/current 查询。凡是要参与生成的事实，都必须由上层在
  :class:`GeneratorInput` 里**显式传入**（explicit facts in, candidate out）。

R35-A 只实现一个**确定性**的 parameter variant generator，用来证明整条链路可工作::

    generator → candidate → fingerprint → persistence → reload

它不追求"找到赚钱策略"：本阶段的目标是可信的生成基础设施，不是策略研究结论。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import strategy_candidate as SC
import strategy_dsl_schema as DSL
import strategy_parameter_schema as SPS

#: generator 契约版本：``GeneratorInput`` 的形状变化必须递增。
GENERATOR_CONTRACT_VERSION = "strategy-generator-contract-v1"

#: R35-A 唯一的 generator 类型与语义版本。
PARAMETER_VARIANT_GENERATOR = "parameter_variant"
PARAMETER_VARIANT_VERSION = "v1"

#: 一次生成请求最多产出多少候选。搜索空间上限是**输入契约**的一部分，
#: 不是"运行时随手截断"：超限一律拒绝，绝不静默丢弃候选。
MAX_VARIANTS_PER_INPUT = 64


class StrategyGeneratorError(ValueError):
    """Stable rejection from the generator boundary (always fail closed)."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise StrategyGeneratorError("generator_fact_not_serializable")


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _text(value, label: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise StrategyGeneratorError(f"{label}_is_required")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise StrategyGeneratorError(f"{label}_is_required")
        return None
    return text


@dataclass(frozen=True, slots=True)
class ParentStrategyPin:
    """A parent strategy pinned to an **immutable** exact version.

    只记 ``strategy_id`` 是不够的：那样未来会从 current registry 重新解释"当时
    用的是哪一版"。``strategy_version`` + ``strategy_checksum`` 让"哪一版、什么
    内容"同时被钉住，``dsl_ast`` 则是**那一版**读出来的定义。

    Authority 分工：本类只**记录** pin，不验证它。checksum 的权威在
    ``strategy_registry``（它自己会对 exact version + checksum 不符直接报错），
    验证发生在 application service（``strategy_candidate_service``）——它按显式
    version + checksum 读 registry，再把读到的定义冻结成 pin。纯生成域因此既不
    依赖 registry，也不可能"顺手去查一下 current"。
    """

    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    dsl_ast: Mapping
    asof: str
    research_provenance: Mapping | None = None
    universe_spec: Mapping | None = None
    intended_market_regime: str | None = None
    constraints: Mapping | None = None
    exit_spec: Mapping | None = None
    factor_spec: Mapping | None = None

    def __post_init__(self):
        strategy_id = _text(self.strategy_id, "parent_strategy_id")
        if not SC._IDENT.fullmatch(strategy_id):
            raise StrategyGeneratorError("parent_strategy_id_is_not_a_canonical_identifier")
        if isinstance(self.strategy_version, bool) or not isinstance(self.strategy_version, int) \
                or self.strategy_version < 1:
            raise StrategyGeneratorError("parent_strategy_version_must_be_a_positive_integer")
        checksum = _text(self.strategy_checksum, "parent_strategy_checksum")
        if not SC._SHA256.fullmatch(checksum):
            raise StrategyGeneratorError("parent_strategy_checksum_must_be_a_sha256_hex_digest")
        if not isinstance(self.dsl_ast, Mapping):
            raise StrategyGeneratorError("parent_dsl_ast_must_be_an_object")
        try:
            DSL.normalize(self.dsl_ast)
        except DSL.StrategyDslValidationError as exc:
            raise StrategyGeneratorError(f"parent_dsl_ast_rejected:{exc}") from exc
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "strategy_checksum", checksum)
        object.__setattr__(self, "asof", SC._asof_day(self.asof))
        object.__setattr__(self, "dsl_ast", _freeze(DSL.normalize(self.dsl_ast)))
        object.__setattr__(self, "research_provenance",
                           None if self.research_provenance is None
                           else _freeze(dict(self.research_provenance)))
        object.__setattr__(self, "universe_spec",
                           None if self.universe_spec is None
                           else _freeze(dict(self.universe_spec)))
        object.__setattr__(self, "constraints",
                           None if self.constraints is None
                           else _freeze(dict(self.constraints)))
        object.__setattr__(self, "exit_spec",
                           None if self.exit_spec is None else _freeze(dict(self.exit_spec)))
        object.__setattr__(self, "factor_spec",
                           None if self.factor_spec is None else _freeze(dict(self.factor_spec)))
        object.__setattr__(self, "intended_market_regime",
                           _text(self.intended_market_regime, "intended_market_regime",
                                 required=False))

    @property
    def identity(self) -> dict:
        return {"strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "strategy_checksum": self.strategy_checksum}

    def projection(self) -> dict:
        return {"strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "strategy_checksum": self.strategy_checksum,
                "asof": self.asof,
                "dsl_ast": _thaw(self.dsl_ast)}


@dataclass(frozen=True, slots=True)
class GeneratorInput:
    """The complete, frozen set of facts a generator is allowed to consume.

    There is deliberately no DB handle, no registry, no clock and no "current"
    selector on this object: anything not passed in here simply does not exist
    for the generator.
    """

    parent_pin: ParentStrategyPin
    parameter_adjustments: Mapping
    universe_spec: Mapping
    intended_market_regime: str
    asof: str
    generator_type: str = PARAMETER_VARIANT_GENERATOR
    generator_version: str = PARAMETER_VARIANT_VERSION
    evidence_count: int | None = None
    hypothesis_id: str | None = None
    research_provenance: Mapping | None = None
    random_seed: int | None = None
    model_identity: Mapping | None = None
    constraints: Mapping | None = None

    def __post_init__(self):
        if not isinstance(self.parent_pin, ParentStrategyPin):
            raise StrategyGeneratorError("explicit_pinned_parent_strategy_is_required")
        if not isinstance(self.parameter_adjustments, Mapping) or not self.parameter_adjustments:
            raise StrategyGeneratorError("parameter_adjustments_are_required")
        adjustments = {}
        for parameter_id, values in self.parameter_adjustments.items():
            name = str(parameter_id)
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise StrategyGeneratorError(
                    f"parameter_variant_values_must_be_a_list:{name}")
            collected = []
            for value in values:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise StrategyGeneratorError(
                        f"parameter_variant_value_must_be_numeric:{name}")
                if float(value) != float(value) or float(value) in (
                        float("inf"), float("-inf")):
                    raise StrategyGeneratorError(
                        f"parameter_variant_value_must_be_finite:{name}")
                collected.append(value)
            if not collected:
                raise StrategyGeneratorError(f"parameter_variant_values_are_empty:{name}")
            adjustments[name] = tuple(collected)
        total = 1
        for values in adjustments.values():
            total *= len(values)
        if total > MAX_VARIANTS_PER_INPUT:
            raise StrategyGeneratorError("parameter_variant_space_exceeds_the_declared_maximum")
        seed = self.random_seed
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise StrategyGeneratorError("random_seed_must_be_an_integer_or_null")
        generator_type = _text(self.generator_type, "generator_type")
        if generator_type != PARAMETER_VARIANT_GENERATOR:
            raise StrategyGeneratorError("generator_type_is_not_implemented_in_r35a")
        generator_version = _text(self.generator_version, "generator_version")
        if generator_version != PARAMETER_VARIANT_VERSION:
            raise StrategyGeneratorError("generator_version_is_not_implemented_in_r35a")
        object.__setattr__(self, "generator_type", generator_type)
        object.__setattr__(self, "generator_version", generator_version)
        evidence = self.evidence_count
        if evidence is not None and (isinstance(evidence, bool) or not isinstance(evidence, int)
                                     or evidence < 0):
            raise StrategyGeneratorError("evidence_count_must_be_a_non_negative_integer")
        object.__setattr__(self, "parameter_adjustments", _freeze(adjustments))
        object.__setattr__(self, "universe_spec", _freeze(dict(self.universe_spec or {})))
        object.__setattr__(self, "intended_market_regime",
                           _text(self.intended_market_regime, "intended_market_regime"))
        object.__setattr__(self, "asof", SC._asof_day(self.asof))
        object.__setattr__(self, "hypothesis_id",
                           _text(self.hypothesis_id, "hypothesis_id", required=False))
        object.__setattr__(self, "research_provenance",
                           None if self.research_provenance is None
                           else _freeze(dict(self.research_provenance)))
        object.__setattr__(self, "model_identity",
                           None if self.model_identity is None
                           else _freeze(dict(self.model_identity)))
        object.__setattr__(self, "constraints",
                           None if self.constraints is None
                           else _freeze(dict(self.constraints)))

    def projection(self) -> dict:
        return {
            "generator_contract_version": GENERATOR_CONTRACT_VERSION,
            "generator_type": self.generator_type,
            "generator_version": self.generator_version,
            "parent_pin": dict(self.parent_pin.identity),
            "parent_asof": self.parent_pin.asof,
            "asof": self.asof,
            "parameter_adjustments": _thaw(self.parameter_adjustments),
            "evidence_count": self.evidence_count,
            "universe_spec": _thaw(self.universe_spec),
            "intended_market_regime": self.intended_market_regime,
            "hypothesis_id": self.hypothesis_id,
            "research_provenance": _thaw(self.research_provenance or {}),
            "random_seed": self.random_seed,
            "model_identity": _thaw(self.model_identity or {}),
            "constraints": _thaw(self.constraints or {}),
        }

    @property
    def input_fingerprint(self) -> str:
        """生成输入的 canonical 身份（同一输入 → 同一组候选）。"""
        return hashlib.sha256(_canonical(self.projection()).encode("utf-8")).hexdigest()


def _variant_combinations(adjustments: Mapping) -> list[dict]:
    """确定性展开参数网格：键排序 + 值保持调用方顺序（顺序是输入的一部分）。"""
    names = sorted(adjustments)
    combinations: list[dict] = [{}]
    for name in names:
        expanded = []
        for base in combinations:
            for value in adjustments[name]:
                candidate = dict(base)
                candidate[name] = value
                expanded.append(candidate)
        combinations = expanded
    return combinations


def generate_parameter_variants(
    generator_input: GeneratorInput,
) -> tuple[SC.StrategyCandidate, ...]:
    """Produce deterministic constrained parameter variants of one pinned parent.

    Deterministic: the same :class:`GeneratorInput` always yields the same
    candidates in the same order, with the same fingerprints. The parent's DSL
    **structure** can never change here — only values of parameters that the
    parent's own parameter contract already declares as editable, in-bounds and
    within ``max_step``; the existing ``strategy_parameter_schema`` owner
    enforces every one of those rules.

    Returns candidates in a stable order and never mutates the input. Duplicate
    specifications collapse to one candidate (identity, not ordering, is the
    dedup authority).
    """
    if not isinstance(generator_input, GeneratorInput):
        raise StrategyGeneratorError("canonical_generator_input_is_required")
    pin = generator_input.parent_pin
    schema = SPS.StrategyParameterSchema.from_dsl(pin.dsl_ast)
    declared = {item.parameter_id for item in schema.parameters}
    undeclared = sorted(set(generator_input.parameter_adjustments) - declared)
    if undeclared:
        raise StrategyGeneratorError(
            f"parameter_is_not_declared_by_the_pinned_parent:{undeclared[0]}")

    produced: dict[str, SC.StrategyCandidate] = {}
    for combination in _variant_combinations(generator_input.parameter_adjustments):
        try:
            application = schema.apply(pin.dsl_ast, combination,
                                       evidence_count=generator_input.evidence_count)
        except SPS.StrategyParameterAdjustmentError as exc:
            raise StrategyGeneratorError(f"parameter_variant_rejected:{exc}") from exc
        candidate = SC.build_strategy_candidate(
            parent_identity=pin.identity,
            generator_type=generator_input.generator_type,
            generator_version=generator_input.generator_version,
            generator_contract_version=GENERATOR_CONTRACT_VERSION,
            asof=generator_input.asof,
            entry_spec=application.dsl_ast,
            universe_spec=generator_input.universe_spec,
            intended_market_regime=generator_input.intended_market_regime,
            factor_spec=pin.factor_spec,
            exit_spec=pin.exit_spec,
            constraints=(generator_input.constraints
                         if generator_input.constraints is not None else pin.constraints),
            hypothesis_id=generator_input.hypothesis_id,
            research_provenance=(generator_input.research_provenance
                                 or pin.research_provenance),
            random_seed=generator_input.random_seed,
            model_identity=generator_input.model_identity,
        )
        # 去重的唯一权威是 canonical candidate identity；同一个候选被重复提出时
        # 只保留一个身份，绝不制造"语义相同但 ID 不同"的两个策略。
        produced.setdefault(candidate.candidate_id, candidate)
    return tuple(produced[key] for key in sorted(produced))


__all__ = [
    "GENERATOR_CONTRACT_VERSION", "MAX_VARIANTS_PER_INPUT",
    "PARAMETER_VARIANT_GENERATOR", "PARAMETER_VARIANT_VERSION", "GeneratorInput",
    "ParentStrategyPin", "StrategyGeneratorError", "generate_parameter_variants",
]
