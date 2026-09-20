# -*- coding: utf-8 -*-
"""replacement candidate / historical review 的**as-of 有界**只读证据层（R18）。

本模块存在的理由
----------------
R18 之前，``_best_replacement_candidate`` 这样挑今天的替补：

    next_day = _next_weekday(day).isoformat()
    SELECT ... FROM paper_signals
     WHERE account_id=? AND status IN (...)
       AND intended_date>=? AND intended_date<=?     -- today .. tomorrow

于是**明天的候选**可以成为**今天**的替补。后果不是"买单偶尔失败"，而是确定性错误：

    monitor_risk(D)
      → 选中 intended_date=D+1 的高分 signal
      → replacement_score 很高 → consolidation_exit
      → 今天真的把当前持仓卖掉
      → _rotation_buy_candidate → _buy_order(asof_day=D)
      → ELC.signal_freshness 要求 intended_date == D
      → usable=False，"信号属于 D+1，禁止使用旧信号开新仓"
      → 今天为明天候选卖了仓，明天候选还被顺手打成 expired

同时 ``_slot_upgrade_context`` 读历史 review 时只按 ``ORDER BY id DESC LIMIT 1``，
没有 ``review_date <= asof_day``，因此 ``asof=D`` 会读到 ``D+1`` 的 review —— 真正的
future leakage。

核心不变量
----------
::

    A candidate may influence a same-day sell only if
    candidate.intended_date == asof_day
    and candidate.signal_date <= asof_day.

    Slot-upgrade review evidence must be bounded by
    (cycle_id, review_date <= asof_day).

两条都是**等式 / 上界**，不是 range，也不是"取最新一行"。找不到就是 ``None`` /
空列表 —— 未知保持未知，绝不 fallback 到别的周期、别的日期。

归档表不参与
------------
R17 的 current-episode provenance 可以读 ``paper_signals_archive``，因为那是在证明
**历史** opening signal。replacement candidate 是"**现在**准备执行的新 BUY 意图"，
所以只允许 active ``paper_signals``：归档行已不是 executable candidate，不得复活。

硬边界（由 ``test_paper_trading_architecture_guard.py`` 静态强制）
------------------------------------------------------------------
* 只允许 stdlib 与调用方传入的 sqlite 连接；
* 不 import ``paper_trading``；不读网络 / 文件系统；
* **零 wall clock** —— ``asof_day`` / ``cycle_id`` 一律显式传入；
* 不做 active cycle 解析（那是调用方的既有事实）。
"""
from __future__ import annotations

import sqlite3

__all__ = [
    "REPLACEMENT_EVIDENCE_VERSION",
    "load_replacement_candidates",
    "latest_position_review",
]

REPLACEMENT_EVIDENCE_VERSION = "replacement-evidence-v1"

#: replacement candidate 的列清单。``intended_date`` 与 ``signal_date`` 都在里面，
#: 因为"属于哪个交易日"与"证据生成日"是两件必须分别证明的事。
CANDIDATE_COLUMNS = (
    "id", "account_id", "signal_date", "intended_date", "code", "name",
    "rank_score", "t_score", "payload", "status", "created_at",
)


def _normalize_day(value):
    """把日期规整成 ``YYYY-MM-DD``；空值返回 ``None``（= 不可比较 ⇒ fail closed）。"""
    text = str(value or "").strip()
    return text[:10] if text else None


def load_replacement_candidates(conn, *, account_id, asof_day, statuses):
    """读**当天**可用的 replacement 候选（只读、有界、绝不跨日）。

    查询语义（**不得**改回 range）::

        WHERE account_id=?
          AND status IN (...)
          AND intended_date = ?      -- 等于 asof_day，不是 today..next_day
          AND signal_date <= ?       -- 证据不得来自未来

    合法 overnight 计划（``signal_date = D-1``、``intended_date = D``）因此**仍然可
    用**；而"明天才打算买"（``intended_date = D+1``）与"signal 来自明天"（
    ``signal_date = D+1``）都被排除。

    返回 ``dict`` 列表（调用方需要 ``.get()``，``sqlite3.Row`` 不支持）。
    """
    if not account_id:
        return []
    asof = _normalize_day(asof_day)
    if not asof:
        return []
    active = [str(item) for item in (statuses or ()) if str(item or "").strip()]
    if not active:
        return []
    placeholders = ",".join("?" for _ in active)
    try:
        rows = conn.execute(
            f"SELECT {', '.join(CANDIDATE_COLUMNS)} FROM paper_signals"
            f" WHERE account_id=? AND status IN ({placeholders})"
            "   AND intended_date=? AND signal_date<=?"
            " ORDER BY COALESCE(t_score,0) DESC,COALESCE(rank_score,0) DESC,id DESC",
            (account_id, *active, asof, asof),
        ).fetchall()
    except sqlite3.Error:
        # 表缺失（极简夹具 / 未迁移的库）⇒ 没有可证明的候选。
        return []
    return [dict(row) for row in rows]


def latest_position_review(conn, *, cycle_id, account_id, code, asof_day):
    """读该持仓在**已认领周期**内、``review_date <= asof_day`` 的最近一次复核。

    查询语义（**不得**改回 ``ORDER BY id DESC`` 无上界）::

        WHERE cycle_id=? AND account_id=? AND code=? AND review_date<=?
        ORDER BY review_date DESC,id DESC LIMIT 1

    找不到返回 ``None``。**绝不** fallback 到别的周期 / current cycle / 全库最新
    review —— slot upgrade 的 weakest 分不能由未来或别的周期供给。
    """
    try:
        cycle = int(cycle_id)
    except (TypeError, ValueError):
        return None
    asof = _normalize_day(asof_day)
    if not asof:
        return None
    try:
        row = conn.execute(
            "SELECT score,action,review_date FROM paper_position_reviews"
            " WHERE cycle_id=? AND account_id=? AND code=? AND review_date<=?"
            " ORDER BY review_date DESC,id DESC LIMIT 1",
            (cycle, str(account_id or ""), str(code or ""), asof),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None
