# -*- coding: utf-8 -*-
"""供定时任务调用的单次模拟盘任务。"""
import argparse
import datetime as dt
import json
import time

from paper_schedule_contract import is_hot_path, window_run_key
import paper_timing as PTM


_SUCCESS_STATUSES = frozenset({
    "completed", "already_done", "skipped", "ok", "up_to_date",
    # PR-11 B：风险 lane 与 intraday 兜底共用 durable scan claim。同一分钟被
    # 另一个入口先认领时，本轮返回 already_scanned —— 这是幂等成功，不是失败，
    # 退出码必须是 0，否则每一跳"被对端先跑"的调度都会在日志里冒充故障。
    "already_scanned",
})


def _result_exit_code(result):
    """Map a slot result to a scheduler-visible exit code.

    A runner is a one-shot cron boundary.  Returning after printing a
    ``failed``/``blocked`` result makes the scheduler believe the slot
    completed and prevents its retry path from running.  Only terminal
    non-error states are successful; partial/deferred/in-progress states must
    remain non-zero so the owning scheduler can retry them.
    """
    if not isinstance(result, dict):
        return 1
    if str(result.get("status") or "").strip().lower() not in _SUCCESS_STATUSES:
        return 1
    return 0


def main():
    from paper_schedule_contract import RUNNER_SLOTS

    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=list(RUNNER_SLOTS))
    args = parser.parse_args()

    with PTM.capture(args.slot):
        return _run(args.slot)


def _run_slot_with_risk_retry(paper, slot):
    """The cron-launched process retries risk deferrals twice, in this window.

    run_slot has already finalized the failed attempt and released its lease
    and writer transaction before we sleep. Its durable retry budget and all
    risk/cycle/fill fences still apply; no force flag is passed.
    """
    if slot != "execution-resume":
        return paper.run_slot(slot)
    window = window_run_key(slot, dt.datetime.now())
    result = paper.run_slot(slot)
    for _retry in range(2):
        if (result.get("status") != "failed"
                or result.get("retry_reason") != "risk_in_progress"
                or window_run_key(slot, dt.datetime.now()) != window):
            break
        with PTM.phase("execution_risk_retry_backoff"):
            time.sleep(10)
        if window_run_key(slot, dt.datetime.now()) != window:
            break
        result = paper.run_slot(slot)
    return result


def _run(slot):
    if slot == "auction-track":
        import auction_tracking
        result = auction_tracking.run()
        PTM.outcome(result.get("status", "unknown"))
        print(json.dumps(result, ensure_ascii=False))
        return _result_exit_code(result)
    # 重试导入和执行（应对数据库锁）
    from paper_storage import is_sqlite_busy_error, sqlite_busy_backoff
    is_hot = is_hot_path(slot)
    max_attempts = 5 if is_hot else 4
    for attempt in range(max_attempts):
        try:
            with PTM.phase("engine_import"):
                import paper_trading as paper
            result = (
                paper.monitor_fast_entries()
                if slot == "fast-entry"
                else _run_slot_with_risk_retry(paper, slot)
            )
            PTM.outcome(result.get("status", "unknown"))
            result = {**result, "timing": PTM.snapshot("runner_result")}
            print(json.dumps(result, ensure_ascii=False))
            return _result_exit_code(result)
        except Exception as e:
            if is_sqlite_busy_error(e) and attempt < max_attempts - 1:
                delay = sqlite_busy_backoff(attempt, hot_path=is_hot)
                with PTM.phase("sqlite_retry_backoff"):
                    time.sleep(delay)
                continue
            PTM.outcome("failed")
            print(json.dumps({"error": str(e)[:500], "slot": slot,
                              "timing": PTM.snapshot("runner_error")}, ensure_ascii=False))
            return 1

if __name__ == "__main__":
    raise SystemExit(main())
