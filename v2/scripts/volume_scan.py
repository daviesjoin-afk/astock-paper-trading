#!/usr/bin/env python3
"""Scan volume/price factors before touching the Rust factor engine.

Every signal tested so far used closes only, and seven changes in a row made
things worse. The database has carried volume and turnover on 99% of its rows
the whole time and nothing has used them.

Doing this in Python first is deliberate: extending `Factor` is a real change to
the expression engine and its tests, and it is not worth doing until a
volume-based measure shows a sign that survives several periods. This script
answers that question against the same database, with the same rank-IC
definition the Rust probe uses.

Factors here are all "how did volume behave relative to price":

* `volume_ratio_20` — today's turnover against its 20-day mean. High means
  unusual participation.
* `price_up_volume_down` — the classic divergence: rising price on falling
  volume. Encoded as the price move in bps minus the volume change in bps.
* `turnover_shock` — today's turnover against its 60-day mean, which is a
  slower baseline for the same question.
* `amihud_20` — mean of |return| / turnover: how much price moves per unit of
  money. A liquidity/impact measure, not a forecast.
* `volume_weighted_momentum_20` — the 20-day price move weighted by how much
  volume it happened on.

Usage:
    python scripts/volume_scan.py --db market.sqlite3 \
        --periods 2024-03-15,2024-07-19,2024-11-15,2025-03-14,2025-07-18,2025-11-14,2026-03-13 \
        --out volume-scan.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import statistics


def rank(values: list[float]) -> list[float]:
    """Average ranks, ties share the mean of their span."""
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


MIN_POINTS = 80


def load_window(conn: sqlite3.Connection, start: str, end: str):
    """Per-symbol ordered rows inside a window."""
    rows = conn.execute(
        "SELECT symbol, day, close_cents, volume_shares, amount_cents FROM bar "
        "WHERE adjust='none' AND day BETWEEN ? AND ? AND close_cents > 0 "
        "ORDER BY symbol, day",
        (start, end),
    ).fetchall()
    series: dict[str, list[tuple[str, int, int, int]]] = {}
    for symbol, day, close, volume, amount in rows:
        series.setdefault(symbol, []).append((day, close, volume or 0, amount or 0))
    return series


def factor_values(series: dict, name: str, horizon: int = 20) -> dict[str, float]:
    """One value per symbol, computed strictly BEFORE the forward window.

    The first version computed the factor at `closes[-1]` and labelled it with a
    forward return that also ended at `closes[-1]` -- the two windows overlapped,
    so the scan was measuring a series against itself. That is why
    `volume_weighted_momentum_20` came back with +8611 bps IC and IR 61: it was
    not a strong signal, it was a look-ahead.

    The factor is now evaluated at `closes[-1 - horizon]`, and the forward
    return spans the `horizon` sessions that follow it. No close is used on both
    sides of the split.
    """
    out: dict[str, float] = {}
    for symbol, points in series.items():
        if len(points) < MIN_POINTS:
            continue
        # Cut the series at the decision day. Everything the factor sees is
        # before this point; everything the label measures is after it.
        decision_index = len(points) - 1 - horizon
        if decision_index < 60:
            continue
        closes = [point[1] for point in points[: decision_index + 1]]
        volumes = [point[2] for point in points[: decision_index + 1]]
        try:
            value = compute(name, closes, volumes)
        except (ZeroDivisionError, IndexError):
            continue
        if value is not None:
            out[symbol] = value
    return out


def compute(name: str, closes: list[int], volumes: list[int]) -> float | None:
    latest, past = closes[-1], closes[-21]
    if past <= 0 or latest <= 0:
        return None
    if name == "volume_ratio_20":
        window = volumes[-20:]
        if not window or sum(window) <= 0:
            return None
        return (volumes[-1] * 10_000) / (sum(window) / len(window))
    if name == "turnover_shock":
        window = volumes[-60:]
        if not window or sum(window) <= 0:
            return None
        return (volumes[-1] * 10_000) / (sum(window) / len(window))
    if name == "price_up_volume_down":
        price_bps = (latest - past) * 10_000 / past
        recent_volume = sum(volumes[-5:]) / 5
        older_volume = sum(volumes[-20:-5]) / 15 if len(volumes) >= 20 else 0
        if older_volume <= 0:
            return None
        volume_bps = (recent_volume - older_volume) * 10_000 / older_volume
        return price_bps - volume_bps
    if name == "amihud_20":
        total = 0.0
        count = 0
        for index in range(len(closes) - 20, len(closes)):
            if index <= 0 or closes[index - 1] <= 0:
                continue
            move = abs(closes[index] - closes[index - 1]) * 10_000 / closes[index - 1]
            if volumes[index] <= 0:
                continue
            total += move / volumes[index]
            count += 1
        return total / count * 1_000_000 if count else None
    if name == "volume_weighted_momentum_20":
        numerator = 0.0
        denominator = 0.0
        for index in range(len(closes) - 20, len(closes)):
            if index <= 0 or closes[index - 1] <= 0:
                continue
            move = (closes[index] - closes[index - 1]) * 10_000 / closes[index - 1]
            weight = float(volumes[index])
            if weight <= 0:
                continue
            numerator += move * weight
            denominator += weight
        return numerator / denominator if denominator > 0 else None
    raise ValueError(f"unknown factor {name}")


FACTORS = (
    "volume_ratio_20",
    "turnover_shock",
    "price_up_volume_down",
    "amihud_20",
    "volume_weighted_momentum_20",
)


def forward_returns(series: dict, horizon: int = 20) -> dict[str, float]:
    """Return over the `horizon` sessions after the decision day.

    Same cut as `factor_values`: the label starts where the factor stops.
    """
    out = {}
    for symbol, points in series.items():
        if len(points) < MIN_POINTS:
            continue
        closes = [point[1] for point in points]
        decision_index = len(points) - 1 - horizon
        if decision_index < 0:
            continue
        base = closes[decision_index]
        if base <= 0:
            continue
        out[symbol] = (closes[-1] - base) * 10_000 / base
    return out


def scan(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict]:
    """Rank IC of every factor against the forward return inside the window."""
    series = load_window(conn, start, end)
    forwards = forward_returns(series)
    results = {}
    for name in FACTORS:
        values = factor_values(series, name, horizon=20)
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
                        help="comma separated 'start:end' windows")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")
    windows = [part.strip() for part in args.periods.split(",") if part.strip()]
    if len(windows) < 3:
        raise SystemExit("at least three windows are required before a sign means anything")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    # Guard the guard: a window with too few sessions scores nothing, and an
    # empty scan looks exactly like "no signal". Refuse it instead.
    for window in windows:
        start, _, end = window.partition(":")
        count = conn.execute(
            "SELECT COUNT(DISTINCT day) FROM bar WHERE adjust='none' AND day BETWEEN ? AND ?",
            (start, end),
        ).fetchone()[0]
        if count < MIN_POINTS:
            raise SystemExit(
                f"window {window} has {count} sessions; at least {MIN_POINTS} are "
                "needed (60 lookback + 20 forward)"
            )
    per_window = []
    try:
        for window in windows:
            start, _, end = window.partition(":")
            if not start or not end:
                raise SystemExit(f"window must be START:END, got {window!r}")
            result = scan(conn, start, end)
            per_window.append({"window": window, **result})
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
        "per_window": per_window,
        "summary": summary,
        "ranking": ordered,
        "horizon_sessions": 20,
        "note": ("Volume is the one input the project has always had and never "
                 "used. This measures it before any engine change."),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in ordered:
        print(f"  {row['name']:28s} mean {row['mean_rank_ic_bps']:+6d} "
              f"IR {row['ir_bps']} pos {row['positive_periods']}/{row['periods']} "
              f"flips {row['sign_flips']} ics={row['rank_ics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
