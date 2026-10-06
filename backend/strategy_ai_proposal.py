# -*- coding: utf-8 -*-
"""R35-C —— AI candidate proposal contract：AI provider **允许提出什么**。

一句话 authority：本模块拥有 **AI candidate proposal 的 schema**，即"一次 AI
research hypothesis 能提出什么样的 search-space declaration"。

它**不**拥有：

* candidate identity / fingerprint —— 那是 :mod:`strategy_candidate`；
* 搜索空间的组合基数与展开 —— 那是 :mod:`strategy_candidate_search_space` 与
  :mod:`strategy_generator`；
* DSL / parameter 合法性 —— 那是既有的 :mod:`strategy_dsl_schema` 与
  :mod:`strategy_parameter_schema`；
* 任何网络访问、数据库、registry、墙上时钟或 current state。

─────────────── 边界：proposal ≠ candidate ───────────────

provider 输出的是**一个 bounded search-space declaration**，绝不是
:class:`~strategy_candidate.StrategyCandidate`：

```text
AI Proposal ──► R35-B CandidateSearchSpace ──► R35-B StrategyCandidate
```

provider 因此**无权**声明 candidate identity、parent、as-of、universe、regime、
constraints、evaluation、score、promotion。凡是它试图声明的，一律
``invalid_proposal_field`` fail closed —— 不是忽略。

"研究结论 supported"与"策略可用"之间没有推理关系：
:attr:`ResearchHypothesis.is_authoritative` 恒为 ``False``，
:attr:`~ResearchHypothesis.authority` 恒为 ``research``。本模块不读 confidence，
也不允许任何 confidence 阈值参与资格判定（资格 gate 在 service 层，只看
``status == supported``）。

─────────────── 为什么本模块自己不做 DSL 校验 ───────────────

本模块只验证**JSON 形状、资源上界与禁止字段**。备选 AST 是否是合法 bounded DSL、
备选参数是否在父策略契约内，一律交给既有 owner：先由
:mod:`strategy_candidate_search_space` 构造 ``CandidateSearchSpace``（它内部调
``strategy_candidate`` 的 role 校验与 ``strategy_dsl_schema``），失败即整体拒绝。

绝不"自动修复"非法输出（删节点、clamp 参数、截断备选、换成 parent 值）：那会让
实际候选不再等于 AI 提出的候选，把拒绝变成静默的语义漂移。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import strategy_candidate_search_space as SS

__all__ = [
    "AI_PROPOSAL_CONTRACT_VERSION",
    "FORBIDDEN_PROVIDER_FIELDS",
    "MAX_AI_ALTERNATIVES_PER_SLOT",
    "MAX_AI_CANDIDATES_PER_REQUEST",
    "MAX_AI_PARAMETERS",
    "MAX_AI_SLOT_ROLES",
    "MAX_AI_VALUES_PER_PARAMETER",
    "Reason",
    "RESEARCH_GATE",
    "StrategyAIProposal",
    "build_proposal",
    "search_space_payload",
]

#: proposal contract 版本。形状或资源上界变化时递增（它进 batch provenance）。
AI_PROPOSAL_CONTRACT_VERSION = "strategy-ai-proposal-contract-v1"

#: 本轮把单次 AI 请求的候选上限**收紧**到 R35-B 全局上限（128）之下。
#:
#: 这不是性能调优，而是控制 AI proposal 的爆炸半径：AI 可能返回 10000 个 AST
#: alternative，最后去重只剩 3 个 —— 那种输入先把解析器撑爆。调用方只能继续
#: **收紧**，不能放宽。
MAX_AI_CANDIDATES_PER_REQUEST = 32

#: 单个请求的资源上界。每一项都限制"AI 能把解析器逼到多远"，与最终
#: cardinality 无关。
MAX_AI_PARAMETERS = 8
MAX_AI_VALUES_PER_PARAMETER = 8
MAX_AI_ALTERNATIVES_PER_SLOT = 8
MAX_AI_SLOT_ROLES = 3

#: provider **无权**输出的字段。出现即 ``invalid_proposal_field``。
#:
#: 前三个是 candidate identity；随后是 parent pin 与业务日；再后是 universe /
#: regime / constraints（AI 不拥有 risk 与 universe 事实）；然后是 generator 能力
#: 与预算；最后是 research provenance（由 service 从 exact research run 派生）以及
#: 一整族 evaluation / execution 词汇。
FORBIDDEN_PROVIDER_FIELDS = frozenset({
    "strategy_id", "strategy_version", "strategy_checksum",
    "candidate_id", "candidate_fingerprint", "candidate_schema_version",
    "asof",
    "universe_spec", "intended_market_regime",
    "constraints",
    "generator_type", "generator_version", "generator_contract_version",
    "max_candidates", "evidence_count",
    "hypothesis_id", "research_provenance", "model_identity",
    "status", "score", "rank", "sharpe", "return", "drawdown", "winner",
    "promotion", "deploy", "execution", "order", "position", "risk_override",
})

#: 唯一允许的 slot 角色 —— 直接来自 R35-B 的 slot 词汇，不发明第二套 slot 语言。
SLOT_ROLES = ("factor_slot", "entry_slot", "exit_slot")

#: 唯一允许的顶层键。
_ALLOWED_TOP_LEVEL = frozenset({"parameter_variants", *SLOT_ROLES})

#: 资格 gate 允许使用的唯一 research status。
#:
#: ``supported`` 在这里的**唯一**含义是"这条历史 research artifact 有足够已核验证据
#: 支持，允许拿来产生**研究候选**"。它不是 candidate quality、不是 promotion
#: eligibility、不是 expected return。
RESEARCH_GATE = "supported"


class Reason:
    """稳定 machine reasons（调用方与测试按字符串判断，不解析文案）。"""

    INVALID_PROVIDER_FIELD = "invalid_proposal_field"
    INVALID_PROPOSAL_SHAPE = "invalid_proposal_shape"
    UNKNOWN_PROVIDER_FIELD = "unknown_proposal_field"
    RESOURCE_LIMIT = "proposal_resource_limit"
    NO_VARIATION = "no_op_proposal"
    SEARCH_SPACE_REJECTED = "search_space_rejected"


class AIProposalError(ValueError):
    """Stable rejection from the AI proposal contract (always fail closed)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)


