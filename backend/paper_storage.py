# -*- coding: utf-8 -*-
"""SQLite 存储边界。

这里仅负责连接生命周期和通用事务重试，不包含任何交易规则或业务查询。
通过显式传入数据库路径，测试和回放可以使用临时数据库；上层引擎继续
保留兼容包装，避免改变既有调用方。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager


@contextmanager
def db(db_path, immediate=False):
    """获取数据库连接并保证事务在关闭前明确提交或回滚。"""
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        # 异常退出：回滚本事务块内尚未提交的写入，避免脏数据残留。
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    else:
        # 正常退出：提交本事务块内的全部写入。
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                # 锁冲突：写入仍在事务里，必须原样重试；二次失败先回滚，
                # 不能让 close() 对半提交状态做隐式处理。
                time.sleep(0.2)
                try:
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                # 非锁类提交失败：显式回滚，保证连接关闭前状态确定。
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
    finally:
        conn.close()


def wal_checkpoint(db_path):
    """显式收缩 WAL 日志，失败时降级为 PASSIVE 尽力处理。"""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
    except Exception:
        pass


def execute_with_retry(conn, sql, params=(), max_retries=3):
    """执行 SQL，并在数据库锁定时按递增间隔重试。"""
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def executemany_with_retry(conn, sql, params_list, max_retries=3):
    """批量执行 SQL，并在数据库锁定时按递增间隔重试。"""
    for attempt in range(max_retries):
        try:
            return conn.executemany(sql, params_list)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def commit_with_retry(conn, max_retries=3):
    """提交事务，并在数据库锁定时按递增间隔重试。"""
    for attempt in range(max_retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


@contextmanager
def db_readonly(db_path):
    """打开不执行迁移、不获取写锁的只读 ledger 连接。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    try:
        yield conn
    finally:
        conn.close()
