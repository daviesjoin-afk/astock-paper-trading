# -*- coding: utf-8 -*-
"""Explicit, bounded candidate search-space contract（R35-B）.

一句话 authority：本模块拥有**候选搜索空间是什么** —— 一个显式、有限、可
canonical fingerprint 的生成输入声明，外加它的组合基数与 slot 继承语义。

它**不**拥有：

* 展开/构造候选 —— 那是 ``strategy_generator``（deterministic expansion）；
* candidate identity —— 那是 ``strategy_candidate``（canonical fingerprint）；
* 持久化 —— 那是 ``strategy_candidate_repository``；
* 任何"当前状态"读取权 —— 没有 DB、没有 registry、没有机器时钟、没有
  latest/current 查询。父策略只能以**已冻结的 exact pin** 形式传入。

R35-B 的边界（明确不属于本模块，也不属于 R35-B）：

* 不评估候选（没有 backtest / PIT / robustness / scoring / ranking）；
* 不选 winner、不 promotion、不 lifecycle transition、不下单、不碰 allocation；
* 不做无界搜索、不做"根据上一个候选的结果决定下一个候选"。

搜索空间必须**有限**：``cardinality`` 在生成前就能算出来，超过
:data:`MAX_CANDIDATES_PER_GENERATION_REQUEST` 一律 fail closed。**绝不静默截断** ——
截断会让 candidate universe 依赖遍历顺序，那是"看起来跑完了"的假象。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import strategy_candidate as SC
import strategy_dsl_schema as DSL

#: search-space 契约版本。声明形状或组合语义变化时必须递增。
SEARCH_SPACE_CONTRACT_VERSION = "strategy-candidate-search-space-v1"

#: role slot 的三种**互斥**语义。
#:
#: 字段"为空"到底表示"没有这个规则"还是"继承 parent"，是 R35-B 最容易出歧义的
#: 地方：用 ``None`` 同时表示三件事，会让同一份 JSON 有两种读法。因此 slot 必须
#: **显式**声明 kind，缺省不存在。
INHERIT_PARENT = "inherit_parent"
EXPLICIT_VARIANT = "explicit_variant"
ABSENT = "absent"
SLOT_KINDS = (INHERIT_PARENT, EXPLICIT_VARIANT, ABSENT)
ROLE_SLOTS = ("factor", "entry", "exit")

#: 组合策略。只有"显式声明的有限集合的笛卡尔积"这一种。
CARTESIAN_COMBINATION = "cartesian"
COMBINATION_POLICIES = (CARTESIAN_COMBINATION,)

#: 一次生成请求的硬上限：generator contract 的一部分，不是运行时随手截断的阈值。
#: R35-B 要的是几十 / 低百量级**可审计**的候选集；更大的空间由 R36 search
#: controller 拆成多批，而不是在这里偷偷放宽。
MAX_CANDIDATES_PER_GENERATION_REQUEST = 128

#: 内部维度标识（不是 DSL op）。
PARAMETER_DIMENSION = "parameters"
DIMENSIONS = (PARAMETER_DIMENSION, "factor", "entry", "exit")


class StrategyGeneratorError(ValueError):
    """Stable rejection from the generation input contract (always fail closed)."""


class SearchSpaceError(StrategyGeneratorError):
    """A search-space declaration is ambiguous, unbounded, or otherwise invalid."""


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
    raise SearchSpaceError("search_space_fact_not_serializable")


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _text(value, label: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise SearchSpaceError(f"{label}_is_required")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise SearchSpaceError(f"{label}_is_required")
        return None
    return text


def _closed_mapping(value, allowed: frozenset[str], label: str) -> dict:
    if not isinstance(value, Mapping):
        raise SearchSpaceError(f"{label}_must_be_an_object")
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise SearchSpaceError(f"{label}_has_unsupported_key:{unknown[0]}")
    return {str(key): item for key, item in value.items()}


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

    R35-B 起 pin 是 search space 的一个字段：``inherit_parent`` 的 slot 与默认
    constraints 都从**这个冻结对象**解析，绝不回读 registry。
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
            raise SearchSpaceError("parent_strategy_id_is_not_a_canonical_identifier")
        if isinstance(self.strategy_version, bool) or not isinstance(self.strategy_version, int) \
                or self.strategy_version < 1:
            raise SearchSpaceError("parent_strategy_version_must_be_a_positive_integer")
        checksum = _text(self.strategy_checksum, "parent_strategy_checksum")
        if not SC._SHA256.fullmatch(checksum):
            raise SearchSpaceError("parent_strategy_checksum_must_be_a_sha256_hex_digest")
        if not isinstance(self.dsl_ast, Mapping):
            raise SearchSpaceError("parent_dsl_ast_must_be_an_object")
        try:
            DSL.normalize(self.dsl_ast)
        except DSL.StrategyDslValidationError as exc:
            raise SearchSpaceError(f"parent_dsl_ast_rejected:{exc}") from exc
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


def _slot(value, role: str) -> dict:
    """Validate one role slot declaration into its canonical form.

    ``entry`` 只能是 ``inherit_parent`` 或 ``explicit_variant``：candidate 契约要求
    entry_spec 必需，"没有 entry 的候选"不是一个可验证的候选。factor / exit 三者皆可。
    """
    declared = _closed_mapping(value, frozenset({"kind", "alternatives"}),
                               f"{role}_slot")
    kind = _text(declared.get("kind"), f"{role}_slot_kind")
    if kind not in SLOT_KINDS:
        raise SearchSpaceError(f"{role}_slot_kind_is_not_allowlisted")
    if kind == INHERIT_PARENT:
        if "alternatives" in declared:
            raise SearchSpaceError(f"{role}_slot_inherit_takes_no_alternatives")
        return {"kind": INHERIT_PARENT}
    if kind == ABSENT:
        if "alternatives" in declared:
            raise SearchSpaceError(f"{role}_slot_absent_takes_no_alternatives")
        if role == "entry":
            raise SearchSpaceError("entry_slot_cannot_be_absent")
        return {"kind": ABSENT}
    alternatives = declared.get("alternatives")
    if isinstance(alternatives, (str, bytes)) or not isinstance(alternatives, Sequence) \
            or not alternatives:
        raise SearchSpaceError(f"{role}_slot_requires_a_non_empty_alternative_list")
    normalized = []
    for ast in alternatives:
        # 每一个 alternative 都必须单独通过既有 bounded DSL 契约：R35-B 不发明
        # 第二套表达式语言，也不接受 Python / eval / exec / 自由源码。
        try:
            normalized_ast = SC._rule_spec(ast, role, allow_strategy_root=(role == "entry"))
        except SC.CandidateValidationError as exc:
            raise SearchSpaceError(f"{role}_alternative_rejected:{exc}") from exc
        normalized.append(_canonical(normalized_ast))
    # 去重 + canonical 排序：alternative 是**集合**语义，声明顺序不承载语义。
    unique = sorted(set(normalized))
    return {"kind": EXPLICIT_VARIANT, "alternatives": unique}


def _parameter_variants(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SearchSpaceError("parameter_variants_must_be_an_object")
    collected = {}
    for parameter_id, values in value.items():
        name = str(parameter_id)
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise SearchSpaceError(f"parameter_variant_values_must_be_a_list:{name}")
        unique = set()
        for item in values:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise SearchSpaceError(f"parameter_variant_value_must_be_numeric:{name}")
            number = float(item)
            if number != number or number in (float("inf"), float("-inf")):
                raise SearchSpaceError(f"parameter_variant_value_must_be_finite:{name}")
            unique.add(item)
        if not unique:
            raise SearchSpaceError(f"parameter_variant_values_are_empty:{name}")
        # 值也排序：同一组取值无论声明顺序如何，都得到同一个 canonical 空间。
        collected[name] = tuple(sorted(unique, key=float))
    return collected


def _tightened_constraints(parent_constraints, override) -> dict:
    """Resolve candidate constraints under "inherit, or only tighten".

    R35-A 已保证"没给 override 时继承 exact pinned parent 的 constraints"。R35-B
    再多一条：**给了 override 也只能收紧，不能放宽**。放开仓位 / 敞口 / 权重上限
    是风险放大动作，必须走正式 risk evidence gate（``asymmetric_risk`` 的观察期），
    不属于 candidate generator 的权限。这里没有那个 gate，因此 fail closed。
    """
    inherited = SC._constraints(parent_constraints)
    if override is None:
        return inherited
    declared = SC._constraints(override)
    for key, parent_value in inherited.items():
        if key not in declared:
            # override 缺了父策略有的边界 = 把该边界丢掉 = 放宽。
            raise SearchSpaceError(f"constraint_override_would_drop_parent_bound:{key}")
        if declared[key] > parent_value:
            raise SearchSpaceError(f"constraint_override_would_expand_risk:{key}")
    return declared


@dataclass(frozen=True, slots=True)
class CandidateSearchSpace:
    """The complete, frozen, finite set of facts a generator is allowed to expand.

    There is deliberately no DB handle, no registry, no clock and no "current"
    selector on this object: anything not passed in here simply does not exist for
    the generator.
    """

    parent_pin: ParentStrategyPin
    asof: str
    universe_spec: Mapping
    intended_market_regime: str
    #: generator 能力身份（§7 / §16）：**显式**声明，不是从别处推出来的。能力
    #: 类型与语义版本都是封闭词汇，绝不使用一个含糊的 ``generic`` 再靠自由文本解释。
    generator_type: str = ""
    generator_version: str = ""
    parameter_variants: Mapping = field(default_factory=dict)
    factor_slot: Mapping = field(default_factory=lambda: {"kind": INHERIT_PARENT})
    entry_slot: Mapping = field(default_factory=lambda: {"kind": INHERIT_PARENT})
    exit_slot: Mapping = field(default_factory=lambda: {"kind": INHERIT_PARENT})
    combination_policy: str = CARTESIAN_COMBINATION
    max_candidates: int = MAX_CANDIDATES_PER_GENERATION_REQUEST
    evidence_count: int | None = None
    hypothesis_id: str | None = None
    research_provenance: Mapping | None = None
    random_seed: int | None = None
    model_identity: Mapping | None = None
    constraints: Mapping | None = None
    search_space_contract_version: str = SEARCH_SPACE_CONTRACT_VERSION

    def __post_init__(self):
        if not isinstance(self.parent_pin, ParentStrategyPin):
            raise SearchSpaceError("explicit_pinned_parent_strategy_is_required")
        pin = self.parent_pin
        object.__setattr__(self, "generator_type",
                           _text(self.generator_type, "generator_type"))
        object.__setattr__(self, "generator_version",
                           _text(self.generator_version, "generator_version"))
        object.__setattr__(self, "asof", SC._asof_day(self.asof))
        if not isinstance(self.universe_spec, Mapping) or not self.universe_spec:
            raise SearchSpaceError("explicit_universe_spec_required")
        object.__setattr__(self, "universe_spec", _freeze(dict(self.universe_spec)))
        object.__setattr__(self, "intended_market_regime",
                           _text(self.intended_market_regime, "intended_market_regime"))
        object.__setattr__(self, "parameter_variants",
                           _freeze(_parameter_variants(self.parameter_variants)))
        for role in ROLE_SLOTS:
            object.__setattr__(self, f"{role}_slot", _freeze(_slot(
                getattr(self, f"{role}_slot"), role)))
        policy = _text(self.combination_policy, "combination_policy")
        if policy not in COMBINATION_POLICIES:
            raise SearchSpaceError("combination_policy_is_not_allowlisted")
        object.__setattr__(self, "combination_policy", policy)
        budget = self.max_candidates
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise SearchSpaceError("max_candidates_must_be_a_positive_integer")
        if budget > MAX_CANDIDATES_PER_GENERATION_REQUEST:
            # 调用方只能**收紧**上限，不能放宽契约上限。
            raise SearchSpaceError("max_candidates_exceeds_the_declared_contract_maximum")
        object.__setattr__(self, "max_candidates", int(budget))
        evidence = self.evidence_count
        if evidence is not None and (isinstance(evidence, bool) or not isinstance(evidence, int)
                                     or evidence < 0):
            raise SearchSpaceError("evidence_count_must_be_a_non_negative_integer")
        seed = self.random_seed
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise SearchSpaceError("random_seed_must_be_an_integer_or_null")
        object.__setattr__(self, "hypothesis_id",
                           _text(self.hypothesis_id, "hypothesis_id", required=False))
        object.__setattr__(self, "research_provenance",
                           None if self.research_provenance is None
                           else _freeze(dict(self.research_provenance)))
        object.__setattr__(self, "model_identity",
                           None if self.model_identity is None
                           else _freeze(dict(self.model_identity)))
        # constraints 在这里就解析成**最终值**：继承 exact pinned parent，或只能收紧。
        # 候选因此永远不携带 "inherit-current-parent" 这种需要未来回读 registry 的语义。
        resolved = _tightened_constraints(pin.constraints, self.constraints)
        object.__setattr__(self, "constraints", _freeze(resolved))
        object.__setattr__(self, "search_space_contract_version",
                           _text(self.search_space_contract_version,
                                 "search_space_contract_version"))

    # ---- 维度取值 -------------------------------------------------------

    def dimension_values(self, dimension: str) -> tuple:
        """Return the finite, canonical value list of one dimension.

        ``inherit_parent`` 在**这里**就解析成那一版父策略的最终语义（entry 是父
        dsl_ast、factor / exit 是 pin 上冻结的 spec），所以继承永远不会变成
        "未来回读 registry" 的悬空引用。``absent`` 是显式的"本候选没有这个角色"，
        与"继承"严格区分。
        """
        if dimension == PARAMETER_DIMENSION:
            combinations: list[dict] = [{}]
            for name in sorted(self.parameter_variants):
                expanded = []
                for base in combinations:
                    for value in self.parameter_variants[name]:
                        combination = dict(base)
                        combination[name] = value
                        expanded.append(combination)
                combinations = expanded
            return tuple(combinations)
        if dimension == "entry":
            slot, inherited = self.entry_slot, _thaw(self.parent_pin.dsl_ast)
        elif dimension == "factor":
            slot, inherited = self.factor_slot, (
                None if self.parent_pin.factor_spec is None
                else _thaw(self.parent_pin.factor_spec))
        elif dimension == "exit":
            slot, inherited = self.exit_slot, (
                None if self.parent_pin.exit_spec is None
                else _thaw(self.parent_pin.exit_spec))
        else:
            raise SearchSpaceError(f"unknown_search_space_dimension:{dimension}")
        if slot["kind"] == INHERIT_PARENT:
            return (inherited,)
        if slot["kind"] == ABSENT:
            return (None,)
        return tuple(json.loads(item) for item in slot["alternatives"])

    def dimension_cardinality(self, dimension: str) -> int:
        return len(self.dimension_values(dimension))

    def cardinality_for(self, dimensions) -> int:
        """Cartesian cardinality of the dimensions one capability actually expands.

        ``bounded_combination`` 展开全部四个维度（§8 的 3×4×2×3×2 例子），而
        ``factor_variant`` 只展开 factor —— 其余维度各自继承父策略，因此基数是 1。
        """
        total = 1
        for dimension in dimensions:
            if dimension not in DIMENSIONS:
                raise SearchSpaceError(f"unknown_search_space_dimension:{dimension}")
            total *= self.dimension_cardinality(dimension)
        return total

    @property
    def cardinality(self) -> int:
        """Full Cartesian cardinality of the declared space (all four dimensions)."""
        return self.cardinality_for(DIMENSIONS)

    def projection(self) -> dict:
        return {
            "search_space_contract_version": self.search_space_contract_version,
            "parent_pin": dict(self.parent_pin.identity),
            "parent_asof": self.parent_pin.asof,
            "asof": self.asof,
            "generator_type": self.generator_type,
            "generator_version": self.generator_version,
            "universe_spec": _thaw(self.universe_spec),
            "intended_market_regime": self.intended_market_regime,
            "parameter_variants": {name: list(values)
                                   for name, values in sorted(self.parameter_variants.items())},
            "factor_slot": _thaw(self.factor_slot),
            "entry_slot": _thaw(self.entry_slot),
            "exit_slot": _thaw(self.exit_slot),
            "combination_policy": self.combination_policy,
            "evidence_count": self.evidence_count,
            "hypothesis_id": self.hypothesis_id,
            "research_provenance": _thaw(self.research_provenance or {}),
            "random_seed": self.random_seed,
            "model_identity": _thaw(self.model_identity or {}),
            "constraints": _thaw(self.constraints or {}),
        }

    @property
    def fingerprint(self) -> str:
        """The search space's canonical identity.

        ``max_candidates`` 是**预算**而不是空间语义：同一个空间配不同预算展开出的
        候选集合相同，因此它刻意不进指纹（否则同一输入的两个 batch 无法对比）。
        """
        return hashlib.sha256(_canonical(self.projection()).encode("utf-8")).hexdigest()


__all__ = [
    "ABSENT", "CARTESIAN_COMBINATION", "COMBINATION_POLICIES", "CandidateSearchSpace",
    "DIMENSIONS", "EXPLICIT_VARIANT", "INHERIT_PARENT",
    "MAX_CANDIDATES_PER_GENERATION_REQUEST", "PARAMETER_DIMENSION",
    "ParentStrategyPin", "ROLE_SLOTS", "SEARCH_SPACE_CONTRACT_VERSION", "SLOT_KINDS",
    "SearchSpaceError", "StrategyGeneratorError",
]
