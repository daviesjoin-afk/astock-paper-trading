# -*- coding: utf-8 -*-
"""R27-B2C-7 —— runtime / incident owner fact → typed research evidence 的 **adapter**。

依赖方向（单向，不可反转）：

    adaptive_engine.AdaptiveRunFactProjection      （owner：identity / 可用性 / 核验的 authority）
    paper_trading.PaperJobRunFactProjection         （owner：同上）
              ↓
    ai_research_runtime_adapter        （本模块：唯一把两个 runtime owner 投影翻译成 typed evidence 的地方）
              ↓
    ai_research_contract               （research core：不 import 任何 owner / DB 模块）

**为什么这是一个真实的 adapter boundary，而不是 wrapper 债。** 两个 owner 都是 DB-backed
（``adaptive_engine`` 带 ``adaptive_runs``，``paper_trading`` 带 ``paper_job_runs`` 与调度
租约）。让 ``ai_research_contract`` 直接 import 它们，等于让纯契约依赖两个带 DB 的 owner；
反过来让 owner import research，又会反转依赖方向（AIG-02 / RG-04 已禁止）。本模块是 roadmap
本身要求的那个接缝，并且是**唯一**一处同时认识这两套 owner 词表与 research 词表的地方。

它刻意**不**新增 registry framework / ``BaseAdapter`` / service / manager / repository /
facade。公开面只有一个函数 (:func:`evidence_ref_from_runtime_projection`)。

────────────── source_type：本轮**新增** ``runtime_incident`` ──────────────

刻意**不**复用既有的 ``signal`` 或 ``strategy_research``：

```text
signal              "市场信号" —— run/job 失败不是信号
strategy_research   "策略实验事实" —— runtime 运行失败不是实验事实
```

两个语义都不准确，用它们等于让 runtime 事实冒充另一类 authority。因此本 PR 在
``ai_research_contract`` 里新增 ``EVIDENCE_SOURCE_RUNTIME_INCIDENT``（配套
``EVENT_RUNTIME_INCIDENT_OBSERVED``），并同步四处闭集。细分放在 **``record_kind``**
（继承 owner 自己的 record kind：``adaptive_run`` / ``paper_job_run``）与 ``detail`` 里。

────────────── 两个 owner，两张闭集，一张显式穷尽归口表 ──────────────

每个 owner **自己**发布极小核验闭集，本模块只把 owner 的状态词归到 owner-neutral 三态：

```python
adaptive_engine   ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES
paper_trading     PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES
```

归口是一张**完整表**，不是 ``if/else`` 链。每次签发前都做**双向**一致性检查（表必须恰好
覆盖两家闭集的并集、且两家闭集互不重叠），且**不缓存**：owner 新增一个状态时本层必须在
**下一次调用**就 fail closed。

**本层不产生任何 vocabulary**：表里的键全部是 owner 模块的常量。

────────────── 四条"看起来像核验、其实不是"的边界 ──────────────

```text
runtime lifecycle status     running / completed / failed / killed / intraday_failed …
                              是**事实内容**，不是核验；本层结构性看不到它
incident severity            critical / high / medium / low / info 是 research 结论；
                              本层没有 severity 参数或字段
business rejection           paper_orders 的 blocked / rejected / deferred_capacity /
                              不是系统事故；本层**根本不读** paper_orders
incident root cause          "provider 中断" / "数据损坏" 是 research 结论，不是 owner 事实
```

因此本层**没有** ``severity`` / ``is_incident`` / ``root_cause`` / ``requires_restart``
参数或字段。``failed`` 照样可以是一条 ``OWNER_OUTCOME_VERIFIED`` 的事实 —— verified 的
含义是"这条事实可信"，**不是**"运行成功"。这与 ``completed`` 也一样：``completed``
**不是**通用的 verified 状态。

────────────── identity / as_of：只从 owner 投影派生 ──────────────

```text
adaptive_run   |<run_id>@<runtime_status>@<finished_at>
paper_job_run  |<run_key>@<started_at>|<finished_at>
```

``as_of`` 只来自 ``projection.availability_day``，即 owner 从**自己证明的**可用瞬间按 owner
时区（Asia/Shanghai）派生的业务日：adaptive 的终态是 ``finished_at``、进行中是
``started_at``；paper 的终态是 ``finished_at``、进行中是 ``started_at``。

调用方不能提供 ``source_id`` / ``as_of`` / ``verification`` / ``outcome``：本函数的签名里
**只有** projection。``market_date`` / ``profile_date`` / ``run_date`` 这类业务标签**永不**
进 ``as_of``。

────────────── 入口只做精确类型判定 ──────────────

``type(projection) is`` 两个**已批准**的 owner 投影类型之一，子类、dict、``Mapping``、
duck-typed 伪对象一律拒绝。刻意不做 ``Protocol`` / ``isinstance`` 宽化。

────────────── 本层不做任何业务计算 ──────────────

不读 DB、不读墙钟、不联网、不调 LLM，也**不重算** incident 分级 / job 重试 / 租约状态 /
订单状态分布。这些全部归各自 owner（订单执行事实归 ``execution_owner``，本层不复制它）。

────────────── 已知限制：owner-origin provenance = OPEN ──────────────

```text
contract-issued typed projection:                      CLOSED
caller self-declared identity / as_of / verification:   CLOSED
physical database origin / trusted provenance:          OPEN / REQUIRED
```

调用方仍可自造 SQLite fixture 并调用 owner 的 public read 拿到投影。那一步属于"可信数据库
provenance"，是 R27 完成的前置条件，本 PR 不把它写成已解决。

────────────── 本轮没有 production consumer（刻意） ──────────────

``deepseek_research._incident_evidence()`` 的 runtime 迁移**不在**本 PR 里：

```text
OPEN / REQUIRED:
deepseek_research._incident_evidence 仍直读 adaptive_runs / paper_jobs / paper_orders。

incident_triage runtime migration = DEFERRED（不是 REMOVED）── 留给 R27-B2C-8
```

于是 :func:`evidence_ref_from_runtime_projection` 在 production 里的调用点今天是 **0**。
"""
from __future__ import annotations

