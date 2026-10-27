# -*- coding: utf-8 -*-
"""平台授权、预算预留与跨库采用回执。

预算在正式库写入前于本库 ``BEGIN IMMEDIATE`` 中预留；正式提交后只追加回执，
不再事后补记预算。attempt 是幂等身份，恢复会重新读取当前授权并重放全部校验。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import time
import uuid

import ai_capability as CAP

SCHEMA_VERSION = "ai-capability-store-v2"
EVENT_ISSUED = "issued"
EVENT_REVOKED = "revoked"
R_OPERATOR_REQUIRED = "operator_identity_required"
R_CAPABILITY_CONFLICT = "capability_id_already_issued_differently"
R_CAPABILITY_UNKNOWN = "capability_unknown"
R_ADOPTION_IN_PROGRESS = "adoption_in_progress"
R_ADOPTION_MISMATCH = "adoption_identity_mismatch"
R_ADOPTION_STATE = "adoption_state_conflict"
LEASE_SECONDS = 30
_JOIN_POLL_SECONDS = 0.05

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ai_capabilities(
       capability_id TEXT PRIMARY KEY,credential TEXT NOT NULL,fingerprint TEXT NOT NULL,
       operator TEXT NOT NULL,issued_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ai_capability_events(
       id INTEGER PRIMARY KEY AUTOINCREMENT,capability_id TEXT NOT NULL,event TEXT NOT NULL,
       operator TEXT NOT NULL,payload TEXT NOT NULL,recorded_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ai_capability_events_capability
       ON ai_capability_events(capability_id,id)""",
    """CREATE TABLE IF NOT EXISTS ai_capability_usage(
       capability_id TEXT NOT NULL,window_key TEXT NOT NULL,
       changes_used REAL NOT NULL DEFAULT 0.0,cost_used REAL NOT NULL DEFAULT 0.0,
       trials_used INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
       PRIMARY KEY(capability_id,window_key))""",
    """CREATE TRIGGER IF NOT EXISTS ai_capability_events_append_only
       BEFORE UPDATE ON ai_capability_events
       BEGIN SELECT RAISE(ABORT,'capability_event_is_append_only'); END""",
    """CREATE TABLE IF NOT EXISTS ai_adoption_attempts(
       adoption_id TEXT PRIMARY KEY,capability_id TEXT NOT NULL,window_key TEXT NOT NULL,
       request_hash TEXT NOT NULL,request_json TEXT NOT NULL,context_json TEXT NOT NULL,
       credential_fingerprint TEXT NOT NULL,changes_used REAL NOT NULL,cost_used REAL NOT NULL,
       trials_used INTEGER NOT NULL,status TEXT NOT NULL,lease_owner TEXT,lease_until TEXT,
       receipt_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ai_adoption_attempts_capability
       ON ai_adoption_attempts(capability_id,window_key,status)""",
)


class CapabilityStoreError(ValueError):
    pass


def _now(now=None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(moment, str):
        return dt.datetime.fromisoformat(moment.replace("Z", "+00:00")).isoformat(timespec="milliseconds")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")


def _moment(value) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)


def ensure_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (str(name),)).fetchone() is not None


def _dump(credential: CAP.CapabilityCredential) -> str:
    return json.dumps(dataclasses.asdict(credential), sort_keys=True, ensure_ascii=False)


def _load_credential(payload: str) -> CAP.CapabilityCredential:
    data = json.loads(payload)
    for name in ("strategy_ids", "allowed_target_kinds", "allowed_fields"):
        data[name] = tuple(data[name])
    return CAP.CapabilityCredential(**data)


def _append_event(conn, capability_id, event, operator, payload, stamp):
    conn.execute("INSERT INTO ai_capability_events(capability_id,event,operator,payload,recorded_at)"
                 " VALUES(?,?,?,?,?)", (str(capability_id), str(event), str(operator),
                 json.dumps(payload, sort_keys=True, ensure_ascii=False), stamp))


