# -*- coding: utf-8 -*-
"""当前持仓的**唯一**权威只读读取器（Round-10）。

不变量::

    paper_position_lots is the current executable position authority.
    paper_position_risk_state is the runtime peak / take-stage authority.
    paper_positions is a compatibility projection with zero execution authority.

本模块存在的理由：在 PR #168 之前，多个模块各自写一份
``SELECT ... FROM paper_positions WHERE qty>0`` 并把它解释成「现在持有什么」。
``paper_positions`` 只是 ``paper_position_lots`` 的兼容投影 —— 它没有
``cycle_id``、没有 ``source_order_id``、没有已验证取得证据，因此**无法证明**
某行属于当前周期。一个旧周期的残留镜像行会被这些消费者当成当前持仓，进而影响
候选排除、holding 分类、影子组合输入。

所以「现在持有什么」只能有一个实现，且必须满足：

1. 只读 —— 不写任何表；
2. 只取当前 active cycle；
3. active cycle 查询本身只读（**不调用** ``_active_cycle``，它会 ``_ensure_cycle``
   并 INSERT 一个周期 —— 那等于用一次写操作回答「现在属于哪个周期」）；
4. 无 active cycle ⇒ 返回空（fail closed），绝不回落 ``paper_positions``；
5. 数量来自 ``paper_position_lots.remaining_qty``；
6. 绝不从 ``paper_positions`` 创造持仓。

依赖刻意保持最小（``execution_verification`` + ``paper_portfolio`` 都是零依赖模块），
这样 ``paper_trading``、``news_learning``、``adaptive_engine``、``rebalance_scanner``
可以共用同一实现，而**不会**形成循环导入。
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import execution_verification as EV
import paper_portfolio as PP

POSITION_READ_MODEL_VERSION = "position-read-model-v1"

#: 允许从同周期 ``paper_position_risk_state`` 读取的风险状态列 —— 这是
#: peak_price（移动止损）与 take_stage（阶梯止盈）的**唯一**权威来源。
#: ``paper_positions`` 投影不再向任何执行判定供给这两个字段（R14）。
RISK_STATE_METADATA_COLUMNS = ("peak_price", "take_stage")


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _date(value=None):
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return dt.date.today()


def active_cycle_id(conn) -> int | None:
    """**只读**读取当前 active cycle id；不存在或读不到就返回 ``None``。

    与 ``paper_trading._active_cycle`` 的关键区别：后者在没有周期时会
    ``_ensure_cycle`` 插入一个新周期。「现在持有什么」是纯查询问题 —— 如果因为
    库里没有周期就顺手造一个，答案就从「没有周期」变成了「我刚造的那个周期」。
    ``paper_cycles`` 表不存在（极简 schema / 测试库）同样返回 ``None``：读不到
    周期事实就是「无法证明」，而不是让底层 ``OperationalError`` 冒出去。
    """
    try:
        row = conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row["id"] if hasattr(row, "keys") else row[0])
    except (TypeError, ValueError, IndexError):
        return None


def _verified_cash_flows(conn) -> dict:
    """已验证成交的现金流，供展示成本（摊薄成本）使用。

    只覆盖 ``execution_verified=1 AND execution_status='verified'`` 的行；历史
    NULL 行被排除 ⇒ 缺失表达「未知」，而不是「有定义的 0」。
    """
    try:
        rows = conn.execute(
            "SELECT account_id,code,"
            "       SUM(CASE WHEN side='buy' THEN COALESCE(amount,0)+COALESCE(fees,0) ELSE 0 END) AS buy_cash,"
            "       SUM(CASE WHEN side='sell' THEN COALESCE(amount,0)-COALESCE(fees,0) ELSE 0 END) AS sell_cash"
            "  FROM paper_orders WHERE status='filled' AND " + EV.VERIFIED_PREDICATE +
            " GROUP BY account_id,code"
        ).fetchall()
    except sqlite3.Error:
        return {}
    # 转 dict：``paper_portfolio`` 用 ``flow.get(...)`` 读字段，``sqlite3.Row``
    # 不支持 ``.get``。
    return {(row["account_id"], row["code"]): dict(row) for row in rows}


def _dicts(rows):
    """把 ``sqlite3.Row`` 转成 dict。

    ``paper_portfolio.aggregate_positions`` 用 ``.get()`` 读可选列，而
    ``sqlite3.Row`` 只有下标访问 —— 上游 ``paper_trading._rows`` 一直做这层
    转换，新模块必须保持同一形状，否则聚合层会在 ``lot.get("name")`` 上崩掉。
    """
    return [dict(row) for row in rows]


def current_positions(conn, *, account_id=None, asof_day=None):
    """当前 active cycle 的权威持仓（只读）。

    返回与旧 ``_position_rows`` 相同形状的字典列表，因此调用方可以就地替换，
    不需要改自己的下游逻辑。``qty`` / ``cost`` / ``entry_date`` / ``available_qty``
    / ``locked_qty`` 全部由 lot 派生；``peak_price`` / ``take_stage`` **只**来自
    同周期的 ``paper_position_risk_state``（R14 起执行权威），并且**仅对已有
    权威 lot 的 account/code**。

    没有风险状态行的持仓（升级前遗留 / 无 episode 事实）得到显式 fail-safe
    默认：peak 锚定成本、``take_stage=None``（未知）—— 绝不回落
    ``paper_positions`` 投影，"未知"不能升级成"已知"。

    没有 active cycle 时返回 ``[]`` —— 不建周期、不回落投影。
    本函数是**纯读**：不 INSERT/UPDATE 风险状态、不创建周期、不修投影。
    初始化只发生在 verified execution lifecycle（``paper_trading._record_lot``）。
    """
    cycle_id = active_cycle_id(conn)
    if cycle_id is None:
        return []
    day = _date(asof_day).isoformat()
    sql = "SELECT * FROM paper_position_lots WHERE cycle_id=? AND remaining_qty>0"
    params = [cycle_id]
    if account_id:
        sql += " AND account_id=?"
        params.append(account_id)
    lots = _dicts(conn.execute(sql, tuple(params)).fetchall())

    try:
        state_rows = _dicts(conn.execute(
            "SELECT * FROM paper_position_risk_state WHERE cycle_id=?",
            (cycle_id,)).fetchall())
    except sqlite3.Error:
        # 表不存在（极简 schema / 未迁移的库）⇒ 没有可证明的风险状态。
        # 绝不回落 paper_positions 投影 —— 那正是本 PR 要消灭的执行权威泄漏。
        state_rows = []

    return PP.aggregate_positions(
        lots, state_rows, _verified_cash_flows(conn), day, num=_num)


def current_holding_keys(conn, *, account_id=None) -> set:
    """当前 active cycle 里**确实持有**的 ``(account_id, code)`` 集合。

    供候选排除、holding 分类等「是否持有」判断使用。只读、cycle-scoped。
    """
    cycle_id = active_cycle_id(conn)
    if cycle_id is None:
        return set()
    sql = ("SELECT DISTINCT account_id,code FROM paper_position_lots"
           " WHERE cycle_id=? AND remaining_qty>0")
    params = [cycle_id]
    if account_id:
        sql += " AND account_id=?"
        params.append(account_id)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return set()
    return {(str(row["account_id"]), str(row["code"])) for row in rows}


def current_held_codes(conn, *, account_id=None) -> set:
    """当前 active cycle 里确实持有的代码集合（只读、cycle-scoped）。"""
    return {code for _account, code in current_holding_keys(conn, account_id=account_id)}


def current_holding_rows(conn, *, account_id=None):
    """当前 active cycle 确实持有的持仓行（只读、cycle-scoped）。

    返回 ``account_id`` / ``code`` / ``name`` / ``industry``，元数据取自
    ``paper_position_lots`` 自身（该表就有 name/industry 列）—— 不需要、也
    不应该回落到 ``paper_positions`` 投影去补展示字段。
    """
    cycle_id = active_cycle_id(conn)
    if cycle_id is None:
        return []
    sql = ("SELECT account_id,code,MAX(name) AS name,MAX(industry) AS industry"
           "  FROM paper_position_lots WHERE cycle_id=? AND remaining_qty>0")
    params = [cycle_id]
    if account_id:
        sql += " AND account_id=?"
        params.append(account_id)
    sql += " GROUP BY account_id,code ORDER BY account_id,code"
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def connect_readonly(path, timeout=20):
    """以只读方式打开账本；调用方负责关闭。

    ``mode=ro`` 保证即使调用方误写也写不进去 —— 当前持仓读取不应有任何写权限。
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn
