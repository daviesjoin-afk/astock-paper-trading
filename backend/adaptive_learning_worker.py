# -*- coding: utf-8 -*-
"""Resident dispatcher for isolated, one-shot adaptive learning children."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import adaptive_learning_dispatch as dispatch


def _execute_one(request):
    import adaptive_engine as adaptive
    from resource_guard import heavy_job_lease

    trigger = f"{str(request.get('trigger') or 'manual-worker')[:40]}:{request.get('job_id', '')[:8]}"
    with heavy_job_lease(f"adaptive-manual-{request.get('request_id', '')[:8]}") as admission:
        if not admission.get("allowed"):
            print(json.dumps({"status": "deferred", "admission": admission}, ensure_ascii=False), flush=True)
            return 75
        adaptive.run_learning_cycle(trigger=trigger)
        with adaptive._connect() as conn:
            row = conn.execute(
                "SELECT status,detail FROM adaptive_runs WHERE trigger=? ORDER BY id DESC LIMIT 1",
                (trigger,),
            ).fetchone()
        status = str(row["status"] if row else "missing")
        if status != "completed":
            raise RuntimeError(f"learning_run_{status}: {row['detail'] if row else 'missing run row'}")
    return 0


def _child_main(raw_request):
    try:
        request = json.loads(raw_request)
        return _execute_one(request)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
        return 1


def run_forever(poll_seconds=2.0):
    dispatch.recover_orphaned()
    while True:
        request = dispatch.claim()
        if request is None:
            time.sleep(poll_seconds)
            continue
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--run-one", json.dumps(request, ensure_ascii=False)],
            env=env,
        )
        if result.returncode == 75 and int(request.get("attempt") or 0) < 3:
            dispatch.requeue(request, "heavy job busy or memory high; queued for retry")
            time.sleep(15)
        elif result.returncode == 0:
            dispatch.finish(request, "completed")
        else:
            dispatch.finish(request, "failed", f"learning worker exit {result.returncode}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-one":
        raise SystemExit(_child_main(sys.argv[2]))
    run_forever()
