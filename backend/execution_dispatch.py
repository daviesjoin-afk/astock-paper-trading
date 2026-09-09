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
- **TTL 清扫**：被挂起的委托都有 ``expires_at``。到期未放行的委托一律
  终态作废（PR-29 单一归属语义：过期 order row 永远 terminal，绝不原地
  改写成 ``execution_retry``）；``strict_ttl`` 画像与信号过期一并收敛，
  非 strict 画像在信号仍新鲜时把信号送回复试管道，由下一轮扫描基于
  Signal 重建新委托（新 id / 新 ``expires_at``，旧委托以
  ``retry_of_order_id`` 保留审计血缘）——执行器故障不会让一笔已过风控
  的候选永久丢失，但也绝不留下"活动/重试状态却已过期"的委托。

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

EXECUTION_DISPATCH_VERSION = "execution-dispatch-v2"

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
# 一次性标记：批量窗口到期放行后写入，下一轮扫描绕过批量闸门。
BATCH_RELEASE_PAYLOAD_KEY = "execution_batch_release"

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


def _marker_fresh(marker: Any, now: dt.datetime) -> bool:
    """标记只在写入当日有效；跨日重新回到画像默认行为。"""
    if not isinstance(marker, Mapping):
        return False
    return str(marker.get("at") or "")[:10] == now.date().isoformat()


def is_verification_rejected(conn, account_id, code, day=None) -> bool:
    """该账户当日该标的是否已被人工核验驳回。

    驳回只把信号标为 ``rejected`` 是不够的：日内引导会把普通 rejected
    信号 supersede 掉，并可能用**新的 signal 行**（payload 为空）重建同一
    候选，于是同一标的会再次进入核验队列。驳回结论因此按
    ``账户 × 标的 × 交易日`` 持久化在信号 payload 里，重建的新行也要先查
    历史行。
    """
    if conn is None or not code:
        return False
    target_day = str(day)[:10] if day is not None else _now().date().isoformat()
    try:
        rows = conn.execute(
            """SELECT payload FROM paper_signals
                WHERE account_id=? AND code=? AND substr(COALESCE(intended_date,created_at),1,10)=?
                  AND payload LIKE '%execution_verification%'""",
            (str(account_id), str(code), target_day),
        ).fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        state = _verification_state(_signal_payload_from_row(row))
        if state and not state.get("approved"):
            return True
    return False


