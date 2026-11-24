#!/usr/bin/env python3
"""Do announcements predict anything? Measure before building anything.

Announcements are the first dimension in this project that carries a real
publication date. Close, volume, turnover and OHLC shape were all scanned and
none produced a stable sign; `company` data is a snapshot with no history, so it
cannot be used in a backtest at all.

Two things this scan insists on:

* **A dated announcement is not usable on its own date.** CNINFO gives a date,
  not a time of day, so an announcement dated `d` may have appeared after the
  close of `d`. It therefore counts for a decision taken at `d'` only when
  `d < d'` -- that is, from the next trading session onward. The label must
  respect the same rule: it starts at the close of the first session *after*
  the decision day, never at the decision day's own close. A test asserts that
  moving the horizon cannot change the factor, and another asserts that the
  decision-day close never enters the label.
* **A sign needs more than one window.** With fewer than three windows the
  scan refuses to compute a sign-consistency summary, because one window's
  sign is not a sign.

Usage:
    python v2/scripts/announcement_scan.py --announcements ann.sqlite3 \
        --bars research.sqlite3 --periods a:b,c:d,... --out report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import statistics

MIN_POINTS = 60
HORIZON = 20
# A daily announcement is only knowable from the next session, so the forward
# label must start there too: the decision-day close is not a tradable entry.
LABEL_START_OFFSET = 1

FACTORS = (
    "announcements_5d",
    "announcements_20d",
    "announcements_60d",
    "days_since_last",
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


def load_announcements(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT symbol, announcement_date FROM announcement "
        "WHERE announcement_date BETWEEN ? AND ? ORDER BY symbol, announcement_date",
        (start, end),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for symbol, day in rows:
        out.setdefault(symbol, []).append(day)
    return out


def load_closes(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[tuple[str, int]]]:
    rows = conn.execute(
        "SELECT symbol, day, close_cents FROM bar WHERE adjust='none' AND close_cents > 0 "
        "AND day BETWEEN ? AND ? ORDER BY symbol, day",
        (start, end),
    ).fetchall()
    out: dict[str, list[tuple[str, int]]] = {}
    for symbol, day, close in rows:
        out.setdefault(symbol, []).append((day, close))
    return out


def factor_value(name: str, days: list[str], decision_day: str) -> float | None:
    """Value at `decision_day`, counting only announcements dated before that day.

    A CNINFO date carries no time of day, so an announcement dated `d` may have
    been published after the close of `d`; it is therefore not knowable at a
    decision taken at `d`. Only strictly earlier dates count, which is exactly
    "usable from the next trading session onward".
    """
    known = [day for day in days if day < decision_day]
    if not known:
        # "No announcement ever" and "no announcement in the window" are
        # different claims; a symbol with no history cannot be scored.
        return None
    if name == "announcements_5d":
        return float(sum(1 for day in known if day >= _shift(decision_day, -5)))
    if name == "announcements_20d":
        return float(sum(1 for day in known if day >= _shift(decision_day, -20)))
    if name == "announcements_60d":
        return float(sum(1 for day in known if day >= _shift(decision_day, -60)))
    if name == "days_since_last":
        return float(_day_gap(known[-1], decision_day))
    raise ValueError(f"unknown factor {name}")


def _shift(day: str, days: int) -> str:
    """Calendar arithmetic on an ISO date, used only for window comparisons."""
    import datetime
    value = datetime.date.fromisoformat(day) + datetime.timedelta(days=days)
    return value.isoformat()


def _day_gap(earlier: str, later: str) -> int:
    import datetime
    a = datetime.date.fromisoformat(earlier)
    b = datetime.date.fromisoformat(later)
    return (b - a).days


def scan(announcements, closes, start: str, end: str) -> dict:
    results = {}
    # Decision days: every session that has enough forward bars left.
    for name in FACTORS:
        values, forwards = [], []
        for symbol, series in closes.items():
            if len(series) < MIN_POINTS:
                continue
            # The factor at bar `index` uses only announcements dated before
            # that bar, and the label starts one session later: the close that
            # produced the decision is not a price anyone could still trade at,
            # and it is also the bar a same-day announcement might have landed
            # after. Both ends therefore move by one session.
            days_only = [day for day, _ in series]
            for index in range(MIN_POINTS, len(series) - HORIZON - LABEL_START_OFFSET):
                decision_day = days_only[index]
                value = factor_value(name, announcements.get(symbol, []), decision_day)
                if value is None:
                    continue
                base_index = index + LABEL_START_OFFSET
                base = series[base_index][1]
                future = series[base_index + HORIZON][1]
                if base <= 0:
                    continue
                values.append(value)
                forwards.append((future - base) * 10_000 / base)
        if len(values) < 100:
            results[name] = {"samples": len(values), "rank_ic_bps": None}
            continue
        rho = spearman(values, forwards)
        results[name] = {
            "samples": len(values),
            "rank_ic_bps": None if rho is None else round(rho * 10_000),
        }
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--announcements", required=True, type=Path)
    parser.add_argument("--bars", required=True, type=Path)
    parser.add_argument("--periods", required=True,
                        help="comma separated START:END windows, at least three")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    windows = [part.strip() for part in args.periods.split(",") if part.strip()]
    if len(windows) < 3:
        raise SystemExit("at least three windows are required before a sign means anything")

    per_window = []
    with sqlite3.connect(f"file:{args.announcements}?mode=ro", uri=True) as ann, \
         sqlite3.connect(f"file:{args.bars}?mode=ro", uri=True) as bars:
        for window in windows:
            start, _, end = window.partition(":")
            if not start or not end:
                raise SystemExit(f"window must be START:END, got {window!r}")
            announcements = load_announcements(ann, start, end)
            closes = load_closes(bars, start, end)
            per_window.append({"window": window, **scan(announcements, closes, start, end)})

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
        "label_start_offset_sessions": LABEL_START_OFFSET,
        "announcement_availability": "next_tradable_session_after_source_date",
        "summary": summary,
        "ranking": ordered,
        "note": ("Announcement-dated factors use only announcements dated strictly "
                 "before the decision day (a date-precision announcement may be "
                 "published after that day's close), and the label starts at the "
                 "next session's close. Tests assert that the factor cannot move "
                 "when the horizon changes and that the decision-day close never "
                 "enters the label."),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in ordered:
        print(f"  {row['name']:20s} mean {row['mean_rank_ic_bps']:+6d} IR {row['ir_bps']} "
              f"pos {row['positive_periods']}/{row['periods']} flips {row['sign_flips']} "
              f"ics={row['rank_ics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
