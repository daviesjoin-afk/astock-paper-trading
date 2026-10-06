# -*- coding: utf-8 -*-
"""Canonical ``StrategyCandidate`` identity contract（R35-A）.

本模块是**策略候选是什么**的唯一 authority。它只回答一个问题：

    某个 generator 在显式 as-of / provenance 下，从哪个 **pinned parent** 提出了
    哪一份 canonical strategy specification？

它**不**回答"这个候选好不好"。评估结果（Sharpe / 收益率 / 回撤 / 胜率 /
promotion 结论）不是 candidate identity 的一部分，在这里会被**拒绝**：那些是
R36 / R31 的事实，由各自的 owner 产生。

调用链（R35-A 只到候选台账为止）::

    Research Hypothesis
            ↓
    Generator Input
            ↓
    Constrained Generator
            ↓
    StrategyCandidate          ← 本模块
            ↓
    Candidate Ledger
            ↓
    R36 Experiment Search / Validation

两条硬性质：

* **immutable** —— 已记录的 candidate 不得原地修改。改动任何语义事实产生的是
  **另一个** candidate（新的 ``candidate_id`` / fingerprint）。台账 append-only。
* **canonical fingerprint** —— 同一份 canonical specification 永远得到同一个
  fingerprint；fingerprint 覆盖全部语义事实，但**刻意排除**持久化元数据
  （``created_at``）与展示材料（数据库 row id、UI 排序、JSON key 顺序）。

表达约束复用仓库既有的 bounded DSL（``strategy_dsl_schema``）与参数契约
（``strategy_parameter_schema``），本模块**不**发明第二套表达式语言：candidate
不携带任何可执行源码，只有经过校验的声明式 AST。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import strategy_dsl_schema as DSL
import strategy_parameter_schema as SPS

#: candidate 契约版本。字段语义或指纹材料变化时必须递增。
CANDIDATE_CONTRACT_VERSION = "strategy-candidate-contract-v2"

#: 当前 candidate schema 版本。
#:
#: v2（R35-B）把**提案 provenance 整体**移出 candidate identity 与 payload。原因不是
#: 审美：candidate 是**内容**身份，而"谁在什么时候、以什么 model / hypothesis /
#: 研究来源 / seed 提出了它"是**事件** provenance。
#:
#: 移出的是两族事实：
#:
#: 1. generator 能力身份（``generator_type`` / ``generator_version`` /
#:    ``generator_contract_version``）——同一份 canonical specification 由
#:    ``factor_variant`` 与 ``bounded_combination`` 分别提出时必须得到同一个
#:    ``candidate_id``；
#: 2. 提案 provenance（``hypothesis_id`` / ``research_provenance`` /
#:    ``random_seed`` / ``model_identity``）——R35-C 接入 AI generator 后，GPT model A
#:    与 model B 提出同一份策略时必须得到同一个 ``candidate_id``。
#:
#: 否则只是把"generator_type 分裂身份"修掉，model / hypothesis / source / seed
#: 仍然能造成同样的分裂，同一策略会被重复送进实验 / PIT / robustness。
#:
#: 这些事实记在 **proposal 事件**与 **generation batch** 上（见
#: ``strategy_candidate_repository`` / ``strategy_candidate_search_space``），
#: 仍然显式、可审计、绝不是自由文本。
CANDIDATE_SCHEMA_VERSION = "strategy-candidate-v2"

#: R35-A 的历史 schema 版本。v1 行**必须继续可验证**：它们的 fingerprint 材料里
#: 含 generator 三件套与四项提案 provenance，因此材料是按
#: ``candidate_schema_version`` 版本化的，历史行一律不改写。
CANDIDATE_SCHEMA_VERSION_V1 = "strategy-candidate-v1"

#: 只有 v1 材料才携带的 generator 身份键（R35-A）。
LEGACY_GENERATOR_IDENTITY_KEYS = (
    "generator_type", "generator_version", "generator_contract_version",
)

#: 只有 v1 材料才携带的提案 provenance 键（R35-A）。v2 起它们属于 proposal 事件与
#: generation batch，不再是 candidate 的内容。
PROPOSAL_PROVENANCE_KEYS = (
    "hypothesis_id", "research_provenance", "random_seed", "model_identity",
)

#: 允许的 scope 词汇。这是**输入声明**的词汇，不是行情事实的 authority：
#: 某只票在某个 as-of 是否真的属于某个板块，由 universe / tradability owner 判定。
UNIVERSE_SCOPE_KINDS = ("a_share_all", "a_share_boards", "explicit_symbols")
UNIVERSE_BOARDS = ("main_board", "chinext")
UNIVERSE_KEYS = frozenset({"scope_kind", "boards", "symbols", "asof_universe_identity"})

#: 允许的 constraint 词汇：只有数值边界，没有自由文本（自由文本无法被校验）。
CONSTRAINT_KEYS = frozenset({"max_positions", "max_exposure_pct", "max_weight_pct"})

#: research provenance 的允许键。R35-B 起这份词表由
#: ``strategy_candidate_search_space`` 使用（provenance 是 proposal 事件 / batch 的
#: 输入侧事实），这里保留常量以维持单一词表来源。
RESEARCH_SOURCE_KINDS = ("human", "ai_research", "experiment", "external")
RESEARCH_PROVENANCE_KEYS = frozenset(
    {"source_kind", "source_identity", "source_fingerprint", "hypothesis_id"})

#: AI 生成时适用的 model 身份键（owner 同上：search space 侧）。
MODEL_IDENTITY_KEYS = frozenset({"provider", "model", "version"})

#: candidate identity 明确**不得**携带的评估事实。出现即拒绝（fail closed）：
#: 一旦它们进入指纹，同一个候选就会因为"跑过一次"而变成另一个候选。
FORBIDDEN_EVALUATION_KEYS = frozenset({
    "sharpe", "sharpe_ratio", "annual_return", "return_pct", "total_return",
    "max_drawdown", "drawdown_pct", "win_rate", "winrate", "profit_factor",
    "promotion_result", "promotion_decision", "lifecycle_state", "status",
    "score", "rank", "is_best", "winner",
})

#: rule role → candidate 字段。entry 是必需的；factor / exit 允许为 null
#: （null 的语义是"本候选未声明该角色"，绝不代表"沿用当前策略"）。
RULE_ROLES = ("factor", "entry", "exit")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: 版本/契约标识允许连字符与点（``strategy-generator-contract-v1``），
#: 但仍然是**封闭字符集**：版本字符串永远来自代码常量，不来自自由文本。
_VERSION_IDENT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SYMBOL = re.compile(r"^[0-9]{6}$")


class CandidateValidationError(ValueError):
    """Stable rejection from the candidate contract (always fail closed)."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise CandidateValidationError("candidate_fact_not_serializable")


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _closed_mapping(value, allowed: frozenset[str], label: str) -> dict:
    """接受一个**封闭**键集的对象；未知键一律拒绝，不做静默丢弃。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CandidateValidationError(f"{label}_must_be_an_object")
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise CandidateValidationError(f"{label}_has_unsupported_key:{unknown[0]}")
    return {str(key): item for key, item in value.items()}


def _declared_text(value, label: str, pattern: re.Pattern | None = None,
                   *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise CandidateValidationError(f"{label}_is_required")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise CandidateValidationError(f"{label}_is_required")
        return None
    if pattern is not None and not pattern.fullmatch(text):
        raise CandidateValidationError(f"{label}_is_not_a_canonical_identifier")
    return text


def _sha_text(value, label: str, *, required: bool = True) -> str | None:
    text = _declared_text(value, label, required=required)
    if text is None:
        return None
    if not _SHA256.fullmatch(text):
        raise CandidateValidationError(f"{label}_must_be_a_sha256_hex_digest")
    return text


def _finite(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateValidationError(f"{label}_must_be_a_finite_number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise CandidateValidationError(f"{label}_must_be_a_finite_number")
    return number


def _asof_day(value) -> str:
    """显式业务 as-of 日期。绝不接受"现在"：没有默认值，没有机器时钟。"""
    text = _declared_text(value, "asof")
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CandidateValidationError("asof_must_be_an_iso_date") from exc


def _reject_forbidden_evaluation_keys(value, label: str) -> None:
    """candidate 材料里出现评估事实即拒绝（防止"跑过一次"改变候选身份）。"""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).strip().lower()
            if name in FORBIDDEN_EVALUATION_KEYS:
                raise CandidateValidationError(
                    f"{label}_must_not_carry_evaluation_fact:{name}")
            _reject_forbidden_evaluation_keys(item, label)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_evaluation_keys(item, label)


def _rule_spec(ast, role: str, *, allow_strategy_root: bool):
    """校验一个 rule 角色：必须是 bounded DSL 的 boolean AST。

    只接受 ``strategy_dsl_schema.normalize`` 认得的声明式节点。任何"像代码"的
    输入（``python`` / ``eval`` / ``exec`` / ``import`` 之类 op，或自由源码字符串）
    在这里就被 schema 拒绝——这不是子串过滤，而是**没有那个 op**。
    """
    if ast is None:
        if role == "entry":
            raise CandidateValidationError("entry_spec_is_required")
        return None
    if not isinstance(ast, Mapping):
        raise CandidateValidationError(f"{role}_spec_must_be_an_object")
    _reject_forbidden_evaluation_keys(ast, f"{role}_spec")
    try:
        normalized = DSL.normalize(ast)
    except DSL.StrategyDslValidationError as exc:
        raise CandidateValidationError(f"{role}_spec_rejected:{exc}") from exc
    root_is_strategy = normalized.get("op") == "strategy"
    if root_is_strategy and not allow_strategy_root:
        raise CandidateValidationError(f"{role}_spec_must_not_declare_parameters")
    if not root_is_strategy and _contains_strategy_node(normalized):
        # 参数只能在 entry 的 ``strategy`` 根上声明一次，避免第二套参数 authority。
        raise CandidateValidationError(f"{role}_spec_must_not_declare_parameters")
    return normalized


def _contains_strategy_node(value) -> bool:
    if isinstance(value, Mapping):
        if value.get("op") == "strategy":
            return True
        return any(_contains_strategy_node(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_strategy_node(item) for item in value)
    return False


def _universe_spec(value) -> dict:
    declared = _closed_mapping(value, UNIVERSE_KEYS, "universe_spec")
    scope_kind = _declared_text(declared.get("scope_kind"), "universe_scope_kind")
    if scope_kind not in UNIVERSE_SCOPE_KINDS:
        raise CandidateValidationError("universe_scope_kind_is_not_allowlisted")
    boards = declared.get("boards") or ()
    symbols = declared.get("symbols") or ()
    if isinstance(boards, (str, bytes)) or not isinstance(boards, Sequence):
        raise CandidateValidationError("universe_boards_must_be_a_list")
    if isinstance(symbols, (str, bytes)) or not isinstance(symbols, Sequence):
        raise CandidateValidationError("universe_symbols_must_be_a_list")
    normalized_boards = tuple(sorted({str(item).strip() for item in boards}))
    normalized_symbols = tuple(sorted({str(item).strip() for item in symbols}))
    for board in normalized_boards:
        if board not in UNIVERSE_BOARDS:
            raise CandidateValidationError("universe_board_is_not_allowlisted")
    for symbol in normalized_symbols:
        if not _SYMBOL.fullmatch(symbol):
            raise CandidateValidationError("universe_symbol_must_be_six_digits")
    if scope_kind == "a_share_all":
        if normalized_boards or normalized_symbols:
            raise CandidateValidationError("a_share_all_scope_takes_no_boards_or_symbols")
    elif scope_kind == "a_share_boards":
        if not normalized_boards or normalized_symbols:
            raise CandidateValidationError("a_share_boards_scope_requires_boards_only")
    elif not normalized_symbols or normalized_boards:
        raise CandidateValidationError("explicit_symbols_scope_requires_symbols_only")
    result = {"scope_kind": scope_kind, "boards": list(normalized_boards),
              "symbols": list(normalized_symbols)}
    identity = _declared_text(declared.get("asof_universe_identity"),
                              "universe_asof_universe_identity", required=False)
    result["asof_universe_identity"] = identity
    return result


def _constraints(value) -> dict:
    declared = _closed_mapping(value, CONSTRAINT_KEYS, "constraints")
    result = {}
    if "max_positions" in declared:
        raw = declared["max_positions"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise CandidateValidationError("constraint_max_positions_must_be_a_positive_integer")
        result["max_positions"] = int(raw)
    for key in ("max_exposure_pct", "max_weight_pct"):
        if key in declared:
            number = _finite(declared[key], f"constraint_{key}")
            if not 0.0 < number <= 1.0:
                raise CandidateValidationError(f"constraint_{key}_must_be_a_fraction")
            result[key] = number
    return result


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """One immutable, canonically fingerprinted strategy candidate.

    ``candidate_id`` **就是** canonical fingerprint（schema 层由
    ``CHECK(candidate_id = candidate_fingerprint)`` 再钉一次）。这样"同一个候选"
    在任何地方都只有一个身份，去重不需要名字、描述或时间相似度。
    """

    candidate_id: str
    candidate_fingerprint: str
    parent_strategy_id: str | None
    parent_strategy_version: int | None
    parent_strategy_checksum: str | None
    strategy_schema_version: str
    factor_spec: Mapping | None
    entry_spec: Mapping
    exit_spec: Mapping | None
    parameter_spec: Mapping
    universe_spec: Mapping
    intended_market_regime: str
    constraints: Mapping
    asof: str
    candidate_schema_version: str = CANDIDATE_SCHEMA_VERSION
    #: 以下**只**为 v1 历史行存在。v2 行上它们全是 None / 空，因为 generator 能力与
    #: 提案 provenance 都不是候选内容（见 :data:`PROPOSAL_PROVENANCE_KEYS` 与
    #: :data:`LEGACY_GENERATOR_IDENTITY_KEYS`）。保留字段是为了让历史行的持久化投影
    #: 仍能逐字往返重建与自证，而不是给新行留后门。
    generator_type: str | None = None
    generator_version: str | None = None
    generator_contract_version: str | None = None
    hypothesis_id: str | None = None
    research_provenance: Mapping = field(default_factory=dict)
    random_seed: int | None = None
    model_identity: Mapping = field(default_factory=dict)

    def __post_init__(self):
        """v2 shape invariant：一个 v2 候选**不能**承载任何 generation provenance。

        放在 dataclass 上（而不是只靠 ``build_strategy_candidate`` / 投影形状），
        是为了让三条入口 —— 构造器、``candidate_from_projection``、直接
        ``replace()``/手工构造 —— 守住同一条契约。否则"v2 指纹不看 provenance"会
        变成一个静默的口子：能造出一个 ``candidate_id`` 正确、却挂着
        ``hypothesis_id`` / ``random_seed`` 的候选，被写进独立列后读不出来也清不掉。

        v1 历史行**不受影响**：它们的 provenance 属于各自的指纹材料。
        """
        if self.candidate_schema_version != CANDIDATE_SCHEMA_VERSION:
            return
        carried = [key for key in (*LEGACY_GENERATOR_IDENTITY_KEYS,
                                   *PROPOSAL_PROVENANCE_KEYS)
                   if getattr(self, key) not in (None, {}, (), [])]
        if carried:
            raise CandidateValidationError(
                f"candidate_provenance_belongs_to_proposal_not_content:{carried[0]}")

    def projection(self) -> dict:
        """candidate 的完整可持久化材料（不含持久化元数据 ``created_at``）。

        v2 的投影**只**包含候选内容：schema version、exact parent pin、DSL schema
        version、factor / entry / exit / parameter spec、universe、regime、
        constraints、asof。generator 能力身份与提案 provenance（hypothesis /
        research source / seed / model）**不在这里**——它们是 proposal 事件与
        generation batch 的 provenance。v1 行按历史形状继续携带这些键，保证
        ``candidate_json`` 逐字往返仍可自证。
        """
        material = {
            "candidate_schema_version": self.candidate_schema_version,
            "candidate_id": self.candidate_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "parent_strategy_id": self.parent_strategy_id,
            "parent_strategy_version": self.parent_strategy_version,
            "parent_strategy_checksum": self.parent_strategy_checksum,
            "strategy_schema_version": self.strategy_schema_version,
            "factor_spec": _thaw(self.factor_spec) if self.factor_spec is not None else None,
            "entry_spec": _thaw(self.entry_spec),
            "exit_spec": _thaw(self.exit_spec) if self.exit_spec is not None else None,
            "parameter_spec": _thaw(self.parameter_spec),
            "universe_spec": _thaw(self.universe_spec),
            "intended_market_regime": self.intended_market_regime,
            "constraints": _thaw(self.constraints),
            "asof": self.asof,
        }
        if self.candidate_schema_version == CANDIDATE_SCHEMA_VERSION_V1:
            # v1 行的 provenance 仍然属于它的材料，否则旧行无法自证。
            material.update({
                "generator_type": self.generator_type,
                "generator_version": self.generator_version,
                "generator_contract_version": self.generator_contract_version,
                "hypothesis_id": self.hypothesis_id,
                "research_provenance": _thaw(self.research_provenance),
                "random_seed": self.random_seed,
                "model_identity": _thaw(self.model_identity),
            })
        return material

    def fingerprint_material(self) -> dict:
        """指纹材料 = candidate 投影，减去 identity 自身。

        v2 的材料里既没有 generator 能力身份、也没有提案 provenance —— candidate 是
        **内容**身份。v1 材料按历史形状包含它们，因此 R35-A 已落库的行仍按自己的规则
        自证（绝不"升级"历史行）。
        """
        material = self.projection()
        material.pop("candidate_id")
        material.pop("candidate_fingerprint")
        return material

    def identity(self) -> dict:
        """parent pin 的显式投影（读路径用它证明"绑定的是哪一版"）。"""
        return {
            "strategy_id": self.parent_strategy_id,
            "strategy_version": self.parent_strategy_version,
            "strategy_checksum": self.parent_strategy_checksum,
        }


def build_strategy_candidate(
    *,
    parent_identity: Mapping | None,
    asof: str,
    entry_spec,
    universe_spec,
    intended_market_regime: str,
    factor_spec=None,
    exit_spec=None,
    constraints=None,
) -> StrategyCandidate:
    """Assemble one immutable candidate from already-captured explicit facts.

    Pure：没有 DB、没有 registry、没有机器时钟、没有 current/latest 查询。缺任何一个
    必需事实（parent version/checksum、asof）都在这里 fail closed，绝不"偷偷读
    latest 补齐"。

    R35-B 起本函数**不接受**任何 provenance：generator 能力身份（type / version /
    contract version）与提案 provenance（hypothesis / research source / seed / model）
    都不属于候选内容。同一份 canonical specification 无论由哪个能力、哪个 model、
    哪条 hypothesis 提出，都是同一个 candidate；这些事实记在 **proposal 事件**与
    **generation batch** 上。接受它们再丢弃同样会误导调用方，因此这里直接不提供。
    """
    parent_id = parent_version = parent_checksum = None
    if parent_identity is not None:
        pinned = _closed_mapping(
            parent_identity,
            frozenset({"strategy_id", "strategy_version", "strategy_checksum"}),
            "parent_identity")
        parent_id = _declared_text(pinned.get("strategy_id"), "parent_strategy_id",
                                   _IDENT, required=False)
        if parent_id is not None:
            raw_version = pinned.get("strategy_version")
            if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
                raise CandidateValidationError("parent_strategy_version_is_required")
            parent_version = int(raw_version)
            parent_checksum = _sha_text(pinned.get("strategy_checksum"),
                                        "parent_strategy_checksum")
        else:
            leftover = sorted(key for key in pinned if key != "strategy_id")
            if leftover:
                raise CandidateValidationError("parent_identity_without_a_strategy_id")

    entry = _rule_spec(entry_spec, "entry", allow_strategy_root=True)
    factor = _rule_spec(factor_spec, "factor", allow_strategy_root=False)
    exit_rule = _rule_spec(exit_spec, "exit", allow_strategy_root=False)
    # 参数契约由既有 owner 编译：候选不自己解析参数，也不复制第二套参数规则。
    # 没有声明可调参数的候选仍然合法（结构变体），此时 editable/immutable 都是空集。
    parameter_spec = SPS.StrategyParameterSchema.from_dsl(entry).to_dict()

    material = {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "parent_strategy_id": parent_id,
        "parent_strategy_version": parent_version,
        "parent_strategy_checksum": parent_checksum,
        "strategy_schema_version": DSL.DSL_SCHEMA_VERSION,
        "factor_spec": factor,
        "entry_spec": entry,
        "exit_spec": exit_rule,
        "parameter_spec": parameter_spec,
        "universe_spec": _universe_spec(universe_spec),
        "intended_market_regime": _declared_text(
            intended_market_regime, "intended_market_regime", _IDENT),
        "constraints": _constraints(constraints),
        "asof": _asof_day(asof),
    }
    fingerprint = _sha(material)
    return StrategyCandidate(
        candidate_id=fingerprint, candidate_fingerprint=fingerprint,
        parent_strategy_id=parent_id, parent_strategy_version=parent_version,
        parent_strategy_checksum=parent_checksum,
        strategy_schema_version=material["strategy_schema_version"],
        factor_spec=None if factor is None else _freeze(factor),
        entry_spec=_freeze(entry),
        exit_spec=None if exit_rule is None else _freeze(exit_rule),
        parameter_spec=_freeze(parameter_spec),
        universe_spec=_freeze(material["universe_spec"]),
        intended_market_regime=material["intended_market_regime"],
        constraints=_freeze(material["constraints"]),
        asof=material["asof"],
        candidate_schema_version=CANDIDATE_SCHEMA_VERSION,
    )


def verify_candidate_fingerprint(candidate: StrategyCandidate) -> bool:
    """Re-derive the fingerprint from the candidate's own material."""
    return (isinstance(candidate, StrategyCandidate)
            and candidate.candidate_id == candidate.candidate_fingerprint
            and _sha(candidate.fingerprint_material()) == candidate.candidate_fingerprint)