def issue(conn, credential: CAP.CapabilityCredential, *, operator: str, now=None):
    if not str(operator or "").strip():
        raise CapabilityStoreError(R_OPERATOR_REQUIRED)
    ensure_schema(conn)
    fingerprint = CAP.credential_fingerprint(credential)
    stamp = _now(now)
    existing = conn.execute("SELECT fingerprint,credential FROM ai_capabilities WHERE capability_id=?",
                            (credential.capability_id,)).fetchone()
    if existing is not None:
        if str(existing[0]) != fingerprint:
            raise CapabilityStoreError(R_CAPABILITY_CONFLICT)
        return _load_credential(existing[1])
    conn.execute("INSERT INTO ai_capabilities(capability_id,credential,fingerprint,operator,issued_at,updated_at)"
                 " VALUES(?,?,?,?,?,?)", (credential.capability_id, _dump(credential), fingerprint,
                 str(operator), stamp, stamp))
    _append_event(conn, credential.capability_id, EVENT_ISSUED, operator,
                  {"fingerprint": fingerprint, "mode": credential.mode}, stamp)
    return credential


def revoke(conn, capability_id: str, *, operator: str, reason: str = "", now=None):
    if not str(operator or "").strip():
        raise CapabilityStoreError(R_OPERATOR_REQUIRED)
    ensure_schema(conn)
    row = conn.execute("SELECT credential FROM ai_capabilities WHERE capability_id=?",
                       (str(capability_id),)).fetchone()
    if row is None:
        raise CapabilityStoreError(R_CAPABILITY_UNKNOWN)
    current = _load_credential(row[0])
    revoked = dataclasses.replace(current, revocation_epoch=current.revocation_epoch + 1)
    stamp = _now(now)
    conn.execute("UPDATE ai_capabilities SET credential=?,fingerprint=?,operator=?,updated_at=?"
                 " WHERE capability_id=?", (_dump(revoked), CAP.credential_fingerprint(revoked),
                 str(operator), stamp, str(capability_id)))
    _append_event(conn, capability_id, EVENT_REVOKED, operator,
                  {"revocation_epoch": revoked.revocation_epoch, "reason": str(reason)[:200]}, stamp)
    return revoked


def load(conn, capability_id: str):
    if not _has_table(conn, "ai_capabilities"):
        return None
    row = conn.execute("SELECT credential FROM ai_capabilities WHERE capability_id=?",
                       (str(capability_id),)).fetchone()
    return None if row is None else _load_credential(row[0])


def list_capabilities(conn) -> list[dict]:
    if not _has_table(conn, "ai_capabilities"):
        return []
    result = []
    for row in conn.execute("SELECT capability_id,credential,operator,issued_at,updated_at"
                            " FROM ai_capabilities ORDER BY issued_at").fetchall():
        credential = _load_credential(row["credential"])
        described = CAP.describe_capability(credential)
        described.update({"operator": str(row["operator"]), "issued_at": str(row["issued_at"]),
                          "updated_at": str(row["updated_at"]),
                          "event_count": len(events(conn, credential.capability_id))})
        result.append(described)
    return result


def events(conn, capability_id: str) -> list[dict]:
    if not _has_table(conn, "ai_capability_events"):
        return []
    return [dict(row) for row in conn.execute(
        "SELECT event,operator,payload,recorded_at FROM ai_capability_events"
        " WHERE capability_id=? ORDER BY id", (str(capability_id),)).fetchall()]


def budget(conn, capability_id: str, window_key: str) -> CAP.BudgetState:
    if not _has_table(conn, "ai_capability_usage"):
        return CAP.BudgetState(window_key=str(window_key))
    row = conn.execute("SELECT changes_used,cost_used,trials_used FROM ai_capability_usage"
                       " WHERE capability_id=? AND window_key=?",
                       (str(capability_id), str(window_key))).fetchone()
    if row is None:
        return CAP.BudgetState(window_key=str(window_key))
    return CAP.BudgetState(window_key=str(window_key), changes_used=float(row[0]),
                           cost_used=float(row[1]), trials_used=int(row[2]))


def save_budget(conn, capability_id: str, state: CAP.BudgetState, *, now=None) -> None:
    ensure_schema(conn)
    conn.execute("INSERT INTO ai_capability_usage(capability_id,window_key,changes_used,cost_used,trials_used,updated_at)"
                 " VALUES(?,?,?,?,?,?) ON CONFLICT(capability_id,window_key) DO UPDATE SET"
                 " changes_used=excluded.changes_used,cost_used=excluded.cost_used,"
                 " trials_used=excluded.trials_used,updated_at=excluded.updated_at",
                 (str(capability_id), str(state.window_key), float(state.changes_used),
                  float(state.cost_used), int(state.trials_used), _now(now)))


