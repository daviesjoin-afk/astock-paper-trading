# -*- coding: utf-8 -*-
"""SQLite 存储边界。

这里仅负责连接生命周期和通用事务重试，不包含任何交易规则或业务查询。
通过显式传入数据库路径，测试和回放可以使用临时数据库；上层引擎继续
保留兼容包装，避免改变既有调用方。
"""
from __future__ import annotations

import contextvars
import random
import sqlite3
import time
from contextlib import contextmanager

import data_paths
import paper_timing as PTM


def execute_script(conn, script):
    """Execute SQL statements without SQLite executescript's implicit commit.

    The caller owns the transaction. This is the existing migration runner's
    statement parser, shared by schema owners so trigger bodies stay intact.
    """
    statement = []
    for char in script:
        statement.append(char)
        if char == ";" and sqlite3.complete_statement("".join(statement)):
            sql = "".join(statement).strip()
            if sql:
                conn.execute(sql)
            statement = []
    sql = "".join(statement).strip()
    if sql and not sql.startswith("--"):
        conn.execute(sql)


_SCHEMA_SCOPE = contextvars.ContextVar("ledger_schema_scope", default=None)


def schema_is_ready(conn):
    """Whether *this exact connection* was already verified by the caller.

    The answer is scoped to the enclosing :func:`schema_ready_scope` in the
    current context.  Nothing about a database is cached process-wide, so a
    switch to another ``DB_PATH`` (or a second connection) never inherits it.
    """
    return _SCHEMA_SCOPE.get() is conn


@contextmanager
def schema_ready_scope(conn):
    """Nested owners may skip DDL only for one verified initialization call.

    Scope: this connection object, inside this ``with`` block, in this context.
    No process/DB_PATH readiness is cached, nothing is written, and business
    seed/recovery must still run — the scope only suppresses repeated DDL.
    """
    token = _SCHEMA_SCOPE.set(conn)
    try:
        yield conn
    finally:
        _SCHEMA_SCOPE.reset(token)


def is_sqlite_busy_error(exc: Exception) -> bool:
    """Check whether an exception represents an SQLite busy / locked condition.

    Checks in priority order:
    1. Modern Python sqlite_errorname (e.g. SQLITE_BUSY, SQLITE_LOCKED)
    2. sqlite_errorcode attribute (e.g. 5, 6, 261, 517)
    3. String pattern matching fallback
    """
    if not isinstance(exc, (sqlite3.OperationalError, sqlite3.DatabaseError)):
        return False
    errorname = getattr(exc, "sqlite_errorname", None)
    if errorname in ("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_BUSY_RECOVERY", "SQLITE_BUSY_SNAPSHOT"):
        return True
    errorcode = getattr(exc, "sqlite_errorcode", None)
    if errorcode in (5, 6, 261, 517):
        return True
    msg = str(exc).lower()
    return any(p in msg for p in ("database is locked", "database table is locked", "busy", "locked"))


def sqlite_busy_backoff(attempt: int, hot_path: bool = False) -> float:
    """Calculate backoff duration for SQLite busy retry.

    Hot paths (trading slots): short bounded exponential backoff with jitter (10ms - 200ms).
    Cold / heavy paths: standard exponential backoff with jitter (100ms - 1000ms).
    """
    if hot_path:
        base = min(0.2, 0.02 * (2 ** attempt))
        jitter = random.uniform(0.005, 0.03)
        return base + jitter
    else:
        base = min(1.0, 0.1 * (2 ** attempt))
        jitter = random.uniform(0.02, 0.1)
        return base + jitter


def apply_journal_mode(conn, attempts=6):
    """把连接切到 WAL，并容忍"另一个进程正在初始化"的瞬时锁。

    ``PRAGMA journal_mode=WAL`` 不是普通读：它可能要改写数据库头。两个进程同时
    启动、其中一个已持有写事务时，切换会立刻返回 SQLITE_BUSY（SQLite 对
    journal-mode 变更不做无限等待）。这里沿用存储层既有的 busy 判定与退避做有界
    重试：连接超时、事务隔离与 fencing 语义都不变，只是不在启动竞态里直接失败。
    """
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if not is_sqlite_busy_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(sqlite_busy_backoff(attempt))


