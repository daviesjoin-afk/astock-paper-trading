#!/usr/bin/env python3
"""Split a long history into successive out-of-sample folds.

One held-out window gives one number. One number cannot distinguish "the
strategy works" from "this year happened to suit it". This module builds the
successive (train, test) pairs that turn one number into a distribution.

The rules it enforces, all learned from getting them wrong:

1. **No overlap.** The test window must start strictly after the training
   window ends. A share-by-one-day overlap silently turns an out-of-sample
   result into an in-sample one, and the number looks better, not worse.
2. **No shrinking training set.** By default the training window expands
   (Qlib's `ROLL_EX`): each fold adds history. A fixed-width rolling window is
   available, but it discards old data and is a different claim — so it is
   opt-in, not the default.
3. **Overlapping test windows are declared, not implied.** A `--step-days`
   shorter than `--test-days` reuses test days in later folds. The capability
   stays (a sliding window is sometimes what you want), but every fold now
   carries how many days it reuses from the fold before it, and the summary
   states `test_windows_overlap`. Downstream aggregation must not call such
   folds independent samples — the count overstates the evidence.

Usage:
    python scripts/rolling_windows.py --start 2023-01-03 --end 2026-11-11 \
        --test-days 120 --step-days 120 --min-train-days 250 --out folds.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def parse_day(raw: str) -> tuple[int, int, int]:
    parts = raw.split("-")
    if len(parts) != 3:
        raise ValueError(f"bad date {raw!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def trading_days(db_path: Path, start: str, end: str) -> list[str]:
    """The market's own calendar, not a generated one.

    A fold boundary has to land on a day that actually traded. Building windows
    from a date arithmetic function would put `test_start` on a holiday and
    quietly pull the first real test day into the training window.
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT day FROM bar WHERE adjust='none' AND day BETWEEN ? AND ? ORDER BY day",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def build_folds(
    days: list[str],
    test_days: int,
    step_days: int,
    min_train_days: int,
    expanding: bool = True,
) -> list[dict]:
    """Successive (train, test) folds over an ordered list of trading days.

    `test_days` is measured in trading days, so a fold is a fixed number of
    sessions long regardless of holidays.
    """
    if test_days <= 0 or step_days <= 0 or min_train_days <= 0:
        raise ValueError("test_days, step_days and min_train_days must all be positive")
    folds: list[dict] = []
    index = 0
    fold = 0
    previous_test_end: int | None = None
    while True:
        train_end_index = min_train_days - 1 + index
        test_start_index = train_end_index + 1
        test_end_index = test_start_index + test_days - 1
        if test_end_index >= len(days):
            break
        if expanding:
            train_start_index = 0
        else:
            train_start_index = max(0, train_end_index - min_train_days + 1)
        train_start = days[train_start_index]
        train_end = days[train_end_index]
        test_start = days[test_start_index]
        test_end = days[test_end_index]
        # The invariant, asserted rather than assumed: a later edit that
        # shifts an index by one must fail here, not produce a quiet leak.
        assert train_end < test_start, (
            f"fold {fold} would leak: train ends {train_end}, test starts {test_start}"
        )
        # How many of this fold's test days the previous fold already tested.
        # A `step_days` shorter than `test_days` produces a positive count, and
        # those folds are not independent samples of the configuration.
        reused = 0
        if previous_test_end is not None and test_start_index <= previous_test_end:
            reused = previous_test_end - test_start_index + 1
        folds.append({
            "fold": fold,
            "train": [train_start, train_end],
            "test": [test_start, test_end],
            "train_days": train_end_index - train_start_index + 1,
            "test_days": test_days,
            "overlaps_previous": reused > 0,
            "reused_test_days": reused,
        })
        previous_test_end = test_end_index
        fold += 1
        index += step_days
    return folds


