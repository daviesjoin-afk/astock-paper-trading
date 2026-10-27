# -*- coding: utf-8 -*-
"""Agent 任务执行器：领取 → 续租 → 续跑 → 终态。

分工（方案 §3.1）：队列负责"任务是什么状态"，本模块负责"把一个任务推进一格"，
handler 负责"这件事本身怎么做"。三者都不判断候选是否该被采用 —— 采用走独立的
授权与证据校验链。

handler 合同：

- 签名 ``handler(task, ctx) -> TaskOutcome``。
- **按步幂等**：``task["checkpoint"]`` / ``task["step"]`` 是上次已提交的位置。
  重启续跑会带着它们再次进来，handler 必须从该位置继续，不能重放下游已经提交的副作用。
- 只有可重试错误抛 ``RetryableTaskError``；数据/标签/授权未就绪抛 ``WaitingTaskError``。
  其他异常一律按 `failed` 记账，不伪装成"稍后再试就会好"。
- 想做重型工作就注册 ``heavy=True``：执行前会拿 ``resource_guard`` 的资源租约，
  拿不到时任务转 `deferred`，不去和交易扫描抢 CPU/内存。
"""
from __future__ import annotations

import dataclasses
import os
import time
import uuid

import agent_task_queue as AQ
import resource_guard

REACHABLE_FROM_RUNNING = frozenset({
    AQ.STATUS_SUCCEEDED, AQ.STATUS_REJECTED, AQ.STATUS_FAILED,
    AQ.STATUS_DEFERRED, AQ.STATUS_CANCELLED, AQ.STATUS_EXPIRED,
}) | frozenset(AQ.WAITING_STATUSES)


class TaskError(Exception):
    """handler 用来表达"这次没跑完"的基类。"""


class RetryableTaskError(TaskError):
    """可重试：供应商超时、资源繁忙等。计入 attempts，有上限。"""

    def __init__(self, reason: str, retry_in_seconds: float | None = None):
        super().__init__(reason)
        self.reason = str(reason)
        self.retry_in_seconds = retry_in_seconds


class WaitingTaskError(TaskError):
    """等待类：缺数据、缺标签、缺授权。不是故障，不消耗失败重试预算。"""

    def __init__(self, status: str, reason: str, wake_at=None):
        super().__init__(reason)
        if status not in AQ.WAITING_STATUSES:
            raise AQ.InvalidTransition(f"WaitingTaskError 只接受等待态，收到 {status!r}")
        self.status = status
        self.reason = str(reason)
        self.wake_at = wake_at


@dataclasses.dataclass
class TaskOutcome:
    status: str = AQ.STATUS_SUCCEEDED
    result: dict | None = None
    reason: str | None = None
    plan: dict | None = None
    step: int | None = None
    checkpoint: dict | None = None
    next_action: str | None = None
    budget_used: dict | None = None
    retry_in_seconds: float | None = None
    wake_at: object = None
    #: 可选：把"发布"推迟到执行器的短事务里执行。
    #:
    #: 处理器在事务**外**完成模型调用与计算，把要写的副作用包成 ``publish(conn)``；
    #: 执行器随后在一个保存点里复核租约、执行 publish、写检查点、置终态、提交。
    #: 这样写入窗口只有几毫秒，而计算可以跑几十秒。
    publish: object = None

    def __post_init__(self):
        if self.status not in REACHABLE_FROM_RUNNING:
            raise AQ.InvalidTransition(
                f"outcome 状态不可由 running 到达: {self.status!r}")
        if self.publish is not None and not callable(self.publish):
            raise AQ.TaskQueueError("outcome.publish 必须是可调用对象")


@dataclasses.dataclass(frozen=True)
class HandlerSpec:
    handler: object
    heavy: bool = False


class TaskContext:
    """handler 能看到的全部能力：阶段计时、续租、以及自己的任务快照。"""

    def __init__(self, conn, task, *, worker_id: str, lease_seconds: float, clock):
        self.conn = conn
        self.task = task
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.timer = AQ.StageTimer()
        self.lease_lost = False
        #: 发布工作单元期间为 True：此时续约不能提交，否则会摧毁保存点。
        self.publishing = False

    @property
    def resume_step(self) -> int:
        return int(self.task.get("step") or 0)

    @property
    def resume_checkpoint(self) -> dict:
        return dict(self.task.get("checkpoint") or {})

    @property
    def budget(self) -> dict:
        return dict(self.task.get("budget") or {})

    def stage(self, name):
        return self.timer.stage(name)

    def heartbeat(self):
        """长步骤之间调用。租约已被接管时置 `lease_lost`，handler 应尽快收手。"""
        try:
            AQ.heartbeat(self.conn, self.task["task_id"], self.task.get("lease_token"),
                         extend_seconds=self.lease_seconds, now=self.clock(),
                         commit=not self.publishing)
        except AQ.StaleLease:
            self.lease_lost = True
        return not self.lease_lost