from typing import Any, Mapping

import adaptive_engine as AE
import ai_research_contract as ARC
import paper_trading as PT

__all__ = (
    "evidence_ref_from_runtime_projection",
)


# ---------------------------------------------------------------------------
# owner 词表 → owner-neutral outcome：显式穷尽表，不是 catch-all
# ---------------------------------------------------------------------------

#: 两家 owner 的核验闭集 → owner-neutral 三态。
#:
#: **这是一张完整表，不是一个 ``if/else`` 链。** catch-all ``else`` 会让 owner 未来新增一个
#: 状态时被 research 层**静默**归成一态 —— 那正是"research 替 owner 决定它自己的词是什么
#: 意思"。显式表把这件事变成 :func:`_runtime_owner_verification` 的 fail-closed 拒绝。
#:
#: ``*_recorded`` → ``VERIFIED`` 的含义**仅**是"这是一条 owner 自洽签发的可靠事实"，
#: **不是**"这个 run 成功"、**不是**"这个 job 没有故障"、**不是**"这是一次系统事故"。
#: 前三句分别由 lifecycle status 与 research 分级回答，而它们都**不在**这张表里。
#:
#: ``*_unproven`` → ``UNVERIFIED``：记录可读，但 owner 无法自证它的时间戳形状。契约里
#: ``unverified`` 的定义正是"owner 做了核验判定，结论是没有通过"，逐字一致。
_RUNTIME_OUTCOME_BY_STATUS = {
    AE.ADAPTIVE_RUN_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
    AE.ADAPTIVE_RUN_FACT_OWNER_UNPROVEN: ARC.OWNER_OUTCOME_UNVERIFIED,
    PT.PAPER_JOB_RUN_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
    PT.PAPER_JOB_RUN_FACT_OWNER_UNPROVEN: ARC.OWNER_OUTCOME_UNVERIFIED,
}

#: ``(owner 名, 该 owner 发布的核验闭集)``。用于把归口表的漂移**逐 owner** 归因。
_OWNER_VERIFICATION_VOCABULARIES = (
    ("adaptive_engine", AE.ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES),
    ("paper_trading", PT.PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES),
)


