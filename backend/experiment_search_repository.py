# -*- coding: utf-8 -*-
"""R36-A —— 拥有 experiment search **control plane** 的持久化台账。

一句话职责：拥有 ``experiment_search_runs`` / ``experiment_search_jobs`` /
``experiment_search_job_events`` 三张 append-only 表。

它**不**拥有：

* candidate identity（:mod:`strategy_candidate`）；
* 实验事实与结论（R28/R29/R30）；
* selection / ranking / promotion（R36-C / R31）。

─────────────── 为什么 queue 状态必须由 event_seq 推导 ───────────────

R35 的 proposal history 刻意**没有** latest：proposal 是业务历史，没有 ordering
authority，同 timestamp 下随机 opaque id 的先后不代表任何真实顺序。

R36 的 job queue **需要** current operational state，因此这里显式建立
``event_seq INTEGER PRIMARY KEY AUTOINCREMENT`` 作为**唯一** ordering authority。于是
current state 可以定义为"该 job 的 ``MAX(event_seq)`` 事件"，而不是靠 ``created_at``
或 ``event_id`` 字典序去猜。

必须始终记住：这是 **operational queue projection**，不是实验业务结果 authority。
``completed`` 的语义要求 executor 产生可核验的外部 evidence，绝不表示 candidate passed /
good / promotable。R36-A 尚无这种绑定，当前写入接口一律拒绝 completed。

─────────────── 没有可变状态列，也没有指标列 ───────────────

三张表都没有 ``status`` / ``attempts`` / ``claimed_at`` 之类的可变快照列 —— 留一个就会
立刻出现"append-only 证据 + 可变快照"两套 authority。也没有任何实验指标列
（``sharpe`` / ``return`` / ``drawdown`` / ``rank`` / ``score`` / ``promotion`` /
``winner``）：那些事实属于 R29/R30 evidence 与 R36-C selection policy。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping

import experiment_search_contract as ESC

__all__ = [
    "ExperimentSearchRepositoryError",
    "FORBIDDEN_CONTROLLER_FIELDS",
    "get_search_run",
    "list_job_events",
    "list_run_events",
    "list_search_jobs",
    "project_job_states",
    "record_job",
    "record_job_event",
    "record_search_run",
]

#: 三张表都不允许出现的实验指标 / 结论字段。写入前逐层检查，出现即拒绝：
#: 它们属于 R29/R30 evidence 与 R36-C selection，不属于 control plane。
FORBIDDEN_CONTROLLER_FIELDS = frozenset({
    "total_return", "sharpe", "sharpe_ratio", "drawdown", "max_drawdown",
    "volatility", "win_rate", "rank", "ranking", "score", "promotion",
    "promote", "winner", "expected_return", "alpha", "pnl",
})


class ExperimentSearchRepositoryError(ValueError):
    """Stable rejection from the search control-plane repository."""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject_metric_fields(material: Mapping, *, where: str) -> None:
    present = sorted(set(material) & FORBIDDEN_CONTROLLER_FIELDS)
    if present:
        raise ExperimentSearchRepositoryError(
            f"controller_must_not_store_evaluation_metrics:{where}:{present[0]}")


def _event_identity(created_at: str | None) -> tuple[str, str]:
    """事件身份与持久化时刻。

    事件身份是 **opaque CSPRNG**：同一件事重复记录必须是两个事件，因此绝不能用
    timestamp / 计数器 / 内容 hash 当唯一性来源。``created_at`` 只是持久化元数据。
    """
    return secrets.token_hex(32), (created_at or _now())


# ---------------------------------------------------------------------------
# search run
# ---------------------------------------------------------------------------


def record_search_run(conn: sqlite3.Connection, *, spec: ESC.ExperimentSearchSpec,
                      run_id: str, created_at: str | None = None) -> str:
    """Append one search run (append-only, idempotent on exact content).

    ``search_run_id`` 由调用方提供（opaque event identity）；``search_input_fingerprint``
    来自 SearchSpec（content identity）。同一个 run id 再用**不同**内容写入即冲突 ——
    绝不静默覆盖。
    """
    if not isinstance(spec, ESC.ExperimentSearchSpec):
        raise ExperimentSearchRepositoryError("canonical_experiment_search_spec_required")
    run_id = str(run_id or "")
    if not ESC.is_search_identity(run_id):
        raise ExperimentSearchRepositoryError("invalid_search_run_id")
    spec_json = _canonical(spec.projection())
    _reject_metric_fields(spec.projection(), where="search_run")
    payload = {
        "search_run_id": run_id,
        "search_input_fingerprint": spec.search_input_fingerprint,
        "search_contract_version": spec.search_contract_version,
        "generation_batch_id": spec.generation_batch_id,
        "generation_input_fingerprint": spec.generation_input_fingerprint,
        "candidate_count": spec.candidate_count,
        "budget_json": _canonical(spec.budget.projection()),
        "search_spec_json": spec_json,
        "created_at": created_at or _now(),
    }
    payload["payload_fingerprint"] = _fingerprint(payload)
    columns = ("search_run_id", "search_input_fingerprint", "search_contract_version",
               "generation_batch_id", "generation_input_fingerprint",
               "candidate_count", "budget_json", "search_spec_json",
               "created_at", "payload_fingerprint")
    conn.execute(
        f"INSERT OR IGNORE INTO experiment_search_runs({','.join(columns)})"
        f" VALUES({','.join('?' for _ in columns)})",
        tuple(payload[column] for column in columns),
    )
    row = conn.execute(
        "SELECT payload_fingerprint FROM experiment_search_runs WHERE search_run_id=?",
        (run_id,)).fetchone()
    if row is None or str(row[0]) != payload["payload_fingerprint"]:
        # 同一 run id、不同内容 = 冲突，绝不覆盖。
        raise ExperimentSearchRepositoryError("search_run_idempotency_conflict")
    return run_id


def record_job(conn: sqlite3.Connection, *, job: ESC.SearchJobSpec,
               created_at: str | None = None) -> str:
    """Append one immutable job declaration."""
    if not isinstance(job, ESC.SearchJobSpec):
        raise ExperimentSearchRepositoryError("canonical_search_job_spec_required")
    projection = job.projection()
    _reject_metric_fields(projection, where="search_job")
    payload = {
        **projection,
        "job_json": _canonical(projection),
        "created_at": created_at or _now(),
    }
    columns = ("job_id", "job_fingerprint", "search_run_id", "candidate_id",
               "stage", "job_contract_version", "job_json", "created_at")
    conn.execute(
        f"INSERT INTO experiment_search_jobs({','.join(columns)})"
        f" VALUES({','.join('?' for _ in columns)})",
        tuple(payload[column] for column in columns),
    )
    return job.job_id


def record_job_event(conn: sqlite3.Connection, *, job_id: str, search_run_id: str,
                     event_kind: str, attempt_number: int | None = None,
                     actor: str | None = None, reason: str | None = None,
                     evidence_owner: str | None = None, evidence_id: str | None = None,
                     created_at: str | None = None) -> str:
    """General event entry: completion requires the specialized verified service path."""
    if event_kind == "completed":
        raise ExperimentSearchRepositoryError("completion_evidence_binding_unavailable")
    return _append_job_event(conn, job_id=job_id, search_run_id=search_run_id, event_kind=event_kind,
                             attempt_number=attempt_number, actor=actor, reason=reason,
                             evidence_owner=evidence_owner, evidence_id=evidence_id, created_at=created_at)


def record_verified_completion_event(conn, *, job_id, search_run_id, run_key, created_at=None, actor=None):
    """Trusted persistence primitive; only candidate_experiment_service may call it.

    The service independently re-reads and binds canonical R29 evidence before this write.
    This primitive never accepts an arbitrary evidence owner or metrics.
    """
    if not ESC.is_search_identity(run_key):
        raise ExperimentSearchRepositoryError("invalid_validation_run_key")
    return _append_job_event(conn, job_id=job_id, search_run_id=search_run_id, event_kind="completed",
                             evidence_owner="experiment_validation_run", evidence_id=run_key,
                             created_at=created_at, actor=actor)


def _append_job_event(conn: sqlite3.Connection, *, job_id: str, search_run_id: str,
                     event_kind: str, attempt_number: int | None = None,
                     actor: str | None = None, reason: str | None = None,
                     evidence_owner: str | None = None, evidence_id: str | None = None,
                     created_at: str | None = None) -> str:
    """Append one job event and return its ``event_id``.

    ``event_seq`` 由 SQLite 分配（AUTOINCREMENT），是唯一定序权威。``reason`` 必须是稳定
    machine code（lowercase snake case）—— 完整 exception repr / stack trace / 自由文本
    绝不能进入 canonical event identity，详细调试信息走日志系统。
    """
    if event_kind not in ESC.EVENT_KINDS:
        raise ExperimentSearchRepositoryError(f"unknown_job_event_kind:{event_kind}")
    if reason is not None:
        reason = str(reason)
        if not reason or reason != reason.strip().lower().replace("-", "_"):
            raise ExperimentSearchRepositoryError("reason_must_be_a_stable_machine_code")
        if " " in reason or ":" in reason or "\n" in reason:
            raise ExperimentSearchRepositoryError("reason_must_be_a_stable_machine_code")
    event_id, stamp = _event_identity(created_at)
    material = {
        "event_id": event_id,
        "job_id": str(job_id),
        "search_run_id": str(search_run_id),
        "event_kind": event_kind,
        "attempt_number": None if attempt_number is None else int(attempt_number),
        "actor": None if actor is None else str(actor),
        "reason": reason,
        "evidence_owner": None if evidence_owner is None else str(evidence_owner),
        "evidence_id": None if evidence_id is None else str(evidence_id),
        "created_at": stamp,
    }
    _reject_metric_fields(material, where="job_event")
    payload = {**material, "event_json": _canonical(material)}
    payload["payload_fingerprint"] = _fingerprint(payload)
    columns = ("event_id", "job_id", "search_run_id", "event_kind", "attempt_number",
               "actor", "reason", "evidence_owner", "evidence_id", "created_at",
               "event_json", "payload_fingerprint")
    conn.execute(
        f"INSERT INTO experiment_search_job_events({','.join(columns)})"
        f" VALUES({','.join('?' for _ in columns)})",
        tuple(payload[column] for column in columns),
    )
    return event_id


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_search_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """Read exactly one search run by id — never "the latest one"."""
    row = conn.execute(
        "SELECT search_run_id,search_input_fingerprint,search_contract_version,"
        "generation_batch_id,generation_input_fingerprint,candidate_count,budget_json,"
        "search_spec_json,created_at,payload_fingerprint"
        " FROM experiment_search_runs WHERE search_run_id=?", (str(run_id or ""),)).fetchone()
    if row is None:
        return None
    try:
        spec = ESC.search_spec_from_projection(json.loads(row[7]))
        if (spec.search_input_fingerprint != row[1] or spec.search_contract_version != row[2]
                or spec.generation_batch_id != row[3] or spec.generation_input_fingerprint != row[4]
                or spec.candidate_count != row[5] or spec.budget.projection() != json.loads(row[6])):
            raise ValueError("search projection mismatch")
        payload = dict(zip(("search_run_id", "search_input_fingerprint", "search_contract_version",
                            "generation_batch_id", "generation_input_fingerprint", "candidate_count",
                            "budget_json", "search_spec_json", "created_at"), row[:9], strict=True))
        if _fingerprint(payload) != row[9]:
            raise ValueError("search payload mismatch")
    except (TypeError, ValueError, KeyError) as exc:
        raise ExperimentSearchRepositoryError("corrupt_search_run") from exc
    return {
        "search_run_id": row[0],
        "search_input_fingerprint": row[1],
        "search_contract_version": row[2],
        "generation_batch_id": row[3],
        "generation_input_fingerprint": row[4],
        "candidate_count": int(row[5]),
        "budget": json.loads(row[6]),
        "search_spec": json.loads(row[7]),
        "created_at": row[8],
        "payload_fingerprint": row[9],
    }


def list_search_jobs(conn: sqlite3.Connection, run_id: str) -> tuple[dict, ...]:
    """List the immutable job declarations of one search run, in canonical order."""
    rows = conn.execute(
        "SELECT job_id,job_fingerprint,search_run_id,candidate_id,stage,"
        "job_contract_version,job_json,created_at"
        " FROM experiment_search_jobs WHERE search_run_id=?"
        " ORDER BY candidate_id ASC, stage ASC", (str(run_id or ""),)).fetchall()
    return tuple({
        "job_id": row[0],
        "job_fingerprint": row[1],
        "search_run_id": row[2],
        "candidate_id": row[3],
        "stage": row[4],
        "job_contract_version": row[5],
        "job": json.loads(row[6]),
        "created_at": row[7],
    } for row in rows)


def list_job_events(conn: sqlite3.Connection, job_id: str) -> tuple[dict, ...]:
    """List one job's events in **event_seq** order (the only ordering authority)."""
    rows = conn.execute(
        "SELECT event_seq,event_id,job_id,search_run_id,event_kind,attempt_number,"
        "actor,reason,evidence_owner,evidence_id,created_at,event_json,payload_fingerprint"
        " FROM experiment_search_job_events WHERE job_id=? ORDER BY event_seq ASC",
        (str(job_id or ""),)).fetchall()
    return tuple({
        "event_seq": int(row[0]),
        "event_id": row[1],
        "job_id": row[2],
        "search_run_id": row[3],
        "event_kind": row[4],
        "attempt_number": None if row[5] is None else int(row[5]),
        "actor": row[6],
        "reason": row[7],
        "evidence_owner": row[8],
        "evidence_id": row[9],
        "created_at": row[10],
        "event": json.loads(row[11]),
        "payload_fingerprint": row[12],
    } for row in rows)


