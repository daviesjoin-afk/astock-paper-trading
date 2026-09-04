# -*- coding: utf-8 -*-
"""Paper ledger schema compatibility migrations.

所有增量 schema 变更集中在这里，并保持幂等（重复执行不会改变结果）。
调用方负责事务边界；本模块不导入交易引擎，也不执行行情或订单逻辑。
"""
from __future__ import annotations

import sqlite3


def table_columns(conn, table):
    """返回表的列名；表不存在时返回空集合。"""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def ensure_columns(conn, table, definitions):
    """按定义补齐缺失列，返回实际新增的列名。"""
    columns = table_columns(conn, table)
    added = []
    for column, definition in definitions.items():
        if column not in columns and columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(column)
    return tuple(added)


def ensure_paper_columns(conn):
    """补齐旧 paper ledger 的订单、lot、持仓和账户字段。"""
    changes = {}
    changes["paper_orders"] = ensure_columns(
        conn,
        "paper_orders",
        {
            "realized_pnl": "REAL",
            "order_type": "TEXT NOT NULL DEFAULT 'market'",
            "origin": "TEXT NOT NULL DEFAULT 'strategy'",
            "expires_at": "TEXT",
            "cancelled_at": "TEXT",
        },
    )
    changes["paper_position_lots"] = ensure_columns(
        conn,
        "paper_position_lots",
        {"cost_fee_included": "INTEGER NOT NULL DEFAULT 0"},
    )
    changes["paper_positions"] = ensure_columns(
        conn,
        "paper_positions",
        {"asset_type": "TEXT NOT NULL DEFAULT 'stock_t1'"},
    )
    changes["paper_accounts"] = ensure_columns(
        conn,
        "paper_accounts",
        {
            "cycle_id": "INTEGER",
            "mode": "TEXT NOT NULL DEFAULT 'swing'",
            "style": "TEXT NOT NULL DEFAULT 'pullback'",
            "risk_profile": "TEXT NOT NULL DEFAULT 'aggressive'",
            "params": "TEXT NOT NULL DEFAULT '{}'",
            "daily_start_nav": "REAL",
            "daily_nav_date": "TEXT",
            "cooldown_until": "TEXT",
        },
    )
    return changes


def ensure_runtime_lease_columns(conn):
    """补齐调度租约/fencing 字段，并规范旧时间分隔符。"""
    migrations = {
        "paper_jobs": {
            "owner_key": "TEXT",
            "heartbeat_at": "TEXT",
            "expires_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_job_runs": {
            "owner_key": "TEXT",
            "heartbeat_at": "TEXT",
            "expires_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_runtime_locks": {
            "heartbeat_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_nav": {"quote_status": "TEXT NOT NULL DEFAULT 'verified'"},
    }
    changes = {}
    for table, definitions in migrations.items():
        changes[table] = ensure_columns(conn, table, definitions)
    # Older runner builds used an ISO ``T`` separator while the rest of the
    # ledger used a space. Normalize once so expiry comparisons stay correct.
    for table in ("paper_jobs", "paper_job_runs", "paper_runtime_locks"):
        columns = table_columns(conn, table)
        for column in ("started_at", "acquired_at", "heartbeat_at", "expires_at"):
            if column not in columns:
                continue
            conn.execute(
                f"UPDATE {table} SET {column}=replace({column},'T',' ') "
                f"WHERE {column} IS NOT NULL AND instr({column},'T')>0"
            )
    return changes


def ensure_ignition_shadow_table(conn):
    """补齐点火影子表及其索引；影子表缺失不应阻断主交易链路。"""
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_ignition_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                bucket TEXT NOT NULL,
                code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                price REAL,
                pct REAL,
                runup REAL,
                old_rule_passed INTEGER NOT NULL DEFAULT 0,
                old_rule_reason TEXT,
                ignition_passed INTEGER NOT NULL DEFAULT 0,
                ignition_reasons TEXT,
                price_30m REAL,
                at_30m TEXT,
                price_60m REAL,
                at_60m TEXT,
                resolved INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_ignition_shadow_unique
                ON paper_ignition_shadow(day, bucket, code)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_paper_ignition_shadow_recent
                ON paper_ignition_shadow(day, resolved)"""
        )
        return True
    except Exception:
        return False
