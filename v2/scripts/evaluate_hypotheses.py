#!/usr/bin/env python3
"""Evaluate LLM-proposed factor hypotheses against real history.

This closes the loop that makes an LLM useful here: it proposes, deterministic
code decides. Without this step the proposals are just plausible-looking JSON
sitting in a file.

Design notes borrowed from the reference project's trajectory evaluation:

* This is a **reporter, not a gate.** A hypothesis failing is not a process
  error — it is the expected, informative outcome. Exit 0 whenever the report
  was produced.
* Rejections stay visible. Every hypothesis gets a row, including the ones
  that could not be evaluated and why.
* Nothing here writes a ledger or a candidate. It produces evidence; promoting
  a candidate is a separate, governed act.

Usage:
    python scripts/evaluate_hypotheses.py \
        --proposals ../work/hypotheses.json \
        --db ../../.data/v2bars.sqlite3 \
        --days 2026-08-17,2026-08-24,2026-09-01 \
        --out ../work/hypothesis-report.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path


def run_probe(probe: Path, db: Path, day: str, expr: dict, timeout: float) -> dict:
    """Run the Rust factor probe for one expression on one day."""
    result = subprocess.run(
        [str(probe), str(db), day, json.dumps(expr)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()[:300]}
    try:
        return {"ok": True, "data": json.loads(result.stdout)}
    except json.JSONDecodeError as error:
        return {"ok": False, "error": f"unparseable_probe_output: {error}"}


def summarise(rank_ics: list[int]) -> dict:
    """Turn a list of per-period RankICs into the statistics governance uses.

    IR needs at least two periods. Reporting an IR from a single period would
    be inventing stability, which is exactly what P4's gate refuses.
    """
    if not rank_ics:
        return {"periods": 0, "mean_rank_ic_bps": None, "ir_bps": None}
    mean = statistics.fmean(rank_ics)
    ir = None
    if len(rank_ics) >= 2:
        stdev = statistics.pstdev(rank_ics)
        if stdev > 0:
            ir = round(mean / stdev)
    return {
        "periods": len(rank_ics),
        "mean_rank_ic_bps": round(mean),
        "ir_bps": ir,
        # Sign flips across periods are the cheapest overfitting tell, so they
        # are surfaced rather than left for the reader to notice.
        "sign_flips": sum(
            1
            for left, right in zip(rank_ics, rank_ics[1:])
            if (left >= 0) != (right >= 0)
        ),
        "period_rank_ic_bps": rank_ics,
    }


def dedupe_rows(rows: list[dict], args) -> dict:
    """Flag hypotheses that order the market the same way as an earlier one.

    Uses the same day and probe as the evaluation, so the comparison is on data
    the rows already used. A failure to dedupe must not discard the evaluation,
    so any problem returns a note instead of raising.
    """
    if len(rows) < 2:
        return {"checked": False, "reason": "fewer than two hypotheses"}
    try:
        import os
        os.environ["ASTOCK_PROBE_VALUES"] = "1"
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from dedupe_hypotheses import probe_values, spearman  # noqa: E402

        series = []
        for index, row in enumerate(rows):
            values = probe_values(Path(args.probe), Path(args.db), args.days[0], row["expr"])
            if values is None:
                continue
            series.append((row.get("rationale") or f"h{index}", values))
        pairs = []
        for i in range(len(series)):
            for j in range(i + 1, len(series)):
                left_label, left = series[i]
                right_label, right = series[j]
                common = sorted(set(left) & set(right))
                if len(common) < 3:
                    continue
                rho = spearman([left[s] for s in common], [right[s] for s in common])
                if rho is None:
                    continue
                if abs(rho) >= 0.999:
                    pairs.append({
                        "left": left_label, "right": right_label,
                        "rho": round(rho, 6),
                        "verdict": "mirror" if rho < 0 else "identical",
                    })
        return {
            "checked": True,
            "day": args.days[0],
            "pairs": pairs,
            "duplicate_count": len(pairs),
            "note": ("A mirror is the same information with the sign flipped: "
                     "duplicate for budget, not the same claim."),
        }
    except Exception as error:  # never lose the evaluation over a dedupe step
        return {"checked": False, "reason": f"{type(error).__name__}: {error}"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path,
                        help="path to the release factor_probe binary")
    parser.add_argument("--days", required=True,
                        help="comma separated decision days")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    proposals = json.loads(args.proposals.read_text(encoding="utf-8"))
    accepted = proposals.get("accepted") or []
    days = [day.strip() for day in args.days.split(",") if day.strip()]
    if not accepted:
        print("no accepted proposals to evaluate", file=sys.stderr)
        return 2
    if not days:
        print("at least one day is required", file=sys.stderr)
        return 2

    rows = []
    for item in accepted:
        expr = item["expr"]
        per_period: list[dict] = []
        rank_ics: list[int] = []
        for day in days:
            outcome = run_probe(args.probe, args.db, day, expr, args.timeout)
            if not outcome["ok"]:
                per_period.append({"day": day, "ok": False, "error": outcome["error"]})
                continue
            data = outcome["data"]
            stats = data.get("stats") or {}
            # A run that scored nothing is a failure to evaluate, not a zero.
            if not stats.get("samples"):
                per_period.append({"day": day, "ok": False, "error": "no_samples"})
                continue
            # Alphalens' max_loss guard: a factor scored on a small surviving
            # slice can produce a respectable IC that looks identical to one
            # measured on the whole market. Past the threshold the day is
            # unusable, not merely imperfect.
            coverage = data.get("coverage") or {}
            if coverage and coverage.get("usable") is False:
                per_period.append({
                    "day": day, "ok": False,
                    "error": "coverage_below_max_loss",
                    "coverage": coverage,
                })
                continue
            rank_ic = stats.get("rank_ic_bps")
            rank_ics.append(rank_ic)
            per_period.append(
                {
                    "day": day,
                    "ok": True,
                    "samples": stats.get("samples"),
                    "rank_ic_bps": rank_ic,
                    "ic_bps": stats.get("ic_bps"),
                    "spread_bps": (data.get("spread") or {}).get("spread_bps"),
                    "insufficient_history": data.get("insufficient_history"),
                    "coverage": coverage or None,
                }
            )
        rows.append(
            {
                "expr": expr,
                "rationale": item.get("rationale", ""),
                "proposed_by": item.get("proposed_by", ""),
                "periods_requested": len(days),
                "periods_evaluated": len(rank_ics),
                "summary": summarise(rank_ics),
                "periods": per_period,
            }
        )

    report = {
        "theme": proposals.get("theme"),
        "model": proposals.get("model"),
        "days": days,
        "hypotheses": rows,
        "note": (
            "Reporter, not a gate: a weak factor is an expected result, not an "
            "error. Nothing here promotes a candidate — that stays a governed act."
        ),
    }
    # A report is only worth reading if its rows are independent claims.
    # Duplicates are detected from raw factor orderings and reported rather
    # than silently dropped, so the cost of a lazy proposal stays visible.
    report["duplicates"] = dedupe_rows(rows, args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in rows:
        summary = row["summary"]
        print(
            f"periods {row['periods_evaluated']}/{row['periods_requested']} "
            f"mean_rank_ic {summary['mean_rank_ic_bps']} "
            f"ir {summary['ir_bps']} "
            f"flips {summary.get('sign_flips')} "
            f"| {json.dumps(row['expr'], ensure_ascii=False)[:60]}",
            file=sys.stderr,
        )
    print(f"report -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
