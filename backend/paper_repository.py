# -*- coding: utf-8 -*-
"""Paper ledger 的最小仓储接口。

仓储（repository）是隔离数据库读写的薄接口。本阶段只统一通用行读取和
审计写入；具体业务 SQL 仍由上层编排，便于后续按账户、订单和成交逐步迁移。
"""
from __future__ import annotations


def rows(conn, sql, params=()):
    """执行查询并把 sqlite Row 转成普通字典，保持旧返回形状。"""
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def audit(conn, account_id, event, detail, created_at):
    """写入一条结构化审计事件。"""
    conn.execute(
        "INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
        (account_id, event, detail, created_at),
    )


def account_metric_inputs(conn, account_ids, today):
    """批量读取 dashboard 账户卡片所需的窄账本投影。

    这里只负责 SQL 读取和按账户分组，不计算 NAV 或收益率；这样上层
    dashboard 可以复用同一份卖出、成交计数和 NAV 历史，避免逐账户重复扫表。
    """
    ids = [str(account_id) for account_id in account_ids if account_id]
    empty = {
        "latest_nav": {}, "navs": {}, "previous_nav": {}, "sells": {},
        "buy_count": {}, "rejected": {},
    }
    if not ids:
        return empty
    placeholders = ",".join("?" for _ in ids)
    order_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
    realized_field = "realized_pnl" if "realized_pnl" in order_columns else "NULL AS realized_pnl"
    executed_field = "executed_at" if "executed_at" in order_columns else "NULL AS executed_at"
    sell_fields = (
        f"id,account_id,code,qty,filled_price,amount,fees,status,{realized_field},"
        f"created_at,{executed_field}"
    )
    sell_rows = rows(
        conn,
        f"SELECT {sell_fields} FROM paper_orders "
        f"WHERE account_id IN ({placeholders}) AND side='sell' AND status='filled'",
        tuple(ids),
    )
    sells = {account_id: [] for account_id in ids}
    for row in sell_rows:
        sells.setdefault(str(row.get("account_id") or ""), []).append(row)
    buy_rows = rows(
        conn,
        f"SELECT account_id,COUNT(*) AS count FROM paper_fills "
        f"WHERE account_id IN ({placeholders}) AND side='buy' GROUP BY account_id",
        tuple(ids),
    )
    buy_count = {str(row["account_id"]): int(row["count"] or 0) for row in buy_rows}
    rejected_rows = rows(
        conn,
        f"SELECT account_id,COUNT(*) AS count FROM paper_orders "
        f"WHERE account_id IN ({placeholders}) AND status!='filled' GROUP BY account_id",
        tuple(ids),
    )
    rejected = {str(row["account_id"]): int(row["count"] or 0) for row in rejected_rows}
    nav_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_nav)").fetchall()}
    quote_status_field = ",quote_status" if "quote_status" in nav_columns else ""
    nav_rows = rows(
        conn,
        f"SELECT account_id,nav_date,nav,benchmark{quote_status_field},created_at "
        f"FROM paper_nav WHERE account_id IN ({placeholders}) ORDER BY account_id,nav_date",
        tuple(ids),
    )
    navs = {account_id: [] for account_id in ids}
    latest_nav = {}
    previous_nav = {}
    today_key = str(today)[:10]
    for row in nav_rows:
        account_id = str(row.get("account_id") or "")
        navs.setdefault(account_id, []).append(row.get("nav"))
        latest_nav[account_id] = row
        if str(row.get("nav_date") or "") < today_key:
            previous_nav[account_id] = row
    return {
        "latest_nav": latest_nav, "navs": navs, "previous_nav": previous_nav,
        "sells": sells, "buy_count": buy_count, "rejected": rejected,
    }
