"""模拟任务的低开销诊断：标准库计时，复用任务详情与调度日志。

阶段耗时包含被调用的子阶段，不能相加作为总耗时。不采集 SQL、URL、
参数或异常正文；计时数据不参与任务身份、租约和交易判断。
"""
from __future__ import annotations

import contextvars
import functools
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager


_CURRENT = contextvars.ContextVar("paper_timing", default=None)


class _Record:
    def __init__(self, slot):
        self.started = time.perf_counter()
        self.attempt_id = uuid.uuid4().hex
        self.slot = str(slot)
        self.identity = {}
        self.stages = {}
        self.status = "unknown"


def bind_job(**identity):
    record = _CURRENT.get()
    if record is not None:
        record.identity.update(identity)


def outcome(status):
    record = _CURRENT.get()
    if record is not None:
        record.status = str(status)


@contextmanager
def phase(name):
    record = _CURRENT.get()
    if record is None:
        yield
        return
    started = time.perf_counter()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        elapsed = max(0.0, time.perf_counter() - started)
        item = record.stages.setdefault(name, {"seconds": 0.0, "calls": 0, "errors": 0})
        item["seconds"] += elapsed
        item["calls"] += 1
        item["errors"] += int(failed)


def timed(name):
    def decorate(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            with phase(name):
                return func(*args, **kwargs)
        return wrapped
    return decorate


def snapshot(scope):
    """未启用计时时为空；持久化前快照不冒充完成提交后的全程耗时。"""
    record = _CURRENT.get()
    if record is None:
        return {}
    result = {
        "version": 1, "attempt_id": record.attempt_id, "slot": record.slot,
        "scope": scope, "identity": dict(record.identity),
        "elapsed_seconds": round(max(0.0, time.perf_counter() - record.started), 6),
        "stages": {
            name: {**value, "seconds": round(value["seconds"], 6)}
            for name, value in record.stages.items()
        },
    }
    # 只代表主机 paper-scan.lock 的等待；外层 cron 锁、Docker 启动不在此内。
    try:
        wait = int(os.environ["ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS"])
        if 0 <= wait <= 86400:
            result["host_paper_lock_wait_seconds"] = wait
            result["host_lock_resolution_seconds"] = 1
    except (KeyError, ValueError, TypeError):
        pass
    return result


@contextmanager
def capture(slot):
    """同一调用链只发一条结束日志；后台线程不隐式继承调用者事务上下文。"""
    if _CURRENT.get() is not None:
        yield
        return
    token = _CURRENT.set(_Record(slot))
    try:
        yield
    except BaseException:
        outcome("failed")
        raise
    finally:
        try:
            record = _CURRENT.get()
            print(json.dumps({
                "event": "paper_slot_timing", "status": record.status,
                "timing": snapshot("finished"),
            }, ensure_ascii=False), file=sys.stderr, flush=True)
        except Exception:
            # 日志输出故障不得改变已有任务的成功/失败和重试语义。
            pass
        finally:
            _CURRENT.reset(token)


def traced_slot(func):
    @functools.wraps(func)
    def wrapped(slot, asof_date=None, force=False):
        with capture(slot):
            result = func(slot, asof_date=asof_date, force=force)
            outcome(result.get("status", "unknown"))
            return {**result, "timing": snapshot("slot_return")}
    return wrapped
