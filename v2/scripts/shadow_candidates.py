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


def performance(api: str, cycle: str, days: list[str]) -> dict | None:
    """Reviewed-series performance, or None when coverage is missing."""
    report = call(api, f"experiments/{cycle}/performance?days={','.join(days)}")
    if isinstance(report, dict) and report.get("status") == "unavailable":
        return None
    return report


def metrics_from_performance(report: dict) -> dict:
    return {
        "candidate_id": report["cycle_id"],
        "out_of_sample_days": report["days"],
        "shadow_days": report["days"],
        "trades": report["trades"],
        "return_bps": report["return_bps"],
        "drawdown_bps": report["drawdown_bps"],
    }


def decide(
    api: str,
    base_cycle: str,
    candidate_cycle: str,
    days: list[str],
    *,
    record_event: bool = False,
    event_id: str | None = None,
    decided_at: str | None = None,
    policy: dict | None = None,
) -> dict:
    """Compare two experiments on the same reviewed days and optionally decide.

    Without coverage on both sides the result is `unavailable`: no decision is
    invented from partial metrics.
    """
    base = performance(api, base_cycle, days)
    candidate = performance(api, candidate_cycle, days)
    if base is None or candidate is None:
        return {
            "status": "unavailable",
            "reason": "no_reviewed_price_set",
            "base_available": base is not None,
            "candidate_available": candidate is not None,
        }
    payload = {
        "kind": "promotion",
        "incumbent": metrics_from_performance(base),
        "candidate": metrics_from_performance(candidate),
    }
    if policy is not None:
        payload["policy"] = policy
    decision = call(api, "evolution/decisions", payload)
    event = None
    if record_event:
        if not decided_at:
            raise ValueError("record_event_requires_decided_at")
        event_payload = dict(payload)
        event_payload["event_id"] = event_id or f"shadow-{candidate_cycle}"
        event_payload["decided_at"] = decided_at
        event = call(api, "evolution/events", event_payload)
    return {
        "status": "decided",
        "base": base,
        "candidate": candidate,
        "decision": decision,
        "event": event,
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
    parser.add_argument("--base-cycle")
    parser.add_argument("--performance-days", default="")
    parser.add_argument("--decide", action="store_true")
    parser.add_argument("--record-event", action="store_true")
    parser.add_argument("--event-id")
    parser.add_argument("--decided-at")
    args = parser.parse_args(argv)
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    output = json.loads(args.output.read_text(encoding="utf-8"))
    try:
        result = run(args.api, artifact, output, args.cycle, args.cash, args.pack, args.policy)
    except (AssertionError, KeyError, ValueError, SystemExit) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    days = [day.strip() for day in args.performance_days.split(",") if day.strip()]
    if days and args.base_cycle:
        result["performance"] = {
            "base": performance(args.api, args.base_cycle, days),
            "candidate": performance(args.api, args.cycle, days),
        }
    if args.decide and args.base_cycle and days:
        result["decision"] = decide(
            args.api,
            args.base_cycle,
            args.cycle,
            days,
            record_event=args.record_event,
            event_id=args.event_id,
            decided_at=args.decided_at,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
