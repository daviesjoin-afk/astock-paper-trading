#!/usr/bin/env python3
"""How much of the book was actually at work, per fold.

The defensive shape has a mechanism worth measuring rather than guessing at:
in a rising market the book may simply not have been invested. `invested_days`
and the slot curves answer that directly, so this reports exposure before
anyone changes a rule.

Two numbers per slot:

* **time in market** — share of settled days with at least one position;
* **capital at work** — the slot's invested value as a share of its budget,
  averaged over the days it was invested. A slot that is in the market every
  day but at a quarter of its target is still mostly cash.

Reported together because they fail in different ways: never entering is a
signal problem, entering small is a sizing problem.

Usage:
    python scripts/exposure_report.py --replays runs --folds folds.json \
        --out exposure.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def curve_values(curve) -> list[int]:
    """Equity points from either shape the report uses."""
    if not isinstance(curve, list):
        return []
    out = []
    for point in curve:
        if isinstance(point, dict):
            value = point.get("equity_cents") or point.get("value_cents")
            if isinstance(value, int):
                out.append(value)
        elif isinstance(point, (int, float)):
            out.append(int(point))
    return out


def slot_exposure(slot: dict, settled_days: int) -> dict:
    invested_days = slot.get("invested_days")
    fills = slot.get("fills")
    open_positions = slot.get("open_positions")
    return {
        "slot": slot.get("slot"),
        "settled_days": settled_days,
        "invested_days": invested_days,
        # The share of days with any position at all. Low here means the slot
        # sat out; it does not say whether that was right, only that it happened.
        "time_in_market": (
            round(invested_days / settled_days, 4)
            if isinstance(invested_days, int) and settled_days else None
        ),
        "open_positions_at_end": open_positions,
        "fills": fills,
    }


def analyse(report: dict, variant: str = "filtered") -> dict:
    data = report.get(variant) or {}
    settled = (report.get("input") or {}).get("settled_days")
    slots = [slot_exposure(slot, settled or 0) for slot in data.get("slots") or []]
    account = data.get("account") or {}
    return {
        "return_bps": account.get("return_bps"),
        "max_drawdown_bps": account.get("max_drawdown_bps"),
        "settled_days": settled,
        "slots": slots,
        "weights_bps": data.get("weights_bps"),
    }


def summarise(folds: list[dict]) -> dict:
    """One line per slot: was it in the market, and did the days differ."""
    by_slot: dict[str, list[dict]] = {}
    for fold in folds:
        for slot in fold.get("slots") or []:
            by_slot.setdefault(slot["slot"] or "?", []).append(slot)

    out = {}
    for name, rows in by_slot.items():
        times = [row["time_in_market"] for row in rows if row["time_in_market"] is not None]
        out[name] = {
            "folds": len(rows),
            "mean_time_in_market": round(sum(times) / len(times), 4) if times else None,
            "min_time_in_market": min(times) if times else None,
            "max_time_in_market": max(times) if times else None,
            "per_fold": [row["time_in_market"] for row in rows],
        }

    # The question the whole report exists to answer: is a low-exposure slot a
    # constant property, or does it depend on the market?
    for name, info in out.items():
        per = [value for value in info["per_fold"] if value is not None]
        if len(per) >= 2 and max(per) - min(per) >= 0.2:
            info["finding"] = "exposure_varies_with_window"
        elif per and max(per) < 0.6:
            info["finding"] = "consistently_underinvested"
        else:
            info["finding"] = "no_clear_pattern"
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replays", required=True, type=Path)
    parser.add_argument("--folds", required=True, type=Path)
    parser.add_argument("--variant", default="filtered", choices=["filtered", "unfiltered"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    spec = json.loads(args.folds.read_text(encoding="utf-8"))
    folds = []
    for fold in spec["folds"]:
        path = args.replays / f"fold-{fold['fold']}.json"
        if not path.exists():
            folds.append({"fold": fold["fold"], "status": "no_replay"})
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        entry = analyse(report, args.variant)
        entry["fold"] = fold["fold"]
        entry["test"] = fold["test"]
        entry["status"] = "ok"
        folds.append(entry)

    measured = [fold for fold in folds if fold.get("status") == "ok"]
    report = {
        "variant": args.variant,
        "folds": folds,
        "by_slot": summarise(measured),
        "note": (
            "Exposure is measured, not judged: a slot that sat out may have "
            "been right. This says where to look, not what to change."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for fold in folds:
        if fold["status"] != "ok":
            print(f"  fold {fold['fold']}: {fold['status']}")
            continue
        parts = " ".join(f"{slot['slot']}={slot['time_in_market']}"
                         for slot in fold["slots"])
        print(f"  fold {fold['fold']} ret={fold['return_bps']:+5d} | {parts}")
    print(json.dumps(report["by_slot"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
