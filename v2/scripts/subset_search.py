#!/usr/bin/env python3
"""Search candidate subgroups, then correct for having searched.

Scanning 271 closed trades across ten candidate splits produced three whose
95% confidence intervals excluded zero: aggressive-in-trending (+85, n=112),
aggressive-score>=95 (+119, n=75), aggressive-score>=97 (+204, n=41). That looks
like a discovery and is not one. Ten hypotheses at a 5% level expect about half a
false positive; finding three is ordinary, and a Bonferroni correction at the
99.5% level made all three disappear.

This script exists so that finding is not re-learned the hard way every time:
it reports the raw intervals, the number of hypotheses tried, and the corrected
intervals **side by side**, and refuses to call a result a discovery unless it
survives the correction.

It also reports the account-level projection, because a trade-level effect has
to be multiplied by the slot's weight before it reaches the account -- and that
multiplication is usually what turns a "significant" trade-level finding into
something the account-level A/B could never resolve.

Usage:
    python scripts/subset_search.py --replays runs --out subset.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import statistics


def load_trades(replay_dir: Path) -> list[dict]:
    """Closed trades with the decision basis attached."""
    trades: list[dict] = []
    for path in sorted(replay_dir.glob("fold-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fold = path.stem.replace("fold-", "")
        data = payload.get("filtered") or {}
        open_by_key: dict[tuple[str, str], list[tuple[str, int, dict]]] = defaultdict(list)
        for trade in data.get("trades") or []:
            key = (trade.get("slot") or "", trade.get("symbol") or "")
            if trade.get("side") == "buy":
                open_by_key[key].append(
                    (trade.get("trade_day") or "", trade.get("price_cents") or 0,
                     trade.get("basis") or {})
                )
            elif trade.get("side") == "sell" and open_by_key[key]:
                _, bought, basis = open_by_key[key].pop(0)
                sold = trade.get("price_cents") or 0
                if bought > 0:
                    trades.append({
                        "fold": fold,
                        "slot": key[0],
                        "pnl_bps": (sold - bought) * 10_000 // bought,
                        "regime": basis.get("regime"),
                        "score": basis.get("score"),
                    })
    return trades


def interval(values: list[float], z: float) -> dict:
    if len(values) < 2:
        return {"n": len(values), "mean": None, "lo": None, "hi": None}
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / len(values) ** 0.5
    return {
        "n": len(values),
        "mean": round(mean),
        "lo": round(mean - z * standard_error),
        "hi": round(mean + z * standard_error),
    }


def build_hypotheses() -> list[tuple[str, callable]]:
    """Every split the scan will try. The count matters for the correction."""
    hypotheses: list[tuple[str, callable]] = []
    for slot in ("steady", "aggressive"):
        hypotheses.append((f"slot={slot}", lambda t, s=slot: t["slot"] == s))
    for regime in ("trending", "choppy", "declining"):
        hypotheses.append((
            f"aggressive+regime={regime}",
            lambda t, r=regime: t["slot"] == "aggressive" and t["regime"] == r,
        ))
    for threshold in (80, 85, 90, 95, 97):
        hypotheses.append((
            f"aggressive+score>={threshold}",
            lambda t, s=threshold: (
                t["slot"] == "aggressive"
                and isinstance(t["score"], int)
                and t["score"] >= s
            ),
        ))
    return hypotheses


def report(trades: list[dict], slot_weight_bps: int = 5_500) -> dict:
    hypotheses = build_hypotheses()
    tried = len(hypotheses)
    # Bonferroni: the per-hypothesis level that keeps the family at 5%.
    z = 1.96 + 0.0
    # Two-sided 95% over `tried` tests is a z of roughly 1.96 + a small amount;
    # rather than hand-roll a normal quantile, use the standard bound: divide
    # alpha by `tried` and map with an explicit table for the sizes we use.
    alpha = 0.05 / max(tried, 1)
    z_corrected = z_for_alpha(alpha)

    rows = []
    for name, predicate in hypotheses:
        values = [trade["pnl_bps"] for trade in trades if predicate(trade)]
        if len(values) < 10:
            rows.append({"hypothesis": name, "skipped": "fewer than 10 trades"})
            continue
        raw = interval(values, z)
        corrected = interval(values, z_corrected)
        raw_sig = raw["lo"] is not None and (raw["lo"] > 0 or raw["hi"] < 0)
        corr_sig = corrected["lo"] is not None and (corrected["lo"] > 0 or corrected["hi"] < 0)
        rows.append({
            "hypothesis": name,
            "n": raw["n"],
            "mean_bps": raw["mean"],
            "raw_ci95": [raw["lo"], raw["hi"]],
            "raw_significant": raw_sig,
            "corrected_ci": [corrected["lo"], corrected["hi"]],
            "corrected_significant": corr_sig,
            # What the effect would look like at account level after the slot's
            # weight is applied, compared with the account-level resolution.
            "account_excess_bps": round(raw["mean"] * slot_weight_bps / 10_000)
            if raw["mean"] is not None else None,
        })

    raw_hits = [row for row in rows if row.get("raw_significant")]
    survivors = [row for row in rows if row.get("corrected_significant")]
    return {
        "trades": len(trades),
        "hypotheses_tried": tried,
        "alpha_after_correction": round(alpha, 5),
        "raw_significant": len(raw_hits),
        "surviving_significant": len(survivors),
        "rows": rows,
        "verdict": (
            "survives_correction" if survivors
            else "nothing_survives_correction"
        ),
        "note": (
            "A subgroup found by searching is not a discovery until it survives "
            "the correction for having searched. The account-level column exists "
            "because a trade-level effect must be multiplied by the slot weight "
            "before it can move the account, and that usually puts it back under "
            "the account-level resolution."
        ),
    }


def z_for_alpha(alpha: float) -> float:
    """Two-sided z for a given alpha, from a small table plus linear fallback.

    Written out rather than imported so the tool has no dependency; the values
    are the standard normal quantiles at the levels this project uses.
    """
    table = {
        0.05: 1.960, 0.01: 2.576, 0.005: 2.807, 0.001: 3.291,
        0.0001: 3.891, 0.00001: 4.417,
    }
    if alpha in table:
        return table[alpha]
    # Between 0.05 and 0.000001 the quantile is close to linear in -log10(alpha).
    import math
    if alpha >= 0.05:
        return 1.960
    if alpha <= 0.000001:
        return 4.892
    lower = math.log10(alpha)
    return 1.960 + (-lower - 1.301) / (6.0 - 1.301) * (4.892 - 1.960)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replays", required=True, type=Path)
    parser.add_argument("--slot-weight-bps", type=int, default=5_500)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.replays.is_dir():
        raise SystemExit(f"replay directory not found: {args.replays}")
    trades = load_trades(args.replays)
    if not trades:
        raise SystemExit("no closed trades found in the replay directory")

    result = report(trades, args.slot_weight_bps)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"trades={result['trades']} hypotheses={result['hypotheses_tried']} "
          f"alpha={result['alpha_after_correction']}")
    for row in result["rows"]:
        if "skipped" in row:
            print(f"  {row['hypothesis']:34s} skipped: {row['skipped']}")
            continue
        flag = "RAW" if row["raw_significant"] else "   "
        keep = "KEEP" if row["corrected_significant"] else "    "
        print(f"  {row['hypothesis']:34s} n={row['n']:>3} mean {row['mean_bps']:+6d} "
              f"raw[{row['raw_ci95'][0]:+5d},{row['raw_ci95'][1]:+5d}] {flag} "
              f"corr[{row['corrected_ci'][0]:+5d},{row['corrected_ci'][1]:+5d}] {keep} "
              f"acct {row['account_excess_bps']:+4d}")
    print(f"verdict: {result['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
