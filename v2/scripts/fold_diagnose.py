#!/usr/bin/env python3
"""Diagnose why a fold underperformed the market it traded in.

The rolling folds showed a defensive shape: excess when the market falls,
given back when it rises. That is a finding, not yet an explanation. This reads
the ledger a fold produced and asks the questions the decision record can
actually answer:

* How often was entry even allowed? (`allow_entries` in each trade's basis)
* Was the book built to the intended size, or left short?
  (`target_weight_bps` vs the cash actually deployed)
* Did the regime filter call the market wrong in that window?
* How much did turnover cost? (fees against gross traded value)

Every number here comes from `trade.basis_json`, which is written at decision
time. Nothing is inferred from the outcome — that is the whole point of having
recorded the basis.

Usage:
    python scripts/fold_diagnose.py --replay fold-1.json --out diagnosis.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sqlite3


def load_from_replay(path: Path, variant: str = "filtered") -> list[dict]:
    """Read trades straight out of a `regime_ab` replay report.

    The replay already carries `basis` per trade, embedded at decision time.
    Reading it from there avoids needing a ledger file that the A/B run never
    writes.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get(variant) or {}
    trades = []
    for item in data.get("trades") or []:
        trades.append({
            "slot": item.get("slot"),
            "symbol": item.get("symbol"),
            "side": item.get("side"),
            "quantity": item.get("quantity") or 0,
            "price_cents": item.get("price_cents") or 0,
            "fees_cents": item.get("fees_cents") or 0,
            "day": item.get("trade_day"),
            "reason": item.get("reason"),
            "basis": item.get("basis") or {},
            "notional_cents": (item.get("quantity") or 0) * (item.get("price_cents") or 0),
        })
    return trades


