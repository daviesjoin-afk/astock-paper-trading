# -*- coding: utf-8 -*-
"""业绩公告 ledger 的**生产存储位置**与连接约定。

职责刻意只有三件：决定文件放哪、按什么 PRAGMA 连、什么时候可以创建 schema。
判定逻辑在 ``earnings_disclosure_facts``，事实语义在 ``earnings_disclosure_ledger``，
抓取在 ``earnings_disclosure_ingestion`` —— 本模块不解析、不抓取、不做业务判断。

──────────────────────── 读路径**不创建文件** ────────────────────────

``connect_readonly`` 在文件不存在时直接抛 ``ledger_unavailable``，而不是让
``sqlite3.connect`` 顺手建一个空库。原因不是洁癖：一个 GET 请求在只读路径上创建出
空库之后，下一次摄取会看到"表已存在但没有行"，而真正的"从未采集过"就再也区分不出来。

写路径用 WAL + ``synchronous=NORMAL``（与 ``paper_storage`` 同一取向）：摄取是一批
小事务，崩溃后宁可丢最后一轮也不要让每次写入都等 fsync。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from typing import Iterator

import data_paths
import earnings_disclosure_ledger as LEDGER

__all__ = (
    "DB_FILENAME",
    "db_path",
    "connect_readonly",
    "connect_writable",
    "ensure",
)

#: 运行时数据目录下的文件名。``ASTOCK_DATA_DIR`` 会把整个目录改向别处，
#: 因此浏览器 E2E 与测试不会碰到生产库（与其它运行时状态同一机制）。
DB_FILENAME = "earnings_disclosure.sqlite"

_WRITE_TIMEOUT_SECONDS = 30.0
_READ_TIMEOUT_SECONDS = 5.0


def db_path() -> str:
    """生产库路径（绝对路径，不自动创建目录）。"""
    return data_paths.data_path(DB_FILENAME)


@contextlib.contextmanager
def connect_writable(path=None) -> Iterator[sqlite3.Connection]:
    """写连接：负责建目录、设 PRAGMA、成功时提交、异常时回滚。"""
    target = os.fspath(path or db_path())
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(target, timeout=_WRITE_TIMEOUT_SECONDS)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def connect_readonly(path=None) -> Iterator[sqlite3.Connection]:
    """只读连接。**文件不存在即拒绝**，绝不在读路径上建库。

    ``PRAGMA query_only=ON`` 是第二道闸：即使调用方在这条连接上误发写入，
    SQLite 也会拒绝，而不是把只读视图悄悄变成写路径。
    """
    target = os.fspath(path or db_path())
    if not os.path.exists(target):
        raise LEDGER.EarningsLedgerError(
            "ledger_unavailable", f"{DB_FILENAME} 尚未创建（还没有跑过摄取）",
        )
    conn = sqlite3.connect(target, timeout=_READ_TIMEOUT_SECONDS)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


def ensure(path=None) -> str:
    """建立 schema（幂等），返回实际使用的路径。**只有写入路径调用它。**"""
    target = os.fspath(path or db_path())
    with connect_writable(target) as conn:
        LEDGER.ensure_schema(conn)
    return target
