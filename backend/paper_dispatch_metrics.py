# -*- coding: utf-8 -*-
"""调度级观测：把已有的结构化事件聚合成 PR-11 的验收指标。

数据来源是主机 ``scheduler.log`` 里的若干类 JSON 行（每行一个事件）。本模块只
负责**聚合与读取**，不产生事件、不判断交易：

* ``{"event": "paper_host_lock", "status": "not_acquired", "slot": "intraday",
  "wait_seconds": 61}`` —— ``deploy/run_paper_slot.sh`` 抢 ``paper-scan.lock``
  失败，本轮**没有进入 runner**；
* ``{"event": "paper_host_lock", "status": "yielded_to_full_scan", "slot":
  "fast-entry", ...}`` —— ``fast-entry`` 看到全扫描意图标记后主动让路；
* ``{"event": "paper_slot_start", "slot": "intraday", "tick_id": "...",
  "wait_seconds": 0, "at": "..."}`` —— 拿到主机锁、**即将**启动 runner；
* ``{"event": "paper_slot_exit", "slot": "intraday", "tick_id": "...",
  "exit_code": 1, "at": "..."}`` —— runner 进程返回（含被杀/崩溃）；
* ``{"event": "paper_slot_timing", "status": "completed", "timing": {"slot":
  "intraday", "started_at": "...", "stages": {...}, ...}}`` ——
  ``paper_timing.capture`` 在每个 slot 结束时输出一次。

**slot 位置的唯一事实来源是 ``timing.slot``。** ``paper_timing.capture`` 把 slot
写在 ``timing`` 里，顶层没有 slot；从顶层读取会把所有真实任务记成 ``unknown``，
从而让 start-to-start 与"上次成功风控"全部失真（#324 复审的第一条阻断）。
``paper_host_lock`` / ``paper_slot_start`` / ``paper_slot_exit`` 是主机脚本自己
发的，slot 在顶层。两种形状分别取，互不回落。

口径：

* **tick** 是一次调度尝试。``paper_slot_start`` 记"真正进入 runner"的那一次；
  ``paper_host_lock`` 的 ``not_acquired`` / ``yielded_to_full_scan`` 记"被锁挡住"
  的那一次。两者相加才是日志里可观测的尝试数。
* **queue age** 是进入 runner 之前的等待（主机 ``paper-scan.lock``），不含外层
  cron 锁与 Docker/Python 启动。有 ``paper_slot_start`` 时以它为准（进入时刻的
  权威值），否则退回 ``timing.host_paper_lock_wait_seconds``；同一轮不重复计数。
* **start-to-start** 是同一 slot 相邻两次 ``started_at`` 的间隔，用 ``started_at``
  而不是结束时间，因为 PR-11 的验收写的是 start-to-start interval。
* **状态四分类**（``completed`` / ``idle`` / ``failed`` / ``unknown``）：只有
  ``completed``（``completed`` / ``ok``）代表真的跑完一次新扫描；``skipped`` /
  ``already_done`` / ``up_to_date`` 归入 ``idle``，**不算**新的成功风控；
  ``failed`` / ``error`` / ``blocked`` 归入 ``failed``。
* **last successful risk scan**：优先取 ``slot == "risk"`` 的 completed 事件；旧
  调度里风控跑在 ``intraday`` 内，此时退化为"completed 且含 ``risk`` 前缀阶段、
  阶段 errors 全为 0"的 ``intraday`` 事件，并用 ``source`` 标明规则来源。

已知盲区（不粉饰）：外层 cron 的 ``flock -n`` 拒绝（``auction`` / ``open`` /
``close`` 等）不会执行脚本，因而**不产生任何日志行**；被信号直接杀死的 runner
只在 ``paper_slot_exit`` 上留痕。因此 ``ticks.started`` 是"进入 runner 的次数"，
不是"cron 触发次数"；``ticks.source`` 会说明日志里是否存在启动事件。

本模块不 import 任何项目模块，不碰数据库，不读取任何凭据，也不参与交易判断；
它只把日志行变成数字。**没有这些数字，PR-11 的"risk-fast interval 达到目标"
无法验收**，这是它存在的唯一理由。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

SLOT_TIMING_EVENT = "paper_slot_timing"
SLOT_START_EVENT = "paper_slot_start"
SLOT_EXIT_EVENT = "paper_slot_exit"
HOST_LOCK_EVENT = "paper_host_lock"

#: 主动让路的原因值：``fast-entry`` 为全扫描让路。
YIELDED = "yielded_to_full_scan"
#: 抢锁失败的原因值：等待窗口内没有拿到锁。
NOT_ACQUIRED = "not_acquired"

#: 真的跑完一次新扫描。
COMPLETED_STATUSES = frozenset({"completed", "ok"})
#: 没有产生新扫描：幂等跳过、非交易时段、数据已最新。
IDLE_STATUSES = frozenset({"already_done", "skipped", "up_to_date"})
#: 明确失败或被阻断。
FAILED_STATUSES = frozenset({"failed", "error", "blocked"})

#: 日志里既没有启动事件也没有结束事件时的占位 slot。
UNKNOWN_SLOT = "unknown"

#: PR-11 的目标节拍：风险快通道每 3 分钟一次。
DEFAULT_TARGET_INTERVAL_SECONDS = 180

#: 结束事件来源：有 ``paper_slot_start`` 时用启动计数，否则只能退回结束事件。
SOURCE_START_EVENTS = "runner_start_events"
SOURCE_END_EVENTS = "slot_timing_end_events"


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


def scheduler_log_path(report_dir, environ=None):
    """主机调度日志路径：默认落在报告目录，允许 ``ASTOCK_SCHEDULER_LOG`` 覆盖。

    ``report_dir`` 由调用方（``data_paths.report_dir``）提供，本模块不 import
    项目模块。宿主 cron 写 ``/opt/astock-codex/reports/scheduler.log``，而 API
    容器把 ``./reports`` 挂在 ``/app/reports``，所以默认值必须是"报告目录下的
    scheduler.log"，而不是宿主绝对路径——否则日志真实存在，Dashboard 却永远
    ``available=False``（#324 复审的第二条阻断）。
    """
    environ = os.environ if environ is None else environ
    override = environ.get("ASTOCK_SCHEDULER_LOG") or ""
    if override.strip():
        return override.strip()
    return os.path.join(str(report_dir), "scheduler.log")


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
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _timing_block(event):
    timing = event.get("timing")
    return timing if isinstance(timing, dict) else None


def _timing_slot(event):
    """``paper_slot_timing`` 的 slot 只在 ``timing.slot``；顶层不算证据。"""
    timing = _timing_block(event)
    return _text(timing.get("slot")) if timing is not None else None


def _host_slot(event):
    """主机脚本事件（锁/启动/退出）的 slot 在顶层。"""
    return _text(event.get("slot"))


def _wait_seconds(value):
    try:
        wait = float(value)
    except (TypeError, ValueError):
        return None
    return wait if wait >= 0 else None


def _lock_wait(event):
    kind = event.get("event")
    if kind == HOST_LOCK_EVENT or kind == SLOT_START_EVENT:
        return _wait_seconds(event.get("wait_seconds"))
    timing = _timing_block(event)
    return _wait_seconds(timing.get("host_paper_lock_wait_seconds")) if timing is not None else None


def _status_bucket(status):
    """把 runner 的 ``status`` 归入 completed / idle / failed / unknown。"""
    lowered = str(status or "").strip().lower()
    if lowered in COMPLETED_STATUSES:
        return "completed"
    if lowered in IDLE_STATUSES:
        return "idle"
    if lowered in FAILED_STATUSES:
        return "failed"
    return "unknown"


def _stage_errors(value):
    """阶段 ``errors`` 转成整数；不是数字时返回 None（当作未知，不冒充 0）。"""
    if not isinstance(value, dict):
        return None
    try:
        return int(value.get("errors") or 0)
    except (TypeError, ValueError):
        return None


def _is_risk_pass(event, slot, bucket):
    """旧调度里风控跑在 intraday 内：completed 且 risk 阶段零错误才算成功。

    停在这一步的三种情况都**不算**一次新的成功风控：``skipped`` /
    ``already_done`` 之类的 idle 结束、``failed`` 结束、以及阶段 errors
    无法解析（未知不等于 0）。
    """
    if slot != "intraday" or bucket != "completed":
        return False
    timing = _timing_block(event)
    stages = timing.get("stages") if timing is not None else None
    if not isinstance(stages, dict):
        return False
    risk_stages = [value for name, value in stages.items() if str(name).startswith("risk")]
    if not risk_stages:
        return False
    errors = [_stage_errors(value) for value in risk_stages]
    if any(value is None for value in errors):
        return False
    return all(value == 0 for value in errors)


def _tick_id(event):
    """tick 身份：主机事件在顶层，``paper_timing`` 的结束事件在 ``timing`` 里。"""
    tick = _text(event.get("tick_id"))
    if tick is not None:
        return tick
    timing = _timing_block(event)
    return _text(timing.get("scheduler_tick_id")) if timing is not None else None


def summarize(events, *, target_interval_seconds=DEFAULT_TARGET_INTERVAL_SECONDS):
    """把事件流聚合成 PR-11 的调度指标。

    返回纯数据（可 JSON 序列化），字段含义见模块 docstring。``events`` 按日志
    顺序（时间递增）假定；同一 slot 的 start-to-start 只按该顺序取相邻。
    坏输入（``timing`` 不是对象、``started_at`` 不是时间、``errors`` 不是数字、
    ``wait_seconds`` 为负）一律降级为"这条证据不成立"，不抛异常。
    """
    target = float(target_interval_seconds)

    runs = {}
    skips = {}
    skips_by_slot = {}
    waits = []
    start_waits = []
    timing_waits = []
    intervals = {}
    last_starts = {}
    last_risk = {"at": None, "source": None, "status": None, "slot": None}

    started = 0
    end_events = 0
    skip_total = 0
    unparsed = 0
    buckets = {"completed": 0, "idle": 0, "failed": 0, "unknown": 0}
    #: slot -> 待配对的启动记录（按到达顺序；tick_id 用于精确配对）。
    pending = {}

    def _bucket_for(slot):
        return runs.setdefault(slot, {
            "started": 0, "end_events": 0, "skipped": 0,
            "completed": 0, "idle": 0, "failed": 0, "unknown": 0,
        })

    def _take_pending(slot, tick):
        queue = pending.get(slot) or []
        for index, entry in enumerate(queue):
            if tick is None or entry["tick"] == tick:
                return queue.pop(index)
        return None

    for event in events:
        kind = event.get("event")

        if kind == HOST_LOCK_EVENT:
            status = _text(event.get("status")) or ""
            if status in (YIELDED, NOT_ACQUIRED):
                skips[status] = skips.get(status, 0) + 1
                skip_total += 1
                slot = _host_slot(event) or UNKNOWN_SLOT
                _bucket_for(slot)["skipped"] += 1
                by_slot = skips_by_slot.setdefault(slot, {})
                by_slot[status] = by_slot.get(status, 0) + 1
            wait = _lock_wait(event)
            if wait is not None:
                waits.append(wait)
            continue

        if kind == SLOT_START_EVENT:
            slot = _host_slot(event) or UNKNOWN_SLOT
            started += 1
            _bucket_for(slot)["started"] += 1
            pending.setdefault(slot, []).append({"tick": _tick_id(event), "exited": False})
            wait = _lock_wait(event)
            if wait is not None:
                start_waits.append(wait)
            continue

        if kind == SLOT_EXIT_EVENT:
            slot = _host_slot(event) or UNKNOWN_SLOT
            entry = _take_pending(slot, _tick_id(event))
            if entry is not None:
                # 启动过、退出过、却没有结束事件：runner 被杀或崩溃在打印之前。
                pending[slot].append(entry)
                entry["exited"] = True
            continue

        if kind != SLOT_TIMING_EVENT:
            continue

        end_events += 1
        slot = _timing_slot(event)
        if slot is None:
            # 形状不成立（不是缺少顶层 slot 的问题）：计入 unknown，不猜。
            unparsed += 1
            slot = UNKNOWN_SLOT
        # 有启动记录时把它配掉：配上的才算"跑完"，没配上的留在 pending 里
        # 计成 unfinished / exited_without_result。
        _take_pending(slot, _tick_id(event))
        status = str(event.get("status") or "").strip().lower() or "unknown"
        bucket = _status_bucket(status)
        buckets[bucket] += 1
        slot_runs = _bucket_for(slot)
        slot_runs["end_events"] += 1
        slot_runs[bucket] += 1

        timing = _timing_block(event)
        wait = _lock_wait(event)
        if wait is not None:
            timing_waits.append(wait)

        started_at = _parse_started_at((timing or {}).get("started_at"))
        if started_at is not None:
            previous = last_starts.get(slot)
            if previous is not None:
                gap = (started_at - previous).total_seconds()
                if gap >= 0:
                    intervals.setdefault(slot, []).append(gap)
            last_starts[slot] = started_at

        if slot == "risk" and bucket == "completed":
            last_risk = {
                "at": started_at.isoformat() if started_at else None,
                "source": "risk_slot", "status": status, "slot": slot,
            }
        elif last_risk["source"] != "risk_slot" and _is_risk_pass(event, slot, bucket):
            last_risk = {
                "at": started_at.isoformat() if started_at else None,
                "source": "intraday_risk_phase", "status": status, "slot": slot,
            }

    unfinished = 0
    exited_without_result = 0
    for queue in pending.values():
        for entry in queue:
            if entry["exited"]:
                exited_without_result += 1
            else:
                unfinished += 1

    # 有启动事件时用进入时刻的等待；没有就退回结束事件里的等待，同一轮不重复计。
    if start_waits:
        waits.extend(start_waits)
    else:
        waits.extend(timing_waits)

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
            "started": started,
            "skipped": skip_total,
            "end_events": end_events,
            "completed": buckets["completed"],
            "idle": buckets["idle"],
            "failed": buckets["failed"],
            "unknown_status": buckets["unknown"],
            "unfinished": unfinished,
            "exited_without_result": exited_without_result,
            "unparsed": unparsed,
            "source": SOURCE_START_EVENTS if started else SOURCE_END_EVENTS,
        },
        "runs": runs,
        "skips": skips,
        "skips_by_slot": skips_by_slot,
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


def summarize_file(path, *, max_lines=4000, max_bytes=1_500_000,
                   target_interval_seconds=DEFAULT_TARGET_INTERVAL_SECONDS):
    """读取日志尾部并聚合；读不到时返回带原因的来源标记而不是抛异常。

    ``max_lines`` / ``max_bytes`` 只保留尾部：调度日志是追加写的长文件，
    验收要看的是"最近这段"的节拍。按字节从文件末尾回退读取，避免把整份
    日志读进内存——这个函数在 dashboard 刷新路径上被调用。

    ``error`` 区分 ``missing``（文件不存在）与 ``unreadable``（存在但没权限/读错），
    因为"日志没挂进来"和"权限不对"是两种完全不同的运维动作。
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - int(max_bytes))
            handle.seek(start)
            chunk = handle.read()
    except FileNotFoundError:
        return {"available": False, "path": str(path), "error": "missing"}
    except OSError:
        return {"available": False, "path": str(path), "error": "unreadable"}
    except (TypeError, ValueError):
        return {"available": False, "path": str(path), "error": "bad_path"}
    lines = chunk.decode("utf-8", "replace").splitlines()
    if start and lines:
        # 从任意字节偏移切进来，第一行通常是被截断的半行；它解析不了，丢掉。
        lines = lines[1:]
    lines = lines[-int(max_lines):] if max_lines else lines
    summary = summarize_lines(lines, target_interval_seconds=target_interval_seconds)
    summary["available"] = True
    summary["path"] = str(path)
    summary["lines_scanned"] = len(lines)
    return summary
