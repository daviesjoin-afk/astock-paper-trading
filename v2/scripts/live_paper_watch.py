#!/usr/bin/env python3
"""Read-only host probe for the scheduled AStock V2 paper account."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
ROUND_GRACE = timedelta(minutes=7)


def observe(base: str, calendar: dict, now: datetime) -> dict:
    day = now.date().isoformat()
    clock = now.time()
    during_session = time(9, 37) <= clock <= time(11, 30) or time(13, 7) <= clock < time(15)
    after_close = time(15, 7) <= clock <= time(15, 20)
    if now.weekday() >= 5 or not (during_session or after_close):
        return {"status": "skipped", "reason": "outside_check_window"}
    if not calendar["coverage_start"] <= day <= calendar["coverage_end"]:
        return {"status": "alert", "issues": ["calendar_coverage_missing"]}
    if any(item["start"] <= day <= item["end"] for item in calendar["holidays"]):
        return {"status": "skipped", "reason": "market_holiday"}

    opener = build_opener(ProxyHandler({}))

    def get(path):
        with opener.open(base + path, timeout=7) as response:
            return json.load(response)

    issues = []
    if get("/v2/health")["status"] != "ok":
        issues.append("kernel_health")
    rounds = [(datetime.fromisoformat(item["started_at"]).astimezone(SHANGHAI), item)
              for item in get("/v2/live/rounds")]
    rounds = [(stamp, item) for stamp, item in rounds if stamp.date() == now.date() and stamp <= now]
    rounds.sort(key=lambda pair: pair[0])
    report = {"rounds_today": len(rounds),
              "ai_accepted_today": sum(item["ai_ok"] is True for _, item in rounds),
              "simulated_fills_today": sum(item["trade_count"] for _, item in rounds)}
    if rounds:
        stamp, latest = rounds[-1]
        report.update(latest_round=latest["round_id"], latest_quote_count=latest["quote_count"],
                      latest_age_seconds=int((now - stamp).total_seconds()))
        horizon = now if during_session else now.replace(hour=15, minute=0, second=0, microsecond=0)
        if horizon - stamp > ROUND_GRACE:
            issues.append("latest_round_stale")
        if latest["quote_count"] <= 0:
            issues.append("latest_round_without_quotes")
        elif len(rounds) >= 4 and all(latest["quote_count"] * 10 < row["quote_count"] * 9
                                     for _, row in rounds[-4:-1]):
            issues.append("quote_coverage_drop")
        if len(rounds) >= 3 and all(item["ai_ok"] is False for _, item in rounds[-3:]):
            issues.append("ai_unavailable_three_rounds")
    else:
        issues.append("no_rounds_today")

    if after_close:
        proposals = [row for row in get("/v2/live/evolution") if row["day"] == day]
        report["close_proposals"] = len(proposals)
        if len(proposals) != 1:
            issues.append("close_proposal_count")
        elif proposals[0]["accepted"] is not False:
            issues.append("close_proposal_active")
        elif proposals[0]["before_metrics"].get("round_count") != len(rounds):
            issues.append("close_proposal_round_count")
    return {**report, "status": "alert" if issues else "ok", "issues": issues}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:18765")
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--at", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.at is not None and args.at.tzinfo is None:
        parser.error("--at requires a UTC offset")
    now = args.at.astimezone(SHANGHAI) if args.at else datetime.now(SHANGHAI)
    try:
        calendar = json.loads(args.calendar.read_text(encoding="utf-8"))
        result = observe(args.api.rstrip("/"), calendar, now)
    except (OSError, URLError, ValueError, KeyError, TypeError) as error:
        result = {"status": "alert", "issues": [type(error).__name__]}
    print(json.dumps({"event": "paper_watch", "at": now.isoformat(timespec="seconds"), **result}))
    return int(result["status"] == "alert")


if __name__ == "__main__":
    raise SystemExit(main())