def load_calendar(replay: Path) -> list[str] | None:
    """The settled days of the replay, which is the true trading calendar."""
    try:
        payload = json.loads(replay.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    windows = (payload.get("input") or {}).get("windows") or []
    days = [window.get("day") for window in windows if isinstance(window, dict)]
    return [day for day in days if day] or None


def load_from_ledger(path: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT slot,symbol,side,quantity,price_cents,fees_cents,trade_day,reason,basis_json "
            "FROM trade ORDER BY trade_day, id"
        ).fetchall()
    finally:
        conn.close()
    trades = []
    for slot, symbol, side, quantity, price_cents, fees, day, reason, basis_json in rows:
        try:
            basis = json.loads(basis_json) if basis_json else {}
        except json.JSONDecodeError:
            basis = {}
        trades.append({
            "slot": slot, "symbol": symbol, "side": side, "quantity": quantity,
            "price_cents": price_cents, "fees_cents": fees, "day": day,
            "reason": reason, "basis": basis,
            "notional_cents": quantity * price_cents,
        })
    return trades


def analyse(trades: list[dict], calendar: list[str] | None = None) -> dict:
    """Everything the decision record can speak to, and nothing it cannot."""
    if not trades:
        return {"trades": 0, "note": "no trades; nothing to diagnose"}

    entries = [t for t in trades if t["side"] == "buy"]
    exits = [t for t in trades if t["side"] == "sell"]

    allowed = [t for t in trades if t["basis"].get("allow_entries") is True]
    blocked = [t for t in trades if t["basis"].get("allow_entries") is False]

    target = [t["basis"].get("target_weight_bps") for t in entries
              if isinstance(t["basis"].get("target_weight_bps"), int)]
    scores = [t["basis"].get("score") for t in trades
              if isinstance(t["basis"].get("score"), int)]
    threshold = [t["basis"].get("entry_threshold") for t in trades
                 if isinstance(t["basis"].get("entry_threshold"), int)]
    regimes = Counter(t["basis"].get("regime") for t in trades
                      if t["basis"].get("regime") is not None)

    gross = sum(t["notional_cents"] for t in trades)
    fees = sum(t["fees_cents"] for t in trades)
    fee_bps = (fees * 10_000 // gross) if gross else 0

    # How much of the intended exposure was actually built. A target of 3000 bps
    # (30%) that is repeatedly only half filled means the book never carried the
    # risk the signal asked for — which in a rising market costs performance
    # without ever showing up as a losing trade.
    size_ratio = None
    if target and entries:
        per_slot: dict[str, list[int]] = {}
        for trade in entries:
            value = trade["basis"].get("target_weight_bps")
            if isinstance(value, int):
                per_slot.setdefault(trade["slot"], []).append(value)
        if per_slot:
            size_ratio = {
                slot: {"entries": len(values), "mean_target_bps": sum(values) // len(values)}
                for slot, values in per_slot.items()
            }

    # Holding period and exit reasons. A book that buys breakouts and exits on
    # the first pullback churns in a rising market: the signal is never given
    # time to work, and the cost is paid every cycle.
    exit_reasons = Counter(t["reason"] for t in exits)
    holding = []
    open_by_symbol: dict[str, list[str]] = {}
    # Holding period is measured over the *trading calendar*, not over the days
    # that happened to trade. Using traded days only would count a position
    # held across a quiet month as one session, which is exactly backwards.
    all_days = (sorted(calendar) if calendar
                else sorted({trade["day"] for trade in trades}))
    day_order = {day: position for position, day in enumerate(all_days)}
    for trade in trades:
        symbol = trade["symbol"]
        if trade["side"] == "buy":
            open_by_symbol.setdefault(symbol, []).append(trade["day"])
        elif trade["side"] == "sell" and open_by_symbol.get(symbol):
            bought = open_by_symbol[symbol].pop(0)
            if bought in day_order and trade["day"] in day_order:
                holding.append(day_order[trade["day"]] - day_order[bought])
    holding_stats = None
    if holding:
        holding.sort()
        holding_stats = {
            "closed": len(holding),
            "mean_days": round(sum(holding) / len(holding), 1),
            "median_days": holding[len(holding) // 2],
            "shortest_days": holding[0],
            "longest_days": holding[-1],
            "share_within_5_days": round(
                sum(1 for days in holding if days <= 5) / len(holding), 3),
        }

    diagnosis = {
        "trades": len(trades),
        "exit_reasons": dict(exit_reasons),
        "holding_period": holding_stats,
        "entries": len(entries),
        "exits": len(exits),
        "days_traded": len({t["day"] for t in trades}),
        "allow_entries_true": len(allowed),
        "allow_entries_false": len(blocked),
        "allow_entries_share": round(len(allowed) / len(trades), 4) if trades else None,
        "regimes": dict(regimes),
        "gross_notional_cents": gross,
        "fees_cents": fees,
        "fee_bps_of_gross": fee_bps,
        "mean_score": (sum(scores) // len(scores)) if scores else None,
        "entry_threshold": (threshold[0] if threshold else None),
        "target_weight_bps": size_ratio,
        "note": (
            "Every field comes from trade.basis_json, written when the decision "
            "was made. Nothing here is inferred from the outcome."
        ),
    }

    # The two questions worth answering explicitly, as booleans a reader can act
    # on rather than a table they have to interpret.
    findings = []
    if blocked and len(blocked) > len(allowed):
        findings.append({
            "finding": "entries_blocked_majority",
            "detail": (f"{len(blocked)} of {len(trades)} decisions had entries disabled; "
                       "in a rising market that is the likely source of the shortfall"),
        })
    if fee_bps >= 30:
        findings.append({
            "finding": "turnover_costly",
            "detail": f"fees are {fee_bps} bps of gross traded value",
        })
    if holding_stats and holding_stats["share_within_5_days"] >= 0.6:
        findings.append({
            "finding": "churns_faster_than_the_signal_can_work",
            "detail": (f"{int(holding_stats['share_within_5_days'] * 100)}% of positions "
                       f"close within 5 sessions (median {holding_stats['median_days']}); "
                       "the exit is firing before the entry can pay for itself"),
        })
    if size_ratio:
        thin = [slot for slot, info in size_ratio.items() if info["mean_target_bps"] < 3000]
        if thin:
            findings.append({
                "finding": "target_weights_below_full",
                "detail": f"slots with mean target under 30%: {', '.join(sorted(thin))}",
            })
    diagnosis["findings"] = findings
    return diagnosis


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, help="regime_ab JSON report")
    parser.add_argument("--ledger", type=Path, help="ledger database with a trade table")
    parser.add_argument("--variant", default="filtered", choices=["filtered", "unfiltered"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.replay and not args.ledger:
        raise SystemExit("give --replay or --ledger")

    if args.replay:
        if not args.replay.exists():
            raise SystemExit(f"replay not found: {args.replay}")
        trades = load_from_replay(args.replay, args.variant)
    else:
        if not args.ledger.exists():
            raise SystemExit(f"ledger not found: {args.ledger}")
        trades = load_from_ledger(args.ledger)

    result = analyse(trades, load_calendar(args.replay) if args.replay else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
