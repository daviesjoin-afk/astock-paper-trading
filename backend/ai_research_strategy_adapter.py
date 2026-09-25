# -*- coding: utf-8 -*-
"""R27-B2C-6 —— adaptive / experiment owner fact → typed research evidence 的 **adapter**。

依赖方向（单向，不可反转）：

    adaptive_risk / adaptive_selection / learning_evaluation   （owner：identity、可用性、
                                                                  核验结论的 authority）
              ↓
    ai_research_strategy_adapter        （本模块：唯一把三家 owner 投影翻译成 typed evidence 的地方）
              ↓
    ai_research_contract                （research core：不 import 任何 owner / DB 模块）

**为什么这是一个真实的 adapter boundary，而不是 wrapper 债。**

三个 owner 都是 DB-backed：``adaptive_risk`` / ``adaptive_selection`` 带自己的 ledger 与
outbox，``learning_evaluation`` 带自己的 append-only manifest。让 ``ai_research_contract``
直接 import 它们，等于让纯契约依赖三个带 DB 的 owner；反过来让 owner import research，
又会反转依赖方向（AIG-02 / RG-04 已禁止）。本模块是 roadmap 本身要求的那个接缝：它消费
owner 已经发布的 typed 投影，产出一个 research 侧的 typed 引用，并且是**唯一**一处同时认识
这三套词表与 research 词表的地方。

它刻意**不**新增 registry framework / ``BaseAdapter`` / service / manager / repository /
facade / 第二套 source type。公开面只有一个函数
(:func:`evidence_ref_from_strategy_projection`)。

────────────── source_type：复用既有的 family seam，不新增 ──────────────

``strategy_research`` 已经在 ``ai_research_contract`` 里存在，本模块**不**新增
``adaptive`` / ``experiment`` / ``candidate`` / ``alpha_experiment`` 这类 source type。
risk 候选 / selection 候选 / experiment 评估的细分放在 **``record_kind``**（继承 owner 自己
的 record kind）与 ``detail`` 里，因此 ``InformationEvent.kind`` 自动仍然是
``strategy_research_observed``。

────────────── 三个 owner，三张闭集，一张显式穷尽归口表 ──────────────

每个 owner **自己**发布极小核验闭集，本模块只把 owner 的状态词归到 owner-neutral 三态：

```python
adaptive_risk        RISK_FACT_VERIFICATION_STATUSES
adaptive_selection   SELECTION_FACT_VERIFICATION_STATUSES
learning_evaluation  EXPERIMENT_FACT_VERIFICATION_STATUSES
```

归口是一张**完整表**，不是 ``if/else`` 链。每次签发前都做**双向**一致性检查（表必须恰好
覆盖三家闭集的并集，且三家闭集互不重叠），且**不缓存**：owner 新增一个状态时本层必须在
**下一次调用**就 fail closed，而不是静默落进 ``else`` 被当成 ``unverified``。

**本层不产生任何 vocabulary。** 表里的键全部是 owner 模块的常量；本模块只是把它们指向
中性结论。三家闭集刻意**互不重叠**（``risk_candidate_recorded`` /
``selection_candidate_recorded`` / ``experiment_evaluation_recorded``），因此每个键的归属
是唯一的，漂移检查可以逐 owner 精确归因，而不是靠前缀猜。

────────────── 四条"看起来像核验、其实不是"的边界 ──────────────

```text
candidate lifecycle status      waiting_data / shadow_candidate / eligible_* / applied …
                                是生命周期与资格，不是核验；本层结构性看不到它
run_date                       是"这次评估关于哪一天"的标签，不是可用瞬间
evaluation_contract_ok /
evaluation_admitted            是"这份评估是否通过自己的契约门禁"，不是"策略为真"
promotion_science.promotable   是晋升结论，不是核验结论
```

因此本层**没有** ``confidence`` / ``score`` / ``promotable`` / ``eligible`` 参数或字段，
也**不读** promotion verdict：``evaluation_admitted=False`` 或 ``promotable=False`` 照样可以
是一条 ``OWNER_OUTCOME_VERIFIED`` 的事实 — "科学门禁明确判定不通过"本身是可信事实。这与
execution 侧 "not_executed but verified fact" 是同一种区分。

────────────── identity / as_of：只从 owner 投影派生 ──────────────

```text
risk_candidate|<candidate_id>@<revision_at>
selection_candidate|<candidate_id>@<revision_at>
experiment_evaluation|<evaluation_fingerprint>
```

``as_of`` 只来自 ``projection.availability_day``，即 owner 从**自己证明的**可用瞬间按 owner
时区派生的业务日：候选是 ``updated_at``（每次改写都会推进，因此内容一变 identity 就变），
评估是 manifest 的 ``created_at``（**结果产生瞬间**，**不是** dataset cutoff）。

调用方不能提供 ``source_id`` / ``as_of`` / ``verification`` / ``outcome``：本函数的签名里
**只有** projection。

``detail`` 只保持最小审计信息（``content_fingerprint`` / ``contract_version`` /
``record_kind``），**不**复制 owner 的 factual payload。

────────────── 入口只做精确类型判定 ──────────────

``type(projection) is`` 三个**已批准**的 owner 投影类型之一，子类、dict、``Mapping``、
duck-typed 伪对象一律拒绝。刻意不做 ``Protocol`` / ``isinstance`` 宽化：一旦接受"长得像投影
的对象"，调用方就能自己拼 ``availability_day`` 与 ``fact_verification_status`` 来冒充 owner。

────────────── 本层不做任何业务计算 ──────────────

不读 DB、不读墙钟、不联网、不调 LLM，也**不重算** candidate eligibility / evaluation
fitness / promotion gate / risk reduction / selection weights / experiment metrics。这些全部
归各自 owner，本层只做 owner projection → ``ResearchEvidenceRef`` 的映射。

────────────── 已知限制：owner-origin provenance = OPEN ──────────────

本层保证的是：调用方不能 raw 构造 ref、不能自述 identity / as_of / status / outcome、
只能传已批准类型。它**不**声称关闭 physical database origin：

```text
contract-issued typed projection:                      CLOSED
caller self-declared identity / as_of / verification:   CLOSED
physical database origin / trusted provenance:          OPEN / REQUIRED
```

调用方仍然可以自造 SQLite fixture 并调用 owner 的 public read 拿到投影。那一步属于"可信
数据库 provenance"，是 R27 完成的前置条件，本 PR 不把它写成已解决。

────────────── 本轮没有 production consumer（刻意） ──────────────

B2C-6 的交付物是**能力 + 契约 + 回归**：三个 owner 能发布 typed 事实、research 侧有唯一
adapter 能把它变成 ``ResearchEvidenceRef``。``deepseek_research._candidate_evidence()`` 与
``_overfit_evidence()`` 的 runtime 迁移**不在**本 PR 里：

```text
OPEN / REQUIRED:
deepseek_research._candidate_evidence / _overfit_evidence 仍直读 ledger。

candidate_challenge / overfit_watch runtime migration = DEFERRED（不是 REMOVED）
```

于是 :func:`evidence_ref_from_strategy_projection` 在 production 里的调用点今天是 **0** ——
这是预期状态，不是空转，也不是"这些 runtime 已完全迁移"。
"""
from __future__ import annotations

