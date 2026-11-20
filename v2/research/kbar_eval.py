"""Evaluate the nine Alpha158 KBAR factors against real forward returns.

This is an **independent** implementation. It does not call the Rust
`KBars`/`factor_stats` code: the point of a second implementation is to be able
to disagree with the first. If both were the same code there would be nothing
to check.

The two rules that make the numbers mean anything:

1. A factor is computed from a session's own open/high/low/close. The forward
   return it is graded against starts at the *next* session, so the label can
   never be derived from a price the factor already saw.
2. A symbol is only graded on a day it actually traded, and its forward return
   must exist. Missing labels are dropped, never filled with zero -- a zero
   forward return is a claim that the price did not move.

Output is per-factor, per-day RankIC, then aggregated to mean IC / IR, plus the
top-minus-bottom quintile spread. IR here is mean(IC)/std(IC) over days, which
is the standard definition and is what the project's ≥300 bps promotion gate
refers to.

Usage:
    python research/kbar_eval.py --db work/hist.sqlite3 --horizon 5
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict

KBAR_FIELDS = (
    "kmid", "klen", "kmid2", "kup", "kup2",
    "klow", "klow2", "ksft", "ksft2",
)


def scale_bps(numerator: int, denominator: int) -> int | None:
    """numerator / denominator * 10_000, rounded half away from zero."""
    if denominator <= 0:
        return None
    scaled = numerator * 10_000
    if scaled >= 0:
        return (scaled + denominator // 2) // denominator
    return -((-scaled + denominator // 2) // denominator)


def kbar_fields(open_c: int, high: int, low: int, close: int) -> dict[str, int | None] | None:
    """The nine factors for one session, or None when it carries no shape."""
    if open_c <= 0 or high <= 0 or low <= 0:
        return None
    if high < low:
        return None
    rng = high - low
    body = close - open_c
    upper = high - max(open_c, close)
    lower = min(open_c, close) - low
    half = 2 * close - high - low
    return {
        "kmid": scale_bps(body, open_c),
        "klen": scale_bps(rng, open_c),
        "kmid2": scale_bps(body, rng),
        "kup": scale_bps(upper, open_c),
        "kup2": scale_bps(upper, rng),
        "klow": scale_bps(lower, open_c),
        "klow2": scale_bps(lower, rng),
        "ksft": scale_bps(half, open_c),
        "ksft2": scale_bps(half, rng),
    }


def average_ranks(values: list[float]) -> list[float]:
    """1-based ranks, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average = (start + end - 1) / 2.0 + 1.0
        for index in order[start:end]:
            ranks[index] = average
        start = end
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return 0.0
    return num / (dx * dy)


def rank_ic(observations: list[tuple[float, float]]) -> float:
    if len(observations) < 20:
        return 0.0
    factors = average_ranks([o[0] for o in observations])
    forwards = average_ranks([o[1] for o in observations])
    return pearson(factors, forwards)


def quantile_spread(observations: list[tuple[float, float]], buckets: int = 5):
    if len(observations) < buckets * 2:
        return None, None, None
    ordered = sorted(observations, key=lambda o: o[0])
    size = len(ordered) // buckets
    if size == 0:
        return None, None, None
    low = [r for _, r in ordered[:size]]
    high = [r for _, r in ordered[-size:]]
    low_mean = int(sum(low) / len(low))
    high_mean = int(sum(high) / len(high))
    return low_mean, high_mean, high_mean - low_mean


