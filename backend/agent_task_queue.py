# -*- coding: utf-8 -*-
"""持久 Agent 任务队列：状态闭集、租约与 fencing、去重、优先级、阶段计时。

定位（见 plans/ai-deep-intervention-master-plan.md §3.1/§3.2、§4.3）：

- 只负责"任务"这件事 —— 领取、续约、检查点、终态和计时。它不知道策略、不写正式
  账本、不调用模型，也不判断某个候选是否该被采用。
- 存储刻意独立于 `paper_trading.sqlite3`：研究任务的重型写入不能和交易路径争同一个
  SQLite 写锁。正式账本由上层以只读适配器提供冻结输入。
- 一切时间都由调用方注入（`now=`），默认取 UTC。测试用固定时钟复现租约过期、
  重启续跑与乱序事件，不靠 sleep。

不变量：

1. 状态是闭集，未列出的迁移一律拒绝，不用"未知状态"兜底。
2. 每次领取都换 `lease_token` 并递增 `fencing_counter`；持旧 token 的写入被拒绝，
   超时"复活"的旧进程不能覆盖新结果。
3. 去重键只约束活跃状态；终态任务不阻止同键新任务（新事件是新的工作）。
4. 检查点只能前进：重启续跑从已提交 `step/checkpoint` 继续，不重放已完成副作用。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import uuid

import data_paths

DB_FILENAME = "agent_tasks.sqlite3"

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_DEFERRED = "deferred"
STATUS_WAITING_DATA = "waiting_data"
STATUS_WAITING_LABEL = "waiting_label"
STATUS_WAITING_AUTHORIZATION = "waiting_authorization"
STATUS_SUCCEEDED = "succeeded"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

WAITING_STATUSES = (
    STATUS_WAITING_DATA,
    STATUS_WAITING_LABEL,
    STATUS_WAITING_AUTHORIZATION,
)
ACTIVE_STATUSES = (
    STATUS_QUEUED,
    STATUS_CLAIMED,
    STATUS_RUNNING,
    STATUS_DEFERRED,
) + WAITING_STATUSES
TERMINAL_STATUSES = (
    STATUS_SUCCEEDED,
    STATUS_REJECTED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
)
ALL_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES

TRANSITIONS = {
    STATUS_QUEUED: frozenset({STATUS_CLAIMED, STATUS_CANCELLED, STATUS_EXPIRED}),
    STATUS_CLAIMED: frozenset({
        STATUS_RUNNING, STATUS_DEFERRED, STATUS_FAILED,
        STATUS_CANCELLED, STATUS_EXPIRED,
    }),
    STATUS_RUNNING: frozenset({
        STATUS_DEFERRED, STATUS_SUCCEEDED, STATUS_REJECTED, STATUS_FAILED,
        STATUS_CANCELLED, STATUS_EXPIRED,
    }) | frozenset(WAITING_STATUSES),
    STATUS_DEFERRED: frozenset({
        STATUS_CLAIMED, STATUS_CANCELLED, STATUS_FAILED, STATUS_EXPIRED,
    }),
    STATUS_WAITING_DATA: frozenset({
        STATUS_CLAIMED, STATUS_CANCELLED, STATUS_FAILED, STATUS_EXPIRED,
    }),
    STATUS_WAITING_LABEL: frozenset({
        STATUS_CLAIMED, STATUS_CANCELLED, STATUS_FAILED, STATUS_EXPIRED,
    }),
    STATUS_WAITING_AUTHORIZATION: frozenset({
        STATUS_CLAIMED, STATUS_CANCELLED, STATUS_FAILED, STATUS_EXPIRED,
    }),
    STATUS_SUCCEEDED: frozenset(),
    STATUS_REJECTED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_EXPIRED: frozenset(),
}

# 事件优先级（方案 §3.2）：数字越小越先被领取。
PRIORITY_CRITICAL = 0
PRIORITY_HIGH = 1
PRIORITY_MEDIUM = 2
PRIORITY_LOW = 3
PRIORITIES = (PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)

# 阶段词表（方案 §5）：只记录这些名字，避免每个 handler 自造指标让报表失真。
STAGE_QUEUE_WAIT = "queue_wait"
STAGE_SNAPSHOT_READ = "snapshot_read"
STAGE_ARCHIVE_DECODE = "archive_decode"
STAGE_FEATURE_BUILD = "feature_build"
STAGE_PROVIDER_WAIT = "provider_wait"
STAGE_EVALUATOR_CPU = "evaluator_cpu"
STAGE_SQLITE_LOCK_WAIT = "sqlite_lock_wait"
STAGE_PERSIST = "persist"
STAGE_ADMISSION_WAIT = "admission_wait"
STAGES = frozenset({
    STAGE_QUEUE_WAIT, STAGE_SNAPSHOT_READ, STAGE_ARCHIVE_DECODE, STAGE_FEATURE_BUILD,
    STAGE_PROVIDER_WAIT, STAGE_EVALUATOR_CPU, STAGE_SQLITE_LOCK_WAIT, STAGE_PERSIST,
    STAGE_ADMISSION_WAIT,
})

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_TOOL_STEPS = 12
DEFAULT_LEASE_SECONDS = 120.0
_RETRY_BASE_SECONDS = 30.0
_RETRY_MAX_SECONDS = 900.0

_JSON_COLUMNS = ("parent_identity", "payload", "budget", "budget_used", "plan",
                 "checkpoint", "result", "timing")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        priority INTEGER NOT NULL,
        goal TEXT NOT NULL,
        strategy_id TEXT,
        event_version TEXT,
        input_fingerprint TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        status TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        parent_identity TEXT NOT NULL DEFAULT '{}',
        budget TEXT NOT NULL DEFAULT '{}',
        budget_used TEXT NOT NULL DEFAULT '{}',
        plan TEXT,
        step INTEGER NOT NULL DEFAULT 0,
        max_steps INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL,
        checkpoint TEXT,
        next_action TEXT,
        blocking_reason TEXT,
        result TEXT,
        timing TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        available_at TEXT NOT NULL,
        wake_at TEXT,
        claimed_at TEXT,
        finished_at TEXT,
        worker_id TEXT,
        lease_token TEXT,
        lease_expires_at TEXT,
        fencing_counter INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_tasks_active_dedupe
        ON agent_tasks(dedupe_key)
        WHERE status IN ('queued','claimed','running','deferred',
                         'waiting_data','waiting_label','waiting_authorization')
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_tasks_claimable
        ON agent_tasks(status, priority, available_at, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_tasks_strategy
        ON agent_tasks(strategy_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_task_stage_timing (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        stage TEXT NOT NULL,
        duration_ms REAL NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_task_stage_timing_stage
        ON agent_task_stage_timing(stage, recorded_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        reason TEXT,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_task_events_task
        ON agent_task_events(task_id, id)
    """,
)


