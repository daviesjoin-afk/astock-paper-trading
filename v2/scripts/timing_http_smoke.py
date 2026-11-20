#!/usr/bin/env python3
"""Isolated HTTP checks for persisted execution timing; no active DB is used."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
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


def request(base: str, path: str, body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def environment(db: Path, work: Path, timing: str | None) -> tuple[dict, str]:
    env = os.environ.copy()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(ASTOCK_V2_DB=str(db), ASTOCK_V2_ADDR=f"127.0.0.1:{port}", ASTOCK_PRICE_SETS_DIR=str(work / "prices"))
    env.pop("ASTOCK_V2_FILLS", None)
    env.pop("ASTOCK_ALLOW_SYNTHETIC", None)
    if timing is not None:
        env["ASTOCK_V2_FILLS"] = timing
    return env, f"http://127.0.0.1:{port}"


@contextmanager
def server(binary: Path, db: Path, work: Path, timing: str | None):
    env, base = environment(db, work, timing)
    process = subprocess.Popen([str(binary)], env=env, cwd=binary.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("isolated service exited before readiness")
            try:
                request(base, "/v2/health")
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


def raw_spec(db: Path) -> str:
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT spec_json FROM account").fetchone()[0]


def rejects_startup(binary: Path, db: Path, work: Path, timing: str, code: str) -> None:
    env, _ = environment(db, work, timing)
    process = subprocess.Popen([str(binary)], env=env, cwd=binary.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, errors = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError("conflicting timing was allowed to start")
    assert process.returncode != 0 and code in output + errors, "missing timing configuration rejection"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    parent = Path(__file__).resolve().parents[1] / "work"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="timing-http-", dir=parent) as temporary:
        work = Path(temporary).resolve()
        db = work / "next.sqlite3"
        with server(binary, db, work, "next-session") as base:
            assert request(base, "/v2/health")["fill_timing"] == "next-session"
            request(base, "/v2/account", {"initial_cash_cents": 10000000})
            assert request(base, "/v2/health")["fill_timing_pinned"] is True
        before = raw_spec(db)
        assert json.loads(before)["fill_timing"] == "next-session"
        with server(binary, db, work, None) as base:
            assert request(base, "/v2/health")["fill_timing"] == "next-session"
        assert raw_spec(db) == before
        rejects_startup(binary, db, work, "same-close", "account_fill_timing_conflict")
        assert raw_spec(db) == before
        print("PASS new account pins next-session; restart restores it; conflicting startup rejected without metadata rewrite")

        legacy_db = work / "legacy.sqlite3"
        with server(binary, legacy_db, work, None) as base:
            assert request(base, "/v2/health")["fill_timing"] == "same-close"
            request(base, "/v2/account", {"initial_cash_cents": 10000000})
        # Create an explicit historical-format fixture only after its server stopped.
        with closing(sqlite3.connect(legacy_db)) as conn:
            spec = json.loads(conn.execute("SELECT spec_json FROM account").fetchone()[0])
            spec.pop("fill_timing")
            conn.execute("UPDATE account SET spec_json=?", (json.dumps(spec),))
            conn.commit()
        legacy_before = raw_spec(legacy_db)
        with server(binary, legacy_db, work, None) as base:
            assert request(base, "/v2/health")["fill_timing"] == "same-close"
        rejects_startup(binary, legacy_db, work, "next-session", "account_fill_timing_conflict")
        assert raw_spec(legacy_db) == legacy_before
        print("PASS historical account retains same-close without rewriting; next-session switch rejected")
        rejects_startup(binary, db, work, "unknown", "unsupported_fill_timing")
        print("PASS unknown fill timing rejected")
    print("timing HTTP smoke passed; only isolated databases and processes used")


if __name__ == "__main__":
    main()
