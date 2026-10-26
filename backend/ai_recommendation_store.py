# -*- coding: utf-8 -*-
"""结构化 AI 意见的持久化（方案 §6.1 最小意见对象，append-only）。

这里只存**平台校验过的**意见。三条硬规则：

1. 模型不能自报权威字段。``qty`` / ``shares`` / ``amount`` / ``filled_price`` /
   ``verified`` / ``promoted`` / ``authorized`` / ``capability_id`` 出现在意见里就整条拒绝
   （不是忽略、不是清洗）。
2. 数值必须有限。NaN / Infinity / 非数字都是非法，不夹取、不填空。
3. 追加即终态。意见写入后不可改、不可删——它是"当时模型看到了什么、说了什么"的记录，
   不是可刷新的缓存。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import uuid

RECOMMENDATION_SCHEMA_VERSION = "ai-recommendation-v1"
PROMPT_VERSION = "research-agent-prompt-v1"
TOOLSET_VERSION = "research-toolset-v1"

ACTION_SKIP_ENTRY = "skip_entry"
ACTION_WAIT_ENTRY = "wait_entry"
ACTION_PREFER_ENTRY = "prefer_entry"
ACTION_REDUCE_ENTRY = "reduce_entry"
ACTION_HOLD_POSITION = "hold_position"
ACTION_TRIM_POSITION = "trim_position"
ACTION_REQUEST_EXIT = "request_exit"
ACTION_WAIT_SOFT_EXIT = "wait_soft_exit"
ACTION_PROPOSE_ALLOCATION = "propose_allocation"
#: 研究循环内部的推进动作（不是对交易的影响）。它们只推进研究，不产生交易意见。
ACTION_CONTINUE = "continue"
ACTION_REVISE_HYPOTHESIS = "revise_hypothesis"
ACTION_ELIMINATE = "eliminate"
ACTION_WAIT_FOR_DATA = "wait_for_data"

RESEARCH_ACTIONS = (ACTION_CONTINUE, ACTION_REVISE_HYPOTHESIS, ACTION_ELIMINATE,
                    ACTION_WAIT_FOR_DATA)
TRADE_ACTIONS = (ACTION_SKIP_ENTRY, ACTION_WAIT_ENTRY, ACTION_PREFER_ENTRY,
                 ACTION_REDUCE_ENTRY, ACTION_HOLD_POSITION, ACTION_TRIM_POSITION,
                 ACTION_REQUEST_EXIT, ACTION_WAIT_SOFT_EXIT, ACTION_PROPOSE_ALLOCATION)
ALL_ACTIONS = RESEARCH_ACTIONS + TRADE_ACTIONS

#: 模型一旦输出这些键，整条意见作废：它们是平台/授权链的产物，不是模型的判断。
FORBIDDEN_KEYS = frozenset({
    "qty", "shares", "quantity", "amount", "filled_price", "fill_price",
    "verified", "promoted", "authorized", "challenger_win", "capability_id",
    "revocation_epoch", "max_change_per_day", "approved_by",
})

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS ai_recommendations (
        recommendation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        strategy_id TEXT,
        exact_identity TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        action TEXT NOT NULL,
        payload TEXT NOT NULL,
        reason TEXT NOT NULL,
        valid_until TEXT,
        evidence_refs TEXT NOT NULL,
        provider_id TEXT,
        model_id TEXT,
        prompt_version TEXT NOT NULL,
        toolset_version TEXT NOT NULL,
        raw_response_hash TEXT NOT NULL,
        narrative TEXT,
        capability_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ai_recommendations_task
        ON ai_recommendations(task_id, created_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ai_recommendations_no_update
    BEFORE UPDATE ON ai_recommendations
    BEGIN SELECT RAISE(ABORT, 'ai_recommendation_is_append_only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ai_recommendations_no_delete
    BEFORE DELETE ON ai_recommendations
    BEGIN SELECT RAISE(ABORT, 'ai_recommendation_is_append_only'); END
    """,
)


