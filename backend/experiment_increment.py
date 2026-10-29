# -*- coding: utf-8 -*-
"""实验的持续增量执行：会话级任务接续、原子回执、重启续跑。

方案 F3 的落地形态：**一次会话一个任务**，会话之间由台账里的后继状态衔接。

为什么是一个会话一个任务，而不是"一次跑完整段":

- 单个会话失败不会让整段重跑；重启后从台账里已发布的位置继续。
- 回执在会话边界上原子落库，"已发布"和"已推进"是同一个事实。
- 队列里能看见"下一个该跑哪个会话"，而不是一个几十秒的黑盒调用。

幂等边界：

- 重复事件用 ``AQ`` 的去重键收敛成一个任务（同 experiment + 同 environment +
  同 session ⇒ 同任务）。
- 已经发布过的会话再被领取，直接返回 ``succeeded`` 且**不重新发布**回执。
- 同一个回执键上出现不同内容 → ``ReceiptConflict``，不静默覆盖。
"""
from __future__ import annotations

import hashlib
import json

import agent_task_queue as AQ
import agent_task_runner as AR
import native_paired_replay as NPR
import replay_receipt_ledger as RL

TASK_TYPE_SESSION = "paired_replay_session"
WORKER_VERSION = "experiment-increment-v1"

R_RUN_MISSING = "experiment_run_missing"
R_SESSION_OUT_OF_RANGE = "session_index_out_of_range"
R_PREVIOUS_NOT_PUBLISHED = "previous_session_not_published"
R_INCOMPLETE_RECEIPT_PAIR = "incomplete_receipt_pair"
ALREADY_PUBLISHED = "already_published"


class IncrementError(ValueError):
    """增量编排合同违约。"""


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def _state_payload(state: NPR.SideState) -> dict:
    return {"cash": state.cash,
            "lots": [[lot.code, lot.quantity, lot.entry_session, lot.entry_price,
                      lot.entry_index] for lot in state.lots],
            "fees_paid": state.fees_paid, "realized_pnl": state.realized_pnl}


def _state_from_payload(payload, *, reference_capital: float) -> NPR.SideState:
    if not payload:
        return NPR.SideState.initial(reference_capital)
    lots = tuple(NPR.Lot(code=str(row[0]), quantity=int(row[1]),
                         entry_session=str(row[2]), entry_price=float(row[3]),
                         entry_index=int(row[4]) if len(row) > 4 else 0)
                 for row in payload.get("lots") or ())
    return NPR.SideState(cash=float(payload["cash"]), lots=lots,
                         fees_paid=float(payload.get("fees_paid") or 0.0),
                         realized_pnl=float(payload.get("realized_pnl") or 0.0))


def _sides_from_run(run) -> tuple[NPR.SideParameters, NPR.SideParameters]:
    def build(payload, label):
        return NPR.SideParameters(
            label=label, model_family=payload["model_family"],
            condition_overrides=tuple(sorted(
                (payload.get("condition_overrides") or {}).items())),
            weight_overrides=(None if payload.get("weight_overrides") is None
                              else tuple(sorted(payload["weight_overrides"].items()))))

    return (build(run["champion"], NPR.SIDE_CHAMPION),
            build(run["challenger"], NPR.SIDE_CHALLENGER))


def _rules_from_run(run) -> NPR.ReplayRules:
    return NPR.ReplayRules(**run["rules"])


def _session_payloads(environment: NPR.ReplayEnvironment) -> list[dict]:
    return [NPR.session_payload(session) for session in environment.sessions]


def enqueue_experiment(conn, *, experiment_id: str, target_identity: str,
                       environment: NPR.ReplayEnvironment, champion: NPR.SideParameters,
                       challenger: NPR.SideParameters, rules: NPR.ReplayRules,
                       now=None) -> dict:
    """登记一次实验并投递第一个会话任务。重复调用是幂等的。"""
    RL.ensure_schema(conn)
    created = RL.record_run(
        conn, experiment_id=experiment_id, target_identity=target_identity,
        environment_hash=environment.environment_hash,
        champion=champion.identity(), challenger=challenger.identity(),
        rules=rules.identity(), sessions=_session_payloads(environment), now=now)
    task = ensure_next_session_enqueued(conn, experiment_id, now=now)
    return {"experiment_id": experiment_id, "run_recorded": created,
            "next_task": None if task is None else task["task_id"]}


