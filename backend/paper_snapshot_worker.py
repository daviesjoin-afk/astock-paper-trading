"""Process one durable Dashboard snapshot job, independently of the trading runner."""
import argparse
import json
import datetime as dt
from pathlib import Path
import sqlite3

import data_paths
import paper_dispatch_metrics as Metrics
import paper_snapshot_jobs as Jobs
import paper_job_lifecycle as PJL
import paper_storage as PST


def refresh_precheck():
    """Read-only hint; no lease or heavy engine import on idle ticks."""
    path = PST.paper_db_path()
    if not Path(path).is_file():
        return {"due": True, "reason": "database_missing"}
    try:
        with PST.paper_db_readonly(db_path=path) as conn:
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return {"due": PJL.snapshot_refresh_due(conn, now=now)} | Jobs.queue_state(
                PJL.read_snapshot_refresh(conn), now=now)
    except sqlite3.OperationalError as exc:
        if "no such table: paper_dashboard_refresh_jobs" in str(exc):
            return {"due": True, "reason": "schema_missing"}
        raise


def refresh_work_due():
    return refresh_precheck()["due"]


def main(*, metrics=False):
    if metrics:
        state = Jobs.refresh_status(PST.paper_db_readonly)
        log = Metrics.summarize_file(Path(data_paths.report_dir()) / "snapshot-refresh.log")
        print(json.dumps({"queue": state, "events": log}, ensure_ascii=False))
        return 0
    before = refresh_precheck()
    if not before["due"]:
        result = {"status": "idle"} | before
    else:
        import paper_trading as paper

        paper.init_db()
        result = paper.run_risk_snapshot_refresh_job()
    event = {"event": "dashboard_refresh", "lane": "dashboard-refresh",
             "at": dt.datetime.now().isoformat(timespec="seconds"),
             "reason": result.get("reason", result["status"]),
             "queue_age_seconds": result.get("queue_age_seconds", before.get("queue_age_seconds"))} | result
    print(json.dumps(event, ensure_ascii=False))
    return 0 if result["status"] in {"completed", "idle", "skipped"} else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", action="store_true", help="Read queue state and bounded recent skip counters")
    raise SystemExit(main(metrics=parser.parse_args().metrics))