def window_key(now) -> str:
    return _moment(_now(now)).astimezone(dt.timezone.utc).date().isoformat()


def _request_data(request: CAP.AdoptionRequest) -> dict:
    return {"strategy_id": request.strategy_id, "target_kind": request.target_kind,
            "field_changes": [[str(k), float(v)] for k, v in request.field_changes],
            "current_baseline": [[str(k), float(v)] for k, v in request.current_baseline],
            "requested_baseline": [[str(k), float(v)] for k, v in request.requested_baseline],
            "evidence_policy_id": request.evidence_policy_id,
            "revocation_epoch": int(request.revocation_epoch), "producer": request.producer,
            "approver": request.approver, "cost": float(request.cost), "now": request.now}


def _request_from(data, credential) -> CAP.AdoptionRequest:
    values = dict(data)
    values["field_changes"] = tuple((k, v) for k, v in values["field_changes"])
    values["current_baseline"] = tuple((k, v) for k, v in values["current_baseline"])
    values["requested_baseline"] = tuple((k, v) for k, v in values["requested_baseline"])
    return CAP.AdoptionRequest(credential=credential, **values)


def _request_hash(request: CAP.AdoptionRequest) -> str:
    # Baseline, credential epoch and wall clock are runtime facts, not request identity.
    data = _request_data(request)
    for key in ("current_baseline", "requested_baseline", "revocation_epoch", "now"):
        data.pop(key)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclasses.dataclass(frozen=True, slots=True)
class AdoptionReservation:
    adoption_id: str
    owner: str
    request: CAP.AdoptionRequest
    context: dict
    decision: CAP.AdoptionDecision
    status: str
    recovered: bool = False


def _row_value(row, name):
    return row[name] if hasattr(row, "keys") else row[0]


def get_attempt(conn, adoption_id: str):
    if not _has_table(conn, "ai_adoption_attempts"):
        return None
    return conn.execute("SELECT * FROM ai_adoption_attempts WHERE adoption_id=?",
                        (str(adoption_id),)).fetchone()


def _reservation(row, owner, credential, decision, recovered=False) -> AdoptionReservation:
    request = _request_from(json.loads(_row_value(row, "request_json")), credential)
    return AdoptionReservation(adoption_id=str(_row_value(row, "adoption_id")), owner=owner,
                               request=request, context=json.loads(_row_value(row, "context_json")),
                               decision=decision, status=str(_row_value(row, "status")),
                               recovered=recovered)


def _locked_start(conn):
    ensure_schema(conn)
    if conn.in_transaction:
        # This connection is the dedicated authority ledger, not a caller transaction.
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _lease_is_live(row, owner, stamp) -> bool:
    """True when another owner currently holds this attempt's lease."""
    if str(_row_value(row, "status")) != "reserved":
        return False
    lease_owner = _row_value(row, "lease_owner")
    if not lease_owner or str(lease_owner) == owner:
        return False
    lease_until = _row_value(row, "lease_until")
    return lease_until is not None and _moment(lease_until) > _moment(stamp)


def is_revoked(conn, capability_id: str) -> bool:
    """Whether the latest lifecycle event for this authority is a revocation.

    An incremented epoch only fences *requests that carry the old epoch*.  A
    caller could reload the revoked credential and copy its new epoch, so the
    signed authority itself must remain terminal until a new capability id is
    issued.
    """
    if not _has_table(conn, "ai_capability_events"):
        return False
    row = conn.execute(
        "SELECT event FROM ai_capability_events WHERE capability_id=? ORDER BY id DESC LIMIT 1",
        (str(capability_id),)).fetchone()
    return row is not None and str(_row_value(row, "event")) == EVENT_REVOKED