class TaskQueueError(Exception):
    """队列合同被违反。调用方必须处理，不能当成"再试一次就好"。"""


class UnknownStatus(TaskQueueError):
    pass


class InvalidTransition(TaskQueueError):
    pass


class StaleLease(TaskQueueError):
    """租约已过期或被新 worker 接管：写入方必须放弃，不能重试覆盖。"""


class UnknownStage(TaskQueueError):
    pass


class QueueNotInitialized(TaskQueueError):
    """只读读取时队列尚未建立；不是"空队列"，调用方要分开表述。"""
    pass


def db_path() -> str:
    return data_paths.data_path(DB_FILENAME)


def _now_iso(now=None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value):
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _require_status(status: str) -> str:
    text = str(status or "")
    if text not in ALL_STATUSES:
        raise UnknownStatus(f"未知任务状态: {text!r}")
    return text


def dedupe_key(task_type, strategy_id, event_version, input_fingerprint) -> str:
    """去重键：任务类型 + 目标策略 + 事件版本 + 输入指纹（方案 §3.2）。"""
    material = "|".join([
        str(task_type or ""),
        str(strategy_id or "-"),
        str(event_version or "-"),
        str(input_fingerprint or ""),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def connect(path: str | None = None, *, immediate: bool = False):
    """打开队列库。写入默认走短事务；claim/reclaim 用 BEGIN IMMEDIATE 独占排序。"""
    target = path or db_path()
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def commit(conn) -> None:
    """短事务边界。

    领取、续约、发布各自独立提交 —— 这样模型调用与重计算在事务外进行，
    别的研究任务照样能入队和读写，进程崩了也不会丢掉已提交的领取记录。
    """
    conn.commit()


@contextlib.contextmanager
def connect_readonly(path: str | None = None):
    """只读连接：不建表、不拿写锁。用于状态报表与运维巡检。

    库里还没有表时抛 `QueueNotInitialized` —— 读路径不能顺手创建 schema，
    否则"看一眼状态"就改动了运行时数据。
    """
    target = path or db_path()
    if not os.path.exists(target):
        raise QueueNotInitialized(f"队列库不存在: {target}")
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


def has_table(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone()
    return row is not None



def _open_writer(conn):
    ensure_schema(conn)
    return conn


def _row_to_task(row) -> dict:
    task = dict(row)
    for column in _JSON_COLUMNS:
        if column in task:
            default = None if column in ("plan", "checkpoint", "result") else {}
            task[column] = _load(task[column], default)
    for column in ("attempts", "step", "max_steps", "max_attempts", "fencing_counter"):
        if task.get(column) is not None:
            task[column] = int(task[column])
    if task.get("priority") is not None:
        task["priority"] = int(task["priority"])
    task["is_terminal"] = task.get("status") in TERMINAL_STATUSES
    return task


def _record_event(conn, task_id, from_status, to_status, attempt, reason, now_iso):
    conn.execute(
        "INSERT INTO agent_task_events (task_id, from_status, to_status, attempt, reason, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        (task_id, from_status, to_status, int(attempt or 0),
         (str(reason)[:500] if reason else None), now_iso),
    )


def get(conn, task_id: str):
    if not has_table(conn, "agent_tasks"):
        return None
    row = conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (str(task_id),)).fetchone()
    return _row_to_task(row) if row is not None else None


def find_active_by_dedupe(conn, key: str):
    row = conn.execute(
        "SELECT * FROM agent_tasks WHERE dedupe_key = ? AND status IN ("
        + ",".join("?" * len(ACTIVE_STATUSES)) + ")",
        (key, *ACTIVE_STATUSES),
    ).fetchone()
    return _row_to_task(row) if row is not None else None


def enqueue(
    conn,
    *,
    task_type: str,
    goal: str,
    input_fingerprint: str,
    strategy_id: str | None = None,
    parent_identity: dict | None = None,
    event_version: str | None = None,
    priority: int = PRIORITY_MEDIUM,
    payload: dict | None = None,
    budget: dict | None = None,
    available_at=None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_steps: int = DEFAULT_MAX_TOOL_STEPS,
    now=None,
) -> tuple[dict, bool]:
    """登记任务。相同去重键已有活跃任务时返回既有任务，`created=False`。

    返回 ``(task, created)``。重复事件挂同一个任务，但不会吞掉已经终态的同键任务。
    """
    if not str(task_type or "").strip():
        raise TaskQueueError("task_type 不能为空")
    if not str(input_fingerprint or "").strip():
        raise TaskQueueError("input_fingerprint 不能为空：没有输入指纹就无法判定重复事件")
    if int(priority) not in PRIORITIES:
        raise TaskQueueError(f"未知优先级: {priority!r}")
    if int(max_steps) <= 0:
        raise TaskQueueError("max_steps 必须为正整数")
    if int(max_attempts) <= 0:
        raise TaskQueueError("max_attempts 必须为正整数")
    _open_writer(conn)
    stamp = _now_iso(now)
    key = dedupe_key(task_type, strategy_id, event_version, input_fingerprint)
    existing = find_active_by_dedupe(conn, key)
    if existing is not None:
        return existing, False
    task_id = uuid.uuid4().hex
    values = (
        task_id, str(task_type), int(priority), str(goal), strategy_id, event_version,
        str(input_fingerprint), key, STATUS_QUEUED,
        _dump(payload or {}), _dump(parent_identity or {}), _dump(budget or {}),
        _dump({}), int(max_steps), int(max_attempts), stamp, stamp,
        _now_iso(available_at) if available_at is not None else stamp,
    )
    try:
        conn.execute(
            "INSERT INTO agent_tasks (task_id, task_type, priority, goal, strategy_id,"
            " event_version, input_fingerprint, dedupe_key, status, payload, parent_identity,"
            " budget, budget_used, max_steps, max_attempts, created_at, updated_at, available_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    except sqlite3.IntegrityError:
        # 并发 enqueue 撞上活跃去重键：以先到者为准。
        existing = find_active_by_dedupe(conn, key)
        if existing is None:
            raise
        return existing, False
    _record_event(conn, task_id, None, STATUS_QUEUED, 0, str(goal)[:200], stamp)
    return get(conn, task_id), True


def claim(conn, *, worker_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS, now=None):
    """按优先级领取一个任务。

    可领取的是 `queued`，以及到期可重试的 `deferred/waiting_*`。领取即换租约代号，
    旧持租者的后续检查点写入会被 `StaleLease` 拒绝。
    """
    _open_writer(conn)
    stamp = _now_iso(now)
    waiting_slots = ",".join("?" * len(WAITING_STATUSES))
    for _ in range(5):
        row = conn.execute(
            "SELECT * FROM agent_tasks"
            " WHERE (status IN (?,?) AND available_at <= ?)"
            f"    OR (status IN ({waiting_slots}) AND wake_at IS NOT NULL AND wake_at <= ?)"
            " ORDER BY priority ASC, COALESCE(wake_at, available_at) ASC, created_at ASC"
            " LIMIT 1",
            (STATUS_QUEUED, STATUS_DEFERRED, stamp, *WAITING_STATUSES, stamp),
        ).fetchone()
        if row is None:
            return None
        task = _row_to_task(row)
        token = uuid.uuid4().hex
        expires = _now_iso(
            (_parse_iso(stamp) or dt.datetime.now(dt.timezone.utc))
            + dt.timedelta(seconds=float(lease_seconds))
        )
        wait_ms = 0.0
        claimed_from = _parse_iso(stamp)
        became_claimable = (_parse_iso(task.get("wake_at"))
                            or _parse_iso(task.get("available_at"))
                            or _parse_iso(task.get("created_at")))
        if claimed_from is not None and became_claimable is not None:
            wait_ms = max(0.0, (claimed_from - became_claimable).total_seconds() * 1000.0)
        cursor = conn.execute(
            "UPDATE agent_tasks SET status=?, attempts=attempts+1, worker_id=?, lease_token=?,"
            " lease_expires_at=?, claimed_at=?, updated_at=?, blocking_reason=NULL, wake_at=NULL,"
            " fencing_counter=fencing_counter+1"
            " WHERE task_id=? AND status=? AND fencing_counter=?",
            (STATUS_CLAIMED, str(worker_id), token, expires, stamp, stamp,
             task["task_id"], task["status"], task["fencing_counter"]),
        )
        if cursor.rowcount != 1:
            # 另一个 worker 抢先改了这一行：重读候选，而不是把租约发给一个已被接管的行。
            continue
        attempt = int(task["attempts"]) + 1
        _record_stage(conn, task["task_id"], attempt, STAGE_QUEUE_WAIT, wait_ms, stamp)
        _record_event(conn, task["task_id"], task["status"], STATUS_CLAIMED, attempt, None, stamp)
        claimed = get(conn, task["task_id"])
        if claimed is None:
            return None
        claimed["queue_wait_ms"] = wait_ms
        return claimed
    return None


def _authorize(conn, task_id: str, lease_token: str, *, now=None):
    row = conn.execute(
        "SELECT * FROM agent_tasks WHERE task_id = ?", (str(task_id),)
    ).fetchone()
    if row is None:
        raise StaleLease(f"任务不存在: {task_id}")
    task = _row_to_task(row)
    if not lease_token or str(task.get("lease_token") or "") != str(lease_token):
        raise StaleLease(f"租约代已失效: {task_id}（旧 worker 的写入被拒绝）")
    if task.get("status") not in (STATUS_CLAIMED, STATUS_RUNNING):
        raise StaleLease(f"任务已不处于持有状态: {task_id} -> {task.get('status')}")
    # 到期即失效：不能等到别人回收才承认自己已经没有权限。
    expires = _parse_iso(task.get("lease_expires_at"))
    moment = _parse_iso(_now_iso(now))
    if expires is not None and moment is not None and expires <= moment:
        raise StaleLease(f"租约已过期: {task_id}（{task.get('lease_expires_at')}）")
    return task


def heartbeat(conn, task_id: str, lease_token: str, *,
              extend_seconds: float = DEFAULT_LEASE_SECONDS, now=None,
              commit: bool = True) -> dict:
    """续约。持旧代号的调用在这里就会失败，不必等到写结果才发现。

    默认独立提交：续约发生在长计算期间，那时本来就没有开着的事务。
    在"发布工作单元"里续约时传 ``commit=False`` —— 那时候提交会摧毁保存点。
    """
    _open_writer(conn)
    task = _authorize(conn, task_id, lease_token, now=now)
    stamp = _now_iso(now)
    expires = _now_iso(
        (_parse_iso(stamp) or dt.datetime.now(dt.timezone.utc))
        + dt.timedelta(seconds=float(extend_seconds))
    )
    conn.execute(
        "UPDATE agent_tasks SET lease_expires_at=?, updated_at=? WHERE task_id=?",
        (expires, stamp, task["task_id"]),
    )
    if commit:
        conn.commit()
    return get(conn, task["task_id"])


def verify_lease(conn, task_id: str, lease_token: str, *, now=None) -> dict:
    """写前复核：租约仍属于我、未过期、任务仍在持有态。不满足就抛 ``StaleLease``。"""
    return _authorize(conn, task_id, lease_token, now=now)


def start(conn, task_id: str, lease_token: str, *, plan=None, now=None) -> dict:
    """claimed → running。崩溃在 claim 与 start 之间时，任务停在 claimed 并被租约回收。"""
    _open_writer(conn)
    task = _authorize(conn, task_id, lease_token, now=now)
    stamp = _now_iso(now)
    plan_json = _dump(plan) if plan is not None else None
    if task["status"] == STATUS_CLAIMED:
        _check_transition(task["status"], STATUS_RUNNING)
        conn.execute(
            "UPDATE agent_tasks SET status=?, updated_at=?, plan=COALESCE(?, plan)"
            " WHERE task_id=?",
            (STATUS_RUNNING, stamp, plan_json, task["task_id"]),
        )
        _record_event(conn, task["task_id"], task["status"], STATUS_RUNNING,
                      task["attempts"], None, stamp)
    elif plan is not None:
        conn.execute("UPDATE agent_tasks SET plan=?, updated_at=? WHERE task_id=?",
                     (plan_json, stamp, task["task_id"]))
    return get(conn, task["task_id"])


def _check_transition(current: str, target: str) -> None:
    current = _require_status(current)
    target = _require_status(target)
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"非法状态迁移: {current} -> {target}")


def checkpoint(conn, task_id: str, lease_token: str, *,
               step=None, plan=None, checkpoint=None, next_action=None,
               budget_used=None, timing=None, now=None) -> dict:
    """提交检查点。只会前进：`step` 不允许回退，重启续跑从已提交位置继续。"""
    _open_writer(conn)
    task = _authorize(conn, task_id, lease_token, now=now)
    stamp = _now_iso(now)
    if step is not None:
        new_step = int(step)
        if new_step < int(task.get("step") or 0):
            raise InvalidTransition(
                f"step 不能回退: {task.get('step')} -> {new_step}")
        if new_step > int(task.get("max_steps") or 0):
            raise TaskQueueError(
                f"step {new_step} 超过 max_steps {task.get('max_steps')}")
    used = dict(task.get("budget_used") or {})
    used = _merge_budget(used, budget_used or {})
    timing_total = dict(task.get("timing") or {})
    timing_total = _merge_timing(timing_total, timing or {})
    assignments = ["updated_at=?", "budget_used=?", "timing=?"]
    params = [stamp, _dump(used), _dump(timing_total)]
    if step is not None:
        assignments.append("step=?")
        params.append(int(step))
    if plan is not None:
        assignments.append("plan=?")
        params.append(_dump(plan))
    if checkpoint is not None:
        assignments.append("checkpoint=?")
        params.append(_dump(checkpoint))
    if next_action is not None:
        assignments.append("next_action=?")
        params.append(str(next_action)[:500])
    params.append(task["task_id"])
    conn.execute(f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE task_id=?", params)
    return get(conn, task["task_id"])


def _merge_budget(current, delta) -> dict:
    merged = dict(current or {})
    for key, value in (delta or {}).items():
        existing = merged.get(key)
        if isinstance(existing, (int, float)) and not isinstance(existing, bool) \
                and isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = existing + value
        else:
            merged[key] = value
    return merged


def _merge_timing(current, delta) -> dict:
    merged = {str(k): float(v) for k, v in (current or {}).items()}
    for key, value in (delta or {}).items():
        stage = str(key)
        if stage not in STAGES:
            raise UnknownStage(f"未知阶段: {stage!r}")
        merged[stage] = merged.get(stage, 0.0) + float(value)
    return merged


def _record_stage(conn, task_id, attempt, stage, duration_ms, stamp):
    if stage not in STAGES:
        raise UnknownStage(f"未知阶段: {stage!r}")
    conn.execute(
        "INSERT INTO agent_task_stage_timing (task_id, attempt, stage, duration_ms, recorded_at)"
        " VALUES (?,?,?,?,?)",
        (str(task_id), int(attempt or 0), stage, float(duration_ms), stamp),
    )


def record_stages(conn, task_id, attempt, entries, *, now=None) -> None:
    """记录多个阶段耗时（`entries` 为 `{stage: ms}`）。"""
    _open_writer(conn)
    stamp = _now_iso(now)
    for stage, duration_ms in (entries or {}).items():
        _record_stage(conn, task_id, attempt, stage, duration_ms, stamp)


def _transition(conn, task, target, *, status_reason=None, result=None,
                stamp, budget_used=None, timing=None) -> dict:
    _check_transition(task["status"], target)
    assignments = ["status=?", "updated_at=?"]
    params = [target, stamp]
    if target in TERMINAL_STATUSES:
        assignments += ["finished_at=?", "lease_token=NULL", "lease_expires_at=NULL"]
        params.append(stamp)
    elif target == STATUS_DEFERRED or target in WAITING_STATUSES:
        assignments += ["lease_token=NULL", "lease_expires_at=NULL"]
    if status_reason is not None:
        assignments.append("blocking_reason=?")
        params.append(str(status_reason)[:500])
    if result is not None:
        assignments.append("result=?")
        params.append(_dump(result))
    if budget_used:
        assignments.append("budget_used=?")
        params.append(_dump(_merge_budget(task.get("budget_used") or {}, budget_used)))
    if timing:
        assignments.append("timing=?")
        params.append(_dump(_merge_timing(task.get("timing") or {}, timing)))
    params.append(task["task_id"])
    conn.execute(f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE task_id=?", params)
    _record_event(conn, task["task_id"], task["status"], target, task["attempts"],
                  status_reason, stamp)
    return get(conn, task["task_id"])


def finish(conn, task_id: str, lease_token: str, *, status: str, result=None,
           reason=None, budget_used=None, timing=None, now=None) -> dict:
    """进入终态。部分完成不能记成 `succeeded`（方案 §4.3）。"""
    target = _require_status(status)
    if target not in TERMINAL_STATUSES:
        raise InvalidTransition(f"finish 只接受终态，收到 {target!r}")
    _open_writer(conn)
    task = _authorize(conn, task_id, lease_token, now=now)
    return _transition(conn, task, target, status_reason=reason, result=result,
                       stamp=_now_iso(now), budget_used=budget_used, timing=timing)


def defer(conn, task_id: str, lease_token: str, *, reason: str, retry_at=None,
          retry_in_seconds: float | None = None, now=None) -> dict:
    """延迟重试。超过 `max_attempts` 直接转 `failed`，不留悬空任务。"""
    _open_writer(conn)
    task = _authorize(conn, task_id, lease_token, now=now)
    stamp = _now_iso(now)
    attempts = int(task.get("attempts") or 0)
    if attempts >= int(task.get("max_attempts") or DEFAULT_MAX_ATTEMPTS):
        return _transition(conn, task, STATUS_FAILED,
                           status_reason=f"retry_exhausted:{reason}", stamp=stamp)
    if retry_at is None:
        seconds = retry_in_seconds
        if seconds is None:
            seconds = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)))
        base = _parse_iso(stamp) or dt.datetime.now(dt.timezone.utc)
        retry_at = base + dt.timedelta(seconds=float(seconds))
    conn.execute("UPDATE agent_tasks SET available_at=? WHERE task_id=?",
                 (_now_iso(retry_at), task["task_id"]))
    return _transition(conn, task, STATUS_DEFERRED, status_reason=reason, stamp=stamp)