def plan_execution_dispatch(
    profile: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
    signal_payload: Any = None,
    dispatch_settings: Mapping[str, Any] | None = None,
    verification_rejected: bool = False,
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
    blocked = False
    blocked_reason: str | None = None

    requires_verification = bool(profile.get("verification_required"))
    verification = _verification_state(signal_payload)
    if requires_verification and verification_rejected:
        # 当日已被运营驳回：直接终止本次候选，不再重复进入核验队列。
        blocked = True
        blocked_reason = "该标的当日已被人工核验驳回，当日不再重复提交核验"
        explanation.append("命中当日人工核验驳回记录，候选终止")
    elif requires_verification and flags["execution_verification_gate"] and not verification.get("approved"):
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

    released = _marker_fresh(
        (signal_payload or {}).get(BATCH_RELEASE_PAYLOAD_KEY)
        if isinstance(signal_payload, Mapping) else None,
        moment,
    )
    if gate == "none" and blocked and bool(profile.get("batch")):
        explanation.append("候选已被终止，不再评估批量闸门")
    elif gate == "none" and bool(profile.get("batch")) and released:
        # 一次性放行标记：批量窗口到期未撮合时写入，避免同一信号被反复挂起。
        explanation.append("已按执行时限一次性放行，本轮不再进入批量等待")
    elif gate == "none" and bool(profile.get("batch")) and flags["execution_batch_gate"]:
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
        "blocked": blocked,
        "blocked_reason": blocked_reason,
        "batch_released": released,
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


def _signal_payload_from_row(row: Any) -> dict[str, Any]:
    import json

    raw = row["payload"] if row is not None else None
    if isinstance(row, Mapping):
        raw = row.get("payload")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_signal_marker(
    conn,
    signal_id,
    key: str,
    value: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> None:
    """把一次性标记写入信号 payload（信号被 supersede 后仍随行保留）。

    时间戳显式取调用方传入的时钟，保证"清扫时钟"与"判定时钟"是同一个；
    生产里两者都是真实时间。
    """
    if signal_id is None:
        return
    import json

    payload = _signal_payload(conn, signal_id)
    marker = dict(value)
    marker["at"] = _iso(now or _now())
    payload[key] = marker
    try:
        conn.execute(
            "UPDATE paper_signals SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), int(signal_id)),
        )
    except sqlite3.Error:
        pass


def _signal_alive(conn, signal_id, *, now: dt.datetime) -> bool:
    """信号是否仍新鲜（决定过期委托能否由信号重建新的委托尝试）。

    PR-29 单一归属语义：只有 ``signal_freshness`` 判定 usable 的信号才允许
    重新回到复试管道；过期/终态信号不再借尸还魂。
    """
    if signal_id is None:
        return False
    try:
        row = conn.execute(
            """SELECT created_at,intended_date,signal_date,status
                 FROM paper_signals WHERE id=?""",
            (int(signal_id),),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    if str(row["status"] or "") in TERMINAL_SIGNAL_STATUSES:
        return False
    try:
        import entry_lifecycle as ELC

        verdict = ELC.signal_freshness(
            dict(row), now=now, asof_day=now.date().isoformat(),
        )
    except Exception:
        return False
    return bool(verdict.get("usable"))


def _retire_for_retry(
    conn,
    row: Mapping[str, Any],
    reason: str,
    *,
    release_batch: bool = False,
    now: dt.datetime | None = None,
) -> bool:
    """把到期挂起委托收敛为终态 ``superseded``，由信号重建新委托（PR-29）。

    单一归属语义（与 entry_lifecycle 合并）：

    - **过期 order row 永远 terminal**——不允许把已经过期的委托改写成
      ``execution_retry`` 继续占用活动/重试视图；
    - 信号仍 fresh 时回到 ``pending``，下一轮扫描基于 Signal 创建新的
      OrderIntent（新 order id / 新 ``expires_at``），并在新委托上通过
      ``retry_of_order_id`` 保留完整审计血缘；
    - 信号已过期/终态时一并收敛为 ``expired``，候选彻底终止。

    ``release_batch`` 用于批量窗口到期放行：写入一次性放行标记，避免重建的
    新委托在收工后被同一轮转画像再次挂起。
    """
    moment = now or _now()
    order_id = int(row["id"])
    signal_id = row.get("signal_id")
    conn.execute(
        """UPDATE paper_orders
              SET status='superseded',
                  reason=COALESCE(reason,'') || '；' || ?,cancelled_at=?
            WHERE id=?""",
        (reason, _iso(moment), order_id),
    )
    _release_reservation(conn, order_id)
    if release_batch and signal_id is not None:
        _write_signal_marker(conn, signal_id, BATCH_RELEASE_PAYLOAD_KEY, {
            "released": True, "reason": reason,
        }, now=moment)
    if signal_id is not None:
        terminal = ",".join("?" for _ in TERMINAL_SIGNAL_STATUSES)
        if _signal_alive(conn, signal_id, now=moment):
            conn.execute(
                f"""UPDATE paper_signals SET status=?,reason=?
                     WHERE id=? AND status NOT IN ({terminal})""",
                (RETRY_SIGNAL_STATUS, reason, int(signal_id),
                 *TERMINAL_SIGNAL_STATUSES),
            )
        else:
            conn.execute(
                f"""UPDATE paper_signals SET status='expired',
                         reason=COALESCE(reason,'') || '；' || ?
                     WHERE id=? AND status NOT IN ({terminal})""",
                (reason + "；信号已过有效期，一并失效", int(signal_id),
                 *TERMINAL_SIGNAL_STATUSES),
            )
    return True


def _terminate(
    conn,
    row: Mapping[str, Any],
    status: str,
    reason: str,
    *,
    terminal_signal: bool = True,
    signal_status: str = "rejected",
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
            "UPDATE paper_signals SET status=?,reason=? WHERE id=?",
            (signal_status, reason, int(row["signal_id"])),
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
    """清扫到期/失效的挂起委托（PR-29 单一归属 TTL 语义）。

    - 信号已进入终态 → 挂起委托回收（superseded）；
    - 超过 ``expires_at`` → **委托行一律终态**：``strict_ttl`` 画像作废
      （expired），其余也作废（superseded）但信号仍新鲜时回到复试管道，
      由下一轮扫描基于 Signal 创建新委托（新 id / 新 expires_at，旧委托以
      ``retry_of_order_id`` 保留血缘）。**绝不把过期 order row 原地改写成
      ``execution_retry``**——该状态只允许由成交路径为未过期尝试新建。
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
                signal_status="expired",
            )
            summary["expired"] += 1
        else:
            _retire_for_retry(
                conn, row,
                "执行时限到期，挂起委托作废；信号仍新鲜时由下一轮重建新委托",
                release_batch=True, now=moment,
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
    now: dt.datetime | None = None,
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
        "order_id": int(order_id),
    }
    _write_signal_marker(conn, signal_id, VERIFICATION_PAYLOAD_KEY, decision, now=now)
    if approved:
        # PR-29：放行不是把挂起行原地改写为重试行，而是作废本次挂起尝试；
        # 放行结论已写入信号 payload，下一轮由信号重建新委托（新 expires_at）。
        _retire_for_retry(
            conn, record,
            f"人工核验放行（{decision['operator'] or '运营'}）：{note or '无备注'}",
            now=now,
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
        "status": "released" if approved else CANCELLED_ORDER_STATUS,
        "order_status": "superseded" if approved else CANCELLED_ORDER_STATUS,
        "version": EXECUTION_DISPATCH_VERSION,
    }