def reserve(conn, request: CAP.AdoptionRequest, *, adoption_id: str, context: dict | None = None,
            now=None, owner: str | None = None, recovery: bool = False) -> AdoptionReservation:
    """Reserve quota atomically, or resume exactly the same durable attempt.

    A duplicate caller that meets a live lease joins the running attempt instead of
    writing twice or failing outright: the ledger lock is released, the attempt is
    watched for up to ``LEASE_SECONDS``, and the settled decision is re-read.  Only
    after that window does the caller take the lease over.
    """
    if request.credential is None:
        raise CAP.AdoptionRefused(CAP.R_NO_CREDENTIAL)
    owner = str(owner or uuid.uuid4().hex)
    stamp = _now(now or request.now)
    join_deadline = time.monotonic() + LEASE_SECONDS
    while True:
        _locked_start(conn)
        try:
            capability_id = str(request.credential.capability_id)
            current = load(conn, capability_id)
            if current is None:
                raise CAP.AdoptionRefused(CAP.R_NO_CREDENTIAL, capability_id)
            if is_revoked(conn, capability_id):
                raise CAP.AdoptionRefused(CAP.R_REVOKED, "capability_store_revoked")
            row = get_attempt(conn, str(adoption_id))
            if row is not None and str(_row_value(row, "request_hash")) != _request_hash(request):
                raise CapabilityStoreError(R_ADOPTION_MISMATCH)
            if row is not None and not recovery and _lease_is_live(row, owner, stamp):
                # Release the write lock first, otherwise the owner can never commit
                # its receipt and both callers would wait for each other.
                conn.commit()
                if time.monotonic() >= join_deadline:
                    recovery = True
                else:
                    time.sleep(_JOIN_POLL_SECONDS)
                continue
            stored_request = request if row is None else _request_from(
                json.loads(_row_value(row, "request_json")), current)
            # The request object is only a pointer to authority; stored credential wins.
            if str(stored_request.approver) != str(current.issued_by):
                raise CAP.AdoptionRefused(CAP.R_NO_CREDENTIAL, "approver_is_not_issuer")
            checked = dataclasses.replace(stored_request, credential=current, now=stamp,
                                          requested_baseline=stored_request.current_baseline)
            if row is None:
                if context is None:
                    raise CapabilityStoreError("adoption_context_required")
                state = budget(conn, capability_id, window_key(stamp))
                decision = CAP.evaluate(checked, budget=state, apply_writer=lambda _r: None)
                decision = dataclasses.replace(decision, applied=False)
                after = decision.budget_after
                save_budget(conn, capability_id, after, now=stamp)
                lease_until = (_moment(stamp) + dt.timedelta(seconds=LEASE_SECONDS)).isoformat()
                conn.execute("INSERT INTO ai_adoption_attempts(adoption_id,capability_id,window_key,"
                             "request_hash,request_json,context_json,credential_fingerprint,changes_used,"
                             "cost_used,trials_used,status,lease_owner,lease_until,created_at,updated_at)"
                             " VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?,?)",
                             (str(adoption_id), capability_id, window_key(stamp), _request_hash(request),
                              _canonical(_request_data(checked)), _canonical(context),
                              CAP.credential_fingerprint(current), after.changes_used-state.changes_used,
                              after.cost_used-state.cost_used, after.trials_used-state.trials_used,
                              owner, lease_until, stamp, stamp))
                _append_event(conn, capability_id, "adoption_reserved", owner,
                              {"adoption_id": str(adoption_id), "changes": dict(decision.changes)}, stamp)
                row = get_attempt(conn, str(adoption_id))
            else:
                status = str(_row_value(row, "status"))
                if status not in ("reserved", "applied"):
                    raise CapabilityStoreError(R_ADOPTION_STATE, status)
                # Remove this attempt's old reservation before re-checking today's real limits.
                used = budget(conn, capability_id, str(_row_value(row, "window_key")))
                own = CAP.BudgetState(window_key=used.window_key,
                                      changes_used=max(0.0, used.changes_used-float(_row_value(row, "changes_used"))),
                                      cost_used=max(0.0, used.cost_used-float(_row_value(row, "cost_used"))),
                                      trials_used=max(0, used.trials_used-int(_row_value(row, "trials_used"))))
                if own.window_key != window_key(stamp):
                    raise CAP.AdoptionRefused(CAP.R_BUDGET_EXHAUSTED, "recovery_window_changed")
                decision = CAP.evaluate(checked, budget=own, apply_writer=lambda _r: None)
                decision = dataclasses.replace(decision, applied=(status == "applied"))
                lease_until = (_moment(stamp) + dt.timedelta(seconds=LEASE_SECONDS)).isoformat()
                if status == "reserved":
                    conn.execute("UPDATE ai_adoption_attempts SET lease_owner=?,lease_until=?,updated_at=?"
                                 " WHERE adoption_id=?", (owner, lease_until, stamp, str(adoption_id)))
            result = _reservation(row, owner, current, decision,
                                  recovered=recovery or row is not None)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def receipt(conn, reservation: AdoptionReservation, *, receipt: dict, now=None) -> CAP.AdoptionDecision:
    """Durable post-commit receipt; quota was already persisted by reserve()."""
    stamp = _now(now or reservation.request.now)
    _locked_start(conn)
    try:
        row = get_attempt(conn, reservation.adoption_id)
        if row is None or str(_row_value(row, "status")) not in ("reserved", "applied"):
            raise CapabilityStoreError(R_ADOPTION_STATE, reservation.adoption_id)
        payload = _canonical(receipt)
        if _row_value(row, "receipt_json") not in (None, payload):
            raise CapabilityStoreError(R_ADOPTION_MISMATCH, "receipt")
        conn.execute("UPDATE ai_adoption_attempts SET status='applied',lease_owner=NULL,lease_until=NULL,"
                     "receipt_json=?,updated_at=? WHERE adoption_id=?",
                     (payload, stamp, reservation.adoption_id))
        _append_event(conn, reservation.adoption_id and reservation.request.credential.capability_id,
                      "adoption_receipt", reservation.owner,
                      {"adoption_id": reservation.adoption_id, "receipt": receipt}, stamp)
        conn.commit()
        return dataclasses.replace(reservation.decision, applied=True)
    except Exception:
        conn.rollback()
        raise


