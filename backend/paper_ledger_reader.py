# -*- coding: utf-8 -*-
"""Adaptive 使用的只读 paper ledger 连接。

影子学习和审阅流程只能读取模拟账本。这里通过 SQLite ``mode=ro`` 与
``query_only`` 双重约束连接，避免读模型路径意外创建、修改或迁移账本。
"""
from __future__ import annotations

import sqlite3


def connect(db_path, timeout=30):
    """打开只读 paper ledger；调用方仍负责在 finally 中关闭连接。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={max(1, int(float(timeout) * 1000))}")
    return conn
