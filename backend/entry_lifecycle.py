# -*- coding: utf-8 -*-
"""信号与委托生命周期（PR：signal expiry and staged entry）。

三个不变式
----------
1. **信号 TTL**：入场信号是即时证据。超过 ``SIGNAL_TTL_MINUTES`` 仍未成交的
   pending / 延期 / 等待信号转为终态 ``expired``，其活动买单自动取消；旧信号
   永远不能再开一笔新仓——:func:`signal_freshness` 在成交路径最前面拦截。
2. **委托 TTL**：可恢复状态（pending_limit / execution_retry / 手动重试 /
   延期 / 冻结等待）的买单超过 ``ORDER_TTL_MINUTES`` 未成交自动作废并释放
   资金预占，不允许"无限期挂单"继续占用额度或-slot 预算。
3. **分批建仓（entry slices）**：一份信号可以按切片分批建仓，但**每一片都是
   一条独立的新委托意图**——重新取价、重新跑全部风控与 sizing；切片之间不复用
   第一笔的成交价与风险结论，信号失效后剩余切片一并作废。

失败语义
--------
- 时间戳缺失/不可解析 → 判为不新鲜（fail-closed），拒绝开新仓。
- 扫描类函数（expire_*）单行失败不影响其余行；异常向上抛由调用方决定。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Mapping

__all__ = [
    "ENTRY_LIFECYCLE_VERSION",
    "SIGNAL_TTL_MINUTES",
    "ORDER_TTL_MINUTES",
    "SIGNAL_EXPIRED_STATUS",
    "ORDER_EXPIRED_STATUS",
    "RECOVERABLE_ORDER_STATUSES",
    "ACTIVE_OR_RETRY_ORDER_STATUSES",
    "RETRY_SIGNAL_STATUSES",
    "entry_slice_plan",
    "expire_stale_orders",
    "expire_stale_signals",
    "signal_freshness",
    "stale_active_orders",
]

ENTRY_LIFECYCLE_VERSION = "entry-lifecycle-v1"

# 与既有"当日 90 分钟未成交释放再入场"口径保持一致。
SIGNAL_TTL_MINUTES = 90
# 在途买单的最长存活时间；超过即作废（gated 单由执行画像另行管理）。
ORDER_TTL_MINUTES = 240

SIGNAL_EXPIRED_STATUS = "expired"
ORDER_EXPIRED_STATUS = "expired"

# 可恢复但必须受 TTL 约束的买单状态。
RECOVERABLE_ORDER_STATUSES = (
    "pending_limit", "execution_retry", "manual_execution_retry",
    "deferred_capacity", "entry_frozen_waitlist",
)
# 这些状态对应的信号仍在复试管道里，可被 TTL 收敛为终态。
RETRY_SIGNAL_STATUSES = ("pending", "deferred_capacity", "entry_frozen_waitlist")

# PR-29 单一归属 TTL 语义下的"活动/重试态"全集：本模块的可恢复态 +
# 执行器的两个挂起态（字面量定义，避免模块环依赖）。TTL 不变式对全集生效：
# 任何处于这些状态的委托都不允许带着已过期的 expires_at 存活。
ACTIVE_OR_RETRY_ORDER_STATUSES = RECOVERABLE_ORDER_STATUSES + (
    "awaiting_batch", "pending_verification",
)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _parse_ts(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(" ", "T", 1) if "T" not in text and " " in text else text
    try:
        parsed = dt.datetime.fromisoformat(text[:19])
    except ValueError:
        return None
    return parsed


def _signal_timestamp(signal: Mapping[str, Any]) -> dt.datetime | None:
    """信号年龄的时间基准：created_at 优先，其次 intended/signal_date。"""
    for key in ("created_at", "signal_date", "intended_date"):
        parsed = _parse_ts(signal.get(key) if isinstance(signal, Mapping) else None)
        if parsed is not None:
            return parsed
    return None


def signal_freshness(
    signal: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
    ttl_minutes: float = SIGNAL_TTL_MINUTES,
    asof_day=None,
) -> dict[str, Any]:
    """判定信号是否仍可用于开新仓（fail-closed：时间戳缺失 = 不新鲜）。

    旧信号（跨交易日或超过 TTL）一律返回 ``usable=False``——加仓/新开仓只能
    由新信号触发，绝不允许拿着昨日证据继续建仓。
    """
    moment = now or _now()
    stamp = _signal_timestamp(signal)
    if stamp is None:
        return {
            "usable": False, "age_minutes": None, "ttl_minutes": ttl_minutes,
            "reason": "信号缺少有效时间戳，按失效处理（fail-closed）",
            "version": ENTRY_LIFECYCLE_VERSION,
        }
    age_minutes = max(0.0, (moment - stamp).total_seconds() / 60.0)
    if asof_day is not None:
        target_day = str(asof_day)[:10]
        signal_day = str(signal.get("intended_date") or signal.get("signal_date") or "")[:10]
        if signal_day and signal_day != target_day:
            return {
                "usable": False, "age_minutes": round(age_minutes, 1),
                "ttl_minutes": ttl_minutes,
                "reason": f"信号属于 {signal_day}，禁止使用旧信号开新仓",
                "version": ENTRY_LIFECYCLE_VERSION,
            }
        if stamp.strftime("%Y-%m-%d") == target_day and age_minutes > ttl_minutes:
            # 仅日内产生的信号按 TTL 判龄；收盘扫描为下一交易日生成的隔夜
            # 计划信号在目标交易日全天有效（挂钟年龄不代表证据过期）。
            return {
                "usable": False, "age_minutes": round(age_minutes, 1),
                "ttl_minutes": ttl_minutes,
                "reason": f"信号已超过 {int(ttl_minutes)} 分钟有效期（{age_minutes:.0f} 分钟），失效",
                "version": ENTRY_LIFECYCLE_VERSION,
            }
        return {
            "usable": True, "age_minutes": round(age_minutes, 1),
            "ttl_minutes": ttl_minutes, "reason": None,
            "overnight_plan": stamp.strftime("%Y-%m-%d") != target_day,
            "version": ENTRY_LIFECYCLE_VERSION,
        }
    if age_minutes > ttl_minutes:
        return {
            "usable": False, "age_minutes": round(age_minutes, 1),
            "ttl_minutes": ttl_minutes,
            "reason": f"信号已超过 {int(ttl_minutes)} 分钟有效期（{age_minutes:.0f} 分钟），失效",
            "version": ENTRY_LIFECYCLE_VERSION,
        }
    return {
        "usable": True, "age_minutes": round(age_minutes, 1),
        "ttl_minutes": ttl_minutes, "reason": None,
        "version": ENTRY_LIFECYCLE_VERSION,
    }


def _parse_deadline(value: Any) -> dt.datetime | None:
    """解析委托有效期；date-only（YYYY-MM-DD）按**当日收盘**语义处理。

    手动限价单写入的 ``expires_at`` 是包含当日的日期（可比照"当日有效"），
    解析成当天零点会让盘中清扫立刻把仍在等待触发的委托作废。
    """
    text = str(value or "").strip()
    if not text:
        return None
    parsed = _parse_ts(text)
    if parsed is None:
        return None
    if len(text) <= 10:
        return parsed.replace(hour=23, minute=59, second=59)
    return parsed


def entry_slice_plan(
    total_qty: int,
    slice_count: int,
    *,
    lot_size: int = 100,
) -> list[int]:
    """把目标股数切成 ``slice_count`` 片，每片都是整手。

    - 前面的片承担余数（先重后轻），保证切片和恰好等于目标股数；
    - ``slice_count <= 1`` 或目标不足两手时退化为单片（整手向下取整的
      sizing 已在成交路径完成，这里不再二次截断）。
    """
    total = max(0, int(total_qty or 0))
    count = max(1, int(slice_count or 1))
    if count <= 1 or total < 2 * max(1, int(lot_size or 100)):
        return [total]
    lot = max(1, int(lot_size or 100))
    per_slice = max(lot, (total // count // lot) * lot)
    if per_slice == 0:
        return [total]
    slices: list[int] = []
    remaining = total
    while remaining > 0 and len(slices) < count - 1:
        slice_qty = min(per_slice, remaining)
        slices.append(slice_qty)
        remaining -= slice_qty
    if remaining:
        if slices and remaining < lot:
            slices[-1] += remaining
        else:
            slices.append(remaining)
    return slices


def _release_reservation(conn, order_id) -> None:
    try:
        conn.execute(
            """UPDATE paper_capital_reservations
                  SET status='released',released_at=COALESCE(released_at,?)
                WHERE status='reserved' AND CAST(order_key AS TEXT)=?""",
            (_now().isoformat(timespec="seconds"), str(int(order_id))),
        )
    except sqlite3.Error:
        pass


def _order_rows(conn, placeholders: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT o.id,o.status,o.created_at,o.expires_at,o.account_id,o.code,
                   o.signal_id,o.side,o.risk_payload,s.status AS signal_status
              FROM paper_orders o
              LEFT JOIN paper_signals s ON s.id=o.signal_id
             WHERE o.side='buy' AND o.status IN ({placeholders})""",
        RECOVERABLE_ORDER_STATUSES,
    ).fetchall()
    return [dict(row) for row in rows]


