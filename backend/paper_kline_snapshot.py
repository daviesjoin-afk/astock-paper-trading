# -*- coding: utf-8 -*-
"""单次规划内的日线复用（owner）。

一次委托/候选规划里，多个消费者（持仓天数、买卖模型、决策快照）常常需要
**同一份**完整日线。重复读取会重复解析 CSV，也增加"同一轮内两个消费者看到
不同数据"的风险。

本模块只提供一件事：在一个显式的**轮次作用域**内，按
(证券, 截止日, 是否含截止日) 复用一次成功读取的日线。

契约：
- 缓存**只活在一轮之内**，跨调用不保留；下一次规划必须重新取证。
- 只复用成功且非空的结果；空结果/缺失不缓存，失败可以在同一轮内重试。
- 每个消费者拿到独立副本，消费者可以自由修改自己的那份。
- 读取器是注入的能力（loader），本模块不自己读文件。
- 可选 prefetch 只提前读取：空/异常原样交给首个消费者一次，之后仍可重试；
  不把预取失败变为成功，也不将其固定为整轮的失败结论。
"""
from __future__ import annotations

from typing import Any, Callable


class RoundKlineCache:
    """一轮之内的日线快照复用器。"""

    def __init__(self, loader: Callable[..., Any]):
        self._loader = loader
        self._frames: dict[tuple, Any] = {}
        self._pending_reads: dict[tuple, tuple[Any, Exception | None]] = {}
        self.reads = 0
        self.hits = 0

    @staticmethod
    def _key(security, cutoff, inclusive) -> tuple:
        return (str(security), cutoff, bool(inclusive))

    def get(self, security, cutoff, *, inclusive: bool = True):
        key = self._key(security, cutoff, inclusive)
        if key in self._pending_reads:
            frame, error = self._pending_reads.pop(key)
            if error is not None:
                raise error
            return None if frame is None else frame.copy(deep=True)
        if key in self._frames:
            self.hits += 1
            return self._frames[key].copy(deep=True)
        self.reads += 1
        frame = self._loader(security, cutoff, inclusive=inclusive)
        if frame is None or getattr(frame, "empty", False):
            # 空结果不缓存：同一轮内允许重试（例如文件刚写好）。
            return frame
        self._frames[key] = frame.copy(deep=True)
        return self._frames[key].copy(deep=True)

    def prefetch(self, security, cutoff, *, inclusive: bool = True):
        """Move the first read earlier without hiding its empty/error outcome.

        An unsuccessful speculative read is handed to the first actual consumer
        exactly once. Later consumers retry normally; it never becomes a
        round-wide negative cache or an extra source attempt before consumption.
        """
        key = self._key(security, cutoff, inclusive)
        if key in self._frames or key in self._pending_reads:
            return
        try:
            frame = self.get(security, cutoff, inclusive=inclusive)
        except Exception as exc:
            self._pending_reads[key] = (None, exc)
        else:
            if frame is None or getattr(frame, "empty", False):
                self._pending_reads[key] = (frame, None)
