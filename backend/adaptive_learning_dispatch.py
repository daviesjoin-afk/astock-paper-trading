# -*- coding: utf-8 -*-
"""Durable, atomic dispatch for isolated adaptive-learning jobs."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows source tests
    fcntl = None

BASE = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = Path(os.getenv("ASTOCK_CACHE_DIR") or (BASE / "data_cache"))
JOB_DIR = CACHE_DIR / "adaptive_learning_jobs"
PENDING_DIR = JOB_DIR / "pending"
RUNNING_DIR = JOB_DIR / "running"
TERMINAL_DIR = JOB_DIR / "terminal"
STATUS_PATH = JOB_DIR / "status.json"
LOCK_PATH = JOB_DIR / ".dispatch.lock"
_LOCAL_LOCK = threading.Lock()


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _directories():
    for path in (PENDING_DIR, RUNNING_DIR, TERMINAL_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def _dispatch_lock():
    _directories()
    with _LOCAL_LOCK:
        handle = open(LOCK_PATH, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def _jobs(folder):
    return sorted(folder.glob("*.json"), key=lambda path: path.name)


def read_status():
    state = _read(STATUS_PATH) or {"status": "idle"}
    state["running"] = state.get("status") in {"queued", "claimed", "running"}
    return state


def enqueue(trigger):
    with _dispatch_lock():
        active = _jobs(RUNNING_DIR) + _jobs(PENDING_DIR)
        if active:
            state = read_status()
            if not state.get("job_id"):
                state = _read(active[0])
                state["status"] = "running" if active[0].parent == RUNNING_DIR else "queued"
            return False, state
        job_id = uuid.uuid4().hex
        request = {
            "job_id": job_id,
            "logical_key": "manual-adaptive-learning",
            "attempt": 0,
            "trigger": str(trigger or "manual")[:40],
            "requested_at": _now(),
        }
        _write(PENDING_DIR / f"{job_id}.json", request)
        _write(STATUS_PATH, {**request, "status": "queued", "updated_at": _now()})
        return True, read_status()


def recover_orphaned():
    """Mark orphaned work interrupted; never replay an unknown committed stage."""
    with _dispatch_lock():
        for path in _jobs(RUNNING_DIR):
            request = _read(path)
            terminal = {
                **request,
                "status": "interrupted",
                "error": "task worker restarted during learning; automatic replay suppressed",
                "finished_at": _now(),
                "updated_at": _now(),
            }
            _write(TERMINAL_DIR / path.name, terminal)
            path.unlink(missing_ok=True)
            _write(STATUS_PATH, terminal)


def claim():
    with _dispatch_lock():
        if _jobs(RUNNING_DIR):
            return None
        pending = _jobs(PENDING_DIR)
        if not pending:
            return None
        source = pending[0]
        request = _read(source)
        if not request.get("job_id"):
            source.unlink(missing_ok=True)
            return None
        target = RUNNING_DIR / source.name
        os.replace(source, target)
        request.update({
            "status": "running",
            "attempt": int(request.get("attempt") or 0) + 1,
            "worker_pid": os.getpid(),
            "started_at": _now(),
            "updated_at": _now(),
        })
        _write(target, request)
        _write(STATUS_PATH, request)
        return request


def requeue(request, reason):
    with _dispatch_lock():
        source = RUNNING_DIR / f"{request.get('job_id')}.json"
        if not source.exists():
            return
        request.update({"status": "queued", "message": str(reason)[:300], "updated_at": _now()})
        _write(source, request)
        os.replace(source, PENDING_DIR / source.name)
        _write(STATUS_PATH, request)


def finish(request, status, error=None):
    with _dispatch_lock():
        source = RUNNING_DIR / f"{request.get('job_id')}.json"
        if not source.exists():
            return
        terminal = {
            **request,
            "status": str(status),
            "error": str(error)[:800] if error else None,
            "finished_at": _now(),
            "updated_at": _now(),
        }
        _write(source, terminal)
        os.replace(source, TERMINAL_DIR / source.name)
        _write(STATUS_PATH, terminal)
