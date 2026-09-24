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

fact verification 的**语义**由**事实的 owner 自己**定义：market 事实的
``verification`` / ``verification_method`` 含义与合法组合归 R24，execution 事实的四态
与证据来源归 ``execution_verification``。research core **不解释**任何一个 owner 的
状态词 —— 它只消费一个 owner-neutral 的三态结论
(:data:`OWNER_OUTCOMES`)，而把"owner 词表 → 该结论"的映射留在**各 owner 自己的
factory** 里（market 的那一份就是 :func:`_market_owner_verification`）。R27 因此既
不重新判定，也**不证明**输入对象的 provenance。relation 只有 research 层能声明，
且是显式输入。
本契约不做评分体系（没有 ``quality_score`` / ``weighted_support`` / Bayesian 合并），
因为把两个正交维度压成一个分数会让下游只能猜。

──────────────── Authority 边界 ────────────────

AI 只消费事实，永不成为 authority：:attr:`ResearchHypothesis.is_authoritative` 恒为
``False``；研究状态词汇（``supported`` / ``insufficient_evidence`` / ``unsupported``）
与 ``paper_signals`` 生命周期（``pending`` / ``approved`` …）**不相交**；本模块只
import ``market_data_contract``（R24 纯契约）与标准库，不 import DB / 网络 / 时钟 /
LLM SDK。authority 不得反向 import 本模块。

evidence 走**类型化 owner evidence** 边界：每个已批准的 owner 有**一个**公开构造入口，
它要求该 owner 的 typed projection，并从投影**派生** identity、业务日与核验维度；
裸构造 ``ResearchEvidenceRef(...)`` 抛 ``TypeError``。当前批准的 owner adapter 由
:data:`SUPPORTED_OWNER_ADAPTERS` 登记：

```text
market_data   evidence_ref_from_market_reading      （本模块；typed R24 reading）
execution     evidence_ref_from_execution_projection（ai_research_execution_adapter）
```

**factory 不必都住在本文件里。** execution 的 factory 住在
``ai_research_execution_adapter``，因为它是唯一需要同时认识 execution owner 词表与
research 契约的接缝；本契约继续**不** import 任何 execution 模块（依赖方向单向）。
核验维度本身是 **owner-neutral** 的 (:class:`OwnerVerification`)：owner 自己发布结论，
research core 不解释任何 owner 的状态字符串。保证与**已知限制**（两层伪造路径、以及
为什么需要 owner 签发 token）集中在 :class:`ResearchEvidenceRef` 的 docstring 里，
此处不重复。

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
    "RESEARCH_REASON_NO_EVIDENCE", "RESEARCH_REASON_NO_SUPPORTING_EVIDENCE",
    "RESEARCH_REASON_EVIDENCE_NOT_VERIFIED", "RESEARCH_REASON_EVIDENCE_CONTRADICTED",
    "RESEARCH_REASON_EVIDENCE_UNAVAILABLE", "RESEARCH_REASONS",
    # owner-native verification —— research 唯一认识的核验形状
    "OWNER_OUTCOME_VERIFIED", "OWNER_OUTCOME_UNVERIFIED", "OWNER_OUTCOME_SOURCE_UNUSABLE",
    "OWNER_OUTCOMES", "OwnerVerification",
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
#:
#: R27-B2C-3 起 ``execution`` 也在其中。这张表表示的是"研究层已经存在**批准的
#: owner adapter**"，**不是**"所有 public factory 都定义在本文件里"：execution 的
#: factory 住在 ``ai_research_execution_adapter``（它必须同时认识 execution 与
#: research 两套词表，而本契约刻意不 import 任何 execution 模块）。
#: 本契约不需要、也不得 import 那个 adapter —— registry 是声明式的，一致性由
#: ``test_ai_research_evidence_ownership_guard`` 双向强制。
SUPPORTED_OWNER_ADAPTERS = frozenset({
    EVIDENCE_SOURCE_MARKET_DATA,
    EVIDENCE_SOURCE_EXECUTION,
})

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

#: 没有任何 evidence。
RESEARCH_REASON_NO_EVIDENCE = "no_evidence"
#: 有 evidence，但**没有一条**通过核验的 supports —— 例如只有 context。
#: 与 ``evidence_not_verified`` 的区别是这里的事实**本身可信**，只是与 thesis 无关。
RESEARCH_REASON_NO_SUPPORTING_EVIDENCE = "no_supporting_evidence"
#: 有 relation=supports 的 evidence，但它自己没通过 owner 核验（单源 / 从未核验）。
RESEARCH_REASON_EVIDENCE_NOT_VERIFIED = "evidence_not_verified"
#: 存在通过核验的 contradicts —— 可信事实反对这个结论。
RESEARCH_REASON_EVIDENCE_CONTRADICTED = "evidence_contradicted"
#: evidence 的 owner 核验失败或来源不可用（disagreement / unavailable）。
#: 只说明这条 evidence 不可靠，**不**等于 thesis 被反驳。
RESEARCH_REASON_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
RESEARCH_REASONS = (
    RESEARCH_REASON_NO_EVIDENCE, RESEARCH_REASON_NO_SUPPORTING_EVIDENCE,
    RESEARCH_REASON_EVIDENCE_NOT_VERIFIED, RESEARCH_REASON_EVIDENCE_CONTRADICTED,
    RESEARCH_REASON_EVIDENCE_UNAVAILABLE,
)

