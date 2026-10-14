# -*- coding: utf-8 -*-
"""15:15 A股收盘快照确认：只刷新行情，不下单、不改参数。"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, "/app/backend")

import data_fetcher as dfc  # noqa: E402
import market_data_contract as MDC  # noqa: E402
import market_data_service as MDSvc  # noqa: E402


def main():
    started = dt.datetime.now(dt.timezone.utc)
    try:
        # A weekday is not necessarily an A-share session.  The close
        # snapshot must never publish a holiday/partial-day result.
        import universe as U
        today_cn = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        if not U.is_trade_day(today_cn):
            payload = {
                "status": "skipped", "slot": "close-snapshot",
                "reason": "non-trading-day", "started_at": started.isoformat(),
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        reading = MDSvc.refresh_snapshot(
            MDC.CLOSE_SNAPSHOT_POLICY, now=started, asof_day=today_cn, force=True,
        )
        rows = [dict(row) for row in reading.rows()]
        unique_rows = {str(row.get("code")): row for row in rows if row.get("code")}
        source_at = reading.snapshot.observed_at if reading.snapshot else None
        detail = dict(reading.snapshot.verification_detail) if reading.snapshot else {}
        fresh_rows = int(detail.get("close_fresh_rows") or 0)
        rows_ok = len(unique_rows) >= dfc.FULL_MARKET_MIN_ROWS
        freshness_ok = (reading.available and reading.freshness == MDC.FRESHNESS_FRESH
                        and fresh_rows >= dfc.FULL_MARKET_MIN_ROWS)
        window = MDC.close_snapshot_window(started, today_cn)
        close_cutoff = (window[0] + dt.timedelta(seconds=MDC.CLOSE_SNAPSHOT_POLICY.max_age_seconds)
                        if window else None)
        freshness_detail = (
            f"fresh_rows={fresh_rows}/{len(rows)}; reason={reading.reason}; "
            f"minimum_quote_at={detail.get('close_min_quote_at')}"
        )
        status = "completed" if (rows_ok and freshness_ok) else (
            "failed" if not rows_ok else "stale_rejected"
        )
        payload = {
            "status": status,
            "slot": "close-snapshot",
            "market_data": reading.projection(),
            "rows": len(rows),
            "unique_rows": len(unique_rows),
            "required_rows": dfc.FULL_MARKET_MIN_ROWS,
            "rows_ok": rows_ok,
            "fresh_rows": fresh_rows,
            "fresh_coverage_pct": round(fresh_rows / max(len(unique_rows), 1) * 100, 2),
            "freshness_ok": freshness_ok,
            "freshness_detail": freshness_detail,
            "source_at": source_at,
            "close_cutoff": close_cutoff.isoformat() if close_cutoff else None,
            "started_at": started.isoformat(),
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    except Exception as exc:
        payload = {
            "status": "failed",
            "slot": "close-snapshot",
            "error": f"{type(exc).__name__}: {exc}",
            "started_at": started.isoformat(),
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