def stale_active_orders(
    conn,
    *,
    now: dt.datetime | None = None,
    ttl_minutes: float = ORDER_TTL_MINUTES,
) -> list[dict[str, Any]]:
    """PR-29 TTL 不变式体检：仍处于活动/重试状态但已过有效期的委托。

    判定口径与 :func:`expire_stale_orders` 一致（显式 ``expires_at`` 优先，
    否则按 created_at + TTL）。验收口径即"数据库中不存在 active/retry
    order 带过期 expires_at"——两个清扫器（本模块与执行器）跑完后，
    本函数返回值必须为空。
    """
    moment = now or _now()
    placeholders = ",".join("?" for _ in ACTIVE_OR_RETRY_ORDER_STATUSES)
    try:
        rows = conn.execute(
            f"""SELECT id,status,created_at,expires_at,signal_id,side
                  FROM paper_orders
                 WHERE side='buy' AND status IN ({placeholders})""",
            ACTIVE_OR_RETRY_ORDER_STATUSES,
        ).fetchall()
    except sqlite3.Error:
        return []
    stale: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        deadline = _parse_deadline(record.get("expires_at"))
        if deadline is None:
            created = _parse_ts(record.get("created_at"))
            if created is not None:
                deadline = created + dt.timedelta(minutes=ttl_minutes)
        if deadline is not None and moment > deadline:
            record["deadline"] = deadline.isoformat(timespec="seconds")
            stale.append(record)
    return stale


