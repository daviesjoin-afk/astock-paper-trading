# -*- coding: utf-8 -*-
"""受限研究候选：首版只允许 tq_breakout 的真实 selection weight ``flow``。

这里只创建研究候选和成对回放，不读取/修改正式账户参数，更没有采用权限。
"""
from __future__ import annotations

import json
import uuid

import agent_task_queue as AQ
import experiment_increment as EI
import experiment_target_manifest as ETM
import native_paired_replay as NPR

SUPPORTED_STRATEGY = "tq_breakout"
SUPPORTED_FIELD = "flow"
MIN_FLOW = 0.05
MAX_FLOW = 0.45
MAX_DELTA = 0.05
TABLE = "research_flow_candidates"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
R_FROZEN_SESSIONS_REQUIRED = "frozen_event_sessions_required"
R_MODEL_SUPPLIED_SESSIONS = "model_must_not_supply_market_sessions"


def ensure_schema(conn):
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            experiment_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
            model_family TEXT NOT NULL, field TEXT NOT NULL,
            baseline REAL NOT NULL, variant REAL NOT NULL,
            environment_hash TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")


def baseline(manifest=None):
    manifest = manifest or ETM.manifest_for(
        SUPPORTED_STRATEGY, ETM.TARGET_SELECTION_WEIGHTS, "first_board_relay")
    return float(dict(manifest.baseline_defaults)[SUPPORTED_FIELD])


def get(conn, experiment_id):
    ensure_schema(conn)
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE experiment_id=?",
                       (str(experiment_id),)).fetchone()
    return None if row is None else dict(row)


def mark_complete(conn, experiment_id) -> bool:
    ensure_schema(conn)
    return conn.execute(
        f"UPDATE {TABLE} SET status=? WHERE experiment_id=? AND status=?",
        (STATUS_COMPLETE, str(experiment_id), STATUS_RUNNING)).rowcount == 1


def _frozen_sessions(frozen_sessions):
    """事件冻结的会话是**唯一**允许的行情来源；空集合同样拒绝。"""
    if not isinstance(frozen_sessions, (list, tuple)) or not frozen_sessions:
        raise ValueError(R_FROZEN_SESSIONS_REQUIRED)
    sessions = []
    for item in frozen_sessions:
        sessions.append(NPR.session_from_payload(dict(item or {})))
    return sessions


def create_and_enqueue(conn, arguments, *, task_id=None, frozen_sessions=None) -> dict:
    """模型可调用的唯一写工具；所有字段、幅度、证据来源在此受限。

    ``frozen_sessions`` 来自业务事件已经冻结的行情。模型自己给的 ``sessions``
    一律拒绝：研究工作可以挑参数，不能挑行情。
    """
    args = dict(arguments or {})
    strategy_id = str(args.get("strategy_id") or SUPPORTED_STRATEGY)
    model_family = str(args.get("model_family") or "first_board_relay")
    if strategy_id != SUPPORTED_STRATEGY or str(args.get("field") or SUPPORTED_FIELD) != SUPPORTED_FIELD:
        raise ValueError("only_tq_breakout_selection_flow_is_supported")
    target = ETM.manifest_for(strategy_id, ETM.TARGET_SELECTION_WEIGHTS, model_family)
    ETM.check_fields_allowed(target, (SUPPORTED_FIELD,))
    base = baseline(target)
    try:
        variant = float(args["variant"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("finite_flow_variant_required") from exc
    if not MIN_FLOW <= variant <= MAX_FLOW or not 1e-9 < abs(variant - base) <= MAX_DELTA:
        raise ValueError(f"flow_variant_out_of_bounds:{base}:{MIN_FLOW}:{MAX_FLOW}:{MAX_DELTA}")

    if args.get("sessions") not in (None, (), []):
        raise ValueError(R_MODEL_SUPPLIED_SESSIONS)
    sessions = _frozen_sessions(frozen_sessions)
    environment = NPR.ReplayEnvironment(sessions=tuple(sessions))

    champion, challenger = NPR.sides_from_manifest(
        strategy_id, target_kind=ETM.TARGET_SELECTION_WEIGHTS,
        model_family=model_family, variant_fields={SUPPORTED_FIELD: variant})
    rules = NPR.ReplayRules(
        reference_capital=float(args.get("reference_capital") or 100_000.0),
        max_positions=int(args.get("max_positions") or 3),
        hold_sessions=int(args.get("hold_sessions") or 1),
        topn=int(args.get("topn") or 3))
    experiment_id = str(args.get("experiment_id") or f"flow-{uuid.uuid4().hex}")
    ensure_schema(conn)
    conn.execute(
        f"INSERT INTO {TABLE} (experiment_id,strategy_id,model_family,field,baseline,variant,"
        " environment_hash,status) VALUES (?,?,?,?,?,?,?,?)",
        (experiment_id, strategy_id, model_family, SUPPORTED_FIELD, base, variant,
         environment.environment_hash, STATUS_RUNNING))
    target_identity = json.dumps({
        "candidate": experiment_id, "strategy_id": strategy_id,
        "model_family": model_family, "field": SUPPORTED_FIELD,
        "baseline": base, "variant": variant,
    }, sort_keys=True, ensure_ascii=False)
    EI.enqueue_experiment(
        conn, experiment_id=experiment_id, target_identity=target_identity,
        environment=environment, champion=champion, challenger=challenger,
        rules=rules)
    return {"experiment_id": experiment_id, "strategy_id": strategy_id,
            "field": SUPPORTED_FIELD, "baseline": base, "variant": variant,
            "environment_hash": environment.environment_hash,
            "sessions": len(sessions), "sessions_source": "research_event",
            "status": "queued", "task_id": task_id}


def result(conn, experiment_id) -> dict:
    progress = EI.experiment_progress(conn, str(experiment_id))
    candidate = get(conn, experiment_id)
    if candidate is None:
        raise ValueError("research_candidate_missing")
    return {**progress, "candidate": candidate,
            "historical_replay_only": True,
            "promotion_eligible": False,
            "interpretation": "ADVISORY evidence only; no live adoption authority"}


def wake_waiters(conn, experiment_id: str) -> list[str]:
    """实验完成后唤醒等待该实验结果的持久研究任务。"""
    woken = []
    for task in AQ.list_tasks(conn, status=list(AQ.WAITING_STATUSES), limit=1000):
        wait = dict((task.get("payload") or {}).get("wait_subscription") or {})
        if wait.get("kind") == "experiment" and str(wait.get("experiment_id")) == str(experiment_id):
            AQ.wake(conn, task["task_id"], reason=f"experiment_complete:{experiment_id}")
            woken.append(task["task_id"])
    return woken
