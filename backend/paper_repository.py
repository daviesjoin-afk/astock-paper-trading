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