def list_run_events(conn: sqlite3.Connection, run_id: str) -> tuple[dict, ...]:
    """Every event of one search run, grouped by job then event_seq."""
    rows = conn.execute(
        "SELECT job_id,event_seq,event_kind,attempt_number,created_at"
        " FROM experiment_search_job_events WHERE search_run_id=?"
        " ORDER BY job_id ASC, event_seq ASC", (str(run_id or ""),)).fetchall()
    return tuple({"job_id": row[0], "event_seq": int(row[1]), "event_kind": row[2],
                  "attempt_number": None if row[3] is None else int(row[3]),
                  "created_at": row[4]} for row in rows)


def project_job_states(conn: sqlite3.Connection, run_id: str) -> tuple[dict, ...]:
    """**Operational queue projection**：每个 job 的 ``MAX(event_seq)`` 事件。

    这只是"队列现在跑到哪了"，**不是**实验业务结果 authority：

    ```text
    current_state = completed
    → executor 成功产生了一份外部 evidence
    → 绝不表示 candidate passed / good / promotable
    ```

    顺序权威是 ``event_seq``；绝不使用 ``created_at`` 或 ``event_id`` 字典序。
    """
    rows = conn.execute(
        "SELECT j.job_id,j.candidate_id,j.stage,"
        "       e.event_kind,e.attempt_number,e.event_seq"
        " FROM experiment_search_jobs j"
        " LEFT JOIN experiment_search_job_events e"
        "   ON e.job_id=j.job_id"
        "  AND e.event_seq=(SELECT MAX(event_seq) FROM experiment_search_job_events"
        "                   WHERE job_id=j.job_id)"
        " WHERE j.search_run_id=?"
        " ORDER BY j.candidate_id ASC, j.stage ASC", (str(run_id or ""),)).fetchall()
    projected = []
    for row in rows:
        attempts = conn.execute(
            "SELECT COUNT(*) FROM experiment_search_job_events"
            " WHERE job_id=? AND event_kind='claimed'", (row[0],)).fetchone()[0]
        projected.append({
            "job_id": row[0],
            "candidate_id": row[1],
            "stage": row[2],
            # 没有事件时 current_state 为 None（声明刚创建，尚未排队）。
            "current_state": row[3],
            "attempt_count": int(attempts),
            "last_event_seq": None if row[5] is None else int(row[5]),
            "last_attempt_number": None if row[4] is None else int(row[4]),
        })
    return tuple(projected)
