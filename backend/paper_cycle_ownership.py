# -*- coding: utf-8 -*-
"""当期周期**所有权**解析的唯一真相源（read-only ownership resolver）。

本模块只回答一个问题：*给定一个账本连接，当前周期的经济所有权、执行参与者
与判定来源分别是什么*。它不拥有任何写路径。

四口径互不等价，本模块只拥有前两条：

    cycle economic ownership  !=  execution eligibility
                              !=  registry active scope
                              !=  risk-exit eligibility

锁句：**Cycle owns capital. Lifecycle controls execution permission.
Existing exposure still owns risk-exit rights.**

- **经济所有权** = ``paper_cycles.enabled_strategies``（能力位过滤，**不查**
  Registry ``lifecycle_status``）∩ ``paper_accounts.cycle_id == 目标周期``。
  lifecycle pause **不**改变这个集合——暂停一个策略不是把它从周期里删掉。
- **执行资格** = 经济所有权 − ``strategy_definitions.lifecycle_status`` 处于
  ``LIFECYCLE_PAUSED_STATUSES`` 的策略。pause 立即停止新信号 / 新委托 /
  资金预占，但**不得**解绑 ``paper_accounts.cycle_id``、清零 ``cash`` /
  ``initial_cash``、改写 ``enabled_strategies`` 或破坏历史。
- **显式 idle 周期**（``enabled_strategies == []``）是合法的零策略周期：
  执行参与者为空，**绝不**回落内置五套。缺失 / 损坏（``NULL`` 或不可解析）
  才是「未配置」，保留 legacy 回退。两者语义**不得**合并。
- **注册表 active 作用域**（``ACTIVE_ACCOUNT_IDS`` / ``_active_account_clause``）
  与**风控退出资格**（``_risk_exit_account_ids``）**不在本模块**。

注册表 active 投影（``SR.active_ids() ∩ 声明键``）按仓库不变量 #8 留在权威层
``paper_trading``，由调用方以 ``builtin_scope`` 参数**在调用时**注入，仅用于两条
legacy 回退路径（未配置周期的迁移期回退 / 无周期回退）。本模块**不**重新计算
该投影，也不 import ``paper_trading``。

依赖方向（单向，反向禁止）::

    paper_trading → paper_cycle_ownership → paper_account_specs
                                        → user_strategy_participation
                                        → json / sqlite3 / stdlib

本模块是**只读**解析器：不联网、不读行情、不下单、不撮合、不改订单状态、
不改资金、不开户、不建周期、不归档、不做风险决策、不生成信号、不做分配或
sizing；无 ``INSERT`` / ``UPDATE`` / ``DELETE``，也无模块级调用（import 期零
副作用）。
"""
from __future__ import annotations

import json
import sqlite3

import paper_account_specs as PAS
import user_strategy_participation as USP

OWNERSHIP_MODULE_VERSION = "paper-cycle-ownership-v1"

# PR-38：执行层参与者解析的权威口径（single-owner）。
#
# Registry（``strategy_definitions``）只回答"下一周期能否启用某策略"；
# 执行层（产生新信号 / 新委托 / 占用共享资金）只认周期快照：
#
#     paper_cycles.enabled_strategies  ∩  paper_accounts.cycle_id == 当期 id
#
# 否则"注册表里仍是 active"的策略会被执行层偷偷拉回一个已经把它摘掉的
# 周期，继续占用共享池资金并产生本周期不该存在的信号与委托。
# lifecycle pause（注册表 lifecycle_status='paused'）可以从执行层临时
# 禁用新信号，而不必改写周期快照、也不必把账户摘出周期（历史仍可查）。
CYCLE_PARTICIPANT_VERSION = "cycle-participant-v1"
LIFECYCLE_PAUSED_STATUSES = ("paused",)


def _rows(conn, sql, params=()):
    """把查询结果组装成普通 dict 列表，**不依赖**调用方的 ``row_factory``。

    ``row_factory`` 是连接级属性，模块无权假设，也绝不修改它——本模块会被
    来自不同来源的连接调用（``sqlite3.Row`` 或裸连接）。用
    ``cursor.description`` + ``zip`` 自行组装，对两种连接都成立。
    """
    cursor = conn.execute(sql, params)
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _row(conn, sql, params=()):
    """把单行查询结果组装成普通 dict，不依赖调用方的 ``row_factory``。"""
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description or ()]
    return dict(zip(columns, row, strict=True))


def _loads(value, default=None):
    """与 ``paper_trading._loads`` 同形的宽松 JSON 解码（缺失 → ``default``）。"""
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError):
        return default if default is not None else {}