# ---------------------------------------------------------------------------
# owner-native verification —— research 唯一认识的核验形状
# ---------------------------------------------------------------------------
#
# 这三个值是 **owner-neutral** 的：market / execution / news / adaptive / runtime
# 各自有自己的状态词，但都必须把自己的结论归到这三态之一。于是：
#
# * research core **不**比较任何 owner 的状态字符串（因此 market 的 ``"verified"``
#   与 execution 的 ``"verified"`` 是两套不同词表，不会互相污染）；
# * "为什么这条证据还不足以判断"仍然能给出**准确且互不相同**的 reason；
# * 一个 owner 新增状态词只需要更新**它自己的** factory 映射，不需要动 research。

#: owner 自己判定"这条事实通过了它那套核验"。
OWNER_OUTCOME_VERIFIED = "verified"
#: owner 做了核验判定，结论是**没有通过**（单源 / 从未核验 / 证据不足）。
OWNER_OUTCOME_UNVERIFIED = "unverified"
#: owner 的核验**没能做出判定**：核验源本身不可用，或多个源互相否证。
#: 与 ``unverified`` 的区别在于障碍出在**核验过程**，而不是"这条事实不够好" ——
#: 因此假设的 reason 也不同（``evidence_unavailable`` vs ``evidence_not_verified``）。
OWNER_OUTCOME_SOURCE_UNUSABLE = "source_unusable"

