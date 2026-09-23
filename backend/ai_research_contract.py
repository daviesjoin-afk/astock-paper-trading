# -*- coding: utf-8 -*-
"""R27-A —— AI 信息/研究层的**纯契约**：研究事实、研究假设、证据引用。

本模块只回答三个问题：

1. **AI 看到了什么事实？**（:class:`InformationEvent`）
2. **这个事实来自谁、哪一天、核验到什么程度？**（:class:`ResearchEvidenceRef`）
3. **基于这些事实，AI 提出了什么假设，它站得住吗？**（:class:`ResearchHypothesis`）

──────────────────────── Authority 边界 ────────────────────────

AI **不是** Market Data / Signal / Execution / Risk / Promotion 的 authority。
本契约因此在类型层面就把 AI 输出限定为 **research**：

* :attr:`ResearchHypothesis.is_authoritative` 恒为 ``False``；
* 假设状态词汇表（``supported`` / ``insufficient_evidence`` / ``unsupported``）
  与 ``paper_signals`` 的生命周期词汇（``pending`` / ``approved`` …）**没有交集** ——
  一份研究结论在词汇层面就无法被直接写成一条正式 signal；
* 本模块**只 import R24 的纯契约**（``market_data_contract``）来复用核验维度，
  不 import ``signal_service`` / ``execution_*`` / ``paper_*`` / 任何 DB、网络、
  LLM SDK 或时钟模块。AI 层是**消费者**，依赖方向只能是
  ``ai_research_contract → market_data_contract``；反向 import（authority → AI）
  由 ``test_ai_research_contract.py`` 的 guard 拒绝。

──────────────────────── 为什么不发明新的核验词汇 ────────────────────────

``verified`` / ``single_source`` / ``disagreement`` / ``unavailable`` /
``not_attempted`` 是 R24 的既有维度。本契约**引用**它们，并在
:func:`evidence_ref_from_reading` 里**逐字复制**：绝不重新判定，
绝不把 ``single_source`` 或 ``not_attempted`` 升级成 ``verified``。
「这条事实可不可信」只有一个来源，AI 层无权给出第二个答案。

同理，AI 自己生成的文本**不是事实来源**：:data:`EVIDENCE_SOURCE_TYPES`
是一个不含任何 AI 自产类别的闭集，因此"把 AI 的叙事当成市场证据"
不是被禁止，而是**无法表达**。

──────────────────────── 能力边界（诚实声明） ────────────────────────

本契约**不**做 I/O：不开数据库、不联网、不调 LLM、不读墙上时钟
（时间一律由调用方显式传入）。因此它**不可能**用 ``today()`` 回填历史研究，
也**不可能**在写信号的事务里偷偷发起一次 provider 刷新或一次模型调用。

* 它**能**判定：给定一组已存在的证据引用，这个研究假设站不站得住。
* 它**不能**制造事实：没有可引用的证据 → ``insufficient_evidence``，
  绝不默认 ``supported``、绝不编造一个 as-of 或一个 verification。

持久化刻意**不在本轮**：仓库里没有既有的 hypothesis / research-ledger owner，
为这个 PR 新建一套 AI 数据库体系会提前引入第二个事实存放点。本模块是纯契约，
writer 留给 R27-B。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import market_data_contract as MDC

__all__ = [
    # evidence source —— 事实来自哪一类 owner
    "EVIDENCE_SOURCE_MARKET_DATA", "EVIDENCE_SOURCE_SIGNAL", "EVIDENCE_SOURCE_EXECUTION",
    "EVIDENCE_SOURCE_STRATEGY_RESEARCH", "EVIDENCE_SOURCE_PORTFOLIO_RESEARCH",
    "EVIDENCE_SOURCE_NEWS", "EVIDENCE_SOURCE_TYPES",
    # information event kind —— 与 source 一一对应，由本模块独占映射
    "EVENT_MARKET_OBSERVED", "EVENT_SIGNAL_OBSERVED", "EVENT_EXECUTION_OBSERVED",
    "EVENT_STRATEGY_RESEARCH_OBSERVED", "EVENT_PORTFOLIO_RESEARCH_OBSERVED",
    "EVENT_NEWS_OBSERVED", "EVENT_KINDS",
    # evidence standing —— 证据对结论的**方向**
    "STANDING_SUPPORTING", "STANDING_DEGRADED", "STANDING_REJECTING", "STANDINGS",
    # hypothesis status —— research 专用，与 signal 生命周期无交集
    "HYPOTHESIS_SUPPORTED", "HYPOTHESIS_INSUFFICIENT_EVIDENCE", "HYPOTHESIS_UNSUPPORTED",
    "HYPOTHESIS_STATUSES",
    # reasons —— 为什么站不住（同一个原因只有一个名字）
    "RESEARCH_REASON_NO_EVIDENCE", "RESEARCH_REASON_EVIDENCE_NOT_VERIFIED",
    "RESEARCH_REASON_EVIDENCE_CONTRADICTED", "RESEARCH_REASON_EVIDENCE_UNAVAILABLE",
    "RESEARCH_REASONS",
    # contract
    "ResearchEvidenceRef", "InformationEvent", "ResearchHypothesis",
    # helper
    "UnsupportedResearch", "evidence_ref_from_reading",
]

# ---------------------------------------------------------------------------
# evidence source —— 事实来自哪一类 owner（闭集，且不含 AI 自产文本）
# ---------------------------------------------------------------------------

#: R24 Market Data Reading（唯一行情事实 owner）。
EVIDENCE_SOURCE_MARKET_DATA = "market_data"
#: R25 Signal Evidence / frozen provenance（signal 侧的紧凑投影）。
EVIDENCE_SOURCE_SIGNAL = "signal"
#: R26 Execution Evidence（order / fill 事实）。
EVIDENCE_SOURCE_EXECUTION = "execution"
#: 历史 strategy / portfolio research facts（已落库的研究事实）。
EVIDENCE_SOURCE_STRATEGY_RESEARCH = "strategy_research"
EVIDENCE_SOURCE_PORTFOLIO_RESEARCH = "portfolio_research"
#: 已落库的新闻事件事实（``news_events``，带 source_url / published_at）。
EVIDENCE_SOURCE_NEWS = "news"

#: **刻意**是一个闭集：AI 的 hypothesis / narrative / LLM 输出不在其中。
#: 于是"把 AI 自己生成的文本当成市场事实"这件事在类型层面就无法表达 ——
#: 不需要额外写一条禁令，也就不存在"忘记检查"的路径。
EVIDENCE_SOURCE_TYPES = (
    EVIDENCE_SOURCE_MARKET_DATA, EVIDENCE_SOURCE_SIGNAL, EVIDENCE_SOURCE_EXECUTION,
    EVIDENCE_SOURCE_STRATEGY_RESEARCH, EVIDENCE_SOURCE_PORTFOLIO_RESEARCH,
    EVIDENCE_SOURCE_NEWS,
)

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

#: 唯一的 kind ↔ source_type 映射。
#: ``InformationEvent.kind`` 是**派生只读**属性（见该类文档）：调用方无法把一份
#: execution 事实标成 market 事实，因此这里不需要"两套词汇保持一致"的维护负担 ——
#: 只有这一份映射，它自己就是唯一来源。
_KIND_BY_SOURCE_TYPE = {
    EVIDENCE_SOURCE_MARKET_DATA: EVENT_MARKET_OBSERVED,
    EVIDENCE_SOURCE_SIGNAL: EVENT_SIGNAL_OBSERVED,
    EVIDENCE_SOURCE_EXECUTION: EVENT_EXECUTION_OBSERVED,
    EVIDENCE_SOURCE_STRATEGY_RESEARCH: EVENT_STRATEGY_RESEARCH_OBSERVED,
    EVIDENCE_SOURCE_PORTFOLIO_RESEARCH: EVENT_PORTFOLIO_RESEARCH_OBSERVED,
    EVIDENCE_SOURCE_NEWS: EVENT_NEWS_OBSERVED,
}

# ---------------------------------------------------------------------------
# evidence standing —— 一条证据对结论的**方向**，不是一个分数
# ---------------------------------------------------------------------------

#: 该事实通过了它自己那套核验（R24 ``verified``）。可以支撑结论。
STANDING_SUPPORTING = "supporting"
#: 事实存在，但没有通过核验（单源 / 从未核验）。**不足以**支撑结论。
STANDING_DEGRADED = "degraded"
#: 事实被否证或核验源不可用。**反对**结论。绝不静默挑一个源。
STANDING_REJECTING = "rejecting"
STANDINGS = (STANDING_SUPPORTING, STANDING_DEGRADED, STANDING_REJECTING)

#: 每个 R24 verification 状态对应的 standing。
#:
#: ``not_attempted`` 是 ``DEGRADED`` 而不是 ``REJECTING``：它表达"没人核验过"，
#: 与"两个源互相否证"是两个结论 —— 前者是证据不足，后者是证据反对。
#: 两者都**不能**得出 ``supported``，但给用户的原因必须不同。
_STANDING_BY_VERIFICATION = {
    MDC.VERIFICATION_VERIFIED: STANDING_SUPPORTING,
    MDC.VERIFICATION_SINGLE_SOURCE: STANDING_DEGRADED,
    MDC.VERIFICATION_NOT_ATTEMPTED: STANDING_DEGRADED,
    MDC.VERIFICATION_DISAGREEMENT: STANDING_REJECTING,
    MDC.VERIFICATION_UNAVAILABLE: STANDING_REJECTING,
}

# ---------------------------------------------------------------------------
# hypothesis status —— research 专用词汇，与 signal 生命周期刻意不重叠
# ---------------------------------------------------------------------------

#: 有 ≥1 条 ``supporting`` 证据，且没有任何 ``rejecting`` 证据。
HYPOTHESIS_SUPPORTED = "supported"
#: 证据不足以判断（一条都没有 / 全部未通过核验）。**不是** approval。
HYPOTHESIS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
#: 已有证据与假设相冲突（多源否证 / 核验源不可用）。证据反对这个结论。
HYPOTHESIS_UNSUPPORTED = "unsupported"

#: 刻意**不含** ``pending`` / ``approved`` / ``blocked`` / ``waitlist`` / ``recovery``。
#: 那些是 ``paper_signals`` 的生命周期值，由 R25 独占。两个词汇表不相交，
#: 因此"AI 结论直接落成一条 pending signal"在词汇层面就不成立。
HYPOTHESIS_STATUSES = (
    HYPOTHESIS_SUPPORTED, HYPOTHESIS_INSUFFICIENT_EVIDENCE, HYPOTHESIS_UNSUPPORTED,
)

# ---------------------------------------------------------------------------
# reasons —— 为什么这个假设站不住（§47：同一个原因只有一个名字）
# ---------------------------------------------------------------------------

RESEARCH_REASON_NO_EVIDENCE = "no_evidence"
RESEARCH_REASON_EVIDENCE_NOT_VERIFIED = "evidence_not_verified"
RESEARCH_REASON_EVIDENCE_CONTRADICTED = "evidence_contradicted"
RESEARCH_REASON_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
RESEARCH_REASONS = (
    RESEARCH_REASON_NO_EVIDENCE, RESEARCH_REASON_EVIDENCE_NOT_VERIFIED,
    RESEARCH_REASON_EVIDENCE_CONTRADICTED, RESEARCH_REASON_EVIDENCE_UNAVAILABLE,
)


def _freeze(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """把证据 payload 冻结成只读视图；``None`` 归一成空（与 R24 同一约定）。"""
    if not mapping:
        return MappingProxyType({})
    return MappingProxyType(dict(mapping))


def _required_day(value: Any, *, what: str) -> str:
    """归一出一个**显式**的业务日；无法证明即拒绝（绝不猜、绝不用今天）。

    §七 要求所有研究对象显式 as-of：PIT 不可证明的事实不得进入研究链路。
    因此这里 fail closed 是**构造期**行为，而不是留给消费者去发现。
    """
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


def _authoritative_verification_pair(verification: Any, method: Any) -> tuple[str, str]:
    """``(verification, method)`` 的合法性由 R24 authority **独占**判定。

    刻意**构造一个 MarketDataSnapshot** 来问它，而不是在这里重写
    "``verified`` 允许配哪些 method"：核验词汇只有一个 owner，R24 收紧定义时
    本契约自动跟随（与 ``signal_service._cross_source_verified`` 同一手法）。

    非法组合（例如 ``verified`` 配 ``none``，或一个闻所未闻的状态词）在构造期
    就被拒绝，而不是被静默降级成"看起来能用"。这一点直接支撑 AI-02：
    AI 层没有任何入口可以伪造或升级一条事实的核验状态。
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
    """一条**已存在事实**的不可变引用。只留引用与判定维度，不复制 payload。

    * ``source_type`` 是 :data:`EVIDENCE_SOURCE_TYPES` 的成员 —— 闭集，
      不含 AI 自产类别；
    * ``source_id`` 是"是哪一条事实"（policy / signal_id / order_id / run_id…），
      由对应 owner 定义，本契约不重新编号；
    * ``as_of`` 是**这条事实自己的业务日**，必填。无法证明业务日的事实不得被
      引用（fail closed），这样"用未来事实支撑今天的结论"就没有入口；
    * ``verification`` / ``verification_method`` 是 R24 的既有维度，**逐字**来自
      事实 owner，本层不做任何重新判定。

    :attr:`standing` 由 verification 派生（只读）：它表达这条证据对结论的
    **方向**，而不是"可信度分数"。刻意不合成一个 ``quality_score`` —— 那会让
    消费者只能猜，且无法把"被否证"与"从没核验"区分开。
    """

    source_type: str
    source_id: str
    as_of: str
    verification: str = MDC.VERIFICATION_NOT_ATTEMPTED
    verification_method: str = MDC.VERIFICATION_METHOD_NONE
    detail: Mapping[str, Any] = field(default_factory=dict)

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
        verification, method = _authoritative_verification_pair(
            self.verification, self.verification_method,
        )
        object.__setattr__(self, "verification", verification)
        object.__setattr__(self, "verification_method", method)
        object.__setattr__(self, "detail", _freeze(self.detail))

    # ---------- derived ----------

    @property
    def standing(self) -> str:
        """这条证据对结论的方向（由 R24 verification 派生，不是第二个判据）。"""
        return _STANDING_BY_VERIFICATION[self.verification]

    @property
    def cross_source_verified(self) -> bool:
        """是否**真的**通过了逐票多源交叉核验（判据委托给 R24 authority）。

        读它而不是比较 ``verification == "verified"``：R24 的 ``verified`` 也可能
        来自 ``coverage_integrity``（快照完整且覆盖达标），那不是逐票第二源。
        """
        return MDC.is_cross_source_verified(
            MDC.MarketDataSnapshot(
                kind="research_evidence",
                verification=self.verification,
                verification_method=self.verification_method,
            )
        )

    # ---------- projections ----------

    def identity(self) -> tuple[str, str, str]:
        """让两条引用是**同一条事实**的字段。"""
        return (self.source_type, self.source_id, self.as_of)

    def projection(self) -> dict[str, Any]:
        """给 API / 前端的稳定投影：只 render，不重算核验语义。"""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "as_of": self.as_of,
            "verification": self.verification,
            "verification_method": self.verification_method,
            "cross_source_verified": self.cross_source_verified,
            "standing": self.standing,
        }


