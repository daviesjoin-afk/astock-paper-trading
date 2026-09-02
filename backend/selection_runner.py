# -*- coding: utf-8 -*-
"""Post-close job for daily screener snapshots and forward validation."""
import argparse
import datetime as dt
import json
import sys

import main as M
import paper_trading as P
import selection_tracking as ST
import strategies as S
from resource_guard import heavy_job_lease


def run_daily():
    now = dt.datetime.now(ST.CHINA_TZ)
    # Weekday-only checks run on statutory closures (Spring Festival, National
    # Day, etc.) and can publish a stale "daily" snapshot.  The shared universe
    # calendar is the single source of truth; if it cannot classify the date,
    # fail closed and let the next scheduled run retry.
    try:
        if not M.U.is_trade_day(now.date()):
            return {"status": "skipped", "reason": "non-trading-day"}
    except Exception as exc:
        return {"status": "skipped", "reason": "calendar-unavailable",
                "error": f"{type(exc).__name__}: {exc}"}
    if now.time() < dt.time(15, 15):
        return {"status": "skipped", "reason": "post-close only"}
    with heavy_job_lease("selection-daily") as admission:
        if not admission.get("allowed"):
            return {"status": "deferred", "reason": admission.get("reason"), "admission": admission}
        return _run_daily(admission, now)


def _run_daily(admission=None, now=None):
    # ``now`` must be passed in (or derived here): this function previously
    # referenced run_daily's local ``now``, raising NameError on every
    # scheduled post-close run that reached the freshness gate.
    if now is None:
        now = dt.datetime.now(ST.CHINA_TZ)
    ST.ensure_schema()
    tracked = ST.update_observations()
    # 盘后因子更新必须在选股前补齐。目标日期使用共享数据层的完整日线
    # 截止点：若行情源尚未发布当天收盘，就使用上一完整交易日，而不是
    # 把“昨天的有效响应”误判成过期数据。
    target_date = M._complete_daily_cutoff()
    refresh = None
    factor_refresh = None
    try:
        refresh = M.U.refresh_history(asof_day=target_date, workers=2, max_seconds=420)
        # A partial history refresh is not permission to publish a partial factor cache.
        if refresh.get("status") in {"ok", "up_to_date"}:
            factor_refresh = P._rebuild_selection_factor_cache(target_date)
    except Exception as exc:
        refresh = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    saved = []
    failures = []
    refresh_gate = None
    # Do not publish a new post-close snapshot if the current day's complete
    # bars were not actually written.  Without this gate the selector would
    # pass its normal "previous trading day is fresh" check and silently
    # save yesterday's candidates under today's run date.
    if target_date == now.date():
        universe = M.U.load_universe() or []
        required_codes = {
            str(row.get("code")) for row in universe
            if row.get("code") and not row.get("listing_status") == "pending"
        }
        manifest = M.dfc.get_kline_manifest()
        target_count = sum(
            1 for code in required_codes
            if str((manifest.get(code) or {}).get("last_date") or "")[:10] >= target_date.isoformat()
        )
        threshold = max(4000, int(len(required_codes) * 0.90 + 0.9999)) if required_codes else 4000
        refresh_gate = {
            "target_date": target_date.isoformat(),
            "target_rows": target_count,
            "required_rows": len(required_codes),
            "minimum_rows": threshold,
            "passed": target_count >= threshold,
        }
        if target_count < threshold:
            reason = (
                f"收盘日线尚未完成：{target_date.isoformat()} 仅 {target_count}/"
                f"{len(required_codes)} 只，已保留上一有效结果并等待自动重试"
            )
            failures = [{"strategy": strategy, "error": {"need_init": True, "message": reason}}
                        for strategy in S.STRATEGIES]
            return {
                "status": "partial",
                "date": now.date().isoformat(),
                "target_date": target_date.isoformat(),
                "history_refresh": refresh,
                "factor_refresh": factor_refresh,
                "refresh_gate": refresh_gate,
                "tracked": tracked,
                "saved": saved,
                "failures": failures,
            }
        if factor_refresh is None:
            factor_refresh = P._rebuild_selection_factor_cache(target_date)
    # One stored top-10 list per strategy is enough to form an unbiased daily
    # research sample without turning the job into a second paper-trading engine.
    for strategy in S.STRATEGIES:
        try:
            result = M._select_uncached(strategy=strategy, topn=10)
            if not isinstance(result, dict) or result.get("need_init") or result.get("error"):
                failures.append({"strategy": strategy, "error": result})
                continue
            saved.append(ST.record_run(result, run_date=now.date().isoformat(), source="scheduled"))
        except Exception as exc:
            failures.append({"strategy": strategy, "error": str(exc)})
    # A run is only successful when every configured strategy produced a
    # persisted result.  Returning success after one strategy failed prevents
    # the scheduler from retrying the missing strategy and leaves the daily
    # evidence set incomplete.
    all_strategies_saved = len(saved) == len(S.STRATEGIES) and not failures
    return {
        "status": "ok" if all_strategies_saved else "partial",
        "date": now.date().isoformat(),
        "target_date": target_date.isoformat(),
        "history_refresh": refresh,
        "factor_refresh": factor_refresh,
        "refresh_gate": refresh_gate,
        "tracked": tracked,
        "saved": saved,
        "failures": failures,
        "admission": admission,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", choices=["daily"], default="daily")
    args = parser.parse_args()
    result = run_daily()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] in {"ok", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