def awaiting(conn, task_id: str, lease_token: str, *, status: str, reason: str,
             wake_at=None, now=None) -> dict:
    """进入等待类状态（等待数据/标签/授权）。

    这些状态仍占去重键（同一事件不会反复排队），也不是故障：不计入失败重试预算。
    只有给了 `wake_at` 才会被定期重查；否则必须由对应事件显式 `wake()`，
    避免"数据还没到就一直重跑"的空转。
    """
    target = _require_status(status)
    if target not in WAITING_STATUSES:
        raise InvalidTransition(f"awaiting 只接受等待态，收到 {target!r}")
    _open_writer(conn)
    task = _authorize(conn, task_id, lease_token, now=now)
    stamp = _now_iso(now)
    conn.execute("UPDATE agent_tasks SET wake_at=? WHERE task_id=?",
                 (_now_iso(wake_at) if wake_at is not None else None, task["task_id"]))
    return _transition(conn, task, target, status_reason=reason, stamp=stamp)


def wake(conn, task_id: str, *, reason: str = "event", now=None) -> dict:
    """把等待中的任务重新变成可领取（对应事件到来时的显式唤醒）。"""
    _open_writer(conn)
    task = get(conn, task_id)
    if task is None:
        raise TaskQueueError(f"任务不存在: {task_id}")
    if task["status"] not in WAITING_STATUSES:
        return task
    stamp = _now_iso(now)
    conn.execute(
        "UPDATE agent_tasks SET status=?, available_at=?, wake_at=NULL, updated_at=?,"
        " blocking_reason=NULL WHERE task_id=?",
        (STATUS_QUEUED, stamp, stamp, task["task_id"]),
    )
    _record_event(conn, task["task_id"], task["status"], STATUS_QUEUED, task["attempts"],
                  reason, stamp)
    return get(conn, task_id)