class RecommendationRejected(ValueError):
    """意见不合法（整条拒绝，不做部分接受）。"""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now(now=None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone() is not None


def ensure_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def scan_forbidden(value, *, path: str = "") -> list[str]:
    """递归找出被禁止的键。模型输出里出现它们就是整条拒绝。"""
    hits: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).strip().lower()
            if name in FORBIDDEN_KEYS:
                hits.append(f"{path}.{name}" if path else name)
            hits.extend(scan_forbidden(item, path=f"{path}.{name}" if path else name))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hits.extend(scan_forbidden(item, path=f"{path}[{index}]"))
    return hits


def scan_non_finite(value, *, path: str = "") -> list[str]:
    if isinstance(value, float) and not math.isfinite(value):
        return [path or "<root>"]
    if isinstance(value, dict):
        hits: list[str] = []
        for key, item in value.items():
            hits.extend(scan_non_finite(item, path=f"{path}.{key}" if path else str(key)))
        return hits
    if isinstance(value, (list, tuple)):
        hits = []
        for index, item in enumerate(value):
            hits.extend(scan_non_finite(item, path=f"{path}[{index}]"))
        return hits
    return []


def record(
    conn,
    *,
    task_id: str,
    action: str,
    reason: str,
    exact_identity: dict,
    input_fingerprint: str,
    payload: dict | None = None,
    valid_until: str | None = None,
    evidence_refs=(),
    provider_id: str | None = None,
    model_id: str | None = None,
    raw_response: str = "",
    narrative: str | None = None,
    capability_id: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    toolset_version: str = TOOLSET_VERSION,
    now=None,
) -> dict:
    """写入一条已校验的意见。任何一项不合法都整条拒绝。"""
    if str(action) not in ALL_ACTIONS:
        raise RecommendationRejected(f"unknown_action:{action}")
    if not str(reason or "").strip():
        raise RecommendationRejected("reason_required")
    if not str(input_fingerprint or "").strip():
        raise RecommendationRejected("input_fingerprint_required")
    if not isinstance(exact_identity, dict) or not exact_identity:
        raise RecommendationRejected("exact_identity_required")
    body = dict(payload or {})
    forbidden = scan_forbidden(body)
    if forbidden:
        raise RecommendationRejected(f"forbidden_fields:{','.join(sorted(set(forbidden)))}")
    bad_numbers = scan_non_finite(body)
    if bad_numbers:
        raise RecommendationRejected(f"non_finite_numbers:{','.join(bad_numbers)}")
    refs = [str(item) for item in evidence_refs or ()]
    ensure_schema(conn)
    stamp = _now(now)
    recommendation = {
        "recommendation_id": uuid.uuid4().hex,
        "task_id": str(task_id),
        "schema_version": RECOMMENDATION_SCHEMA_VERSION,
        "strategy_id": exact_identity.get("strategy_id"),
        "exact_identity": exact_identity,
        "input_fingerprint": str(input_fingerprint),
        "action": str(action),
        "payload": body,
        "reason": str(reason),
        "valid_until": valid_until,
        "evidence_refs": refs,
        "provider_id": provider_id,
        "model_id": model_id,
        "prompt_version": prompt_version,
        "toolset_version": toolset_version,
        "raw_response_hash": fingerprint({"raw": raw_response}),
        "narrative": narrative,
        "capability_id": capability_id,
        "created_at": stamp,
    }
    conn.execute(
        "INSERT INTO ai_recommendations (recommendation_id, task_id, schema_version,"
        " strategy_id, exact_identity, input_fingerprint, action, payload, reason,"
        " valid_until, evidence_refs, provider_id, model_id, prompt_version,"
        " toolset_version, raw_response_hash, narrative, capability_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (recommendation["recommendation_id"], recommendation["task_id"],
         recommendation["schema_version"], recommendation["strategy_id"],
         _canonical(exact_identity), str(input_fingerprint),
         str(action), _canonical(body),
         recommendation["reason"], valid_until, _canonical(refs), provider_id, model_id,
         prompt_version, toolset_version, recommendation["raw_response_hash"],
         narrative, capability_id, stamp),
    )
    return recommendation


def list_for_task(conn, task_id: str) -> list[dict]:
    if not _has_table(conn, "ai_recommendations"):
        return []
    rows = conn.execute(
        "SELECT * FROM ai_recommendations WHERE task_id=? ORDER BY created_at",
        (str(task_id),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("exact_identity", "payload", "evidence_refs"):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, ValueError):
                item[field] = None
        result.append(item)
    return result
