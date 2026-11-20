#!/usr/bin/env python3
"""Isolated HTTP check: blocked orders are served, repeat runs are idempotent.

Builds a next-session account on synthetic bars, seals one held name on the
fill day, then verifies the decision-fixed sale is reported as blocked rather
than silently dropped. Only temporary databases and processes are used.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import date, timedelta
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

SYMBOLS = ["600000.SH", "000001.SZ"]


def call(base: str, path: str, body: dict | None = None, method: str | None = None):
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=payload, method=method,
                                    headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def trading_days(count: int) -> list[str]:
    day = date(2026, 7, 1)
    out: list[str] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


@contextmanager
def server(binary: Path, db: Path, work: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(ASTOCK_V2_DB=str(db), ASTOCK_V2_ADDR=f"127.0.0.1:{port}",
               ASTOCK_PRICE_SETS_DIR=str(work / "prices"), ASTOCK_V2_FILLS="next-session")
    env.pop("ASTOCK_ALLOW_SYNTHETIC", None)
    process = subprocess.Popen([str(binary)], env=env, cwd=binary.parent,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise AssertionError("isolated service exited before readiness")
            try:
                call(base, "/v2/health")
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            raise AssertionError("isolated service readiness timed out")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def seed(binary: Path, db: Path, work: Path) -> None:
    # The kernel owns the schema; create it through one short-lived process.
    with server(binary, db, work):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    parent = Path(__file__).resolve().parents[1] / "work"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="intent-http-", dir=parent, ignore_cleanup_errors=True) as temporary:
        work = Path(temporary).resolve()
        db = work / "intent.sqlite3"
        seed(binary, db, work)
        days = trading_days(75)
        rows = []
        for symbol, base_price in zip(SYMBOLS, (1000, 1200)):
            for index, day in enumerate(days):
                price = base_price + index * 3
                rows.append((symbol, day, price, price, price, price, price, 1000, price * 1000, 1))
        with closing(sqlite3.connect(db)) as conn:
            conn.executemany(
                "INSERT INTO bar(symbol,day,open_cents,high_cents,low_cents,close_cents,"
                "prev_close_cents,volume_shares,amount_cents,tradable) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()

        with server(binary, db, work) as base:
            assert call(base, "/v2/health")["fill_timing"] == "next-session"
            call(base, "/v2/account", {"initial_cash_cents": 10_000_000})
            # Settle through the second-to-last day: the crash below is then
            # decision evidence, while the seal is only visible on the fill day.
            call(base, "/v2/account/run", {"day": days[-2]})
            with closing(sqlite3.connect(db)) as conn:
                held = conn.execute(
                    "SELECT DISTINCT symbol FROM position ORDER BY symbol").fetchall()
                assert held, "fixture must hold a position before the sealed sale"
                symbols = [row[0] for row in held]
                # Every name crashes on the decision day, so the book wants to
                # leave its holdings; the fill day then seals the sell side.
                for symbol in symbols:
                    conn.execute(
                        "UPDATE bar SET close_cents=open_cents*800/1000,"
                        "low_cents=open_cents*800/1000,high_cents=open_cents*800/1000 "
                        "WHERE day=? AND symbol=?", (days[-2], symbol))
                for symbol in symbols:
                    reference = conn.execute(
                        "SELECT close_cents FROM bar WHERE day=? AND symbol=?",
                        (days[-2], symbol)).fetchone()[0]
                    sealed_price = reference * 900 // 1000
                    conn.execute(
                        "UPDATE bar SET close_cents=?,open_cents=?,high_cents=?,low_cents=?,"
                        "prev_close_cents=? WHERE day=? AND symbol=?",
                        (sealed_price, sealed_price, sealed_price, sealed_price,
                         reference, days[-1], symbol))
                conn.commit()
            run = call(base, "/v2/account/run", {"day": days[-1]})
            blocked = call(base, "/v2/account/blocked")
            assert isinstance(blocked, list), "blocked endpoint must return a list"
            keep = [item for item in blocked if item["blocked_day"] == days[-1]]
            assert keep, "the sealed sale must be recorded as blocked evidence"
            assert all(item["decision_day"] == days[-2] for item in keep)
            assert all(item["quantity"] % 100 == 0 for item in keep)
            trades_before = call(base, "/v2/account/trades")
            first = blocked
            # Re-advancing a settled day returns an empty day list and must not
            # create trades or evidence.
            repeat = call(base, "/v2/account/run", {"day": days[-1]})
            assert repeat.get("processed_days") == [], "a settled day must not be processed twice"
            assert len(call(base, "/v2/account/trades")) == len(trades_before), \
                "a repeat run must not duplicate trades"
            assert call(base, "/v2/account/blocked") == first, "a repeat run must not duplicate evidence"
            print(f"blocked_evidence={len(keep)} reasons={sorted({item['reason'] for item in keep})}")
            print("trades_on_fill_day=", len(run.get("trades") or []))
            print("PASS blocked orders are served, tied to their decision day, and idempotent")
    print("intent HTTP smoke passed; no active account or original database touched")


if __name__ == "__main__":
    main()