def cancel(conn, task_id: str, *, reason: str = "cancelled", lease_token=None, now=None) -> dict:
    """取消任务。授权撤销与预算用尽走这里，不依赖任何模型调用。"""
    _open_writer(conn)
    task = get(conn, task_id)
    if task is None:
        raise TaskQueueError(f"任务不存在: {task_id}")
    if task["status"] in TERMINAL_STATUSES:
        return task
    if task["status"] in (STATUS_CLAIMED, STATUS_RUNNING) and lease_token is not None:
        task = _authorize(conn, task_id, lease_token, now=now)
    return _transition(conn, task, STATUS_CANCELLED, status_reason=reason,
                       stamp=_now_iso(now))


def reclaim_expired(conn, *, now=None) -> list[dict]:
    """回收过期租约。

    仍在活跃集合里的任务被移回可重试状态，并**已**清空租约代号 —— 原进程醒来后
    拿旧代号写检查点会得到 `StaleLease`，不能覆盖新结果。
    """
    _open_writer(conn)
    stamp = _now_iso(now)
    rows = conn.execute(
        "SELECT * FROM agent_tasks WHERE status IN (?,?) AND lease_expires_at IS NOT NULL"
        " AND lease_expires_at <= ?",
        (STATUS_CLAIMED, STATUS_RUNNING, stamp),
    ).fetchall()
    reclaimed = []
    for row in rows:
        task = _row_to_task(row)
        if int(task.get("attempts") or 0) >= int(task.get("max_attempts") or DEFAULT_MAX_ATTEMPTS):
            updated = _transition(conn, task, STATUS_FAILED,
                                  status_reason="lease_expired_retry_exhausted", stamp=stamp)
        else:
            updated = _transition(conn, task, STATUS_DEFERRED,
                                  status_reason="lease_expired", stamp=stamp)
        reclaimed.append(updated)
    return reclaimed


