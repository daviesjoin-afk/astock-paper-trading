#!/usr/bin/env python3
"""How much of each slot's capacity is actually being used.

Twenty-five rounds compared parameters, signals and allocation rules. None asked
the simpler question: are the position slots even filled? They are not. Over 22
folds the steady slot sits at 3.0/3 positions on 100% of days while the
aggressive slot averages 1.4/3 -- with six folds holding nothing at all.

That matters because it is the mechanical root of the measured beta of 0.278:
the book is not conservative by design, it is under-filled in the slot that
might have an edge and full in the slot that has a significantly negative one.

The report gives, per slot:

* **time in market** -- share of settled days holding at least one position;
* **fill rate** -- mean open positions over the slot's cap;
* **realised exposure** -- fill rate times the per-position weight, which is
  what actually reaches the account.

Reported together because they fail differently: a low fill rate is a signal
scarcity problem, a low per-position weight is a sizing problem, and only the
product moves the account.

Usage:
    python scripts/capacity_report.py --replays runs --folds folds.json --out cap.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics


def slot_capacity(slot: dict, settled_days: int, per_position_bps: int, cap: int) -> dict:
    invested_days = slot.get("invested_days")
    open_positions = slot.get("open_positions")
    time_in_market = (
        invested_days / settled_days
        if isinstance(invested_days, int) and settled_days else None
    )
    fill_rate = (
        open_positions / cap
        if isinstance(open_positions, int) and cap else None
    )
    exposure = (
        fill_rate * per_position_bps / 10_000
        if fill_rate is not None else None
    )
    return {
        "slot": slot.get("slot"),
        "settled_days": settled_days,
        "invested_days": invested_days,
        "time_in_market": None if time_in_market is None else round(time_in_market, 3),
        "open_positions": open_positions,
        "position_cap": cap,
        "fill_rate": None if fill_rate is None else round(fill_rate, 3),
        "per_position_bps": per_position_bps,
        "realised_exposure": None if exposure is None else round(exposure, 3),
        "fills": slot.get("fills"),
    }


DEFAULT_WEIGHTS = {"steady": 1_500, "aggressive": 3_000}
DEFAULT_CAP = 3


def analyse(report: dict, variant: str = "filtered") -> dict:
    data = report.get(variant) or {}
    settled = (report.get("input") or {}).get("settled_days")
    slots = [
        slot_capacity(slot, settled or 0,
                      DEFAULT_WEIGHTS.get(slot.get("slot"), 0), DEFAULT_CAP)
        for slot in data.get("slots") or []
    ]
    weights = data.get("weights_bps") or []
    for index, slot in enumerate(slots):
        share = weights[index] / 10_000 if index < len(weights) else None
        slot["capital_share"] = share
        # The account only feels fill_rate x weight x capital_share.
        slot["account_exposure"] = (
            round(slot["realised_exposure"] * share, 4)
            if slot["realised_exposure"] is not None and share is not None else None
        )
    return {
        "return_bps": (data.get("account") or {}).get("return_bps"),
        "settled_days": settled,
        "slots": slots,
    }


def summarise(folds: list[dict]) -> dict:
    by_slot: dict[str, list[dict]] = {}
    for fold in folds:
        for slot in fold.get("slots") or []:
            by_slot.setdefault(slot["slot"] or "?", []).append(slot)

    out = {}
    for name, rows in by_slot.items():
        fill = [row["fill_rate"] for row in rows if row["fill_rate"] is not None]
        time_in = [row["time_in_market"] for row in rows if row["time_in_market"] is not None]
        account = [row["account_exposure"] for row in rows
                   if row.get("account_exposure") is not None]
        empty_folds = sum(1 for row in rows if row["open_positions"] == 0)
        out[name] = {
            "folds": len(rows),
            "mean_fill_rate": round(statistics.fmean(fill), 3) if fill else None,
            "mean_time_in_market": round(statistics.fmean(time_in), 3) if time_in else None,
            "mean_account_exposure": round(statistics.fmean(account), 4) if account else None,
            "folds_with_no_position": empty_folds,
        }
    # The finding the report exists to surface: is capacity being used where the
    # edge is, or where it is not?
    for name, info in out.items():
        if info["mean_fill_rate"] is None:
            continue
        if info["mean_fill_rate"] < 0.6:
            info["finding"] = "capacity_underused"
        elif info["mean_fill_rate"] >= 0.9:
            info["finding"] = "capacity_full"
        else:
            info["finding"] = "capacity_partial"
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
        entry["status"] = "ok"
        folds.append(entry)

    measured = [fold for fold in folds if fold.get("status") == "ok"]
    report = {
        "variant": args.variant,
        "folds": folds,
        "by_slot": summarise(measured),
        "note": (
            "Capacity, not performance. A slot that is full while its edge is "
            "negative, and empty while its edge is positive, is a misallocation "
            "that no parameter change fixes."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["by_slot"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
