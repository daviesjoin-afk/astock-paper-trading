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

Factors here are all "how did volume or turnover behave relative to price".
The unit each one is computed from is part of its definition, because the two
are not interchangeable — a 3-yuan bank and a 300-yuan name trade wildly
different share counts for the same money:

* `volume_ratio_20` — today's traded **shares** against their 20-day mean.
  Unit: share-based, in bps of the mean (10 000 = exactly average).
* `price_up_volume_down` — the classic divergence: rising price on falling
  **share** volume. Encoded as the price move in bps minus the share-volume
  change in bps.
* `turnover_shock` — today's traded **amount** (元) against its 60-day mean.
  Unit: amount-based, bps of the mean (10 000 = exactly average).
* `amihud_20` — mean of |return bps| / amount in 元, scaled by 1e8. Unit:
  bps per 100 million yuan traded — price impact per unit of money, which is
  what Amihud (2002) defines. Using share count here answers a different
  question ("impact per share"), and for two names at different prices that is
  not comparable.
* `volume_weighted_momentum_20` — the 20-day price move weighted by traded
  **shares**; the weight is a participation proxy, not money.

Every report states these units in `units`, so a reader cannot mistake a
share-based number for an amount-based one.

The cross-section is taken on one market decision day, not on each symbol's own
twenty-first-from-last record. A halted name is missing rows; counting backwards
from its own end put it on a different day from everyone else and mixed decision
dates in a single correlation. Symbols without a bar on the decision day (or on
the label day) are left out of that cross-section.

Usage:
    python scripts/volume_scan.py --db market.sqlite3 \
        --periods 2024-03-15,2024-07-19,2024-11-15,2025-03-14,2025-07-18,2025-11-14,2026-03-13 \
        --out volume-scan.json
"""
from __future__ import annotations

import argparse
from datetime import date
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
HORIZON = 20
# Sessions the factor needs behind the decision bar: 20 for the shortest
# lookback, 60 for the slow turnover baseline.
MIN_LOOKBACK = 60

# What each factor is measured in. Written into the report so a share-based
# number cannot be read as a money-based one.
UNITS = {
    "volume_ratio_20": "bps of the 20-session mean traded shares (10000 = average)",
    "turnover_shock": "bps of the 60-session mean traded amount in yuan (10000 = average)",
    "price_up_volume_down": "price move bps minus traded-share change bps",
    "amihud_20": "|return| bps per 100 million yuan traded, averaged over 20 sessions",
    "volume_weighted_momentum_20": "bps, 20-session returns weighted by traded shares",
}


def load_window(conn: sqlite3.Connection, start: str, end: str):
    """Per-symbol ordered rows inside a window.

    Each point is `(day, close_cents, volume_shares, amount_cents)`. Both the
    share count and the amount travel together because the factors are not all
    measured in the same unit.
    """
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


def market_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """The market's trading days inside the window: the union of all bars.

    A cross-section has to share one decision day. Counting backwards from each
    symbol's own last row put a halted name on a different day from everyone
    else, so a single "rank IC" mixed several decision dates. The union of the
    window's bars is the market calendar this database can attest to (the same
    convention the Rust store uses for `--from`/`--through`).

    A day on which *nothing* traded is absent from this union. That is a data
    outage, not a factor question, and the window-length guard below refuses a
    window that is short on sessions rather than silently scoring one.
    """
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT day FROM bar WHERE adjust='none' AND day BETWEEN ? AND ? "
        "AND close_cents > 0 ORDER BY day",
        (start, end),
    )]


def decision_and_label_days(calendar: list[str], horizon: int = HORIZON):
    """The one decision day and one label day the whole cross-section shares.

    Returns `(decision_day, label_day)`; the label day is `horizon` sessions
    after the decision day *on the market calendar*, so a symbol that halted
    through part of the window cannot shorten its own label.
    """
    if len(calendar) < horizon + 1:
        return None, None
    return calendar[-1 - horizon], calendar[-1]


def _series_index(points: list) -> dict[str, int]:
    return {point[0]: position for position, point in enumerate(points)}


def _lookback(points: list, index: int):
    """Closes / shares / amounts from the symbol's own start up to `index`."""
    window = points[: index + 1]
    return ([point[1] for point in window],
            [point[2] for point in window],
            [point[3] for point in window])


