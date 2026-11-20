#!/usr/bin/env python3
"""Measure whether the regime call was right, per fold.

The five folds showed a defensive shape. Diagnosing one fold suggested the
regime classifier was pointing the wrong way: days labelled `declining` were
followed by gains, days labelled `trending` by losses.

One fold is a coincidence. This script measures the same statistic on every
fold, so a real inversion (all folds agree) can be told apart from a single
unlucky window.

The measurement: for each decision day, take the regime the system recorded in
`trade.basis_json`, then look at the market's own return over the next N
sessions. If the classifier is informative, `trending` days should precede
gains and `declining` days should precede losses.

Usage:
    python scripts/regime_accuracy.py --folds folds.json --replay-dir runs \
        --db market.sqlite3 --out regime-accuracy.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sqlite3
import statistics

# What each regime *claims* about the coming sessions.
EXPECTED_SIGN = {"trending": 1, "choppy": 0, "declining": -1}


def market_series(db: Path, start: str, end: str) -> tuple[list[str], list[int]]:
    """A market proxy: the cross-sectional mean close per day.

    Mean rather than median here because the classifier itself is built from an
    equal-weight index of mean closes; using a different construction would
    grade it against something it never saw.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT day, CAST(AVG(close_cents) AS INTEGER) FROM bar "
            "WHERE adjust='none' AND day BETWEEN ? AND ? AND close_cents > 0 "
            "GROUP BY day ORDER BY day",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows], [row[1] for row in rows]


def forward_return_bps(series: list[int], index: int, horizon: int) -> int | None:
    if index + horizon >= len(series):
        return None
    start, end = series[index], series[index + horizon]
    if start <= 0:
        return None
    return (end - start) * 10_000 // start


def regime_calls(replay: Path, variant: str) -> dict[str, str]:
    """Day -> regime, from the decision record. First call of a day wins."""
    payload = json.loads(replay.read_text(encoding="utf-8"))
    data = payload.get(variant) or {}
    calls: dict[str, str] = {}
    for trade in data.get("trades") or []:
        basis = trade.get("basis") or {}
        regime = basis.get("regime")
        day = trade.get("trade_day")
        if regime and day and day not in calls:
            calls[day] = regime
    return calls


def score_calls(calls: dict[str, str], days: list[str], series: list[int],
                horizon: int) -> dict:
    index = {day: position for position, day in enumerate(days)}
    grouped: dict[str, list[int]] = defaultdict(list)
    for day, regime in calls.items():
        position = index.get(day)
        if position is None:
            continue
        forward = forward_return_bps(series, position, horizon)
        if forward is None:
            continue
        grouped[regime].append(forward)

    table = {}
    for regime, values in grouped.items():
        table[regime] = {
            "samples": len(values),
            "median_forward_bps": int(statistics.median(values)),
            "mean_forward_bps": round(statistics.fmean(values)),
        }
    return table


def judge(table: dict) -> dict:
    """Was each regime's claim supported, and was the overall direction right?"""
    verdicts = {}
    correct = 0
    measured = 0
    for regime, expected in EXPECTED_SIGN.items():
        row = table.get(regime)
        if not row or row["samples"] == 0:
            verdicts[regime] = "no_sample"
            continue
        median = row["median_forward_bps"]
        if expected == 0:
            # "Choppy" claims nothing about direction; it is graded only on
            # whether the move was small, which is a different question and is
            # reported rather than scored.
            verdicts[regime] = "directionless"
            continue
        measured += 1
        agreed = (median > 0) == (expected > 0)
        verdicts[regime] = "supported" if agreed else "inverted"
        if agreed:
            correct += 1

    if measured == 0:
        return {"verdicts": verdicts, "direction": "unmeasured",
                "note": "no directional regime had samples"}
    if correct == 0:
        direction = "inverted"
        note = ("Every directional call pointed the wrong way: days labelled "
                "trending were followed by losses and declining by gains.")
    elif correct == measured:
        direction = "supported"
        note = "Every directional call pointed the right way."
    else:
        direction = "mixed"
        note = "Directional calls disagreed; no consistent direction across regimes."
    return {"verdicts": verdicts, "direction": direction, "note": note}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folds", required=True, type=Path)
    parser.add_argument("--replay-dir", required=True, type=Path,
                        help="directory with fold-<n>.json replay reports")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--variant", default="filtered", choices=["filtered", "unfiltered"])
    parser.add_argument("--horizon", type=int, default=20,
                        help="sessions ahead to measure the market move")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    spec = json.loads(args.folds.read_text(encoding="utf-8"))
    per_fold = []
    for fold in spec["folds"]:
        replay = args.replay_dir / f"fold-{fold['fold']}.json"
        if not replay.exists():
            per_fold.append({"fold": fold["fold"], "status": "no_replay",
                             "test": fold["test"]})
            continue
        calls = regime_calls(replay, args.variant)
        if not calls:
            per_fold.append({"fold": fold["fold"], "status": "no_calls",
                             "test": fold["test"]})
            continue
        days, series = market_series(args.db, fold["test"][0], fold["test"][1])
        table = score_calls(calls, days, series, args.horizon)
        per_fold.append({
            "fold": fold["fold"],
            "test": fold["test"],
            "status": "ok",
            "calls": len(calls),
            "by_regime": table,
            "judgement": judge(table),
        })

    measured = [row for row in per_fold if row["status"] == "ok"]
    directions = [row["judgement"]["direction"] for row in measured]
    summary = {
        "folds_measured": len(measured),
        "folds_total": len(per_fold),
        "horizon_sessions": args.horizon,
        "directions": directions,
    }
    if not directions:
        summary["verdict"] = "unmeasured"
    elif len(directions) < 3:
        # One inverted fold is an anecdote. Saying "everywhere" from a single
        # sample is exactly the overreach this project keeps having to undo.
        summary["verdict"] = "too_few_folds"
        summary["why"] = (
            f"{len(directions)} measured fold(s). Three is the minimum before "
            "'everywhere' means anything; below that it is one observation."
        )
    elif all(direction == "inverted" for direction in directions):
        summary["verdict"] = "inverted_everywhere"
        summary["why"] = (
            "Every fold's directional regime call preceded the opposite move. "
            "That is a finding about the classifier, not about one window."
        )
    elif all(direction == "supported" for direction in directions):
        summary["verdict"] = "supported_everywhere"
        summary["why"] = "Every fold's directional call was right."
    else:
        summary["verdict"] = "inconsistent"
        summary["why"] = "Folds disagree on direction; one fold's inversion is not a pattern."

    report = {"summary": summary, "folds": per_fold,
              "caveats": [
                  "A 20-session forward window overlaps between nearby decision days; "
                  "the samples are not independent.",
                  "The market proxy is a cross-sectional mean, not a tradable index.",
                  "Inversion here does not license flipping the sign: that would fit "
                  "this sample. It licenses asking whether the inversion is structural.",
              ]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in per_fold:
        if row["status"] != "ok":
            print(f"  fold {row['fold']}: {row['status']}")
            continue
        parts = " ".join(f"{regime}={info['median_forward_bps']:+d}"
                         for regime, info in sorted(row["by_regime"].items()))
        print(f"  fold {row['fold']}: {parts} -> {row['judgement']['direction']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
