#!/usr/bin/env python3
"""Scan OHLC-shape factors before touching the engine.

Close-only signals have been tried exhaustively and none reached significance.
Volume and turnover were tried next and also failed. Open, high and low have
been in the database on 99% of rows the whole time and nothing has used them.

These factors are all about the *shape* of a session rather than its level:

* `upper_shadow_ratio` — how much of the range was given back. A long upper
  shadow is supply meeting demand.
* `lower_shadow_ratio` — the mirror: a long lower shadow is demand absorbing
  supply.
* `close_location` — where the close sits inside the day's range, in bps. This
  is the classic "where did the auction finish" measure.
* `body_ratio` — the real body as a share of the range. A small body in a wide
  range is indecision.
* `gap_bps` — today's open against yesterday's close.
* `range_bps` — the day's range against the previous close, averaged over the
  window. A volatility measure built from OHLC rather than closes.

The one thing this must not repeat is the look-ahead that was caught earlier:
the factor is measured strictly before the forward window, and a test asserts
that changing the forward window cannot change the factor value.

Usage:
    python scripts/ohlc_scan.py --db market.sqlite3 \
        --periods 2023-09-01:2024-03-15,... --out ohlc-scan.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import statistics

MIN_POINTS = 80
HORIZON = 20

FACTORS = (
    "upper_shadow_ratio",
    "lower_shadow_ratio",
    "close_location",
    "body_ratio",
    "gap_bps",
    "range_bps",
)


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    a, b = rank(left), rank(right)
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    variance_a = sum((x - mean_a) ** 2 for x in a)
    variance_b = sum((y - mean_b) ** 2 for y in b)
    if variance_a <= 0 or variance_b <= 0:
        return None
    return covariance / (variance_a * variance_b) ** 0.5


def load_window(conn: sqlite3.Connection, start: str, end: str):
    rows = conn.execute(
        "SELECT symbol, day, open_cents, high_cents, low_cents, close_cents, prev_close_cents "
        "FROM bar WHERE adjust='none' AND day BETWEEN ? AND ? AND close_cents > 0 "
        "AND open_cents > 0 AND high_cents > 0 AND low_cents > 0 "
        "ORDER BY symbol, day",
        (start, end),
    ).fetchall()
    series: dict[str, list[tuple[str, int, int, int, int, int]]] = {}
    for symbol, day, open_cents, high, low, close, prev in rows:
        series.setdefault(symbol, []).append((day, open_cents, high, low, close, prev))
    return series


def compute(name: str, points: list[tuple]) -> float | None:
    """One value from the bar at the end of `points`."""
    _, open_cents, high, low, close, prev = points[-1]
    span = high - low
    if span <= 0 or close <= 0 or prev <= 0:
        return None
    if name == "upper_shadow_ratio":
        top = max(open_cents, close)
        return (high - top) * 10_000 / span
    if name == "lower_shadow_ratio":
        bottom = min(open_cents, close)
        return (bottom - low) * 10_000 / span
    if name == "close_location":
        return (close - low) * 10_000 / span
    if name == "body_ratio":
        return abs(close - open_cents) * 10_000 / span
    if name == "gap_bps":
        return (open_cents - prev) * 10_000 / prev
    if name == "range_bps":
        window = points[-20:]
        total = 0
        count = 0
        for _, op, hi, lo, cl, pv in window:
            if pv <= 0:
                continue
            total += (hi - lo) * 10_000 // pv
            count += 1
        return total / count if count else None
    raise ValueError(f"unknown factor {name}")


def factor_values(series: dict, name: str, horizon: int = HORIZON) -> dict[str, float]:
    """Value strictly before the forward window.

    Same cut as `volume_scan`: the factor is evaluated at the decision bar and
    the label starts one step after it, so no bar is used on both sides.
    """
    out: dict[str, float] = {}
    for symbol, points in series.items():
        if len(points) < MIN_POINTS:
            continue
        decision_index = len(points) - 1 - horizon
        if decision_index < 60:
            continue
        value = compute(name, points[: decision_index + 1])
        if value is not None:
            out[symbol] = value
    return out


def forward_returns(series: dict, horizon: int = HORIZON) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol, points in series.items():
        if len(points) < MIN_POINTS:
            continue
        closes = [point[4] for point in points]
        decision_index = len(points) - 1 - horizon
        if decision_index < 0:
            continue
        base = closes[decision_index]
        if base <= 0:
            continue
        out[symbol] = (closes[-1] - base) * 10_000 / base
    return out


def scan(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict]:
    series = load_window(conn, start, end)
    forwards = forward_returns(series)
    results = {}
    for name in FACTORS:
        values = factor_values(series, name)
        common = sorted(set(values) & set(forwards))
        if len(common) < 100:
            results[name] = {"samples": len(common), "rank_ic_bps": None}
            continue
        rho = spearman([values[s] for s in common], [forwards[s] for s in common])
        results[name] = {
            "samples": len(common),
            "rank_ic_bps": None if rho is None else round(rho * 10_000),
        }
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--periods", required=True,
                        help="comma separated 'start:end' windows, at least three")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")
    windows = [part.strip() for part in args.periods.split(",") if part.strip()]
    if len(windows) < 3:
        raise SystemExit("at least three windows are required before a sign means anything")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        for window in windows:
            start, _, end = window.partition(":")
            if not start or not end:
                raise SystemExit(f"window must be START:END, got {window!r}")
            count = conn.execute(
                "SELECT COUNT(DISTINCT day) FROM bar WHERE adjust='none' AND day BETWEEN ? AND ?",
                (start, end),
            ).fetchone()[0]
            if count < MIN_POINTS:
                raise SystemExit(
                    f"window {window} has {count} sessions; at least {MIN_POINTS} "
                    "are needed (60 lookback + 20 forward)"
                )
        per_window = []
        for window in windows:
            start, _, end = window.partition(":")
            per_window.append({"window": window, **scan(conn, start, end)})
    finally:
        conn.close()

    summary = {}
    for name in FACTORS:
        ics = [entry[name]["rank_ic_bps"] for entry in per_window
               if entry[name]["rank_ic_bps"] is not None]
        if not ics:
            summary[name] = {"periods": 0, "usable": False}
            continue
        positive = sum(1 for value in ics if value > 0)
        mean = round(statistics.fmean(ics))
        ir = None
        if len(ics) >= 2:
            stdev = statistics.pstdev(ics)
            if stdev > 0:
                ir = round(mean / stdev)
        summary[name] = {
            "periods": len(ics),
            "rank_ics": ics,
            "mean_rank_ic_bps": mean,
            "ir_bps": ir,
            "positive_periods": positive,
            "sign_flips": sum(1 for left, right in zip(ics, ics[1:])
                              if (left >= 0) != (right >= 0)),
            "all_same_sign": positive in (0, len(ics)),
            "usable": True,
        }

    ordered = sorted(
        (dict(name=name, **info) for name, info in summary.items() if info.get("usable")),
        key=lambda row: (not row["all_same_sign"], -row["positive_periods"],
                         -abs(row["mean_rank_ic_bps"])),
    )
    report = {
        "windows": windows,
        "horizon_sessions": HORIZON,
        "summary": summary,
        "ranking": ordered,
        "note": ("Open/high/low were ingested from day one and never used by a "
                 "signal. The factor is measured before the forward window, and "
                 "a test asserts that changing the label cannot move it."),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in ordered:
        print(f"  {row['name']:22s} mean {row['mean_rank_ic_bps']:+6d} IR {row['ir_bps']} "
              f"pos {row['positive_periods']}/{row['periods']} flips {row['sign_flips']} "
              f"ics={row['rank_ics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
