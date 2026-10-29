# -*- coding: utf-8 -*-
"""持久化全 A 股基础库的读取边界。

``universe.json`` 由 ``universe.build_universe`` 写出。行情侧在主导源不可用时
需要这份代码集合来发起备用报价，而 ``universe`` 本身又要用行情服务构建基础库，
两边一旦互相 import 就会形成闭环。把文件读取放在这一层，两个消费者取的是同一份
事实，依赖方向只剩单向。

读取范围故意保持很窄：只有代码列表与文件身份，不含成员资格、交易日历或历史
完整性判断——那些仍归 ``universe`` / ``point_in_time``。
"""
from __future__ import annotations

import json
import os
import threading

import data_fetcher as dfc

UNIVERSE_FILE_NAME = "universe.json"
FORMAL_UNIVERSE_MIN_ROWS = 4000

# 基础库是低频变更的静态文件，但每轮盘中扫描会通过多条路径重复读取数十次
# （1.86MB JSON）。按 (路径, mtime, size) 识别同一份文件，文件被重建时自动失效。
_cache_rows = None
_cache_identity = None
_cache_lock = threading.Lock()


def universe_path() -> str:
    return os.path.join(dfc.CACHE_DIR, UNIVERSE_FILE_NAME)


def universe_file_signature(path=None):
    target = str(path or universe_path())
    try:
        return (os.path.getmtime(target), os.path.getsize(target))
    except OSError:
        return None


def load_formal_universe(path=None):
    """返回已持久化的全 A 股行；没有合格正式清单时返回空列表。"""
    global _cache_rows, _cache_identity
    target = str(path or universe_path())
    signature = universe_file_signature(target)
    if signature is None:
        return []
    identity = (target, signature)
    if _cache_rows is not None and identity == _cache_identity:
        return _cache_rows
    with _cache_lock:
        # 双检：锁内再确认一次，避免并发下重复读盘。
        if _cache_rows is not None and identity == _cache_identity:
            return _cache_rows
        rows = _read_formal_universe(target)
        _cache_rows, _cache_identity = rows, identity
        return rows


def _read_formal_universe(target):
    if not os.path.exists(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        stocks = payload.get("stocks", []) if isinstance(payload, dict) else []
        codes = {str(row.get("code") or "") for row in stocks
                 if isinstance(row, dict) and row.get("code")}
        # Legacy/debug files must not become a formal input merely because the
        # JSON itself is parseable.  The writer records requested_limit=None for
        # the only publishable all-market form; a formal file also needs the
        # same 4,000-row floor.
        if (
            not isinstance(stocks, list)
            or not isinstance(payload, dict)
            or payload.get("scope") != "all_a_shares"
            or payload.get("requested_limit") is not None
            or len(codes) < FORMAL_UNIVERSE_MIN_ROWS
        ):
            return []
        return stocks
    except (OSError, ValueError, TypeError):
        return []
