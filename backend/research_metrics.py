# -*- coding: utf-8 -*-
"""研究链路的耗时度量（方案 S2：先能定位，再谈提速）。

回答一个具体问题：**这段时间到底花在哪了**。拆成四段，互不重叠：

- ``queue_wait``：入队到被领取。
- ``provider_wait``：等模型返回（外部），加它没用，要减少调用次数或换模型。
- ``compute``：本地计算（快照读取、评估、发布），加并发或优化算法有效。
- ``evidence_wait``：等真实数据/标签（等市场），任何工程手段都「加速」不了它。

把这些混成一个「总耗时」会导向错误结论：比如总耗时很长但 95% 在等次日标签，
那就不该去调模型超时，而该去做别的候选研究。

数据来源只有两类既成事实：任务生命周期时间戳与阶段计时明细；不引入新的埋点。
"""
from __future__ import annotations

import datetime as dt

import agent_task_queue as AQ

SCHEMA_VERSION = "research-latency-report-v1"

COMPUTE_STAGES = (AQ.STAGE_SNAPSHOT_READ, AQ.STAGE_ARCHIVE_DECODE,
                  AQ.STAGE_FEATURE_BUILD, AQ.STAGE_EVALUATOR_CPU,
                  AQ.STAGE_SQLITE_LOCK_WAIT, AQ.STAGE_PERSIST,
                  AQ.STAGE_ADMISSION_WAIT)


class MetricsError(ValueError):
    """度量合同违约。"""


def _iso(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _seconds(start, end) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _percentile(values, fraction: float):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _waiting_seconds(conn, task_id: str) -> float:
    """从状态迁移台账算"等待真实数据"的累计时长。"""
    rows = conn.execute(
        "SELECT from_status, to_status, recorded_at FROM agent_task_events"
        " WHERE task_id=? ORDER BY id", (str(task_id),)).fetchall()
    waiting = set(AQ.WAITING_STATUSES)
    total = 0.0
    entered = None
    for row in rows:
        status = str(row["to_status"])
        moment = _iso(row["recorded_at"])
        if status in waiting and entered is None:
            entered = moment
        elif status not in waiting and entered is not None:
            total += _seconds(entered, moment)
            entered = None
    return round(total, 6)


def _stage_totals(conn, task_id: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in conn.execute(
            "SELECT stage, SUM(duration_ms) AS ms FROM agent_task_stage_timing"
            " WHERE task_id=? GROUP BY stage", (str(task_id),)).fetchall():
        totals[str(row["stage"])] = float(row["ms"] or 0.0) / 1000.0
    return totals


def task_breakdown(conn, task_id: str) -> dict:
    """单个任务的四段拆分。``unaccounted`` 是四段之外剩下的墙钟时间。"""
    task = AQ.get(conn, task_id)
    if task is None:
        raise MetricsError(f"unknown_task:{task_id}")
    created = _iso(task.get("created_at"))
    claimed = _iso(task.get("claimed_at"))
    finished = _iso(task.get("finished_at"))
    stages = _stage_totals(conn, task_id)
    provider_wait = round(stages.get(AQ.STAGE_PROVIDER_WAIT, 0.0), 6)
    compute = round(sum(stages.get(name, 0.0) for name in COMPUTE_STAGES), 6)
    evidence_wait = _waiting_seconds(conn, task_id)
    wall = _seconds(created, finished) if finished else None
    accounted = round(provider_wait + compute + evidence_wait, 6)
    # 排队时间要从"它变成可领取的那一刻"开始算：被唤醒或延迟重试的任务，
    # available_at 才是它重新排队的时间，用 created_at 会把等待期重复算一遍。
    became_claimable = (_iso(task.get("wake_at")) or _iso(task.get("available_at"))
                        or created)
    queue_wait = round(_seconds(became_claimable, claimed), 6)
    return {
        "task_id": task["task_id"],
        "task_type": task["task_type"],
        "status": task["status"],
        "queue_wait_s": queue_wait,
        "provider_wait_s": provider_wait,
        "compute_s": compute,
        "evidence_wait_s": evidence_wait,
        "wall_s": wall,
        "accounted_s": accounted,
        "unaccounted_s": (None if wall is None else round(max(0.0, wall - accounted), 6)),
    }


def latency_report(conn, *, task_type: str | None = None,
                   limit: int = 500) -> dict:
    """汇总拆分：每段的 p50/p95，以及"最长的那一段是什么"。"""
    if not AQ._has_table(conn, "agent_tasks"):
        return {"schema_version": SCHEMA_VERSION, "samples": 0, "stages": {},
                "bottleneck": None, "tasks": []}
    sql = "SELECT task_id FROM agent_tasks WHERE finished_at IS NOT NULL"
    params: list = []
    if task_type is not None:
        sql += " AND task_type=?"
        params.append(str(task_type))
    sql += " ORDER BY finished_at DESC LIMIT ?"
    params.append(int(limit))
    rows = [row[0] for row in conn.execute(sql, params).fetchall()]
    breakdowns = [task_breakdown(conn, task_id) for task_id in rows]
    if not breakdowns:
        return {"schema_version": SCHEMA_VERSION, "samples": 0, "stages": {},
                "bottleneck": None, "tasks": []}
    stages = {}
    for name in ("queue_wait_s", "provider_wait_s", "compute_s", "evidence_wait_s",
                 "wall_s", "unaccounted_s"):
        values = [item[name] for item in breakdowns if item[name] is not None]
        stages[name] = {
            "samples": len(values),
            "p50_s": None if not values else round(_percentile(values, 0.5), 3),
            "p95_s": None if not values else round(_percentile(values, 0.95), 3),
            "max_s": None if not values else round(max(values), 3),
            "total_s": round(sum(values), 3),
        }
    candidates = {name: stages[name]["total_s"] for name in
                  ("queue_wait_s", "provider_wait_s", "compute_s", "evidence_wait_s")}
    bottleneck = max(candidates, key=lambda name: candidates[name])
    return {
        "schema_version": SCHEMA_VERSION,
        "samples": len(breakdowns),
        "stages": stages,
        "bottleneck": {"stage": bottleneck, "total_s": candidates[bottleneck]},
        "tasks": breakdowns,
    }


def speed_hint(report: dict) -> str:
    """把"最长那一段"翻译成一句可执行的判断，避免拿错药。"""
    if not report.get("samples"):
        return "没有已完成的样本，先跑通一个任务再谈提速"
    stage = report["bottleneck"]["stage"]
    if stage == "evidence_wait_s":
        return "主要在等真实数据/标签：工程加速无效，应转去做别的候选研究"
    if stage == "provider_wait_s":
        return "主要在等模型：减少调用次数或换更快的模型，别加并发挤占交易资源"
    if stage == "queue_wait_s":
        return "主要在排队：检查是否有长任务占住 worker，或提高领取频率"
    return "主要在本地计算：先看是哪一段阶段计时最长，再决定优化点"