def candidate_from_projection(value: Mapping) -> StrategyCandidate:
    """Rebuild one candidate from its persisted projection so it can be verified.

    重建是**结构化**的：每个事实按存储形态读回，再由
    :func:`verify_candidate_fingerprint` 重新推导指纹。因此任何被篡改的持久化
    组件都会失败，而不是被悄悄接受成一个"新的候选"。

    R35-A 的 v1 行按 v1 材料重建（含 generator 三件套 + 四项提案 provenance），
    R35-B 的 v2 行按 v2 材料重建 —— v2 形状里**不存在**这些键，v1 形状里**必须**都有。
    两者都必须在**自己的** schema 版本下自证，绝不把 v1 行"升级"成 v2。
    """
    if not isinstance(value, Mapping):
        raise CandidateValidationError("candidate_projection_invalid")
    try:
        parent_id = value.get("parent_strategy_id")
        schema_version = str(
            value.get("candidate_schema_version", CANDIDATE_SCHEMA_VERSION))
        legacy = {}
        if schema_version == CANDIDATE_SCHEMA_VERSION_V1:
            for key in LEGACY_GENERATOR_IDENTITY_KEYS:
                raw = value.get(key)
                if raw is None:
                    raise CandidateValidationError("candidate_projection_invalid")
                legacy[key] = str(raw)
            for key in PROPOSAL_PROVENANCE_KEYS:
                if key not in value:
                    raise CandidateValidationError("candidate_projection_invalid")
        else:
            # v2 形状**不允许**携带 provenance：出现了就说明有人把它塞回了内容。
            smuggled = sorted(
                key for key in (*LEGACY_GENERATOR_IDENTITY_KEYS, *PROPOSAL_PROVENANCE_KEYS)
                if key in value)
            if smuggled:
                raise CandidateValidationError(
                    f"candidate_projection_carries_provenance:{smuggled[0]}")
        seed = None if value.get("random_seed") is None else int(value["random_seed"])
        candidate = StrategyCandidate(
            candidate_id=str(value["candidate_id"]),
            candidate_fingerprint=str(value["candidate_fingerprint"]),
            parent_strategy_id=None if parent_id is None else str(parent_id),
            parent_strategy_version=(None if value.get("parent_strategy_version") is None
                                     else int(value["parent_strategy_version"])),
            parent_strategy_checksum=(None if value.get("parent_strategy_checksum") is None
                                      else str(value["parent_strategy_checksum"])),
            strategy_schema_version=str(value["strategy_schema_version"]),
            factor_spec=(None if value.get("factor_spec") is None
                         else _freeze(value["factor_spec"])),
            entry_spec=_freeze(value["entry_spec"]),
            exit_spec=(None if value.get("exit_spec") is None else _freeze(value["exit_spec"])),
            parameter_spec=_freeze(value.get("parameter_spec") or {}),
            universe_spec=_freeze(value["universe_spec"]),
            intended_market_regime=str(value["intended_market_regime"]),
            constraints=_freeze(value.get("constraints") or {}),
            asof=str(value["asof"]),
            candidate_schema_version=schema_version,
            generator_type=legacy.get("generator_type"),
            generator_version=legacy.get("generator_version"),
            generator_contract_version=legacy.get("generator_contract_version"),
            hypothesis_id=(None if value.get("hypothesis_id") is None
                           else str(value["hypothesis_id"])),
            research_provenance=_freeze(value.get("research_provenance") or {}),
            random_seed=seed,
            model_identity=_freeze(value.get("model_identity") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateValidationError("candidate_projection_invalid") from exc
    return candidate


def candidate_projection_keys() -> tuple[str, ...]:
    """v2 candidate 投影的封闭键集（只有**内容**，没有 provenance）。

    前端/测试用它断言"没有评估事实、没有 generator 能力、没有提案 provenance 混入"。
    v1 的历史键集见 :data:`LEGACY_GENERATOR_IDENTITY_KEYS` + :data:`PROPOSAL_PROVENANCE_KEYS`
    —— 它们只在 v1 行里合法。
    """
    return tuple(sorted({
        "candidate_schema_version", "candidate_id", "candidate_fingerprint",
        "parent_strategy_id", "parent_strategy_version", "parent_strategy_checksum",
        "strategy_schema_version",
        "factor_spec", "entry_spec", "exit_spec", "parameter_spec", "universe_spec",
        "intended_market_regime", "constraints", "asof",
    }))


__all__ = [
    "CANDIDATE_CONTRACT_VERSION", "CANDIDATE_SCHEMA_VERSION",
    "CANDIDATE_SCHEMA_VERSION_V1", "CONSTRAINT_KEYS",
    "CandidateValidationError", "FORBIDDEN_EVALUATION_KEYS",
    "LEGACY_GENERATOR_IDENTITY_KEYS", "MODEL_IDENTITY_KEYS",
    "PROPOSAL_PROVENANCE_KEYS", "RESEARCH_PROVENANCE_KEYS", "RESEARCH_SOURCE_KINDS",
    "RULE_ROLES",
    "StrategyCandidate", "UNIVERSE_BOARDS", "UNIVERSE_SCOPE_KINDS",
    "build_strategy_candidate", "candidate_from_projection",
    "candidate_projection_keys", "verify_candidate_fingerprint",
]