def trim_queue(conn, *, capacity: int, now=None) -> list[str]:
    """队列满时先丢最低优先级、最旧的活跃任务（方案 §3.2）。

    `PRIORITY_CRITICAL` 永不裁剪：授权撤销与保护性动作不能因为排不上队而丢。
    """
    _open_writer(conn)
    if capacity < 0:
        raise TaskQueueError("capacity 不能为负")
    stamp = _now_iso(now)
    rows = conn.execute(
        "SELECT task_id FROM agent_tasks"
        " WHERE status IN (" + ",".join("?" * len(ACTIVE_STATUSES)) + ")"
        "   AND priority > ?"
        " ORDER BY priority DESC, created_at ASC",
        (*ACTIVE_STATUSES, PRIORITY_CRITICAL),
    ).fetchall()
    excess = len(rows) - int(capacity)
    cancelled = []
    for row in rows[:max(0, excess)]:
        task = get(conn, row["task_id"])
        if task is None or task["status"] in TERMINAL_STATUSES:
            continue
        _transition(conn, task, STATUS_CANCELLED, status_reason="queue_capacity", stamp=stamp)
        cancelled.append(task["task_id"])
    return cancelled


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


def stage_report(conn, *, attempt_scope: str = "all", limit: int = 1000) -> dict:
    """按阶段给出 p50/p95，并指出真正最长的那一段等待（W0 的验收要求）。"""
    if not has_table(conn, "agent_task_stage_timing"):
        return {"stages": {}, "longest_waiting_stage": None}
    sql = "SELECT stage, duration_ms FROM agent_task_stage_timing"
    params: list = []
    if attempt_scope == "first_attempt":
        sql += " WHERE attempt = 1"
    elif attempt_scope != "all":
        raise TaskQueueError(f"未知 attempt_scope: {attempt_scope!r}")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    buckets: dict[str, list[float]] = {}
    for row in conn.execute(sql, params).fetchall():
        buckets.setdefault(str(row["stage"]), []).append(float(row["duration_ms"]))
    stages = {}
    for stage, values in buckets.items():
        stages[stage] = {
            "samples": len(values),
            "p50_ms": round(_percentile(values, 0.5) or 0.0, 1),
            "p95_ms": round(_percentile(values, 0.95) or 0.0, 1),
            "max_ms": round(max(values), 1),
            "total_ms": round(sum(values), 1),
        }
    longest = None
    if stages:
        name = max(stages, key=lambda key: stages[key]["total_ms"])
        longest = {"stage": name, **stages[name]}
    return {"stages": stages, "longest_waiting_stage": longest}


