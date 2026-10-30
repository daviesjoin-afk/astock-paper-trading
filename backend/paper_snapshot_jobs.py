"""Durable display refresh orchestration; no trading writes or provider policy."""
from __future__ import annotations

import datetime as dt
import threading
import uuid

import paper_job_lifecycle as PJL

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
RETRY_SECONDS = 60


def _stamp(offset=0):
    return (dt.datetime.now() + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")


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
    if row is None:
        return {"status": "idle", "running": False, "last_error": None}
    return {key: row[key] for key in (
        "status", "trigger", "requested_at", "started_at", "finished_at", "heartbeat_at",
        "expires_at", "attempt", "next_retry_at", "last_error", "last_completed_at",
    )} | {"running": row["status"] in {"queued", "running"}}


def run_refresh_job(db_factory, refresh):
    """Run in a durable external process; the only thread here renews its lease.

    ``refresh(publish)`` collects/builds outside a writer transaction, then calls
    ``publish(write_files)`` exactly once. Only file publication and completion
    run under the short fenced transaction, so an obsolete worker cannot write.
    """
    owner = uuid.uuid4().hex
    with db_factory(immediate=True, hot_path=True) as conn:
        job = PJL.claim_snapshot_refresh(conn, owner=owner, now=_stamp(), expires_at=_stamp(LEASE_SECONDS))
    if job is None:
        return {"status": "idle"}
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