_HANDLERS: dict[str, HandlerSpec] = {}


def register(task_type: str, handler, *, heavy: bool = False) -> None:
    _HANDLERS[str(task_type)] = HandlerSpec(handler=handler, heavy=bool(heavy))


def registered_types() -> list[str]:
    return sorted(_HANDLERS)


def _resolve(task_type: str, handlers):
    table = handlers if handlers is not None else _HANDLERS
    spec = table.get(str(task_type))
    if spec is None:
        raise TaskError(f"没有注册的 handler: {task_type}")
    return spec if isinstance(spec, HandlerSpec) else HandlerSpec(handler=spec)


def _flush_timing(conn, task, ctx: TaskContext, clock) -> bool:
    """把阶段耗时写一次：明细行供分位数，任务 JSON 供单任务追溯。

    租约已被接管时安静放弃 —— 这不是错误，旧 worker 本来就不该再写。
    """
    if not ctx.timer.totals:
        return False
    AQ.record_stages(conn, task["task_id"], task["attempts"], ctx.timer.totals, now=clock())
    try:
        AQ.checkpoint(conn, task["task_id"], task.get("lease_token"),
                      timing=ctx.timer.totals, now=clock())
    except AQ.StaleLease:
        return False
    return True


def _persist_progress(conn, task, outcome: TaskOutcome, ctx: TaskContext, clock) -> None:
    """进度只在这里落一次库：`_apply_outcome` 不再重复携带 timing/budget。"""
    if ctx.timer.totals:
        AQ.record_stages(conn, task["task_id"], task["attempts"], ctx.timer.totals, now=clock())
    if not any([outcome.plan is not None, outcome.step is not None,
                outcome.checkpoint is not None, outcome.next_action is not None,
                outcome.budget_used, ctx.timer.totals]):
        return
    AQ.checkpoint(
        conn, task["task_id"], task.get("lease_token"),
        step=outcome.step,
        plan=outcome.plan,
        checkpoint=outcome.checkpoint,
        next_action=outcome.next_action,
        budget_used=outcome.budget_used,
        timing=ctx.timer.totals,
        now=clock(),
    )


def _apply_outcome(conn, task, outcome: TaskOutcome, ctx: TaskContext, clock) -> dict:
    task_id = task["task_id"]
    token = task.get("lease_token")
    status = outcome.status
    if status in (AQ.STATUS_SUCCEEDED, AQ.STATUS_REJECTED, AQ.STATUS_FAILED, AQ.STATUS_CANCELLED):
        return AQ.finish(conn, task_id, token, status=status, result=outcome.result,
                         reason=outcome.reason, now=clock())
    if status == AQ.STATUS_DEFERRED:
        return AQ.defer(conn, task_id, token, reason=outcome.reason or "deferred",
                        retry_in_seconds=outcome.retry_in_seconds, now=clock())
    return AQ.awaiting(conn, task_id, token, status=status,
                       reason=outcome.reason or status, wake_at=outcome.wake_at, now=clock())


def _record_loss(task, error) -> dict:
    return {
        "task_id": task["task_id"] if task else None,
        "status": "lease_lost",
        "error": f"{type(error).__name__}: {error}",
    }