def queue_report(conn) -> dict:
    """队列现状：活跃任务按状态/阻塞原因分组，终态按结果统计。"""
    if not has_table(conn, "agent_tasks"):
        return {"active_total": 0, "active_by_status": {}, "blocking_reasons": {},
                "terminal_by_status": {}}
    active_by_status: dict[str, int] = {}
    blocking: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM agent_tasks WHERE status IN ("
        + ",".join("?" * len(ACTIVE_STATUSES)) + ") GROUP BY status",
        ACTIVE_STATUSES,
    ).fetchall():
        active_by_status[str(row["status"])] = int(row["n"])
    for row in conn.execute(
        "SELECT COALESCE(blocking_reason, 'unspecified') AS reason, COUNT(*) AS n"
        " FROM agent_tasks WHERE status IN ("
        + ",".join("?" * len(ACTIVE_STATUSES)) + ") GROUP BY reason ORDER BY n DESC",
        ACTIVE_STATUSES,
    ).fetchall():
        blocking[str(row["reason"])] = int(row["n"])
    terminal_by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM agent_tasks WHERE status IN ("
        + ",".join("?" * len(TERMINAL_STATUSES)) + ") GROUP BY status",
        TERMINAL_STATUSES,
    ).fetchall():
        terminal_by_status[str(row["status"])] = int(row["n"])
    return {
        "active_total": sum(active_by_status.values()),
        "active_by_status": active_by_status,
        "blocking_reasons": blocking,
        "terminal_by_status": terminal_by_status,
    }


