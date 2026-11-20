#!/usr/bin/env python3
"""Compare two regime_ab runs day by day, object by object.

A digest or an endpoint match cannot prove a performance change preserved
behaviour: a faster run that buys a different name still ends some days at the
same equity. This walks every settled day and reports the first divergence with
the exact field that differs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

OBJECTS = ("fills", "blocked", "slots", "rebalance")


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def variant_day(report: dict, variant: str, day: str) -> dict | None:
    items = report.get(variant, {}).get("snapshots")
    if not items:
        return None
    for item in items:
        if item.get("day") == day:
            return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--report", type=Path, help="write the full comparison as JSON")
    args = parser.parse_args()

    base, cand = load(args.baseline), load(args.candidate)
    days = [row["day"] for row in base["input"]["windows"]]
    result = {"days": len(days), "compared": 0, "differences": [], "missing_snapshots": 0}

    for variant in ("unfiltered", "filtered"):
        for day in days:
            left = variant_day(base, variant, day)
            right = variant_day(cand, variant, day)
            if left is None or right is None:
                result["missing_snapshots"] += 1
                result["differences"].append({
                    "variant": variant, "day": day, "field": "<snapshot>",
                    "baseline": left is not None, "candidate": right is not None,
                })
                continue
            result["compared"] += 1
            for key in OBJECTS:
                if left.get(key) != right.get(key):
                    result["differences"].append({
                        "variant": variant, "day": day, "field": key,
                        "baseline": left.get(key), "candidate": right.get(key),
                    })

    # Endpoints are compared too, but daily equality is the acceptance bar.
    for variant in ("unfiltered", "filtered"):
        left = (base.get(variant) or {}).get("account") or {}
        right = (cand.get(variant) or {}).get("account") or {}
        for key in ("equity_cents", "profit_cents", "return_bps", "max_drawdown_bps"):
            if left.get(key) != right.get(key):
                result["differences"].append({
                    "variant": variant, "day": "<end>", "field": key,
                    "baseline": left.get(key), "candidate": right.get(key),
                })

    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"compared {result['compared']} variant-days, "
          f"{result['missing_snapshots']} missing snapshots, "
          f"{len(result['differences'])} differences")
    for item in result["differences"][:10]:
        print(f"  {item['variant']} {item['day']} {item['field']}: "
              f"{json.dumps(item['baseline'], ensure_ascii=False)[:90]} != "
              f"{json.dumps(item['candidate'], ensure_ascii=False)[:90]}")
    return 0 if not result["differences"] else 1


if __name__ == "__main__":
    sys.exit(main())
