"""Offline PR299 before/after probe; run this same absolute script in each checkout.

Provider counters are fixed-response substitutes, NOT production HTTP attempts.
Uses only temporary ledgers. Startup samples are fresh Python processes with
warm filesystem caches; the measured interval includes process creation/exit.
"""
import argparse
import contextlib
import datetime as dt
import itertools
import io
import json
import math
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from unittest import mock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--startup-samples", type=int, default=10)
    args = parser.parse_args()
    backend = Path.cwd() / "backend"
    sys.path.insert(0, str(backend))
    with tempfile.TemporaryDirectory(prefix="pr299-probe-") as tmp, contextlib.ExitStack() as stack:
        root = Path(tmp)
        stack.enter_context(mock.patch.dict(os.environ, ASTOCK_DATA_DIR=str(root),
                                           ASTOCK_ENABLE_FALLBACK_THREADS="0", LLM_ADVISOR_ENABLED="0"))
        stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=RuntimeError("offline probe")))
        import api_paper as API
        import paper_job_lifecycle as PJL
        import paper_snapshot_jobs as Jobs
        import paper_trading as P

        now = dt.datetime(2026, 10, 30, 10)
        stack.enter_context(mock.patch.object(Jobs, "_stamp", side_effect=lambda offset=0:
                            (now + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")))
        stack.enter_context(mock.patch.object(P, "_risk_base_dashboard", return_value={"positions": []}))
        stack.enter_context(mock.patch.object(P.RC, "load_snapshot", return_value={"asof": "fixed old source"}))
        stack.enter_context(mock.patch.object(P.RC, "snapshot_age_seconds", return_value=9999))
        stack.enter_context(mock.patch.object(P.RC, "build_dashboard", side_effect=lambda *_: {}))
        stack.enter_context(mock.patch.object(P, "_market_state", return_value={}))
        stack.enter_context(mock.patch.object(P.U, "load_universe", return_value=[]))
        stack.enter_context(mock.patch.object(P.dfc, "load_source_health", return_value={}))
        generations = itertools.count()
        stack.enter_context(mock.patch.object(API, "_current_cache_generation", side_effect=lambda: str(next(generations))))

        def capture():
            with P._db_readonly() as conn:
                row = PJL.read_snapshot_refresh(conn)
            return {key: row[key] for key in ("request_version", "claimed_version", "attempt", "status")}

        def gets():
            for _ in range(50):
                API.risk_overview()

        results = {}
        for scenario in ("running_gets", "running_manual_then_gets", "post_then_get", "failed_backoff",
                         "completed_old_source", "already_done"):
            case = root / scenario
            case.mkdir()
            P.DB_PATH = str(case / "paper_trading.sqlite3")
            P.init_db()
            API._cclear()
            calls = {"market": 0, "sector": 0, "sector_flow": 0}
            hook = None
            during = None

            def market(scenario=scenario, calls=calls):
                nonlocal hook, during
                calls["market"] += 1
                if scenario == "failed_backoff":
                    raise RuntimeError("fixed offline provider failure")
                if hook:
                    action, hook = hook, None
                    action()
                    during = capture()
                return []

            def sector(calls=calls):
                calls["sector"] += 1
                return []

            def sector_flow(*_, calls=calls):
                calls["sector_flow"] += 1
                return []

            def drain():
                for _ in range(3):
                    if P.run_risk_snapshot_refresh_job()["status"] != "completed":
                        break

            with mock.patch.object(P.MDSvc, "refresh_rows", side_effect=market), \
                    mock.patch.object(P.dfc, "fetch_hot_sector_snapshot", side_effect=sector), \
                    mock.patch.object(P.dfc, "fetch_sector_flow", side_effect=sector_flow), \
                    mock.patch.object(P.RC, "SNAPSHOT_PATH", str(case / "snapshot.json")), \
                    mock.patch.object(P.RC, "HISTORY_PATH", str(case / "history.json")):
                if scenario == "post_then_get":
                    API.risk_refresh()
                    API.risk_overview()
                    during = capture()
                    drain()
                else:
                    API.risk_overview()
                    if scenario == "running_gets":
                        hook = gets
                    elif scenario == "running_manual_then_gets":
                        hook = lambda: (API.risk_refresh(), gets())
                    drain()
                    if scenario == "failed_backoff":
                        gets()
                        drain()
                    elif scenario == "completed_old_source":
                        for _ in range(5):
                            gets()
                            drain()
                    elif scenario == "already_done":
                        with mock.patch.object(P, "init_db"), mock.patch.object(P.PSS, "run_preflight"), \
                                mock.patch.object(P, "_run_slot_impl", return_value={"status": "already_done"}), \
                                contextlib.redirect_stderr(io.StringIO()):
                            for _ in range(50):
                                P.run_slot("risk", asof_date="2026-10-30")
                        drain()
                results[scenario] = {"during": during, "final": capture(), "provider_substitute_calls": calls,
                                     "refreshes": calls["market"]}

        # A full, initialized empty ledger, shared by both variants' idle process samples.
        idle = root / "idle"
        idle.mkdir()
        P.DB_PATH = str(idle / "paper_trading.sqlite3")
        P.init_db()
        child = """import json, runpy, socket, sys
sys.path.insert(0, sys.argv[1])
def denied(*args, **kwargs):
    raise RuntimeError('offline startup probe')
socket.socket.connect = denied
try:
    runpy.run_path(sys.argv[2], run_name='__main__')
except SystemExit as exc:
    if exc.code not in (0, None):
        raise
print(json.dumps({'heavy_imported': 'paper_trading' in sys.modules}))
"""
        env = dict(os.environ, ASTOCK_DATA_DIR=str(idle), PYTHONDONTWRITEBYTECODE="1")
        durations = []
        imports = []
        for index in range(args.startup_samples + 1):
            started = time.perf_counter()
            completed = subprocess.run([sys.executable, "-c", child, str(backend),
                                        str(backend / "paper_snapshot_worker.py")],
                                       env=env, capture_output=True, text=True, timeout=30, check=True)
            elapsed = (time.perf_counter() - started) * 1000
            if index:
                durations.append(elapsed)
                imports.append(json.loads(completed.stdout.splitlines()[-1])["heavy_imported"])
        durations.sort()
        results["idle_startup"] = {
            "samples": len(durations), "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(durations[math.ceil(len(durations) * .95) - 1], 3),
            "max_ms": round(max(durations), 3), "heavy_imports": sum(imports),
        }
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