@contextmanager
def db(db_path, immediate=False, hot_path=False):
    """获取数据库连接并保证事务在关闭前明确提交或回滚。"""
    timeout = 1.0 if hot_path else 120.0
    busy_timeout_ms = 800 if hot_path else 60000
    with PTM.phase("db_acquire"):
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        apply_journal_mode(conn)
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
    try:
        with PTM.phase("db_transaction_body"):
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
        with PTM.phase("db_commit"):
            try:
                conn.commit()
            except sqlite3.OperationalError as exc:
                if is_sqlite_busy_error(exc):
                    # 锁冲突：写入仍在事务里，必须原样重试；二次失败先回滚，
                    # 不能让 close() 对半提交状态做隐式处理。
                    time.sleep(sqlite_busy_backoff(0, hot_path=hot_path))
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


def execute_with_retry(conn, sql, params=(), max_retries=3, hot_path=True):
    """执行 SQL，并在数据库锁定时按递增间隔重试。"""
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except Exception as exc:
            if is_sqlite_busy_error(exc) and attempt < max_retries - 1:
                time.sleep(sqlite_busy_backoff(attempt, hot_path=hot_path))
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


# ---------------------------------------------------------------------------
# 账本连接的公开面
#
# 逐笔交易账本的连接以前由 ``paper_trading._db`` / ``_db_readonly`` /
# ``_evolution_conn`` 私下提供，10 个模块为了拿一条连接去 import 交易主引擎：
# 连接与事务重试属于存储边界，不属于交易规则，方向是反的。这里把它声明成
# 公开能力；``paper_trading`` 的同名包装保留为薄委托，行为不变。
#
# 复用与失效口径：连接是进程内短生命周期的，不缓存、不跨事务共享；调用方
# 拿到的是它自己那条连接。本轮只搬"谁提供连接"，不搬 schema 迁移
# （``init_db``）——那属于下一批。
# ---------------------------------------------------------------------------

SLOT_HOT_PATH = contextvars.ContextVar("slot_hot_path", default=False)

PAPER_DB_FILENAME = "paper_trading.sqlite3"
ADAPTIVE_DB_FILENAME = "adaptive_learning.sqlite3"


_ledger_path_provider = None


def bind_ledger_path(provider) -> None:
    """由账本 owner 在组装期登记"当前账本路径"的取值方式。

    存储边界不认识交易引擎，也不需要认识：它只在下一次开连接时问一次。
    这样测试把账本指到临时库（``P.DB_PATH = tmp``）时，公开连接面自动跟随，
    不必逐个 patch 私有函数。
    """
    global _ledger_path_provider
    _ledger_path_provider = provider


def paper_db_path() -> str:
    """当前账本 SQLite 路径（唯一来源；跟随账本 owner 的实时取值）。"""
    if _ledger_path_provider is not None:
        return str(_ledger_path_provider())
    return data_paths.data_path(PAPER_DB_FILENAME)


def adaptive_db_path() -> str:
    """自进化参数仓 SQLite 路径（唯一来源）。"""
    return data_paths.data_path(ADAPTIVE_DB_FILENAME)


@contextmanager
def paper_db(immediate: bool = False, hot_path: bool = False, db_path: str | None = None):
    """账本连接。

    ``hot_path`` 为假时服从当前 slot 的热路径标记：交易热路径要短超时、短
    busy_timeout，冷路径要能容忍长等待。两者用同一个 ``SLOT_HOT_PATH``。
    """
    effective = bool(hot_path or SLOT_HOT_PATH.get())
    with db(db_path or paper_db_path(), immediate=immediate, hot_path=effective) as conn:
        yield conn


@contextmanager
def paper_db_readonly(db_path: str | None = None):
    """只读账本连接：不迁移、不取写锁，浏览器读历史不被盘中 worker 挡住。"""
    with db_readonly(db_path or paper_db_path()) as conn:
        yield conn


def evolution_connection(db_path: str | None = None):
    """自进化参数仓连接；调用方负责关闭。刻意不做 schema 迁移。"""
    conn = sqlite3.connect(db_path or adaptive_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
