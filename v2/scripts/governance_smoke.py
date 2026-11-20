#!/usr/bin/env python3
"""Isolated check of the research namespace and its governance.

Boots a real kernel on a temporary database, walks one candidate through the
gates — including a refusal — and confirms the funded account never moves.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def call(base: str, path: str, body: dict | None = None):
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=payload, method="POST" if body else "GET",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


@contextmanager
def server(binary: Path, work: Path):
    db = work / "gov.sqlite3"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(ASTOCK_V2_DB=str(db), ASTOCK_V2_ADDR=f"127.0.0.1:{port}",
               ASTOCK_PRICE_SETS_DIR=str(work / "prices"))
    env.pop("ASTOCK_V2_TOKEN", None)
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
            raise AssertionError("readiness timed out")
        yield base, db
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    parent = Path(__file__).resolve().parents[1] / "work"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gov-", dir=parent, ignore_cleanup_errors=True) as tmp:
        work = Path(tmp).resolve()
        with server(binary, work) as (base, db):
            account_before = call(base, "/v2/account") if _exists(base) else None
            call(base, "/v2/account", {"initial_cash_cents": 10_000_000})
            before = call(base, "/v2/account")
            health = call(base, "/v2/health")
            assert health["fill_timing"] in ("same-close", "next-session")

            # No candidates yet, and no auth gate.
            assert call(base, "/v2/research/candidates") == []

            # Walk one candidate. Promotion is done directly through the store
            # contract in tests; here we prove the read model and the refusal
            # path are visible over HTTP without touching the account.
            with __import__("sqlite3").connect(db) as conn:
                conn.execute(
                    "INSERT INTO shadow_candidate(candidate_id,algorithm_id,algorithm_version,"
                    "state,created_day,updated_day,evidence_json) VALUES(?,?,?,?,?,?,?)",
                    ("probe-1", "momentum-reversal-v1", "v1", "shadow",
                     "2026-11-10", "2026-11-10",
                     json.dumps({"shadow_days": 40, "rank_ic_bps": 500, "ir_bps": 600,
                                 "tuned_on_same_window": False, "ran_clean": True})),
                )
                conn.execute(
                    "INSERT INTO shadow_state_event(candidate_id,from_state,to_state,day,"
                    "allowed,reasons_json,shortfalls_json) VALUES(?,?,?,?,?,?,?)",
                    ("probe-1", "shadow", "eligible", "2026-11-10", 0,
                     json.dumps(["shadow days 3 < required 20"]),
                     json.dumps([{"gate": "shadow_days", "required": 20, "actual": 3}])),
                )
                conn.commit()

            listed = call(base, "/v2/research/candidates")
            assert len(listed) == 1 and listed[0]["candidate_id"] == "probe-1"
            assert listed[0]["state"] == "shadow"

            history = call(base, "/v2/research/candidates/probe-1/history")
            assert len(history) == 1
            assert history[0]["allowed"] is False
            assert history[0]["shortfalls"][0]["gate"] == "shadow_days"

            # An unknown candidate is a 404, not an empty history that looks
            # like "no refusals recorded".
            try:
                call(base, "/v2/research/candidates/nope/history")
                raise AssertionError("unknown candidate should 404")
            except urllib.error.HTTPError as error:
                assert error.code == 404

            after = call(base, "/v2/account")
            assert after == before, "research namespace must not change the funded account"
            print("PASS candidates listed, refusal history served, unknown candidate 404")
            print("PASS funded account unchanged:", before["cash_cents"], "cents cash")
            print("PASS no authorization header required")
    print("governance smoke passed")
    return 0


def _exists(base: str) -> bool:
    try:
        call(base, "/v2/account")
        return True
    except urllib.error.HTTPError as error:
        return error.code != 404


if __name__ == "__main__":
    raise SystemExit(main())
