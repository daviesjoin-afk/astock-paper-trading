# -*- coding: utf-8 -*-
"""当前 position episode 的**入场信号 provenance** 只读解析器（R17）。

不变量::

    The entry-model evidence of a position belongs to exactly one episode,
    and that episode's origin is its verified opening BUY order.

本模块存在的理由
----------------
在 R17 之前，``_position_quality_score`` 这样决定持仓的"原始模型分"：

    SELECT rank_score,t_score,payload FROM paper_signals
     WHERE account_id=? AND code=? ORDER BY signal_date DESC,id DESC LIMIT 1

这个查询回答的是「这个账户+股票**最近一次出现的** signal 是什么」，而不是
「当前这笔持仓 episode 是**由哪个 signal 建出来的**」。它既没有 episode 归属，
也没有 ``signal_date <= asof_day`` 上界，于是：

* 后来一条**与本持仓无关**的 signal（同账户同代码）会把当前 episode 的
  ``model_score`` 顶掉 —— 一个真实影响 ``_concentration_action`` 的自动换仓判定；
* 历史回放 ``asof_day=D`` 时能读到 ``D`` 之后的 signal —— 明确的 future leakage。

权威链（不得绕过）
------------------
::

    paper_position_risk_state.opened_order_id      （#170 建立的 episode 归属）
      → paper_orders.id                             （必须 same cycle / verified / filled / buy）
        → paper_orders.signal_id                    （建仓时冻结的 signal 身份）
          → paper_signals.id                        （精确 lookup，不是 latest）

任何一环不成立 ⇒ ``status="unknown"``。**绝不** fallback 到
「account+code 的最近一条 signal」：unknown 不等于 latest guess。

``paper_signals_archive`` 也算同一环
-----------------------------------
分批建仓（sliced entry）在**首片成交之后**会把 signal 留在
``deferred_capacity``（``_buy_order`` 的后半段），而这条 signal 已经建出了真实
持仓。``_cleanup_stale_data`` 24 小时后会把 ``deferred_capacity`` 的 signal
整体搬进 ``paper_signals_archive`` 并从 ``paper_signals`` 删除 —— 行身份
（``id``）被原样保留，订单链完好无损。因此精确 id 查找必须覆盖这两张表，
否则一条**可证明**的 episode 会因为归档被误判成 unknown、真实模型分退化成
中性 50。归档查找同样只按 ``id`` 精确命中，并保留全部身份与 asof 校验 ——
这不是 latest 搜索，而是同一行的持久化副本。

硬边界（由 ``test_paper_trading_architecture_guard.py`` 静态强制）
------------------------------------------------------------------
* 只允许依赖 ``execution_verification`` 与 stdlib；
* 不 import ``paper_trading``（依赖方向单向）；
* 零网络 / 零文件系统 / 零 wall clock —— ``cycle_id`` 与 ``asof_day`` 一律由
  调用方显式传入；
* **绝不** 出现 ``ORDER BY signal_date DESC`` 式的 latest 搜索。
"""
from __future__ import annotations

import sqlite3

import execution_verification as EV

__all__ = [
    "ENTRY_PROVENANCE_VERSION",
    "resolve_entry_signal",
]

ENTRY_PROVENANCE_VERSION = "entry-provenance-v1"

#: 未能证明 provenance 时的状态码（都是显式的"不知道"，不是错误）。
UNKNOWN_REASONS = (
    "missing_opened_order_id",
    "order_not_found",
    "order_cycle_mismatch",
    "order_identity_mismatch",
    "order_not_buy",
    "order_not_filled",
    "order_unverified",
    "missing_signal_id",
    "signal_not_found",
    "signal_identity_mismatch",
    "signal_after_asof",
)


#: 精确 id 命中 signal 的**行来源**。活跃表优先；归档表是同一行被
#: ``_cleanup_stale_data`` 搬走后的持久化副本（``id`` 原样保留）。
SIGNAL_SOURCES = ("paper_signals", "paper_signals_archive")

#: 两张表列一致（``INSERT ... SELECT *`` 搬运），因此列清单只写一次。
_SIGNAL_COLUMNS = "id,account_id,code,signal_date,rank_score,t_score,payload"


def _unknown(reason, **extra):
    payload = {
        "status": "unknown",
        "reason": reason,
        "opened_order_id": None,
        "signal_id": None,
        "signal_date": None,
        "signal": None,
        "provenance_version": ENTRY_PROVENANCE_VERSION,
    }
    payload.update(extra)
    return payload


def _row_value(row, key):
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _normalize_day(value):
    """把 asof 规整成 ``YYYY-MM-DD``；空值返回 ``None``（= 不可比较）。"""
    text = str(value or "").strip()
    return text[:10] if text else None