def expire_stale_orders(
    conn,
    *,
    now: dt.datetime | None = None,
    ttl_minutes: float = ORDER_TTL_MINUTES,
) -> dict[str, Any]:
    """把超龄的活动买单作废并释放预占；关联信号转终态 ``expired``。"""
    moment = now or _now()
    summary = {
        "checked": 0, "expired": 0, "version": ENTRY_LIFECYCLE_VERSION,
        "ttl_minutes": ttl_minutes,
    }
    for row in _order_rows(conn, ",".join("?" for _ in RECOVERABLE_ORDER_STATUSES)):
        summary["checked"] += 1
        # 显式 expires_at（gated / 手动限价单）优先；否则按委托创建时间 + TTL。
        deadline: dt.datetime | None = None
        explicit = _parse_deadline(row.get("expires_at"))
        if explicit is not None:
            deadline = explicit
        else:
            created = _parse_ts(row.get("created_at"))
            if created is not None:
                deadline = created + dt.timedelta(minutes=ttl_minutes)
        if deadline is None or moment <= deadline:
            continue
        order_id = int(row["id"])
        reason = "委托超过时限未成交，自动作废并释放预占"
        _release_reservation(conn, order_id)
        conn.execute(
            """UPDATE paper_orders
                  SET status=?,reason=COALESCE(reason,'') || '；' || ?,cancelled_at=?
                WHERE id=?""",
            (ORDER_EXPIRED_STATUS, reason, moment.isoformat(timespec="seconds"), order_id),
        )
        signal_id = row.get("signal_id")
        if signal_id is not None and str(row.get("signal_status") or "") not in ("filled",):
            conn.execute(
                """UPDATE paper_signals SET status=?,reason=COALESCE(reason,'') || '；' || ?
                    WHERE id=? AND status IN (?,?,?)""",
                (SIGNAL_EXPIRED_STATUS, reason, int(signal_id), *RETRY_SIGNAL_STATUSES),
            )
        summary["expired"] += 1
    return summary


