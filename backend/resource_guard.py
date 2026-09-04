# -*- coding: utf-8 -*-
"""Small cross-process resource lease for non-trading batch jobs.

The paper runner must stay responsive even when full-market history, factor
rebuilds or adaptive learning are slow.  These jobs run in a separate worker
container, but still share one cache volume, so this file lock is the final
admission-control boundary across cron invocations and manual runs.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path

try:  # Linux production image; keep imports harmless for Windows tests.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = Path(os.getenv("ASTOCK_CACHE_DIR") or (BASE_DIR / "data_cache"))
LOCK_PATH = CACHE_DIR / ".resource-heavy.lock"
STATE_PATH = CACHE_DIR / "resource_heavy_status.json"
STATE_LOCK_PATH = CACHE_DIR / ".resource-heavy-state.lock"
DEFAULT_HIGH_WATER_PCT = float(os.getenv("ASTOCK_WORKER_HIGH_WATER_PCT") or "88")
DEFAULT_LEASE_TTL_SECONDS = max(60, int(os.getenv("ASTOCK_HEAVY_LEASE_TTL_SECONDS") or "900"))


def _read_int(path: str):
    try:
        value = open(path, encoding="utf-8").read().strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def memory_snapshot():
    """Return cgroup memory facts without allocating data frames or network I/O."""
    current = _read_int("/sys/fs/cgroup/memory.current")
    maximum = _read_int("/sys/fs/cgroup/memory.max")
    events = {}
    try:
        for line in open("/sys/fs/cgroup/memory.events", encoding="utf-8"):
            key, value = line.split()[:2]
            events[key] = int(value)
    except (OSError, ValueError):
        pass
    pct = round(current / maximum * 100, 1) if current is not None and maximum else None
    return {"current_bytes": current, "max_bytes": maximum, "usage_pct": pct, "events": events}


@contextlib.contextmanager
def _state_file_lock():
    """Serialize status-file updates without touching the admission lock.

    A rejected cron invocation must not truncate the status written by the
    process that currently owns the heavy-job lease.  The separate lock also
    makes the state file safe when the API and worker containers share the
    cache volume.
    """
    handle = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        handle = open(STATE_LOCK_PATH, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def _read_state_unlocked():
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(payload, *, owner_id=None, preserve_active=True):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _state_file_lock():
            current = _read_state_unlocked()
            # A denied process may report its own admission failure, but it
            # cannot replace an active owner's lease state.  Keep the event as
            # an audit field on the active record instead.
            current_owner = str(current.get("owner_id") or "")
            current_expiry = _parse_timestamp(current.get("expires_at"))
            active = bool(
                preserve_active
                and current.get("status") == "running"
                and current_owner
                and current_expiry is not None
                and current_expiry > dt.datetime.now(dt.timezone.utc)
                and current_owner != str(owner_id or "")
            )
            if active:
                current["last_event"] = payload
                current["last_event_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                payload = current
            tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
    except (OSError, TypeError, ValueError):
        pass


def _parse_timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


@contextlib.contextmanager
def heavy_job_lease(job_name: str, high_water_pct: float | None = None,
                    ttl_seconds: int | None = None):
    """Yield a fenced, heartbeating admission decision.

    ``flock`` is the authoritative cross-process lease.  The persisted owner,
    fencing token and heartbeat make the state observable and prevent a
    blocked contender from overwriting the active job.  Callers should carry
    ``fencing_token`` into any durable side effect they create.
    """
    high_water_pct = float(high_water_pct or DEFAULT_HIGH_WATER_PCT)
    ttl_seconds = max(60, int(ttl_seconds or DEFAULT_LEASE_TTL_SECONDS))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+", encoding="utf-8")
    acquired = False
    heartbeat_stop = threading.Event()
    owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"
    fencing_token = uuid.uuid4().hex

    def _heartbeat(state):
        interval = max(10.0, min(30.0, ttl_seconds / 3.0))
        while not heartbeat_stop.wait(interval):
            now = dt.datetime.now(dt.timezone.utc)
            state.update({
                "heartbeat_at": now.isoformat(),
                "expires_at": (now + dt.timedelta(seconds=ttl_seconds)).isoformat(),
            })
            _write_state(state, owner_id=owner_id, preserve_active=True)

    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                snapshot = memory_snapshot()
                result = {"allowed": False, "reason": "heavy_job_busy", "job": job_name,
                          "memory": snapshot, "owner_id": owner_id,
                          "fencing_token": fencing_token}
                _write_state({"status": "busy", **result,
                              "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                             owner_id=owner_id)
                yield result
                return
        else:
            acquired = True
        snapshot = memory_snapshot()
        if snapshot.get("usage_pct") is not None and snapshot["usage_pct"] >= high_water_pct:
            result = {"allowed": False, "reason": "memory_high_water", "job": job_name,
                      "memory": snapshot, "high_water_pct": high_water_pct}
            _write_state({"status": "deferred", **result,
                          "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                         owner_id=owner_id, preserve_active=False)
            yield result
            return
        now = dt.datetime.now(dt.timezone.utc)
        state = {
            "allowed": True, "job": job_name, "memory": snapshot,
            "high_water_pct": high_water_pct, "owner_id": owner_id,
            "fencing_token": fencing_token, "pid": os.getpid(),
            "status": "running", "started_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(seconds=ttl_seconds)).isoformat(),
        }
        _write_state(state, owner_id=owner_id, preserve_active=False)
        heartbeat = threading.Thread(target=_heartbeat, args=(state,),
                                     name=f"heavy-lease-{job_name}", daemon=True)
        heartbeat.start()
        try:
            yield state
        except BaseException as exc:
            heartbeat_stop.set()
            state.update({"status": "failed", "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "error": f"{type(exc).__name__}: {exc}"})
            _write_state(state, owner_id=owner_id, preserve_active=False)
            raise
        else:
            heartbeat_stop.set()
            state.update({"status": "completed", "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "memory_after": memory_snapshot()})
            _write_state(state, owner_id=owner_id, preserve_active=False)
    finally:
        heartbeat_stop.set()
        if acquired and fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
