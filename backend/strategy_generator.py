# -*- coding: utf-8 -*-
"""Deterministic candidate expansion over an explicit bounded search space（R35-A/B）.

一句话 authority：本模块拥有**确定性候选展开** —— 把一份已冻结的
:class:`~strategy_candidate_search_space.CandidateSearchSpace` 展开成
``StrategyCandidate`` 元组。

它明确**不拥有**：

* promotion / lifecycle 权限 —— 不 import ``strategy_lifecycle`` /
  ``strategy_promotion``，不写任何 lifecycle 状态；
* execution 权限 —— 不 import ``paper_trading`` / ``execution_*`` /
  ``order_intent``，不下单、不成交、不碰正式账本；
* evaluation 权限 —— 不 import backtest / PIT / robustness / 评估 owner，
  没有 score、没有 ranking、没有 winner selection。R35-B 只回答"能生成哪些受约束
  候选"，不回答"哪个候选更好"；
* "当前状态"读取权 —— 没有 DB 连接、没有 registry、没有 ``datetime.now()``
  业务 as-of、没有 latest/current 查询。凡是要参与生成的事实，都必须由上层在
  search space 里**显式传入**（explicit facts in, candidate out）。

R35-B 的展开能力是一个**显式 registry**，不是不断增长的 ``if/elif`` 链：每个能力
只声明"它展开哪些维度"，展开本身是同一个确定性笛卡尔展开。因此新增能力不需要碰
展开逻辑，也不可能出现"某个能力偷偷多展开一个维度"。

组合顺序不决定业务结果：``candidate_id`` 是 canonical fingerprint，输出按
``candidate_id`` 排序，声明顺序（JSON key 顺序 / 参数声明顺序 / alternative 顺序）
不改变候选集合。超过声明上限**一律拒绝**，绝不静默截断。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import strategy_candidate as SC
import strategy_candidate_search_space as SS
import strategy_parameter_schema as SPS

#: generator 契约版本：search space / 展开语义变化必须递增。
#: 它记在 **proposal 事件**与 **generation batch** 上，不参与 candidate fingerprint
#: （候选是内容身份，同一份 specification 由不同能力提出仍是同一个 candidate）。
GENERATOR_CONTRACT_VERSION = "strategy-generator-contract-v2"

#: R35-A 的旧契约版本，仅用于让历史 proposal / batch 行可读。
GENERATOR_CONTRACT_VERSION_V1 = "strategy-generator-contract-v1"

#: 各能力的语义版本。展开语义变化时必须递增（能力身份在 proposal / batch 上）。
PARAMETER_VARIANT_VERSION = "v1"
FACTOR_VARIANT_VERSION = "v1"
ENTRY_VARIANT_VERSION = "v1"
EXIT_VARIANT_VERSION = "v1"
BOUNDED_COMBINATION_VERSION = "v1"

#: generator 类型词汇（§16：不允许一个含糊的 ``generic`` 靠自由文本解释）。
PARAMETER_VARIANT_GENERATOR = "parameter_variant"
FACTOR_VARIANT_GENERATOR = "factor_variant"
ENTRY_VARIANT_GENERATOR = "entry_variant"
EXIT_VARIANT_GENERATOR = "exit_variant"
BOUNDED_COMBINATION_GENERATOR = "bounded_combination"


#: 生成边界的拒绝类型。**别名**，不是新类：``SearchSpaceError`` 继承自它，因此
#: 捕获 ``StrategyGeneratorError`` 一定能覆盖"空间声明非法"与"展开被拒"两种情况。
#: 若另立一个互不相干的类，空间拒绝会漏过 ``except StrategyGeneratorError``，
#: 把契约拒绝变成 5xx。
StrategyGeneratorError = SS.StrategyGeneratorError

#: search space 的拒绝也属于生成边界的拒绝，调用方只需捕获一个类型。
SearchSpaceError = SS.SearchSpaceError


@dataclass(frozen=True, slots=True)
class GeneratorCapability:
    """One registered expansion capability.

    ``dimensions`` 是**唯一**的能力语义声明：这个能力展开搜索空间的哪几个维度，
    其余维度各自继承（基数 1）。因此"能力"不是一段会膨胀的分支代码，而是一条
    可审计的数据。
    """

    generator_type: str
    generator_version: str
    dimensions: tuple[str, ...]

    @property
    def expands_parameters(self) -> bool:
        return SS.PARAMETER_DIMENSION in self.dimensions


#: 唯一的生成能力 registry。key 是 ``generator_type``。
GENERATOR_CAPABILITIES: dict[str, GeneratorCapability] = {
    PARAMETER_VARIANT_GENERATOR: GeneratorCapability(
        PARAMETER_VARIANT_GENERATOR, PARAMETER_VARIANT_VERSION,
        (SS.PARAMETER_DIMENSION,)),
    FACTOR_VARIANT_GENERATOR: GeneratorCapability(
        FACTOR_VARIANT_GENERATOR, FACTOR_VARIANT_VERSION, ("factor",)),
    ENTRY_VARIANT_GENERATOR: GeneratorCapability(
        ENTRY_VARIANT_GENERATOR, ENTRY_VARIANT_VERSION, ("entry",)),
    EXIT_VARIANT_GENERATOR: GeneratorCapability(
        EXIT_VARIANT_GENERATOR, EXIT_VARIANT_VERSION, ("exit",)),
    # §17：bounded_combination 只做"显式有限备选集的笛卡尔积"。
    BOUNDED_COMBINATION_GENERATOR: GeneratorCapability(
        BOUNDED_COMBINATION_GENERATOR, BOUNDED_COMBINATION_VERSION, SS.DIMENSIONS),
}

#: R35-A 的别名：parameter variant generator 的名字没变。
ParentStrategyPin = SS.ParentStrategyPin


def resolve_capability(generator_type, generator_version) -> GeneratorCapability:
    """Resolve one capability by exact type + version, or fail closed."""
    name = str(generator_type or "").strip()
    capability = GENERATOR_CAPABILITIES.get(name)
    if capability is None:
        raise StrategyGeneratorError("generator_type_is_not_implemented")
    version = str(generator_version or "").strip()
    if version != capability.generator_version:
        raise StrategyGeneratorError("generator_version_is_not_implemented")
    return capability


def _apply_parameters(entry_ast, combination, evidence_count, label: str):
    """Apply declared parameter values to one entry AST, or fail closed.

    The parameter contract is compiled by the existing owner
    (``strategy_parameter_schema``): R35-B 不复制第二套参数规则，因此 allowlist /
    bounds / ``max_step`` / locked / ``min_evidence`` 的裁决点只有一个。
    """
    if not combination:
        return entry_ast
    schema = SPS.StrategyParameterSchema.from_dsl(entry_ast)
    try:
        return schema.apply(entry_ast, combination, evidence_count=evidence_count).dsl_ast
    except SPS.StrategyParameterAdjustmentError as exc:
        raise StrategyGeneratorError(f"{label}:{exc}") from exc


def generate_candidates(
    search_space: SS.CandidateSearchSpace,
) -> tuple[SC.StrategyCandidate, ...]:
    """Expand one explicit bounded search space into deterministic candidates.

    Deterministic: the same search space always yields the same candidates in the
    same order, with the same fingerprints. The parent's DSL **structure** can never
    change here except through a slot the caller declared explicitly; parameter
    values still have to pass the parent's own parameter contract.

    Returns candidates in a canonical (``candidate_id``) order and never mutates the
    input. Duplicate specifications collapse to one candidate — identity, not
    ordering, is the dedup authority.
    """
    if not isinstance(search_space, SS.CandidateSearchSpace):
        raise StrategyGeneratorError("canonical_candidate_search_space_is_required")
    capability = resolve_capability(search_space.generator_type,
                                   search_space.generator_version)
    pin = search_space.parent_pin

    # §8：基数必须在生成**之前**算出来；超限显式拒绝，绝不生成前 N 个再截断
    # （截断会让 candidate universe 依赖遍历顺序）。
    cardinality = search_space.cardinality_for(capability.dimensions)
    if cardinality > search_space.max_candidates:
        raise StrategyGeneratorError("candidate_space_exceeds_the_declared_maximum")

    values = {dimension: search_space.dimension_values(dimension)
              for dimension in SS.DIMENSIONS}

    # 参数只允许落在**实际会被使用的** entry 上；未声明的参数在这里就拒绝，
    # 而不是等到某个组合偶然失败。
    if search_space.parameter_variants:
        declared: set[str] = set()
        for entry_ast in values["entry"]:
            declared |= {item.parameter_id for item in
                         SPS.StrategyParameterSchema.from_dsl(entry_ast).parameters}
        undeclared = sorted(set(search_space.parameter_variants) - declared)
        if undeclared:
            raise StrategyGeneratorError(
                f"parameter_is_not_declared_by_the_pinned_parent:{undeclared[0]}")

    # 能力**不展开**的维度必须只有一个取值：否则就是"声明了 3 个 entry 备选、
    # 却只用了第 1 个"的静默截断，会让 candidate universe 依赖遍历顺序 ——
    # 与超限截断是同一类错误，因此同样 fail closed，而不是悄悄取第一个。
    for dimension in SS.DIMENSIONS:
        if dimension in capability.dimensions:
            continue
        declared_arity = len(values[dimension])
        if declared_arity != 1:
            raise StrategyGeneratorError(
                f"dimension_is_not_expanded_by_this_generator:{dimension}")

    expanded_axes = [values[dimension] for dimension in capability.dimensions]
    fixed = {dimension: values[dimension][0]
             for dimension in SS.DIMENSIONS if dimension not in capability.dimensions}

    produced: dict[str, SC.StrategyCandidate] = {}
    for combination in itertools.product(*expanded_axes):
        chosen = dict(fixed)
        chosen.update(dict(zip(capability.dimensions, combination, strict=True)))
        entry_ast = _apply_parameters(chosen["entry"], chosen[SS.PARAMETER_DIMENSION],
                                      search_space.evidence_count, "parameter_variant_rejected")
        candidate = SC.build_strategy_candidate(
            parent_identity=pin.identity,
            asof=search_space.asof,
            entry_spec=entry_ast,
            universe_spec=SS._thaw(search_space.universe_spec),
            intended_market_regime=search_space.intended_market_regime,
            factor_spec=chosen["factor"],
            exit_spec=chosen["exit"],
            constraints=SS._thaw(search_space.constraints),
            hypothesis_id=search_space.hypothesis_id,
            research_provenance=(SS._thaw(search_space.research_provenance)
                                 if search_space.research_provenance is not None
                                 else (SS._thaw(pin.research_provenance)
                                       if pin.research_provenance is not None else None)),
            random_seed=search_space.random_seed,
            model_identity=(SS._thaw(search_space.model_identity)
                            if search_space.model_identity is not None else None),
        )
        # 去重的唯一权威是 canonical candidate identity；同一个候选被重复提出时
        # 只保留一个身份，绝不制造"语义相同但 ID 不同"的两个策略。
        produced.setdefault(candidate.candidate_id, candidate)
    return tuple(produced[key] for key in sorted(produced))


#: R35-A 的入口名保留：parameter variant 生成仍然是同一个函数，只是输入换成了
#: 统一的 search space contract。
generate_parameter_variants = generate_candidates


__all__ = [
    "BOUNDED_COMBINATION_GENERATOR", "BOUNDED_COMBINATION_VERSION",
    "ENTRY_VARIANT_GENERATOR", "ENTRY_VARIANT_VERSION", "EXIT_VARIANT_GENERATOR",
    "EXIT_VARIANT_VERSION", "FACTOR_VARIANT_GENERATOR", "FACTOR_VARIANT_VERSION",
    "GENERATOR_CAPABILITIES", "GENERATOR_CONTRACT_VERSION",
    "GENERATOR_CONTRACT_VERSION_V1", "GeneratorCapability",
    "PARAMETER_VARIANT_GENERATOR", "PARAMETER_VARIANT_VERSION",
    "ParentStrategyPin", "SearchSpaceError", "StrategyGeneratorError",
    "generate_candidates", "generate_parameter_variants", "resolve_capability",
]
