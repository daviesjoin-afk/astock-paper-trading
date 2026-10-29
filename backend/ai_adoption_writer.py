# -*- coding: utf-8 -*-
"""Bounded adoption wired to the sole formal parameter writer.

The capability ledger reserves quota first.  A temporary paper-DB trigger closes
SQLite's cross-database baseline race, then ``adaptive_selection.apply_candidate``
performs its normal durable outbox + formal commit.  The receipt is written last;
any interruption is resumed by adoption id and repeats authority/live/quota checks.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

import ai_capability as CAP

R_STALE_BASE = "stale_base"
R_CANDIDATE_MISMATCH = "candidate_does_not_match_authorized_change"
R_WRITER_REFUSED = "formal_writer_refused"
R_ACCOUNT_MISSING = "paper_account_missing"
R_CANDIDATE_APPLIED = "candidate_already_applied_to_different_base"


class AdoptionWiringError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = str(reason)
        self.detail = detail


def _loads(value, fallback=None):
    empty = fallback if fallback is not None else {}
    if value in (None, ""):
        return empty
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return empty
    return parsed if isinstance(parsed, dict) else empty


def _value(row, name):
    return row[name] if hasattr(row, "keys") else row[0]


def live_selection_baseline(conn, account_id: str) -> dict:
    """Read the same effective weights that the sole writer will use."""
    import adaptive_selection as AS

    row = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (str(account_id),)).fetchone()
    if row is None:
        raise AdoptionWiringError(R_ACCOUNT_MISSING, str(account_id))
    account = dict(row)
    model, weights, _delta, params, meta = AS.current_selection(account)
    overlay = _loads(params.get("adaptive_selection"))
    return {"active": str(meta.get("status") or "") == "active",
            "weights": dict(weights), "conditions": dict(_loads(overlay.get("conditions"))),
            "model_family": model, "version": meta.get("version"),
            "account_version": account.get("version"), "params": params,
            # The exact text the compare-and-swap trigger must match; re-serialising
            # the dict would produce a different byte string than the writer stores.
            "params_text": account.get("params")}


def _candidate(conn, candidate_id):
    row = conn.execute("SELECT * FROM adaptive_selection_candidates WHERE id=?",
                       (int(candidate_id),)).fetchone()
    if row is None:
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, f"candidate_missing:{candidate_id}")
    return dict(row)


def _same_numbers(left: dict, right: dict) -> bool:
    if set(left) != set(right):
        return False
    try:
        return all(abs(float(left[key]) - float(right[key])) <= 1e-9 for key in left)
    except (TypeError, ValueError):
        return False


def actual_candidate_changes(candidate: dict, live: dict, target_kind: str) -> tuple:
    """Derive every effective mutation from candidate + target + live, never declarations."""
    section = "weights" if target_kind == "selection_weights" else "conditions"
    requested = _loads(candidate.get(section))
    baseline = live.get(section) or {}
    if not isinstance(requested, dict) or not requested:
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, f"empty_{section}")
    changes = []
    for name, value in requested.items():
        if name not in baseline:
            raise AdoptionWiringError(R_CANDIDATE_MISMATCH, f"not_in_live_baseline:{name}")
        try:
            target, current = float(value), float(baseline[name])
        except (TypeError, ValueError) as exc:
            raise AdoptionWiringError(R_CANDIDATE_MISMATCH, f"non_numeric:{name}") from exc
        if abs(target - current) > 1e-9:
            changes.append((str(name), target))
    return tuple(sorted(changes))


def validate_candidate(conn, candidate_id, request, live: dict) -> tuple:
    item = _candidate(conn, candidate_id)
    if str(item["account_id"]) != str(request.strategy_id):
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, "target_account")
    candidate = _loads(item["candidate_params"])
    allowed_keys = {"weights"} if request.target_kind == "selection_weights" else {"conditions"}
    if set(candidate) != allowed_keys:
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, "unauthorized_candidate_fields")
    expected_version = f"select-evo-{str(item['run_date']).replace('-', '')}-{int(candidate_id)}"
    materialized = live.get("account_version") == expected_version
    if materialized and item["status"] == "applied":
        changes = tuple(sorted(request.field_changes or ()))
    else:
        if item["status"] == "applied":
            raise AdoptionWiringError(R_CANDIDATE_APPLIED, str(candidate_id))
        changes = actual_candidate_changes(candidate, live, request.target_kind)
        if not changes:
            raise CAP.AdoptionRefused(CAP.R_STALE_BASE, "candidate_already_materialized")
    if not _same_numbers(dict(changes), dict(request.field_changes or ())):
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, "declared_change_differs_from_actual")
    return changes


def candidate_matches(conn, candidate_id, field_changes, *, live=None,
                      target_kind="selection_weights", strategy_id=None) -> None:
    """Backward-compatible strict candidate validator (no subset matching)."""
    item = _candidate(conn, candidate_id)
    if strategy_id is not None and str(item["account_id"]) != str(strategy_id):
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, "target_account")
    candidate = _loads(item["candidate_params"])
    if live is None:
        # Without an account there is no authoritative live baseline; callers must supply it.
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, "live_baseline_required")
    changes = actual_candidate_changes(candidate, live, target_kind)
    if not _same_numbers(dict(changes), dict(field_changes or ())):
        raise AdoptionWiringError(R_CANDIDATE_MISMATCH, "declared_change_differs_from_actual")


def _sql_literal(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


class _PaperBaselineGuard:
    """Make the live read and the writer's formal commit one SQLite serialization domain.

    The trigger is a compare-and-swap precondition, not a lock: any connection may
    still write while it is installed as long as ``paper_accounts.params`` is byte
    identical to the text this adopter read.  Only a value that drifted since the
    read is refused, so a concurrent manual edit always wins.
    """

    def __init__(self, paper_path, account_id, expected_text):
        self.paper_path = paper_path
        self.account_id = str(account_id)
        self.expected = expected_text
        self.name = "ai_adoption_base_" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:24]
        self.installed = False
        self.conn = None

    def __enter__(self):
        import sqlite3

        self.conn = sqlite3.connect(self.paper_path, timeout=30)
        self.conn.execute("PRAGMA busy_timeout=30000")
        quoted = '"' + self.name + '"'
        self.conn.execute(
            f"CREATE TRIGGER {quoted} BEFORE UPDATE OF params ON paper_accounts "
            "WHEN OLD.id='" + self.account_id.replace("'", "''") + "' "
            "AND OLD.params IS NOT " + _sql_literal(self.expected) + " "
            "BEGIN SELECT RAISE(ABORT,'stale_base'); END")
        # DDL must be committed explicitly: an uncommitted schema change is dropped by
        # close() and is invisible to the writer's own connection.
        self.conn.commit()
        self.installed = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
            try:
                if self.installed:
                    self.conn.execute('DROP TRIGGER IF EXISTS "' + self.name + '"')
                    self.conn.commit()
            finally:
                self.conn.close()


def _now_callable(now):
    if callable(now):
        return now
    if now is None:
        return _default_now
    if isinstance(now, dt.datetime):
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        return lambda: stamp
    text = str(now)
    return lambda: text


def _default_now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def paper_db_path_of(paper_conn) -> str:
    for row in paper_conn.execute("PRAGMA database_list").fetchall():
        name = row[1] if not hasattr(row, "keys") else row["name"]
        if str(name) == "main":
            return str(row[2] if not hasattr(row, "keys") else row["file"])
    raise AdoptionWiringError(R_ACCOUNT_MISSING, "paper_db_path_unavailable")


def _invoke_adopt(capability_conn, adaptive_conn, paper_path, request, candidate_id,
                  adoption_id, context, now, approved_by, recovery=False):
    import adaptive_selection as AS
    import ai_capability_store as STORE

    reservation = STORE.reserve(capability_conn, request, adoption_id=adoption_id,
                                context=None if recovery else context, now=now, recovery=recovery)
    live = live_selection_baseline_from_path(paper_path, request.strategy_id)
    validate_candidate(adaptive_conn, candidate_id, reservation.request, live)
    if reservation.status == "applied":
        return reservation.decision

    # Recheck against the values persisted with the reservation as well as current values.
    expected_version = f"select-evo-{str(_candidate(adaptive_conn, candidate_id)['run_date']).replace('-', '')}-{int(candidate_id)}"
    if live.get("account_version") != expected_version:
        baseline_pairs = tuple(sorted((key, float(value))
                                      for key, value in live["weights"].items()
                                      if key in dict(reservation.request.field_changes)))
        if dict(baseline_pairs) != dict(reservation.request.current_baseline):
            raise CAP.AdoptionRefused(CAP.R_STALE_BASE, "live_baseline")

    def apply(_request):
        try:
            with _PaperBaselineGuard(paper_path, request.strategy_id, live["params_text"]):
                result = AS.apply_candidate(adaptive_conn, paper_path, candidate_id,
                                            _now_callable(now), approved_by=approved_by)
            return {"candidate_id": int(result), "account_id": request.strategy_id,
                    "version": f"select-evo-{str(_candidate(adaptive_conn, candidate_id)['run_date']).replace('-', '')}-{int(candidate_id)}"}
        except AdoptionWiringError:
            raise
        except Exception as exc:
            # The guard aborts through RAISE(ABORT), which surfaces as
            # sqlite3.IntegrityError -- not a ValueError.  Map it back to the
            # domain reason so callers see stale_base and the quota is released.
            if "stale_base" in str(exc):
                raise CAP.AdoptionRefused(CAP.R_STALE_BASE, "formal_transaction") from exc
            if isinstance(exc, ValueError):
                raise AdoptionWiringError(R_WRITER_REFUSED, str(exc)) from exc
            raise

    try:
        proof = apply(reservation.request)
    except (AdoptionWiringError, CAP.AdoptionRefused) as exc:
        # Remove any pre-commit outbox too: a generic replay must never outlive its reservation.
        AS.cancel_outbox(adaptive_conn, candidate_id, _now_callable(now)(),
                         reason=f"adoption_not_applied:{exc}")
        STORE.release(capability_conn, reservation, reason=str(exc), now=now)
        raise
    # Other exceptions have an ambiguous cross-DB outcome: retain the reservation for replay.
    return STORE.receipt(capability_conn, reservation, receipt=proof, now=now)


def live_selection_baseline_from_path(paper_path, account_id):
    import sqlite3

    conn = sqlite3.connect(paper_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return live_selection_baseline(conn, account_id)
    finally:
        conn.close()


def _attempt_identity(capability_conn, request, candidate_id):
    capability_id = None if request.credential is None else request.credential.capability_id
    return f"{capability_id}:{request.strategy_id}:{int(candidate_id)}"


def _find_attempt(capability_conn, capability_id, candidate_id):
    rows = capability_conn.execute(
        "SELECT adoption_id,context_json,status FROM ai_adoption_attempts"
        " WHERE capability_id=? AND status IN ('reserved','applied')", (str(capability_id),)).fetchall()
    for row in rows:
        context = _loads(_value(row, "context_json"))
        if int(context.get("candidate_id", -1)) == int(candidate_id):
            return row
    return None


def adopt_candidate(*, capability_conn, adaptive_conn, paper_conn, request, candidate_id,
                    declared_baseline: dict | None = None, now=None,
                    approved_by: str = "bounded-auto", adoption_id: str | None = None,
                    recovery: bool = False):
    """Reserve -> guarded formal commit -> receipt, with idempotent recovery."""
    import ai_capability_store as STORE

    if request.credential is None:
        raise CAP.AdoptionRefused(CAP.R_NO_CREDENTIAL)
    STORE.ensure_schema(capability_conn)
    paper_path = paper_db_path_of(paper_conn)
    existing = _find_attempt(capability_conn, request.credential.capability_id, candidate_id)
    if adoption_id is None and existing is not None:
        adoption_id = str(_value(existing, "adoption_id"))
        recovery = True
    adoption_id = str(adoption_id or _attempt_identity(capability_conn, request, candidate_id))
    live = live_selection_baseline_from_path(paper_path, request.strategy_id)
    changes = validate_candidate(adaptive_conn, candidate_id, request, live)
    import dataclasses
    checked_request = dataclasses.replace(request, field_changes=changes,
                                          current_baseline=tuple(sorted(
                                              (k, float(v)) for k, v in live["weights"].items()
                                              if k in dict(changes))),
                                          requested_baseline=())
    if declared_baseline is not None:
        declared = dict(declared_baseline)
        for name in ("weights", "conditions"):
            if name in declared and not _same_numbers(_loads(declared.get(name)), live[name]):
                raise AdoptionWiringError(R_STALE_BASE, name)
    context = {"candidate_id": int(candidate_id), "paper_path": paper_path,
               "approved_by": approved_by, "declared_baseline": declared_baseline,
               "live_weights": live["weights"], "live_account_version": live["account_version"]}
    return _invoke_adopt(capability_conn, adaptive_conn, paper_path, checked_request,
                         int(candidate_id), adoption_id, context, now, approved_by, recovery)


def replay_pending_adoptions(*, capability_conn, adaptive_conn, paper_conn, now=None,
                             limit=20):
    """Recover unfinished attempts through the same authority/live/quota boundary."""
    import ai_capability_store as STORE

    STORE.ensure_schema(capability_conn)
    rows = capability_conn.execute(
        "SELECT adoption_id,capability_id,request_json,context_json FROM ai_adoption_attempts"
        " WHERE status='reserved' ORDER BY created_at LIMIT ?",
        (max(1, min(int(limit), 100)),)).fetchall()
    recovered = []
    for row in rows:
        context = _loads(_value(row, "context_json"))
        data = json.loads(_value(row, "request_json"))
        credential = STORE.load(capability_conn, _value(row, "capability_id"))
        if credential is None:
            continue
        for name in ("field_changes", "current_baseline", "requested_baseline"):
            data[name] = tuple((key, value) for key, value in data[name])
        request = CAP.AdoptionRequest(credential=credential, **data)
        try:
            adopt_candidate(capability_conn=capability_conn, adaptive_conn=adaptive_conn,
                            paper_conn=paper_conn, request=request,
                            candidate_id=int(context["candidate_id"]),
                            declared_baseline=context.get("declared_baseline"), now=now,
                            approved_by=context.get("approved_by", "bounded-auto"),
                            adoption_id=str(_value(row, "adoption_id")), recovery=True)
            recovered.append(int(context["candidate_id"]))
        except Exception:
            # Keep the durable attempt and its reserved budget for the next retry.
            continue
    return recovered