#: 刻意**不含** verified_with_caveat / partially_verified / trusted 这类档位：
#: 那会把"通过核验"与"有多可信"压成一个刻度，而本契约不做评分模型。
OWNER_OUTCOMES = (
    OWNER_OUTCOME_VERIFIED, OWNER_OUTCOME_UNVERIFIED, OWNER_OUTCOME_SOURCE_UNUSABLE,
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
#:
#: 刻意**不含** ``bytes``：``payload`` / ``detail`` 是"投给 JSON provider 的 JSON-like
#: 内容"这一契约的字面含义，而 ``json.dumps`` 从不接受 bytes。在**契约边界**拒绝，
#: 才能让"bytes 进不了研究链路"成为一个构造期就成立的保证；否则一个**合法构造**的
#: typed event 会在下游 ``json.dumps`` 时抛裸 ``TypeError: Object of type bytes is not
#: JSON serializable`` —— 那是没有契约的失败，既不在本层，也不带 machine reason。
_SCALARS = (str, bool, int, float)

#: 冻结失败文案里逐字列出的合法标量 —— 错误信息本身也是一份对外契约。
_SCALAR_NAMES = "str / bool / int / float / None"


def _deep_freeze(value: Any, *, what: str) -> Any:
    """递归冻结容器；遇到非 JSON-like 值**拒绝**（fail closed）。

    只做浅拷贝会让 ``ref.detail["nested"]["items"].append(...)`` 或调用方自己保留的
    原始 dict 继续改写"已冻结"的研究内容 —— 那会直接破坏本契约承诺的可审计性。

    刻意只支持 JSON-like 值：遇到自定义 mutable object 就拒绝，而不是保留一个以后
    可能被改写的引用，也不写通用 object freezer 框架。

    **mapping key 也必须在冻结时是 ``str``。** 只校验 value 会留下同一族缺陷的后门：
    ``{b"k": 1}`` 的 value 完全合法，却会在下游 ``json.dumps`` 抛裸
    ``TypeError: keys must be str, int, float, bool or None, not bytes``。
    契约一旦声称"payload 是 JSON-like 内容"，这个声称就必须在构造期完整成立。
    """
    if value is None or isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"{what} mapping keys must be str; got {type(key).__name__} — "
                    f"合法标量：{_SCALAR_NAMES}；研究内容必须能稳定 JSON 序列化"
                )
            frozen[key] = _deep_freeze(item, what=what)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item, what=what) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item, what=what) for item in value)
    raise TypeError(
        f"{what} must contain only JSON-like values (mapping with str keys / sequence / "
        f"set / scalar: {_SCALAR_NAMES}); got {type(value).__name__} — "
        "研究事实不接受任意对象，也不接受 bytes 这类无法 JSON 序列化的标量"
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


def _plain(value: Any) -> Any:
    """已冻结的 JSON-like 容器 → 普通 dict / list，供投影输出。

    只做**一层形状还原**（与 provider 的 ``_jsonable`` 同手法），不改变任何内容，也不
    引入通用 serializer 框架：``_deep_freeze`` 保证输入里只有 str key 与 JSON-like
    标量，因此还原是可逆且无歧义的。
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_plain(item) for item in sorted(value, key=repr)]
    return value


def _market_verification_pair(verification: Any, method: Any) -> tuple[str, str]:
    """``(verification, method)`` 的合法性由 R24 authority **独占**判定。

    **这是 market-specific 的**，不是通用的 owner verification 校验：它刻意构造一个
    ``MarketDataSnapshot`` 来问 R24，而不是在这里重写"``verified`` 允许配哪些 method"。
    核验词汇只有一个 owner，R24 收紧定义时本契约自动跟随（与
    ``signal_service._cross_source_verified`` 同一手法）。

    命名刻意带 ``market``：把 market 专属校验叫作"owner verification"会重新制造一个
    假的通用抽象 —— 它只认识 ``market_data_contract`` 的词表，对 execution / news
    的核验闭集一无所知。

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
# owner-native verification —— 事实的 owner 对"这条事实是否通过核验"的发布结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerVerification:
    """一个 owner 对**它自己那条事实**的核验发布结果，形状对 research 中性。

    三个字段各有明确归属：

    ``outcome``
        owner-neutral 的**三态结论**，是 research core 唯一消费的字段。owner 有自己
        的状态词（market 的 ``verified`` / ``single_source`` / …；execution 的四态），
        但每个 owner 的 factory 必须把自己的结论归到 :data:`OWNER_OUTCOMES` 之一。
        三态而不是布尔，是因为"核验源不可用"与"事实没通过核验"必须给出**不同**的
        research reason —— 压成一个 bool 会让这两种情况在假设层无法区分。

    ``status``
        owner 自己的状态词，**逐字保留**。research core 不解释这个字符串，也不拿它
        做任何比较：它只用于展示、审计与冲突诊断。因此两个 owner 可以同时使用
        ``"verified"`` 而互不干扰。

    ``attributes``
        owner-specific 的不可变附加维度（market：``verification_method`` /
        ``cross_source_verified``；未来的 execution：``verification_scope`` /
        ``verification_source`` 等）。**只有产生它的 owner factory 知道这些键的
        语义**，research core 只保存、投影与参与冲突检测。

    刻意**没有** ``verified_at`` / ``confidence`` / ``score``：核验结论不是评分，
    时间维度也不属于"这条事实是否通过核验"。
    """

    outcome: str
    status: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        outcome = str(self.outcome or "").strip()
        if outcome not in OWNER_OUTCOMES:
            raise ValueError(
                f"unknown owner verification outcome: {outcome!r}; allowed: {OWNER_OUTCOMES}"
            )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self, "status",
            _required_text(self.status, what="owner verification status"),
        )
        object.__setattr__(
            self, "attributes",
            _deep_freeze(self.attributes, what="owner verification attributes"),
        )

    @property
    def is_verified(self) -> bool:
        """owner 是否判定这条事实**通过**了它那套核验。

        这是 research hypothesis 消费的唯一判据 —— 不是 ``status`` 的字符串比较。
        """
        return self.outcome == OWNER_OUTCOME_VERIFIED

    @property
    def source_unusable(self) -> bool:
        """核验过程本身没能做出判定（源不可用 / 多源互相否证）。"""
        return self.outcome == OWNER_OUTCOME_SOURCE_UNUSABLE

    def canonical(self) -> tuple:
        """deterministic canonical form —— 冲突检测的比较键。

        必须包含 ``status`` **与** ``attributes``，否则同一 identity 下
        ``verified + cross_source`` 与 ``verified + coverage_integrity`` 会被误判成
        同一条事实状态（market 的永久不变量，见 RVERIFY-06）。
        """
        return (self.outcome, self.status, _canonical_attributes(self.attributes))


