# -*- coding: utf-8 -*-
"""API 响应缓存模块。

特性：
- 内存缓存，零网络开销
- TTL 自动过期
- 支持按 key 失效
- 线程安全
- 统计命中率
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from functools import wraps
from typing import Any, Callable, Optional

_cache: dict[str, dict] = {}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "sets": 0, "evictions": 0}

# 默认 TTL（秒）
DEFAULT_TTL = 60
# 最大缓存条目
MAX_CACHE_SIZE = 1000


def _make_key(prefix: str, *args, **kwargs) -> str:
    """生成缓存 key。"""
    parts = [prefix]
    for arg in args:
        parts.append(str(arg))
    for k, v in sorted(kwargs.items()):
        parts.append(f"{k}={v}")
    key_str = ":".join(parts)
    return hashlib.md5(key_str.encode()).hexdigest()


def get(key: str) -> Optional[Any]:
    """获取缓存值。"""
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            _stats["misses"] += 1
            return None
        if entry["expires_at"] < time.time():
            del _cache[key]
            _stats["evictions"] += 1
            _stats["misses"] += 1
            return None
        _stats["hits"] += 1
        return entry["value"]


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """设置缓存值。"""
    with _lock:
        # 检查缓存大小限制
        if len(_cache) >= MAX_CACHE_SIZE:
            _evict_oldest()
        _cache[key] = {
            "value": value,
            "expires_at": time.time() + ttl,
            "created_at": time.time(),
        }
        _stats["sets"] += 1


def delete(key: str) -> None:
    """删除缓存条目。"""
    with _lock:
        _cache.pop(key, None)


def clear(prefix: str = None) -> int:
    """清除缓存。如果指定 prefix，只清除匹配的条目。"""
    with _lock:
        if prefix is None:
            count = len(_cache)
            _cache.clear()
            return count
        keys_to_delete = [k for k in _cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del _cache[k]
        return len(keys_to_delete)


def _evict_oldest() -> None:
    """驱逐最旧的缓存条目。"""
    if not _cache:
        return
    oldest_key = min(_cache, key=lambda k: _cache[k]["created_at"])
    del _cache[oldest_key]
    _stats["evictions"] += 1


def get_stats() -> dict:
    """获取缓存统计。"""
    with _lock:
        total = _stats["hits"] + _stats["misses"]
        hit_rate = _stats["hits"] / total if total > 0 else 0
        return {
            **_stats,
            "size": len(_cache),
            "hit_rate": round(hit_rate * 100, 2),
        }


def cached(prefix: str, ttl: int = DEFAULT_TTL, key_func: Callable = None):
    """缓存装饰器。

    Args:
        prefix: 缓存 key 前缀
        ttl: 过期时间（秒）
        key_func: 自定义 key 生成函数

    Example:
        @cached("dashboard", ttl=30)
        def dashboard():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存 key
            if key_func:
                key = key_func(*args, **kwargs)
            else:
                key = _make_key(prefix, *args, **kwargs)

            # 尝试从缓存获取
            result = get(key)
            if result is not None:
                return result

            # 执行函数并缓存结果
            result = func(*args, **kwargs)
            set(key, result, ttl)
            return result
        return wrapper
    return decorator


def invalidate_on_write(prefixes: list[str]):
    """写操作装饰器：执行后清除相关缓存。

    Example:
        @invalidate_on_write(["dashboard", "positions"])
        def submit_order(...):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            for prefix in prefixes:
                clear(prefix)
            return result
        return wrapper
    return decorator
