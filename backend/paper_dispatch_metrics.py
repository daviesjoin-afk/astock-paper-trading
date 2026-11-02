# -*- coding: utf-8 -*-
"""调度级观测：把已有的结构化事件聚合成 PR-11 的验收指标。

数据来源是主机 ``scheduler.log`` 里两类 JSON 行（每行一个事件），
两者都已经在生产里输出，本模块只负责**聚合与读取**：

* ``{"event": "paper_host_lock", "status": "not_acquired", "wait_seconds": 61}``
  —— ``deploy/run_paper_slot.sh`` 抢 ``paper-scan.lock`` 失败；
* ``{"event": "paper_host_lock", "status": "yielded_to_full_scan", ...}``
  —— ``fast-entry`` 看到全扫描意图标记后主动让路；
* ``{"event": "paper_slot_timing", "status": "ok", "timing": {...}}``
  —— ``paper_timing.capture`` 在每个 slot 结束时输出一次，含 ``started_at``、
  ``elapsed_seconds``、``host_paper_lock_wait_seconds`` 和分阶段耗时。

口径（只回答"这一轮有没有跑、等了多久、最近一次风险成功是什么时候"）：

* **tick** 指一次调度尝试：抢锁事件或 runner 结束事件各算一次；
* **queue age** 指进入 runner 之前的等待（主机锁等待，不含 Docker/Python 启动）；
* **start-to-start** 指同一 slot 相邻两次 ``started_at`` 的间隔。用 ``started_at``
  而不是结束时间，是因为 PR-11 的验收写的是 start-to-start interval；
* **last successful risk scan**：优先取 ``slot == "risk"`` 的 ok 事件；旧调度里
  风控跑在 ``intraday`` 内，此时退化为"含 ``risk`` 前缀阶段且没有阶段报错的
  ``intraday`` 事件"，并在结果里用 ``source`` 标明用的是哪条规则。

本模块不 import 任何项目模块，不碰数据库，不读取任何凭据，也不参与交易判断；
它只把日志行变成数字。**没有这些数字，PR-11 的"risk-fast interval 达到目标"
无法验收**，这是它存在的唯一理由。
"""
from __future__ import annotations

import json
from datetime import datetime

SLOT_TIMING_EVENT = "paper_slot_timing"
HOST_LOCK_EVENT = "paper_host_lock"

#: 主动让路的原因值：``fast-entry`` 为全扫描让路。
YIELDED = "yielded_to_full_scan"
#: 抢锁失败的原因值：等待窗口内没有拿到锁。
NOT_ACQUIRED = "not_acquired"

#: PR-11 的目标节拍：风险快通道每 3 分钟一次。
DEFAULT_TARGET_INTERVAL_SECONDS = 180


def iter_events(lines):
    """逐行容忍坏输入：空行、非 JSON、非对象、缺 ``event`` 的跳过。

    ``scheduler.log`` 同时收 cron 的 stdout/stderr，混入普通日志是常态；
    这里不抛异常，也不猜格式，解析不了的行走不到聚合里。
    """
    for line in lines:
        if not line:
            continue
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            event = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and isinstance(event.get("event"), str):
            yield event