def _canonical_attributes(value: Any) -> Any:
    """把已冻结的 owner attributes 归一成**顺序无关**的可比较形状。

    只做形状归一，不做序列化框架：mapping 按键排序成 tuple、序列成 tuple、集合成
    frozenset。输入已经过 :func:`_deep_freeze`，因此这里只处理 JSON-like 容器。
    """
    if isinstance(value, Mapping):
        return tuple(sorted((key, _canonical_attributes(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical_attributes(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_canonical_attributes(item) for item in value)
    return value


#: R24 的**每一个**合法状态 → owner-neutral 三态的**显式穷尽**映射。
#:
#: 刻意是一张完整表，而不是 ``if/elif/else``：catch-all ``else`` 会让 R24 未来新增一个
#: 合法状态时被 research 层**静默**归成 ``unverified`` —— 那正是"research 替 owner 决定
#: 它自己的词是什么意思"，与本契约的核心原则直接冲突。显式表把这件事变成
#: :func:`_market_owner_verification` 的 fail-closed 拒绝，从而要求**人工**决定新状态
#: 如何归口（见 RVERIFY-11）。
#:
#: 与 :data:`_KIND_BY_SOURCE_TYPE` 同一手法：词表只有一份，一致性由构造期检查强制。
_MARKET_OUTCOME_BY_VERIFICATION = {
    # 通过了该 kind 的 verification policy（含 coverage_integrity）。
    MDC.VERIFICATION_VERIFIED: OWNER_OUTCOME_VERIFIED,
    # 有事实，但**没有**通过核验：单源证据，或本次读取根本没发起核验。
    MDC.VERIFICATION_SINGLE_SOURCE: OWNER_OUTCOME_UNVERIFIED,
    MDC.VERIFICATION_NOT_ATTEMPTED: OWNER_OUTCOME_UNVERIFIED,
    # 核验**过程**没能做出判定：两个源互相否证，或核验源本身不可用。
    # 与"没通过核验"必须分开，否则假设层给不出 ``evidence_unavailable`` 这个原因。
    MDC.VERIFICATION_DISAGREEMENT: OWNER_OUTCOME_SOURCE_UNUSABLE,
    MDC.VERIFICATION_UNAVAILABLE: OWNER_OUTCOME_SOURCE_UNUSABLE,
}


def _market_outcome_mapping_problems(
    mapping: Mapping[str, str], verifications: Any,
) -> list[str]:
    """显式 outcome 映射与 owner 词表的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个已知状态"与"多一个未知状态"两个方向都能被**直接
    测到**（见 RVERIFY-11 的非空性），而不是只能靠改 R24 源码来验 —— 与
    ``test_ai_research_evidence_ownership_guard._registry_problems`` 同一手法。

    两个方向都必须报问题：任何一处漂移都意味着"某个 owner 状态的含义"已经不再由人工
    决定，而是由 research 层的默认分支决定。
    """
    problems: list[str] = []
    missing = sorted(set(verifications) - set(mapping))
    if missing:
        problems.append(
            f"owner 已有但没有登记 owner-neutral outcome 的状态：{missing}"
            "（research 层不得替 owner 猜一个新状态的含义）"
        )
    unknown = sorted(set(mapping) - set(verifications))
    if unknown:
        problems.append(
            f"outcome 映射里有 owner 已不认识的状态：{unknown}"
            "（说明这张表与 owner 词表漂移了）"
        )
    return problems


def _market_owner_verification(projection: Mapping[str, Any], snapshot: Any) -> OwnerVerification:
    """R24 投影 → owner-neutral 核验发布结果。**market owner 的 factory**。

    owner authority 完整保留在 R24 一侧，本函数只做**归口**，不做判定：

    1. ``(verification, verification_method)`` 的合法性问 R24（:func:`_market_verification_pair`）；
    2. ``cross_source_verified`` 问 R24 的 :func:`market_data_contract.is_cross_source_verified`
       —— 本层**不**比较 ``verification == "verified"``（``coverage_integrity`` 的
       ``verified`` 不是逐票双源）；
    3. 把 R24 的状态词**按显式穷尽表**归到 owner-neutral 三态。

    第 3 步是本层唯一的解释行为，且方向是"把 owner 的词映射到中性结论"，**不是**
    "用中性结论替代 owner 判定"。research core 反过来看不到这张表。

    **fail closed 是这里的关键性质。** 映射表必须**恰好**覆盖 :data:`MDC.VERIFICATIONS`：

    * R24 新增一个合法状态 → ``_market_verification_pair`` 会放行它，但下表查不到 →
      **拒绝**，而不是静默归成 ``unverified``。人工必须明确决定新状态归哪一态；
    * 表里出现 R24 已不认识的状态 → 同样拒绝（说明这张表与 R24 漂移了）。

    少了这条，"R24 加状态"会变成一次静默的语义发明；有了它，那是一次必须人工处理的
    契约变更。这也是本模块唯一允许引入 owner 词表的地方，因此穷尽性必须在此强制。
    """
    # 词表漂移在**构造期**就 fail closed，而不是等到某个新状态恰好出现在数据里。
    # 这一步同时让下面的直接查表成为**全函数**：R24 词表被完整覆盖，且
    # ``_market_verification_pair`` 已保证状态属于该词表。
    problems = _market_outcome_mapping_problems(
        _MARKET_OUTCOME_BY_VERIFICATION, MDC.VERIFICATIONS,
    )
    if problems:
        raise ValueError(
            "market verification vocabulary drifted from the explicit outcome mapping: "
            + "; ".join(problems)
        )

    verification, method = _market_verification_pair(
        projection.get("verification") or MDC.VERIFICATION_NOT_ATTEMPTED,
        projection.get("verification_method") or MDC.VERIFICATION_METHOD_NONE,
    )
    outcome = _MARKET_OUTCOME_BY_VERIFICATION[verification]
    return OwnerVerification(
        outcome=outcome,
        status=verification,
        attributes={
            "verification_method": method,
            "cross_source_verified": MDC.is_cross_source_verified(snapshot),
        },
    )


# ---------------------------------------------------------------------------
# evidence ref —— "这个结论引用了哪一条已存在的事实"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchEvidenceRef:
    """一条**已存在事实**的类型化引用。只留引用与核验维度，不复制 payload。

    本类只回答"这是什么事实 / 哪一个 owner 口径 / 哪一份快照 / 业务日是什么 / owner 对
    它的 verification 是什么"。它**不能**回答"它支持什么 thesis" —— 那由
    :class:`HypothesisEvidence.relation` 显式声明。因此这里没有 ``standing`` 之类的
    派生方向字段：``verified`` 只意味着"来源通过了它自己的核验"。

    **canonical 存储是 owner-neutral 的**（:class:`OwnerVerification`），不是
    ``(verification, verification_method)`` 那一对 market 形状。这一层曾经只携带
    market 词表，于是 execution / news / adaptive 想进入 research 时只剩两个错误选择：
    假装自己是 ``market_data``，或把自己的核验结论翻译成 market 词 —— 后者就是由非
    owner 发明核验结论。现在 owner 自己发布 :class:`OwnerVerification`，research core
    只消费它，**不解释**任何 owner 的状态字符串。

    **没有公开 raw 构造器。** ``ResearchEvidenceRef(...)`` 一律抛 ``TypeError``；
    唯一的签发路径是**该 owner 自己的 factory**（market 的每一份是
    :func:`evidence_ref_from_market_reading`，execution 的每一份是
    ``ai_research_execution_adapter.evidence_ref_from_execution_projection``）。identity
    由 owner 投影派生（调用方不提供），核验维度由**该 owner 的 factory** 归口 —— 因此
    "传字符串把自己声明成 owner 已核验事实"不可表达。

    **保证与已知限制**（不要把这个边界读成 provenance 证明）。R24 的
    ``MarketDataReading`` / ``MarketDataSnapshot`` 是**公开 dataclass**，因此

        手工造 MarketDataSnapshot(verified, cross_source) → 手工造 MarketDataReading
            → evidence_ref_from_market_reading(...) → 得到一条 verified 的 ref

    在本层是**可以通过**的：伪造只是从一步变成两步。execution 亦然
    （``ExecutionEvidence`` 公开可构造 → ``fact_projection`` → execution factory）。
    本层真正保证的是：

    * 调用方**不能提供 identity** —— ``source_id`` 由 reading 派生
      （``policy | kind | subject @ observed_at``），所以同一份事实无法被改名成
      FACT_A / FACT_B / FACT_C 绕过去重与冲突检测，两只不同股票也不会撞成一条；
    * 本层**没有独立的 ``verification`` 参数** —— 核验结论来自 supplied reading 的
      投影，且由 market factory 归口，R27 无从自行发明一个核验结论；
    * AI 代码里不再出现自由形式的核验字符串。

    **不要把第二点读成"核验结论可信"。** 准确表述是：R27 factory 没有独立的
    ``verification`` 参数，它逐字复制 supplied R24 reading projection 的 verification；
    R27 **本身无法证明**该 reading 是 owner 产生还是调用方手工构造。所以
    ``single_source`` 不会在**本层**被改写，但一个手工构造的 reading 里写了什么，本层
    照样原样复制。

    要真正证明"这份事实由 ``market_data_service`` 产生"，需要 **R24 自己签发 evidence
    token**（R24 的职责，不在 R27-A 范围内）。本条限制由
    ``test_AI_TYPED_06_two_step_forgery_is_documented_not_claimed_closed`` 断言下来，
    而不是假装已封堵；也不引入 ``issued=True`` / 私有哨兵 / factory registry 这类
    只提供虚假安全感的形式主义。
    """

    source_type: str
    source_id: str
    as_of: str
    owner_verification: OwnerVerification
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "ResearchEvidenceRef has no public constructor: 调用方不能仅凭传 "
            "source_type / source_id / verification 字符串声明一条事实。"
            "请使用该 owner 已批准的 factory（market_data: "
            "evidence_ref_from_market_reading；execution: "
            "ai_research_execution_adapter.evidence_ref_from_execution_projection）："
            "identity 与核验维度都由 owner 投影派生，调用方不参与"
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
        if not isinstance(self.owner_verification, OwnerVerification):
            raise TypeError(
                "research evidence requires an OwnerVerification; got "
                f"{type(self.owner_verification).__name__} — 核验结论必须由 owner 的 "
                "factory 发布，而不是由调用方传一个同名字段的对象"
            )
        object.__setattr__(
            self, "detail", _deep_freeze(self.detail, what="research evidence detail"),
        )

    # ---------- owner-neutral fact-level questions ----------

    @property
    def is_verified(self) -> bool:
        """这条事实是否通过**它自己那个 owner** 的核验。

        research hypothesis 只消费这一项 —— 它**不是** ``verification == "verified"``
        的字符串比较，因此 market / execution / news 各自的状态词不会互相污染。
        """
        return self.owner_verification.is_verified

    @property
    def verification_attributes(self) -> Mapping[str, Any]:
        """owner-specific 的不可变附加维度（research core 不解释其语义）。"""
        return self.owner_verification.attributes

    @property
    def verification(self) -> str:
        """这条事实的**owner-native 状态词**（兼容读法）。

        对 ``market_data`` 它与 B2C-2 之前逐字相同（R24 的状态词）；对其它 owner 它是
        该 owner 自己的状态词。**research core 不比较这个字符串** —— 判据是
        :attr:`is_verified`。保留它是因为读侧 / 前端 / 持久化投影都在用它做展示与审计。
        """
        return self.owner_verification.status

    @property
    def verification_method(self) -> str | None:
        """**market 兼容语义，不是通用 owner 维度。**

        R24 的 ``verified`` 可能来自 ``cross_source`` 或 ``coverage_integrity``，因此
        需要双源保证的消费者必须同时读这一项。它**只对 market 事实有定义**：对非
        ``market_data`` 来源返回 ``None``（明确的"不适用"），而不是填
        ``MDC.VERIFICATION_METHOD_NONE`` —— 那个值本身属于 market 词表，用它表示
        "非 market owner" 会把别的 owner 重新塞回 market 的坐标系。
        """
        if self.source_type != EVIDENCE_SOURCE_MARKET_DATA:
            return None
        return self.owner_verification.attributes.get("verification_method")

    @property
    def cross_source_verified(self) -> bool:
        """**market-only 兼容问题**：这条事实是否真的通过了逐票多源交叉核验。

        判据由 market factory 委托 R24 的 :func:`market_data_contract.is_cross_source_verified`
        （本层不比较 ``verification == "verified"``：``coverage_integrity`` 的
        ``verified`` 不是逐票第二源）。

        对非 ``market_data`` 来源返回 ``False``，且**这必须被正确理解**：``False``
        不代表"那条 execution 事实的核验失败"，只代表"这不是一条 market
        cross-source claim"。将来要求 execution / news 提供这个字段是把 market 语义
        强加给别的 owner。
        """
        if self.source_type != EVIDENCE_SOURCE_MARKET_DATA:
            return False
        return bool(self.owner_verification.attributes.get("cross_source_verified"))

    def identity(self) -> tuple[str, str, str]:
        """让两条引用指向**同一条事实**的字段。

        ``source_id`` 已编码 ``policy | kind | subject @ observed_at``，因此两只不同股票
        （或两个不同时点）的 identity 必然不同 —— 不会被误认成同一条事实而静默去重。
        """
        return (self.source_type, self.source_id, self.as_of)

    def fact_state(self) -> tuple:
        """这条事实被 owner 观测到的**事实内容**状态（用于冲突检测）。

        **owner-neutral**：核验维度取 owner 发布的整个
        :meth:`OwnerVerification.canonical`（三态结论 + owner 状态词 + owner-specific
        attributes），而不是 market 的 ``(verification, verification_method)`` 那一对。
        因此"同一 identity + owner 核验状态不同 → :class:`EvidenceConflict`"对任何
        owner 都成立，而 market 的
        ``verified + cross_source`` vs ``verified + coverage_integrity`` 仍然算冲突
        （attributes 参与了 canonical form）。

        刻意**不**包含 ``status`` 那种 reading 级展示判定 —— 同一份快照在不同 ``now``
        下可能是 fresh 或 stale，那属于时效而非"事实变了"。
        """
        return (
            self.owner_verification.canonical(),
            self.detail.get("content_fingerprint"),
        )

    def projection(self) -> dict[str, Any]:
        """给 API / 前端的稳定投影：只 render，不重算核验语义。

        已有 market key/value 刻意**逐字不变**（``verification`` /
        ``verification_method`` / ``cross_source_verified``），B2C-2 只做 additive：
        新增 ``is_verified`` 与 ``verification_attributes`` 两个 owner-neutral 维度。
        非 market 事实的 ``verification_method`` 是 ``None``，``cross_source_verified``
        是 ``False`` —— 见 :attr:`verification_method` 的说明，它们是 market-only
        问题而不是通用核验结论。
        """
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "as_of": self.as_of,
            "verification": self.verification,
            "verification_method": self.verification_method,
            "cross_source_verified": self.cross_source_verified,
            "is_verified": self.is_verified,
            "verification_attributes": _plain(self.owner_verification.attributes),
        }


def _subject_of(snapshot: Any) -> str:
    """从 R24 snapshot 取**观测主体** —— 这条事实是关于"谁"的。

    ``symbol_quote`` 把单票放进 ``rows=(envelope,)``，因此 ``code`` 是主体。横截面
    (``full_market`` 等) 没有单一主体，用行数做 scope，而不是编一个假 id。

    这一项是 identity 的唯一性来源：``live_market @ 10:30`` 对 600000 与 000001 是
    **两条不同的事实**，只靠 policy + 时点会撞成一条（并被静默去重）。
    """
    if snapshot is None:
        return ""
    by_code = snapshot.by_code()
    if by_code:
        return "|".join(sorted(by_code))
    return f"rows={snapshot.row_count}"


def _content_fingerprint(snapshot: Any) -> str:
    """这份快照**内容**的稳定指纹（用于区分"同一条事实"与"内容变了"）。

    刻意只覆盖事实性字段并按 key 排序：同一个 identity 下内容不同就是
    :class:`EvidenceConflict`，而不是静默去重 —— 把 payload 完全排除在冲突判断之外
    会让"报价被悄悄改写"看起来像"同一条事实"。
    """
    if snapshot is None:
        return ""
    parts: list[str] = []
    for row in snapshot.rows:
        if not isinstance(row, Mapping):
            parts.append(repr(row))
            continue
        parts.append("&".join(f"{key}={row[key]!r}" for key in sorted(row)))
    return "||".join(parts)


def _market_evidence_identity(reading: Any) -> tuple[str, str, str]:
    """从 R24 reading **派生** ``(source_id, as_of, content_fingerprint)``。

    调用方完全不参与。identity 需要四个正交维度才能唯一标识一条市场事实：

        policy          哪一套读取口径
        kind            这是哪一类事实（symbol_quote / full_market …）
        subject         关于谁（单票 code；横截面用 scope）
        observed_at     哪一份快照

    早期版本只有 ``policy @ observed_at``，于是同一时刻的两只**不同股票**得到完全相同的
    identity —— 不只 identity 相同，连冲突状态也相同，因此会被静默去重成一条事实。
    这是本 contract 自己的 correctness 缺陷，不是理论问题。

    缺 policy / kind / 时点即 fail closed：无法稳定识别的事实不得进入研究链路。
    """
    policy = str(reading.policy_name or "").strip()
    if not policy:
        raise ValueError(
            "market reading carries no policy — 无法派生稳定的 evidence identity；"
            "研究层不接受无口径的事实"
        )
    snapshot = reading.snapshot
    kind = str(getattr(snapshot, "kind", "") or "").strip()
    if not kind:
        raise ValueError(
            "market reading snapshot carries no kind — 无法区分 symbol_quote 与横截面，"
            "identity 会碰撞"
        )
    observed_at = str(getattr(snapshot, "observed_at", "") or "").strip()
    as_of = snapshot.as_of
    stamp = observed_at or str(as_of or "").strip()
    if not stamp:
        raise ValueError(
            "market reading snapshot carries neither observed_at nor as_of — "
            "PIT 不可证明的事实不得进入研究链路"
        )
    source_id = f"{policy}|{kind}|{_subject_of(snapshot)}@{stamp}"
    return source_id, as_of, _content_fingerprint(snapshot)


def _issue_evidence_ref(
    *, source_type: str, source_id: Any, as_of: Any,
    owner_verification: OwnerVerification, detail: Mapping[str, Any],
) -> ResearchEvidenceRef:
    """签发一个 ref。只有**已批准的 owner factory** 调用它。

    调用者集合是**精确 allowlist**（由 ``test_ai_research_evidence_ownership_guard``
    结构扫描强制，module + enclosing function，不做控制流分析）：

    ```text
    ai_research_contract.evidence_ref_from_market_reading
    ai_research_execution_adapter.evidence_ref_from_execution_projection
    ```

    绕过 ``__init__``（它恒抛错）并在设置完全部字段后跑 ``__post_init__``，
    使校验逻辑仍然只有一份、且紧挨字段定义。

    ``owner_verification`` 是**必须**的具名参数：调用方不能靠省略它来签发一条"没有核验
    结论"的证据，也不能靠传一个 duck-typed 对象绕过 :class:`OwnerVerification` 的校验。
    """
    ref = object.__new__(ResearchEvidenceRef)
    object.__setattr__(ref, "source_type", source_type)
    object.__setattr__(ref, "source_id", source_id)
    object.__setattr__(ref, "as_of", as_of)
    object.__setattr__(ref, "owner_verification", owner_verification)
    object.__setattr__(ref, "detail", detail)
    ref.__post_init__()
    return ref


def evidence_ref_from_market_reading(reading: Any) -> ResearchEvidenceRef:
    """把 R24 的 typed projection 映射成 :class:`ResearchEvidenceRef`。

    **market_data 的 owner factory。** 要求 ``reading`` 是真正的
    ``market_data_contract.MarketDataReading``（不是任何带 ``projection()`` 的
    duck-typed 对象），并从它派生全部身份与核验维度：

    * ``source_id`` / ``as_of`` / 内容指纹由 :func:`_market_evidence_identity` 从
      **reading 自身**派生（policy + kind + subject + 观测时点），**调用方不提供** ——
      因此既无法把一份事实改名成多条，也无法对两只不同股票造出同一个 identity；
    * 核验维度由 :func:`_market_owner_verification` 归口成 owner-neutral 的
      :class:`OwnerVerification`：R24 仍然是**唯一**的 market 核验 authority
      （合法性、``cross_source_verified`` 全部问它），本层只把它的状态词映射到中性三态。

    注意这**不等于**"这条事实一定由 owner 产生" —— 见
    :class:`ResearchEvidenceRef` 的已知限制。

    刻意**没有** ``source_id`` 参数：一个由调用方命名的 identity 不是 identity，
    而是一个可以被用来绕过去重与冲突检测的自由字符串。
    """
    if not isinstance(reading, MDC.MarketDataReading):
        raise TypeError(
            "evidence must be mapped from a typed R24 projection: "
            f"expected market_data_contract.MarketDataReading, got {type(reading).__name__}"
        )

    projection = reading.projection()
    source_id, as_of, fingerprint = _market_evidence_identity(reading)
    return _issue_evidence_ref(
        source_type=EVIDENCE_SOURCE_MARKET_DATA,
        source_id=source_id,
        as_of=as_of or projection.get("observed_at"),
        owner_verification=_market_owner_verification(projection, reading.snapshot),
        detail={
            "observed_at": projection.get("observed_at"),
            "policy": projection.get("policy"),
            "status": projection.get("status"),
            "kind": getattr(reading.snapshot, "kind", None),
            "subject": _subject_of(reading.snapshot),
            "content_fingerprint": fingerprint,
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
        """这条事实的核验状态 —— **继承**自证据引用，AI 层无权改写。

        owner-native：market 事实是 R24 的状态词，其它 owner 是它自己的状态词。
        """
        return self.evidence_ref.verification

    @property
    def verification_method(self) -> str | None:
        """**market 兼容属性**：R24 的 ``verified`` 通过哪套 policy 得到。

        对非 ``market_data`` 事实返回 ``None`` —— 它不是一个通用 owner 维度，让
        execution / news 提供它就是把 market 语义强加给别的 owner。owner-neutral 的
        核验维度请读 :attr:`is_verified` / :attr:`verification_attributes`。
        """
        return self.evidence_ref.verification_method

    @property
    def is_verified(self) -> bool:
        """owner 是否判定这条事实通过了它自己那套核验（owner-neutral）。"""
        return self.evidence_ref.is_verified

    @property
    def verification_attributes(self) -> Mapping[str, Any]:
        """owner-specific 的不可变核验维度（research core 不解释其语义）。"""
        return self.evidence_ref.verification_attributes

    def projection(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "as_of": self.as_of,
            "source": self.source,
            "evidence_id": self.evidence_id,
            "verification": self.verification,
            "verification_method": self.verification_method,
            "is_verified": self.is_verified,
            "evidence": self.evidence_ref.projection(),
        }


# ---------------------------------------------------------------------------
# hypothesis evidence —— 事实 + 它对 thesis 的关系（两个维度的交汇点）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HypothesisEvidence:
    """把一条类型化事实与一个**显式声明**的 relation 绑在一起。

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
        """这条事实本身是否通过 owner 的核验（fact level）。

        **判据完全来自 owner 的发布结果**（``ref.is_verified`` → ``OwnerVerification``），
        不是 ``verification == MDC.VERIFICATION_VERIFIED`` 这种 market 字符串比较。因此
        execution / news / adaptive 的核验结论不需要翻译成 market 词就能进入假设判定，
        而 market 的行为逐字不变。
        """
        return self.ref.is_verified

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
            seen = sorted({entry.ref.owner_verification.status for entry in group})
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

    reason 必须与事实层的核验结论**一致**：一条 verified 的事实若只是
    ``relation=context``，原因只能是"没有 supporting evidence"，绝不能报成
    ``evidence_not_verified`` —— 那会把刚刚拆开的两个维度又混回去。
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

    # 没有可信的 supports / contradicts。依次区分三种不同的"不足以判断"：
    # 1. owner 自己都没核验成功（源不可用 / 多源否证）—— 障碍最根本；
    # 2. 有 supports 关系，但那条事实未通过核验（单源 / 从未核验）；
    # 3. 事实可信，却没有一条与 thesis 相关（例如只有 context）。
    # 三者都不足以判断，但给用户的原因必须准确且互不相同。
    #
    # 第 1 条读的是 owner-neutral 的 ``source_unusable`` 结论，**不是**
    # ``verification in (MDC.VERIFICATION_UNAVAILABLE, MDC.VERIFICATION_DISAGREEMENT)``
    # —— 后者是 market 词表，会让其它 owner 的"核验源不可用"永远识别不出来。
    if any(e.ref.owner_verification.source_unusable for e in evidence):
        return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_EVIDENCE_UNAVAILABLE
    if any(e.relation == RELATION_SUPPORTS for e in evidence):
        return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_EVIDENCE_NOT_VERIFIED
    return HYPOTHESIS_INSUFFICIENT_EVIDENCE, RESEARCH_REASON_NO_SUPPORTING_EVIDENCE


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
