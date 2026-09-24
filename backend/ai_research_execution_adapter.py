# -*- coding: utf-8 -*-
"""R27-B2C-3 —— execution owner fact → typed research evidence 的 **adapter**。

依赖方向（单向，不可反转）：

    execution_verification                （owner：事实、身份、业务日、核验结论的 authority）
              ↓
    ai_research_execution_adapter         （本模块：唯一把 owner 投影翻译成 typed evidence 的地方）
              ↓
    ai_research_contract                  （research core：不 import 任何 execution 模块）

**为什么这是一个真实的 adapter boundary，而不是 wrapper 债。**

``execution_verification`` 同时承担两件事：owner fact contract（纯 value type）与 SQLite
读路径 / 回填 / 闸门谓词。让 research domain contract 直接 ``import
execution_verification`` 会把"研究契约"绑到 execution 的 DB 读实现上；反过来让
``execution_verification`` import research 又会反转依赖方向（AIG-02 / RG-04 已禁止）。
本模块是本轮 roadmap **本身要求**的那一个接缝：它消费 owner 已发布的 typed 投影，
产出一个 research 侧的 typed 引用，并且是**唯一**一处同时认识这两套词表的地方。

它刻意**不**新增 registry framework / ``BaseAdapter`` / service / manager / repository /
facade。公开面只有一个函数（:func:`evidence_ref_from_execution_projection`）。

──────────────────────── 两个不同的问题（本模块的核心） ────────────────────────

execution owner 的 ``verification["is_verified"]`` 回答的是：

    "**整张订单**是否被证明完整成交？"   （只有 ``execution_status == verified`` 时为真）

research 的 ``OwnerVerification.is_verified`` 回答的是**另一个**问题：

    "这个 execution owner-native 结论是否有足够的 owner evidence，
     可以作为 research fact 被依赖？"

**两者不是同一个问题，把前者当成后者会制造两类系统性错误**：owner 明确区分四态

```text
verified       账本证明完整成交
partial        账本证明**发生过真实的部分成交**，只是整单没有完全成交
not_executed   owner 有肯定性证据确认"没有执行"
unknown        证据不足或自相矛盾 —— fail closed
```

其中 ``partial`` 与 ``not_executed`` 都是**可以依赖的事实结论**：前者说"部分成交是真的"，
后者说"确认没有执行"。用整单布尔位去回答"这个事实可不可信"，会把这两种结论一律降级成
"不可信事实"，于是研究层再也无法引用一条真实发生的部分成交。因此本站**禁止**：

```python
outcome = (OWNER_OUTCOME_VERIFIED
           if projection.verification["is_verified"]
           else OWNER_OUTCOME_UNVERIFIED)          # ← 错误：把 partial / not_executed 降级
```

于是：

```text
status = partial        execution statement is_verified = False   research outcome = verified
                        ⇔ 完整成交？NO；"部分成交"这个事实可信？YES
status = not_executed   execution statement is_verified = False   research outcome = verified
                        ⇔ 完整成交？NO；确认没有执行？YES
```

owner 原来的整单布尔位**不丢**，只是换了一个准确的 key 进入 research attributes：
``execution_fully_verified``（见 :data:`ATTRIBUTE_EXECUTION_FULLY_VERIFIED`）。刻意不沿用
``is_verified`` 这个名字：那样同一份对象上会同时出现 ``ref.is_verified = True`` 与
``verification_attributes["is_verified"] = False``，而两者对 ``partial`` 的值本来就不同，
极易被误读。

──────────────────────── 翻译没有发生 ────────────────────────

本模块**不**把 execution 状态读成 market 词（``partial`` 不会变成 ``single_source``，
``unknown`` 不会变成 ``unavailable``，``not_executed`` 不会变成 ``not_attempted``）。
``OwnerVerification.status`` 逐字保留 owner 的状态词，research core 不解释它。
research core 唯一消费的是三态 ``outcome``。

──────────────────────── PIT：只有 owner 记录的业务日能进 ────────────────────────

``ResearchEvidenceRef.as_of`` 取自 ``projection.business_day``，且必须是
``is_known == True``。业务日 ``unknown`` 时**拒绝签发**，绝不 fallback 到 ``created_at`` /
``observed_at`` 的日期 / ``order_time`` / 墙钟。

已知且**故意保留**的缺口：被拒 / 被撤等部分 execution facts 今天在 ``paper_orders`` 上
**没有** owner 记录的业务日列，因此它们的投影 ``business_day`` 是 ``unknown``。

```text
execution fact without owner business_day
    → NOT ADAPTABLE YET（owner 数据前置条件未满足）
    → owner data gap = OPEN
```

这不是"删掉原 roadmap 能力"，而是一个明确记录的 owner data prerequisite：本层不伪造日期，
也不把缺失的业务日伪造成"今天"。它由 B2C-1 的 ``EXFACT-04`` 与 ``docs/
R27_B2C_EVIDENCE_OWNER_MATRIX.md`` 一并记录。

──────────────────────── identity / 冲突指纹 ────────────────────────

``source_id`` 完全由 ``projection.identity_kind`` + ``projection.identity`` 派生
（``<identity_kind>|<identity>``）。adapter **不重新计算** event key、不接受调用方传
``order_id``：identity authority 是 execution owner，本层只把它编码成一个 research 引用，
不重新定义 execution identity。fact contract 版本刻意**不**进入 identity —— 版本升级
不应该把同一条事实变成另一条事实。

内容指纹覆盖 factual projection（version / identity_kind / order_id / lifecycle_state /
fill_verdict / business_day / observed_at / inconsistencies），使用
``json.dumps(sort_keys=True)`` + sha256 的确定性编码。刻意**不**用 ``hash()``（Python
进程间不稳定）/ ``repr(object)`` / 内存地址 / 当前时间 / 随机数。verification statement
不重复进指纹：它已经由 ``OwnerVerification.canonical()`` 单独进入 ``fact_state``。

──────────────────────── 已知限制：owner-origin provenance = OPEN ────────────────────────

``ExecutionEvidence`` 仍然是**公开可构造**的（B2C-1 已记录），因此

```text
手工造 ExecutionEvidence → fact_projection(...) → evidence_ref_from_execution_projection(...)
```

仍是一条**两步伪造**路径，与 ``MarketDataReading`` 在 R27-A 的情况相同。本层保证的是：
adapter 不接受 duck-typed / dict / 子类输入，身份、业务日、核验结论**全部**从 owner 投影
派生，调用方无法自述。它**不**声称已关闭 owner-origin provenance —— 那是 R27 完成的前置
条件，继续 OPEN / REQUIRED。B2C-3 不宣称关闭 two-step forgery。

──────────────────────── 本轮没有 production consumer ────────────────────────

B2C-3 的交付物是**能力 + 契约 + 回归**：factory 存在、research contract 支持 execution、
测试覆盖它。production runtime 仍然**没有**调用本 factory —— ``pnl_attribution`` 的迁移是
B2C-4。因此"本模块今天零调用点"是预期的，不是空转。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import ai_research_contract as ARC
import execution_verification as EV

__all__ = (
    "evidence_ref_from_execution_projection",
)

#: execution owner 的「整张订单是否被证明完整成交」在 research attributes 里的名字。
#:
#: 刻意**不**沿用 owner 的 ``is_verified``：那会与 ``ResearchEvidenceRef.is_verified``
#: 同名而含义不同（对 ``partial`` 两者取值相反），下游读到时无法分辨。
ATTRIBUTE_EXECUTION_FULLY_VERIFIED = "execution_fully_verified"


# ---------------------------------------------------------------------------
# owner 词表 → owner-neutral outcome：显式穷尽表，不是 catch-all
# ---------------------------------------------------------------------------

#: execution 的合法 ``(verification_status, verification_source)`` 组合 → owner-neutral 三态。
#:
#: **这是一张完整表，不是一个 ``if/else`` 链。** catch-all ``else`` 会让 execution owner
#: 未来新增一个合法组合时被 research 层**静默**归成一态 —— 那正是"research 替 owner 决定
#: 它自己的词是什么意思"，与 R27-B2C 的核心原则直接冲突。显式表把这件事变成
#: :func:`_execution_owner_verification` 的 fail-closed 拒绝，从而要求**人工**决定新组合
#: 如何归口。
#:
#: 三个来源级别的判定（与状态无关，因为障碍出在**证据来源**而不在结论内容）：
#:
#: * ``evidence_inconsistent`` —— 证据自相矛盾（例如写着 ``filled`` 却没有流水）。
#:   owner 仍然可以给出一个状态，但作为 research fact 的依据本身不可用。
#: * ``legacy_row_without_fill_evidence`` —— 升级前的旧行，本来就没有流水可核。
#:   注意：即使状态是 ``not_executed``（结论看起来可信），**证据基础**仍是旧行，
#:   因此归 ``source_unusable``。这是**来源**层面的判定，**不是**把
#:   "确认未执行"这个结论降级 —— owner 从来没有为这条旧行发布过可依赖的证据。
#: * ``no_evidence_available`` —— 连证据对象都没有。
#:
#: 与 B2C-2 的 :data:`ai_research_contract._MARKET_OUTCOME_BY_VERIFICATION` 同一手法：
#: 词表只有一份（在 owner 那里），adapter 的一致性由 :func:`_owner_legal_pairs` 强制。
_EXECUTION_OUTCOME_BY_VERIFICATION = {
    # 账本证据给出了明确的 execution fact：完整成交 / 部分成交 / 确认未执行，
    # 三者都是**可以依赖的事实结论**，因此 outcome 都是 verified。
    (
        EV.EXECUTION_STATUS_VERIFIED,
        EV.EVIDENCE_SOURCE_LEDGER,
    ): ARC.OWNER_OUTCOME_VERIFIED,
    (
        EV.EXECUTION_STATUS_PARTIAL,
        EV.EVIDENCE_SOURCE_LEDGER,
    ): ARC.OWNER_OUTCOME_VERIFIED,
    (
        EV.EXECUTION_STATUS_NOT_EXECUTED,
        EV.EVIDENCE_SOURCE_LEDGER,
    ): ARC.OWNER_OUTCOME_VERIFIED,
    # owner 有记录，但仍无法得出明确 execution fact（在途 / 状态不可识别）：
    # 核验判定做了，结论是"不足以判断"。**不是** source_unusable。
    (
        EV.EXECUTION_STATUS_UNKNOWN,
        EV.EVIDENCE_SOURCE_LEDGER,
    ): ARC.OWNER_OUTCOME_UNVERIFIED,
    # 证据自相矛盾：四种状态都可能配这个来源，障碍全部出在证据本身。
    (
        EV.EXECUTION_STATUS_VERIFIED,
        EV.EVIDENCE_SOURCE_INCONSISTENT,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
    (
        EV.EXECUTION_STATUS_PARTIAL,
        EV.EVIDENCE_SOURCE_INCONSISTENT,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
    (
        EV.EXECUTION_STATUS_NOT_EXECUTED,
        EV.EVIDENCE_SOURCE_INCONSISTENT,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
    (
        EV.EXECUTION_STATUS_UNKNOWN,
        EV.EVIDENCE_SOURCE_INCONSISTENT,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
    # 升级前的旧行：没有可核的流水。
    (
        EV.EXECUTION_STATUS_NOT_EXECUTED,
        EV.EVIDENCE_SOURCE_LEGACY,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
    (
        EV.EXECUTION_STATUS_UNKNOWN,
        EV.EVIDENCE_SOURCE_LEGACY,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
    # 连证据对象都没有。
    (
        EV.EXECUTION_STATUS_UNKNOWN,
        EV.EVIDENCE_SOURCE_ABSENT,
    ): ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
}


def _owner_legal_pairs() -> frozenset:
    """owner 自己认为合法的 ``(状态, 来源)`` 集合 —— 由 owner 的**公开**契约推导。

    刻意**不** import owner 的私有 ``_LEGAL_SOURCES_BY_STATUS``：读私有表等于把本层的
    合法性判据绑死在 owner 的内部结构上，owner 重构一次就会静默失效。这里遍历公开的
    ``EXECUTION_STATUSES`` × ``EVIDENCE_SOURCES`` 并逐个调用公开的
    :func:`execution_verification.verification_contract`，让它自己回答哪些组合成立。

    刻意**不**缓存：owner 增删状态 / 来源 / 组合时，本层必须在**下一次调用**就 fail closed，
    而不是等进程重启。缓存会让"owner 词表漂移"变成一次静默通过。
    """
    pairs = set()
    for status in EV.EXECUTION_STATUSES:
        for source in EV.EVIDENCE_SOURCES:
            try:
                EV.verification_contract(status, source)
            except EV.ExecutionFactContractError:
                continue
            pairs.add((status, source))
    return frozenset(pairs)


def _execution_outcome_mapping_problems(mapping: dict, legal_pairs: Any) -> list[str]:
    """显式 outcome 映射与 owner 合法组合表的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个合法组合"与"多一个已不接受的组合"两个方向都能被
    **直接测到**，而不是只能靠改 owner 源码来验 —— 与
    ``test_ai_research_evidence_ownership_guard._registry_problems``、
    ``ai_research_contract._market_outcome_mapping_problems`` 同一手法。

    两个方向都必须报问题：任何一处漂移都意味着"某个 execution 组合的含义"已经不再由人工
    决定，而是由 adapter 的默认分支决定。
    """
    problems: list[str] = []
    missing = sorted(set(legal_pairs) - set(mapping))
    if missing:
        problems.append(
            f"execution owner 已有但没有登记 owner-neutral outcome 的 (状态, 来源) 组合：{missing}"
            "（research 层不得替 execution owner 猜一个新组合的含义）"
        )
    unknown = sorted(set(mapping) - set(legal_pairs))
    if unknown:
        problems.append(
            f"outcome 映射里有 execution owner 已不接受的组合：{unknown}"
            "（说明这张表与 owner 的合法组合表漂移了）"
        )
    return problems


def _execution_owner_verification(declared: Any) -> ARC.OwnerVerification:
    """execution owner 的核验声明 → owner-neutral 核验发布结果。**execution owner factory。**

    owner authority 完整保留在 ``execution_verification`` 一侧，本函数只做**归口**：

    1. ``(状态, 来源)`` 的合法性问 owner（:func:`_owner_legal_pairs`，即公开的
       ``verification_contract``），本层不重写组合表；
    2. 把 owner 的状态词**按显式穷尽表**归到中性三态；
    3. ``status`` 逐字保留 owner 的状态词，``attributes`` 保留 owner 的核验维度
       （scope / version / source / ``execution_fully_verified``）。

    第 2 步是本层唯一的解释行为，方向是"把 owner 的词映射到中性结论"，**不是**"用中性
    结论替代 owner 判定"，也**不是**"把 owner 的词翻译成另一个 owner 的词"。research core
    反过来看不到这张表。

    **fail closed 是这里的关键性质。** 映射表必须与 owner 的合法组合集合**精确相等**：

    * owner 新增一个合法组合 → 下表查不到 → **拒绝**（而不是静默归成 ``unverified``）；
    * 表里出现 owner 已不接受的组合 → 同样拒绝（说明这张表已经漂移）。

    少了这条，"owner 加一个状态"会变成一次静默的语义发明；有了它，那是一次必须人工处理的
    契约变更。
    """
    declared = dict(declared or {})
    status = str(declared.get("verification_status") or "")
    source = str(declared.get("verification_source") or "")

    # 词表漂移在**每次**调用时就 fail closed，而不是等到某个新组合恰好出现在数据里。
    legal_pairs = _owner_legal_pairs()
    problems = _execution_outcome_mapping_problems(
        _EXECUTION_OUTCOME_BY_VERIFICATION, legal_pairs,
    )
    if problems:
        raise ValueError(
            "execution verification vocabulary drifted from the explicit outcome mapping: "
            + "; ".join(problems)
        )

    key = (status, source)
    if key not in legal_pairs:
        raise EV.ExecutionFactContractError(
            "illegal_verification_pair",
            f"{status!r} cannot carry source {source!r}",
        )

    return ARC.OwnerVerification(
        outcome=_EXECUTION_OUTCOME_BY_VERIFICATION[key],
        status=status,
        attributes={
            "verification_scope": declared.get("verification_scope"),
            "verification_version": declared.get("verification_version"),
            "verification_source": source,
            ATTRIBUTE_EXECUTION_FULLY_VERIFIED: bool(declared.get("is_verified")),
        },
    )


# ---------------------------------------------------------------------------
# 事实内容指纹 —— "同一条事实"与"内容变了"的判据
# ---------------------------------------------------------------------------


def _content_fingerprint(projection: Any) -> str:
    """execution 事实**内容**的稳定指纹（用于区分"同一条事实"与"内容变了"）。

    只覆盖 factual projection，并刻意排除 verification statement：后者已经由
    ``OwnerVerification.canonical()`` 单独进入 ``fact_state``，重复放进指纹会让同一件事有
    两个 authority。

    编码必须是**确定性**的：``json.dumps(sort_keys=True, separators=(",", ":"))`` + sha256。
    刻意**不**用 ``hash()``（跨进程不稳定）/ ``repr(object)`` / 内存地址 / 当前时间 /
    随机数 —— 任何一个都会让"内容没变"被报成"内容变了"（或反之），从而让冲突检测失去意义。
    """
    payload = {
        "version": projection.version,
        "identity_kind": projection.identity_kind,
        "order_id": projection.order_id,
        "lifecycle_state": projection.lifecycle_state,
        "fill_verdict": projection.fill_verdict,
        "business_day": projection.business_day.as_dict(),
        "observed_at": projection.observed_at.as_dict(),
        "inconsistencies": list(projection.inconsistencies),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 唯一公开 factory
# ---------------------------------------------------------------------------


def evidence_ref_from_execution_projection(projection: Any) -> ARC.ResearchEvidenceRef:
    """把 execution owner 的 typed projection 映射成 :class:`ResearchEvidenceRef`。

    **execution 的 owner factory。** 签名里**只有** ``projection`` —— 调用方不能提供
    ``source_id`` / ``as_of`` / ``verification`` / ``outcome`` / ``business_day`` /
    ``verification_source``。这些全部从 owner 投影派生：

    * ``source_id`` ← ``projection.identity_kind | projection.identity``
      （identity authority 是 execution owner；本层不重算 event key，也不接受 ``order_id``）
    * ``as_of`` ← ``projection.business_day``，且必须 ``is_known``；否则**拒绝签发**
    * ``owner_verification`` ← :func:`_execution_owner_verification` 归口的 owner-neutral 值对象
    * ``detail["content_fingerprint"]`` ← :func:`_content_fingerprint` 的确定性指纹

    入口**先做类型校验**：``projection`` 必须是**真正的**
    ``execution_verification.ExecutionFactProjection``（``type(...) is``，子类也不算）。
    dict / ``Mapping`` / duck-typed 伪对象 / 自带 ``projection()`` 方法的对象一律拒绝。
    少了这一步，调用方就能自己拼 ``identity`` / ``business_day`` / ``verification`` 然后
    冒充 execution owner 投影 —— 那会让 owner contract 这个边界形同虚设。

    **已知限制**：``ExecutionEvidence`` 仍然公开可构造，因此"手工造 evidence →
    ``fact_projection`` → 本函数"仍是一条两步伪造路径。owner-origin provenance 仍是
    **OPEN / REQUIRED**，本层不声称已关闭它（见模块 docstring）。

    .. note::
       B2C-3 结束时 production 里**没有**调用点：本 factory 是 roadmap 要求的能力交付，
       ``pnl_attribution`` 的运行时迁移属于 B2C-4。
    """
    if type(projection) is not EV.ExecutionFactProjection:
        raise TypeError(
            "evidence must be mapped from a typed execution projection: expected "
            "execution_verification.ExecutionFactProjection, got "
            f"{type(projection).__name__} — dict / duck-typed / 子类对象不得冒充 owner 投影"
        )

    business_day = projection.business_day
    if not business_day.is_known:
        # 无 fallback：不用 created_at / observed_at 的日期 / order_time / 墙钟。
        raise ValueError(
            "execution evidence carries no owner-recorded business_day "
            f"({business_day.state}: {business_day.detail or 'no reason recorded'}) — "
            "PIT 不可证明的 execution 事实不得进入研究链路；"
            "owner 必须先记录业务日（owner data gap = OPEN）"
        )

    return ARC._issue_evidence_ref(
        source_type=ARC.EVIDENCE_SOURCE_EXECUTION,
        source_id=f"{projection.identity_kind}|{projection.identity}",
        as_of=business_day.require(),
        owner_verification=_execution_owner_verification(projection.verification),
        detail={
            "identity_kind": projection.identity_kind,
            "order_id": projection.order_id,
            "lifecycle_state": projection.lifecycle_state,
            "fill_verdict": projection.fill_verdict,
            "observed_at": projection.observed_at.maybe(),
            "content_fingerprint": _content_fingerprint(projection),
            "fact_contract_version": projection.version,
        },
    )
