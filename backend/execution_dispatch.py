# -*- coding: utf-8 -*-
"""执行画像的执行器（PR-11）：批量窗口、人工核验、TTL 清扫。

设计
----
PR-10 把 TTL / batch / verification 作为订单审计字段引入，但具体的撮合
窗口、人工核验流程和到期清扫只是"延期依据"，没有真正的执行器。本模块补齐
这三类执行器，并遵守 PR-10 的边界：**只用 market / limit 两种订单类型**。

三个执行器的作用对象都是"被挂起"的委托（gated order），即本可以立即成交、
但按执行画像需要在特定条件下放行的委托：

- **批量窗口（rotation）**：``awaiting_batch``。轮动画像的委托不在盘中零散
  成交，而是等收盘前的批量窗口统一放行。窗口内到达的委托立即成交（不挂起）。
- **人工核验（event）**：``pending_verification``。事件画像的委托先进入核验
  队列，由运营显式放行或驳回，未核验前不成交。
- **TTL 清扫**：被挂起的委托都有 ``expires_at``。到期未放行的委托按画像
  ``strict_ttl`` 决定终态作废还是放回重试管道——**默认放回重试管道**，
  保证任何执行器故障都不会让一笔已经通过全部风控的候选永久丢失。

被挂起的委托**不预占资金、不占用席位**（挂起发生在预占之前），所以批量
等待不会削弱其它策略的可用额度。

失败语义
--------
- 设置缺省：``execution_batch_gate`` 默认开启、``execution_verification_gate``
  默认关闭（开启后该账户每笔买入都需人工放行，属于运营决策）、
  ``execution_ttl_sweep`` 默认开启。
- 画像未知 → :func:`execution_profiles.execution_profile_for` 已 fail-closed
  回落 composite（无批量、无核验），本模块不做二次猜测。
- 数据库读取异常一律返回空队列，绝不让执行器崩掉主扫描。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Mapping, Sequence

__all__ = [
    "EXECUTION_DISPATCH_VERSION",
    "BATCH_HOLD_STATUS",
    "VERIFICATION_HOLD_STATUS",
    "GATED_ORDER_STATUSES",
    "EXPIRED_ORDER_STATUS",
    "CANCELLED_ORDER_STATUS",
    "ROTATION_BATCH_WINDOWS",
    "RETRY_ORDER_STATUS",
    "RETRY_SIGNAL_STATUS",
    "TERMINAL_SIGNAL_STATUSES",
    "VERIFICATION_PAYLOAD_KEY",
    "active_gated_order",
    "batch_queue",
    "batch_window_state",
    "dispatch_overview",
    "plan_execution_dispatch",
    "resolve_verification",
    "retire_gated_orders",
    "run_execution_dispatch",
    "settings",
    "verification_queue",
]

EXECUTION_DISPATCH_VERSION = "execution-dispatch-v1"

# 挂起状态：这两个状态都位于成交之前，不预占、不占席位。
BATCH_HOLD_STATUS = "awaiting_batch"
VERIFICATION_HOLD_STATUS = "pending_verification"
GATED_ORDER_STATUSES = (BATCH_HOLD_STATUS, VERIFICATION_HOLD_STATUS)

EXPIRED_ORDER_STATUS = "expired"
CANCELLED_ORDER_STATUS = "cancelled"

# 放回重试管道时使用的既有状态（复用 PR-10 之前的 retry 机制）。
RETRY_ORDER_STATUS = "execution_retry"
RETRY_SIGNAL_STATUS = "pending"

# 与 ``_reconcile_signal_order_states`` 保持一致的信号终态集合。
TERMINAL_SIGNAL_STATUSES = frozenset({
    "superseded", "filled", "rejected", "blocked", "expired", "shadow_q3",
})

# 批量撮合窗口（本地时钟 HH:MM）：收盘前两段，覆盖轮动调仓的常规时点。
ROTATION_BATCH_WINDOWS = (("14:30", "14:45"), ("14:50", "15:00"))

# 信号 payload 里记录核验结论的键；放行的委托在下一轮扫描就不再被挂起。
VERIFICATION_PAYLOAD_KEY = "execution_verification"

SETTING_KEYS = (
    "execution_batch_gate",
    "execution_verification_gate",
    "execution_ttl_sweep",
)

DEFAULT_SETTINGS: dict[str, bool] = {
    "execution_batch_gate": True,
    "execution_verification_gate": False,
    "execution_ttl_sweep": True,
}


def _now() -> dt.datetime:
    return dt.datetime.now()


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _parse_hhmm(value) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if len(text) < 4 or ":" not in text:
        return None
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _minutes(value: dt.datetime) -> int:
    return value.hour * 60 + value.minute


def _window_bounds(now: dt.datetime, window) -> tuple[dt.datetime, dt.datetime] | None:
    start = _parse_hhmm(window[0]) if isinstance(window, (tuple, list)) else None
    end = _parse_hhmm(window[1]) if isinstance(window, (tuple, list)) and len(window) > 1 else None
    if start is None or end is None:
        return None
    base = now.replace(second=0, microsecond=0)
    start_at = base.replace(hour=start[0], minute=start[1])
    end_at = base.replace(hour=end[0], minute=end[1])
    if end_at <= start_at:
        return None
    return start_at, end_at


def batch_window_state(
    now: dt.datetime | None = None,
    windows: Sequence = ROTATION_BATCH_WINDOWS,
) -> dict[str, Any]:
    """返回当前所处的批量撮合窗口状态（纯计算，不读库）。"""
    moment = now or _now()
    ordered = []
    for index, window in enumerate(windows or ()):
        bounds = _window_bounds(moment, window)
        if bounds is None:
            continue
        start_at, end_at = bounds
        ordered.append((index, start_at, end_at))
    in_window = False
    current: dict[str, Any] | None = None
    next_window: dict[str, Any] | None = None
    for index, start_at, end_at in ordered:
        if start_at <= moment <= end_at:
            in_window = True
            current = {
                "index": index,
                "start": start_at.strftime("%H:%M"),
                "end": end_at.strftime("%H:%M"),
                "starts_at": _iso(start_at),
                "ends_at": _iso(end_at),
            }
            break
    if not in_window:
        now_minutes = _minutes(moment)
        for index, start_at, end_at in ordered:
            if _minutes(start_at) > now_minutes:
                next_window = {
                    "index": index,
                    "start": start_at.strftime("%H:%M"),
                    "end": end_at.strftime("%H:%M"),
                    "starts_at": _iso(start_at),
                    "ends_at": _iso(end_at),
                }
                break
    return {
        "in_window": in_window,
        "current_window": current,
        "next_window": next_window,
        "windows": [
            {"index": index, "start": start_at.strftime("%H:%M"),
             "end": end_at.strftime("%H:%M")}
            for index, start_at, end_at in ordered
        ],
        "version": EXECUTION_DISPATCH_VERSION,
    }


def _next_business_window_start(
    now: dt.datetime,
    windows: Sequence = ROTATION_BATCH_WINDOWS,
) -> dt.datetime | None:
    """返回下一个尚未开始的窗口起点；今日已无窗口则返回 None。"""
    now_minutes = _minutes(now)
    for window in windows or ():
        bounds = _window_bounds(now, window)
        if bounds is None:
            continue
        start_at = bounds[0]
        if _minutes(start_at) > now_minutes:
            return start_at
    return None


def settings(conn=None) -> dict[str, bool]:
    """读取执行器开关；无连接或读失败时返回缺省值。"""
    resolved = dict(DEFAULT_SETTINGS)
    if conn is None:
        return resolved
    try:
        import runtime_settings as RSET

        for key in SETTING_KEYS:
            resolved[key] = bool(RSET.get(conn, key, DEFAULT_SETTINGS[key]))
    except Exception:
        return dict(DEFAULT_SETTINGS)
    return resolved


def _verification_state(signal_payload: Any) -> dict[str, Any]:
    if isinstance(signal_payload, Mapping):
        state = signal_payload.get(VERIFICATION_PAYLOAD_KEY)
        if isinstance(state, Mapping):
            return dict(state)
    return {}


def plan_execution_dispatch(
    profile: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
    signal_payload: Any = None,
    dispatch_settings: Mapping[str, Any] | None = None,
    windows: Sequence = ROTATION_BATCH_WINDOWS,
) -> dict[str, Any]:
    """按执行画像决定本笔委托是否需要挂起，以及挂起到何时。

    返回 ``gate`` ∈ ``{"none", "batch", "verification"}``：

    - ``none``：正常走成交路径（订单类型仍是 market/limit）；
    - ``batch``：挂起为 ``awaiting_batch``，等到下一个批量窗口放行；
    - ``verification``：挂起为 ``pending_verification``，等待人工核验。

    无论哪种结果都带 ``explanation``（逐步可读解释）与完整审计字段。
    """
    moment = now or _now()
    flags = dict(DEFAULT_SETTINGS)
    if isinstance(dispatch_settings, Mapping):
        for key in SETTING_KEYS:
            if key in dispatch_settings:
                flags[key] = bool(dispatch_settings[key])
    window_state = batch_window_state(moment, windows)
    ttl_minutes = profile.get("ttl_minutes")
    try:
        ttl_minutes = float(ttl_minutes) if ttl_minutes is not None else None
    except (TypeError, ValueError):
        ttl_minutes = None

    explanation: list[str] = []
    gate = "none"
    status: str | None = None
    expires_at: dt.datetime | None = None
    reason: str | None = None

    requires_verification = bool(profile.get("verification_required"))
    verification = _verification_state(signal_payload)
    if requires_verification and flags["execution_verification_gate"] and not verification.get("approved"):
        gate = "verification"
        status = VERIFICATION_HOLD_STATUS
        reason = (
            f"{profile.get('label', profile.get('family', '执行'))}画像需人工核验："
            "委托已进入核验队列，放行后按限价执行"
        )
        explanation.append(
            f"画像 {profile.get('family', 'unknown')} 要求人工核验，委托挂起等待放行"
        )
    elif requires_verification and not flags["execution_verification_gate"]:
        explanation.append("画像要求人工核验，但核验闸门当前关闭，按普通路径执行")

    if gate == "none" and bool(profile.get("batch")) and flags["execution_batch_gate"]:
        if window_state["in_window"]:
            explanation.append(
                f"处于批量窗口 {window_state['current_window']['start']}-"
                f"{window_state['current_window']['end']}，按窗口内成交"
            )
        else:
            gate = "batch"
            status = BATCH_HOLD_STATUS
            next_start = _next_business_window_start(moment, windows)
            grace = dt.timedelta(minutes=ttl_minutes if ttl_minutes else 30.0)
            if next_start is not None:
                expires_at = next_start + grace
                wait_minutes = max(0, int((next_start - moment).total_seconds() // 60))
                reason = (
                    f"{profile.get('label', profile.get('family', '执行'))}画像批量撮合："
                    f"委托挂起至 {next_start.strftime('%H:%M')} 批量窗口（约 {wait_minutes} 分钟），"
                    f"窗口后 {int(grace.total_seconds() // 60)} 分钟未撮合则自动放行"
                )
                explanation.append(
                    f"挂起至下一批量窗口 {next_start.strftime('%H:%M')}，宽限期 "
                    f"{int(grace.total_seconds() // 60)} 分钟"
                )
            else:
                expires_at = moment + grace
                reason = (
                    f"{profile.get('label', profile.get('family', '执行'))}画像批量撮合："
                    "今日已无批量窗口，委托挂起至宽限期结束自动放行"
                )
                explanation.append("今日已无剩余批量窗口，按宽限期自动放行")

    return {
        "gate": gate,
        "status": status,
        "reason": reason,
        "expires_at": _iso(expires_at),
        "ttl_minutes": ttl_minutes,
        "strict_ttl": bool(profile.get("strict_ttl")),
        "family": profile.get("family"),
        "label": profile.get("label"),
        "in_batch_window": bool(window_state["in_window"]),
        "current_window": window_state["current_window"],
        "next_window": window_state["next_window"],
        "verification": verification or None,
        "settings": {key: bool(flags[key]) for key in SETTING_KEYS},
        "explanation": explanation or ["画像无需挂起，按普通路径执行"],
        "version": EXECUTION_DISPATCH_VERSION,
    }


def active_gated_order(conn, signal_id) -> dict[str, Any] | None:
    """返回该信号当前仍处于挂起状态的委托（至多一条）。"""
    if signal_id is None:
        return None
    try:
        row = conn.execute(
            """SELECT id,status,expires_at,created_at,reason
                 FROM paper_orders
                WHERE signal_id=? AND side='buy' AND status IN (?,?)
                ORDER BY id DESC LIMIT 1""",
            (int(signal_id), *GATED_ORDER_STATUSES),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def _gated_rows(conn) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """SELECT o.id,o.status,o.expires_at,o.created_at,o.reason,
                      o.account_id,o.code,o.name,o.qty,o.planned_price,
                      o.signal_id,o.risk_payload,s.status AS signal_status
                 FROM paper_orders o
                 LEFT JOIN paper_signals s ON s.id=o.signal_id
                WHERE o.side='buy' AND o.status IN (?,?)
                ORDER BY o.id""",
            GATED_ORDER_STATUSES,
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _payload_of(row: Mapping[str, Any]) -> dict[str, Any]:
    import json

    raw = row.get("risk_payload")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _release_reservation(conn, order_id) -> None:
    try:
        conn.execute(
            """UPDATE paper_capital_reservations
                  SET status='released',released_at=COALESCE(released_at,?)
                WHERE status='reserved' AND CAST(order_key AS TEXT)=?""",
            (_iso(_now()), str(int(order_id))),
        )
    except sqlite3.Error:
        pass


def _retire_retry_rows(conn, signal_id) -> int:
    """让同一信号只保留一条重试委托，避免部分唯一索引冲突。"""
    if signal_id is None:
        return 0
    try:
        rows = conn.execute(
            """SELECT id FROM paper_orders
                WHERE signal_id=? AND side='buy' AND status=?
                ORDER BY id""",
            (int(signal_id), RETRY_ORDER_STATUS),
        ).fetchall()
    except sqlite3.Error:
        return 0
    for row in rows:
        order_id = int(row["id"])
        conn.execute(
            """UPDATE paper_orders
                  SET status='superseded',
                      reason=COALESCE(reason,'') || '；执行器放行，旧重试委托已回收'
                WHERE id=? AND status=?""",
            (order_id, RETRY_ORDER_STATUS),
        )
        _release_reservation(conn, order_id)
    return len(rows)


def _release_to_retry(conn, row: Mapping[str, Any], reason: str) -> None:
    """把挂起委托放回重试管道；信号回到 pending，下一轮扫描重跑全部闸门。"""
    order_id = int(row["id"])
    signal_id = row.get("signal_id")
    _retire_retry_rows(conn, signal_id)
    conn.execute(
        "UPDATE paper_orders SET status=?,reason=COALESCE(reason,'') || '；' || ? WHERE id=?",
        (RETRY_ORDER_STATUS, reason, order_id),
    )
    if signal_id is not None:
        conn.execute(
            "UPDATE paper_signals SET status=?,reason=? WHERE id=?",
            (RETRY_SIGNAL_STATUS, reason, int(signal_id)),
        )


def _terminate(
    conn,
    row: Mapping[str, Any],
    status: str,
    reason: str,
    *,
    terminal_signal: bool = True,
) -> None:
    order_id = int(row["id"])
    _release_reservation(conn, order_id)
    conn.execute(
        """UPDATE paper_orders
              SET status=?,reason=COALESCE(reason,'') || '；' || ?,cancelled_at=?
            WHERE id=?""",
        (status, reason, _iso(_now()), order_id),
    )
    if terminal_signal and row.get("signal_id") is not None:
        conn.execute(
            "UPDATE paper_signals SET status='rejected',reason=? WHERE id=?",
            (reason, int(row["signal_id"])),
        )


def retire_gated_orders(conn, signal_id, *, reason: str = "执行闸门已开，挂起委托回收") -> int:
    """回收某信号遗留的挂起委托（闸门打开后由成交路径调用）。"""
    if signal_id is None:
        return 0
    try:
        rows = conn.execute(
            """SELECT id FROM paper_orders
                WHERE signal_id=? AND side='buy' AND status IN (?,?)""",
            (int(signal_id), *GATED_ORDER_STATUSES),
        ).fetchall()
    except sqlite3.Error:
        return 0
    for row in rows:
        order_id = int(row["id"])
        conn.execute(
            """UPDATE paper_orders
                  SET status='superseded',reason=COALESCE(reason,'') || '；' || ?
                WHERE id=? AND status IN (?,?)""",
            (reason, order_id, *GATED_ORDER_STATUSES),
        )
        _release_reservation(conn, order_id)
    return len(rows)


def sweep_expired_gated_orders(
    conn,
    now: dt.datetime | None = None,
    dispatch_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """清扫到期/失效的挂起委托。

    - 信号已进入终态 → 挂起委托回收（superseded）；
    - 超过 ``expires_at`` → ``strict_ttl`` 画像终态作废，其余放回重试管道
      （fail-open：执行器故障不会永久吞掉一笔已过风控的候选）。
    """
    flags = settings(conn)
    if isinstance(dispatch_settings, Mapping):
        flags.update({key: bool(dispatch_settings[key]) for key in SETTING_KEYS if key in dispatch_settings})
    moment = now or _now()
    summary = {
        "checked": 0, "released": 0, "expired": 0, "retired": 0,
        "version": EXECUTION_DISPATCH_VERSION,
    }
    if not flags["execution_ttl_sweep"]:
        return summary
    for row in _gated_rows(conn):
        summary["checked"] += 1
        signal_status = str(row.get("signal_status") or "")
        if signal_status in TERMINAL_SIGNAL_STATUSES:
            _terminate(
                conn, row, "superseded",
                "关联信号已进入终态，挂起委托回收", terminal_signal=False,
            )
            summary["retired"] += 1
            continue
        expires_at = row.get("expires_at")
        if not expires_at:
            continue
        try:
            deadline = dt.datetime.fromisoformat(str(expires_at))
        except ValueError:
            continue
        if moment <= deadline:
            continue
        strict = False
        payload = _payload_of(row)
        profile = payload.get("execution_profile") or {}
        if isinstance(profile, Mapping):
            strict = bool(profile.get("strict_ttl"))
        if strict:
            _terminate(
                conn, row, EXPIRED_ORDER_STATUS,
                "严格时限画像到期未成交，委托作废",
            )
            summary["expired"] += 1
        else:
            _release_to_retry(
                conn, row,
                "执行器时限到期未撮合，自动放行并由下一轮扫描重新过闸",
            )
            summary["released"] += 1
    return summary


def run_execution_dispatch(
    conn,
    now: dt.datetime | None = None,
    dispatch_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """周期清扫入口（每轮扫描调用一次），返回本轮处置摘要。"""
    return sweep_expired_gated_orders(conn, now=now, dispatch_settings=dispatch_settings)


def _queue_row(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _payload_of(row)
    profile = payload.get("execution_profile") or {}
    sizing = payload.get("sizing") or {}
    return {
        "order_id": int(row["id"]),
        "signal_id": row.get("signal_id"),
        "account_id": row.get("account_id"),
        "code": row.get("code"),
        "name": row.get("name"),
        "qty": int(row.get("qty") or 0),
        "planned_price": row.get("planned_price"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "reason": row.get("reason"),
        "family": (profile or {}).get("family") if isinstance(profile, Mapping) else None,
        "profile_label": (profile or {}).get("label") if isinstance(profile, Mapping) else None,
        "target_amount": (sizing or {}).get("target_amount") if isinstance(sizing, Mapping) else None,
    }


def batch_queue(conn, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    window_state = batch_window_state(now)
    rows = [
        {**_queue_row(row), "window": window_state["current_window"] or window_state["next_window"]}
        for row in _gated_rows(conn)
        if row.get("status") == BATCH_HOLD_STATUS
    ]
    return rows


def verification_queue(conn) -> list[dict[str, Any]]:
    return [
        _queue_row(row)
        for row in _gated_rows(conn)
        if row.get("status") == VERIFICATION_HOLD_STATUS
    ]


def dispatch_overview(conn, now: dt.datetime | None = None) -> dict[str, Any]:
    """运营视图：开关 + 窗口 + 两条队列。"""
    moment = now or _now()
    return {
        "engine": EXECUTION_DISPATCH_VERSION,
        "settings": settings(conn),
        "windows": batch_window_state(moment),
        "batch_queue": batch_queue(conn, moment),
        "verification_queue": verification_queue(conn),
    }


def _signal_payload(conn, signal_id) -> dict[str, Any]:
    if signal_id is None:
        return {}
    try:
        row = conn.execute(
            "SELECT payload FROM paper_signals WHERE id=?", (int(signal_id),)
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    import json

    try:
        parsed = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_verification(
    conn,
    order_id: int,
    *,
    approved: bool,
    operator: str = "",
    note: str = "",
) -> dict[str, Any]:
    """人工核验结论：放行（放回重试管道）或驳回（终态作废）。"""
    row = conn.execute(
        "SELECT id,status,signal_id,risk_payload FROM paper_orders WHERE id=?",
        (int(order_id),),
    ).fetchone()
    if row is None:
        return {"ok": False, "reason": "委托不存在", "order_id": int(order_id)}
    record = dict(row)
    if record.get("status") != VERIFICATION_HOLD_STATUS:
        return {
            "ok": False,
            "reason": f"委托状态为 {record.get('status')}，不在核验队列中",
            "order_id": int(order_id),
        }
    signal_id = record.get("signal_id")
    decision = {
        "approved": bool(approved),
        "operator": str(operator or "")[:64],
        "note": str(note or "")[:500],
        "at": _iso(_now()),
        "order_id": int(order_id),
    }
    payload = _signal_payload(conn, signal_id)
    payload[VERIFICATION_PAYLOAD_KEY] = decision
    import json

    if signal_id is not None:
        conn.execute(
            "UPDATE paper_signals SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), int(signal_id)),
        )
    if approved:
        _release_to_retry(
            conn, record,
            f"人工核验放行（{decision['operator'] or '运营'}）：{note or '无备注'}",
        )
    else:
        _terminate(
            conn, record, CANCELLED_ORDER_STATUS,
            f"人工核验驳回（{decision['operator'] or '运营'}）：{note or '无备注'}",
        )
    return {
        "ok": True,
        "order_id": int(order_id),
        "approved": bool(approved),
        "status": RETRY_ORDER_STATUS if approved else CANCELLED_ORDER_STATUS,
        "version": EXECUTION_DISPATCH_VERSION,
    }