def release(conn, reservation: AdoptionReservation, *, reason: str, now=None) -> None:
    """Release only when the caller proved the formal commit did not happen."""
    stamp = _now(now or reservation.request.now)
    _locked_start(conn)
    try:
        row = get_attempt(conn, reservation.adoption_id)
        if row is None or str(_row_value(row, "status")) != "reserved":
            return
        used = budget(conn, reservation.request.credential.capability_id,
                      str(_row_value(row, "window_key")))
        remaining = CAP.BudgetState(
            window_key=used.window_key,
            changes_used=round(max(0.0, used.changes_used-float(_row_value(row, "changes_used"))), 8),
            cost_used=round(max(0.0, used.cost_used-float(_row_value(row, "cost_used"))), 8),
            trials_used=max(0, used.trials_used-int(_row_value(row, "trials_used"))))
        save_budget(conn, reservation.request.credential.capability_id, remaining, now=stamp)
        conn.execute("DELETE FROM ai_adoption_attempts WHERE adoption_id=?", (reservation.adoption_id,))
        _append_event(conn, reservation.request.credential.capability_id, "adoption_released",
                      reservation.owner, {"adoption_id": reservation.adoption_id,
                                         "reason": str(reason)[:200]}, stamp)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def resume(conn, request: CAP.AdoptionRequest, *, adoption_id: str, now=None,
           owner=None) -> AdoptionReservation:
    return reserve(conn, request, adoption_id=adoption_id, context=None, now=now,
                   owner=owner, recovery=True)


def adopt(conn, request: CAP.AdoptionRequest, *, apply_writer, now=None,
          adoption_id: str | None = None, context: dict | None = None,
          recovery: bool = False):
    """Generic reserve/write/receipt flow; writers must be idempotent by adoption_id."""
    if apply_writer is None:
        raise CAP.AdoptionRefused(CAP.R_APPLY_NOT_WIRED)
    adoption_id = str(adoption_id or uuid.uuid4().hex)
    reservation = reserve(conn, request, adoption_id=adoption_id,
                          context={} if context is None else context, now=now, recovery=recovery)
    if reservation.status == "applied":
        return reservation.decision
    # An ambiguous writer exception deliberately leaves the durable reservation for replay.
    result = apply_writer(reservation.request)
    proof = result if isinstance(result, dict) else {"result": str(result)}
    return receipt(conn, reservation, receipt=proof, now=now)