from typing import Any, Mapping

import adaptive_risk as AR
import adaptive_selection as ASEL
import ai_research_contract as ARC
import learning_evaluation as LE

__all__ = (
    "evidence_ref_from_strategy_projection",
)


# ---------------------------------------------------------------------------
# owner 词表 → owner-neutral outcome：显式穷尽表，不是 catch-all
# ---------------------------------------------------------------------------

#: 三家 owner 的核验闭集 → owner-neutral 三态。
#:
#: **这是一张完整表，不是一个 ``if/else`` 链。** catch-all ``else`` 会让 owner 未来新增一个
#: 状态时被 research 层**静默**归成一态 —— 那正是"research 替 owner 决定它自己的词是什么
#: 意思"。显式表把这件事变成 :func:`_strategy_owner_verification` 的 fail-closed 拒绝，
#: 从而要求**人工**决定新状态如何归口。
#:
#: ``*_recorded`` → ``VERIFIED`` 的含义**仅**是"这是一条 owner 自洽签发的可靠事实"，
#: **不是**"这条候选通过了晋级验证"、**不是**"这个策略为真"、**不是**"值得 apply"。
#: 这三句话分别由 lifecycle status、evaluation verdict、promotion verdict 回答，而它们都
#: **不在**这张表里。
#:
#: ``*_unproven`` → ``UNVERIFIED``：记录可读，但 owner 无法自证是它自己签发的事实。
#: 契约里 ``unverified`` 的定义正是"owner 做了核验判定，结论是没有通过"，逐字一致。
_STRATEGY_OUTCOME_BY_STATUS = {
    AR.RISK_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
    AR.RISK_FACT_OWNER_UNPROVEN: ARC.OWNER_OUTCOME_UNVERIFIED,
    ASEL.SELECTION_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
    ASEL.SELECTION_FACT_OWNER_UNPROVEN: ARC.OWNER_OUTCOME_UNVERIFIED,
    LE.EXPERIMENT_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
    LE.EXPERIMENT_FACT_OWNER_UNPROVEN: ARC.OWNER_OUTCOME_UNVERIFIED,
}