def pending_sessions(conn, experiment_id: str) -> list[int]:
    run = RL.get_run(conn, experiment_id)
    if run is None:
        raise IncrementError(R_RUN_MISSING)
    published = RL.published_sessions(conn, experiment_id)
    return [index for index, payload in enumerate(run["sessions"])
            if str(payload["session_date"]) not in published]


def ensure_next_session_enqueued(conn, experiment_id: str, *, now=None):
    """保证"下一个未发布的会话"恰好有一个待办任务。全部发布完返回 None。"""
    run = RL.get_run(conn, experiment_id)
    if run is None:
        raise IncrementError(R_RUN_MISSING)
    pending = pending_sessions(conn, experiment_id)
    if not pending:
        return None
    index = pending[0]
    session_payload = run["sessions"][index]
    task, _created = AQ.enqueue(
        conn, task_type=TASK_TYPE_SESSION,
        goal=f"推进会话 {session_payload['session_date']} 的成对回放",
        input_fingerprint=_fingerprint({"experiment_id": experiment_id,
                                        "environment_hash": run["environment_hash"],
                                        "session_date": session_payload["session_date"]}),
        event_version=str(session_payload["session_date"]),
        payload={"experiment_id": experiment_id, "session_index": index},
        budget={"tool_steps": 12},
        now=now)
    if task.get("status") in AQ.WAITING_STATUSES:
        # 去重命中了一个"等前序会话"的任务：前序现在已发布，必须显式唤醒它，
        # 否则它永远停在等待态，链条就断在这里。
        task = AQ.wake(conn, task["task_id"], reason="predecessor_published", now=now)
    return task


def handle_session_task(task, ctx):
    """一个会话的完整推进：续状态 → 成对回放 → 原子发回执 → 投递下一会话。"""
    payload = dict(task.get("payload") or {})
    experiment_id = str(payload.get("experiment_id") or "")
    index = int(payload.get("session_index") or 0)
    conn = ctx.conn
    run = RL.get_run(conn, experiment_id)
    if run is None:
        return AR.TaskOutcome(status=AQ.STATUS_FAILED, reason=R_RUN_MISSING)
    sessions = run["sessions"]
    if not 0 <= index < len(sessions):
        return AR.TaskOutcome(status=AQ.STATUS_FAILED, reason=R_SESSION_OUT_OF_RANGE)
    session_payload = sessions[index]
    session_date = str(session_payload["session_date"])

    published = RL.published_sessions(conn, experiment_id)
    if session_date in published:
        # 幂等：已经发布过就不再发布一次，也不重算状态。
        ensure_next_session_enqueued(conn, experiment_id, now=ctx.clock())
        return AR.TaskOutcome(status=AQ.STATUS_SUCCEEDED,
                              reason=ALREADY_PUBLISHED,
                              result={"session_date": session_date,
                                      "already_published": True})

    if index > 0:
        previous = str(sessions[index - 1]["session_date"])
        if previous not in published:
            # 顺序是硬约束：状态必须从上一步续出来，不能跳步重算。
            return AR.TaskOutcome(status=AQ.STATUS_WAITING_DATA,
                                  reason=R_PREVIOUS_NOT_PUBLISHED)

    # 半侧残留说明过去某次发布没跑完原子：先修数据，不能在残缺证据上继续推进。
    residue = RL.incomplete_sessions(conn, experiment_id)
    if residue:
        return AR.TaskOutcome(status=AQ.STATUS_FAILED,
                              reason=f"{R_INCOMPLETE_RECEIPT_PAIR}:{sorted(residue)}",
                              result={"incomplete_sessions": residue})

    rules = _rules_from_run(run)
    champion, challenger = _sides_from_run(run)
    champion_state = _state_from_payload(
        (RL.latest_state(conn, experiment_id, NPR.SIDE_CHAMPION) or {}).get("state"),
        reference_capital=rules.reference_capital)
    challenger_state = _state_from_payload(
        (RL.latest_state(conn, experiment_id, NPR.SIDE_CHALLENGER) or {}).get("state"),
        reference_capital=rules.reference_capital)

    session = NPR.session_from_payload(session_payload)
    try:
        paired = NPR.run_paired_session(
            session=session, index=index, champion_state=champion_state,
            challenger_state=challenger_state, champion=champion, challenger=challenger,
            rules=rules, select=NPR.strategies_selector(),
            experiment_id=experiment_id, target_identity=run["target_identity"],
            environment_hash=run["environment_hash"])
    except NPR.ReplayRefused as exc:
        reason = str(exc)
        if reason.startswith(NPR.R_MISSING_POSITION_QUOTE):
            # 缺持仓估值是数据还没到，不是任务失败：保留状态等报价，不要伪造估值。
            return AR.TaskOutcome(status=AQ.STATUS_WAITING_DATA, reason=reason)
        return AR.TaskOutcome(status=AQ.STATUS_FAILED, reason=reason)

    # 写回执与投递下一会话属于"发布"，交给执行器的短事务执行：
    # 上面的回放是纯计算，不该在持有写锁的情况下跑。
    def publish(conn):
        RL.publish_pair(
            conn, champion=paired.receipts[0], challenger=paired.receipts[1],
            champion_step=paired.champion_step.projection(),
            challenger_step=paired.challenger_step.projection(),
            champion_state=_state_payload(paired.champion_state),
            challenger_state=_state_payload(paired.challenger_state),
            now=ctx.clock())
        ensure_next_session_enqueued(conn, experiment_id, now=ctx.clock())
        return {"receipts": [item.receipt_fingerprint for item in paired.receipts]}

    return AR.TaskOutcome(
        status=AQ.STATUS_SUCCEEDED,
        result={"session_date": session_date, "session_index": index},
        # 工具步数是"这个任务用了几步"，不是实验第几天。把会话序号写进 step
        # 会让第 13 个会话直接撞上 max_tool_steps=12 的工具预算。
        step=1,
        checkpoint={"published_session": session_date,
                    "session_index": index,
                    "champion_nav": paired.champion_step.nav,
                    "challenger_nav": paired.challenger_step.nav},
        next_action="enqueue_next_session",
        publish=publish)



