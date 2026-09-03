# -*- coding: utf-8 -*-
"""供定时任务调用的单次模拟盘任务。"""
import argparse
import json
import time


_SUCCESS_STATUSES = frozenset({"completed", "already_done", "skipped", "ok", "up_to_date"})


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
    return 0 if str(result.get("status") or "").strip().lower() in _SUCCESS_STATUSES else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["auction", "open", "risk", "close", "weekly-review", "intraday", "fast-entry"])
    args = parser.parse_args()

    # 重试导入和执行（应对数据库锁）
    for attempt in range(5):
        try:
            import paper_trading as paper
            result = (
                paper.monitor_fast_entries()
                if args.slot == "fast-entry"
                else paper.run_slot(args.slot)
            )
            print(json.dumps(result, ensure_ascii=False))
            return _result_exit_code(result)
        except Exception as e:
            if "database is locked" in str(e) and attempt < 4:
                time.sleep(3)
                continue
            print(json.dumps({"error": str(e)[:500], "slot": args.slot}, ensure_ascii=False))
            return 1

if __name__ == "__main__":
    raise SystemExit(main())
