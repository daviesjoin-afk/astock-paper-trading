#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transport-only realtime snapshot feed for the intraday Rust kernel.

This module converts an explicit universe file into the ``Quote`` JSON array
accepted by ``POST /v2/live/round``. It owns no trading rule, no eligibility
judgement and no risk gate. Missing facts are omitted, never substituted:
``avg_volume`` is deliberately never emitted because ``tencent_quote`` has no
explicit trailing average-volume series, and ``vol_ratio`` is not that series.

The local clock only supplies ``fetched_at`` and, when the upstream snapshot has
no observation time, an explicitly flagged *estimated* ``quote_time``. It never
supplies a price, a trading day, or a market-state decision.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time as time_cls, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable, Mapping, Sequence

# Make ``astock_public`` / ``source_http`` importable both from v2/ and repo root.
SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_SOURCES_DIR = SCRIPTS_DIR.parent / "data_sources"
if str(DATA_SOURCES_DIR) not in sys.path:
    sys.path.insert(0, str(DATA_SOURCES_DIR))

DEFAULT_UNIVERSE = SCRIPTS_DIR.parent / "work" / "live-a-universe.txt"
# Codes per Tencent snapshot request. Measured on the aarch64 server
# (2026-11-16, 5 219 symbols, 3-minute rounds) a full-market fetch costs:
#   batch=200 -> 27 requests, 34.2s   (the old default)
#   batch=400 -> 14 requests, 22.6s
#   batch=800 ->  7 requests, 13.9s   <- chosen
# The upstream caps a request at 800 codes (1000 returns HTTP 414), so this is
# the largest safe batch; the per-request round-trip, not parsing, dominates.
DEFAULT_BATCH_SIZE = 800
DEFAULT_HISTORY_DAYS = 20
SOURCE_NAME = "tencent_quote"
SYMBOL_LENGTH = 6
SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_LLM_BASE = ""  # Set ASTOCK_LLM_BASE for the deployment.
DEFAULT_LLM_MODEL = "deepseek-v4.1-flash"

# Yuan -> integer cents. Tencent's amount_wan is in 10,000 yuan.
YUAN_TO_CENTS = 100
WAN_TO_YUAN = 10_000
AMOUNT_WAN_TO_CENTS = WAN_TO_YUAN * YUAN_TO_CENTS

PRICE_FIELDS = (
    ("price", "price_cents"),
    ("last_close", "prev_close_cents"),
    ("open", "open_cents"),
    ("high", "high_cents"),
    ("low", "low_cents"),
    ("limit_up", "limit_up_cents"),
    ("limit_down", "limit_down_cents"),
)


class FeedError(Exception):
    """A transport failure with a concrete, human-readable cause."""


def utc_now_iso(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def finite_number(value: Any) -> float | None:
    """Parse one finite source number; None means absent, never zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_cents(value: Any, *, positive_only: bool = True) -> int | None:
    """``int(round(float(v)*100))`` for a source field in yuan."""
    number = finite_number(value)
    if number is None or (positive_only and number <= 0):
        return None
    cents = int(round(number * YUAN_TO_CENTS))
    if positive_only and cents <= 0:
        return None
    return cents


def integral_value(value: Any) -> int | None:
    """Convert an integral source count; None means absent, not zero."""
    number = finite_number(value)
    if number is None or number <= 0 or number != int(number):
        return None
    return int(number)


def symbol_from_universe(line: str) -> str | None:
    """``600000.SH`` -> ``600000.SH``; blank/comment lines are not data."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if len(text) != 9 or text[6] != "." or not text[:6].isdigit():
        raise FeedError(f"invalid universe symbol: {text!r}")
    return text.upper()


def tencent_code(symbol: str) -> str:
    """``600000.SH`` -> ``sh600000``; the suffix is always authoritative."""
    code, suffix = symbol[:SYMBOL_LENGTH], symbol[7:].lower()
    if suffix not in ("sh", "sz", "bj"):
        raise FeedError(f"unsupported universe suffix: {symbol!r}")
    return suffix + code


def load_universe(path: str | Path) -> list[str]:
    file = Path(path)
    try:
        lines = file.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise FeedError(f"cannot read universe {file}: {error}") from error
    symbols: list[str] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, 1):
        try:
            symbol = symbol_from_universe(line)
        except FeedError as error:
            raise FeedError(f"{file}:{number}: {error}") from error
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    if not symbols:
        raise FeedError(f"universe is empty: {file}")
    return symbols