def _union_calendar(series: dict) -> list[str]:
    """The calendar to use when the caller supplied none.

    The rows are read in chronological order per symbol, so the first-seen order
    is chronological for a single series and for the leading symbol of a set.
    Where every day is an ISO date the union is sorted as well, so a multi-symbol
    call without an explicit calendar still lands on the right day. This is a
    convenience path for tests and hand-built inputs; the scans themselves pass
    the database's market calendar.
    """
    days = list(dict.fromkeys(point[0] for points in series.values() for point in points))
    try:
        for day in days:
            date.fromisoformat(day)
    except ValueError:
        return days
    return sorted(days)


def factor_values(series: dict, name: str, horizon: int = HORIZON,
                  calendar: list[str] | None = None,
                  decision_day: str | None = None) -> dict[str, float]:
    """One value per symbol at the shared decision day.

    The first version computed the factor at `closes[-1]` and labelled it with a
    forward return that also ended at `closes[-1]` -- the two windows overlapped,
    so the scan was measuring a series against itself. That is why
    `volume_weighted_momentum_20` came back with +8611 bps IC and IR 61: it was
    not a strong signal, it was a look-ahead.

    The factor is now evaluated at the decision bar and the forward return
    starts one session after it. The decision bar is located by *date* on the
    supplied market calendar; a symbol with no bar that day is not in this
    cross-section. Passing no calendar means "the union of the series' own
    days", which is only the market calendar when one symbol is supplied.
    """
    if calendar is None:
        calendar = _union_calendar(series)
    if decision_day is None:
        decision_day, _label_day = decision_and_label_days(calendar, horizon)
    if decision_day is None:
        return {}
    decision_index = calendar.index(decision_day) if decision_day in calendar else None
    if decision_index is None:
        return {}
    out: dict[str, float] = {}
    for symbol, points in series.items():
        position = _series_index(points).get(decision_day)
        if position is None:
            # Missing a bar on the shared decision day: this name cannot be in
            # this cross-section, whatever its own record count says.
            continue
        if decision_index < MIN_LOOKBACK or position < MIN_LOOKBACK:
            continue
        closes, shares, amounts = _lookback(points, position)
        try:
            value = compute(name, closes, shares, amounts)
        except (ZeroDivisionError, IndexError):
            continue
        if value is not None:
            out[symbol] = value
    return out


def compute(name: str, closes: list[int], shares: list[int],
            amounts: list[int] | None = None) -> float | None:
    """One factor value from aligned close / share / amount series.

    `shares` is the traded share count; `amounts` is the traded amount in yuan.
    They are separate arguments because they answer different questions, and
    the factor name decides which one is used.
    """
    latest, past = closes[-1], closes[-21]
    if past <= 0 or latest <= 0:
        return None
    if name == "volume_ratio_20":
        window = shares[-20:]
        if not window or sum(window) <= 0:
            return None
        return (shares[-1] * 10_000) / (sum(window) / len(window))
    if name == "turnover_shock":
        # Amount, not share count: "turnover" is money changing hands.
        if not amounts:
            return None
        window = amounts[-60:]
        if not window or sum(window) <= 0:
            return None
        return (amounts[-1] * 10_000) / (sum(window) / len(window))
    if name == "price_up_volume_down":
        price_bps = (latest - past) * 10_000 / past
        recent_volume = sum(shares[-5:]) / 5
        older_volume = sum(shares[-20:-5]) / 15 if len(shares) >= 20 else 0
        if older_volume <= 0:
            return None
        volume_bps = (recent_volume - older_volume) * 10_000 / older_volume
        return price_bps - volume_bps
    if name == "amihud_20":
        # Amihud (2002): |return| per unit of *money* traded. Using the share
        # count made a 3-yuan name look ten times more illiquid than a
        # 30-yuan name with the same turnover.
        if not amounts:
            return None
        total = 0.0
        count = 0
        for index in range(len(closes) - 20, len(closes)):
            if index <= 0 or closes[index - 1] <= 0:
                continue
            move = abs(closes[index] - closes[index - 1]) * 10_000 / closes[index - 1]
            if amounts[index] <= 0:
                continue
            # |return| in bps divided by yuan, scaled to bps per 1e8 yuan.
            total += move * 1e8 / amounts[index]
            count += 1
        return total / count if count else None
    if name == "volume_weighted_momentum_20":
        numerator = 0.0
        denominator = 0.0
        for index in range(len(closes) - 20, len(closes)):
            if index <= 0 or closes[index - 1] <= 0:
                continue
            move = (closes[index] - closes[index - 1]) * 10_000 / closes[index - 1]
            weight = float(shares[index])
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