def register_handlers() -> None:
    AR.register(TASK_TYPE_SESSION, handle_session_task)


def main(argv=None) -> int:
    """运维入口：查看某次实验的增量进度。只读，不推进任何任务。"""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="experiment_increment")
    sub = parser.add_subparsers(dest="command", required=True)
    progress = sub.add_parser("progress", help="查看实验的已发布/待跑会话")
    progress.add_argument("--experiment", required=True)
    progress.add_argument("--path", default=None, help="研究库路径；默认取运行时数据目录")
    args = parser.parse_args(argv)
    try:
        with AQ.connect_readonly(args.path) as conn:
            payload = experiment_progress(conn, args.experiment)
    except AQ.QueueNotInitialized:
        payload = {"experiment_id": args.experiment, "initialized": False}
    except IncrementError as exc:
        print(json.dumps({"experiment_id": args.experiment, "error": str(exc)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def experiment_progress(conn, experiment_id: str) -> dict:
    """只读进度：已发布/待跑会话、下一会话、两侧净值序列。"""
    run = RL.get_run(conn, experiment_id)
    if run is None:
        raise IncrementError(R_RUN_MISSING)
    sessions = [str(payload["session_date"]) for payload in run["sessions"]]
    published = RL.published_sessions(conn, experiment_id)
    rows = RL.receipts(conn, experiment_id)
    navs: dict[str, list[list]] = {NPR.SIDE_CHAMPION: [], NPR.SIDE_CHALLENGER: []}
    for row in rows:
        step = row.get("step") or {}
        if row["side"] in navs:
            navs[row["side"]].append([row["session_date"], step.get("nav")])
    pending = [day for day in sessions if day not in published]
    residue = RL.incomplete_sessions(conn, experiment_id)
    active = AQ.list_tasks(conn, task_type=TASK_TYPE_SESSION, limit=500)
    failed = [task for task in active
              if task.get("status") in (AQ.STATUS_FAILED, AQ.STATUS_REJECTED)
              and str((task.get("payload") or {}).get("experiment_id")) == experiment_id]
    return {
        "experiment_id": experiment_id,
        "target_identity": run["target_identity"],
        "environment_hash": run["environment_hash"],
        "sessions_total": len(sessions),
        "sessions_published": len(sessions) - len(pending),
        "pending_sessions": pending,
        "next_session": pending[0] if pending else None,
        # "跑完了"不等于"证据完整"：半侧残留与失败任务都要单独呈现。
        "complete": not pending and not residue and not failed,
        "incomplete_receipt_sessions": residue,
        "failed_tasks": [{"task_id": task["task_id"],
                          "blocking_reason": task.get("blocking_reason")}
                         for task in failed],
        "nav_series": navs,
        "receipt_count": len(rows),
    }


if __name__ == "__main__":
    raise SystemExit(main())

# ---------------------------------------------------------------------------
# 兼容别名：模块内部已全部改用公开名；下面这些只为旧调用方/旧测试保留。
_handle_session_task = handle_session_task