# ---------------------------------------------------------------------------
# information event —— "AI 看到了什么事实"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InformationEvent:
    """AI **观测到**一条已存在事实的记录。

    ``kind`` / ``verification`` / ``verification_method`` / ``evidence_id`` 都是
    **派生只读**属性，只从 :attr:`evidence_ref` 得来。这是有意的：把 kind 做成
    调用方可传的字段，就等于允许"引用了 execution 事实却标成 market 事实"，
    而那正是让数据冒充另一类 authority 的起点。派生之后，错标无法表达，
    也就不需要一条"记得校验 kind 与 source 一致"的规则。

    ``payload`` 是 AI 从这条事实里读出的内容（紧凑、只读）。它**不是**事实本身：
    事实在 :attr:`evidence_ref`，payload 只是这一次观察的投影。

    每个事件都必须有 ``evidence_ref`` —— 无来源的"AI 看到"不是信息事件，
    那是叙事，不在本契约内。
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
        # PIT：观测日不得早于它所引用事实的业务日 —— 否则就是用未来事实解释过去。
        if self.evidence_ref.as_of > self.as_of:
            raise ValueError(
                f"information event as_of={self.as_of} references evidence from "
                f"{self.evidence_ref.as_of}（未来事实）; 禁止 look-ahead"
            )
        object.__setattr__(self, "payload", _freeze(self.payload))

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
# hypothesis status —— 由证据**派生**，不是一个可以传进来的字段
# ---------------------------------------------------------------------------


def _derive_status(
    evidence_refs: Sequence[ResearchEvidenceRef],
) -> tuple[str, str | None]:
    """把一组证据引用判定成 ``(status, reason)``。

    判定顺序是业务语义，不是实现细节：

    1. **没有任何证据** → ``insufficient_evidence`` / ``no_evidence``。
       绝不因为"AI 写得很确定"就 ``supported``。
    2. **存在反对证据** → ``unsupported``。被否证（``disagreement``）与核验源
       不可用（``unavailable``）都反对结论，但原因分开记 —— 前者是"两个源互相
       否证"，后者是"核验源本身挂了"，它们的处置方式不同。
    3. **至少要有一条 ``supporting`` 证据** 才 ``supported``。
    4. **其余情况**（有事实、但全部未通过核验：单源 / 从未核验）→
       ``insufficient_evidence`` / ``evidence_not_verified``。

    关键性质：``supported`` 的**唯一**充分条件是"至少一条通过核验的证据，
    且没有任何证据反对"。confidence 不参与判定（见 :class:`ResearchHypothesis`），
    因此一个自信的假设本身不会把自己变成有证据的结论。
    """
    if not evidence_refs:
        return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_NO_EVIDENCE

    rejecting = [ref for ref in evidence_refs if ref.standing == STANDING_REJECTING]
    if rejecting:
        if any(ref.verification == MDC.VERIFICATION_UNAVAILABLE for ref in rejecting):
            return HYPOTHESIS_UNSUPPORTED, RESEARCH_REASON_EVIDENCE_UNAVAILABLE
        return HYPOTHESIS_UNSUPPORTED, RESEARCH_REASON_EVIDENCE_CONTRADICTED

    if any(ref.standing == STANDING_SUPPORTING for ref in evidence_refs):
        return HYPOTHESIS_SUPPORTED, None

    return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_EVIDENCE_NOT_VERIFIED


# ---------------------------------------------------------------------------
# hypothesis —— "AI 提出了什么研究假设"
# ---------------------------------------------------------------------------


class UnsupportedResearch(ValueError):
    """fail closed：一个假设尚未被证据支持，因此不能被当成结论消费。"""


@dataclass(frozen=True)
class ResearchHypothesis:
    """一个 AI 研究假设：**advisory / research only，永不 authoritative**。

    ``status`` / :attr:`is_authoritative` / :attr:`authority` 都是**派生只读**
    属性，刻意不做成可传字段。原因很直接：如果 ``status`` 可以由调用方给出，
    那么"给一个没有证据的假设贴上 ``supported``"就是一个普通的关键字参数 ——
    而这恰恰是本轮要根除的漏洞（默认批准）。派生之后，一个假设的强度**只**由它
    引用的、已存在的、且各自 owner 已经核验过的事实决定。

    ``confidence`` 是 AI 对自己说法的自评（``[0, 1]``），**不参与** status 判定。
    它是给人看的运维信号，不是证据。把它和证据混在一起算分，会立刻得到一个
    "AI 越自信结论越强"的环路，这正是 §五 禁止的自动升级。

    PIT：:attr:`as_of` 必须显式，且**每一条**证据的 ``as_of`` 都不得晚于它
    （:attr:`look_ahead_refs` 为空）。历史假设因此无法引用今天的事实，
    也无法引用 current strategy head / active cycle —— 那些根本没有 ``as_of``
    可证明，连构造成证据引用的资格都没有。

    ``hypothesis_id`` 由调用方提供（本契约不生成 id，也不读时钟）。
    """

    hypothesis_id: str
    as_of: str
    subject: str
    thesis: str
    evidence_refs: tuple[ResearchEvidenceRef, ...] = ()
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

        refs = tuple(self.evidence_refs or ())
        for ref in refs:
            if not isinstance(ref, ResearchEvidenceRef):
                raise TypeError(
                    "research hypothesis evidence_refs must be ResearchEvidenceRef; "
                    f"got {type(ref).__name__}（AI 自产文本不是证据）"
                )
        # 去重：两条引用指向同一条事实只是重复计数，不是两份独立证据。
        deduped: list[ResearchEvidenceRef] = []
        seen: set[tuple[str, str, str]] = set()
        for ref in refs:
            if ref.identity() in seen:
                continue
            seen.add(ref.identity())
            deduped.append(ref)
        refs = tuple(deduped)
        object.__setattr__(self, "evidence_refs", refs)

        # PIT：引用未来事实 → 拒绝构造，而不是悄悄过滤掉。
        # 静默丢弃会掩盖"AI 在用未来信息解释过去"这件事本身。
        if self.look_ahead_refs:
            offenders = ", ".join(
                f"{ref.source_type}:{ref.source_id}@{ref.as_of}" for ref in self.look_ahead_refs
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
    def look_ahead_refs(self) -> tuple[ResearchEvidenceRef, ...]:
        """引用了**晚于**本假设业务日的事实的证据（应为空）。"""
        return tuple(ref for ref in self.evidence_refs if ref.as_of > self.as_of)

    @property
    def status(self) -> str:
        """研究结论的强度 —— 由证据派生，不由调用方声明。"""
        return _derive_status(self.evidence_refs)[0]

    @property
    def reason(self) -> str | None:
        """为什么它（还）不是 ``supported``；``supported`` 时为 ``None``。"""
        return _derive_status(self.evidence_refs)[1]

    @property
    def authority(self) -> str:
        """本产物在系统中的定位：``research``，而非任何交易 authority。"""
        return "research"

    @property
    def is_authoritative(self) -> bool:
        """**恒为** ``False``。

        研究假设永远不构成 Signal / Order / Risk / Promotion 的依据。消费它的
        下游必须自行回到对应的 authority（R24 / R25 / R26）取正式事实，而不是
        把这里的结论当成行情、把 ``supported`` 当成 ``approved``。
        """
        return False

    @property
    def is_supported(self) -> bool:
        return self.status == HYPOTHESIS_SUPPORTED

    # ---------- fail-closed consumption ----------

    def require_supported(self) -> "ResearchHypothesis":
        """返回自身或抛 :class:`UnsupportedResearch` —— 绝不返回默认值。

        与分析代码的其余部分一致：想消费一个研究结论，就必须面对"它还没有
        证据"这个事实，而不是拿到一个空壳继续往下走。
        """
        if self.status != HYPOTHESIS_SUPPORTED:
            raise UnsupportedResearch(
                f"research hypothesis {self.hypothesis_id} is {self.status}"
                f"（{self.reason or 'no reason recorded'}）— advisory only"
            )
        return self

    # ---------- projections ----------

    def projection(self) -> dict[str, Any]:
        """给 API / 前端的稳定投影：status / reason / authority 都是后端算好的。

        前端只渲染这两个字段，**不重算**证据强度 —— 与 R24/R25 同一约定。
        ``is_authoritative`` 也一并下发，使"这只是研究"这一结论**可追溯**，
        而不是让调用方从 ``status`` 自己猜。
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
            "evidence_count": len(self.evidence_refs),
            "evidence": [ref.projection() for ref in self.evidence_refs],
        }


