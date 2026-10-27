# -*- coding: utf-8 -*-
"""生产业务事件到持久研究任务的窄入口。

交易/学习 owner 只提交已经存在的事实；本模块独立打开研究库，不持有账本写锁。
所有异常由 ``submit_safe`` 变成可观测结果，绝不改变规则交易结论。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import agent_task_queue as AQ

EVENT_MARKET = "market"
EVENT_FILL = "fill"
EVENT_LABEL = "label"
EVENT_EXPERIMENT = "experiment"
WAITING_EVENT_TYPES = (EVENT_MARKET, EVENT_FILL, EVENT_LABEL, EVENT_EXPERIMENT)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                      separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def submit(conn, event) -> dict:
    """持久化一个事件，并按同一事务唤醒匹配的等待任务。

    ``event`` 至少含 kind/strategy_id/observed_at；code 可用于股票级事件。
    事件 id 是内容指纹，重复提交幂等，不制造额外模型工作。
    """
    import ai_research_agent as AGENT
    import experiment_target_manifest as ETM
    import research_candidate as RC

    body = dict(event or {})
    kind = str(body.get("kind") or "").strip()
    strategy_id = str(body.get("strategy_id") or "tq_breakout")
    if kind not in WAITING_EVENT_TYPES:
        raise ValueError(f"unknown_research_event:{kind}")
    observed_at = str(body.get("observed_at") or "").strip()
    if not observed_at:
        raise ValueError("observed_at_required")
    event_id = fingerprint(body)
    AQ.ensure_schema(conn)
    RC.ensure_schema(conn)
    if not _has_table(conn, "research_business_events"):
        conn.execute(
            """CREATE TABLE research_business_events (
                event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, strategy_id TEXT NOT NULL,
                code TEXT, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
    created = conn.execute(
        "INSERT OR IGNORE INTO research_business_events"
        " (event_id,kind,strategy_id,code,observed_at,payload,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (event_id, kind, strategy_id, body.get("code"), observed_at, _canonical(body),
         dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")),
    ).rowcount == 1
    woken = []
    for task in AQ.list_tasks(conn, status=list(AQ.WAITING_STATUSES), limit=1000):
        wait = dict((task.get("payload") or {}).get("wait_subscription") or {})
        if not wait:
            continue
        if str(wait.get("kind")) != kind:
            continue
        if wait.get("strategy_id") and str(wait["strategy_id"]) != strategy_id:
            continue
        if wait.get("code") and str(wait["code"]) != str(body.get("code") or ""):
            continue
        AQ.wake(conn, task["task_id"], reason=f"business_event:{kind}:{event_id}")
        woken.append(task["task_id"])

    task = None
    task_created = False
    # 没有冻结证据就不开研究任务：事件仍然记录并唤醒等待者，但不给模型
    # 一个"没有行情可回放"的空任务。
    if created and kind in (EVENT_MARKET, EVENT_LABEL) and body.get("frozen_sessions"):
        # 只有存在受支持目标清单的策略才产生模型任务；其余策略仍记录事件，
        # 但不会为它们凭空造一个"可研究字段"。
        try:
            manifest = ETM.manifest_for(strategy_id, ETM.TARGET_SELECTION_WEIGHTS,
                                        "first_board_relay")
        except ETM.ManifestError:
            manifest = None
        if manifest is not None:
            try:
                RC.validate_frozen_sessions(body["frozen_sessions"])
            except (ValueError, TypeError, KeyError) as exc:
                return {"event_id": event_id, "created": created, "task_id": None,
                        "task_created": False, "woken_task_ids": woken,
                        "task_blocked_reason": str(exc), "status": "recorded"}
            identity = dict(body.get("exact_identity") or {
                "strategy_id": strategy_id, "strategy_version": 1,
                "strategy_checksum": body.get("strategy_checksum") or "runtime-current",
            })
            task, task_created = AGENT.enqueue_research_question(
                conn,
                goal=f"用冻结证据检验 {strategy_id} 的 flow 权重是否值得进入 ADVISORY 研究",
                hypothesis="flow 权重的有界变化在同环境扣费回放中存在可观察差异",
                exact_identity=identity,
                input_fingerprint=f"business:{event_id}",
                slot=str(body.get("slot") or "ai1"),
                payload_extra={
                    "event_id": event_id,
                    "research_target": {
                        "strategy_id": strategy_id,
                        "model_family": manifest.model_family,
                        "target_kind": manifest.target_kind,
                        "field": RC.SUPPORTED_FIELD,
                        "baseline": RC.baseline(manifest),
                        "min": RC.MIN_FLOW, "max": RC.MAX_FLOW,
                        "max_delta": RC.MAX_DELTA,
                    },
                    # 冻结会话只随事件走：模型工具不能自带行情，只能消费这里的一份。
                    "frozen_sessions": list(body.get("frozen_sessions") or ()),
                    "environment_ref": body.get("environment_ref"),
                },
                now=body.get("observed_at"),
            )
    return {"event_id": event_id, "created": created,
            "task_id": None if task is None else task["task_id"],
            "task_created": task_created, "woken_task_ids": woken,
            "status": "recorded"}


def submit_safe(event, *, queue_path=None, ledger_conn=None) -> dict:
    """生产 fail-open 入口：研究失败只留审计，不干扰调用方的规则决策。"""
    try:
        with AQ.connect(queue_path) as conn:
            result = submit(conn, event)
        if ledger_conn is not None:
            _audit(ledger_conn, "research_event_recorded", result)
        return result
    except Exception as exc:  # noqa: BLE001 - 研究隔离边界
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        if ledger_conn is not None:
            try:
                _audit(ledger_conn, "research_event_failed", result)
            except Exception:
                pass
        return result


def _audit(conn, event, detail):
    import paper_repository as R
    R.audit(conn, None, event, _canonical(detail),
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"))


def completed_experiment_event(conn, experiment_id: str) -> dict:
    """worker 在成对回执提交后投递完成事件，唤醒正在等实验的模型任务。"""
    import experiment_increment as EI
    import research_candidate as RC

    progress = EI.experiment_progress(conn, experiment_id)
    if not progress["complete"]:
        return {"status": "waiting_experiment", "experiment_id": experiment_id}
    candidate = RC.get(conn, experiment_id)
    if candidate is None:
        return {"status": "not_a_research_candidate", "experiment_id": experiment_id}
    # 观察时刻取最后一条已发布回执的会话日；没有回执时退回当前时刻。
    stamps = [str(row[0]) for rows in (progress.get("nav_series") or {}).values()
              for row in rows or () if row]
    observed_at = max(stamps) if stamps else dt.datetime.now(
        dt.timezone.utc).isoformat(timespec="seconds")
    return submit(conn, {
        "kind": EVENT_EXPERIMENT, "experiment_id": experiment_id,
        "strategy_id": candidate["strategy_id"], "observed_at": observed_at,
        "progress": progress,
    })
