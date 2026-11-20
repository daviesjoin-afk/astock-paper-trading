#!/usr/bin/env python3
"""Scan candidate factors for a usable, stable sign before any of them is wired in.

The strategy's entry signal is a momentum breakout, and momentum measured
RankIC -1347 bps on this history: the book has been buying what goes down next.
Replacing it needs a factor with a *positive* IC that survives more than one
period, so this scans a fixed library over many periods and reports the
distribution rather than the best day.

What it refuses to do:

* Pick the best factor by mean IC alone. A factor positive on three periods and
  hugely negative on two has a fine mean and no usable sign.
* Report a scan that dropped most of the market. Coverage is asserted per
  period via the probe's own `max_loss` guard.
* Hide a factor that could not be evaluated. It is listed with the reason.

Usage:
    python scripts/factor_scan.py --db market.sqlite3 --probe probe.exe \
        --periods 2025-02-14,2025-04-18,2025-06-20,2025-08-15,2025-10-17 \
        --out scan.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys

# The library under test. Each entry is a named expression the Rust probe can
# evaluate; the names are what a report and a governance row will refer to.
CANDIDATES: dict[str, dict] = {
    # The current signal's shape, kept as the benchmark it has to beat.
    "momentum_20": {"momentum": {"window": 20}},
    "momentum_60": {"momentum": {"window": 60}},
    # The reversal family: the same measure with the sign flipped, which is what
    # a negative IC says to look at -- as a hypothesis to test on all periods,
    # not as a conclusion drawn from one.
    "reversal_5": {"sub": [{"const": {"value": 0}}, {"momentum": {"window": 5}}]},
    "reversal_20": {"sub": [{"const": {"value": 0}}, {"momentum": {"window": 20}}]},
    # Low volatility, in two horizons. A different claim from reversal, and it
    # was the one factor with a positive mean in the earlier evaluation.
    "low_vol_20": {"sub": [{"const": {"value": 0}}, {"std_dev": {"window": 20}}]},
    "low_vol_60": {"sub": [{"const": {"value": 0}}, {"std_dev": {"window": 60}}]},
    # Distance from the recent high: a cheap way to ask "has it already run".
    "drawdown_from_high_20": {
        "sub": [{"close": {"offset": 0}}, {"max": {"window": 20}}]
    },
    "drawdown_from_high_60": {
        "sub": [{"close": {"offset": 0}}, {"max": {"window": 60}}]
    },
    # Position inside the recent range: near the low versus near the high.
    "range_position_20": {
        "div": [
            {"sub": [{"close": {"offset": 0}}, {"min": {"window": 20}}]},
            {"sub": [{"max": {"window": 20}}, {"min": {"window": 20}}]},
        ]
    },
}


def run_probe(probe: Path, db: Path, day: str, expr: dict, timeout: float) -> dict:
    result = subprocess.run(
        [str(probe), str(db), day, json.dumps(expr)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()[:200]}
    try:
        return {"ok": True, "data": json.loads(result.stdout)}
    except json.JSONDecodeError:
        return {"ok": False, "error": "probe returned non-JSON"}


def score(name: str, expr: dict, probe: Path, db: Path, days: list[str],
          timeout: float) -> dict:
    rank_ics: list[int] = []
    unusable: list[dict] = []
    for day in days:
        outcome = run_probe(probe, db, day, expr, timeout)
        if not outcome["ok"]:
            unusable.append({"day": day, "reason": outcome["error"]})
            continue
        data = outcome["data"]
        coverage = data.get("coverage") or {}
        if coverage and coverage.get("usable") is False:
            unusable.append({"day": day, "reason": "coverage_below_max_loss",
                             "coverage": coverage})
            continue
        stats = data.get("stats") or {}
        if not stats.get("samples"):
            unusable.append({"day": day, "reason": "no_samples"})
            continue
        rank_ics.append(stats["rank_ic_bps"])

    if not rank_ics:
        return {"name": name, "expr": expr, "usable": False,
                "reason": "no period produced a measurement",
                "unusable": unusable}

    positive = sum(1 for value in rank_ics if value > 0)
    mean = round(statistics.fmean(rank_ics))
    ir = None
    if len(rank_ics) >= 2:
        stdev = statistics.pstdev(rank_ics)
        if stdev > 0:
            ir = round(mean / stdev)
    flips = sum(
        1 for left, right in zip(rank_ics, rank_ics[1:])
        if (left >= 0) != (right >= 0)
    )
    return {
        "name": name,
        "expr": expr,
        "usable": True,
        "periods": len(rank_ics),
        "rank_ics": rank_ics,
        "mean_rank_ic_bps": mean,
        "median_rank_ic_bps": round(statistics.median(rank_ics)),
        "ir_bps": ir,
        "sign_flips": flips,
        "positive_periods": positive,
        "all_same_sign": positive in (0, len(rank_ics)),
        "unusable": unusable,
    }


def rank_candidates(rows: list[dict]) -> list[dict]:
    """Order by sign consistency first, then magnitude.

    Consistency first because a factor whose sign changes is not a weaker
    version of a good factor -- it is a different, unusable object. The
    governance gate already refuses sign-flippers, so promoting one here would
    only defer the refusal.
    """
    usable = [row for row in rows if row.get("usable")]
    usable.sort(key=lambda row: (
        not row["all_same_sign"],          # consistent sign first
        -row["positive_periods"],          # then more positive periods
        -abs(row["mean_rank_ic_bps"]),     # then magnitude
    ))
    for position, row in enumerate(usable, 1):
        row["rank"] = position
    return usable


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--periods", required=True,
                        help="comma separated decision days, ideally spread out")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--only", default="", help="comma separated candidate names")
    args = parser.parse_args(argv)

    for path in (args.db, args.probe):
        if not path.exists():
            raise SystemExit(f"not found: {path}")
    days = [day.strip() for day in args.periods.split(",") if day.strip()]
    if len(days) < 3:
        raise SystemExit("at least three periods are required before a sign means anything")

    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    library = {name: expr for name, expr in CANDIDATES.items()
               if not wanted or name in wanted}

    rows = []
    for name, expr in library.items():
        print(f"scanning {name}", file=sys.stderr)
        rows.append(score(name, expr, args.probe, args.db, days, args.timeout))

    ranked = rank_candidates(rows)
    report = {
        "periods": days,
        "candidates_tested": len(rows),
        "candidates_usable": len(ranked),
        "ranking": ranked,
        "unusable": [row for row in rows if not row.get("usable")],
        "decision_rule": (
            "A replacement must have the same sign on every period and mean "
            "RankIC above the governance floor. A better mean with a sign flip "
            "is not a candidate; it is the object the gate refuses."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in ranked:
        print(f"  {row['rank']}. {row['name']:24s} mean {row['mean_rank_ic_bps']:+6d} "
              f"median {row['median_rank_ic_bps']:+6d} IR {row['ir_bps']} "
              f"pos {row['positive_periods']}/{row['periods']} "
              f"flips {row['sign_flips']} ics={row['rank_ics']}")
    for row in rows:
        if not row.get("usable"):
            print(f"  - {row['name']}: unusable ({row.get('reason')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
