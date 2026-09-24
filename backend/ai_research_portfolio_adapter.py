# -*- coding: utf-8 -*-
"""R27-B2C-4B —— portfolio/accounting owner fact → typed research evidence 的 **adapter**。

依赖方向（单向，不可反转）：

    paper_portfolio_read_model            （owner：记账事实、身份、业务日、核验结论的 authority）
              ↓
    ai_research_portfolio_adapter         （本模块：唯一把 owner 投影翻译成 typed evidence 的地方）
              ↓
    ai_research_contract                  （research core：不 import 任何 portfolio / DB 模块）

**为什么这是一个真实的 adapter boundary，而不是 wrapper 债。**

``paper_portfolio_read_model`` 是 DB-backed owner：import 它就会把 SQLite 读路径 /
schema 探测 / 账本重建绑进"研究契约"。让 ``ai_research_contract`` 直接 import 它，等于让
纯契约依赖一个带 DB 的 owner；反过来让 owner import research，又会反转依赖方向
（AIG-02 / RG-04 已禁止）。本模块是 roadmap **本身要求**的那一个接缝：它消费 owner 已发布
的 typed 投影，产出一个 research 侧的 typed 引用，并且是**唯一**一处同时认识这两套词表的
地方。

它刻意**不**新增 registry framework / ``BaseAdapter`` / service / manager / repository /
facade / 第二套 source type。公开面只有一个函数
(:func:`evidence_ref_from_portfolio_projection`)。

──────────────── 本 owner 只发布它**能证明**的记账事实 ────────────────

owner 只发布三种 fact kind：

```text
cash                      bounded 重建现金（cycle 初始本金 + 已验证成交现金流）
realized_pnl              已验证 SELL 的已实现盈亏（cycle/as-of bounded）
position_cost_summary     durable lots 的开仓数与成本合计
```

它**不发布** NAV / latest_nav / prior_nav / market_value / unrealized_pnl / daily_pnl /
daily_return / benchmark / 估值价格 / quote_status。原因是 provenance，不是懒惰：

```text
NAV = 组合账本（cash + 持仓成本） + R24 market valuation（估值腿）
```

``paper_portfolio_read_model.portfolio_for_context(...)`` 今天接受**调用方提供**的
``valuations`` Mapping；那只能证明"调用方给了一个合法数字"，**不能**证明这些价格来自 R24
owner、通过哪套核验、对应哪个真实 market snapshot。把 ``market_value_status == verified``
当成"market owner 已证明这条估值"，就是把 caller 的裸数字升级成 owner 事实。因此：

* 本模块不 import / 不调用 ``portfolio_for_context``；
* owner 的 typed fact factory 签名里没有 ``valuations`` / ``MarketDataReading`` /
  current quote / latest quote；
* NAV 家族是 **B2C-4C** 的跨 owner 组合产物：

```text
execution owner facts + portfolio/accounting owner facts + R24 market owner facts
        ↓
B2C-4C cross-owner pnl_attribution composition
```

拿不到能证明对应业务日的 R24 valuation 时，结论必须是 ``unknown``，而不是
current quote fallback / cost fallback / ``paper_nav.quote_status == "verified"``。

同理，``paper_nav`` 与 ``paper_positions`` 本轮仍然是 legacy / compatibility：前者允许
``local_snapshot_fallback`` / ``cost_fallback`` 且它的 ``quote_status='verified'`` **不是**
R24 的 ``OwnerVerification``、也没有 typed market evidence identity；后者 R22 已明确是
compatibility-only 投影。两者都不被读来签发 typed fact，本 PR 也不迁它们的 writer。

──────────────── status → owner-neutral outcome：显式穷尽表 ────────────────

portfolio owner 的状态闭集今天只有两态：

```text
verified   owner 证明了这条记账事实（值是一个有限数，或一个 typed cost summary）
unknown    owner 证明不了（归属不可证明 / archived / 数量未证明 / 账本值非有限）
```

映射是一张**完整表**，不是 ``if/else`` 链：

```python
_PORTFOLIO_OUTCOME_BY_STATUS = {
    PPRM.STATUS_VERIFIED: ARC.OWNER_OUTCOME_VERIFIED,
    PPRM.STATUS_UNKNOWN: ARC.OWNER_OUTCOME_UNVERIFIED,
}
```

每次签发前都做**双向**一致性检查（``mapping keys == owner 的 PORTFOLIO_FACT_STATUSES``），
且**不缓存**：owner 新增一个状态时本层必须在**下一次调用**就 fail closed，而不是静默落进
``else`` 被当成 ``unverified`` —— 那正是"research 替 owner 决定它自己的词是什么意思"。

刻意**不**产生 ``source_unusable``：本 owner 今天没有发布"证据源不可用"这个独立状态，
因此 research 侧不得替它猜一个。将来 owner 真的发布了那种状态，人工把它登记进上表即可。

``OwnerVerification.status`` 逐字保留 owner 的状态词；``attributes`` 只放本 owner 的核验
维度（``verification_scope`` / ``fact_contract_version`` / ``read_model_version`` /
``fact_kind``），刻意**没有** ``confidence`` / ``score`` / ``quality_score``，也刻意**不**
引入 ``verification_method`` / ``cross_source_verified`` —— 那两个是 **market-only** 问题，
对非 market owner 保持"不适用"，不是"核验失败"。

──────────────── identity / 内容指纹 ────────────────

``source_id`` 完全由 projection 派生：

```text
<fact_kind>|cycle=<cycle_id>|account=<account_id>
```

调用方不能提供 ``source_id`` / ``as_of`` / ``status`` / ``outcome`` / ``cycle`` /
``account``。``as_of`` 只能来自 ``PortfolioFactProjection.asof_day``（即
``PortfolioReadContext.asof_day``）—— 本项目**不**接受 ``created_at`` / ``updated_at`` /
``today()`` / latest NAV date 作为 fallback，因为 owner 的 context 已经把业务日显式钉住。

内容指纹覆盖 fact contract 版本 / fact_kind / cycle_id / account_id / asof_day /
**canonical value**，用 ``json.dumps(sort_keys=True, separators=(",", ":"))`` + sha256。
刻意**不**用 ``hash()``（跨进程不稳定）/ ``repr(object)`` / 内存地址 / 当前时间 / 随机数。
verification statement 不重复进指纹：它已经由 ``OwnerVerification.canonical()`` 单独进入
``fact_state``。

``ResearchEvidenceRef.detail`` 只保持最小审计信息（``content_fingerprint`` /
``contract_version`` / ``read_model_version``），**不**复制 owner 的 factual payload
（cash 数字 / realized_pnl 数字 / position_count / cost_value）。正确分层仍然是：

```text
PortfolioFactProjection   owner factual truth
ResearchEvidenceRef       identity + verification + fingerprint
InformationEvent.payload  一次 research observation 投影（B2C-4C 再建立）
```

──────────────── 已知限制：owner-origin provenance = OPEN ────────────────

本层保证的是：

* 调用方不能 raw 构造 ``ResearchEvidenceRef``；
* 不能自述 identity / as_of / status / outcome；
* adapter 只接受**真正的** ``paper_portfolio_read_model.PortfolioFactProjection``
  （``type(...) is``），dict / ``Mapping`` / duck-typed 伪对象 / 子类一律拒绝；
* status → outcome 映射与 owner 的公开闭集双向精确一致，漂移即 fail closed。

它**不**声称关闭 physical database origin：

```text
contract-issued portfolio projection:                 CLOSED
caller self-declared identity / status / as_of:       CLOSED
physical database origin / trusted database provenance: OPEN / REQUIRED
```

调用方仍然可以自造一个 SQLite connection / fixture，调用 owner 的 public read 拿到投影。
那一步属于"可信数据库 provenance"，是 R27 完成的前置条件，本 PR 不把它写成已解决。

──────────────── 本轮没有 production consumer（刻意） ────────────────

B2C-4B 的交付物是**能力 + 契约 + 回归**：owner 能发布 typed 记账事实、research 侧有唯一
adapter 能把它变成 ``ResearchEvidenceRef``。``pnl_attribution`` 的 runtime 迁移是
**B2C-4C**，因此 ``evidence_ref_from_portfolio_projection`` 在 production 里的调用点今天
是 **0** —— 这是预期状态，不是空转。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import ai_research_contract as ARC
import paper_portfolio_read_model as PPRM

__all__ = (
    "evidence_ref_from_portfolio_projection",
)


# ---------------------------------------------------------------------------
# owner 词表 → owner-neutral outcome：显式穷尽表，不是 catch-all
# ---------------------------------------------------------------------------

#: portfolio owner 的合法核验结论 → owner-neutral 三态。
#:
#: **这是一张完整表，不是一个 ``if/else`` 链。** catch-all ``else`` 会让 owner 未来新增
#: 一个状态时被 research 层**静默**归成一态 —— 那正是"research 替 owner 决定它自己的词
#: 是什么意思"，与 R27-B2C 的核心原则直接冲突。显式表把这件事变成
#: :func:`_portfolio_owner_verification` 的 fail-closed 拒绝，从而要求**人工**决定新状态
#: 如何归口。
#:
#: 本 owner 刻意**没有** ``source_unusable``：它没有发布"证据源不可用"这个独立状态，
#: research 侧不得替它猜一个（``unknown`` 与"源的核验过程失败"是不同的事）。这与 market
#: 和 execution 两位 owner 的归口手法完全一致：词表只有一份（在 owner 那里），adapter 的
#: 一致性由 :func:`_portfolio_outcome_mapping_problems` 双向强制。
_PORTFOLIO_OUTCOME_BY_STATUS = {
    # owner 证明了这条记账事实：值是有限数或 typed cost summary。
    PPRM.STATUS_VERIFIED: ARC.OWNER_OUTCOME_VERIFIED,
    # owner 做了核验判定，结论是"证明不了"（归属不可证明 / archived / 数量未证明 /
    # 账本值非有限）。**不是** source_unusable —— 障碍不在核验过程，而在这条事实本身
    # 还不足以被证明。
    PPRM.STATUS_UNKNOWN: ARC.OWNER_OUTCOME_UNVERIFIED,
}


def _portfolio_outcome_mapping_problems(mapping: Mapping, statuses: Any) -> list[str]:
    """显式 outcome 映射与 owner 状态闭集的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个合法状态"与"多一个已不存在的状态"两个方向都能被
    **直接测到**，而不是只能靠改 owner 源码来验 —— 与
    ``test_ai_research_evidence_ownership_guard._registry_problems``、
    ``ai_research_contract._market_outcome_mapping_problems`` 同一手法。

    两个方向都必须报问题：任何一处漂移都意味着"某个 portfolio 状态的含义"已经不再由人工
    决定，而是由 adapter 的默认分支决定。
    """
    problems: list[str] = []
    missing = sorted(set(statuses) - set(mapping))
    if missing:
        problems.append(
            f"portfolio owner 已有但没有登记 owner-neutral outcome 的状态：{missing}"
            "（research 层不得替 portfolio owner 猜一个新状态的含义）"
        )
    unknown = sorted(set(mapping) - set(statuses))
    if unknown:
        problems.append(
            f"outcome 映射里有 portfolio owner 已不认识的状态：{unknown}"
            "（说明这张表与 owner 的状态闭集漂移了）"
        )
    return problems


def _portfolio_owner_verification(projection: Any) -> ARC.OwnerVerification:
    """portfolio owner 的核验声明 → owner-neutral 核验发布结果。**portfolio owner factory。**

    owner authority 完整保留在 ``paper_portfolio_read_model`` 一侧，本函数只做**归口**：

    1. 状态闭集的合法性问 owner（``PPRM.PORTFOLIO_FACT_STATUSES``），本层不重写词表；
    2. 把 owner 的状态词**按显式穷尽表**归到中性三态；
    3. ``status`` 逐字保留 owner 的状态词，``attributes`` 只放本 owner 的核验维度。

    第 2 步是本层唯一的解释行为，方向是"把 owner 的词映射到中性结论"，**不是**"用中性
    结论替代 owner 判定"。research core 反过来看不到这张表。

    **fail closed 是这里的关键性质。** 映射表必须与 owner 的状态闭集**精确相等**：

    * owner 新增一个状态 → 下表查不到 → **拒绝**（而不是静默归成 ``unverified``）；
    * 表里出现 owner 已不认识的状态 → 同样拒绝（说明这张表已经漂移）。

    刻意**不**缓存：owner 词表漂移必须在**下一次调用**就 fail closed，而不是等进程重启。
    """
    problems = _portfolio_outcome_mapping_problems(
        _PORTFOLIO_OUTCOME_BY_STATUS, PPRM.PORTFOLIO_FACT_STATUSES,
    )
    if problems:
        raise ValueError(
            "portfolio fact vocabulary drifted from the explicit outcome mapping: "
            + "; ".join(problems)
        )

    status = projection.status
    if status not in _PORTFOLIO_OUTCOME_BY_STATUS:
        raise PPRM.PortfolioFactContractError(
            "unknown_fact_status",
            f"{status!r} is not a portfolio owner status",
        )
    return ARC.OwnerVerification(
        outcome=_PORTFOLIO_OUTCOME_BY_STATUS[status],
        status=status,
        attributes={
            "verification_scope": PPRM.PORTFOLIO_FACT_VERIFICATION_SCOPE,
            "fact_contract_version": projection.version,
            "read_model_version": PPRM.PORTFOLIO_READ_MODEL_VERSION,
            "fact_kind": projection.fact_kind,
        },
    )


# ---------------------------------------------------------------------------
# 事实内容指纹 —— "同一条事实"与"内容变了"的判据
# ---------------------------------------------------------------------------


def _canonical_value(value: Any) -> Any:
    """把 owner 发布的值归一成确定性、可 JSON 编码的形状。

    ``PositionCostSummary`` 展开成 ``{"position_count", "cost_value"}``；数值事实保持
    浮点数；``unknown`` 的 ``None`` 保持 ``None``。刻意**不**用 ``repr(object)`` /
    内存地址 —— 那会让"内容没变"被报成"内容变了"（或反之），冲突检测随之失去意义。
    """
    if isinstance(value, PPRM.PositionCostSummary):
        return {
            "position_count": value.position_count,
            "cost_value": value.cost_value,
        }
    return value


def _content_fingerprint(projection: Any) -> str:
    """portfolio 事实**内容**的稳定指纹（区分"同一条事实"与"内容变了"）。

    覆盖 fact contract 版本 / ``fact_kind`` / ``cycle_id`` / ``account_id`` /
    ``asof_day`` / **canonical value**。同一 identity 下现金、已实现盈亏或持仓成本被改写
    必须报 ``EvidenceConflict``，而不是被当成"同一条事实"静默去重。

    刻意排除 verification statement：后者已经由 ``OwnerVerification.canonical()`` 单独进入
    ``fact_state``，重复放进指纹会让同一件事有两个 authority。
    """
    payload = {
        "version": projection.version,
        "fact_kind": projection.fact_kind,
        "cycle_id": projection.cycle_id,
        "account_id": projection.account_id,
        "asof_day": projection.asof_day,
        "value": _canonical_value(projection.value),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 唯一公开 factory
# ---------------------------------------------------------------------------


def evidence_ref_from_portfolio_projection(projection: Any) -> ARC.ResearchEvidenceRef:
    """把 portfolio owner 的 typed projection 映射成 :class:`ResearchEvidenceRef`。

    **portfolio owner factory。** 签名里**只有** ``projection`` —— 调用方不能提供
    ``source_id`` / ``as_of`` / ``status`` / ``outcome`` / ``cycle`` / ``account``。
    这些全部从 owner 投影派生：

    * ``source_id`` ← ``<fact_kind>|cycle=<cycle_id>|account=<account_id>``
      （identity authority 是 portfolio owner；本层不重新定义它）
    * ``as_of`` ← ``projection.asof_day``，即 ``PortfolioReadContext.asof_day``；
      **没有** ``created_at`` / ``updated_at`` / ``today()`` / latest NAV date fallback
    * ``owner_verification`` ← :func:`_portfolio_owner_verification` 归口的 owner-neutral 值对象
    * ``detail["content_fingerprint"]`` ← :func:`_content_fingerprint` 的确定性指纹

    入口**先做类型校验**：``projection`` 必须是**真正的**
    ``paper_portfolio_read_model.PortfolioFactProjection``（``type(...) is``，子类也不算）。
    dict / ``Mapping`` / duck-typed 伪对象（哪怕有 ``status`` / ``fact_kind`` 属性）一律拒绝。
    少了这一步，调用方就能自己拼 ``status`` / ``as_of`` / ``cycle_id`` 然后冒充 portfolio
    owner 投影 —— 那会让 owner contract 这个边界形同虚设。

    ``detail`` **不**复制 owner 的 factual payload：detail 的职责是 identity + 核验 +
    内容指纹 + 最小审计元数据，需要记账事实的消费者读 owner 投影本身
    （B2C-4C 构造 typed event 时即如此）。

    **已知限制**：调用方仍可自造 SQLite connection / fixture 并调用 owner public read，因此
    *physical database origin / trusted database provenance* 仍是 **OPEN / REQUIRED**
    （见模块 docstring）。本层不声称已关闭它。

    .. note::
       B2C-4B 结束时 production 里**没有**调用点：本 factory 是 roadmap 要求的能力交付，
       ``pnl_attribution`` 的运行时迁移属于 B2C-4C。
    """
    if type(projection) is not PPRM.PortfolioFactProjection:
        raise TypeError(
            "evidence must be mapped from a typed portfolio projection: expected "
            "paper_portfolio_read_model.PortfolioFactProjection, got "
            f"{type(projection).__name__} — dict / Mapping / duck-typed / 子类对象不得冒充 "
            "owner 投影"
        )

    return ARC._issue_evidence_ref(
        source_type=ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH,
        source_id=(
            f"{projection.fact_kind}|cycle={projection.cycle_id}"
            f"|account={projection.account_id}"
        ),
        as_of=projection.asof_day,
        owner_verification=_portfolio_owner_verification(projection),
        detail={
            "content_fingerprint": _content_fingerprint(projection),
            "contract_version": projection.version,
            "read_model_version": PPRM.PORTFOLIO_READ_MODEL_VERSION,
        },
    )
