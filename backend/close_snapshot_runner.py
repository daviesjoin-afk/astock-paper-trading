# -*- coding: utf-8 -*-
"""15:15 A股收盘快照确认：只刷新行情，不下单、不改参数。"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, "/app/backend")

import data_fetcher as dfc  # noqa: E402


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
        rows = dfc.fetch_market_snapshot_full(max_age=0, force=True)
        unique_rows = {
            str(row.get("code")): row for row in rows
            if isinstance(row, dict) and str(row.get("code") or "")
        }
        source_at = max((str(row.get("quote_at")) for row in unique_rows.values()
                         if row.get("quote_at")), default=None)
        # 完整性校验：行数必须达到全市场门槛，且行情时间必须不早于当日
        # 15:00 收盘（Asia/Shanghai）。此前只检查 >=1000 行，一份 14:57 的
        # 残缺快照也会被标记 completed 并被下游当作有效收盘数据。
        rows_ok = len(rows) >= dfc.FULL_MARKET_MIN_ROWS
        freshness_ok = False
        freshness_detail = "missing"
        fresh_rows = 0
        close_cutoff = None
        if source_at:
            try:
                tz = dt.timezone(dt.timedelta(hours=8))
                close_cutoff = dt.datetime.combine(today_cn, dt.time(15, 0), tzinfo=tz)
                for row in unique_rows.values():
                    try:
                        parsed = dt.datetime.fromisoformat(str(row.get("quote_at")).replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=tz)
                        if parsed.astimezone(tz) >= close_cutoff - dt.timedelta(minutes=1):
                            fresh_rows += 1
                    except (TypeError, ValueError, OverflowError):
                        continue
                required_fresh = max(dfc.FULL_MARKET_MIN_ROWS,
                                     int(len(unique_rows) * 0.90 + 0.9999))
                freshness_ok = fresh_rows >= required_fresh
                freshness_detail = (
                    f"fresh_rows={fresh_rows}/{len(unique_rows)} required={required_fresh}; "
                    f"source_at_max={source_at} vs cutoff={close_cutoff.isoformat()}"
                )
            except ValueError:
                freshness_detail = f"unparseable:{source_at}"
        status = "completed" if (rows_ok and freshness_ok) else (
            "failed" if not rows_ok else "stale_rejected"
        )
        payload = {
            "status": status,
            "slot": "close-snapshot",
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