def test_window_overlaps(folds: list[dict]) -> dict:
    """Whether successive test windows share days, and how many.

    This is a fact about the fold set, not a style choice: overlapping test
    windows mean the folds are not independent draws, so a "four of five folds
    positive" claim is counting the same days twice.
    """
    pairs = []
    for earlier, later in zip(folds, folds[1:]):
        reused = later.get("reused_test_days")
        if reused is None:
            reused = 0
            if later["test"][0] <= earlier["test"][1]:
                reused = sum(1 for day in _days_between(later["test"][0], later["test"][1])
                             if day <= earlier["test"][1])
        pairs.append({"earlier_fold": earlier["fold"], "later_fold": later["fold"],
                      "reused_days": reused})
    total = sum(pair["reused_days"] for pair in pairs)
    return {
        "test_windows_overlap": total > 0,
        "reused_test_days": total,
        "pairs": pairs,
    }


def _days_between(start: str, end: str) -> list[str]:
    """Calendar strings between two `YYYY-MM-DD` bounds, inclusive.

    Only used for fold specs that were written before `reused_test_days`
    existed; the day count there is an upper bound on reused sessions, which is
    enough to flag the overlap rather than to quantify it.
    """
    from datetime import date, timedelta

    def parse(day: str) -> date:
        year, month, day_of_month = parse_day(day)
        return date(year, month, day_of_month)

    days = []
    cursor, last = parse(start), parse(end)
    while cursor <= last:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def summarize(folds: list[dict]) -> dict:
    if not folds:
        return {"folds": 0, "note": "history too short for one fold"}
    overlap = test_window_overlaps(folds)
    summary = {
        "folds": len(folds),
        "first_train_start": folds[0]["train"][0],
        "last_test_end": folds[-1]["test"][1],
        "train_days": [fold["train_days"] for fold in folds],
        "test_days": [fold["test_days"] for fold in folds],
        **overlap,
    }
    if overlap["test_windows_overlap"]:
        summary["independent_samples"] = False
        summary["note"] = (
            "Test windows overlap: "
            f"{overlap['reused_test_days']} test days are reused by a later fold. "
            "The folds are a sliding window over the same days, not independent "
            "samples, and a 'N of M folds' count overstates the evidence."
        )
    else:
        summary["independent_samples"] = True
        summary["note"] = (
            "Every test window starts strictly after its training window ends, and "
            "no test day is reused. One fold is one sample; the point of several "
            "is the spread."
        )
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="market database; supplies the real calendar")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--test-days", type=int, default=120,
                        help="trading days held out per fold")
    parser.add_argument("--step-days", type=int, default=120,
                        help="trading days between fold starts")
    parser.add_argument("--min-train-days", type=int, default=250,
                        help="trading days in the first training window")
    parser.add_argument("--rolling", action="store_true",
                        help="fixed-width training window instead of expanding")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.db:
        if not args.start or not args.end:
            raise SystemExit("--start and --end are required with --db")
        if not args.db.exists():
            raise SystemExit(f"database not found: {args.db}")
        days = trading_days(args.db, args.start, args.end)
        source = f"{args.db} ({args.start}..{args.end})"
    else:
        raise SystemExit("--db is required; a fold boundary must land on a real trading day")

    if not days:
        raise SystemExit("no trading days in the requested range")

    folds = build_folds(
        days,
        test_days=args.test_days,
        step_days=args.step_days,
        min_train_days=args.min_train_days,
        expanding=not args.rolling,
    )
    report = {
        "source": source,
        "mode": "rolling" if args.rolling else "expanding",
        "calendar_days": len(days),
        "test_days_requested": args.test_days,
        "step_days_requested": args.step_days,
        "folds": folds,
        "summary": summarize(folds),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["summary"], ensure_ascii=False))
    for fold in folds:
        note = (f" [reuses {fold['reused_test_days']}d from fold {fold['fold'] - 1}]"
                if fold["overlaps_previous"] else "")
        print(f"  fold {fold['fold']}: train {fold['train'][0]}..{fold['train'][1]} "
              f"({fold['train_days']}d) -> test {fold['test'][0]}..{fold['test'][1]}{note}")
    if report["summary"]["test_windows_overlap"]:
        print("WARNING: test windows overlap; these are not independent samples.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
