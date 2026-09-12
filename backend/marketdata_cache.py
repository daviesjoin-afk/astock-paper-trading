# -*- coding: utf-8 -*-
"""行情缓存与跨进程快照锁的无业务状态工具。

具体缓存字典、锁对象和路径由调用方注入，便于保持 `data_fetcher` 的旧
调试入口与测试 monkeypatch 兼容，同时把文件缓存细节从 provider 编排中移出。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager

try:  # POSIX containers use flock; Windows keeps the thread lock.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on Windows.
    _fcntl = None


# Provider keys include code/date combinations and can grow without bound in
# a long-lived API process.  Keep the hot in-process cache small; disk snapshots
# remain the durable source for larger reads.
MAX_CACHE_ENTRIES = 128


def cached(cache, lock, key, ttl, fn):
    """读取带 TTL 的内存缓存；空结果不写入，避免失败污染。"""
    now = time.time()
    with lock:
        if key in cache and now - cache[key][0] < ttl:
            return cache[key][1]
    value = fn()
    if value:
        with lock:
            if key not in cache and len(cache) >= MAX_CACHE_ENTRIES:
                oldest_key = min(cache, key=lambda item: cache[item][0])
                cache.pop(oldest_key, None)
            cache[key] = (now, value)
    return value


@contextmanager
def full_snapshot_singleflight_lock(thread_lock, lock_path):
    """串行化同进程和跨进程的全市场快照刷新。"""
    lock_handle = None
    with thread_lock:
        try:
            lock_handle = open(lock_path, "a+", encoding="utf-8")
        except OSError:
            yield
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_EX)
            yield
        finally:
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_UN)
            finally:
                lock_handle.close()


def save_source_health(path, payload):
    """以临时文件替换方式持久化数据源健康状态。

    Feed runtime health is attached lazily so `/api/health` can expose provider
    circuit/degradation state without importing transport policy into the API.
    An empty runtime registry leaves the legacy payload shape unchanged.
    """
    snapshot = dict(payload) if isinstance(payload, dict) else {}
    try:
        import marketdata_feeds as feeds
        runtime_feeds = feeds.feed_health_snapshot()
        if runtime_feeds:
            snapshot["runtime_feeds"] = runtime_feeds
    except Exception:
        # Health persistence is diagnostic and must never break the data path.
        pass
    try:
        temp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temp_path, path)
    except OSError:
        pass


def load_source_health(path):
    """读取健康状态并叠加当前进程的实时 feed 状态，不触碰网络。

    持久化快照适合审计，但 circuit/degraded 状态会在请求路径中变化；读取时
    合并内存 registry，确保现有 `/api/health` 看到的是当前状态。进程重启后
    registry 为空时移除旧 runtime 状态，避免把上个进程的 circuit 当成当前值。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload = dict(payload) if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        payload = {}
    try:
        import marketdata_feeds as feeds
        runtime_feeds = feeds.feed_health_snapshot()
        if runtime_feeds:
            payload["runtime_feeds"] = runtime_feeds
        else:
            payload.pop("runtime_feeds", None)
    except Exception:
        # Observability must never make health reads fail.
        pass
    return payload
