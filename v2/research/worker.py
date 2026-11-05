"""V2 offline research producer. Never opens or writes the trading ledger.

Example:
    python3 v2/research/worker.py --output /tmp/v2-signal.json
    python3 v2/research/worker.py --output /tmp/v2-signal.json --send

The output is a versioned proposal. Only the Rust kernel decides trade
eligibility, sizing, fees, lot ownership, and durable state changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import urllib.request

CN_TZ = dt.timezone(dt.timedelta(hours=8))


def build_fixture_proposal() -> dict:
    """Fixed, offline example. Never fetch real-time market data implicitly."""
    day = dt.datetime(2026, 10, 30, 10, 0, tzinfo=CN_TZ)
    return {
        "schema_version": 2,
        "cycle_id": "offline-fixture-cycle",
        "strategy_version_id": "research-fixture-v1",
        "signal_id": "fixture-buy-600000-20261030",
        "decision_at": day.isoformat(timespec="seconds"),
        "signal": {"symbol": "600000.SH", "side": "buy"},
        "quote_ref": {
            "source": "offline-fixture",
            "quote_id": "fixture-quote-20261030-1000",
        },
    }


def emit(proposal: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("v2-signal.json"))
    parser.add_argument("--send", action="store_true",
                        help="Explicitly POST fixture to localhost Rust paper gateway")
    args = parser.parse_args()
    proposal = build_fixture_proposal()
    emit(proposal, args.output)
    print(f"Wrote paper-only, offline research proposal: {args.output}")
    if args.send:
        body = json.dumps(proposal).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8765/v2/simulations",
            body, {"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            print(response.read().decode())


if __name__ == "__main__":
    main()