def run_once(conn, *, worker_id: str | None = None, handlers=None,
             lease_seconds: float = AQ.DEFAULT_LEASE_SECONDS,
             clock=None, heavy_job_name: str = "agent-task") -> dict | None:
    """领取并推进一个任务。没有可领取任务时返回 None。

    `clock` 是返回 datetime 的可注入时钟；测试用它复现租约过期与续跑。
    """
    now_clock = clock or (lambda: None)
    owner = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    task = AQ.claim(conn, worker_id=owner, lease_seconds=lease_seconds, now=now_clock())
    if task is None:
        return None
    # 领取立即提交：处理器里的模型调用与重计算必须在事务外跑，否则会一直占着
    # 研究库的写锁，别的任务连入队都做不到。
    AQ.commit(conn)
    if task["status"] in AQ.TERMINAL_STATUSES:  # pragma: no cover - claim 不会返回终态
        return {"task_id": task["task_id"], "status": task["status"]}
    try:
        spec = _resolve(task["task_type"], handlers)
    except TaskError as exc:
        return _apply_outcome(
            conn, task, TaskOutcome(status=AQ.STATUS_FAILED, reason=str(exc)),
            TaskContext(conn, task, worker_id=owner, lease_seconds=lease_seconds,
                        clock=now_clock), now_clock)
    if spec.heavy:
        with resource_guard.heavy_job_lease(f"{heavy_job_name}:{task['task_type']}") as lease:
            if not lease.get("allowed"):
                return AQ.defer(conn, task["task_id"], task.get("lease_token"),
                                reason=f"resource_{lease.get('reason')}",
                                retry_in_seconds=60.0, now=now_clock())
            return _execute(conn, task, spec, owner, lease_seconds, now_clock)
    return _execute(conn, task, spec, owner, lease_seconds, now_clock)


def _execute(conn, task, spec: HandlerSpec, owner, lease_seconds, clock) -> dict:
    ctx = TaskContext(conn, task, worker_id=owner, lease_seconds=lease_seconds, clock=clock)
    try:
        task = AQ.start(conn, task["task_id"], task.get("lease_token"), now=clock())
        # running 状态也立刻可见：别的进程/运维查询看到的是真实状态，不是 queued。
        AQ.commit(conn)
        ctx.task = task
        # 第一阶段：事务外计算。模型调用、回放、评估都在这里跑，此间不持有任何写锁，
        # 别的任务照样能入队、取消和读取运行状态。
        outcome = spec.handler(task, ctx) or TaskOutcome()
        if not isinstance(outcome, TaskOutcome):
            raise TaskError(f"handler 返回了 {type(outcome).__name__}，需要 TaskOutcome")
        # 第二阶段：短事务发布。复核租约 → 执行 publish → 检查点 → 终态，一次提交。
        final = _publish_work_unit(conn, task, outcome, ctx, clock)
        AQ.commit(conn)
        return final
    except RetryableTaskError as exc:
        _flush_timing(conn, task, ctx, clock)
        result = AQ.defer(conn, task["task_id"], task.get("lease_token"), reason=exc.reason,
                          retry_in_seconds=exc.retry_in_seconds, now=clock())
        AQ.commit(conn)
        return result
    except WaitingTaskError as exc:
        _flush_timing(conn, task, ctx, clock)
        result = AQ.awaiting(conn, task["task_id"], task.get("lease_token"),
                             status=exc.status, reason=exc.reason, wake_at=exc.wake_at,
                             now=clock())
        AQ.commit(conn)
        return result
    except AQ.StaleLease as exc:
        return _record_loss(task, exc)
    except AQ.TaskQueueError as exc:
        # 例如 step 越过 max_tool_steps：任务本身违约，按失败记账而不是无限重试。
        _flush_timing(conn, task, ctx, clock)
        try:
            result = AQ.finish(conn, task["task_id"], task.get("lease_token"),
                               status=AQ.STATUS_FAILED, reason=str(exc), now=clock())
            AQ.commit(conn)
            return result
        except AQ.StaleLease as stale:
            return _record_loss(task, stale)
    except Exception as exc:  # noqa: BLE001 - 失败必须落库成确定终态
        _flush_timing(conn, task, ctx, clock)
        reason = f"{type(exc).__name__}: {exc}"
        try:
            result = AQ.finish(conn, task["task_id"], task.get("lease_token"),
                               status=AQ.STATUS_FAILED, reason=reason[:500], now=clock())
            AQ.commit(conn)
            return result
        except AQ.StaleLease as stale:
            return _record_loss(task, stale)


def _publish_work_unit(conn, task, outcome: TaskOutcome, ctx: TaskContext, clock) -> dict:
    """短事务发布：复核租约 → 执行副作用 → 检查点 → 终态。

    整个窗口在一个保存点里，任何一步失败都整块回滚，不会留下"只有一侧"的结果。
    """
    conn.execute("SAVEPOINT work_unit")
    ctx.publishing = True
    try:
        # 写前复核：租约可能已被接管、任务可能已被取消 —— 那时结果不许落库。
        AQ.verify_lease(conn, task["task_id"], task.get("lease_token"), now=clock())
        extra = outcome.publish(conn) if outcome.publish is not None else None
        if isinstance(extra, dict):
            outcome.result = {**(outcome.result or {}), **extra}
        _persist_progress(conn, task, outcome, ctx, clock)
        final = _apply_outcome(conn, task, outcome, ctx, clock)
    except BaseException:
        _rollback_work_unit(conn)
        raise
    finally:
        ctx.publishing = False
    conn.execute("RELEASE SAVEPOINT work_unit")
    return final


