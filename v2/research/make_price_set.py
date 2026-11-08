#!/usr/bin/env python3
"""Build a reviewed A-share close price set artifact from a CSV export.

Standard library only. The output is data for the Rust service: it never writes
the ledger and never invents prices. Missing (symbol, day) pairs are reported;
by default they fail the build instead of being silently dropped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

COLUMNS = ("symbol", "day", "price", "source", "observed_at")


def parse_price(raw: str) -> int:
    """Yuan string -> integer cents. Reject more than two decimals."""
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as error:
        raise ValueError(f"price_not_a_number:{raw}") from error
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"price_more_than_two_decimals:{raw}")
    cents_int = int(cents)
    if cents_int <= 0:
        raise ValueError(f"price_must_be_positive:{raw}")
    return cents_int


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        missing = [name for name in COLUMNS if name not in header]
        if missing:
            raise ValueError(f"missing_columns:{','.join(missing)}")
        rows = []
        seen = set()
        for line, raw in enumerate(reader, start=2):
            row = {name: (raw.get(name) or "").strip() for name in COLUMNS}
            if not all(row.values()):
                raise ValueError(f"empty_field:line {line}")
            try:
                from datetime import date

                date.fromisoformat(row["day"])
            except ValueError as error:
                raise ValueError(f"invalid_day:line {line}:{row['day']}") from error
            key = (row["symbol"], row["day"])
            if key in seen:
                raise ValueError(f"duplicate_row:{key[0]}:{key[1]}")
            seen.add(key)
            row["price_cents"] = parse_price(row["price"])
            rows.append(row)
    if not rows:
        raise ValueError("empty_input")
    return rows


def source_digest(rows: list[dict]) -> str:
    """Stable digest over the normalized rows, independent of input order."""
    lines = sorted(
        f"{row['symbol']},{row['day']},{row['price_cents']},{row['source']},{row['observed_at']}\n"
        for row in rows
    )
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def build_set(
    rows: list[dict],
    set_id: str,
    pack: str,
    trust: str,
    coverage_start: str,
    coverage_end: str,
    symbols: list[str],
) -> dict:
    from datetime import date

    if trust not in {"reviewed", "synthetic"}:
        raise ValueError(f"invalid_trust:{trust}")
    start = date.fromisoformat(coverage_start)
    end = date.fromisoformat(coverage_end)
    if start > end:
        raise ValueError("invalid_coverage")
    wanted = set(symbols)
    for row in rows:
        if row["symbol"] not in wanted:
            raise ValueError(f"symbol_not_declared:{row['symbol']}")
        day = date.fromisoformat(row["day"])
        if day < start or day > end:
            raise ValueError(f"entry_outside_coverage:{row['symbol']}:{row['day']}")
    entries = [
        {
            "symbol": row["symbol"],
            "day": row["day"],
            "price_cents": row["price_cents"],
            "source": row["source"],
            "observed_at": row["observed_at"],
        }
        for row in sorted(rows, key=lambda item: (item["symbol"], item["day"]))
    ]
    return {
        "schema_version": 1,
        "price_set_id": set_id,
        "market_data_pack_id": pack,
        "trust": trust,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "entries": entries,
        "source_digest": source_digest(rows),
    }


def gap_report(entries: list[dict], symbols: list[str], days: list[str]) -> list[dict]:
    have = {(entry["symbol"], entry["day"]) for entry in entries}
    return [
        {"symbol": symbol, "day": day}
        for symbol in symbols
        for day in days
        if (symbol, day) not in have
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--set-id", required=True)
    parser.add_argument("--pack", required=True)
    parser.add_argument("--trust", default="reviewed", choices=("reviewed", "synthetic"))
    parser.add_argument("--coverage-start", required=True)
    parser.add_argument("--coverage-end", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--days", default="")
    parser.add_argument("--allow-gaps", action="store_true")
    args = parser.parse_args(argv)

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    days = [item.strip() for item in args.days.split(",") if item.strip()]
    try:
        rows = load_rows(args.input)
        artifact = build_set(
            rows,
            args.set_id,
            args.pack,
            args.trust,
            args.coverage_start,
            args.coverage_end,
            symbols,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    missing = gap_report(artifact["entries"], symbols, days) if days else []
    report = {
        "price_set_id": artifact["price_set_id"],
        "entries": len(artifact["entries"]),
        "symbols": symbols,
        "days": days,
        "missing": missing,
        "source_digest": artifact["source_digest"],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if missing and not args.allow_gaps:
        print(
            f"error: price_set_has_gaps:{len(missing)} (use --allow-gaps to write anyway)",
            file=sys.stderr,
        )
        return 3

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {args.out} entries={len(artifact['entries'])} missing={len(missing)} "
        f"source_digest={artifact['source_digest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
