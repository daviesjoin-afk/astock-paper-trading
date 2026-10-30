"""Process one durable Dashboard snapshot job, independently of the trading runner."""
import json
import datetime as dt
from pathlib import Path
import sqlite3

import paper_job_lifecycle as PJL
import paper_storage as PST


def refresh_work_due():
    """Inspect through the existing read-only storage/job owners, without bootstrapping."""
    path = PST.paper_db_path()
    if not Path(path).is_file():
        return True  # Missing database must still use the normal initialization path.
    try:
        with PST.paper_db_readonly(db_path=path) as conn:
            return PJL.snapshot_refresh_due(conn, now=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except sqlite3.OperationalError as exc:
        if "no such table: paper_dashboard_refresh_jobs" in str(exc):
            return True
        raise


def main():
    if not refresh_work_due():
        print(json.dumps({"status": "idle"}))
        return 0
    import paper_trading as paper

    paper.init_db()
    result = paper.run_risk_snapshot_refresh_job()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"completed", "idle"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