def _percentile(sorted_values, fraction):
    """最近秩百分位；空序列返回 None。不插值，避免把 3 个样本伪装成分布。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = max(0, min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[index]


def _round(value):
    return None if value is None else round(float(value), 3)


def _parse_started_at(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _lock_wait(event):
    if event.get("event") == HOST_LOCK_EVENT:
        raw = event.get("wait_seconds")
    else:
        timing = event.get("timing")
        raw = timing.get("host_paper_lock_wait_seconds") if isinstance(timing, dict) else None
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return None
    return wait if wait >= 0 else None


def _is_risk_pass(event):
    """旧调度里风控跑在 intraday 内：有 risk 阶段、且没有阶段报错才算成功。"""
    if event.get("slot") != "intraday":
        return False
    if str(event.get("status") or "").lower() in ("failed", "error"):
        return False
    timing = event.get("timing")
    stages = timing.get("stages") if isinstance(timing, dict) else None
    if not isinstance(stages, dict):
        return False
    risk_stages = [value for name, value in stages.items() if str(name).startswith("risk")]
    if not risk_stages:
        return False
    return all(int(value.get("errors") or 0) == 0 for value in risk_stages if isinstance(value, dict))


def summarize(events, *, target_interval_seconds=DEFAULT_TARGET_INTERVAL_SECONDS):
    """把事件流聚合成 PR-11 的调度指标。

    返回纯数据（可 JSON 序列化），字段含义见模块 docstring。``events`` 里的
    顺序按日志顺序（时间递增）假定；同一 slot 的 start-to-start 只按该顺序取相邻。
    """
    target = float(target_interval_seconds)
    runs = {}
    skips = {}
    waits = []
    intervals = {}
    last_starts = {}
    last_risk = {"at": None, "source": None}

    for event in events:
        kind = event.get("event")

        if kind == HOST_LOCK_EVENT:
            status = str(event.get("status") or "")
            if status in (YIELDED, NOT_ACQUIRED):
                skips[status] = skips.get(status, 0) + 1
            wait = _lock_wait(event)
            if wait is not None:
                waits.append(wait)
            continue

        if kind != SLOT_TIMING_EVENT:
            continue

        slot = str(event.get("slot") or "unknown")
        status = str(event.get("status") or "unknown").lower()
        bucket = runs.setdefault(slot, {"started": 0, "ok": 0, "failed": 0})
        bucket["started"] += 1
        bucket["ok" if status not in ("failed", "error") else "failed"] += 1

        wait = _lock_wait(event)
        if wait is not None:
            waits.append(wait)

        started_at = _parse_started_at((event.get("timing") or {}).get("started_at"))
        if started_at is not None:
            previous = last_starts.get(slot)
            if previous is not None:
                gap = (started_at - previous).total_seconds()
                if gap >= 0:
                    intervals.setdefault(slot, []).append(gap)
            last_starts[slot] = started_at

        if slot == "risk" and status not in ("failed", "error"):
            last_risk = {"at": started_at.isoformat() if started_at else None, "source": "risk_slot"}
        elif last_risk["source"] != "risk_slot" and _is_risk_pass(event):
            last_risk = {"at": started_at.isoformat() if started_at else None, "source": "intraday_risk_phase"}

    waits.sort()
    interval_summary = {}
    for slot, gaps in intervals.items():
        ordered = sorted(gaps)
        interval_summary[slot] = {
            "samples": len(ordered),
            "p50_seconds": _round(_percentile(ordered, 0.50)),
            "p95_seconds": _round(_percentile(ordered, 0.95)),
            "max_seconds": _round(ordered[-1]),
            "target_seconds": _round(target),
            "breaches": sum(1 for gap in ordered if gap > target),
        }

    return {
        "ticks": {
            "started": sum(bucket["started"] for bucket in runs.values()),
            "skipped": sum(skips.values()),
        },
        "runs": runs,
        "skips": skips,
        "queue_age_seconds": {
            "samples": len(waits),
            "p50": _round(_percentile(waits, 0.50)),
            "p95": _round(_percentile(waits, 0.95)),
            "max": _round(waits[-1]) if waits else None,
        },
        "start_to_start_seconds": interval_summary,
        "last_successful_risk_scan": last_risk,
    }


def summarize_lines(lines, *, target_interval_seconds=DEFAULT_TARGET_INTERVAL_SECONDS):
    """直接吃 ``scheduler.log`` 的行迭代器（便于调用方用 ``tail`` 之类的缓冲区）。"""
    return summarize(iter_events(lines), target_interval_seconds=target_interval_seconds)


def summarize_file(path, *, max_lines=50000, target_interval_seconds=DEFAULT_TARGET_INTERVAL_SECONDS):
    """读取日志尾部并聚合；文件不存在时返回空来源标记而不是抛异常。

    ``max_lines`` 只保留尾部若干行：调度日志是追加写的长文件，验收要看的是
    "最近这段"的节拍，而不是开天辟地以来的累计。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-max_lines:]
    except OSError:
        return {"available": False, "path": str(path)}
    summary = summarize_lines(lines, target_interval_seconds=target_interval_seconds)
    summary["available"] = True
    summary["path"] = str(path)
    summary["lines_scanned"] = len(lines)
    return summary
