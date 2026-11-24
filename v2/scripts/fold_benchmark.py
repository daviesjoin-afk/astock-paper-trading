#!/usr/bin/env python3
"""Compare each fold's return against the market it traded in.

Absolute return is the wrong yardstick and it misled us once already:

* A fold that lost 1.86% while the market lost 20.36% was recorded as "negative"
  and read as a failure, when it was a 18.5-point outperformance.
* A fold that gained 5.36% while the market gained 16.55% was recorded as
  "positive" and read as fine, when it gave up 11 points.

Without this comparison the folds look randomly mixed. With it, the structure is
obvious: this configuration is defensive — it earns excess when the market
falls and gives it back when the market rises.

The benchmark here is the **median return of every symbol that traded on both
ends of the window**. Median, not mean: the mean is dragged by a handful of
doublers, and a benchmark nobody can actually hold is not a benchmark.

"The ends of the window" means the market's first and last trading day, not
each symbol's own first and last row. The first version accepted any symbol
with enough rows, which quietly admitted two kinds of non-comparable names:

* a listing that started mid-window had no return over the window at all,
  and its partial move was counted as a full-window one;
* a name halted for the whole period has rows (reference closes, `tradable=0`)
  but never traded, so it was priced as a flat zero return.

Both are now excluded, and the counts are reported with the reason, so a thin
benchmark is visible rather than inferred.

Usage:
    python scripts/fold_benchmark.py --folds folds.json --db market.sqlite3 \
        --results fold-result.json --out benchmark.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sqlite3
import statistics


def market_bounds(conn: sqlite3.Connection, start: str, end: str) -> tuple[str, str] | None:
    """First and last market trading day in the window.

    The union of days any symbol traded on — the same convention the Rust store
    and the scans use for a market calendar in this database.
    """
    row = conn.execute(
        "SELECT MIN(day), MAX(day) FROM bar "
        "WHERE adjust='none' AND day BETWEEN ? AND ? AND close_cents > 0",
        (start, end),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return row[0], row[1]


def market_median_bps(db: Path, start: str, end: str, min_days: int = 10) -> dict:
    """Median window return across symbols that traded at both ends.

    A symbol must have a *tradable* bar on the window's first and last market
    day. `min_days` remains a floor inside the window so a two-day listing
    cannot contribute either. The excluded names and why are counted, because a
    benchmark built from 40 of 5 000 symbols is not the market.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        bounds = market_bounds(conn, start, end)
        if bounds is None:
            return {"symbols": 0, "median_bps": None, "mean_bps": None,
                    "first_day": None, "last_day": None,
                    "excluded": {}, "note": "no market day in the window"}
        first_day, last_day = bounds
        rows = conn.execute(
            "SELECT symbol, day, close_cents, tradable FROM bar "
            "WHERE adjust='none' AND day BETWEEN ? AND ? AND close_cents > 0",
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    series: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
    for symbol, day, close, tradable in rows:
        series[symbol][day] = (close, 1 if tradable is None else int(tradable))

    excluded = {"missing_first_day": 0, "missing_last_day": 0,
                "untradable_at_first_day": 0, "untradable_at_last_day": 0,
                "too_few_observations": 0}
    returns = []
    for points in series.values():
        size = len(points)
        first = points.get(first_day)
        last = points.get(last_day)
        if size < min_days:
            excluded["too_few_observations"] += 1
            continue
        if first is None:
            # Listed after the window opened: no window return exists.
            excluded["missing_first_day"] += 1
            continue
        if last is None:
            excluded["missing_last_day"] += 1
            continue
        if first[1] != 1:
            # A halt on the first day: its "close" is a reference price, not a
            # trade, so a return measured from it is not tradable.
            excluded["untradable_at_first_day"] += 1
            continue
        if last[1] != 1:
            excluded["untradable_at_last_day"] += 1
            continue
        base = first[0]
        if base <= 0:
            excluded["untradable_at_first_day"] += 1
            continue
        returns.append((last[0] - base) * 10_000 // base)

    if not returns:
        return {"symbols": 0, "median_bps": None, "mean_bps": None,
                "first_day": first_day, "last_day": last_day,
                "excluded": excluded,
                "note": "nobody traded at both ends of the window"}
    return {
        "symbols": len(returns),
        "median_bps": int(statistics.median(returns)),
        "mean_bps": round(statistics.fmean(returns)),
        "first_day": first_day,
        "last_day": last_day,
        "excluded": excluded,
        # The median is the benchmark; the mean is reported so a reader can see
        # how much the tail moves it.
        "note": "median is the benchmark; mean is shown to expose tail weight",
    }


def classify(market_bps: int | None, excess_bps: int | None) -> str:
    if market_bps is None or excess_bps is None:
        return "unmeasured"
    if market_bps < 0 and excess_bps > 0:
        return "defensive_win"
    if market_bps < 0 and excess_bps <= 0:
        return "defensive_loss"
    if market_bps > 0 and excess_bps > 0:
        return "offensive_win"
    return "offensive_loss"


def summarise(rows: list[dict]) -> dict:
    measured = [row for row in rows if row["excess_bps"] is not None]
    if not measured:
        return {"folds": 0, "note": "no fold had both a return and a benchmark"}
    down = [row for row in measured if row["market_bps"] < 0]
    up = [row for row in measured if row["market_bps"] > 0]
    summary = {
        "folds_measured": len(measured),
        "down_folds": len(down),
        "up_folds": len(up),
        "mean_excess_bps": round(statistics.fmean([row["excess_bps"] for row in measured])),
        "labels": {row["fold"]: row["label"] for row in measured},
    }
    if down:
        summary["mean_excess_when_down_bps"] = round(
            statistics.fmean([row["excess_bps"] for row in down]))
    if up:
        summary["mean_excess_when_up_bps"] = round(
            statistics.fmean([row["excess_bps"] for row in up]))
    # The shape, stated as a sentence so it survives being quoted out of context.
    if down and up:
        if summary["mean_excess_when_down_bps"] > 0 > summary["mean_excess_when_up_bps"]:
            summary["shape"] = "defensive"
            summary["why"] = (
                "Earns excess when the market falls, gives it back when it rises. "
                "The absolute return is therefore not the thing to judge it by."
            )
        elif summary["mean_excess_when_down_bps"] < 0 < summary["mean_excess_when_up_bps"]:
            summary["shape"] = "offensive"
            summary["why"] = "Earns excess only when the market rises."
        else:
            summary["shape"] = "inconsistent"
            summary["why"] = "Excess has no consistent sign across market states."
    else:
        summary["shape"] = "one_sided"
        summary["why"] = "All folds fell on the same side of the market; no comparison possible."
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folds", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path,
                        help="fold result json (has per-fold return_bps)")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    for path in (args.folds, args.db, args.results):
        if not path.exists():
            raise SystemExit(f"not found: {path}")

    spec = json.loads(args.folds.read_text(encoding="utf-8"))
    results = json.loads(args.results.read_text(encoding="utf-8"))
    returns = {row["fold"]: row for row in results.get("folds") or results.get("results") or []}

    rows = []
    for fold in spec["folds"]:
        number = fold["fold"]
        result = returns.get(number)
        # Two shapes exist in practice: a full `run_folds` report (which carries
        # `status`) and a trimmed archive (which carries only the numbers). A
        # missing status means "assume it ran"; a missing return means it did
        # not, and that is checked separately.
        ran = result is not None and result.get("status", "ok") == "ok"
        if not ran or result.get("return_bps") is None:
            rows.append({"fold": number, "test": fold["test"], "status": "no_return",
                         "market_bps": None, "strat_bps": None, "excess_bps": None,
                         "label": "unmeasured"})
            continue
        market = market_median_bps(args.db, fold["test"][0], fold["test"][1])
        strat = result["return_bps"]
        excess = None if market["median_bps"] is None else strat - market["median_bps"]
        rows.append({
            "fold": number,
            "test": fold["test"],
            "status": "ok",
            "market_bps": market["median_bps"],
            "market_symbols": market["symbols"],
            "market_first_day": market.get("first_day"),
            "market_last_day": market.get("last_day"),
            # Who was left out of the benchmark, so a thin median is visible.
            "market_excluded": market.get("excluded"),
            "strat_bps": strat,
            "excess_bps": excess,
            "label": classify(market["median_bps"], excess),
        })

    report = {
        "folds_file": str(args.folds),
        "database": str(args.db),
        "folds": rows,
        "summary": summarise(rows),
        "caveats": [
            "The benchmark is the median symbol, not a tradable index; it has no fees or turnover.",
            "A median is not what anyone can hold — it is a yardstick for direction, not a rival strategy.",
            "Excess against a falling market is defence, not skill at picking winners.",
            "Only symbols with a tradable bar on both the window's first and last market "
            "day are included; listings, delistings and names halted at either end are "
            "excluded and counted in market_excluded.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in rows:
        if row["status"] != "ok":
            print(f"  fold {row['fold']}: no return")
            continue
        print(f"  fold {row['fold']}: market {row['market_bps']:+6d} | "
              f"strategy {row['strat_bps']:+6d} | excess {row['excess_bps']:+6d} "
              f"({row['label']})")
    print(json.dumps({k: v for k, v in report["summary"].items()
                      if k in ("shape", "why", "mean_excess_when_down_bps",
                               "mean_excess_when_up_bps")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
