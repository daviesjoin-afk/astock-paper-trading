"""Durable display refresh orchestration; no trading writes or provider policy."""
from __future__ import annotations

import datetime as dt
import threading
import uuid

import paper_job_lifecycle as PJL

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
RETRY_SECONDS = 60
MAX_QUEUE_AGE_SECONDS = 300
MAX_CONCURRENCY = 1


def _stamp(offset=0):
    return (dt.datetime.now() + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")


def _claim_wait_seconds(row, *, now, live, follow_up):
    """Queue waiting only; a collector's own run time never counts as waiting.

    The cap bounds *how long a request waits to be claimed*, not how long the
    provider takes. Four cases, in order:

      * a follow-up request — waits on its own request time, whatever the
        current status is. A follow-up issued 1s into a 250s run has already
        waited 309s by the time the 60s retry backoff ends, and must expire
        even though the row now reads ``failed``;
      * still collecting with a live lease — waited until the claim
        (``started_at``), so a slow provider cannot consume its own budget;
      * ran and failed on the same request version — the current wait started
        when that attempt failed. A 250s run plus a 60s backoff is 60s of
        waiting, not 310s;
      * queued, or an orphaned claim — waits since ``requested_at``. An
        orphaned claim has no scheduled retry, so the original request age is
        what stops us from reviving stale intent.

    ``fail_snapshot_refresh`` keeps ``request_version > claimed_version``
    untouched, so "is this a follow-up" must not depend on ``status == running``:
    after a failure the same row still carries an unclaimed follow-up.
    """
    try:
        requested = dt.datetime.fromisoformat(row["requested_at"])
        now_dt = dt.datetime.fromisoformat(now)
        age = (now_dt - requested).total_seconds()
        if follow_up:
            # Fail closed: a follow-up still needs a usable request timestamp.
            return age if age >= 0 else None
        if row["status"] == "running" and live:
            age = (dt.datetime.fromisoformat(row["started_at"]) - requested).total_seconds()
            return age if age >= 0 else None
        if row["status"] == "failed" and row["finished_at"]:
            # max() also keeps a future/invalid request time fail-closed.
            since = max(dt.datetime.fromisoformat(row["finished_at"]), requested)
            age = (now_dt - since).total_seconds()
            return age if age >= 0 else None
        age = (now_dt - requested).total_seconds()
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


def queue_state(row, *, now):
    """Read-only admission facts; pending age is not provider snapshot age."""
    reason = "no_request"
    age = None
    if row is not None:
        status = row["status"]
        live = status == "running" and row["expires_at"] and row["expires_at"] > now
        pending = status in {"queued", "failed"} or (
            status == "running" and row["request_version"] > row["claimed_version"])
        # A coalesced follow-up keeps its own request time in every status:
        # fail_snapshot_refresh leaves request_version > claimed_version set.
        # claimed_version must be non-zero, otherwise a never-claimed first
        # request (1 > 0) would masquerade as a follow-up of itself.
        follow_up = row["claimed_version"] > 0 and row["request_version"] > row["claimed_version"]
        if pending or status == "running":
            # A running collector's elapsed time is not queue waiting, and a
            # failed attempt's run time is not waiting for its own retry.
            age = _claim_wait_seconds(row, now=now, live=bool(live), follow_up=follow_up)
        reason = {"completed": "no_pending_request", "expired": "request_expired"}.get(status, "due")
        if live:
            reason = "already_running"
        elif status == "failed" and row["next_retry_at"] and row["next_retry_at"] > now:
            reason = "retry_backoff"
        elif status in {"queued", "failed", "running"}:
            if age is None:
                reason = "invalid_requested_at"
            elif age > MAX_QUEUE_AGE_SECONDS:
                reason = "queue_age_exceeded"
    return {"reason": reason, "queue_age_seconds": age,
            "max_queue_age_seconds": MAX_QUEUE_AGE_SECONDS, "max_concurrency": MAX_CONCURRENCY}


def request_refresh(db_factory, *, trigger, request_kind="automatic", snapshot_missing=False):
    with db_factory(immediate=True, hot_path=True) as conn:
        row = PJL.enqueue_snapshot_refresh(
            conn, trigger=trigger, now=_stamp(), request_kind=request_kind,
            snapshot_missing=snapshot_missing, completed_before=_stamp(-PJL.SNAPSHOT_AUTO_REFRESH_SECONDS),
        )
    return {"status": {"queued": "scheduled", "running": "already_running",
                       "failed": "backoff", "completed": "completed"}[row["status"]]}


def refresh_status(db_factory):
    with db_factory() as conn:
        row = PJL.read_snapshot_refresh(conn)
    facts = queue_state(row, now=_stamp())
    if row is None:
        return {"status": "idle", "running": False, "last_error": None, "skip_count": 0} | facts
    return {key: row[key] for key in (
        "status", "trigger", "requested_at", "started_at", "finished_at", "heartbeat_at",
        "expires_at", "attempt", "next_retry_at", "last_error", "last_completed_at",
        "skip_count", "last_skip_reason", "last_skipped_at", "last_queue_age_seconds",
    )} | {"running": row["status"] in {"queued", "running"}} | facts


def run_refresh_job(db_factory, refresh):
    """Run in a durable external process; the only thread here renews its lease.

    ``refresh(publish)`` collects/builds outside a writer transaction, then calls
    ``publish(write_files)`` exactly once. Only file publication and completion
    run under the short fenced transaction, so an obsolete worker cannot write.
    """
    owner = uuid.uuid4().hex
    with db_factory(immediate=True, hot_path=True) as conn:
        now = _stamp()
        row = PJL.read_snapshot_refresh(conn)
        facts = queue_state(row, now=now)
        if facts["reason"] in {"queue_age_exceeded", "invalid_requested_at"} and PJL.expire_snapshot_refresh(
            conn, now=now, reason=facts["reason"], queue_age_seconds=facts["queue_age_seconds"],
        ):
            return {"status": "skipped"} | facts
        job = PJL.claim_snapshot_refresh(conn, owner=owner, now=now, expires_at=_stamp(LEASE_SECONDS))
    if job is None:
        return {"status": "idle"} | facts
    fence = job["fencing_token"]
    stop = threading.Event()
    lost = threading.Event()
    published = False

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with db_factory(immediate=True, hot_path=True) as conn:
                    if not PJL.renew_snapshot_refresh(
                        conn, owner=owner, fencing_token=fence, now=_stamp(), expires_at=_stamp(LEASE_SECONDS),
                    ):
                        lost.set()
                        return
            except Exception:
                # Failure cannot grant authority; publication still checks expiry.
                continue

    def publish(write_files):
        nonlocal published
        if published or lost.is_set():
            raise PJL.SnapshotLeaseLost("dashboard snapshot publication no longer allowed")
        with db_factory(immediate=True, hot_path=True) as conn:
            publication_at = _stamp()
            PJL.assert_snapshot_refresh_lease(conn, owner=owner, fencing_token=fence, now=publication_at)
            write_files()
            PJL.complete_snapshot_refresh(conn, owner=owner, fencing_token=fence, now=publication_at)
        published = True

    thread = threading.Thread(target=heartbeat, name="snapshot-lease-heartbeat", daemon=False)
    thread.start()
    try:
        refresh(publish)
        if not published:
            raise RuntimeError("snapshot refresh returned without publication")
        return {"status": "completed"}
    except Exception as exc:
        with db_factory(immediate=True, hot_path=True) as conn:
            changed = PJL.fail_snapshot_refresh(
                conn, owner=owner, fencing_token=fence, now=_stamp(), retry_at=_stamp(RETRY_SECONDS),
                error=f"{type(exc).__name__}: {exc}",
            )
        return {"status": "failed" if changed else "lease_lost", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop.set()
        thread.join()
