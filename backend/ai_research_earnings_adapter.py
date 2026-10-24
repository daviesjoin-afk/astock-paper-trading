# -*- coding: utf-8 -*-
"""业绩公告 owner fact → typed research evidence 的 **adapter**。

依赖方向（单向，不可反转）：

```text
earnings_disclosure_facts     （纯判定：原文复算 / claim 核验）
            ↓
earnings_disclosure_ledger    （owner：身份、PIT 可用性、修正链的 authority）
            ↓
ai_research_earnings_adapter  （本模块：唯一把 owner 投影翻译成 typed evidence 的地方）
            ↓
ai_research_contract          （research core：不 import 任何 owner / DB 模块）
```

──────────────── 为什么新开一个 source type，而不是复用 news ────────────────

业绩公告事实与新闻事实是**两套不同的 authority**，混进同一个词表会让
``ai_research_news_adapter`` 同时为两种核验语义负责：

```text
news       identity = event_key（抓取到的一条报道）；核验 = 能不能追溯到一条来源
disclosure identity = 代码 + 报告期 + 公告类型 + 公告编号；核验 = 从公告原文复算出数字
```

前者的"可追溯"回答不了"这几个数字是不是原文写的"，后者也不该被"有一条链接"降级。
因此新增 :data:`ARC.EVIDENCE_SOURCE_DISCLOSURE`，细分仍然放在 ``detail["kind"]``。

──────────────────────── status → owner-neutral outcome ────────────────────────

映射是一张**完整表**，不是 ``if/else`` 链，每次签发前做**双向**一致性检查（表键 ==
owner 的 ``VERIFICATION_STATUSES``），且不缓存 —— owner 新增状态时本层必须在**下一次
调用**就 fail closed，而不是静默落进 ``else``。

三态语义各有理由：

* ``verified`` → ``OWNER_OUTCOME_VERIFIED``：原文复算出了数字，且公告未被修正取代。
* ``superseded`` → ``OWNER_OUTCOME_UNVERIFIED``：事实本身可能自洽，但**已经不是当前
  事实** —— 这是"没有通过核验"，不是"核验不了"。
* ``unparsable`` → ``OWNER_OUTCOME_SOURCE_UNUSABLE``：障碍出在**核验过程**（我们读不懂
  这份版式），而不是"这条事实不够好"。因此它必须给出**不同**的 research reason
  （``evidence_unavailable`` vs ``evidence_not_verified``）。

──────────────────────── attributes 只放"事实级"维度 ────────────────────────

``attributes`` 只包含一条事实**不随时间改变**的核验维度：``kind`` /
``report_period`` / ``is_correction`` / ``verification_method`` / ``has_figures``。

刻意**不**放 ``superseded_by`` 与最近一次 claim 复核状态：那两个会随外部事件变化。
``OwnerVerification.canonical()`` 参与同一 identity 的冲突检测，把变化量塞进去会让
"同一份事实"在两次读取之间被报成互相矛盾 —— 冲突检测从此失去意义。
claim 复核结论属于**研究过程**的审计材料，由 ledger 的 projection 承载，不进 evidence。

──────────────────────── 已知限制 ────────────────────────

```text
contract-issued earnings projection:        CLOSED（无公开构造器）
caller self-declared identity / status:     CLOSED
physical database origin / provenance:      OPEN / REQUIRED（同各 owner adapter）
production caller:                          0（本轮只交付能力与守卫登记）
```

调用方仍可自造一个 SQLite connection 并调用 owner 的 public read 拿到投影。
"可信数据库 provenance"是 R27 的遗留前置条件，本层不声称已关闭它。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import ai_research_contract as ARC
import earnings_disclosure_ledger as LEDGER

__all__ = (
    "evidence_ref_from_earnings_projection",
)


#: 业绩公告 owner 的核验结论 → owner-neutral 三态。**完整表，不是 catch-all。**
_EARNINGS_OUTCOME_BY_STATUS = {
    LEDGER.VERIFICATION_VERIFIED: ARC.OWNER_OUTCOME_VERIFIED,
    LEDGER.VERIFICATION_SUPERSEDED: ARC.OWNER_OUTCOME_UNVERIFIED,
    LEDGER.VERIFICATION_UNPARSABLE: ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
}


def _earnings_outcome_mapping_problems(mapping: Mapping, statuses: Any) -> list[str]:
    """显式 outcome 映射与 owner 状态闭集的**双向**一致性检查（纯函数）。

    抽成纯函数是为了让"缺一个合法状态"与"多一个已不存在的状态"两个方向都能被**直接
    测到**，而不是只能靠改 owner 源码来验 —— 与
    ``ai_research_news_adapter._news_outcome_mapping_problems`` 同一手法。
    """
    problems: list[str] = []
    missing = sorted(set(statuses) - set(mapping))
    if missing:
        problems.append(
            f"公告 owner 已有但没有登记 owner-neutral outcome 的状态：{missing}"
            "（research 层不得替 owner 猜一个新状态的含义）"
        )
    unknown = sorted(set(mapping) - set(statuses))
    if unknown:
        problems.append(
            f"outcome 映射里有公告 owner 已不认识的状态：{unknown}"
            "（说明这张表与 owner 的状态闭集漂移了）"
        )
    return problems


def _earnings_owner_verification(projection: Any) -> ARC.OwnerVerification:
    """公告 owner 的核验声明 → owner-neutral 核验发布结果。**earnings owner factory。**"""
    problems = _earnings_outcome_mapping_problems(
        _EARNINGS_OUTCOME_BY_STATUS, LEDGER.VERIFICATION_STATUSES,
    )
    if problems:
        raise ValueError(
            "earnings owner verification vocabulary drifted from the explicit outcome "
            "mapping: " + "; ".join(problems)
        )

    status = projection.verification_status
    if status not in _EARNINGS_OUTCOME_BY_STATUS:
        raise ValueError(f"{status!r} is not an earnings owner verification status")
    return ARC.OwnerVerification(
        outcome=_EARNINGS_OUTCOME_BY_STATUS[status],
        status=status,
        attributes={
            "kind": projection.kind,
            "report_period": projection.report_period,
            "is_correction": bool(projection.is_correction),
            "verification_method": projection.verification_method,
            "has_figures": projection.low_wan is not None,
        },
    )


def _content_fingerprint(projection: Any) -> str:
    """owner 投影**内容**的稳定指纹。

    覆盖 owner 的 ``as_dict()``（身份 / PIT / 来源 / 数字 / 核验状态 / 修正指向）。
    同一 identity 下数字、原文指纹或状态被改写，指纹必然变化 —— 审计与去重都靠它。

    刻意**不**用 ``hash()``（跨进程不稳定）/ ``repr(object)`` / 内存地址 / 当前时间。
    """
    encoded = json.dumps(
        dict(projection.as_dict()), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evidence_ref_from_earnings_projection(projection: Any) -> ARC.ResearchEvidenceRef:
    """把业绩公告 owner 的 typed projection 映射成 :class:`ARC.ResearchEvidenceRef`。

    **earnings owner factory。** 签名里**只有** ``projection``：``source_id`` /
    ``as_of`` / ``verification`` / ``outcome`` 全部从 owner 投影派生，调用方不能自述：

    * ``source_type`` ← :data:`ARC.EVIDENCE_SOURCE_DISCLOSURE`
    * ``source_id`` ← ``projection.identity``（owner 已经把它做成
      ``earnings|代码|报告期|类型|公告编号``，本层不重新定义身份）
    * ``as_of`` ← ``projection.availability_day``，即 owner 从 **``first_seen_at``**
      派生的业务日；**没有** ``published_at`` / ``created_at`` / ``today()`` fallback
    * ``owner_verification`` ← :func:`_earnings_owner_verification` 归口后的值对象
    * ``detail`` ← 最小审计集：``content_fingerprint`` / ``contract_version`` /
      ``announcement_id`` / ``body_sha256`` / ``kind``

    ``body_sha256`` 进 ``detail`` 是有意的：它让"这条证据对应哪一份公告原文"成为
    evidence 自身可核对的信息，而不是只能回 owner 库里翻。

    入口**先做类型校验**：``projection`` 必须是真正的
    :class:`LEDGER.EarningsEventProjection`（``type(...) is``，子类也不算）。dict /
    ``Mapping`` / duck-typed 伪对象一律拒绝 —— 少了这一步，调用方就能自己拼
    ``availability_day`` 与 ``verification_status`` 然后冒充 owner 投影。
    """
    if type(projection) is not LEDGER.EarningsEventProjection:
        raise TypeError(
            "evidence must be mapped from a typed earnings projection: expected "
            "earnings_disclosure_ledger.EarningsEventProjection, got "
            f"{type(projection).__name__} — dict / Mapping / duck-typed / 子类对象不得冒充 "
            "owner 投影"
        )

    return ARC._issue_evidence_ref(
        source_type=ARC.EVIDENCE_SOURCE_DISCLOSURE,
        source_id=projection.identity,
        as_of=projection.availability_day,
        owner_verification=_earnings_owner_verification(projection),
        detail={
            "content_fingerprint": _content_fingerprint(projection),
            "contract_version": projection.version,
            "announcement_id": projection.announcement_id,
            "body_sha256": projection.body_sha256,
            "kind": projection.kind,
        },
    )