def load_history(path: str | Path | None, *, days: int = DEFAULT_HISTORY_DAYS,
                 as_of_day: str | None = None) -> dict[str, list[int]]:
    """Read *strictly before* the snapshot day from an optional sqlite ``bar``.

    A missing file, missing table, unreadable database or too-short series all
    resolve to an empty list. No current price is ever used as historical data.
    ``as_of_day`` must be derived from the source observation day, not from the
    local wall clock.
    """
    if not path or days <= 0:
        return {}
    db = Path(path)
    if not db.is_file():
        return {}
    if db.suffix.lower() == ".json":
        # A compact, real historical close file for deployment on machines that
        # do not carry the full research database. It is still point-in-time:
        # if its declared through_day is not strictly before the snapshot day,
        # it is refused rather than silently treated as current history.
        try:
            with db.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            through = str(payload.get("through_day", ""))
            if as_of_day and through and through[:10] >= as_of_day[:10]:
                return {}
            raw = payload.get("per_symbol", {})
            if not isinstance(raw, dict):
                return {}
            series: dict[str, list[int]] = {}
            for symbol, values in raw.items():
                if not isinstance(values, list):
                    continue
                window = values[-days:]
                if len(window) >= 5 and all(
                    isinstance(value, int) and value > 0 for value in window
                ):
                    series[str(symbol)] = [int(value) for value in window]
            return series
        except (OSError, ValueError, TypeError, AttributeError):
            return {}
    query = ("SELECT symbol, day, close_cents FROM bar "
             "WHERE close_cents > 0")
    params: tuple[Any, ...] = ()
    if as_of_day:
        query += " AND day < ?"
        params = (as_of_day,)
    query += " ORDER BY symbol ASC, day ASC"
    try:
        uri = "file:" + db.resolve().as_posix() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = connection.execute(query, params)
            distinct_days: list[str] = []
            by_day: dict[tuple[str, str], int] = {}
            for symbol, day, close in rows:
                text_day = str(day)[:10]
                if text_day not in distinct_days:
                    distinct_days.append(text_day)
                by_day[(str(symbol), text_day)] = int(close)
            # Only the last N observed trading days for which this database
            # actually has a market-wide bar set. A symbol missing from one of
            # those days is missing history, not a shorter fabricated series.
            window = distinct_days[-days:]
            series: dict[str, list[int]] = {}
            for symbol in {key[0] for key in by_day}:
                values = [by_day.get((symbol, day)) for day in window]
                if all(value is not None for value in values):
                    series[symbol] = [int(value) for value in values]
            return series
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {}


