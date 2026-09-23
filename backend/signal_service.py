# -*- coding: utf-8 -*-
"""R25 —— Signal Pipeline 的**单一 persistence boundary**。

Candidate → Evidence → Decision → Approval → Frozen Provenance → Signal Persistence

本模块是 ``paper_signals`` 生产写入的**唯一** owner：

* :func:`commit_signal` 独占 ``INSERT INTO paper_signals`` —— ``paper_trading``
  的 normal / bootstrap 两条路径都必须经此落库，不得再内联 SQL；
* provenance（``strategy_id`` / ``strategy_version`` / ``strategy_checksum`` /
  ``cycle_id``）**只**来自调用方传入的 frozen :class:`SignalWriteContext`
  （由 R23 resolver 在 commit phase 解析），行数据无法携带另一套戳；
* conflict 策略显式二选一：

  - ``CONFLICT_IGNORE`` —— close 路径：``INSERT OR IGNORE``，重跑幂等、
    绝不覆盖已存在的 signal；
  - ``CONFLICT_REFRESH`` —— bootstrap 路径：``ON CONFLICT ... DO UPDATE``
    仅刷新业务列，**不可变 provenance 列永不出现在 SET 子句**。

:func:`conflict_statement` 是这两条语句的**唯一构造入口**：``commit_signal``
与回归测试都调用它，因此「测试断言的 SQL」与「生产执行的 SQL」不可能分叉。

依赖方向（单向）::

    paper_trading  →  signal_service  →  strategy_selection_resolver
                                      →  market_data_contract

本模块**不** import ``paper_trading``，**不**解析 cycle / 版本，**不**读时钟，
**不**联网，**不**开启事务 —— 它只负责把调用方已冻结的事实安全地写进账本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

import market_data_contract as MDC
import strategy_selection_resolver as SRES

__all__ = [
    "CONFLICT_IGNORE",
    "CONFLICT_REFRESH",
    "SignalDecision",
    "SignalEvidence",
    "SignalWriteContext",
    "commit_signal",
    "conflict_statement",
    "decide_signal",
    "signal_evidence",
]

#: Re-export the frozen context type so callers need only import this module
#: for the full commit contract (resolution still happens in the resolver).
SignalWriteContext = SRES.SignalWriteContext

#: Close 路径：``INSERT OR IGNORE`` —— 幂等重跑，已存在即跳过。
CONFLICT_IGNORE = "ignore"
#: Bootstrap 路径：upsert 业务列，冻结 provenance 列不参与冲突更新。
CONFLICT_REFRESH = "refresh"

_CONFLICT_POLICIES = frozenset({CONFLICT_IGNORE, CONFLICT_REFRESH})

#: 完整 INSERT 列序（与表 schema 对齐）。
_INSERT_COLUMNS = (
    "account_id",
    "signal_date",
    "intended_date",
    "code",
    "name",
    "industry",
    "close_price",
    "rank_score",
    "t_tier",
    "t_score",
    "payload",
    "status",
    "reason",
    "created_at",
    "strategy_id",
    "strategy_version",
    "strategy_checksum",
    "cycle_id",
)

#: ``ON CONFLICT DO UPDATE`` 允许刷新的业务列。
#: **不可变 provenance 列（strategy_* / cycle_id）故意不在其中** ——
#: 升级前的 NULL cycle 必须保持 NULL，已有 stamp 不得被改写。
_REFRESHABLE_COLUMNS = (
    "intended_date",
    "name",
    "industry",
    "close_price",
    "rank_score",
    "t_tier",
    "t_score",
    "payload",
    "status",
    "reason",
    "created_at",
)

#: 调用方必须提供的**业务**列；provenance 四列由 context 独占供给，
#: ``status`` / ``reason`` 由 decision 独占供给（见 :data:`_DECISION_OWNED_ROW_COLUMNS`）。
#: ``payload`` 由调用方提供业务内容，但其中两个裁决键由 writer 注入/校验。
_ROW_COLUMNS = (
    "signal_date",
    "intended_date",
    "code",
    "name",
    "industry",
    "close_price",
    "rank_score",
    "t_tier",
    "t_score",
    "payload",
    "created_at",
)

#: 由 :class:`SignalDecision` **独占**的列 —— 调用方不得在 ``row`` 里提供。
#:
#: 这是 R25 的 Candidate→Decision→Commit 边界：如果允许 caller 自己写
#: ``status="pending"`` 而 decision 是 blocked，那么"唯一 writer"就只是把
#: 内联 SQL 换了个位置，任何人（含未来 R27 的 AI candidate producer）都能绕过
#: 决策直接伪造一条正式 signal。拒绝而非静默忽略 —— 静默忽略会掩盖 caller 的
#: 误解，让旁路继续以"能跑"的形式存在。
_DECISION_OWNED_ROW_COLUMNS = ("status", "reason")

#: payload 里由 writer 独占注入的键（调用方不得自行写入；写了就必须一致）。
_DECISION_OWNED_PAYLOAD_KEYS = ("signal_decision", "signal_evidence")


def conflict_statement(conflict: str) -> str:
    """构造某个 conflict policy 对应的生产 SQL —— 唯一构造入口。

    刻意做成公开函数而不是内联字符串：``commit_signal`` 与回归测试都调用它，
    于是「测试读到的语句」就是「生产执行的语句」。此前测试按字面量从
    ``paper_trading.py`` 里 grep 这段 SQL，迁移写入点后探针失明 —— 那种
    脆弱性不应再有第二次。
    """
    if conflict not in _CONFLICT_POLICIES:
        raise ValueError(f"unknown signal conflict policy: {conflict!r}")
    columns = ",".join(_INSERT_COLUMNS)
    placeholders = ",".join("?" for _ in _INSERT_COLUMNS)
    if conflict == CONFLICT_IGNORE:
        return f"INSERT OR IGNORE INTO paper_signals({columns}) VALUES({placeholders})"
    set_clause = ",".join(f"{col}=excluded.{col}" for col in _REFRESHABLE_COLUMNS)
    return (
        f"INSERT INTO paper_signals({columns}) VALUES({placeholders}) "
        f"ON CONFLICT(account_id,signal_date,code) DO UPDATE SET {set_clause}"
    )


# ---------------------------------------------------------------------------
# Evidence —— "这条 signal 用了什么事实"，而不是第二份决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalEvidence:
    """一条 signal 产生时所依据的 market-data 证据投影（不可变、紧凑）。

    它**不**复制行情 payload：只保留可复核的引用与判定维度。逐票双源核验
    由 :attr:`cross_source_verified` 显式回答，判据来自 R24 authority 的
    :func:`market_data_contract.is_cross_source_verified` —— 而不是在这里
    比较 ``verification == "verified"``。

    为什么必须区分：R24 把 ``verified`` 定义为"**该 kind 的 policy 已通过**"，
    而"通过的是哪一套"由 ``verification_method`` 表达。``coverage_integrity``
    也是 ``verified``，但它只是"快照完整且覆盖达标"，**不是**逐票第二源核验。
    若 signal 侧只看 ``verified``，一次覆盖完整性通过就会被当成双源可信。
    """

    #: 逐票行情核验的 R24 维度（``verified`` / ``single_source`` / ``disagreement`` …）。
    verification: str = MDC.VERIFICATION_NOT_ATTEMPTED
    #: 该 ``verified`` 是通过哪一套 policy 得到的（``cross_source`` / ``coverage_integrity``）。
    verification_method: str = MDC.VERIFICATION_METHOD_NONE
    #: 决策所用的 as-of 日（不可变事实，不回填 current）。
    asof_day: str = ""
    #: 行情源观测时点（逐票或横截面），用于复核时效。
    observed_at: str | None = None
    #: 引用哪个 Market Data policy（名称，不复制 policy 数值 —— §52）。
    policy: str | None = None
    #: 解释性细节（价格差/涨跌差等），保持紧凑。
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "verification", str(self.verification or ""))
        object.__setattr__(self, "verification_method", str(self.verification_method or ""))
        object.__setattr__(self, "asof_day", str(self.asof_day or ""))
        object.__setattr__(self, "detail", MDC._freeze(self.detail))

    @property
    def cross_source_verified(self) -> bool:
        """是否**真的**通过了逐票多源交叉核验（R24 单一判据）。"""
        return _cross_source_verified(self.verification, self.verification_method)

    def projection(self) -> dict[str, Any]:
        """给 API / 前端的稳定投影：只 render，不重算。

        前端拿到的 ``cross_source_verified`` 是后端算好的业务谓词；
        ``verification`` / ``verification_method`` 同时下发，使"双源"这一
        结论**可追溯**，而不是让前端从 ``verified`` 自己猜。
        """
        return {
            "verification": self.verification,
            "verification_method": self.verification_method,
            "cross_source_verified": self.cross_source_verified,
            "asof_day": self.asof_day or None,
            "observed_at": self.observed_at,
            "policy": self.policy,
        }


def _cross_source_verified(verification: str, method: str) -> bool:
    """把 ``(verification, method)`` 两维喂给 R24 的单一判据。

    直接构造 :class:`market_data_contract.MarketDataSnapshot` 而不是重写
    ``== "verified" and method == "cross_source"``：判据只有一份，将来
    R24 收紧定义时这里自动跟随。

    ``verified`` 配 ``none`` 这类非法组合由 snapshot 构造期拒绝；此处按
    fail closed 处理成"不构成双源保证"，而不是让异常冒到 signal 决策里。
    """
    try:
        snapshot = MDC.MarketDataSnapshot(
            kind="symbol_quote", verification=verification, verification_method=method,
        )
    except ValueError:
        return False
    return MDC.is_cross_source_verified(snapshot)


def signal_evidence(
    quote: Mapping[str, Any] | None,
    *,
    asof_day: str = "",
    policy: str | None = None,
) -> SignalEvidence:
    """把逐票行情映射成 :class:`SignalEvidence`。

    核验语义复用 R24 authority 的 ``verification_from_cross_status``：既有
    ``quote_validation`` 术语（``cross_source_checked`` / ``cross_source_failed``
    / ``cross_source_unavailable`` / ``range_timestamp_checked``）在这里被翻译
    成契约维度，而不是让 signal 侧另立一套字符串判据。

    逐票行情是**双源**性质的事实，所以 ``cross_source_checked`` 映射得到的
    ``verified`` 配 ``cross_source`` method；``range_timestamp_checked`` 只
    通过了区间/时间戳校验，映射成 ``single_source`` —— 它**不是**双源。

    method 只由「该状态是否来自一次逐票核验」决定：``not_attempted``（含所有
    未知状态文本）配 ``none``，其余逐票状态配 ``cross_source``。未知文本因此
    fail closed 成"没核验过"，而不是被乐观地当成双源。
    """
    quote = quote or {}
    status = str(quote.get("quote_validation") or "")
    verification = MDC.verification_from_cross_status(status)
    method = (
        MDC.VERIFICATION_METHOD_NONE
        if verification == MDC.VERIFICATION_NOT_ATTEMPTED
        else MDC.VERIFICATION_METHOD_CROSS_SOURCE
    )
    return SignalEvidence(
        verification=verification,
        verification_method=method,
        asof_day=asof_day,
        observed_at=quote.get("quote_at"),
        policy=policy,
        detail={
            "quote_source": quote.get("quote_source"),
            "quote_validation": status or None,
            "quote_cross_check": quote.get("quote_cross_check"),
        },
    )


# ---------------------------------------------------------------------------
# Decision —— 显式 outcome/reason，而不是裸布尔
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalDecision:
    """一个候选在落库前的显式决策结果。

    ``outcome`` 是业务裁决（是否放行），``status`` 是将写入
    ``paper_signals.status`` 的生命周期状态（可因 waitlist / recovery
    等策略在 approved 基础上细分），``reason`` 是门禁说明，``evidence``
    是这次裁决所依据的 market-data 证据投影。
    """

    outcome: str
    reason: str
    status: str
    evidence: SignalEvidence | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"approved", "blocked"}:
            raise ValueError(f"unknown signal decision outcome: {self.outcome!r}")

    def projection(self) -> dict[str, Any]:
        """给 API / 前端：decision + reason + evidence 状态（§43）。"""
        return {
            "outcome": self.outcome,
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence.projection() if self.evidence is not None else None,
        }

    def business_projection(self) -> dict[str, Any]:
        """落库用的紧凑投影：只留裁决本身。

        刻意**不**重复嵌 evidence —— evidence 已经作为 ``signal_evidence`` 单独
        持久化，再复制一份等于在同一行里放两份事实，将来两者漂移时无法判断
        哪一份是权威。
        """
        return {"outcome": self.outcome, "status": self.status, "reason": self.reason}


def decide_signal(
    *,
    passed: bool,
    reason: str = "",
    status_if_passed: str = "pending",
    status_if_blocked: str = "blocked",
    evidence: SignalEvidence | None = None,
) -> SignalDecision:
    """把 approval 门禁结果映射为 :class:`SignalDecision`。

    bootstrap 的 waitlist / recovery 等细分状态由调用方在拿到
    ``approved`` 决策后自行 ``dataclasses.replace`` 覆盖 ``status``。
    """
    reason = str(reason or "")
    if passed:
        return SignalDecision(
            outcome="approved", reason=reason, status=status_if_passed, evidence=evidence,
        )
    return SignalDecision(
        outcome="blocked", reason=reason, status=status_if_blocked, evidence=evidence,
    )


def commit_signal(
    conn,
    *,
    context: SignalWriteContext,
    decision: SignalDecision,
    row: Mapping[str, Any],
    conflict: str = CONFLICT_IGNORE,
) -> dict[str, Any]:
    """把一行 signal 写入 ``paper_signals`` —— **唯一**生产 INSERT。

    R25 的 Candidate→Decision→Commit 边界就落在这个签名上。

    **``decision`` 是必需参数，且裁决字段由它独占。** writer 从 decision
    派生 ``status`` / ``reason`` / ``payload.signal_decision`` /
    ``payload.signal_evidence``，并且**拒绝**调用方在 ``row`` / ``payload``
    里自己提供这些。理由：

    * 如果 writer 完全信任 caller 传进来的 ``status``，那么"唯一 writer"只是把
      内联 SQL 换了个位置。任何模块（未来包括 R27 的 AI candidate producer）
      只要拿到 ``commit_signal`` 就能写一条 ``status="pending"`` 的正式 signal，
      而完全不经过 Candidate / Evidence / Decision。这条旁路必须不存在，
      而不是"约定不要走"。
    * 静默忽略 caller 提供的裁决字段会让旁路继续以"能跑"的形式存在。因此选择
      **明确拒绝**：caller 的误解会立刻变成可见的失败。

    Returns
    -------
    dict
        实际落库的 canonical payload（已注入裁决与证据）。调用方应使用这个返回
        值去写 risk log / audit，而不是自己再注入一次 —— 那样会出现第二处
        裁决注入点。

    Parameters
    ----------
    conn:
        已处于 write transaction 内的 SQLite 连接（调用方负责 ``BEGIN IMMEDIATE``
        fencing；本函数不开事务、不 commit）。
    context:
        R23 resolver 产出的 frozen :class:`SignalWriteContext`；provenance 四列
        只从这里取。
    decision:
        该候选的 :class:`SignalDecision`（由 ``decide_signal`` 产出）。它是本行
        裁决的唯一来源。
    row:
        **业务**列映射，必须包含 :data:`_ROW_COLUMNS` 全部键。
        ``payload`` 传 mapping（由 writer 序列化），且**不得**预设两个裁决键。
        **不得**包含 ``status`` / ``reason``（decision 独占）。
    conflict:
        ``CONFLICT_IGNORE``（close 幂等）或 ``CONFLICT_REFRESH``
        （bootstrap upsert，仅刷新业务列）。
    """
    if context is None:
        raise ValueError("commit_signal requires a frozen SignalWriteContext")
    if not isinstance(decision, SignalDecision):
        raise ValueError(
            "commit_signal requires a SignalDecision (裁决必须经 Candidate→"
            f"Evidence→Decision 产生，而不是由调用方在 row 里自述)；got {type(decision).__name__}"
        )

    leaked = [col for col in _DECISION_OWNED_ROW_COLUMNS if col in row]
    if leaked:
        raise ValueError(
            "commit_signal row must not carry decision-owned columns "
            f"{leaked}: 这些字段由 SignalDecision 独占，调用方提供会让"
            "Candidate→Decision→Commit 边界失效"
        )

    missing = [col for col in _ROW_COLUMNS if col not in row]
    if missing:
        raise ValueError(f"commit_signal row missing columns: {missing}")

    payload = row["payload"]
    if isinstance(payload, (str, bytes, bytearray)):
        raise ValueError(
            "commit_signal row['payload'] must be a mapping, not a serialized "
            "string: writer 需要在写入前注入裁决与证据，不能信任调用方预序列化的 payload"
        )
    try:
        payload = dict(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"commit_signal row['payload'] must be a mapping: {exc}") from exc

    forged = [key for key in _DECISION_OWNED_PAYLOAD_KEYS if key in payload]
    if forged:
        raise ValueError(
            f"commit_signal payload must not pre-set {forged}: "
            "裁决与证据由 writer 从 SignalDecision 注入"
        )

    values: dict[str, Any] = {
        "account_id": context.account_id,
        "strategy_id": context.strategy_id,
        "strategy_version": context.strategy_version,
        "strategy_checksum": context.strategy_checksum,
        "cycle_id": context.cycle_id,
        # 裁决字段由 decision 独占供给。
        "status": decision.status,
        "reason": decision.reason,
    }
    for col in _ROW_COLUMNS:
        values[col] = row[col]

    payload["signal_decision"] = decision.business_projection()
    if decision.evidence is not None:
        payload["signal_evidence"] = decision.evidence.projection()
    values["payload"] = _serialize_payload(payload)

    params = tuple(values[col] for col in _INSERT_COLUMNS)
    conn.execute(conflict_statement(conflict), params)
    return payload


def _serialize_payload(payload: Mapping[str, Any]) -> str:
    """序列化 payload —— writer 独占这一步。

    与 ``paper_trading._json`` 同口径（``ensure_ascii=False``、``default=str``），
    以保证落库字节与 R25 之前一致；差别只在于两个裁决键由 writer 注入。
    """
    return json.dumps(payload, ensure_ascii=False, default=str)