def expire_stale_signals(
    conn,
    *,
    now: dt.datetime | None = None,
    ttl_minutes: float = SIGNAL_TTL_MINUTES,
    asof_day=None,
) -> dict[str, Any]:
    """把超过 TTL / 跨交易日的复试管道信号收敛为 ``expired``，并回收其买单。

    - 传入 ``asof_day`` 时：信号日期不等于当前交易日即失效（跨日信号禁止开仓）；
    - 未传入时：按 ``ttl_minutes`` 年龄失效。
    """
    moment = now or _now()
    summary = {
        "checked": 0, "expired": 0, "orders_cancelled": 0,
        "version": ENTRY_LIFECYCLE_VERSION, "ttl_minutes": ttl_minutes,
    }
    placeholders = ",".join("?" for _ in RETRY_SIGNAL_STATUSES)
    rows = conn.execute(
        f"""SELECT id,code,account_id,intended_date,signal_date,created_at,status
              FROM paper_signals WHERE status IN ({placeholders})""",
        RETRY_SIGNAL_STATUSES,
    ).fetchall()
    for row in rows:
        summary["checked"] += 1
        signal = dict(row)
        signal_day = str(signal.get("intended_date") or signal.get("signal_date") or "")[:10]
        expired_day = bool(asof_day and signal_day and signal_day != str(asof_day)[:10])
        freshness = signal_freshness(signal, now=moment, ttl_minutes=ttl_minutes)
        if not expired_day and freshness["usable"]:
            continue
        reason = (
            f"信号属于 {signal_day}，跨交易日失效"
            if expired_day else str(freshness["reason"])
        )
        conn.execute(
            """UPDATE paper_signals SET status=?,reason=COALESCE(reason,'') || '；' || ?
                WHERE id=? AND status IN (?,?,?)""",
            (SIGNAL_EXPIRED_STATUS, reason, int(row["id"]), *RETRY_SIGNAL_STATUSES),
        )
        summary["expired"] += 1
    # 回收已失效信号名下的活动买单。
    stale = conn.execute(
        """SELECT o.id FROM paper_orders o
             JOIN paper_signals s ON s.id=o.signal_id
            WHERE o.side='buy' AND o.status IN (?,?,?,?,?)
              AND s.status IN ('expired','superseded','rejected','blocked','shadow_q3')""",
        RECOVERABLE_ORDER_STATUSES,
    ).fetchall()
    for row in stale:
        order_id = int(row["id"])
        _release_reservation(conn, order_id)
        conn.execute(
            """UPDATE paper_orders
                  SET status='superseded',reason=COALESCE(reason,'') || '；信号已失效，委托回收',
                      cancelled_at=?
                WHERE id=? AND status IN (?,?,?,?,?)""",
            (moment.isoformat(timespec="seconds"), order_id, *RECOVERABLE_ORDER_STATUSES),
        )
        summary["orders_cancelled"] += 1
    return summary
