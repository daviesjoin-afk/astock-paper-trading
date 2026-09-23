# -*- coding: utf-8 -*-
"""R27-A —— AI 信息/研究层的**纯契约**。

回答三个问题：

1. AI 看到了什么已存在的事实？(:class:`InformationEvent`)
2. 这条事实来自哪个 owner、业务日是什么、owner 对它的核验结论是什么？
   (:class:`ResearchEvidenceRef`)
3. 基于这些事实，AI 提出了什么假设，它站得住吗？
   (:class:`HypothesisEvidence` / :class:`ResearchHypothesis`)

──────────────── 两个正交维度（本契约的核心） ────────────────

    fact verification    事实 owner 回答："这条事实是否通过它自己那套核验？"
    hypothesis relation  research reasoning **显式**声明："这条事实对当前 thesis
                         是 support / contradict / context？"

两者必须分开。一条 cross-source verified 的报价只说明"这个价格事实可信"，
**不**说明"下一交易日 momentum 会继续"。因此：

* ``verified`` **不再**自动映射成 supporting；
* provider disagreement / unavailable 只说明"这条 evidence 本身不能成为可靠依据"，
  **不**自动等于"这个 thesis 被事实反驳"。

fact verification 只有 owner 能回答，``verification`` / ``verification_method``
**逐字**来自 owner 的投影；relation 只有 research 层能声明，且是显式输入。
本契约不做评分体系（没有 ``quality_score`` / ``weighted_support`` / Bayesian 合并），
因为把两个正交维度压成一个分数会让下游只能猜。

──────────────── Authority 边界 ────────────────

AI 只消费事实，永不成为 authority：:attr:`ResearchHypothesis.is_authoritative` 恒为
``False``；研究状态词汇（``supported`` / ``insufficient_evidence`` / ``unsupported``）
与 ``paper_signals`` 生命周期（``pending`` / ``approved`` …）**不相交**；本模块只
import ``market_data_contract``（R24 纯契约）与标准库，不 import DB / 网络 / 时钟 /
LLM SDK。authority 不得反向 import 本模块。

evidence 是 **owner-issued**：唯一公开构造入口是
:func:`evidence_ref_from_market_reading`，它要求一个 typed owner projection
(``market_data_contract.MarketDataReading``) 并从中复制核验维度。裸构造
``ResearchEvidenceRef(...)`` 抛 ``TypeError``，因此调用方**无法仅凭传字符串**把自己
声明成"R24 verified market fact"。

诚实声明这一层的强度：这是**类型层构造边界**，不是密码学封印。Python 无法阻止有人
import 私有哨兵，也无法区分一个由 ``classify()`` 得出的 reading 和一个被手工拼出来的
reading —— 后者在 R24 自己的 API 里同样可能。本层能保证的是：AI 代码里**不再出现
自由形式的核验字符串**，任何 AI 事实都必须由一个 R24 类型对象承载。

──────────────── 能力边界 ────────────────

不做 I/O：不开数据库、不联网、不调 LLM、不读墙上时钟（时间一律由调用方显式传入）。
没有可引用的证据 → ``insufficient_evidence``，绝不默认 ``supported``。

只引用 ``verification``，**不**引用 R24 的 freshness：freshness 回答"对当前时间是否
仍新鲜"，与"来源是否经过核验"是两个维度，本层不把它们压成 ``trusted=True``。

持久化不在本轮：仓库里没有既有的 hypothesis / research-ledger owner，为这个 PR 新建
一套 AI 数据库体系会提前引入第二个事实存放点。writer 留给 R27-B。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

import market_data_contract as MDC

__all__ = [
    # evidence source —— 事实来自哪一类 owner
    "EVIDENCE_SOURCE_MARKET_DATA", "EVIDENCE_SOURCE_SIGNAL", "EVIDENCE_SOURCE_EXECUTION",
    "EVIDENCE_SOURCE_STRATEGY_RESEARCH", "EVIDENCE_SOURCE_PORTFOLIO_RESEARCH",
    "EVIDENCE_SOURCE_NEWS", "EVIDENCE_SOURCE_TYPES", "SUPPORTED_OWNER_ADAPTERS",
    # information event kind
    "EVENT_MARKET_OBSERVED", "EVENT_SIGNAL_OBSERVED", "EVENT_EXECUTION_OBSERVED",
    "EVENT_STRATEGY_RESEARCH_OBSERVED", "EVENT_PORTFOLIO_RESEARCH_OBSERVED",
    "EVENT_NEWS_OBSERVED", "EVENT_KINDS",
    # hypothesis relation —— 事实对 thesis 的关系（由 research 层显式声明）
    "RELATION_SUPPORTS", "RELATION_CONTRADICTS", "RELATION_CONTEXT", "RELATIONS",
    # hypothesis status —— research 专用，与 signal 生命周期无交集
    "HYPOTHESIS_SUPPORTED", "HYPOTHESIS_INSUFFICIENT_EVIDENCE", "HYPOTHESIS_UNSUPPORTED",
    "HYPOTHESIS_STATUSES",
    # reasons
    "RESEARCH_REASON_NO_EVIDENCE", "RESEARCH_REASON_EVIDENCE_NOT_VERIFIED",
    "RESEARCH_REASON_EVIDENCE_CONTRADICTED", "RESEARCH_REASON_EVIDENCE_UNAVAILABLE",
    "RESEARCH_REASONS",
    # contract
    "ResearchEvidenceRef", "InformationEvent", "HypothesisEvidence", "ResearchHypothesis",
    # errors
    "EvidenceConflict", "EvidenceRelationConflict", "UnsupportedResearch",
    # owner factory
    "evidence_ref_from_market_reading",
]

# ---------------------------------------------------------------------------
# evidence source —— 事实来自哪一类 owner
# ---------------------------------------------------------------------------

EVIDENCE_SOURCE_MARKET_DATA = "market_data"
EVIDENCE_SOURCE_SIGNAL = "signal"
EVIDENCE_SOURCE_EXECUTION = "execution"
EVIDENCE_SOURCE_STRATEGY_RESEARCH = "strategy_research"
EVIDENCE_SOURCE_PORTFOLIO_RESEARCH = "portfolio_research"
EVIDENCE_SOURCE_NEWS = "news"

#: **刻意**是一个闭集：AI 的 hypothesis / narrative / LLM 输出不在其中。
#: 于是"把 AI 自己生成的文本当成市场事实"在类型层面就无法表达。
EVIDENCE_SOURCE_TYPES = (
    EVIDENCE_SOURCE_MARKET_DATA, EVIDENCE_SOURCE_SIGNAL, EVIDENCE_SOURCE_EXECUTION,
    EVIDENCE_SOURCE_STRATEGY_RESEARCH, EVIDENCE_SOURCE_PORTFOLIO_RESEARCH,
    EVIDENCE_SOURCE_NEWS,
)

#: 当前**真的**接好 typed projection 的 owner。其余是已声明的未来来源，
#: 没有公开 factory 可以签发 —— 少支持一个 source 好过允许伪造一个 authority。
SUPPORTED_OWNER_ADAPTERS = frozenset({EVIDENCE_SOURCE_MARKET_DATA})

# ---------------------------------------------------------------------------
# information event kind —— 与 source_type 一一对应
# ---------------------------------------------------------------------------

EVENT_MARKET_OBSERVED = "market_observed"
EVENT_SIGNAL_OBSERVED = "signal_observed"
EVENT_EXECUTION_OBSERVED = "execution_observed"
EVENT_STRATEGY_RESEARCH_OBSERVED = "strategy_research_observed"
EVENT_PORTFOLIO_RESEARCH_OBSERVED = "portfolio_research_observed"
EVENT_NEWS_OBSERVED = "news_observed"
EVENT_KINDS = (
    EVENT_MARKET_OBSERVED, EVENT_SIGNAL_OBSERVED, EVENT_EXECUTION_OBSERVED,
    EVENT_STRATEGY_RESEARCH_OBSERVED, EVENT_PORTFOLIO_RESEARCH_OBSERVED,
    EVENT_NEWS_OBSERVED,
)

#: 唯一的 kind ↔ source_type 映射。``InformationEvent.kind`` 是派生只读属性，
#: 因此调用方无法把一份 execution 事实标成 market 事实，这里也就不存在
#: "两套词汇保持一致"的维护负担。
_KIND_BY_SOURCE_TYPE = {
    EVIDENCE_SOURCE_MARKET_DATA: EVENT_MARKET_OBSERVED,
    EVIDENCE_SOURCE_SIGNAL: EVENT_SIGNAL_OBSERVED,
    EVIDENCE_SOURCE_EXECUTION: EVENT_EXECUTION_OBSERVED,
    EVIDENCE_SOURCE_STRATEGY_RESEARCH: EVENT_STRATEGY_RESEARCH_OBSERVED,
    EVIDENCE_SOURCE_PORTFOLIO_RESEARCH: EVENT_PORTFOLIO_RESEARCH_OBSERVED,
    EVIDENCE_SOURCE_NEWS: EVENT_NEWS_OBSERVED,
}

# ---------------------------------------------------------------------------
# hypothesis relation —— 事实对 thesis 的关系
# ---------------------------------------------------------------------------

#: 这条事实（若它自身通过核验）**支持** thesis。
RELATION_SUPPORTS = "supports"
#: 这条事实（若它自身通过核验）**反对** thesis。
RELATION_CONTRADICTS = "contradicts"
#: 仅作上下文：它帮助理解 thesis，但**不足以**让它成立。
RELATION_CONTEXT = "context"

#: 最小闭集。刻意不含 strong_support / weak_support / neutral_positive /
#: negative / uncertain 等评分档位 —— 本轮不做评分模型。
RELATIONS = (RELATION_SUPPORTS, RELATION_CONTRADICTS, RELATION_CONTEXT)

# ---------------------------------------------------------------------------
# hypothesis status —— research 专用词汇，与 signal 生命周期刻意不重叠
# ---------------------------------------------------------------------------

#: 有 ≥1 条 **通过核验且 relation=supports** 的证据，且没有通过核验的 contradicts。
HYPOTHESIS_SUPPORTED = "supported"
#: 证据不足以判断（一条都没有 / 只有 context / 只有未通过核验的 supports）。
#: **不是** approval。
HYPOTHESIS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
#: 存在**通过核验的 contradicts** —— 可信事实反对这个结论。
HYPOTHESIS_UNSUPPORTED = "unsupported"

#: 刻意**不含** ``pending`` / ``approved`` / ``blocked`` / ``waitlist`` / ``recovery``。
#: 那些是 ``paper_signals`` 的生命周期值，由 R25 独占。两个词汇表不相交，
#: 因此"AI 结论直接落成一条 pending signal"在词汇层面就不成立。
HYPOTHESIS_STATUSES = (
    HYPOTHESIS_SUPPORTED, HYPOTHESIS_INSUFFICIENT_EVIDENCE, HYPOTHESIS_UNSUPPORTED,
)

# ---------------------------------------------------------------------------
# reasons —— 为什么这个假设（还）不成立
# ---------------------------------------------------------------------------

RESEARCH_REASON_NO_EVIDENCE = "no_evidence"
RESEARCH_REASON_EVIDENCE_NOT_VERIFIED = "evidence_not_verified"
RESEARCH_REASON_EVIDENCE_CONTRADICTED = "evidence_contradicted"
RESEARCH_REASON_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
RESEARCH_REASONS = (
    RESEARCH_REASON_NO_EVIDENCE, RESEARCH_REASON_EVIDENCE_NOT_VERIFIED,
    RESEARCH_REASON_EVIDENCE_CONTRADICTED, RESEARCH_REASON_EVIDENCE_UNAVAILABLE,
)


class EvidenceConflict(ValueError):
    """同一个 identity 出现了**互相矛盾的事实状态** —— fail closed。

    不选"更保守"的那一份，也不按输入顺序取先到者：那会让研究结果依赖
    collection order，而顺序不是业务语义。
    """


class EvidenceRelationConflict(ValueError):
    """同一条事实被同时声明成两种 relation —— 假设本身自相矛盾。"""


class UnsupportedResearch(ValueError):
    """fail closed：一个假设尚未被可信证据支持，不能当成结论消费。"""


# ---------------------------------------------------------------------------
# deep freeze —— 嵌套容器也必须不可变
# ---------------------------------------------------------------------------

#: 允许原样保留的 JSON-like 标量。
_SCALARS = (str, bytes, bool, int, float)


def _deep_freeze(value: Any, *, what: str) -> Any:
    """递归冻结容器；遇到无法冻结的对象**拒绝**（fail closed）。

    只做浅拷贝会让 ``ref.detail["nested"]["items"].append(...)`` 或调用方自己保留的
    原始 dict 继续改写"已冻结"的研究内容 —— 那会直接破坏本契约承诺的可审计性。

    刻意只支持 JSON-like 值：遇到自定义 mutable object 就拒绝，而不是保留一个以后
    可能被改写的引用，也不写通用 object freezer 框架。
    """
    if value is None or isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item, what=what) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item, what=what) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item, what=what) for item in value)
    raise TypeError(
        f"{what} must contain only JSON-like values (mapping / sequence / set / scalar); "
        f"got {type(value).__name__} — 研究事实不接受任意可变对象"
    )


def _required_day(value: Any, *, what: str) -> str:
    """归一出一个**显式**的业务日；无法证明即拒绝（绝不猜、绝不用今天）。"""
    day = MDC.canonical_day(value)
    if day is None:
        raise ValueError(
            f"{what} requires an explicit as-of day (YYYY-MM-DD); got {value!r} — "
            "PIT 不可证明的研究必须被拒绝，而不是回落到 current"
        )
    return day


def _required_text(value: Any, *, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{what} requires a non-empty identifier")
    return text


def _owner_verification_pair(verification: Any, method: Any) -> tuple[str, str]:
    """``(verification, method)`` 的合法性由 R24 authority **独占**判定。

    刻意构造一个 ``MarketDataSnapshot`` 来问它，而不是在这里重写"``verified`` 允许配
    哪些 method"：核验词汇只有一个 owner，R24 收紧定义时本契约自动跟随（与
    ``signal_service._cross_source_verified`` 同一手法）。

    非法组合（``verified`` 配 ``none``，或闻所未闻的状态词）构造期即拒绝，
    而不是被静默降级成"看起来能用"。
    """
    text_verification = str(verification if verification is not None else "")
    text_method = str(method if method is not None else "")
    try:
        MDC.MarketDataSnapshot(
            kind="research_evidence",
            verification=text_verification,
            verification_method=text_method,
        )
    except ValueError as error:
        raise ValueError(
            f"research evidence carries an illegal verification pair: {error}"
        ) from None
    return text_verification, text_method


# ---------------------------------------------------------------------------
# evidence ref —— "这个结论引用了哪一条已存在的事实"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchEvidenceRef:
    """一条**已存在事实**的 owner-issued 引用。只留引用与核验维度，不复制 payload。

    本类只回答"这是什么事实 / 谁拥有它 / 是哪一条 / 业务日是什么 / owner 对它的
    verification 是什么"。它**不能**回答"它支持什么 thesis" —— 那由
    :class:`HypothesisEvidence.relation` 显式声明。因此这里没有 ``standing`` 之类的
    派生方向字段：``verified`` 只意味着"来源通过了它自己的核验"。

    **没有公开 raw 构造器。** ``ResearchEvidenceRef(...)`` 一律抛 ``TypeError``；
    唯一的签发路径是 :func:`evidence_ref_from_market_reading`，它要求一个 typed owner
    projection。这不是"标记位"式的假防护：没有可 import 的哨兵，也没有
    ``issued=True`` 之类的开关，因此调用方无法仅凭传字符串把自己声明成
    "R24 verified market fact"。
    """

    source_type: str
    source_id: str
    as_of: str
    verification: str = MDC.VERIFICATION_NOT_ATTEMPTED
    verification_method: str = MDC.VERIFICATION_METHOD_NONE
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "ResearchEvidenceRef is owner-issued: 调用方不能仅凭传 source_type / "
            "source_id / verification 字符串声明一条 R24 verified 事实。"
            "请使用 evidence_ref_from_market_reading(reading, source_id=...) "
            "并提供 owner 的 typed projection"
        )

    def __post_init__(self) -> None:
        source_type = str(self.source_type or "").strip()
        if source_type not in EVIDENCE_SOURCE_TYPES:
            raise ValueError(
                f"unknown research evidence source_type: {source_type!r}; "
                f"allowed: {EVIDENCE_SOURCE_TYPES}（AI 自产文本刻意不在其中）"
            )
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(
            self, "source_id",
            _required_text(self.source_id, what=f"{source_type} evidence source"),
        )
        object.__setattr__(
            self, "as_of", _required_day(self.as_of, what=f"{source_type} evidence"),
        )
        verification, method = _owner_verification_pair(
            self.verification, self.verification_method,
        )
        object.__setattr__(self, "verification", verification)
        object.__setattr__(self, "verification_method", method)
        object.__setattr__(
            self, "detail", _deep_freeze(self.detail, what="research evidence detail"),
        )

    # ---------- fact-level questions only ----------

    @property
    def cross_source_verified(self) -> bool:
        """是否**真的**通过了逐票多源交叉核验（判据委托给 R24 authority）。

        读它而不是比较 ``verification == "verified"``：R24 的 ``verified`` 也可能来自
        ``coverage_integrity``（快照完整且覆盖达标），那不是逐票第二源。
        """
        return MDC.is_cross_source_verified(
            MDC.MarketDataSnapshot(
                kind="research_evidence",
                verification=self.verification,
                verification_method=self.verification_method,
            )
        )

    def identity(self) -> tuple[str, str, str]:
        """让两条引用指向**同一条事实**的字段。"""
        return (self.source_type, self.source_id, self.as_of)

    def fact_state(self) -> tuple:
        """这条事实被 owner 观测到的核验状态（用于冲突检测）。"""
        return (self.verification, self.verification_method, self.detail)

    def projection(self) -> dict[str, Any]:
        """给 API / 前端的稳定投影：只 render，不重算核验语义。"""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "as_of": self.as_of,
            "verification": self.verification,
            "verification_method": self.verification_method,
            "cross_source_verified": self.cross_source_verified,
        }


def _issue_evidence_ref(
    *, source_type: str, source_id: Any, as_of: Any,
    verification: Any, verification_method: Any, detail: Mapping[str, Any],
) -> ResearchEvidenceRef:
    """签发一个 ref。只有本模块的 owner factory 调用它。

    绕过 ``__init__``（它恒抛错）并在设置完全部字段后跑 ``__post_init__``，
    使校验逻辑仍然只有一份、且紧挨字段定义。
    """
    ref = object.__new__(ResearchEvidenceRef)
    object.__setattr__(ref, "source_type", source_type)
    object.__setattr__(ref, "source_id", source_id)
    object.__setattr__(ref, "as_of", as_of)
    object.__setattr__(ref, "verification", verification)
    object.__setattr__(ref, "verification_method", verification_method)
    object.__setattr__(ref, "detail", detail)
    ref.__post_init__()
    return ref


def evidence_ref_from_market_reading(
    reading: Any, *, source_id: Any,
) -> ResearchEvidenceRef:
    """把 R24 的 typed owner projection 映射成 :class:`ResearchEvidenceRef`。

    **唯一的 evidence 签发入口。** 要求 ``reading`` 是真正的
    ``market_data_contract.MarketDataReading``（而不是任何带 ``projection()`` 的
    duck-typed 对象），并从它逐字复制核验维度：于是 ``single_source`` /
    ``not_attempted`` 在这里**不可能**变成 ``verified``，AI 代码里也不存在自由形式的
    核验字符串。

    ``as_of`` 只取自 owner 投影（本函数不接受调用方覆盖），因此一条事实的业务日无法
    被调用方改写 —— PIT 证明链完整。

    ``source_id`` 是"是哪一条事实"的标识（例如 policy 名）。它只是一个**标识**，
    不是 authority 声明：核验结论来自 owner，不来自这个字符串。

    缺少可证明的业务日时拒绝（R24 ``unavailable`` 的 reading 没有 snapshot，
    因此没有可证明的 as_of）—— 缺失就是缺失，研究层不得自己补一个。
    """
    if not isinstance(reading, MDC.MarketDataReading):
        raise TypeError(
            "evidence must be issued from a typed owner projection: "
            f"expected market_data_contract.MarketDataReading, got {type(reading).__name__}"
        )

    projection = reading.projection()
    return _issue_evidence_ref(
        source_type=EVIDENCE_SOURCE_MARKET_DATA,
        source_id=source_id,
        as_of=projection.get("as_of") or projection.get("observed_at"),
        verification=projection.get("verification") or MDC.VERIFICATION_NOT_ATTEMPTED,
        verification_method=(
            projection.get("verification_method") or MDC.VERIFICATION_METHOD_NONE
        ),
        detail={
            "observed_at": projection.get("observed_at"),
            "policy": projection.get("policy"),
            "status": projection.get("status"),
        },
    )


# ---------------------------------------------------------------------------
# information event —— "AI 看到了什么事实"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InformationEvent:
    """AI **观测到**一条已存在事实的记录。

    ``kind`` / ``verification`` / ``verification_method`` / ``evidence_id`` 都是
    **派生只读**属性，只从 :attr:`evidence_ref` 得来。把 kind 做成可传字段就等于允许
    "引用了 execution 事实却标成 market 事实"，而那正是让数据冒充另一类 authority 的
    起点。派生之后，错标无法表达。

    ``payload`` 是 AI 从这条事实里读出的内容（递归冻结）。它**不是**事实本身：
    事实在 :attr:`evidence_ref`，payload 只是这一次观察的投影。

    刻意**不**携带 confidence / relation / decision / status —— 那些属于 hypothesis 层。
    """

    as_of: str
    source: str
    evidence_ref: ResearchEvidenceRef
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_ref, ResearchEvidenceRef):
            raise TypeError(
                "InformationEvent requires a ResearchEvidenceRef; "
                f"got {type(self.evidence_ref).__name__}"
            )
        object.__setattr__(self, "as_of", _required_day(self.as_of, what="information event"))
        object.__setattr__(
            self, "source", _required_text(self.source, what="information event"),
        )
        # PIT：观测日不得早于所引事实的业务日，否则就是用未来事实解释过去。
        if self.evidence_ref.as_of > self.as_of:
            raise ValueError(
                f"information event as_of={self.as_of} references evidence from "
                f"{self.evidence_ref.as_of}（未来事实）; 禁止 look-ahead"
            )
        object.__setattr__(
            self, "payload", _deep_freeze(self.payload, what="information event payload"),
        )

    # ---------- derived ----------

    @property
    def kind(self) -> str:
        """观测到的是哪一类事实（由 source_type 独占映射派生）。"""
        return _KIND_BY_SOURCE_TYPE[self.evidence_ref.source_type]

    @property
    def evidence_id(self) -> str:
        return self.evidence_ref.source_id

    @property
    def verification(self) -> str:
        """这条事实的核验状态 —— **继承**自证据引用，AI 层无权改写。"""
        return self.evidence_ref.verification

    @property
    def verification_method(self) -> str:
        return self.evidence_ref.verification_method

    def projection(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "as_of": self.as_of,
            "source": self.source,
            "evidence_id": self.evidence_id,
            "verification": self.verification,
            "verification_method": self.verification_method,
            "evidence": self.evidence_ref.projection(),
        }


# ---------------------------------------------------------------------------
# hypothesis evidence —— 事实 + 它对 thesis 的关系（两个维度的交汇点）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HypothesisEvidence:
    """把一条 owner-issued 事实与一个**显式声明**的 relation 绑在一起。

    relation 由 research reasoning 给出，**不是**从 ``verification`` 推出来的。
    这正是 P1 的修正：事实可信 ≠ 事实支持 thesis。
    """

    ref: ResearchEvidenceRef
    relation: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ResearchEvidenceRef):
            raise TypeError(
                f"HypothesisEvidence requires a ResearchEvidenceRef, got {type(self.ref).__name__}"
            )
        relation = str(self.relation or "").strip()
        if relation not in RELATIONS:
            raise ValueError(
                f"unknown hypothesis relation: {relation!r}; allowed: {RELATIONS}"
            )
        object.__setattr__(self, "relation", relation)

    @property
    def is_verified(self) -> bool:
        """这条事实本身是否通过 owner 的核验（fact level）。"""
        return self.ref.verification == MDC.VERIFICATION_VERIFIED

    @property
    def bears_on_thesis(self) -> bool:
        """这条事实是否真的（作为可信依据）作用于 thesis。"""
        return self.is_verified and self.relation in (RELATION_SUPPORTS, RELATION_CONTRADICTS)

    def projection(self) -> dict[str, Any]:
        return {"relation": self.relation, **self.ref.projection()}


# ---------------------------------------------------------------------------
# evidence set —— 去重 / 冲突 fail closed
# ---------------------------------------------------------------------------


def _normalise_evidence(items: Any) -> tuple[HypothesisEvidence, ...]:
    """校验一组 evidence：identically 相同去重，事实状态或 relation 冲突即拒绝。

    两条规则都**与顺序无关**：先按 identity 分组再检查，因此 ``[A, B]`` 与 ``[B, A]``
    必然得到同一个结果。first-wins 会让研究结论依赖 collection order。
    """
    grouped: dict[tuple[str, str, str], list[HypothesisEvidence]] = {}
    for item in items or ():
        if not isinstance(item, HypothesisEvidence):
            raise TypeError(
                f"research hypothesis evidence must be HypothesisEvidence, got {type(item).__name__}"
            )
        grouped.setdefault(item.ref.identity(), []).append(item)

    normalised: list[HypothesisEvidence] = []
    for identity, group in grouped.items():
        states = [entry.ref.fact_state() for entry in group]
        if any(state != states[0] for state in states):
            seen = sorted({entry.ref.verification for entry in group})
            raise EvidenceConflict(
                f"conflicting evidence for {identity[0]}:{identity[1]}@{identity[2]}: "
                f"owner reports {seen} — 同一条事实的状态互相矛盾时 fail closed，"
                "绝不按输入顺序取先到者"
            )
        relations = {entry.relation for entry in group}
        if len(relations) > 1:
            raise EvidenceRelationConflict(
                f"evidence {identity[0]}:{identity[1]}@{identity[2]} carries conflicting "
                f"relations {sorted(relations)} — 假设不能同时声称同一条事实既支持又"
                "反对自己，绝不按输入顺序取先到者"
            )
        normalised.append(group[0])
    return tuple(normalised)


def _derive_status(
    evidence: tuple[HypothesisEvidence, ...],
) -> tuple[str, str | None]:
    """把一组 evidence 判定成 ``(status, reason)``。

    判定顺序是业务语义：**可信的对照优先**。只有"通过核验 + relation=supports"才算
    支持；只有"通过核验 + relation=contradicts"才算反驳。provider disagreement /
    unavailable 说明**这条 evidence 本身不可靠**，因此既不支持也不反驳 —— 它只让证据
    数量不足以判断。
    """
    if not evidence:
        return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_NO_EVIDENCE

    verified_supports = [e for e in evidence if e.is_verified and e.relation == RELATION_SUPPORTS]
    verified_contradicts = [
        e for e in evidence if e.is_verified and e.relation == RELATION_CONTRADICTS
    ]

    if verified_contradicts:
        return HYPOTHESIS_UNSUPPORTED, RESEARCH_REASON_EVIDENCE_CONTRADICTED
    if verified_supports:
        return HYPOTHESIS_SUPPORTED, None

    # 没有可信的 supports / contradicts。若证据里存在 owner 自己都没核验成功的事实
    # （disagreement / unavailable），那个障碍比"只有单源"更根本，如实区分原因。
    if any(
        e.ref.verification in (MDC.VERIFICATION_UNAVAILABLE, MDC.VERIFICATION_DISAGREEMENT)
        for e in evidence
    ):
        return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_EVIDENCE_UNAVAILABLE
    return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_EVIDENCE_NOT_VERIFIED


# ---------------------------------------------------------------------------
# hypothesis —— "AI 提出了什么研究假设"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchHypothesis:
    """一个 AI 研究假设：**advisory / research only，永不 authoritative**。

    ``status`` / :attr:`reason` / :attr:`is_authoritative` / :attr:`authority` 都是
    **派生只读**属性，刻意不做成可传字段：若 ``status`` 可由调用方给出，"给一个没有
    证据的假设贴上 supported"就只是一个关键字参数 —— 那正是本轮要根除的默认批准。
    派生之后，假设强度只由它引用的、owner 已核验的事实 + 显式 relation 决定。

    ``confidence`` 是 AI 对自己说法的自评（``[0, 1]``），**不参与** status 判定。
    它是给人看的运维信号，不是证据。把它和证据混在一起会立刻得到"AI 越自信结论越强"
    的环路，正是 §五 禁止的自动升级。

    PIT：:attr:`as_of` 必须显式，且**每一条**证据的 ``as_of`` 都不得晚于它
    (:attr:`look_ahead_refs` 为空)，否则**拒绝构造**而不是静默过滤 —— 静默丢弃会掩盖
    "在用未来信息解释过去"这件事本身。历史假设因此无法引用 current strategy head /
    active cycle：那些没有可证明的 ``as_of``，连构造成证据引用的资格都没有。

    ``hypothesis_id`` 由调用方提供（本契约不生成 id，也不读时钟）。
    """

    hypothesis_id: str
    as_of: str
    subject: str
    thesis: str
    evidence: tuple[HypothesisEvidence, ...] = ()
    confidence: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "hypothesis_id",
            _required_text(self.hypothesis_id, what="research hypothesis"),
        )
        object.__setattr__(
            self, "as_of", _required_day(self.as_of, what="research hypothesis"),
        )
        object.__setattr__(
            self, "subject", _required_text(self.subject, what="research hypothesis subject"),
        )
        object.__setattr__(
            self, "thesis", _required_text(self.thesis, what="research hypothesis thesis"),
        )

        normalised = _normalise_evidence(self.evidence)
        object.__setattr__(self, "evidence", normalised)

        if self.look_ahead_refs:
            offenders = ", ".join(
                f"{item.ref.source_type}:{item.ref.source_id}@{item.ref.as_of}"
                for item in self.look_ahead_refs
            )
            raise ValueError(
                f"research hypothesis as_of={self.as_of} references future evidence "
                f"({offenders}); 历史研究只能使用 as_of <= hypothesis.as_of 的事实"
            )

        confidence = self.confidence
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError(f"research hypothesis confidence must be a number, got {confidence!r}")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"research hypothesis confidence must be within [0, 1], got {confidence}")
        object.__setattr__(self, "confidence", confidence)

    # ---------- derived ----------

    @property
    def look_ahead_refs(self) -> tuple[HypothesisEvidence, ...]:
        """引用了**晚于**本假设业务日的事实的证据（应为空）。"""
        return tuple(item for item in self.evidence if item.ref.as_of > self.as_of)

    @property
    def status(self) -> str:
        """研究结论的强度 —— 由证据派生，不由调用方声明。"""
        return _derive_status(self.evidence)[0]

    @property
    def reason(self) -> str | None:
        """为什么它（还）不是 ``supported``；``supported`` 时为 ``None``。"""
        return _derive_status(self.evidence)[1]

    @property
    def authority(self) -> str:
        """本产物在系统中的定位：``research``，而非任何交易 authority。"""
        return "research"

    @property
    def is_authoritative(self) -> bool:
        """**恒为** ``False``。

        研究假设永远不构成 Signal / Order / Risk / Promotion 的依据。消费它的下游必须
        回到对应的 authority（R24 / R25 / R26）取正式事实，而不是把这里的结论当成行情、
        把 ``supported`` 当成 ``approved``。
        """
        return False

    @property
    def is_supported(self) -> bool:
        return self.status == HYPOTHESIS_SUPPORTED

    # ---------- fail-closed consumption ----------

    def require_supported(self) -> "ResearchHypothesis":
        """返回自身或抛 :class:`UnsupportedResearch` —— 绝不返回默认值。"""
        if self.status != HYPOTHESIS_SUPPORTED:
            raise UnsupportedResearch(
                f"research hypothesis {self.hypothesis_id} is {self.status}"
                f"（{self.reason or 'no reason recorded'}）— advisory only"
            )
        return self

    # ---------- projections ----------

    def projection(self) -> dict[str, Any]:
        """给 API / 前端的稳定投影：status / reason / authority 都是后端算好的。

        前端只渲染，**不重算**证据强度。``is_authoritative`` 一并下发，使"这只是研究"
        这一结论可追溯，而不是让调用方从 ``status`` 自己猜。
        """
        return {
            "hypothesis_id": self.hypothesis_id,
            "as_of": self.as_of,
            "subject": self.subject,
            "thesis": self.thesis,
            "status": self.status,
            "reason": self.reason,
            "confidence": self.confidence,
            "authority": self.authority,
            "is_authoritative": self.is_authoritative,
            "evidence_count": len(self.evidence),
            "evidence": [item.projection() for item in self.evidence],
        }
