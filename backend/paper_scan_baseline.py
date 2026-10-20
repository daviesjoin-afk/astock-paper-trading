"""Summarize finished natural scan logs, without importing the trading engine.

Usage: python backend/paper_scan_baseline.py reports/scheduler.log --date 2026-10-20
Old logs without version/time/source metadata remain explicitly unknown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
import statistics

from paper_schedule_contract import INTRADAY_CRON_MINUTES


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _stats(values):
    values = sorted(value for value in values if value is not None)
    return {"samples": len(values),
            "median_seconds": round(statistics.median(values), 3) if values else None,
            "p95_seconds": round(values[math.ceil(len(values) * .95) - 1], 3) if values else None,
            "max_seconds": round(values[-1], 3) if values else None}


def expected_minutes():
    """Use the deployed cron contract, including the one-shot 09:30 entry."""
    result = {"09:30"}
    for hour, expression in INTRADAY_CRON_MINUTES.items():
        bounds, _, step = expression.partition("/")
        start, end = (0, 59) if bounds == "*" else tuple(map(int, bounds.split("-")))
        result.update(f"{hour:02}:{minute:02}" for minute in range(start, end + 1, int(step or 1)))
    return result


def _coverage(rows, day):
    minutes, timestamped = set(), 0
    for timing, _ in rows:
        try:
            instant = dt.datetime.fromisoformat(timing["started_at"])
            if instant.utcoffset() is not None:
                instant = instant.astimezone(dt.timezone(dt.timedelta(hours=8)))
                if instant.date().isoformat() == day:
                    minutes.add(instant.strftime("%H:%M"))
                    timestamped += 1
        except (KeyError, TypeError, ValueError):
            pass
    missing = sorted(expected_minutes() - minutes) if timestamped else None
    return {"timestamped_attempts": timestamped, "untimestamped_attempts": len(rows) - timestamped,
            "coverage_status": ("unknown" if not timestamped else "complete" if not missing
                                else "partial_timestamps" if timestamped < len(rows) else "partial"),
            "observed_start_minutes": sorted(minutes), "missing_scheduled_minutes": missing,
            "covers_scheduled_minutes": missing == []}


def _counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def summarize(lines, *, market_date=None, excluded_attempts=()):
    """Separate complete/failed/degraded groups; never add overlapping phases."""
    groups, seen = {}, set()
    counts = {"malformed": 0, "duplicates": 0, "excluded": 0, "unknown_date": 0}
    for line in lines:
        if "paper_slot_timing" not in line:
            continue
        try:
            value = json.loads(line[line.index("{"):])
            if value.get("event") != "paper_slot_timing":
                continue
            timing = value["timing"]
            if timing.get("slot") != "intraday" or timing.get("scope") != "finished":
                continue
            identity = timing.get("identity") or {}
            stages = timing.get("stages") or {}
            if not isinstance(identity, dict) or not isinstance(stages, dict):
                raise ValueError("invalid timing metadata")
            day = str(identity.get("market_date") or "unknown")
            if market_date and day != market_date:
                counts["unknown_date"] += int(day == "unknown")
                continue
            attempt = timing.get("attempt_id")
            if not attempt:
                raise ValueError("missing attempt identity")
            if attempt in seen:
                counts["duplicates"] += 1
                continue
            seen.add(attempt)
            if attempt in excluded_attempts:
                counts["excluded"] += 1
                continue
            elapsed = _finite(timing.get("elapsed_seconds"))
            if elapsed is None:
                raise ValueError("invalid total duration")
            scan_path = ("risk_and_candidate" if "risk_pass" in stages and "candidate_bootstrap" in stages
                         else "risk_only" if "risk_pass" in stages else "no_scan_stages")
            key = (str(timing.get("runtime_commit") or "unknown"), day,
                   str(value.get("status") or "unknown"),
                   str(identity.get("snapshot_source") or "unknown"),
                   identity.get("snapshot_price_only"), identity.get("scan_ready"), scan_path)
            group = groups.setdefault(key, [])
            group.append((timing, elapsed))
        except (ValueError, TypeError, KeyError, AttributeError):
            counts["malformed"] += 1
    result, sessions = [], {}
    for (commit, day, status, source, price_only, ready, scan_path), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        session = sessions.setdefault((commit, day), {"rows": [], "outcomes": {}})
        session["rows"].extend(rows)
        session["outcomes"][status] = session["outcomes"].get(status, 0) + len(rows)
        stages, counters, waits = {}, {}, []
        for timing, _ in rows:
            waits.append(_finite(timing.get("host_paper_lock_wait_seconds")))
            for name, stage in (timing.get("stages") or {}).items():
                if isinstance(stage, dict):
                    stages.setdefault(name, []).append(_finite(stage.get("seconds")))
                    counts_for_stage = counters.setdefault(name, {"calls": [], "errors": []})
                    for field in counts_for_stage:
                        count = _counter(stage.get(field))
                        if count is not None:
                            counts_for_stage[field].append(count)
        coverage = _coverage(rows, day)
        result.append({"runtime_commit": commit, "market_date": day, "status": status,
                       "snapshot_source": source, "price_only": price_only, "scan_ready": ready,
                       "scan_path": scan_path,
                       "total": _stats([elapsed for _, elapsed in rows]),
                       "host_lock_wait": _stats(waits),
                       "stages_inclusive": {name: _stats(values) for name, values in sorted(stages.items())},
                       "stage_counters": {
                           name: {field: {"reported_attempts": len(values),
                                          "total": sum(values) if values else None}
                                  for field, values in fields.items()}
                           for name, fields in sorted(counters.items())},
                       **coverage,
                       "p95_is_provisional": len(rows) < 100 or not coverage["covers_scheduled_minutes"]
                       or commit == "unknown" or source == "unknown"})
    session_coverage = [
        {"runtime_commit": commit, "market_date": day,
         "attempts": len(session["rows"]), "outcomes": dict(sorted(session["outcomes"].items())),
         "finished_outcomes": (finished := session["outcomes"].get("completed", 0)
                               + session["outcomes"].get("failed", 0)),
         "observed_failure_fraction": (round(session["outcomes"].get("failed", 0) / finished, 6)
                                       if finished else None),
         **_coverage(session["rows"], day)}
        for (commit, day), session in sorted(sessions.items())]
    return {"version": 2, "counts": counts, "groups": result, "session_coverage": session_coverage,
            "limitations": ["日志不包含未启动或被外层锁拒绝的完整任务耗时。",
                            "阶段含子阶段及并行工作，不能相加；冷启动需按明确 attempt_id 排除并保留原报告。",
                            "时刻覆盖只反映实际开始时刻；等待锁可能导致偏移，不证明交易日、调度成功或策略通道完整运行。",
                            "整场覆盖合并同版本同日的正常、降级与失败样本；分组耗时仍分开，未知时刻不推定为调度缺失。",
                            "观测失败比例仅为 failed / (completed + failed)，不包含 in_progress 或没有结束日志的调度。",
                            "阶段调用与异常计数只汇总明确上报值；一个阶段调用可能包含多次 HTTP 重试，不等于网络请求数。"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--date", dest="market_date", type=dt.date.fromisoformat)
    parser.add_argument("--exclude-attempt", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with args.log.open(encoding="utf-8", errors="replace") as handle:
        result = summarize(handle, market_date=args.market_date.isoformat() if args.market_date else None,
                           excluded_attempts=args.exclude_attempt)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
