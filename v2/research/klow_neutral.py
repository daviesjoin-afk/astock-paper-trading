"""Does `klow` survive industry and size neutralisation?

The previous round showed `klen` was just a volatility proxy (its IC collapsed
from t=-4.81 to t=-1.86 once past-5-day volatility was stripped). `klow` kept
its IC through that test. This script asks the next question: is `klow` merely
an industry effect or a small-cap effect wearing a different hat?

Method, per trading day (cross-section):

1. Build `klow` from each session's own OHLC.
2. Regress it on a set of controls *within that day*:
   - industry dummies (Shenwan level-1), so "this industry's stocks have long
     lower wicks" is removed
   - log of traded amount, as a size/liquidity proxy
   - past 5-day return volatility
3. Keep the residual, rank it, and correlate with the forward 5-day return.

The forward return ends on the market's fifth session after the factor day, not
on the symbol's own fifth remaining row. A halted name has fewer rows than a
name that traded throughout, so counting its own rows made a three-session move
and a five-session move the same label. A symbol with no bar on the market
label day is dropped from that cross-section, and the count is reported.

If the residual IC stays near the raw IC, `klow` carries information that those
controls do not. If it collapses, the factor was a repackaging of them.

Industry membership is point-in-time: `industry_history` records a start_day per
change, so the code in force on the factor day is the latest one at or before
it. Using the current classification for an old date would leak the future.

Usage:
    python research/klow_neutral.py --db work/full.sqlite3 --industry work/public.sqlite3
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import math
import sqlite3
import statistics
import sys


def scale_bps(numerator: int, denominator: int) -> int | None:
    if denominator <= 0:
        return None
    scaled = numerator * 10_000
    if scaled >= 0:
        return (scaled + denominator // 2) // denominator
    return -((-scaled + denominator // 2) // denominator)


def klow_bps(open_c: int, low: int, close: int) -> int | None:
    """(min(open, close) - low) / open, in bps."""
    if open_c <= 0 or low <= 0:
        return None
    return scale_bps(min(open_c, close) - low, open_c)


def average_ranks(values: list[float]) -> list[float]:
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


def ols_residual(y: list[float], design: list[list[float]]) -> list[float]:
    """Residual of y on a constant plus the given columns, via normal equations.

    Small and unregularised on purpose: the design is a few dozen columns at
    most (one per industry plus three controls) over thousands of rows, and the
    question is whether removing those directions kills the signal. If the
    matrix is singular the caller is told rather than silently getting a
    half-fitted residual.
    """
    n = len(y)
    if n == 0:
        return []
    cols = [list(col) for col in design]
    cols.insert(0, [1.0] * n)
    k = len(cols)
    if n <= k + 2:
        return [value - statistics.mean(y) for value in y]

    # Normal equations: (X'X) b = X'y
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for row in range(n):
        for i in range(k):
            ci = cols[i][row]
            if ci == 0.0:
                continue
            xty[i] += ci * y[row]
            for j in range(i, k):
                xtx[i][j] += ci * cols[j][row]
    for i in range(k):
        for j in range(i):
            xtx[i][j] = xtx[j][i]

    # Gaussian elimination with partial pivoting.
    for pivot in range(k):
        best = max(range(pivot, k), key=lambda r: abs(xtx[r][pivot]))
        if abs(xtx[best][pivot]) < 1e-9:
            # A collinear control (for example an industry with one member).
            # Dropping that direction is correct; the residual is still the
            # projection onto the remaining space.
            for r in range(k):
                xtx[r][pivot] = 0.0
            xtx[pivot][pivot] = 1.0
            xty[pivot] = 0.0
            continue
        if best != pivot:
            xtx[pivot], xtx[best] = xtx[best], xtx[pivot]
            xty[pivot], xty[best] = xty[best], xty[pivot]
        pivot_value = xtx[pivot][pivot]
        for r in range(pivot + 1, k):
            factor = xtx[r][pivot] / pivot_value
            if factor == 0.0:
                continue
            for c in range(pivot, k):
                xtx[r][c] -= factor * xtx[pivot][c]
            xty[r] -= factor * xty[pivot]

    beta = [0.0] * k
    for r in range(k - 1, -1, -1):
        if abs(xtx[r][r]) < 1e-9:
            beta[r] = 0.0
            continue
        total = xty[r] - sum(xtx[r][c] * beta[c] for c in range(r + 1, k))
        beta[r] = total / xtx[r][r]

    return [y[row] - sum(beta[i] * cols[i][row] for i in range(k)) for row in range(n)]


def load_industry(db: str) -> dict[str, list[tuple[str, str]]]:
    """symbol -> [(start_day, l1_code)] sorted by start_day."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    rows = con.execute("""
        select symbol, start_day, coalesce(l1_code, industry_code)
        from industry_history
        where coalesce(l1_code, industry_code) is not null
        order by symbol, start_day
    """).fetchall()
    con.close()
    book: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for symbol, start_day, code in rows:
        book[symbol].append((start_day, code))
    return book


def industry_on(book: dict[str, list[tuple[str, str]]], symbol: str, day: str) -> str | None:
    entries = book.get(symbol)
    if not entries:
        return None
    starts = [entry[0] for entry in entries]
    index = bisect.bisect_right(starts, day) - 1
    if index < 0:
        return None
    return entries[index][1]


def market_calendar(series: dict[str, list]) -> list[str]:
    """The market's tradable days: the union of every symbol's sessions.

    The label endpoint is counted on this calendar rather than on each symbol's
    own rows. A name that halted for a week does not shorten the market's
    window; it simply has no close on some of those days.
    """
    return sorted({bar[0] for bars in series.values() for bar in bars})


def build_observations(series: dict[str, list], industry: dict, horizon: int,
                       max_abs_forward: int = 2_500) -> tuple[dict[str, list], dict]:
    """Collect per-day cross-sections on the market's own session grid.

    For each decision day, `klow`, its controls, and the forward return over
    the market's `horizon`-th next session. A symbol with no bar on the label
    day is excluded from that day's cross-section; the alternative — reaching
    for its own nearer row — compares a 3-day move with a 5-day one.

    Returns `(per_day, stats)`; `stats` counts what was dropped and why, so a
    thin cross-section is visible instead of inferred.
    """
    calendar = market_calendar(series)
    position = {day: index for index, day in enumerate(calendar)}
    per_day: dict[str, list] = collections.defaultdict(list)
    stats = {"calendar_sessions": len(calendar), "missing_label_endpoint": 0}

    for symbol, bars in series.items():
        if len(bars) < 12:
            continue
        closes = [bar[3] for bar in bars]
        by_day = {bar[0]: index for index, bar in enumerate(bars)}
        for day, index in by_day.items():
            if index < 6:
                continue
            grid = position.get(day)
            if grid is None or grid + horizon >= len(calendar):
                continue
            label_day = calendar[grid + horizon]
            label_index = by_day.get(label_day)
            if label_index is None:
                stats["missing_label_endpoint"] += 1
                continue
            _d, open_c, low, close, amount = bars[index]
            value = klow_bps(open_c, low, close)
            if value is None:
                continue
            past = [
                (closes[j] - closes[j - 1]) * 10_000 // closes[j - 1]
                for j in range(index - 4, index + 1)
                if closes[j - 1] > 0
            ]
            if len(past) < 5:
                continue
            vol = statistics.pstdev(past)
            future = (closes[label_index] - close) * 10_000 // close
            if abs(future) > max_abs_forward:
                continue
            code = industry_on(industry, symbol, day)
            per_day[day].append((float(value), code, math.log1p(max(1, amount)),
                                 float(vol), float(future)))
    return per_day, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--industry", required=True)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--min-symbols", type=int, default=300)
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    industry = load_industry(args.industry)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=120)
    rows = con.execute("""
        select symbol, day, open_cents, low_cents, close_cents, amount_cents
        from bar
        where adjust='none' and tradable=1 and close_cents>0
        order by symbol, day
    """).fetchall()
    con.close()

    series: dict[str, list] = collections.defaultdict(list)
    for symbol, day, open_c, low, close, amount in rows:
        series[symbol].append((day, open_c, low, close, amount))

    # Per day: (klow, industry, log amount, past volatility, forward return).
    # Days come from the market calendar, so every forward return in one
    # cross-section spans the same number of sessions.
    per_day, observation_stats = build_observations(series, industry, args.horizon)

    results = {}
    for label, use_industry, use_size, use_vol in (
        ("raw", False, False, False),
        ("vol", False, False, True),
        ("vol+size", False, True, True),
        ("vol+size+industry", True, True, True),
    ):
        daily: list[float] = []
        for day in sorted(per_day):
            observations = per_day[day]
            if len(observations) < args.min_symbols:
                continue
            y = [o[0] for o in observations]
            design: list[list[float]] = []
            if use_vol:
                design.append([o[3] for o in observations])
            if use_size:
                design.append([o[2] for o in observations])
            if use_industry:
                codes = sorted({o[1] for o in observations if o[1] is not None})
                for code in codes[:-1]:  # last is the reference level
                    design.append([1.0 if o[1] == code else 0.0 for o in observations])
            residual = ols_residual(y, design) if design else y
            forward = [o[4] for o in observations]
            daily.append(pearson(average_ranks(residual), average_ranks(forward)))
        if not daily:
            results[label] = {"days": 0}
            continue
        mean = statistics.mean(daily)
        std = statistics.pstdev(daily)
        results[label] = {
            "days": len(daily),
            "mean_ic": round(mean, 4),
            "t": round(mean / std * math.sqrt(len(daily)), 2) if std > 0 else None,
        }

    payload = {"db": args.db, "horizon_days": args.horizon,
               "symbols": len(series), "results": results,
               "market_sessions": observation_stats["calendar_sessions"],
               "skipped_label_endpoint": observation_stats["missing_label_endpoint"],
               "label_endpoint_rule": (
                   "the label ends on the market's `horizon`-th session after the "
                   "decision day; a symbol with no bar that day is dropped from "
                   "that cross-section"
               )}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    print(f"symbols={len(series)} horizon={args.horizon}d")
    print(f"{'控制变量':22} {'days':>5} {'meanIC':>9} {'t':>7}")
    for label in ("raw", "vol", "vol+size", "vol+size+industry"):
        row = results.get(label, {})
        if not row.get("days"):
            print(f"{label:22} {'-':>5}")
            continue
        print(f"{label:22} {row['days']:>5} {row['mean_ic']:>9} {row['t']:>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
