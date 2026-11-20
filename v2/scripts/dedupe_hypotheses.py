#!/usr/bin/env python3
"""Detect hypotheses that are the same factor wearing a different expression.

An LLM proposing six factors often proposes one factor six times: `-std20`,
`0 - std20`, `1000000 / std60`, `-1 * std20`. They differ syntactically and are
nearly identical after ranking, so evaluating all of them spends real compute
to produce correlated evidence that looks like independent confirmation.

This script answers one question per pair: **do these two expressions order the
market the same way on the same day?** It uses the rank correlation of the raw
factor values across symbols, which is the quantity a RankIC comparison would
actually see. Two expressions with |rho| above the threshold are reported as
duplicates, and the lower-ranked one is marked redundant.

Sign matters and is reported separately: `-x` against `x` gives rho near -1,
which is the same information with the sign flipped. That is a duplicate for
budget purposes, but not a duplicate claim — so it is labelled `mirror`, not
`identical`.

Usage:
    python scripts/dedupe_hypotheses.py \
        --proposals work/hypotheses.json \
        --db ../../.data/v2bars.sqlite3 \
        --probe work/factor_probe.exe \
        --day 2025-05-16 --threshold 0.999 \
        --out work/dedupe.json
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path


def rank(values: list[float]) -> list[float]:
    """Average ranks, matching the Rust factor_stats convention.

    Ties share the mean of the ranks they span. Without this, a factor that is
    constant over most of the market (a common case for windowed statistics on
    illiquid names) would produce a spurious correlation with anything.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    a = rank(left)
    b = rank(right)
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    variance_a = sum((x - mean_a) ** 2 for x in a)
    variance_b = sum((y - mean_b) ** 2 for y in b)
    if variance_a <= 0 or variance_b <= 0:
        return None
    return covariance / math.sqrt(variance_a * variance_b)


def probe_values(probe: Path, db: Path, day: str, expr: dict,
                 timeout: float = 300.0) -> dict[str, float] | None:
    """Raw factor value per symbol, from the Rust probe."""
    result = subprocess.run(
        [str(probe), str(db), day, json.dumps(expr)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        print(f"probe failed for {json.dumps(expr, ensure_ascii=False)[:60]}: "
              f"{(result.stderr or result.stdout).strip()[:200]}", file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("probe returned non-JSON", file=sys.stderr)
        return None
    values = payload.get("values") or payload.get("samples")
    if isinstance(values, dict):
        return {str(k): float(v) for k, v in values.items()}
    if isinstance(values, list):
        out = {}
        for row in values:
            if isinstance(row, dict) and "symbol" in row and "value" in row:
                out[str(row["symbol"])] = float(row["value"])
        return out or None
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--day", required=True)
    parser.add_argument("--threshold", type=float, default=0.999,
                        help="|rho| at or above which two expressions are duplicates")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    proposals = json.loads(args.proposals.read_text(encoding="utf-8"))
    items = proposals.get("accepted") or proposals.get("hypotheses") or []
    if not items:
        print("no proposals", file=sys.stderr)
        return 2

    series: list[tuple[str, dict, dict[str, float]]] = []
    for index, item in enumerate(items):
        expr = item["expr"]
        values = probe_values(args.probe, args.db, args.day, expr)
        if values is None:
            continue
        label = item.get("candidate_id") or item.get("rationale") or f"h{index}"
        series.append((str(label), expr, values))

    pairs = []
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            left_label, _, left = series[i]
            right_label, _, right = series[j]
            common = sorted(set(left) & set(right))
            if len(common) < 3:
                pairs.append({
                    "left": left_label, "right": right_label,
                    "rho": None, "verdict": "not_comparable",
                    "reason": f"only {len(common)} shared symbols",
                })
                continue
            rho = spearman([left[s] for s in common], [right[s] for s in common])
            if rho is None:
                verdict = "not_comparable"
                reason = "a series is constant across the shared symbols"
            elif abs(rho) >= args.threshold:
                verdict = "mirror" if rho < 0 else "identical"
                reason = "same ordering; one of them adds no new information"
            else:
                verdict = "distinct"
                reason = "orderings differ enough to be separate evidence"
            pairs.append({
                "left": left_label, "right": right_label,
                "rho": None if rho is None else round(rho, 6),
                "shared_symbols": len(common),
                "verdict": verdict, "reason": reason,
            })

    duplicates = [p for p in pairs if p["verdict"] in ("identical", "mirror")]
    survivors = {label for label, _, _ in series}
    for pair in duplicates:
        # Keep the first occurrence; the later one is the redundant copy. The
        # choice is arbitrary but deterministic, which is what matters when a
        # report is compared across runs.
        survivors.discard(pair["right"])

    report = {
        "day": args.day,
        "threshold": args.threshold,
        "evaluated": [label for label, _, _ in series],
        "pairs": pairs,
        "duplicate_count": len(duplicates),
        "survivors": sorted(survivors),
        "note": (
            "A mirror (rho near -1) carries the same information with the sign "
            "flipped: it is a duplicate for compute budget, but its claim is not "
            "the same claim. Both are reported so the distinction is not lost."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{len(series)} evaluated, {len(duplicates)} duplicate pairs, "
          f"{len(survivors)} survivors -> {args.out}")
    for pair in duplicates:
        print(f"  {pair['left']} ~ {pair['right']}: rho={pair['rho']} ({pair['verdict']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