def forward_returns(series: dict, horizon: int = HORIZON,
                    calendar: list[str] | None = None,
                    decision_day: str | None = None,
                    label_day: str | None = None) -> dict[str, float]:
    """Return from the decision day to the label day, both from the calendar.

    Same cut as `factor_values`: the label starts where the factor stops. The
    end point is `horizon` market sessions later, not "this symbol's
    `horizon`-th remaining row" — a name that halted for a week has fewer rows
    than a name that traded, and using its own rows made the two labels span
    different amounts of time.
    """
    if calendar is None:
        calendar = _union_calendar(series)
    if decision_day is None or label_day is None:
        derived_decision, derived_label = decision_and_label_days(calendar, horizon)
        decision_day = decision_day or derived_decision
        label_day = label_day or derived_label
    if decision_day is None or label_day is None:
        return {}
    out = {}
    for symbol, points in series.items():
        index = _series_index(points)
        start, end = index.get(decision_day), index.get(label_day)
        if start is None or end is None or end < start:
            # No bar on either end of the label window: the return over that
            # span is unknown, and an unknown label is dropped, not zeroed.
            continue
        base = points[start][1]
        if base <= 0:
            continue
        out[symbol] = (points[end][1] - base) * 10_000 / base
    return out


def scan(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """Rank IC of every factor against the forward return inside the window."""
    series = load_window(conn, start, end)
    calendar = market_calendar(conn, start, end)
    decision_day, label_day = decision_and_label_days(calendar, HORIZON)
    if decision_day is None:
        return {**{name: {"samples": 0, "rank_ic_bps": None} for name in FACTORS},
                "decision_day": None, "label_day": None, "calendar_sessions": len(calendar),
                "cross_section": 0}
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
        # Factor rows stay at the top level of each window entry, as they were,
        # so existing readers of `per_window[].amihud_20` keep working; the
        # cross-section bookkeeping is added beside them.
        **results,
        "decision_day": decision_day,
        "label_day": label_day,
        "calendar_sessions": len(calendar),
        # How many names actually traded on the decision day with a measurable
        # label. Symbols that halted through it are excluded, not zero-filled.
        "cross_section": cross_section,
    }


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
            summary[name] = {"periods": 0, "usable": False, "unit": UNITS[name]}
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
            "unit": UNITS[name],
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
        "horizon_sessions": HORIZON,
        # The unit of each factor, stated once here as well as per factor, so a
        # share-based IC cannot be read as a money-based one.
        "units": {
            "shares": "traded shares (volume_shares)",
            "amount": "traded amount in yuan (amount_cents / 100)",
            "rank_ic": "basis points of Spearman correlation",
            "factors": UNITS,
        },
        "decision_day_rule": (
            "one market decision day per window (the bar 20 market sessions before "
            "the last), shared by the whole cross-section; a symbol without a bar "
            "on that day is excluded from the cross-section"
        ),
        "note": ("Volume is the one input the project has always had and never "
                 "used. This measures it before any engine change."),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in ordered:
        print(f"  {row['name']:28s} mean {row['mean_rank_ic_bps']:+6d} "
              f"IR {row['ir_bps']} pos {row['positive_periods']}/{row['periods']} "
              f"flips {row['sign_flips']} ics={row['rank_ics']}")
        print(f"  {'':28s} unit: {row['unit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
