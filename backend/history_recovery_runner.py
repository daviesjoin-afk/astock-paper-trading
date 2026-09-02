# -*- coding: utf-8 -*-
"""盘后 K 线恢复任务：只修复数据，不生成信号、不下单。"""
import argparse
import datetime as dt
import json

import data_fetcher as dfc
import universe as U
from resource_guard import heavy_job_lease


def run_recovery(workers=2, max_seconds=780):
    today = dt.datetime.now().date()
    if not U.is_trade_day(today):
        return {"slot": "history-recovery", "status": "skipped",
                "reason": "non-trading-day", "target_date": U.latest_complete_trade_date(today).isoformat()}
    # This is a resumable recovery queue.  Two well-behaved streams are safer
    # and faster overall than six rate-limited requests fighting the Web API.
    with heavy_job_lease("history-recovery") as admission:
        if not admission.get("allowed"):
            return {"slot": "history-recovery", "status": "deferred", "admission": admission}
        return _run_recovery(workers=workers, max_seconds=max_seconds, admission=admission)


def _run_recovery(workers=2, max_seconds=780, admission=None):
    target = U.latest_complete_trade_date(dt.date.today())
    try:
        source_health = dfc.check_data_source_health(force=True)
    except Exception as exc:  # 下载链仍会逐源重试，健康探针失败不应中断恢复。
        source_health = {"healthy": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        refresh = U.refresh_history(
            asof_day=target,
            workers=max(1, min(int(workers), 6)),
            max_seconds=max(60, int(max_seconds)),
        )
    except Exception as exc:
        refresh = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    coverage = U.coverage_report()
    # 历史补齐完成后只重建共享因子缓存，不补发信号、更不会下单。这样凌晨
    # 恢复完成的数据能在下一交易日开盘前立即供选股和风控读取，而不是再等
    # 到下一次 15:05 收盘任务。
    factor_refresh = {"status": "waiting", "reason": "完整日线覆盖尚未达标"}
    if coverage.get("ready"):
        try:
            import paper_trading as P
            factor_refresh = P._rebuild_selection_factor_cache(target)
        except Exception as exc:
            factor_refresh = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    refresh_status = str((refresh or {}).get("status") or "failed")
    factor_status = str((factor_refresh or {}).get("status") or "waiting")
    completed = refresh_status in {"ok", "up_to_date"} and factor_status in {"ok", "up_to_date"}
    return {
        "slot": "history-recovery",
        "status": "ok" if completed else "partial",
        "admission": admission,
        "target_date": target.isoformat(),
        "source_health": source_health,
        "history_refresh": refresh,
        "factor_refresh": factor_refresh,
        "coverage": {
            key: coverage.get(key)
            for key in ("history_required", "fresh", "fresh_pct", "fresh_selection",
                        "fresh_selection_pct", "stale", "fallback_unadjusted", "ready")
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-seconds", type=int, default=780)
    args = parser.parse_args()
    result = run_recovery(args.workers, args.max_seconds)
    print(json.dumps(result, ensure_ascii=False, default=str))
    # A partial/failed recovery is retryable and must be visible to cron and
    # monitoring through a non-zero exit code.  ``skipped`` is a clean result
    # on holidays/non-session dates.
    return 0 if result.get("status") in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
