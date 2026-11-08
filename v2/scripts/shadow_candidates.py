#!/usr/bin/env python3
"""Shadow-run a candidate artifact against the local service.

Creates an experiment with the candidate artifact, imports the quote evidence its
signals need (from the frozen research input), submits every frozen signal, and
prints a compact result. No P&L: this only proves wiring and records outcomes.

Run from v2/ with the service on 127.0.0.1:8765:
    python scripts/shadow_candidates.py --artifact cands/X.artifact.json \
        --output cands/X.output.json --cycle shadow-1
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "research-input-offline-v1.json"
DEFAULT_PACK = "cn-offline-fixture-pack-1"
DEFAULT_POLICY = "v2-paper-policy-6"


def call(api: str, path: str, payload=None, expected=200):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        api.rstrip("/") + "/v2/" + path, data, {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == expected, response.status
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read()
        assert error.code == expected, (path, error.code, body)
        return body


def quote_payload(entry: dict, side: str, pack: str) -> dict:
    quote = dict(entry["quote"])
    direction = entry[side]
    return {
        "schema_version": 2,
        "market_data_pack_id": pack,
        "available_at": entry["available_at"],
        "quote": quote,
        "execution": {
            "schema_version": 2,
            "quote_ref": {
                "market_data_pack_id": pack,
                "source": quote["source"],
                "quote_id": quote["quote_id"],
            },
            "order_side": side,
            "price_cents": direction["price_cents"],
            "available_quantity": direction["available_quantity"],
            "observed_at": quote["observed_at"],
        },
    }


def run(api: str, artifact: dict, output: dict, cycle: str, cash: int, pack: str, policy: str) -> dict:
    call(
        api,
        "experiments",
        {
            "schema_version": 1,
            "cycle_id": cycle,
            "initial_cash_cents": cash,
            "strategy": artifact,
            "market_data_pack_id": pack,
            "execution_policy_version": policy,
        },
    )
    entries = {
        entry["quote"]["quote_id"]: entry
        for entry in json.loads(DEFAULT_INPUT.read_text(encoding="utf-8"))["entries"]
    }
    outcomes = []
    for signal in output["signals"]:
        entry = entries.get(signal["quote_id"])
        if entry is None:
            raise SystemExit(f"missing_frozen_entry:{signal['quote_id']}")
        payload = quote_payload(entry, signal["side"], pack)
        call(api, "quotes", payload)  # tolerant: duplicate identity returns stored
        record = call(
            api,
            "simulations",
            {
                "schema_version": 4,
                "cycle_id": cycle,
                "strategy_version_id": artifact["strategy_version_id"],
                "signal_id": signal["signal_id"],
                "decision_at": signal["decision_at"],
                "signal": {"symbol": signal["symbol"], "side": signal["side"]},
                "quote_ref": {
                    "market_data_pack_id": pack,
                    "source": signal["source"],
                    "quote_id": signal["quote_id"],
                },
            },
        )
        outcomes.append(record["outcome"]["status"])
    portfolio = call(api, f"portfolio?cycle_id={cycle}")
    return {
        "cycle_id": cycle,
        "strategy_version_id": artifact["strategy_version_id"],
        "signals": len(output["signals"]),
        "filled": outcomes.count("filled"),
        "rejected": outcomes.count("rejected"),
        "runs_total": portfolio["runs_total"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--cash", type=int, default=10_000_000)
    parser.add_argument("--pack", default=DEFAULT_PACK)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    args = parser.parse_args(argv)
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    output = json.loads(args.output.read_text(encoding="utf-8"))
    try:
        result = run(args.api, artifact, output, args.cycle, args.cash, args.pack, args.policy)
    except (AssertionError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
