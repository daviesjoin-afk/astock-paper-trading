#!/usr/bin/env python3
"""Read-only weekly review: rolling windows for a candidate against its base.

Prints one line per window and a final verdict. Exit codes:
    0  consistent (candidate may proceed to the governed decision)
    4  unstable (coverage gap, edge too small, or drawdown above the cap)
    2  usage / request error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shadow_candidates as shadow  # noqa: E402


def chunk_days(text: str, window_size: int) -> list[list[str]]:
    """Split an explicit trading-day list into consecutive full windows."""
    if window_size < 1:
        raise ValueError("window_size_must_be_positive")
    days = [day.strip() for day in text.split(",") if day.strip()]
    if len(days) < window_size:
        raise ValueError("not_enough_days_for_one_window")
    windows = [days[index:index + window_size] for index in range(0, len(days) - window_size + 1, window_size)]
    if not windows:
        raise ValueError("no_full_window")
    return windows


def parse_windows(text: str) -> list[list[str]]:
    windows = [
        [day.strip() for day in window.split(",") if day.strip()]
        for window in text.split(";")
        if window.strip()
    ]
    if not windows or any(not window for window in windows):
        raise ValueError("empty_window")
    return windows


def summarize(window: list[str], base: dict, candidate: dict) -> str:
    label = ",".join(window)
    if base.get("status") != "valued" or candidate.get("status") != "valued":
        return f"{label}: 缺少完整覆盖"
    base_perf = base["performance"]
    candidate_perf = candidate["performance"]
    return (
        f"{label}: 基准 {base_perf['return_bps']} bps / 回撤 {base_perf['drawdown_bps']} · "
        f"候选 {candidate_perf['return_bps']} bps / 回撤 {candidate_perf['drawdown_bps']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    parser.add_argument("--base-cycle", required=True)
    parser.add_argument("--candidate-cycle", required=True)
    parser.add_argument("--windows", help="e.g. 2026-10-30,2026-10-31;2026-11-02")
    parser.add_argument("--days", help="comma-separated days to split into windows")
    parser.add_argument("--window-size", type=int, default=5)
    parser.add_argument("--margin-bps", type=int, default=50)
    parser.add_argument("--max-drawdown-bps", type=int, default=1_500)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.windows:
            windows = parse_windows(args.windows)
        elif args.days:
            windows = chunk_days(args.days, args.window_size)
        else:
            raise ValueError("provide --windows or --days")
        base = shadow.segments(args.api, args.base_cycle, windows)
        candidate = shadow.segments(args.api, args.candidate_cycle, windows)
        consistency = shadow.rolling_consistency(
            base, candidate,
            margin_bps=args.margin_bps,
            max_drawdown_bps=args.max_drawdown_bps,
        )
    except (AssertionError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(
            {"windows": windows, "base": base, "candidate": candidate, "consistency": consistency},
            ensure_ascii=False, indent=2,
        ))
    else:
        for index, window in enumerate(windows):
            print(summarize(window, base["segments"][index], candidate["segments"][index]))
        print(f"结论: {consistency['status']}（{consistency['windows']} 个窗口）")
        for reason in consistency["reasons"]:
            print(f"- {reason}")
    return 0 if consistency["status"] == "consistent" else 4


if __name__ == "__main__":
    raise SystemExit(main())
