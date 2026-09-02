# -*- coding: utf-8 -*-
"""One-shot candidate-news and major-event collection runner."""
from __future__ import annotations

import argparse
import json

import news_learning


_SUCCESS_STATUSES = frozenset({"completed", "already_done", "skipped", "ok", "up_to_date"})


def _result_exit_code(status):
    """Return a non-zero code for failed/partial scheduled news cycles."""
    return 0 if str(status or "").strip().lower() in _SUCCESS_STATUSES else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", choices=("premarket", "noon", "after-close", "manual"), default="manual")
    args = parser.parse_args()
    result = news_learning.run_cycle(trigger=f"scheduled-{args.slot}")
    latest = (result.get("runs") or [{}])[0]
    print(json.dumps({
        "status": latest.get("status", "completed"),
        "slot": args.slot,
        "pool_size": (result.get("pool") or {}).get("size", 0),
        "candidate_events": (result.get("totals") or {}).get("events", 0),
        "major_events": (result.get("totals") or {}).get("major_events", 0),
        "mode": result.get("mode"),
    }, ensure_ascii=False))
    return _result_exit_code(latest.get("status", "completed"))


if __name__ == "__main__":
    raise SystemExit(main())