def load_series(db: str):
    """symbol -> ordered [(day, o, h, l, c, volume)] of tradable sessions."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    rows = con.execute("""
        select symbol, day, open_cents, high_cents, low_cents, close_cents, volume_shares
        from bar
        where adjust='none' and tradable=1 and close_cents>0
        order by symbol, day
    """).fetchall()
    con.close()
    series: dict[str, list] = defaultdict(list)
    for symbol, day, o, h, l, c, v in rows:
        series[symbol].append((day, o, h, l, c, v))
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--horizon", type=int, default=5,
                        help="label window in trading days (default 5, matching the aggressive slot)")
    parser.add_argument("--min-symbols", type=int, default=200,
                        help="skip a day whose cross-section is thinner than this")
    parser.add_argument("--json", help="write the full result here")
    parser.add_argument("--max-abs-forward", type=int, default=0,
                        help="drop labels whose |forward return| exceeds this many bps "
                             "(a 10%%-limit board cannot move more without a corporate action)")
    args = parser.parse_args(argv)

    series = load_series(args.db)
    if not series:
        print("no rows", file=sys.stderr)
        return 2

    # Per day: for each factor, the (factor, forward) pairs across symbols.
    per_day: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: {name: [] for name in KBAR_FIELDS}
    )
    graded = 0
    skipped_label = 0
    skipped_shape = 0

    for symbol, bars in series.items():
        if len(bars) < args.horizon + 2:
            continue
        for index in range(len(bars) - args.horizon):
            day, o, h, l, c, _v = bars[index]
            _next_day, _o2, _h2, _l2, future_close, _v2 = bars[index + args.horizon]
            if c <= 0:
                continue
            forward_bps = (future_close - c) * 10_000 // c
            if args.max_abs_forward and abs(forward_bps) > args.max_abs_forward:
                skipped_label += 1
                continue
            fields = kbar_fields(o, h, l, c)
            if fields is None:
                skipped_shape += 1
                continue
            emitted = False
            for name, value in fields.items():
                if value is None:
                    continue
                per_day[day][name].append((float(value), float(forward_bps)))
                emitted = True
            if emitted:
                graded += 1
            else:
                skipped_label += 1

    results = {}
    for name in KBAR_FIELDS:
        daily_ics: list[int] = []
        pooled: list[tuple[float, float]] = []
        spreads: list[int] = []
        for day in sorted(per_day):
            observations = per_day[day][name]
            if len(observations) < args.min_symbols:
                continue
            ic = rank_ic(observations)
            daily_ics.append(int(round(ic * 10_000)))
            pooled.extend(observations)
            low, high, spread = quantile_spread(observations)
            if spread is not None:
                spreads.append(spread)
        if not daily_ics:
            results[name] = {"days": 0, "mean_ic_bps": None, "ir_bps": None}
            continue
        mean_ic = sum(daily_ics) / len(daily_ics)
        std_ic = statistics.pstdev(daily_ics) if len(daily_ics) > 1 else 0.0
        ir = mean_ic / std_ic if std_ic > 0 else None
        results[name] = {
            "days": len(daily_ics),
            "mean_ic_bps": int(round(mean_ic)),
            "std_ic_bps": int(round(std_ic)),
            "ir_bps": int(round(ir * 10_000)) if ir is not None else None,
            "mean_spread_bps": int(sum(spreads) / len(spreads)) if spreads else None,
            "pooled_samples": len(pooled),
        }

    payload = {
        "db": args.db,
        "horizon_days": args.horizon,
        "symbols": len(series),
        "graded_observations": graded,
        "skipped_no_shape": skipped_shape,
        "skipped_no_field": skipped_label,
        "factors": results,
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    print(f"symbols={len(series)} graded={graded} horizon={args.horizon}d")
    print(f"{'factor':8} {'days':>5} {'meanIC':>8} {'IR':>8} {'spread':>8}")
    for name in KBAR_FIELDS:
        row = results[name]
        if not row.get("days"):
            print(f"{name:8} {'-':>5} {'-':>8} {'-':>8} {'-':>8}")
            continue
        ir = row.get("ir_bps")
        spread = row.get("mean_spread_bps")
        print(f"{name:8} {row['days']:>5} {row['mean_ic_bps']:>8} "
              f"{(ir if ir is not None else 0):>8} "
              f"{(spread if spread is not None else 0):>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
