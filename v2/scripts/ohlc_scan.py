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

The second thing it must not repeat is the mixed cross-section: counting
backwards from each symbol's own last row put a halted name on a different
decision day from everyone else, so one "rank IC" mixed several dates. The
decision day is now one market day, shared by the whole cross-section, and a
symbol with no bar that day is excluded from it.

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
MIN_LOOKBACK = 60

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


def market_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """The market's trading days inside the window: the union of all OHLC bars.

    Same convention as `volume_scan.market_calendar` and the Rust store's
    `--from`/`--through`: a day is a market day when some symbol has a bar on
    it. This is the only calendar the database can attest to; a day on which
    nothing traded is invisible here and is surfaced by the session-count guard
    in `main` instead of being scored as a cross-section.
    """
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT day FROM bar WHERE adjust='none' AND day BETWEEN ? AND ? "
        "AND close_cents > 0 ORDER BY day",
        (start, end),
    )]


def decision_and_label_days(calendar: list[str], horizon: int = HORIZON):
    """The one decision day and one label day shared by the cross-section."""
    if len(calendar) < horizon + 1:
        return None, None
    return calendar[-1 - horizon], calendar[-1]


def _series_index(points: list) -> dict[str, int]:
    return {point[0]: position for position, point in enumerate(points)}


def factor_values(series: dict, name: str, horizon: int = HORIZON,
                  calendar: list[str] | None = None,
                  decision_day: str | None = None) -> dict[str, float]:
    """Value strictly before the forward window, on a shared decision day.

    Same cut as `volume_scan`: the factor is evaluated at the decision bar and
    the label starts one session after it, so no bar is used on both sides. The
    decision bar is located by date on the supplied calendar; a symbol with no
    bar that day is not part of this cross-section.
    """
    if calendar is None:
        calendar = sorted({point[0] for points in series.values() for point in points})
    if decision_day is None:
        decision_day, _label_day = decision_and_label_days(calendar, horizon)
    if decision_day is None or decision_day not in calendar:
        return {}
    decision_index = calendar.index(decision_day)
    out: dict[str, float] = {}
    for symbol, points in series.items():
        position = _series_index(points).get(decision_day)
        if position is None:
            # Halted or missing through the shared decision day.
            continue
        if decision_index < MIN_LOOKBACK or position < MIN_LOOKBACK:
            continue
        value = compute(name, points[: position + 1])
        if value is not None:
            out[symbol] = value
    return out


def forward_returns(series: dict, horizon: int = HORIZON,
                    calendar: list[str] | None = None,
                    decision_day: str | None = None,
                    label_day: str | None = None) -> dict[str, float]:
    """Return from the shared decision day to the shared label day.

    Both ends come from the market calendar, so every label spans the same
    number of sessions even when one symbol halted mid-window.
    """
    if calendar is None:
        calendar = sorted({point[0] for points in series.values() for point in points})
    if decision_day is None or label_day is None:
        derived_decision, derived_label = decision_and_label_days(calendar, horizon)
        decision_day = decision_day or derived_decision
        label_day = label_day or derived_label
    if decision_day is None or label_day is None:
        return {}
    out: dict[str, float] = {}
    for symbol, points in series.items():
        index = _series_index(points)
        start, end = index.get(decision_day), index.get(label_day)
        if start is None or end is None or end < start:
            # Unknown label, dropped rather than filled with zero.
            continue
        base = points[start][4]
        if base <= 0:
            continue
        out[symbol] = (points[end][4] - base) * 10_000 / base
    return out


def scan(conn: sqlite3.Connection, start: str, end: str) -> dict:
    series = load_window(conn, start, end)
    calendar = market_calendar(conn, start, end)
    decision_day, label_day = decision_and_label_days(calendar, HORIZON)
    if decision_day is None:
        return {**{name: {"samples": 0, "rank_ic_bps": None} for name in FACTORS},
                "decision_day": None, "label_day": None,
                "calendar_sessions": len(calendar), "cross_section": 0}
    forwards = forward_returns(series, HORIZON, calendar, decision_day, label_day)
    results = {}
    cross_section = 0
    for name in FACTORS:
        values = factor_values(series, name, HORIZON, calendar, decision_day)
        common = sorted(set(values) & set(forwards))
        cross_section = max(cross_section, len(common))
        if len(common) < 100:
            results[name] = {"samples": len(common), "rank_ic_bps": None}
            continue
        rho = spearman([values[s] for s in common], [forwards[s] for s in common])
        results[name] = {
            "samples": len(common),
            "rank_ic_bps": None if rho is None else round(rho * 10_000),
        }
    return {
        **results,
        "decision_day": decision_day,
        "label_day": label_day,
        "calendar_sessions": len(calendar),
        "cross_section": cross_section,
    }


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
        "per_window": per_window,
        "summary": summary,
        "ranking": ordered,
        "decision_day_rule": (
            "one market decision day per window (the bar 20 market sessions before "
            "the last), shared by the whole cross-section; a symbol without a bar "
            "on that day is excluded from the cross-section"
        ),
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