# ---------------------------------------------------------------------------
# 唯一的映射入口 —— 把 owner 的 reading 投影成 evidence ref
# ---------------------------------------------------------------------------


def evidence_ref_from_reading(
    source_type: str,
    source_id: str,
    reading: Any,
    *,
    as_of: Any = None,
) -> ResearchEvidenceRef:
    """把任意 owner 的 reading 映射成 :class:`ResearchEvidenceRef`。

    刻意消费 owner 自己发布的 ``projection()``（R24 ``MarketDataReading`` 与
    R25 ``SignalEvidence`` 都提供），而不是去读它们的内部字段：owner 改内部结构
    时本层不受影响，而"这条事实核验到什么程度"始终由 owner 决定。

    **逐字复制**，绝不重新判定：

    * ``verification`` / ``verification_method`` 直接来自投影。于是
      ``single_source`` / ``not_attempted`` 不会被这里升级成 ``verified``；
    * ``as_of`` 优先用调用方显式给出的值，否则取投影里的 ``as_of`` /
      ``asof_day``。两者都取不到 → 构造期拒绝（fail closed），因为一个业务日
      不可证明的事实不该进入研究链路。

    ``reading`` 不可用（例如 R24 的 ``unavailable``）时投影里没有核验维度，
    而 ``as_of`` 通常也不可证明 —— 因此这里会拒绝，而不是造一条"空事实"。
    这是有意的：缺失就是缺失，研究层不得自己补一个。
    """
    projection: Mapping[str, Any] = {}
    projector = getattr(reading, "projection", None)
    if callable(projector):
        projected = projector()
        if isinstance(projected, Mapping):
            projection = projected

    resolved_as_of = as_of if as_of is not None else (
        projection.get("as_of") or projection.get("asof_day")
    )
    return ResearchEvidenceRef(
        source_type=source_type,
        source_id=source_id,
        as_of=resolved_as_of,
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