#: ``(owner 名, 该 owner 发布的核验闭集)``。用于把归口表的漂移**逐 owner** 归因。
_OWNER_VERIFICATION_VOCABULARIES = (
    ("adaptive_risk", AR.RISK_FACT_VERIFICATION_STATUSES),
    ("adaptive_selection", ASEL.SELECTION_FACT_VERIFICATION_STATUSES),
    ("learning_evaluation", LE.EXPERIMENT_FACT_VERIFICATION_STATUSES),
)


def _strategy_outcome_mapping_problems(mapping: Mapping, vocabularies) -> list[str]:
    """归口表与三家 owner 闭集的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个合法状态"与"多一个已不存在的状态"两个方向都能被
    **直接测到**，而不是只能靠改 owner 源码来验 —— 与
    ``test_ai_research_evidence_ownership_guard._registry_problems``、
    ``ai_research_news_adapter._news_outcome_mapping_problems`` 同一手法。

    三个方向都必须报问题：

    * 某个 owner 有状态而表里没有 → 说明新状态的含义将被默认分支决定；
    * 表里有键不属于任何 owner → 说明表与 owner 词表漂移了；
    * 两个 owner 的闭集**重叠** → 状态词归属不再唯一，逐 owner 归因失效。
    """
    problems: list[str] = []
    owner_statuses: dict[str, set] = {}
    for owner, statuses in vocabularies:
        owner_statuses[owner] = set(statuses)
        missing = sorted(set(statuses) - set(mapping))
        if missing:
            problems.append(
                f"{owner} 已有但没有登记 owner-neutral outcome 的状态：{missing}"
                "（research 层不得替 owner 猜一个新状态的含义）"
            )
    registered = set().union(*owner_statuses.values()) if owner_statuses else set()
    unknown = sorted(set(mapping) - registered)
    if unknown:
        problems.append(
            f"outcome 映射里有任何 owner 都不认识的状态：{unknown}"
            "（说明这张表与 owner 词表漂移了）"
        )
    names = sorted(owner_statuses)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            overlap = sorted(owner_statuses[left] & owner_statuses[right])
            if overlap:
                problems.append(
                    f"owner 核验闭集重叠：{left} 与 {right} 共用 {overlap}"
                    "（状态词归属必须唯一，否则漂移无法逐 owner 归因）"
                )
    return problems


def _strategy_owner_verification(projection: Any) -> ARC.OwnerVerification:
    """三个 owner 的核验声明 → owner-neutral 核验发布结果。

    三家 owner 的 authority 完整保留在各自模块一侧，本函数只做**归口**：

    1. 状态词逐字保留（``OwnerVerification.status``），因此审计能回看 owner 原词；
    2. 把该词**按显式穷尽表**归到中性三态；
    3. ``attributes`` 只放**事实性审计维度**（record kind / 契约版本 / owner 内容指纹），
       刻意**不**放 lifecycle status、``evaluation_contract_ok`` 或任何 verdict —— 把 verdict
       塞进核验维度就是本轮要根除的那类混淆。

    **fail closed 是这里的关键性质。** 表必须恰好覆盖三家闭集的并集：

    * owner 新增一个状态 → 下表查不到 → **拒绝**（而不是静默归成 ``unverified``）；
    * 表里出现任何 owner 都不认识的状态 → 同样拒绝（说明这张表已经漂移）。

    刻意**不**缓存：owner 词表漂移必须在**下一次调用**就 fail closed。
    """
    problems = _strategy_outcome_mapping_problems(
        _STRATEGY_OUTCOME_BY_STATUS, _OWNER_VERIFICATION_VOCABULARIES,
    )
    if problems:
        raise ValueError(
            "adaptive / experiment owner verification vocabulary drifted from the explicit "
            "outcome mapping: " + "; ".join(problems)
        )

    status = str(projection.fact_verification_status)
    if status not in _STRATEGY_OUTCOME_BY_STATUS:
        raise ValueError(
            f"{status!r} is not an adaptive / experiment owner verification status; "
            "research 层不得替 owner 决定一个新状态的含义"
        )
    return ARC.OwnerVerification(
        outcome=_STRATEGY_OUTCOME_BY_STATUS[status],
        status=status,
        attributes={
            "record_kind": str(projection.record_kind),
            "contract_version": str(projection.version),
            "fact_fingerprint": str(projection.content_fingerprint),
        },
    )


