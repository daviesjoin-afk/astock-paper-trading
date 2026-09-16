# -*- coding: utf-8 -*-
"""委托成交状态机（order fill lifecycle state machine）。

本模块回答"这笔委托走到了哪一步"，并且**拒绝**任何没有证据支撑的跳转。它是
:mod:`execution_evidence` 的状态层，本身不读数据库、不碰资金、不做 sizing。

──────────────────────── 九个状态与合法边 ────────────────────────

::

    CREATED ──┬─> SUBMITTED ──┬─> ACCEPTED ──┬─> PARTIAL_FILLED ──┐
              │               │              │        ↑           │
              │               │              └───────>┴──────────>├─> FILLED
              │               │                        (自我循环) │
              ├─> REJECTED <──┤              全部状态 ──> CANCELLED
              ├─> CANCELLED   └─> EXPIRED                  EXPIRED
              └─> EXPIRED                    UNKNOWN ──> 任意状态（仅用于重新对齐）

三条硬约束（本模块存在的全部理由）::

    CREATED   -> FILLED          非法：没有 submit / accept 证据
    SUBMITTED -> FILLED          非法：没有受理（ACCEPTED）证据
    REJECTED  -> FILLED          非法：终态不可逆；重试必须是**新的**委托生命周期

最后一条对应仓库真实语义：``paper_orders`` 的重试走 ``retry_of_order_id``
指向**新的一行**，绝不把已终态的 order row 原地改写成成交。

──────────────────────── 与仓库真实状态的映射 ────────────────────────

``paper_orders.status`` 是自由 TEXT（仓库没有 DB CHECK 约束，也没有状态机），
:func:`canonical_state` 是把它翻译成权威状态的**唯一**入口。

两个必须诚实承认的事实，直接写进映射表而不是靠注释:

1. ``ACCEPTED_STORED_STATUSES`` 是**空集**。仓库没有券商/交易所客户端，也就
   没有独立的"受理回报"证据；任何 stored status 都不允许自称 ``ACCEPTED``。
2. ``shadow_q3`` 这类影子记录映射到 ``CREATED``：它只是一条"本来会买"的记录，
   **没有提交过**，因此永远不能合法地走到 ``FILLED``。

映射表里没有的字符串一律 ``UNKNOWN``，**不猜测**。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional, Sequence

__all__ = [
    "EXECUTION_LIFECYCLE_VERSION",
    "ORDER_STATES",
    "TERMINAL_STATES",
    "FILLED_STATES",
    "NO_FILL_STATES",
    "ALLOWED_TRANSITIONS",
    "ACCEPTED_STORED_STATUSES",
    "ExecutionLifecycleError",
    "IllegalLifecycleTransition",
    "LifecycleEvent",
    "OrderLifecycle",
    "allowed_targets",
    "can_transition",
    "canonical_state",
    "observed_fill_supported",
]

EXECUTION_LIFECYCLE_VERSION = "execution-lifecycle-v1"

STATE_CREATED = "CREATED"
STATE_SUBMITTED = "SUBMITTED"
STATE_ACCEPTED = "ACCEPTED"
STATE_PARTIAL_FILLED = "PARTIAL_FILLED"
STATE_FILLED = "FILLED"
STATE_REJECTED = "REJECTED"
STATE_CANCELLED = "CANCELLED"
STATE_EXPIRED = "EXPIRED"
STATE_UNKNOWN = "UNKNOWN"

ORDER_STATES = (
    STATE_CREATED,
    STATE_SUBMITTED,
    STATE_ACCEPTED,
    STATE_PARTIAL_FILLED,
    STATE_FILLED,
    STATE_REJECTED,
    STATE_CANCELLED,
    STATE_EXPIRED,
    STATE_UNKNOWN,
)

#: 终态：不可逆。任何离开终态的跳转都必须失败（重试 = 新委托）。
TERMINAL_STATES = frozenset({STATE_FILLED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED})
#: 已经持有成交的状态（可能是部分成交）。
FILLED_STATES = frozenset({STATE_PARTIAL_FILLED, STATE_FILLED})
#: 终态中"确认没有成交"的状态。
NO_FILL_STATES = frozenset({STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED})

#: 合法边。终态显式给空集，读代码时不需要去别处确认"能不能离开 REJECTED"。
ALLOWED_TRANSITIONS: Mapping[str, frozenset] = {
    STATE_CREATED: frozenset({
        STATE_SUBMITTED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED, STATE_UNKNOWN,
    }),
    STATE_SUBMITTED: frozenset({
        STATE_ACCEPTED, STATE_REJECTED, STATE_CANCELLED, STATE_EXPIRED, STATE_UNKNOWN,
    }),
    # 受理之后只能成交 / 撤销 / 过期：受理本身就是场所侧的拒绝检查点，
    # 因此 ACCEPTED -> REJECTED 不存在。
    STATE_ACCEPTED: frozenset({
        STATE_PARTIAL_FILLED, STATE_FILLED, STATE_CANCELLED, STATE_EXPIRED, STATE_UNKNOWN,
    }),
    STATE_PARTIAL_FILLED: frozenset({
        STATE_PARTIAL_FILLED, STATE_FILLED, STATE_CANCELLED, STATE_EXPIRED, STATE_UNKNOWN,
    }),
    STATE_FILLED: frozenset(),
    STATE_REJECTED: frozenset(),
    STATE_CANCELLED: frozenset(),
    STATE_EXPIRED: frozenset(),
    # UNKNOWN 是"对账失败"，只允许用它重新对齐（含对齐到成交）。
    STATE_UNKNOWN: frozenset(set(ORDER_STATES) - {STATE_UNKNOWN}),
}

# ─────────────────── 仓库 stored status → 权威状态 ───────────────────

FILLED_STORED_STATUSES = frozenset({"filled"})
REJECTED_STORED_STATUSES = frozenset({
    "risk_rejected", "rejected", "manual_rejected", "order_intent_rejected",
    "unfilled_limit_down", "unfilled_limit_down_wait",
})
CANCELLED_STORED_STATUSES = frozenset({"cancelled", "superseded"})
EXPIRED_STORED_STATUSES = frozenset({"expired", "signal_expired"})
#: 影子记录：产生了"本来会买"的证据，但**从未提交**，所以只能停在 CREATED。
SHADOW_STORED_STATUSES = frozenset({"shadow_q3"})
SUBMITTED_STORED_STATUSES = frozenset({
    "pending_execution", "pending_limit", "deferred_capacity", "entry_frozen_waitlist",
    "execution_retry", "manual_execution_retry", "awaiting_batch", "pending_verification",
    "pending_execution_guard", "pending_execution_retry",
})
#: **故意为空**：仓库没有场所受理证据，不允许任何 stored status 自称 ACCEPTED。
ACCEPTED_STORED_STATUSES: frozenset = frozenset()


class ExecutionLifecycleError(ValueError):
    """状态机契约被违反。"""


class IllegalLifecycleTransition(ExecutionLifecycleError):
    """非法状态跳转（例如 CREATED -> FILLED）。"""


def allowed_targets(from_status: Any) -> frozenset:
    """从 ``from_status`` 出发的合法目标集合（未知起点按 UNKNOWN 处理）。"""
    state = str(from_status or "")
    if state not in ORDER_STATES:
        state = STATE_UNKNOWN
    return ALLOWED_TRANSITIONS[state]


def can_transition(from_status: Any, to_status: Any) -> bool:
    """``from_status -> to_status`` 是否合法。"""
    target = str(to_status or "")
    if target not in ORDER_STATES:
        return False
    return target in allowed_targets(from_status)


def canonical_state(stored_status: Any, *, has_fill: bool = False) -> str:
    """把 ``paper_orders.status`` 翻译成权威状态。**唯一**映射实现。

    ``has_fill=True`` 只做一件事：把"在途"细化成 ``PARTIAL_FILLED``——因为
    "还在途却已经有成交流水"按定义就是部分成交。它**不会**把任何状态升级成
    ``FILLED``：全部成交必须由数量和价格证据另行证明。
    """
    status = str(stored_status or "").strip().lower()
    if status in FILLED_STORED_STATUSES:
        return STATE_FILLED
    if status in REJECTED_STORED_STATUSES:
        return STATE_REJECTED
    if status in CANCELLED_STORED_STATUSES:
        return STATE_CANCELLED
    if status in EXPIRED_STORED_STATUSES:
        return STATE_EXPIRED
    if status in SHADOW_STORED_STATUSES:
        return STATE_CREATED
    if status in SUBMITTED_STORED_STATUSES:
        return STATE_PARTIAL_FILLED if has_fill else STATE_SUBMITTED
    if status in ACCEPTED_STORED_STATUSES:  # pragma: no cover - 当前为空集，保留映射位
        return STATE_ACCEPTED
    return STATE_UNKNOWN


# ─────────────────── 证据 ↔ 状态一致性（"能不能自证"） ───────────────────


def _known_pair(evidence: Any, name: str) -> tuple:
    """从证据对象取出 ``(state, value)``；支持三态字段、映射与裸值。"""
    holder = getattr(evidence, name, None)
    if holder is None and isinstance(evidence, Mapping):
        holder = evidence.get(name)
    if holder is None:
        return ("unknown", None)
    state = getattr(holder, "state", None)
    if state is None:
        return ("known", holder)
    return (str(state), getattr(holder, "value", None))


def _known_value(evidence: Any, name: str) -> Optional[Any]:
    state, value = _known_pair(evidence, name)
    return value if state == "known" else None


def observed_fill_supported(
    *,
    lifecycle_state: Any,
    requested_qty: Any = None,
    filled_qty: Any = None,
    fill_price: Any = None,
    fill_session: Any = None,
) -> dict:
    """stored 状态声称的成交，是否被数量与价格证据支持？

    ``supported=False`` 表示"订单写着成交了，但拿不出成交证据"——这是本层要
    暴露的核心缺陷，**不允许**被当成"那就按成交算"。
    """
    state = str(lifecycle_state or "")
    detail = {
        "state": state,
        "requested_qty": requested_qty,
        "filled_qty": filled_qty,
        "fill_price": fill_price,
        "fill_session": fill_session,
    }
    if state not in FILLED_STATES:
        return {"supported": True, "reason": None, **detail}
    if filled_qty is None:
        return {
            "supported": False,
            "reason": "stored status claims a fill but no fill quantity evidence exists",
            **detail,
        }
    if requested_qty is None or requested_qty <= 0:
        return {
            "supported": False,
            "reason": "stored status claims a fill but the requested quantity is unproven",
            **detail,
        }
    if state == STATE_PARTIAL_FILLED:
        if 0 < filled_qty < requested_qty:
            return {"supported": True, "reason": None, **detail}
        return {
            "supported": False,
            "reason": "partial fill evidence carries an inconsistent filled quantity",
            **detail,
        }
    if filled_qty != requested_qty:
        return {
            "supported": False,
            "reason": "stored status claims a full fill but the filled quantity differs",
            **detail,
        }
    if fill_price is None or fill_price <= 0:
        return {
            "supported": False,
            "reason": "stored status claims a fill but the fill price is not trustworthy",
            **detail,
        }
    if not str(fill_session or "").strip():
        return {
            "supported": False,
            "reason": "stored status claims a fill but the fill session is missing",
            **detail,
        }
    return {"supported": True, "reason": None, **detail}


# ───────────────────────── 生命周期对象 ─────────────────────────


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """一次状态落点。``source`` 说明这条状态由谁写下的（审计必需）。"""

    status: str
    at: Optional[str] = None
    source: str = "unspecified"
    detail: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "at": self.at,
            "source": self.source,
            "detail": self.detail,
        }


class OrderLifecycle:
    """一笔委托的状态机实例。历史是**追加**的，不能回写。

    实例本身只持有状态与事件历史；数量/价格证据由调用方在 :meth:`advance`
    时传入，违反不变式即抛 :class:`IllegalLifecycleTransition`。
    """

    def __init__(
        self,
        order_id: Any,
        *,
        created_at: Optional[str] = None,
        source: str = "order_intent",
        retry_of: Any = None,
        state: str = STATE_CREATED,
    ) -> None:
        if state not in ORDER_STATES:
            raise ExecutionLifecycleError(f"unknown lifecycle state: {state!r}")
        self.order_id = order_id
        self.retry_of = retry_of
        self._events: list = [
            LifecycleEvent(status=state, at=_text_or_none(created_at), source=str(source))
        ]

    # ── 读取 ──
    @property
    def status(self) -> str:
        return self._events[-1].status

    @property
    def history(self) -> tuple:
        return tuple(self._events)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def has_reached(self, state: Any) -> bool:
        return any(event.status == state for event in self._events)

    def as_dict(self) -> dict:
        return {
            "version": EXECUTION_LIFECYCLE_VERSION,
            "order_id": self.order_id,
            "retry_of": self.retry_of,
            "status": self.status,
            "terminal": self.is_terminal,
            "history": [event.as_dict() for event in self._events],
        }

    # ── 前进 ──
    def advance(
        self,
        to_status: str,
        *,
        at: Optional[str] = None,
        source: str = "unspecified",
        evidence: Any = None,
        requested_qty: Any = None,
        filled_qty: Any = None,
        fill_price: Any = None,
        fill_session: Any = None,
    ) -> LifecycleEvent:
        """校验并落一次状态。非法跳转 / 缺证据 / 数量不一致一律抛异常。"""
        target = str(to_status or "")
        if target not in ORDER_STATES:
            raise IllegalLifecycleTransition(f"unknown lifecycle state: {to_status!r}")
        frm = self.status
        if not can_transition(frm, target):
            raise IllegalLifecycleTransition(
                f"illegal lifecycle transition {frm} -> {target} "
                f"(order_id={self.order_id!r}, allowed={sorted(allowed_targets(frm))})"
            )

        quantities = _resolve_quantities(
            evidence,
            requested_qty=requested_qty,
            filled_qty=filled_qty,
            fill_price=fill_price,
            fill_session=fill_session,
        )
        self._validate_quantities(target, quantities)

        event = LifecycleEvent(status=target, at=_text_or_none(at), source=str(source))
        self._events.append(event)
        return event

    def _validate_quantities(self, target: str, quantities: Mapping[str, Any]) -> None:
        requested = quantities.get("requested_qty")
        filled = quantities.get("filled_qty")
        if target in FILLED_STATES:
            if requested is None or requested <= 0:
                raise IllegalLifecycleTransition(
                    f"{target} requires a proven positive requested quantity"
                )
            if filled is None:
                raise IllegalLifecycleTransition(
                    f"{target} requires fill quantity evidence; "
                    "a missing fill must never be assumed filled"
                )
            if target == STATE_PARTIAL_FILLED:
                if not 0 < filled < requested:
                    raise IllegalLifecycleTransition(
                        "PARTIAL_FILLED requires 0 < filled_qty < requested_qty "
                        f"(got filled={filled!r}, requested={requested!r}); "
                        "a partial fill must never be promoted to a full fill"
                    )
                return
            if filled != requested:
                raise IllegalLifecycleTransition(
                    "FILLED requires filled_qty == requested_qty "
                    f"(got filled={filled!r}, requested={requested!r}); "
                    "a partial fill must never be promoted to a full fill"
                )
            price = quantities.get("fill_price")
            if price is None or price <= 0:
                raise IllegalLifecycleTransition(
                    "FILLED requires a trustworthy positive fill price"
                )
            if not str(quantities.get("fill_session") or "").strip():
                raise IllegalLifecycleTransition("FILLED requires a fill session")
            return
        if target == STATE_REJECTED:
            if filled is not None and filled > 0:
                raise IllegalLifecycleTransition(
                    f"REJECTED cannot carry a positive filled quantity (got {filled!r})"
                )
            return
        if target in (STATE_CANCELLED, STATE_EXPIRED):
            # 部分成交后撤单/过期是真实的（剩余委托被回收），但**全部成交**只能
            # 是 FILLED；"撤单"不允许把一次完整成交降级成撤销。
            if filled is not None and filled > 0:
                if requested is None or requested <= 0:
                    raise IllegalLifecycleTransition(
                        f"{target} carries a positive fill without a proven requested quantity"
                    )
                if filled >= requested:
                    raise IllegalLifecycleTransition(
                        f"{target} cannot carry a complete fill; a complete fill is FILLED "
                        f"(got filled={filled!r}, requested={requested!r})"
                    )
            return
        if target in (STATE_SUBMITTED, STATE_ACCEPTED):
            if filled is not None and filled > 0:
                raise IllegalLifecycleTransition(
                    f"{target} cannot carry a positive filled quantity (got {filled!r})"
                )

    # ── 重试：必须是新的委托生命周期 ──
    def retry(
        self,
        new_order_id: Any,
        *,
        at: Optional[str] = None,
        source: str = "retry_new_lifecycle",
    ) -> "OrderLifecycle":
        """为同一信号开一笔**新的**委托生命周期。

        这是 ``REJECTED -> FILLED`` 唯一合法的表达方式：新 order_id、新历史、
        ``retry_of`` 指回旧委托。已成交的委托没有"重试"。
        """
        if self.status == STATE_FILLED:
            raise IllegalLifecycleTransition(
                f"order {self.order_id!r} is already FILLED and cannot be retried"
            )
        return OrderLifecycle(
            new_order_id, created_at=at, source=source, retry_of=self.order_id,
        )


def _text_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _resolve_quantities(
    evidence: Any,
    *,
    requested_qty: Any = None,
    filled_qty: Any = None,
    fill_price: Any = None,
    fill_session: Any = None,
) -> dict:
    """显式参数优先；否则从三态证据里取 **known** 的值。"""
    resolved = {
        "requested_qty": requested_qty,
        "filled_qty": filled_qty,
        "fill_price": fill_price,
        "fill_session": fill_session,
    }
    if evidence is None:
        return resolved
    for key in ("requested_qty", "filled_qty", "fill_price", "fill_session"):
        if resolved[key] is None:
            resolved[key] = _known_value(evidence, key)
    return resolved


def audit_stored_rows(rows: Sequence[Mapping[str, Any]]) -> dict:
    """审计一批委托行：哪些 stored 状态**自证不了**。

    只读纯函数，供离线审计与测试使用；不做任何写操作。
    """
    counts: dict = {}
    unsupported: list = []
    for row in rows or ():
        record = dict(row)
        state = canonical_state(record.get("status"), has_fill=bool(record.get("has_fill")))
        counts[state] = counts.get(state, 0) + 1
        verdict = observed_fill_supported(
            lifecycle_state=state,
            requested_qty=record.get("requested_qty"),
            filled_qty=record.get("filled_qty"),
            fill_price=record.get("fill_price"),
            fill_session=record.get("fill_session"),
        )
        if not verdict["supported"]:
            unsupported.append({
                "order_id": record.get("id"),
                "status": record.get("status"),
                "state": state,
                "reason": verdict["reason"],
            })
    return {
        "version": EXECUTION_LIFECYCLE_VERSION,
        "rows": len(rows or ()),
        "state_counts": counts,
        "unsupported_fill_claims": unsupported,
    }
