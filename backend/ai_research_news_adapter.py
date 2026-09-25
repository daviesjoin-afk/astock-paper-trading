# -*- coding: utf-8 -*-
"""R27-B2C-5 —— news owner fact → typed research evidence 的 **adapter**。

依赖方向（单向，不可反转）：

    news_learning                          （owner：事件身份、PIT 可用性、核验结论的 authority）
              ↓
    ai_research_news_adapter               （本模块：唯一把 news owner 投影翻译成 typed evidence 的地方）
              ↓
    ai_research_contract                   （research core：不 import 任何 news / DB 模块）

**为什么这是一个真实的 adapter boundary，而不是 wrapper 债。**

``news_learning`` 是 DB-backed owner（它自己建表和抓取）。让 ``ai_research_contract`` 直接
import 它，等于让纯契约依赖一个带 DB、带网络 writer 的 owner；反过来让 owner import
research，又会反转依赖方向（AIG-02 / RG-04 已禁止）。本模块是 roadmap 本身要求的那个
接缝：它消费 owner 已发布的 typed 投影，产出一个 research 侧的 typed 引用，并且是**唯一**
一处同时认识这两套词表的地方。

它刻意**不**新增 registry framework / ``BaseAdapter`` / service / manager / repository /
facade / 第二套 source type。公开面只有一个函数
(:func:`evidence_ref_from_news_projection`)。

──────────────── news owner 今天只发布它**能证明**的东西 ────────────────

owner 的核验闭集只有三态，而且**没有 verified**：

```text
single_source     可追溯到一条来源（source_url 或 article_id）—— **不是**核验通过
unverified        ledger 明确记录为未核验（market_major_events.verification_status）
source_unusable   来源不可追溯 → fail closed
```

这不是设计偏好，而是 R27-B2C-5 审计出来的硬事实：``verification_status`` 在整个仓库里
只有**一个** writer，只写得出 ``single_source_linked`` / ``unverified``，且两者都只回答
"有没有 source_url"；没有任何 UPDATE / 第二 writer / 多源复算路径能把它升级。
``news_events`` 连核验列都没有。因此：

```text
CURRENT NEWS OWNER HAS NO VERIFIED STATE
```

本层的归口表因此**不产生** ``OWNER_OUTCOME_VERIFIED``。将来 owner 真的发布核验 writer 时，
人工把那个状态登记进 owner 闭集与下面这张表即可 —— research 侧不得替它猜。

──────────────── 四条"看起来像核验、其实不是"的边界 ────────────────

本 owner 的 ledger 里有四个很容易被误当核验维度使用的表面。本层**全部**拒绝：

```text
evidence_grade            A/B/C/D 是来源可追溯性分级，不是核验结论
verification_status       只表达"有没有链接"，且已由 owner 归口成上表三态
news_source_reputation    来源级聚合统计（credibility_score 是确定性公式）
market_event_candidate_links  "重大事件 → 候选/行业"的相关性映射（启发式 confidence）
```

因此本层：

* **没有** ``confidence`` / ``score`` / ``credibility`` 参数或字段；
* **不读**任何 reputation / candidate-link 表（也不读 DB）；
* 不接受调用方自述 ``verification`` / ``outcome`` / ``evidence_grade`` 覆盖 —
  核验结论只来自 owner 投影的 ``owner_verification_status``。

``grade == "A"`` 不得变成 verified，``credibility_score > 0.8`` 不得变成 verified，
``link.confidence == 0.95`` 也不得变成 verified：这三条在本层是**结构上不可表达**的，
而不是"记得别这么写"。

──────────────── status → owner-neutral outcome：显式穷尽表 ────────────────

映射是一张**完整表**，不是 ``if/else`` 链：

```python
_NEWS_OUTCOME_BY_STATUS = {
    NL.NEWS_OWNER_SINGLE_SOURCE:   ARC.OWNER_OUTCOME_UNVERIFIED,
    NL.NEWS_OWNER_UNVERIFIED:      ARC.OWNER_OUTCOME_UNVERIFIED,
    NL.NEWS_OWNER_SOURCE_UNUSABLE: ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
}
```

每次签发前都做**双向**一致性检查（``mapping keys == owner 的
NEWS_OWNER_VERIFICATION_STATUSES``），且**不缓存**：owner 新增一个状态时本层必须在
**下一次调用**就 fail closed，而不是静默落进 ``else`` 被当成 ``unverified``。

``single_source`` 归 ``OWNER_OUTCOME_UNVERIFIED`` 而不是 ``source_unusable``：单源事实
本身是可信的、只是**没有第二来源复算过它**，障碍不在核验过程。这与
``ai_research_contract.OWNER_OUTCOME_UNVERIFIED`` 的契约文本（"单源 / 从未核验"）逐字一致。

``OwnerVerification.status`` 逐字保留 owner 的状态词；``attributes`` 只放本 owner 的核验
维度（``record_kind`` / ``evidence_grade`` / ``source_type`` / ``has_source_url``，以及
major event 的 ``ledger_verification_status``）。刻意**没有** ``confidence`` /
``score`` / ``credibility``，也刻意**不**引入 ``verification_method`` /
``cross_source_verified`` —— 那两个是 **market-only** 问题，对 news 保持"不适用"，
不是"核验失败"。

──────────────── identity / PIT / 内容指纹 ────────────────

``source_id`` 完全由 projection 派生：

```text
news_event|<event_key>
market_major_event|<event_key>
```

调用方不能提供 ``source_id`` / ``as_of`` / ``verification`` / ``outcome``：本函数的签名里
**只有** projection。``as_of`` 只能来自 ``NewsFactProjection.availability_day``，即 owner 从
``first_seen_at`` 派生的业务日 —— 本项目**不**接受 ``published_at`` / ``created_at`` /
``today()`` 作为 fallback。news 的可用性 authority 是 ``first_seen_at``（系统第一次观测到
它的时刻），不是来源声称的发布时间。

内容指纹覆盖 record_kind / identity / canonical_hash / first_seen_at / availability_day /
来源身份 / event_type / evidence_grade / owner 核验状态，以及事实 payload 的 canonical
形式，用 ``json.dumps(sort_keys=True, separators=(",", ":"))`` + sha256。刻意**不**用
``hash()``（跨进程不稳定）/ ``repr(object)`` / 内存地址 / 当前时间 / 随机数。

``ResearchEvidenceRef.detail`` 只保持最小审计信息（``content_fingerprint`` /
``contract_version`` / ``record_kind``），**不**复制 owner 的 factual payload
（标题 / themes / significance_score 等）。正确分层仍然是：

```text
NewsFactProjection        owner factual truth
ResearchEvidenceRef       identity + verification + fingerprint
InformationEvent.payload  一次 research observation 投影
```

──────────────── 已知限制：owner-origin provenance = OPEN ────────────────

本层保证的是：

* 调用方不能 raw 构造 ``ResearchEvidenceRef``；
* 不能自述 identity / as_of / status / outcome；
* adapter 只接受**真正的** ``news_learning.NewsFactProjection``（``type(...) is``），
  dict / ``Mapping`` / duck-typed 伪对象 / 子类一律拒绝；
* status → outcome 映射与 owner 的公开闭集双向精确一致，漂移即 fail closed。

它**不**声称关闭 physical database origin：

```text
contract-issued news projection:                      CLOSED
caller self-declared identity / status / as_of:        CLOSED
physical database origin / trusted database provenance: OPEN / REQUIRED
```

调用方仍然可以自造一个 SQLite connection / fixture，调用 owner 的 public read 拿到投影。
那一步属于"可信数据库 provenance"，是 R27 完成的前置条件，本 PR 不把它写成已解决。

──────────────── 本轮没有 production consumer（刻意） ────────────────

B2C-5 的交付物是**能力 + 契约 + 回归**：owner 能发布 typed news 事实、research 侧有唯一
adapter 能把它变成 ``ResearchEvidenceRef``。``deepseek_research._event_evidence()`` 的
runtime 迁移是**后续**的 event_evidence convergence，**不在**本 PR 里：它今天仍然直读
ledger、并且在没有 durable events 时回退去抓 live news。因此：

```text
OPEN / REQUIRED:
deepseek_research._event_evidence still contains legacy direct-ledger reads
and live-fetch fallback.

event_evidence runtime migration = DEFERRED（不是 REMOVED）
```

于是 ``evidence_ref_from_news_projection`` 在 production 里的调用点今天是 **0** —— 这是
预期状态，不是空转，也不是"news runtime 已完全迁移"。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import ai_research_contract as ARC
import news_learning as NL

__all__ = (
    "evidence_ref_from_news_projection",
)


# ---------------------------------------------------------------------------
# owner 词表 → owner-neutral outcome：显式穷尽表，不是 catch-all
# ---------------------------------------------------------------------------

#: news owner 的核验结论 → owner-neutral 三态。
#:
#: **这是一张完整表，不是一个 ``if/else`` 链，而且刻意没有 verified。** catch-all
#: ``else`` 会让 owner 未来新增一个状态时被 research 层**静默**归成一态 —— 那正是
#: "research 替 owner 决定它自己的词是什么意思"。显式表把这件事变成
#: :func:`_news_owner_verification` 的 fail-closed 拒绝，从而要求**人工**决定新状态
#: 如何归口。
#:
#: 三条语义各自有理由：
#:
#: * ``single_source`` → ``unverified``：可追溯，但**没有第二来源复算过**。契约里
#:   ``unverified`` 的定义本身就是"owner 做了核验判定，结论是没有通过（单源 / 从未核验）"。
#: * ``unverified`` → ``unverified``：ledger 明确说未核验，照抄。
#: * ``source_unusable`` → ``source_unusable``：来源不可追溯，障碍在**核验过程**而不是
#:   这条事实不够好，因此必须与上两者给出**不同**的 research reason
#:   （``evidence_unavailable`` vs ``evidence_not_verified``）。
_NEWS_OUTCOME_BY_STATUS = {
    NL.NEWS_OWNER_SINGLE_SOURCE: ARC.OWNER_OUTCOME_UNVERIFIED,
    NL.NEWS_OWNER_UNVERIFIED: ARC.OWNER_OUTCOME_UNVERIFIED,
    NL.NEWS_OWNER_SOURCE_UNUSABLE: ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
}


def _news_outcome_mapping_problems(mapping: Mapping, statuses: Any) -> list[str]:
    """显式 outcome 映射与 owner 状态闭集的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个合法状态"与"多一个已不存在的状态"两个方向都能被
    **直接测到**，而不是只能靠改 owner 源码来验 —— 与
    ``test_ai_research_evidence_ownership_guard._registry_problems``、
    ``ai_research_portfolio_adapter._portfolio_outcome_mapping_problems`` 同一手法。

    两个方向都必须报问题：任何一处漂移都意味着"某个 news 状态的含义"已经不再由人工
    决定，而是由 adapter 的默认分支决定。
    """
    problems: list[str] = []
    missing = sorted(set(statuses) - set(mapping))
    if missing:
        problems.append(
            f"news owner 已有但没有登记 owner-neutral outcome 的状态：{missing}"
            "（research 层不得替 news owner 猜一个新状态的含义）"
        )
    unknown = sorted(set(mapping) - set(statuses))
    if unknown:
        problems.append(
            f"outcome 映射里有 news owner 已不认识的状态：{unknown}"
            "（说明这张表与 owner 的状态闭集漂移了）"
        )
    return problems


def _news_owner_verification(projection: Any) -> ARC.OwnerVerification:
    """news owner 的核验声明 → owner-neutral 核验发布结果。**news owner factory。**

    owner authority 完整保留在 ``news_learning`` 一侧，本函数只做**归口**：

    1. 状态闭集的合法性问 owner（``NL.NEWS_OWNER_VERIFICATION_STATUSES``），本层不重写词表；
    2. 把 owner 的状态词**按显式穷尽表**归到中性三态；
    3. ``status`` 逐字保留 owner 的状态词，``attributes`` 只放本 owner 的核验维度。

    第 2 步是本层唯一的解释行为，方向是"把 owner 的词映射到中性结论"，**不是**"用中性
    结论替代 owner 判定"。research core 反过来看不到这张表。

    **fail closed 是这里的关键性质。** 映射表必须与 owner 的状态闭集**精确相等**：

    * owner 新增一个状态 → 下表查不到 → **拒绝**（而不是静默归成 ``unverified``）；
    * 表里出现 owner 已不认识的状态 → 同样拒绝（说明这张表已经漂移）。

    刻意**不**缓存：owner 词表漂移必须在**下一次调用**就 fail closed，而不是等进程重启。

    刻意**不**看 ``evidence_grade`` / source reputation / candidate-link confidence：
    那三个都不属于"这条事件是否被 owner 核验过"。
    """
    problems = _news_outcome_mapping_problems(
        _NEWS_OUTCOME_BY_STATUS, NL.NEWS_OWNER_VERIFICATION_STATUSES,
    )
    if problems:
        raise ValueError(
            "news owner verification vocabulary drifted from the explicit outcome mapping: "
            + "; ".join(problems)
        )

    status = projection.owner_verification_status
    if status not in _NEWS_OUTCOME_BY_STATUS:
        raise NL.NewsFactContractError(
            "unknown_owner_status", f"{status!r} is not a news owner status",
        )
    attributes = {
        "record_kind": projection.record_kind,
        "evidence_grade": projection.evidence_grade,
        "source_type": projection.source_type,
        "has_source_url": projection.source_url is not None,
    }
    # major event 的 ledger 原始状态词只作审计 attribute；归口判据**只看** owner 派生
    # 的那个状态（见 owner 侧 __post_init__ 的自洽强制）。
    if projection.ledger_verification_status is not None:
        attributes["ledger_verification_status"] = projection.ledger_verification_status
    return ARC.OwnerVerification(
        outcome=_NEWS_OUTCOME_BY_STATUS[status],
        status=status,
        attributes=attributes,
    )


# ---------------------------------------------------------------------------
# 事实内容指纹 —— "同一条事实"与"内容变了"的判据
# ---------------------------------------------------------------------------


def _content_fingerprint(projection: Any) -> str:
    """news 事实**内容**的稳定指纹（区分"同一条事实"与"内容变了"）。

    覆盖 owner 身份 / PIT / 来源身份 / 核验状态，以及事实 payload 的 canonical 形式。
    同一 ``event_key`` 下标题、grade、来源链接或核验状态被改写，必须报
    ``EvidenceConflict``，而不是被当成"同一条事实"静默去重。

    ``themes`` / ``affected_industries`` 展开成**有序 list**（owner 侧已把它们冻成元组并
    保留 ledger 顺序），刻意**不**指纹 ``repr(object)`` / 内存地址 / ``hash()`` /
    当前时间：那会让"内容没变"被报成"内容变了"（或反之），冲突检测随之失去意义。
    """
    payload = {
        "version": projection.version,
        "record_kind": projection.record_kind,
        "identity": projection.identity,
        "canonical_hash": projection.canonical_hash,
        "first_seen_at": projection.first_seen_at,
        "availability_day": projection.availability_day,
        "source_name": projection.source_name,
        "source_type": projection.source_type,
        "source_url": projection.source_url,
        "article_id": projection.article_id,
        "event_type": projection.event_type,
        "evidence_grade": projection.evidence_grade,
        "owner_verification_status": projection.owner_verification_status,
        "ledger_verification_status": projection.ledger_verification_status,
        "code": projection.code,
        "title": projection.title,
        "published_at": projection.published_at,
        "significance_score": projection.significance_score,
        "themes": [list(item) for item in projection.themes],
        "affected_industries": list(projection.affected_industries),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 唯一公开 factory
# ---------------------------------------------------------------------------


def evidence_ref_from_news_projection(projection: Any) -> ARC.ResearchEvidenceRef:
    """把 news owner 的 typed projection 映射成 :class:`ResearchEvidenceRef`。

    **news owner factory。** 签名里**只有** ``projection`` —— 调用方不能提供
    ``source_id`` / ``as_of`` / ``verification`` / ``outcome`` / ``evidence_grade``。
    这些全部从 owner 投影派生：

    * ``source_type`` ← ``ARC.EVIDENCE_SOURCE_NEWS``（**不**新建 ``news_event`` /
      ``major_news`` 之类的第二套 source type；细分放在 ``detail["record_kind"]``，
      因此 ``InformationEvent.kind`` 自动仍然是 ``news_observed``）
    * ``source_id`` ← ``<record_kind>|<event_key>``（identity authority 是 news owner；
      本层不重新定义它，也不接受调用方命名）
    * ``as_of`` ← ``projection.availability_day``，即 owner 从 ``first_seen_at`` 派生的
      业务日；**没有** ``published_at`` / ``created_at`` / ``today()`` fallback
    * ``owner_verification`` ← :func:`_news_owner_verification` 归口的 owner-neutral 值对象
    * ``detail["content_fingerprint"]`` ← :func:`_content_fingerprint` 的确定性指纹

    入口**先做类型校验**：``projection`` 必须是**真正的**
    ``news_learning.NewsFactProjection``（``type(...) is``，子类也不算）。dict /
    ``Mapping`` / duck-typed 伪对象（哪怕有 ``identity`` / ``first_seen_at`` /
    ``owner_verification_status`` 属性）一律拒绝。少了这一步，调用方就能自己拼
    ``identity`` / ``availability_day`` / ``owner_verification_status`` 然后冒充 news
    owner 投影 —— 那会让 owner contract 这个边界形同虚设。

    ``detail`` **不**复制 owner 的 factual payload：detail 的职责是 identity + 核验 +
    内容指纹 + 最小审计元数据，需要事件事实的消费者读 owner 投影本身。

    **已知限制**：调用方仍可自造 SQLite connection / fixture 并调用 owner public read，
    因此 *physical database origin / trusted database provenance* 仍是
    **OPEN / REQUIRED**（见模块 docstring）。本层不声称已关闭它。

    .. note::
       B2C-5 结束时 production 里**没有**调用点：本 factory 是 roadmap 要求的能力交付，
       ``deepseek_research._event_evidence`` 的运行时迁移属于后续 event_evidence
       convergence。
    """
    if type(projection) is not NL.NewsFactProjection:
        raise TypeError(
            "evidence must be mapped from a typed news projection: expected "
            "news_learning.NewsFactProjection, got "
            f"{type(projection).__name__} — dict / Mapping / duck-typed / 子类对象不得冒充 "
            "owner 投影"
        )

    return ARC._issue_evidence_ref(
        source_type=ARC.EVIDENCE_SOURCE_NEWS,
        source_id=f"{projection.record_kind}|{projection.identity}",
        as_of=projection.availability_day,
        owner_verification=_news_owner_verification(projection),
        detail={
            "content_fingerprint": _content_fingerprint(projection),
            "contract_version": projection.version,
            "record_kind": projection.record_kind,
        },
    )