def _runtime_outcome_mapping_problems(mapping: Mapping, vocabularies) -> list[str]:
    """归口表与两家 owner 闭集的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个合法状态"与"多一个已不存在的状态"两个方向都能被
    **直接测到**，而不是只能靠改 owner 源码来验 —— 与
    ``test_ai_research_evidence_ownership_guard._registry_problems``、
    ``ai_research_strategy_adapter._strategy_outcome_mapping_problems`` 同一手法。

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


def _runtime_owner_verification(projection: Any) -> ARC.OwnerVerification:
    """两家 owner 的核验声明 → owner-neutral 核验发布结果。

    两家 owner 的 authority 完整保留在各自模块一侧，本函数只做**归口**：

    1. 状态词逐字保留（``OwnerVerification.status``），因此审计能回看 owner 原词；
    2. 把该词**按显式穷尽表**归到中性三态；
    3. ``attributes`` 只放**事实性审计维度**（record kind / 契约版本 / owner 内容指纹 /
       可用瞬间的种类），刻意**不**放 lifecycle status、严重级别或任何 verdict —— 把 verdict
       塞进核验维度就是本轮要根除的那类混淆。
    """
    problems = _runtime_outcome_mapping_problems(
        _RUNTIME_OUTCOME_BY_STATUS, _OWNER_VERIFICATION_VOCABULARIES,
    )
    if problems:
        raise ValueError(
            "runtime owner verification vocabulary drifted from the explicit "
            "outcome mapping: " + "; ".join(problems)
        )

    status = str(projection.fact_verification_status)
    if status not in _RUNTIME_OUTCOME_BY_STATUS:
        raise ValueError(
            f"{status!r} is not a runtime owner verification status; "
            "research 层不得替 owner 决定一个新状态的含义"
        )
    return ARC.OwnerVerification(
        outcome=_RUNTIME_OUTCOME_BY_STATUS[status],
        status=status,
        attributes={
            "record_kind": str(projection.record_kind),
            "contract_version": str(projection.version),
            "fact_fingerprint": str(projection.content_fingerprint),
            "availability_kind": str(projection.availability_kind),
        },
    )


# ---------------------------------------------------------------------------
# 入口：精确类型判定 —— 只有两个已批准的 owner 投影类型
# ---------------------------------------------------------------------------


def _exact_record_kind(projection: Any) -> str | None:
    """**精确**类型判定：``type(...) is``，子类 / dict / Mapping / duck-typed 一律拒绝。

    刻意不用 ``isinstance``（子类会溜进来）也不用 ``Protocol`` / ``Mapping`` 检查：一旦接受
    "长得像投影的对象"，调用方就能自己拼 ``availability_day`` 与
    ``fact_verification_status`` 冒充 owner 投影，owner contract 这个边界就形同虚设。

    返回的 record kind 来自**类型本身**，而不是来自一个可被调用方填写的字段。
    """
    if type(projection) is AE.AdaptiveRunFactProjection:
        return AE.ADAPTIVE_RUN_RECORD_KIND
    if type(projection) is PT.PaperJobRunFactProjection:
        return PT.PAPER_JOB_RUN_RECORD_KIND
    return None


def evidence_ref_from_runtime_projection(projection: Any) -> ARC.ResearchEvidenceRef:
    """把 runtime / incident owner 的 typed 投影映射成 :class:`ResearchEvidenceRef`。

    **runtime_incident 的 owner factory。** 签名里**只有** ``projection`` —— 调用方不能提供
    ``source_id`` / ``as_of`` / ``verification`` / ``outcome``。这些全部从 owner 投影派生：

    * ``source_type`` ← ``ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT``
    * ``source_id`` ← ``<record_kind>|<owner revision identity>``，其中 record kind 来自
      **类型**，revision identity 由 owner 派生（adaptive：``<run_id>@<status>@<finished_at>``；
      paper：``<run_key>@<started_at>|<finished_at>``）
    * ``as_of`` ← ``projection.availability_day``，即 owner 从**已证明的**可用瞬间派生的业务日
    * ``owner_verification`` ← :func:`_runtime_owner_verification` 归口的 owner-neutral 值对象
    * ``detail["content_fingerprint"]`` ← owner 在**构造期**算出的确定性内容指纹

    入口**先做精确类型校验**：只接受两个已批准类型的**本类型**实例。

    ``detail`` **不**复制 owner 的 factual payload（``detail`` JSON / trigger / slot 等）：
    detail 的职责是 identity + 核验 + 内容指纹 + 最小审计元数据，需要事实内容的消费者读
    owner 投影本身。

    **已知限制**：调用方仍可自造 SQLite fixture 并调用 owner public read，因此
    *physical database origin / trusted database provenance* 仍是 **OPEN / REQUIRED**。

    .. note::
       B2C-7 结束时 production 里**没有**调用点：本 factory 是 roadmap 要求的能力交付，
       ``deepseek_research._incident_evidence`` 的 runtime 迁移属于 R27-B2C-8。
    """
    record_kind = _exact_record_kind(projection)
    if record_kind is None:
        raise TypeError(
            "evidence must be mapped from an approved typed runtime owner projection; expected "
            f"{AE.AdaptiveRunFactProjection.__name__} / "
            f"{PT.PaperJobRunFactProjection.__name__}, got "
            f"{type(projection).__name__} — dict / Mapping / duck-typed / 子类对象不得冒充 "
            "owner 投影"
        )

    return ARC._issue_evidence_ref(
        source_type=ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT,
        source_id=f"{record_kind}|{projection.revision_identity}",
        as_of=projection.availability_day,
        owner_verification=_runtime_owner_verification(projection),
        detail={
            "content_fingerprint": str(projection.content_fingerprint),
            "contract_version": str(projection.version),
            "record_kind": record_kind,
        },
    )