def _rollback_work_unit(conn) -> None:
    """撤掉本工作单元里的全部写入，再让调用方去记失败状态。"""
    try:
        conn.execute("ROLLBACK TO SAVEPOINT work_unit")
        conn.execute("RELEASE SAVEPOINT work_unit")
    except Exception:  # noqa: BLE001 - 保存点可能根本没建立
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def maintenance(conn, *, capacity: int | None = None, clock=None) -> dict:
    """周期巡检：回收过期租约；队列过载时先裁最低优先级的活跃任务。"""
    now_clock = clock or (lambda: None)
    reclaimed = AQ.reclaim_expired(conn, now=now_clock())
    trimmed = AQ.trim_queue(conn, capacity=capacity, now=now_clock()) if capacity is not None else []
    return {
        "reclaimed": [task["task_id"] for task in reclaimed],
        "trimmed": trimmed,
    }


def run_batch(conn, *, max_tasks: int, handlers=None, worker_id: str | None = None,
              capacity: int | None = None, clock=None, idle_sleep: float = 0.0) -> list[dict]:
    """连续推进至多 `max_tasks` 个任务；每轮之间做一次巡检。"""
    results = []
    for _ in range(max(0, int(max_tasks))):
        maintenance(conn, capacity=capacity, clock=clock)
        outcome = run_once(conn, worker_id=worker_id, handlers=handlers, clock=clock)
        if outcome is None:
            if idle_sleep:
                time.sleep(idle_sleep)
            break
        results.append(outcome)
        if outcome.get("status") == "lease_lost":
            break
    return results


def status_snapshot(path=None, *, limit: int = 20) -> dict:
    """只读状态快照：队列、阻塞原因、最长阶段等待和最近的活跃任务。"""
    try:
        with AQ.connect_readonly(path) as conn:
            return {
                "initialized": True,
                "queue": AQ.queue_report(conn),
                "stages": AQ.stage_report(conn),
                "active_tasks": [
                    {key: task.get(key) for key in
                     ("task_id", "task_type", "strategy_id", "status", "priority",
                      "step", "attempts", "blocking_reason", "next_action", "available_at")}
                    for task in AQ.list_tasks(conn, status=list(AQ.ACTIVE_STATUSES), limit=limit)
                ],
                "recent_terminal": [
                    {key: task.get(key) for key in
                     ("task_id", "task_type", "status", "blocking_reason", "finished_at")}
                    for task in AQ.list_tasks(conn, status=list(AQ.TERMINAL_STATUSES), limit=limit)
                ],
            }
    except AQ.QueueNotInitialized:
        return {
            "initialized": False,
            "queue": {"active_total": 0, "active_by_status": {},
                      "blocking_reasons": {}, "terminal_by_status": {}},
            "stages": {"stages": {}, "longest_waiting_stage": None},
            "active_tasks": [],
            "recent_terminal": [],
        }


def _exit_code(results) -> int:
    """runner 的退出码只表达"这一批是否都到了成功终态"（沿用 paper_runner 口径）。"""
    if not results:
        return 0
    return 0 if all(row.get("status") == AQ.STATUS_SUCCEEDED for row in results) else 1


def main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="agent_task_runner")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="只读查看队列、阻塞原因与最长阶段等待")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--path", default=None, help="队列库路径；默认取运行时数据目录")
    run = sub.add_parser("run", help="领取并推进一批已注册的 Agent 任务")
    run.add_argument("--max-tasks", type=int, default=5)
    run.add_argument("--capacity", type=int, default=None)
    run.add_argument("--path", default=None, help="队列库路径；默认取运行时数据目录")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(status_snapshot(args.path, limit=args.limit),
                         ensure_ascii=False, indent=2))
        return 0
    if not registered_types():
        print(json.dumps({"status": "no_handlers",
                          "message": "没有注册任何 task_type handler；W0 只交付队列与执行器骨架"},
                         ensure_ascii=False))
        return 2
    with AQ.connect(args.path) as conn:
        results = run_batch(conn, max_tasks=args.max_tasks, capacity=args.capacity)
    print(json.dumps({"results": results}, ensure_ascii=False))
    return _exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