def _required_int(value: Any, *, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AIProposalError(Reason.INVALID_PROPOSAL_SHAPE, f"{what} must be a positive int")
    return int(value)


def _check_resources(payload: Mapping[str, Any]) -> None:
    """资源上界检查 —— 刻意在**任何** AST 校验之前跑。"""
    parameters = payload.get("parameter_variants") or {}
    if len(parameters) > MAX_AI_PARAMETERS:
        raise AIProposalError(Reason.RESOURCE_LIMIT, "too many parameters")
    for name, values in parameters.items():
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise AIProposalError(Reason.INVALID_PROPOSAL_SHAPE,
                                  f"parameter_variants.{name} must be a list")
        if len(values) > MAX_AI_VALUES_PER_PARAMETER:
            raise AIProposalError(Reason.RESOURCE_LIMIT, f"too many values for {name}")
    for role in SLOT_ROLES:
        slot = payload.get(role)
        if not isinstance(slot, Mapping) or slot.get("kind") != SS.EXPLICIT_VARIANT:
            continue
        alternatives = slot.get("alternatives")
        if not isinstance(alternatives, Sequence) or isinstance(alternatives, (str, bytes)):
            raise AIProposalError(Reason.INVALID_PROPOSAL_SHAPE,
                                  f"{role} must carry an alternatives list")
        if len(alternatives) > MAX_AI_ALTERNATIVES_PER_SLOT:
            raise AIProposalError(Reason.RESOURCE_LIMIT, f"too many alternatives in {role}")


def _reject_forbidden(payload: Mapping[str, Any]) -> None:
    forbidden = sorted(set(payload) & FORBIDDEN_PROVIDER_FIELDS)
    if forbidden:
        raise AIProposalError(Reason.INVALID_PROVIDER_FIELD,
                              f"AI may not declare {forbidden}")
    unknown = sorted(set(payload) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise AIProposalError(Reason.UNKNOWN_PROVIDER_FIELD, f"unknown fields {unknown}")


def _declared_axes(payload: Mapping[str, Any]) -> set[str]:
    """Which axes this proposal actually varies（用于 no-op 判定）。

    ``absent`` 也算**已声明的变化**：它显式声明"本候选没有该角色"，与
    ``inherit_parent``（继承父策略那一版语义）是两件事。§22 的 no-op 定义是
    "**全部 inherit parent** 且无任何参数变体" —— 只要有一个 slot 说了 ``absent``
    或给出了 ``explicit_variant`` 备选，就不是"父策略自己"。

    空备选的 ``explicit_variant`` 不算变化（它既没有提出任何备选，也不是继承），
    因此仍然落入 no-op / 无变化分支。
    """
    axes: set[str] = set()
    parameters = payload.get("parameter_variants") or {}
    if parameters:
        axes.add(SS.PARAMETER_DIMENSION)
    for role in SLOT_ROLES:
        slot = payload.get(role)
        if not isinstance(slot, Mapping):
            continue
        kind = slot.get("kind")
        if kind == SS.ABSENT:
            axes.add(role.removesuffix("_slot"))
        elif kind == SS.EXPLICIT_VARIANT:
            alternatives = slot.get("alternatives")
            if isinstance(alternatives, Sequence) and not isinstance(alternatives, (str, bytes)):
                if alternatives:
                    axes.add(role.removesuffix("_slot"))
    return axes


@dataclass(frozen=True, slots=True)
class StrategyAIProposal:
    """One bounded candidate search-space declaration proposed by an AI provider.

    这是 provider 能说出的**全部**内容。candidate identity、parent pin、as-of、
    universe、regime、constraints、research provenance 都不在这里：它们由调用方与
    R35-B 的 deterministic contracts 决定。

    ``payload`` 是 R35-B 原生 search-space 形状的子集：``parameter_variants`` 加
    三个 slot 声明。本类只做形状 / 资源 / 禁止字段校验；DSL 与 parameter 合法性
    留给 :mod:`strategy_candidate_search_space`。
    """

    payload: Mapping[str, Any]
    declared_axes: frozenset[str] = frozenset()
    proposal_contract_version: str = AI_PROPOSAL_CONTRACT_VERSION

    def __post_init__(self):
        if not isinstance(self.payload, Mapping):
            raise AIProposalError(Reason.INVALID_PROPOSAL_SHAPE, "proposal must be an object")
        frozen = {str(key): _freeze(value) for key, value in self.payload.items()}
        object.__setattr__(self, "payload", _freeze(frozen))
        object.__setattr__(self, "declared_axes", frozenset(self.declared_axes))
        object.__setattr__(self, "proposal_contract_version",
                           str(self.proposal_contract_version))

    @property
    def parameter_variants(self) -> dict:
        return dict(self.payload.get("parameter_variants") or {})

    def slot(self, role: str) -> dict | None:
        raw = self.payload.get(role)
        return dict(raw) if isinstance(raw, Mapping) else None

    def projection(self) -> dict:
        return {
            "proposal_contract_version": self.proposal_contract_version,
            "declared_axes": sorted(self.declared_axes),
            "parameter_variants": dict(self.payload.get("parameter_variants") or {}),
            **{role: (dict(self.payload[role]) if isinstance(self.payload.get(role), Mapping)
                      else None) for role in SLOT_ROLES},
        }


def _freeze(value: Any):
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def build_proposal(payload: Any) -> StrategyAIProposal:
    """Validate one provider JSON object into a :class:`StrategyAIProposal`.

    顺序刻意是 **先资源上界、再禁止字段、最后 no-op 判定**：资源检查排在最前，
    所以"10000 个 alternative"这种输入不会先把禁止字段扫描跑一遍。

    **no-op 判定**在最末：全部 inherit / 无任何备选 = 提案没有语义变化，AI 实际上
    什么都没提出 —— 那就生成"父策略自己"当候选，等于让 AI 看起来产出了候选。直接
    ``no_op_proposal``。
    """
    if not isinstance(payload, Mapping):
        raise AIProposalError(Reason.INVALID_PROPOSAL_SHAPE, "proposal must be an object")
    _check_resources(payload)
    _reject_forbidden(payload)
    axes = _declared_axes(payload)
    if not axes:
        raise AIProposalError(Reason.NO_VARIATION,
                              "proposal declares no parameter or slot variant")
    return StrategyAIProposal(payload=_thaw(payload), declared_axes=frozenset(axes))


def search_space_payload(proposal: StrategyAIProposal) -> dict:
    """The R35-B native search-space material declared by this proposal.

    形状与 :func:`strategy_candidate_service.build_search_space` 的关键字一一对应：
    ``parameter_variants`` 是**一个嵌套字典**（参数 id → 备选值），三个 slot 各自
    是 ``kind`` + ``alternatives``。刻意保持嵌套：若把参数 id 平铺到顶层，调用方
    ``pop("parameter_variants")`` 就会永远拿到 ``None`` —— 那会让 AI 提出的参数变体
    **静默消失**，最终只生成父策略自己。

    只包含 proposal 真正声明的轴；**不**补默认值 —— universe / regime /
    constraints / as-of / parent pin 由调用方与 R35-B 决定，不是 AI 的权力。
    """
    material: dict[str, Any] = {"parameter_variants": dict(proposal.parameter_variants)}
    for role in SLOT_ROLES:
        slot = proposal.payload.get(role)
        if isinstance(slot, Mapping):
            material[role] = _thaw(slot)
    return material
