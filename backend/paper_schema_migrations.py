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
            "retry_of_order_id": "INTEGER",
        },
    )
    # 归档表列集必须与活跃表一致（retention 用 SELECT * 整行拷贝）。
    changes["paper_orders_archive"] = ensure_columns(
        conn,
        "paper_orders_archive",
        {"retry_of_order_id": "INTEGER"},
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


def ensure_order_lineage_column(conn):
    """PR-29：订单重试血缘列 retry_of_order_id（活跃表 + 归档表，幂等）。"""
    definitions = {"retry_of_order_id": "INTEGER"}
    changes = {}
    for table in ("paper_orders", "paper_orders_archive"):
        changes[table] = ensure_columns(conn, table, definitions)
    return changes


def ensure_strategy_reference_columns(conn):
    """Append immutable strategy-version stamps to execution evidence tables.

    Live and archive tables intentionally receive the columns in the same
    order because retention still uses ``INSERT ... SELECT *``.
    Historical rows remain NULL and resolve through the small immutable legacy
    binding table; this avoids rewriting a multi-gigabyte ledger.
    """
    definitions = {
        "strategy_id": "TEXT",
        "strategy_version": "INTEGER",
        "strategy_checksum": "TEXT",
    }
    changes = {}
    for table in (
        "paper_signals", "paper_signals_archive",
        "paper_orders", "paper_orders_archive",
        "paper_risk_decisions", "paper_audit",
    ):
        changes[table] = ensure_columns(conn, table, definitions)
    _ensure_strategy_reference_guards(conn)
    return changes


def _ensure_strategy_reference_guards(conn):
    """Reject incomplete, forged, or later-mutated evidence stamps.

    Historical NULL rows are intentionally left untouched. The INSERT guards
    apply only to new account-scoped live evidence; archives accept legacy NULL
    rows copied by retention while preserving any complete stamps verbatim.
    """
    for table in (
        "paper_signals", "paper_orders", "paper_risk_decisions", "paper_audit",
    ):
        if not {"account_id", "strategy_id", "strategy_version", "strategy_checksum"}.issubset(
            table_columns(conn, table)
        ):
            continue
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_strategy_stamp_insert
                BEFORE INSERT ON {table}
                WHEN NEW.account_id IS NOT NULL AND (
                    NEW.strategy_id IS NULL OR NEW.strategy_version IS NULL
                    OR NEW.strategy_checksum IS NULL
                    OR NEW.strategy_id <> NEW.account_id
                    OR NOT EXISTS (
                        SELECT 1 FROM paper_strategy_versions v
                        WHERE v.strategy_id=NEW.strategy_id
                          AND v.version=NEW.strategy_version
                          AND v.checksum=NEW.strategy_checksum
                    )
                )
                BEGIN SELECT RAISE(ABORT, 'invalid strategy version stamp'); END"""
        )
    for table in (
        "paper_signals", "paper_signals_archive", "paper_orders",
        "paper_orders_archive", "paper_risk_decisions", "paper_audit",
    ):
        if not {"strategy_id", "strategy_version", "strategy_checksum"}.issubset(
            table_columns(conn, table)
        ):
            continue
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_strategy_stamp_immutable
                BEFORE UPDATE OF strategy_id,strategy_version,strategy_checksum ON {table}
                WHEN NEW.strategy_id IS NOT OLD.strategy_id
                  OR NEW.strategy_version IS NOT OLD.strategy_version
                  OR NEW.strategy_checksum IS NOT OLD.strategy_checksum
                BEGIN SELECT RAISE(ABORT, 'strategy version stamp is immutable'); END"""
        )


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


def ensure_proposal_lifecycle_columns(conn):
    """PR-33：风险放大提案的生命周期列（resolved_at/resolved_by/note，幂等）。"""
    import asymmetric_risk as AR

    try:
        AR.ensure_proposals_table(conn)
        AR.ensure_proposal_lifecycle_columns(conn)
        return True
    except Exception:
        return False
