#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量版本化数据库迁移器（schema_version 表 + 顺序迁移脚本）。

设计原则：
- 每个数据库维护一张 schema_version 表（db_name, version, applied_at, description）
- 迁移脚本按版本号顺序执行，已应用版本跳过
- 每个迁移在事务中执行，失败回滚
- 零第三方依赖，纯 sqlite3

用法：python db_migrate.py [--db paper_trading|adaptive_learning|all]
"""
import os
import sqlite3
import sys

import paper_schema_migrations as paper_schema

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data_cache")

DB_PATHS = {
    "paper_trading": os.path.join(CACHE_DIR, "paper_trading.sqlite3"),
    "adaptive_learning": os.path.join(CACHE_DIR, "adaptive_learning.sqlite3"),
}

# 迁移注册表：db_name -> [(version, description, sql_or_callable), ...]
MIGRATIONS = {
    "paper_trading": [
        (1, "创建 schema_version 表", """
        CREATE TABLE IF NOT EXISTS schema_version(
            db_name TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            description TEXT
        );
        """),
        (2, "补齐订单、持仓和账户兼容字段", paper_schema.ensure_paper_columns),
        (3, "补齐运行时租约与 fencing 字段", paper_schema.ensure_runtime_lease_columns),
        (4, "补齐点火影子表与索引", paper_schema.ensure_ignition_shadow_table),
    ],
    "adaptive_learning": [
        (1, "创建 schema_version 表", """
        CREATE TABLE IF NOT EXISTS schema_version(
            db_name TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            description TEXT
        );
        """),
    ],
}


def _current_version(conn, db_name):
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE db_name=?", (db_name,)
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _run_operation(conn, operation):
    if callable(operation):
        result = operation(conn)
        if result is False:
            raise RuntimeError("迁移操作未完成")
        return
    statement = []
    for char in operation:
        statement.append(char)
        if char == ";" and sqlite3.complete_statement("".join(statement)):
            sql = "".join(statement).strip()
            if sql:
                conn.execute(sql)
            statement = []
    sql = "".join(statement).strip()
    if sql and not sql.startswith("--"):
        conn.execute(sql)


def migrate(db_name, apply=True, path=None):
    if db_name not in DB_PATHS:
        print(f"未知数据库: {db_name}")
        return
    path = path or DB_PATHS[db_name]
    if not os.path.exists(path):
        print(f"数据库不存在（跳过）: {path}")
        return
    conn = sqlite3.connect(path, timeout=30)
    try:
        current = _current_version(conn, db_name)
        pending = [m for m in MIGRATIONS.get(db_name, []) if m[0] > current]
        if not pending:
            print(f"[{db_name}] schema 已是最新 (v{current})")
            return
        for version, desc, sql in sorted(pending, key=lambda x: x[0]):
            if not apply:
                print(f"[{db_name}] 待应用 v{version}: {desc}")
                continue
            try:
                conn.execute("BEGIN")
                _run_operation(conn, sql)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version(db_name, version, applied_at, description) VALUES(?,?,datetime('now'),?)",
                    (db_name, version, desc),
                )
                conn.commit()
                print(f"[{db_name}] ✓ 已应用 v{version}: {desc}")
            except Exception as exc:
                conn.rollback()
                print(f"[{db_name}] ✗ v{version} 失败: {exc}（事务回滚）")
                raise
    finally:
        conn.close()


def main():
    targets = sys.argv[1:] or ["all"]
    apply = "--dry-run" not in sys.argv
    for target in targets:
        if target == "--dry-run":
            continue
        if target == "all":
            for db_name in DB_PATHS:
                migrate(db_name, apply=apply)
        else:
            migrate(target, apply=apply)


if __name__ == "__main__":
    main()
