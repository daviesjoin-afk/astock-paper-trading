#!/usr/bin/env python3
"""Create a deterministic 80-trading-day CSV for the rewritten V2 demo.

The prices are synthetic and must be published with --trust synthetic. They
exist to exercise both strategies, T+1 and the allocation loop end to end; they
are not market data and never belong in a reviewed production directory.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path


def trading_days(start: str, count: int) -> list[str]:
    day = date.fromisoformat(start)
    days: list[str] = []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--days", type=int, default=80)
    args = parser.parse_args()

    days = trading_days(args.start, args.days)
    symbols = ("600000.SH", "000001.SZ", "600519.SH")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("symbol", "day", "price", "source", "observed_at"))
        for symbol_index, symbol in enumerate(symbols):
            for index, day in enumerate(days):
                base = (1_000, 1_200, 1_500)[symbol_index]
                trend = index * (3 + symbol_index)
                wave = (index % 7) * (2 + symbol_index)
                price = base + trend + wave
                writer.writerow(
                    (
                        symbol,
                        day,
                        f"{price // 100}.{price % 100:02d}",
                        "offline-demo",
                        f"{day}T15:00:00+08:00",
                    )
                )
    print(f"wrote {args.out} rows={len(days) * len(symbols)} days={len(days)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