def explicit_empty_cycle(conn, cycle_id) -> bool:
    """Whether a cycle explicitly declares ``enabled_strategies == []``.

    Missing / NULL / unparsable values are **not** idle; they retain the
    legacy fallback semantics.  Only the explicit empty list means "this
    cycle owns no strategies".
    """
    try:
        row = _row(
            conn,
            "SELECT enabled_strategies FROM paper_cycles WHERE id=?",
            (int(cycle_id),),
        )
        return bool(row) and _loads(row.get("enabled_strategies"), None) == []
    except Exception:
        return False


def lifecycle_paused_ids(conn) -> frozenset:
    """注册表中被生命周期暂停的策略 id；执行层据此临时禁用新信号。

    只读查询 ``strategy_definitions.lifecycle_status``；注册表尚未建表时返回
    空集，绝不抛异常阻断主流程。
    """
    if conn is None:
        return frozenset()
    placeholders = ",".join("?" for _ in LIFECYCLE_PAUSED_STATUSES)
    try:
        rows = conn.execute(
            f"SELECT id FROM strategy_definitions WHERE lifecycle_status IN ({placeholders})",
            LIFECYCLE_PAUSED_STATUSES,
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    return frozenset(str(row[0]) for row in rows if row[0])


def cycle_ledger_filter(conn, cycle_id, column="id", *, builtin_scope):
    """Scope a cycle to all active sleeves once the active set is complete.

    PR-48：这里是**经济所有权**口径——能力位判定用"已知用户策略"
    （``USP.user_known_ids``，**不查** lifecycle/status），因此 lifecycle
    pause 不会把策略移出账本。小账本/迁移库回退行为保留。

    ``builtin_scope`` 是注册表 active 投影（``paper_trading.ACTIVE_ACCOUNT_IDS``），
    由调用方在**调用时**注入，仅用于「未配置周期」的迁移期回退。

    Small in-memory/unit-test ledgers and pre-migration databases can contain
    only one newly introduced sleeve plus legacy IDs.  In that transitional
    shape, falling back to the cycle's own rows preserves cash reconciliation;
    a normal repository cycle has both active IDs and uses the strict filter.
    """
    configured_ids = None
    try:
        cycle_row = _row(conn, "SELECT enabled_strategies FROM paper_cycles WHERE id=?", (cycle_id,))
        parsed = _loads(cycle_row["enabled_strategies"], None) if cycle_row and cycle_row["enabled_strategies"] else None
        if isinstance(parsed, list):
            user_ids = set(USP.user_known_ids(conn))
            configured_ids = tuple(item for item in parsed if item in PAS.ACCOUNT_SPECS or item in user_ids)
    except Exception:
        configured_ids = None
    if configured_ids is not None:
        # PR-47/48：显式空启用集合 = idle 周期 → 空账本（1=0），不再回落
        # 内置五套；未配置（None）才走下面的迁移期回退。
        placeholders = ",".join("?" for _ in configured_ids)
        active_clause = f"{column} IN ({placeholders})" if configured_ids else "1=0"
        return active_clause, configured_ids
    active_ids = tuple(builtin_scope)
    placeholders = ",".join("?" for _ in active_ids)
    active_clause = f"{column} IN ({placeholders})" if active_ids else "1=0"
    count = conn.execute(
        f"SELECT COUNT(*) FROM paper_accounts WHERE cycle_id=? AND {active_clause}",
        (cycle_id, *active_ids),
    ).fetchone()[0]
    if count >= len(active_ids):
        return active_clause, active_ids
    return "1=1", ()


def cycle_ledger_rows(conn, cycle_id, *, builtin_scope):
    """Return the strategy ledgers participating in the shared capital pool.

    ``cycle_id`` **必填**：周期选择（``_active_cycle``，可能触发旧库补周期）
    属于 ``paper_trading`` 的写侧职责，不进入本只读模块。
    """
    active_clause, active_ids = cycle_ledger_filter(conn, cycle_id, "id", builtin_scope=builtin_scope)
    return _rows(
        conn,
        f"SELECT * FROM paper_accounts WHERE cycle_id=? AND {active_clause} ORDER BY id",
        (cycle_id, *active_ids),
    )


def cycle_ledger_ids(conn, cycle_id, *, builtin_scope):
    """PR-48：周期**经济所有权**账户集合（单一事实来源）。

    语义 = ``cycle.enabled_strategies``（能力位过滤，不查 Registry
    lifecycle/status）∩ ``paper_accounts.cycle_id == 该周期``。用于：
    共享现金、NAV、initial capital、configure_capital、资金对账、
    周期暂停/恢复与经济所有权——**lifecycle pause 不改变这个集合**
    （Cycle owns capital; lifecycle controls execution permission）。
    """
    return tuple(
        row["id"]
        for row in cycle_ledger_rows(conn, cycle_id, builtin_scope=builtin_scope)
    )


def cycle_participant_resolution(conn, cycle_id=None, *, builtin_scope):
    """解析当前周期权威参与者，并给出判定来源供审计。

    返回值：``{"ids", "source", "enabled", "bound", "paused", "cycle_id"}``。
    ``source`` 只有这几种：
    - ``cycle_snapshot``：启用集合 ∩ 周期挂接（权威路径）；
    - ``cycle_enabled_unbound_fallback``：周期已声明启用集合但账本尚未
      挂接（新建周期首轮 / 迁移窗口），退化为启用集合本身，避免整轮空转；
    - ``cycle_idle``：显式空启用集合 = 合法零策略 idle 周期 → 零参与者；
    - ``no_cycle`` / ``cycle_not_configured`` / ``no_conn``：沿用内置集合
      （+ 注册表参与者），与 PR-38 之前的行为一致。
    """
    fallback_ids = (
        tuple(dict.fromkeys([*builtin_scope, *USP.user_participant_ids(conn)]))
        if conn is not None else tuple(builtin_scope)
    )
    if conn is None:
        return {"ids": fallback_ids, "source": "no_conn", "enabled": (),
                "bound": frozenset(), "paused": frozenset(), "cycle_id": None,
                "version": CYCLE_PARTICIPANT_VERSION}
    try:
        if cycle_id is None:
            cycle_row = _row(
                conn,
                "SELECT id,enabled_strategies FROM paper_cycles "
                "WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1"
            )
        else:
            cycle_row = _row(
                conn, "SELECT id,enabled_strategies FROM paper_cycles WHERE id=?", (int(cycle_id),)
            )
    except sqlite3.Error:
        cycle_row = None
    if cycle_row is None:
        return {"ids": fallback_ids, "source": "no_cycle", "enabled": (),
                "bound": frozenset(), "paused": frozenset(), "cycle_id": None,
                "version": CYCLE_PARTICIPANT_VERSION}
    parsed = _loads(cycle_row["enabled_strategies"], None) if cycle_row["enabled_strategies"] else None
    if not isinstance(parsed, list):
        # 缺失/损坏 → 旧语义：回落内置五套（未配置 ≠ 零策略）。
        return {"ids": fallback_ids, "source": "cycle_not_configured", "enabled": (),
                "bound": frozenset(), "paused": frozenset(), "cycle_id": cycle_row["id"],
                "version": CYCLE_PARTICIPANT_VERSION}
    if not parsed:
        # PR-47：显式空配置 = 零策略 idle 周期——不产生新参与者，风控扫描、
        # 存量退出与系统调度照常。不再回落内置五套。
        return {"ids": (), "source": "cycle_idle", "enabled": (),
                "bound": frozenset(), "paused": frozenset(), "cycle_id": cycle_row["id"],
                "version": CYCLE_PARTICIPANT_VERSION}
    enabled = tuple(dict.fromkeys(str(item) for item in parsed))
    try:
        bound = frozenset(
            str(row[0]) for row in conn.execute(
                "SELECT id FROM paper_accounts WHERE cycle_id=?", (cycle_row["id"],),
            ).fetchall() if row[0]
        )
    except sqlite3.Error:
        bound = frozenset()
    paused = lifecycle_paused_ids(conn)
    ids = tuple(item for item in enabled if item in bound and item not in paused)
    source = "cycle_snapshot"
    if not ids:
        # 周期已声明启用集合但账本尚未挂接：退化为启用集合本身，
        # 被 lifecycle pause 的 id 仍然不参与执行。
        ids = tuple(item for item in enabled if item not in paused)
        source = "cycle_enabled_unbound_fallback"
    return {
        "ids": ids, "source": source, "enabled": enabled, "bound": bound,
        "paused": paused, "cycle_id": cycle_row["id"],
        "version": CYCLE_PARTICIPANT_VERSION,
    }


def current_cycle_participant_ids(conn, cycle_id=None, *, builtin_scope):
    """当前周期权威参与者 id（PR-38 单一事实来源）。

    权威条件 = ``paper_cycles.enabled_strategies`` ∩
    ``paper_accounts.cycle_id == 当前周期 id``；被 lifecycle pause 的策略
    临时退出执行层（仍保留在周期内，历史可查）。Registry active 只用于
    创建下一周期，不再作为执行层依据。
    """
    return cycle_participant_resolution(conn, cycle_id, builtin_scope=builtin_scope)["ids"]


def execution_participant_ids(conn, cycle_id=None, *, builtin_scope):
    """PR-48：周期**执行资格**账户集合。

    语义 = ``cycle_ledger_ids`` ∩ 当前可执行 lifecycle（被 lifecycle
    pause 的策略临时退出执行层，但保留在经济账本里）。用于：候选、
    新信号、新开仓、资金预占与新持仓。Risk Exit 另见
    ``paper_trading._risk_exit_account_ids``（执行参与者 ∪ 仍有剩余 lots
    的账户）——本模块**不**拥有风控退出资格。
    """
    return current_cycle_participant_ids(conn, cycle_id, builtin_scope=builtin_scope)
