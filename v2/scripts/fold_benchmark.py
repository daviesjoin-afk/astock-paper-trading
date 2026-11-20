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


def market_median_bps(db: Path, start: str, end: str, min_days: int = 10) -> dict:
    """Median window return across symbols present at both ends.

    A symbol needs `min_days` observations inside the window, so a name that
    listed on the last day or halted for the whole period cannot contribute a
    zero-length return.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT symbol, day, close_cents FROM bar "
            "WHERE adjust='none' AND day BETWEEN ? AND ? AND close_cents > 0",
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    series: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for symbol, day, close in rows:
        series[symbol].append((day, close))

    returns = []
    for points in series.values():
        if len(points) < min_days:
            continue
        points.sort()
        first, last = points[0][1], points[-1][1]
        if first <= 0:
            continue
        returns.append((last - first) * 10_000 // first)

    if not returns:
        return {"symbols": 0, "median_bps": None, "mean_bps": None}
    return {
        "symbols": len(returns),
        "median_bps": int(statistics.median(returns)),
        "mean_bps": round(statistics.fmean(returns)),
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