def source_datetime(value: Any) -> datetime | None:
    """Parse a source observation datetime, assuming Asia/Shanghai when naive.

    Tencent's ``YYYY-MM-DD HH:MM:SS`` is Beijing time; an explicit ISO offset
    is preserved. Time-only strings cannot name a trading day and are rejected.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 16 or text[10] not in "T " or text[13] != ":":
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI)


def fetch_snapshots(symbols: Sequence[str], *, batch_size: int = DEFAULT_BATCH_SIZE,
                    client: Any | None = None) -> dict[str, Mapping[str, Any]]:
    """Fetch all batches through PublicClient and merge their returned dicts."""
    if batch_size <= 0:
        raise FeedError("batch size must be positive")
    if client is None:
        try:
            from astock_public import PublicClient  # noqa: PLC0415 - optional heavy import
            from source_http import SourceHttp  # noqa: PLC0415
        except ImportError as error:
            raise FeedError(f"public data layer unavailable: {error}") from error
        try:
            client = PublicClient(http=SourceHttp())
        except Exception as error:  # noqa: BLE001 - construction failure is feed failure
            raise FeedError(f"cannot construct PublicClient: {error}") from error
    combined: dict[str, Mapping[str, Any]] = {}
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset:offset + batch_size]
        codes = [tencent_code(symbol) for symbol in batch]
        try:
            result = client.call(SOURCE_NAME, codes)
        except Exception as error:  # noqa: BLE001 - report the exact transport failure
            raise FeedError(
                f"tencent_quote batch {offset // batch_size + 1} failed: "
                f"{type(error).__name__}: {error}"
            ) from error
        if not isinstance(result, Mapping):
            raise FeedError(
                f"tencent_quote batch {offset // batch_size + 1} returned "
                f"{type(result).__name__}, expected dict"
            )
        for key, row in result.items():
            if isinstance(row, Mapping):
                combined[str(key)] = row
    return combined


def quote_from_snapshot(symbol: str, row: Mapping[str, Any] | None,
                        *, history: Sequence[int] = ()) -> dict[str, Any] | None:
    """Map one source row to Rust ``Quote``; return None when it has no live price."""
    if not isinstance(row, Mapping):
        return None
    price_cents = as_cents(row.get("price"))
    prev_close_cents = as_cents(row.get("last_close"))
    # Rust requires both a live last price and a previous close; neither can be
    # substituted by the other, by zero, or by a historical bar.
    if price_cents is None or prev_close_cents is None:
        return None
    quote: dict[str, Any] = {
        "symbol": symbol,
        "price_cents": price_cents,
        "prev_close_cents": prev_close_cents,
        "trailing_closes": [int(value) for value in history],
        "tradable": row.get("is_stale") is not True,
    }
    # The source snapshot owns the display name. Blanks and upstream
    # placeholders are absent facts, never a name to show a human.
    name = row.get("name")
    if isinstance(name, str) and name.strip() and name.strip() not in ("-", "None"):
        quote["name"] = name.strip()
    for source_name, target in PRICE_FIELDS[2:]:
        converted = as_cents(row.get(source_name))
        if converted is not None:
            quote[target] = converted
    amount_wan = finite_number(row.get("amount_wan"))
    if amount_wan is not None and amount_wan > 0:
        quote["amount_cents"] = int(round(amount_wan * AMOUNT_WAN_TO_CENTS))
    volume = integral_value(row.get("volume"))
    if volume is not None:
        quote["volume"] = volume
    # No avg_volume: vol_ratio/turnover/volume are not an explicit trailing
    # average-volume series, and Rust treats an absent field as an absent factor.
    return quote


def build_snapshot(universe_path: str | Path = DEFAULT_UNIVERSE, *,
                   batch_size: int = DEFAULT_BATCH_SIZE,
                   history_db: str | Path | None = None,
                   history_days: int = DEFAULT_HISTORY_DAYS,
                   client: Any | None = None,
                   clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Return ``{"quotes": [...], "quote_time": ..., ...}`` with no fabrication."""
    symbols = load_universe(universe_path)
    rows = fetch_snapshots(symbols, batch_size=batch_size, client=client)
    fetched_at = utc_now_iso(clock)
    source_times = [source_datetime(row.get("quote_time"))
                    for row in rows.values() if isinstance(row, Mapping)]
    source_times = [value for value in source_times if value is not None]
    observed = min(source_times) if source_times else None
    estimated = observed is None
    quote_time = observed.isoformat() if observed else fetched_at

    # The history cutoff comes from the source observation day, never from the
    # clock used only for fetched_at / an explicitly flagged fallback.
    # When the source provides no quote_time, the fallback clock is explicitly
    # flagged as estimated; use its Shanghai calendar day only as the PIT
    # cutoff for history, never as a claim about the source's observation time.
    cutoff_clock = observed or datetime.fromtimestamp(clock(), tz=timezone.utc)
    as_of_day = cutoff_clock.astimezone(SHANGHAI).date().isoformat()
    history = load_history(history_db, days=history_days, as_of_day=as_of_day)

    quotes: list[dict[str, Any]] = []
    for symbol in symbols:
        row = rows.get(tencent_code(symbol))
        if row is None:
            row = rows.get(symbol)
        if row is None:
            row = rows.get(symbol[:SYMBOL_LENGTH])
        if row is None:
            continue
        quote = quote_from_snapshot(symbol, row,
                                    history=history.get(symbol, ()))
        if quote is not None:
            quotes.append(quote)

    result: dict[str, Any] = {
        "quotes": quotes,
        "quote_time": quote_time,
        "fetched_at": fetched_at,
        "count": len(quotes),
        "source": SOURCE_NAME,
    }
    if estimated:
        result["quote_time_estimated"] = True
    return result


def write_json(path: str | Path | None, payload: Mapping[str, Any]) -> None:
    """UTF-8 JSON with LF newlines, including on Windows."""
    data = (json.dumps(payload, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")
    if path is None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as error:
        raise FeedError(f"cannot write {target}: {error}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE),
                        help="one SYMBOL.SH/SZ/BJ per line")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH_SIZE,
                        help="codes per tencent_quote call (default 200)")
    parser.add_argument("--out", default=None,
                        help="output path (default: stdout as UTF-8 bytes)")
    parser.add_argument("--history-db",
                        default=os.environ.get("ASTOCK_HISTORY_DB"),
                        help="optional sqlite with bar(symbol, day, close_cents); "
                             "ASTOCK_HISTORY_DB is used when the flag is omitted")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_snapshot(
            args.universe,
            batch_size=args.batch,
            history_db=args.history_db,
        )
        write_json(args.out, payload)
    except FeedError as error:
        print(f"live_quote_feed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - CLI must report, not traceback-only
        print(f"live_quote_feed: unexpected {type(error).__name__}: {error}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
