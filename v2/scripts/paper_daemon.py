"""Let the paper account run itself: ingest new days, settle them, repeat.

Why a daemon instead of me running it: a replay that I start and stop is a
backtest with extra steps. The point of the paper account is that it advances on
its own schedule, taking whatever the market published and nothing else, so the
record it accumulates is not shaped by when I happened to be watching.

The cycle, every `--interval` seconds:

1. Ask the data source whether a trading day newer than the newest we hold has
   been published. A weekend or a not-yet-published package is a normal empty
   answer, not an error.
2. Ingest those days into the bar table (idempotent; re-ingesting is harmless).
3. Ask the kernel to settle every day it now holds but has not settled.
4. Log what happened, then sleep.

What it deliberately does NOT do: invent a day, infer a price, or settle a day
whose bar package is missing. A gap in the record is honest; a filled gap is
fabrication.

The wall clock is used only to decide *when to look*, never *what to settle*.
The days come from the data source.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/data_sources")


def log(message: str) -> None:
    print(f"{datetime.datetime.now():%H:%M:%S} {message}", flush=True)


def http_json(url: str, payload: dict | None = None, timeout: float = 60.0):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def held_days(db: str) -> list[str]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        rows = connection.execute("SELECT DISTINCT day FROM bar ORDER BY day").fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def candidate_days(last_held: str | None, lookback: int = 12) -> list[str]:
    """Weekdays after `last_held`, oldest first.

    Only weekdays: an A-share session cannot fall on a Saturday. Public holidays
    are not enumerated here on purpose -- the data source is asked about each
    candidate day and answers "no package", which is more reliable than a
    hard-coded calendar that goes stale.
    """
    if last_held is None:
        return []
    start = datetime.date.fromisoformat(last_held) + datetime.timedelta(days=1)
    today = datetime.date.today()
    days = []
    cursor = start
    while cursor <= today and len(days) < lookback:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += datetime.timedelta(days=1)
    return days


def ingest(db: str, day: str) -> tuple[bool, str]:
    """Fetch and store one day. Returns (stored, note)."""
    from astock_public import PublicClient  # noqa: PLC0415 - heavy import
    from source_http import SourceHttp  # noqa: PLC0415

    cache = os.path.join(os.path.dirname(db), "cache")
    client = PublicClient(http=SourceHttp(cache))
    try:
        result = client.query("bars.tdx_daily_package", day, as_json=True)
        rows = result["result"]["rows"]
    except Exception as error:  # noqa: BLE001 - any failure means "no day today"
        return False, f"{type(error).__name__}: {str(error)[:60]}"

    connection = sqlite3.connect(db, timeout=120)
    connection.execute("PRAGMA journal_mode=WAL")
    batch = []
    for row in rows:
        code = str(row.get("code", ""))
        market = str(row.get("market", ""))
        if not code.isdigit() or len(code) != 6:
            continue
        suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(market)
        if not suffix:
            continue
        if suffix == "SH" and code.startswith(("000", "999")):
            continue
        if suffix == "SZ" and code.startswith("399",):
            continue
        if suffix == "BJ" and not code.startswith(("8", "4", "9")):
            continue

        def cents(value) -> int:
            return 0 if value is None else int(round(float(value) * 100))

        close = cents(row.get("close"))
        if close <= 0:
            continue
        volume = int(row.get("volume") or 0)
        if not (suffix == "SH" and code.startswith("688")):
            volume *= 100
        batch.append((
            f"{code}.{suffix}", day, str(row.get("name") or ""),
            cents(row.get("open")), cents(row.get("high")), cents(row.get("low")),
            close, cents(row.get("prev_close")), volume,
            int(float(row.get("amount") or 0) * 100), "none", "tdx", 1,
        ))
    if not batch:
        connection.close()
        return False, "package had no usable rows"
    connection.executemany("REPLACE INTO bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    connection.commit()
    connection.close()
    return True, f"{len(batch)} rows"


def settle(api: str, day: str) -> tuple[int, str]:
    try:
        payload = http_json(api.rstrip("/") + "/v2/account/run", {"day": day}, timeout=900)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:160]
        return error.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, f"{type(error).__name__}"
    trades = payload.get("trades", [])
    account = payload.get("account", {})
    return 200, (f"trades={len(trades)} equity={account.get('equity_cents')} "
                 f"ret_bps={account.get('return_bps')}")


def one_cycle(args) -> int:
    days = held_days(args.db)
    last_held = days[-1] if days else None

    added = 0
    for day in candidate_days(last_held):
        stored, note = ingest(args.db, day)
        if stored:
            log(f"ingest {day}: {note}")
            added += 1
        elif "没有通达信盘后包" in note:
            log(f"ingest {day}: not published")
            # The most recent unpublished weekday is the frontier; older ones
            # are holidays or missed days. Either way, stop probing forward.
            break
        else:
            log(f"ingest {day}: {note}")

    if args.no_settle:
        return added

    # Settle every held day the account has not reached yet.
    try:
        account = http_json(args.api.rstrip("/") + "/v2/account", timeout=30)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            log("no account yet; create one with POST /v2/account")
            return added
        raise
    settled_through = account.get("last_settled_day") or ""
    days = held_days(args.db)
    pending = [day for day in days if day > settled_through]
    if not pending:
        log(f"nothing to settle (last={settled_through or 'none'})")
        return added

    for day in pending:
        code, note = settle(args.api, day)
        if code != 200:
            log(f"settle {day}: http={code} {note}")
            break
        log(f"settle {day}: {note}")
    return added


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    parser.add_argument("--interval", type=float, default=900.0,
                        help="seconds between checks (default 15 min)")
    parser.add_argument("--once", action="store_true", help="one cycle then exit")
    parser.add_argument("--no-settle", action="store_true",
                        help="only ingest, do not advance the account")
    args = parser.parse_args(argv)

    if args.once:
        one_cycle(args)
        return 0
    log(f"daemon start db={args.db} api={args.api} interval={args.interval:.0f}s")
    while True:
        try:
            one_cycle(args)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except Exception as error:  # noqa: BLE001 - a daemon must not die on one bad cycle
            log(f"cycle failed: {type(error).__name__}: {str(error)[:120]}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