# ---------------------------------------------------------------------------
# 入口：精确类型判定 —— 只有三个已批准的 owner 投影类型
# ---------------------------------------------------------------------------


def _exact_record_kind(projection: Any) -> str | None:
    """**精确**类型判定：``type(...) is``，子类 / dict / Mapping / duck-typed 一律拒绝。

    刻意不用 ``isinstance``（子类会溜进来）也不用 ``Protocol`` / ``Mapping`` 检查：一旦接受
    "长得像投影的对象"，调用方就能自己拼 ``availability_day`` 与
    ``fact_verification_status`` 冒充 owner 投影，owner contract 这个边界就形同虚设。

    返回的 record kind 来自**类型本身**，而不是来自一个可被调用方填写的字段 —— identity 由
    类型决定，进一步收紧"自述 identity"的空间。
    """
    if type(projection) is AR.AdaptiveRiskFactProjection:
        return AR.RISK_CANDIDATE_RECORD_KIND
    if type(projection) is ASEL.AdaptiveSelectionFactProjection:
        return ASEL.SELECTION_CANDIDATE_RECORD_KIND
    if type(projection) is LE.ExperimentEvaluationProjection:
        return LE.EXPERIMENT_EVALUATION_RECORD_KIND
    return None


def evidence_ref_from_strategy_projection(projection: Any) -> ARC.ResearchEvidenceRef:
    """把 adaptive / experiment owner 的 typed 投影映射成 :class:`ResearchEvidenceRef`。

    **strategy_research 的 owner factory。** 签名里**只有** ``projection`` —— 调用方不能提供
    ``source_id`` / ``as_of`` / ``verification`` / ``outcome``。这些全部从 owner 投影派生：

    * ``source_type`` ← ``ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH``（**不**新建
      ``adaptive`` / ``experiment`` / ``candidate`` 之类的第二套 source type；细分放在
      ``detail["record_kind"]``，因此 ``InformationEvent.kind`` 自动仍然是
      ``strategy_research_observed``）
    * ``source_id`` ← ``<record_kind>|<owner revision identity>``，其中 record kind 来自
      **类型**，revision identity 由 owner 派生（候选是 ``<id>@<updated_at>``，评估是
      content-addressed ``evaluation_fingerprint``）
    * ``as_of`` ← ``projection.availability_day``，即 owner 从**已证明的**可用瞬间派生的业务日
    * ``owner_verification`` ← :func:`_strategy_owner_verification` 归口的 owner-neutral 值对象
    * ``detail["content_fingerprint"]`` ← owner 在**构造期**算出的确定性内容指纹

    入口**先做精确类型校验**：只接受三个已批准类型的**本类型**实例。

    ``detail`` **不**复制 owner 的 factual payload（params / metrics / blockers 等）：
    detail 的职责是 identity + 核验 + 内容指纹 + 最小审计元数据，需要事实内容的消费者读 owner
    投影本身。

    **已知限制**：调用方仍可自造 SQLite fixture 并调用 owner public read，因此
    *physical database origin / trusted database provenance* 仍是 **OPEN / REQUIRED**
    （见模块 docstring）。本层不声称已关闭它。

    .. note::
       B2C-6 结束时 production 里**没有**调用点：本 factory 是 roadmap 要求的能力交付，
       ``deepseek_research`` 的 candidate_challenge / overfit_watch runtime 迁移属于后续
       convergence。
    """
    record_kind = _exact_record_kind(projection)
    if record_kind is None:
        raise TypeError(
            "evidence must be mapped from an approved typed owner projection; expected "
            f"{AR.AdaptiveRiskFactProjection.__name__} / "
            f"{ASEL.AdaptiveSelectionFactProjection.__name__} / "
            f"{LE.ExperimentEvaluationProjection.__name__}, got "
            f"{type(projection).__name__} — dict / Mapping / duck-typed / 子类对象不得冒充 "
            "owner 投影"
        )

    return ARC._issue_evidence_ref(
        source_type=ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH,
        source_id=f"{record_kind}|{projection.revision_identity}",
        as_of=projection.availability_day,
        owner_verification=_strategy_owner_verification(projection),
        detail={
            "content_fingerprint": str(projection.content_fingerprint),
            "contract_version": str(projection.version),
            "record_kind": record_kind,
        },
    )
