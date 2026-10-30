"""PR298 隔离编排计时：固定 degraded 响应，无缓存、无锁竞争；非线上吞吐测试。"""

import contextlib
import datetime as dt
import json
from pathlib import Path
import statistics
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path.cwd() / "backend"))
import paper_trading as P

samples = []
for _ in range(20):
    calls = {"health": 0, "snapshot": 0, "risk": 0, "resume": 0}
    start = time.perf_counter()
    metrics = {}

    def health(calls=calls, **kw):
        calls["health"] += 1
        time.sleep(0.002)
        return {}

    def refresh(calls=calls, **kw):
        calls["snapshot"] += 1
        time.sleep(0.030)
        return []

    def risk(*a, calls=calls, metrics=metrics, start=start):
        calls["risk"] += 1
        metrics["risk_start"] = time.perf_counter() - start
        t = time.perf_counter()
        time.sleep(0.010)
        metrics["risk_duration"] = time.perf_counter() - t
        return {"orders": []}

    def resume(*a, calls=calls):
        calls["resume"] += 1
        time.sleep(0.005)
        return [{"order_id": 7, "status": "partially_filled", "filled_qty": 300, "remaining_qty": 700}]

    gate = {
        "ready": False,
        "covered_codes": 0,
        "eligible_codes": 5000,
        "coverage_pct": 0.0,
        "required_codes": 4500,
    }
    with contextlib.ExitStack() as st:
        for name, value in [
            ("init_db", None),
            ("_cleanup_stale_data", None),
            ("_validated_live_universe", []),
            ("_live_scan_gate", gate),
            ("_ignition_shadow_backfill", None),
            ("_active_account_rows", []),
            ("_market_state", {}),
            ("_active_cycle", {"id": 1}),
            ("_cached_close_market", {}),
        ]:
            st.enter_context(mock.patch.object(P, name, return_value=value))
        st.enter_context(mock.patch.object(P, "_db"))
        st.enter_context(mock.patch.object(P, "monitor_risk", side_effect=risk))
        st.enter_context(mock.patch.object(P, "process_pending_execution_orders", side_effect=resume))
        st.enter_context(mock.patch.object(P.dfc, "check_data_source_health", side_effect=health))
        st.enter_context(mock.patch.object(P.MDSvc, "refresh_rows", side_effect=refresh))
        st.enter_context(mock.patch.object(P.MDSvc, "read_snapshot", side_effect=ValueError))
        st.enter_context(mock.patch.object(P.dfc, "full_market_snapshot_source", return_value={}))
        result = P.monitor_intraday(dt.datetime(2026, 10, 20, 10), force=True)
    metrics["scan_duration"] = time.perf_counter() - start
    samples.append({"metrics": metrics, "calls": calls, "result": result})
Path(sys.argv[1]).write_text(json.dumps(samples, ensure_ascii=False), encoding="utf8")
print(
    {k: round(statistics.median(s["metrics"][k] for s in samples) * 1000, 3) for k in samples[0]["metrics"]}
)
