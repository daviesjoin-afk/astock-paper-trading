# -*- coding: utf-8 -*-
"""Single-run entry point for midday observation and post-close evolution."""
import argparse
import datetime as dt
import json
import sqlite3
import time

import adaptive_engine as adaptive
from resource_guard import heavy_job_lease


MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (15, 45)


def _scheduled_close_due(trigger):
    """Apply the operator-configured minimum interval to scheduled close runs.

    Manual runs remain available for diagnosis.  A skipped scheduled run is a
    successful no-op so cron does not retry it as a failure.
    """
    if not str(trigger or "").startswith("scheduled"):
        return True, None
    interval = 24
    try:
        with sqlite3.connect(adaptive.PAPER_DB_PATH, timeout=5) as conn:
            row = conn.execute(
                "SELECT value FROM paper_runtime_settings WHERE key='evolution_interval_hours'"
            ).fetchone()
            if row:
                interval = max(1, min(168, int(json.loads(row[0]))))
    except Exception:
        pass
    try:
        with adaptive._connect() as conn:
            row = conn.execute(
                "SELECT finished_at FROM adaptive_runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception:
        row = None
    if not row or not row[0]:
        return True, None
    try:
        finished = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=dt.timezone.utc)
        now = dt.datetime.now(dt.timezone.utc)
        elapsed = (now - finished.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return True, None
    if elapsed + 1e-9 < interval:
        return False, {"interval_hours": interval, "elapsed_hours": round(elapsed, 2), "last_finished_at": row[0]}
    return True, None


def _latest_run_status(result):
    """读取本次自进化写入的最新运行状态，识别被内部捕获的失败。"""
    if not isinstance(result, dict):
        return "unknown"
    if result.get("status") == "busy":
        return "busy"
    rows = result.get("runs") or []
    if rows and isinstance(rows[0], dict):
        return str(rows[0].get("status") or "unknown")
    return str(result.get("status") or "unknown")


def _run_with_retry(dispatch):
    """失败自动重试，避免一次行情/AI瞬断让整次学习永久丢失。

    每个学习阶段本身使用事务；异常会回滚，重试不会重复提交半成品。
    最终失败仍抛出，让 cron/监控明确看到失败，而不是静默结束。
    """
    last_error = None
    last_result = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            last_result = dispatch()
            status = _latest_run_status(last_result)
            if status not in {"failed", "intraday_failed", "advisor_failed", "busy"}:
                return last_result, attempt
            last_error = RuntimeError(f"自进化任务状态异常：{status}")
        except Exception as exc:
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            delay = RETRY_DELAYS_SECONDS[attempt - 1]
            print(json.dumps({"retry": attempt, "next_in_seconds": delay,
                              "reason": f"{type(last_error).__name__}: {last_error}"}, ensure_ascii=False), flush=True)
            time.sleep(delay)
    if last_error:
        raise last_error
    return last_result, MAX_ATTEMPTS


def _warn_stale_heartbeat():
    """A3：启动期检查上一轮 close 是否被 SIGKILL 杀死（心跳文件未清除）。"""
    try:
        import os
        from datetime import datetime, timezone
        import adaptive_engine as _ae
        hb = getattr(_ae, "LEARNING_HEARTBEAT", None)
        if not hb or not os.path.exists(hb):
            return
        try:
            with open(hb, "r", encoding="utf-8") as _f:
                started = _f.read().strip()
            try:
                # 心跳由 adaptive_common._now() 写入，是带 +08:00 的 aware
                # 时间串。此前用 naive datetime.now() 相减必然抛 TypeError，
                # age_hours 恒为 99.0，无法区分“刚崩溃”与“上周崩溃”。
                ts = datetime.fromisoformat(started)
                if ts.tzinfo is None:
                    ts = ts.astimezone()
                age_h = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
            except Exception:
                age_h = 99.0
            print(json.dumps({"alarm": "stale_learning_heartbeat",
                              "started_at": started,
                              "age_hours": round(age_h, 1),
                              "note": "上一轮 close 学习被杀死（OOM/重启）且未正常收尾，请查内核 OOM 与 adaptive_runs.killed"},
                             ensure_ascii=False), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="scheduled-close")
    parser.add_argument(
        "--session", choices=("midday", "midday-advisor", "close"), default="close",
        help="midday 保存盘中观测；midday-advisor 批量运行 DP 审阅；close 运行正式奖励和调参",
    )
    args = parser.parse_args()
    if args.session == "close":
        _warn_stale_heartbeat()
        due, detail = _scheduled_close_due(args.trigger)
        if not due:
            print(json.dumps({"session": args.session, "status": "skipped_due_interval", "detail": detail}, ensure_ascii=False))
            return 0
    if args.session == "midday":
        dispatch = lambda: adaptive.run_midday_observation(trigger=args.trigger)
    elif args.session == "midday-advisor":
        dispatch = lambda: adaptive.run_midday_advisor(trigger=args.trigger)
    else:
        dispatch = lambda: adaptive.run_learning_cycle(trigger=args.trigger)
    # Midday observation is intentionally light.  Model/close learning is a
    # batch workload and must yield to K-line/factor recovery rather than
    # competing for the same cgroup memory.
    if args.session in {"close", "midday-advisor"}:
        with heavy_job_lease(f"adaptive-{args.session}") as admission:
            if not admission.get("allowed"):
                print(json.dumps({"session": args.session, "status": "deferred",
                                  "reason": admission.get("reason"), "admission": admission}, ensure_ascii=False))
                # A deferred close is retryable (for example while history
                # recovery owns the heavy-job lease); report non-zero so the
                # scheduler/monitor does not treat it as a successful cycle.
                return 75
            result, attempts = _run_with_retry(dispatch)
    else:
        result, attempts = _run_with_retry(dispatch)
    summary = {
        "session": args.session,
        "status": result.get("engine", {}).get("stage"),
        "profile_date": (
            (result.get("midday_observation") if args.session == "midday" else result.get("market_profile"))
            or {}
        ).get("profile_date"),
        "regime": (
            (result.get("midday_observation") if args.session == "midday" else result.get("market_profile"))
            or {}
        ).get("regime"),
        "mature_rewards": result.get("engine", {}).get("mature_reward_count"),
        "attempts": attempts,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main() or 0)