def list_tasks(conn, *, status=None, task_type=None, strategy_id=None, limit: int = 50) -> list[dict]:
    if not has_table(conn, "agent_tasks"):
        return []
    sql = "SELECT * FROM agent_tasks WHERE 1=1"
    params: list = []
    if status is not None:
        statuses = [status] if isinstance(status, str) else list(status)
        for value in statuses:
            _require_status(value)
        sql += " AND status IN (" + ",".join("?" * len(statuses)) + ")"
        params.extend(statuses)
    if task_type is not None:
        sql += " AND task_type = ?"
        params.append(str(task_type))
    if strategy_id is not None:
        sql += " AND strategy_id = ?"
        params.append(str(strategy_id))
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    return [_row_to_task(row) for row in conn.execute(sql, params).fetchall()]


def events(conn, task_id: str) -> list[dict]:
    if not has_table(conn, "agent_task_events"):
        return []
    return [dict(row) for row in conn.execute(
        "SELECT * FROM agent_task_events WHERE task_id = ? ORDER BY id", (str(task_id),)
    ).fetchall()]


class StageTimer:
    """handler 内部用的阶段计时器：`with timer.stage(STAGE_PROVIDER_WAIT): ...`。"""

    def __init__(self):
        self.totals: dict[str, float] = {}

    @contextlib.contextmanager
    def stage(self, name: str):
        if name not in STAGES:
            raise UnknownStage(f"未知阶段: {name!r}")
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.totals[name] = self.totals.get(name, 0.0) + elapsed

    def add(self, name: str, duration_ms: float) -> None:
        if name not in STAGES:
            raise UnknownStage(f"未知阶段: {name!r}")
        self.totals[name] = self.totals.get(name, 0.0) + float(duration_ms)

# ---------------------------------------------------------------------------
# 兼容别名：模块内部已全部改用公开名；下面这些只为旧调用方/旧测试保留。
_has_table = has_table