def _load_signal(conn, signal_id):
    """按**精确 id**读 signal 行：活跃表 → 归档表。

    返回 ``(row, table_name)``，都没命中时 ``(None, None)``。归档表是
    ``_cleanup_stale_data`` 对同一行的搬运（``id`` 不变），所以这里仍然是精确
    provenance，而不是"找不到就去搜一条最新的"。表缺失（极简夹具 / 老库）与
    查不到一律视为"没有这一行"。
    """
    for table in SIGNAL_SOURCES:
        try:
            row = conn.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM {table} WHERE id=?",
                (signal_id,),
            ).fetchone()
        except sqlite3.Error:
            continue
        if row is not None:
            return row, table
    return None, None


def resolve_entry_signal(conn, *, cycle_id, account_id, code, opened_order_id, asof_day):
    """解析当前 episode 的入场 signal（只读、精确、绝不 fallback）。

    返回::

        {"status": "verified", "opened_order_id": 101, "signal_id": 55,
         "signal_date": "2026-09-10", "signal_source": "paper_signals",
         "signal": {...}, "provenance_version": ...}

    或::

        {"status": "unknown", "reason": "<UNKNOWN_REASONS 之一>", ...}

    调用方必须把 ``unknown`` 当作"不可证明"，使用中性回退（``model_score=50``），
    **不得**据此再去找一条最新 signal。
    """
    if cycle_id is None:
        raise ValueError("resolve_entry_signal 需要显式 cycle_id")
    if asof_day is None:
        raise ValueError("resolve_entry_signal 需要显式 asof_day")
    try:
        cycle_id = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"resolve_entry_signal 的 cycle_id 非法: {cycle_id!r}") from exc
    expected_account = str(account_id or "")
    expected_code = str(code or "")
    asof = _normalize_day(asof_day)

    if opened_order_id is None or str(opened_order_id).strip() == "":
        return _unknown("missing_opened_order_id")
    try:
        order_id = int(opened_order_id)
    except (TypeError, ValueError):
        return _unknown("missing_opened_order_id")

    try:
        order = conn.execute(
            "SELECT id,cycle_id,account_id,code,side,status,signal_id,"
            "       execution_verified,execution_status "
            "  FROM paper_orders WHERE id=?",
            (order_id,),
        ).fetchone()
    except sqlite3.Error:
        return _unknown("order_not_found")
    if order is None:
        return _unknown("order_not_found", opened_order_id=order_id)

    # 周期归属：订单必须属于**已认领的**那个周期，不能是别的周期留下的同名 order。
    order_cycle = _row_value(order, "cycle_id")
    if order_cycle is None or int(order_cycle) != cycle_id:
        return _unknown("order_cycle_mismatch", opened_order_id=order_id,
                        order_cycle_id=order_cycle)
    # 身份：账户与代码都必须与当前持仓一致。
    if (str(_row_value(order, "account_id") or "") != expected_account
            or str(_row_value(order, "code") or "") != expected_code):
        return _unknown("order_identity_mismatch", opened_order_id=order_id)
    if str(_row_value(order, "side") or "") != "buy":
        return _unknown("order_not_buy", opened_order_id=order_id)
    if str(_row_value(order, "status") or "") != "filled":
        return _unknown("order_not_filled", opened_order_id=order_id)
    # 复用仓库唯一的"已证明成交"判据，不另发明一套 verified 定义。
    if not EV.is_verified_row(order):
        return _unknown("order_unverified", opened_order_id=order_id)

    signal_id = _row_value(order, "signal_id")
    if signal_id is None or str(signal_id).strip() == "":
        return _unknown("missing_signal_id", opened_order_id=order_id)
    try:
        signal_id = int(signal_id)
    except (TypeError, ValueError):
        return _unknown("missing_signal_id", opened_order_id=order_id)

    signal, signal_source = _load_signal(conn, signal_id)
    if signal is None:
        return _unknown("signal_not_found", opened_order_id=order_id, signal_id=signal_id)

    if (str(_row_value(signal, "account_id") or "") != expected_account
            or str(_row_value(signal, "code") or "") != expected_code):
        return _unknown("signal_identity_mismatch", opened_order_id=order_id,
                        signal_id=signal_id)

    signal_date = _normalize_day(_row_value(signal, "signal_date"))
    if not signal_date or not asof or signal_date > asof:
        return _unknown("signal_after_asof", opened_order_id=order_id,
                        signal_id=signal_id, signal_date=signal_date)

    return {
        "status": "verified",
        "reason": None,
        "opened_order_id": order_id,
        "signal_id": signal_id,
        "signal_date": signal_date,
        "signal_source": signal_source,
        "signal": {
            "id": signal_id,
            "account_id": _row_value(signal, "account_id"),
            "code": _row_value(signal, "code"),
            "signal_date": signal_date,
            "rank_score": _row_value(signal, "rank_score"),
            "t_score": _row_value(signal, "t_score"),
            "payload": _row_value(signal, "payload"),
        },
        "provenance_version": ENTRY_PROVENANCE_VERSION,
    }
