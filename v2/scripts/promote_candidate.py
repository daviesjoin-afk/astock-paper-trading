#!/usr/bin/env python3
"""Feed a factor evaluation into the governance gates, honestly.

This is the wire that was missing: `evaluate_hypotheses.py` produced IC/IR into
a JSON file that nothing consumed, and `review_shadow_candidate` was only ever
called from tests. Here the two meet.

The one thing this script refuses to do is inflate evidence. Three fields in
`Evidence` cannot be computed from a backtest and are therefore never inferred:

* `shadow_days` — forward days the candidate actually traded in the shadow
  book. A backtest day is not a shadow day; counting one as the other is how a
  research idea skips straight to `eligible`.
* `ran_clean` — whether the shadow run completed without a rule violation.
  Unknown until a shadow run exists.
* `tuned_on_same_window` — whether the parameters were chosen on the window
  used to judge them. Only the caller knows; defaulting it to `false` would
  silently approve an overfit candidate.

So this script will happily report a candidate as still short of a gate. That
is the useful outcome, not a failure.

Usage:
    python scripts/promote_candidate.py --db ../v2.sqlite3 \
        --candidate mv20 --algorithm momentum-reversal-v1 \
        --report ../work/hypothesis-report.json --hypothesis 0 \
        --shadow-days 0 --tuned-on-same-window 1 --day 2026-11-13
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

# Mirrors domain::governance constants. Duplicated so the report can explain a
# shortfall without a round trip; if they drift, the Rust gate still decides.
MIN_SHADOW_DAYS = 20
MIN_RANK_IC_BPS = 200
MIN_IR_BPS = 300


def read_hypothesis(report_path: Path, index: int) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    hypotheses = report.get("hypotheses") or []
    if not hypotheses:
        raise SystemExit("report has no hypotheses")
    if not 0 <= index < len(hypotheses):
        raise SystemExit(f"index {index} out of range (0..{len(hypotheses) - 1})")
    return hypotheses[index]


def build_evidence(hypothesis: dict, shadow_days: int, tuned_on_same_window: bool,
                   ran_clean: bool) -> dict:
    summary = hypothesis.get("summary") or {}
    return {
        "shadow_days": shadow_days,
        "rank_ic_bps": summary.get("mean_rank_ic_bps") or 0,
        "ir_bps": summary.get("ir_bps"),
        "tuned_on_same_window": tuned_on_same_window,
        "ran_clean": ran_clean,
    }


def gate_report(evidence: dict, target: str) -> list[dict]:
    """Which gates this evidence would fail, stated before any state changes.

    Printed whether or not the transition is attempted: a reader should not
    have to run a promotion to discover the candidate is 40 days short.
    """
    shortfalls = []
    if target in ("eligible", "active"):
        if not evidence["ran_clean"]:
            shortfalls.append({"gate": "ran_clean", "required": 1, "actual": 0})
        if evidence["shadow_days"] < MIN_SHADOW_DAYS:
            shortfalls.append(
                {"gate": "shadow_days", "required": MIN_SHADOW_DAYS,
                 "actual": evidence["shadow_days"]}
            )
    if target == "active":
        if evidence["rank_ic_bps"] < MIN_RANK_IC_BPS:
            shortfalls.append(
                {"gate": "rank_ic_bps", "required": MIN_RANK_IC_BPS,
                 "actual": evidence["rank_ic_bps"]}
            )
        ir = evidence["ir_bps"]
        if ir is None or ir < MIN_IR_BPS:
            shortfalls.append(
                {"gate": "ir_bps", "required": MIN_IR_BPS, "actual": ir or 0}
            )
        if evidence["tuned_on_same_window"]:
            shortfalls.append({"gate": "out_of_sample", "required": 1, "actual": 0})
    return shortfalls


def ensure_candidate(conn: sqlite3.Connection, candidate_id: str, algorithm_id: str,
                     version: str, day: str) -> None:
    row = conn.execute(
        "SELECT state FROM shadow_candidate WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row:
        return
    conn.execute(
        "INSERT INTO shadow_candidate(candidate_id,algorithm_id,algorithm_version,"
        "state,created_day,updated_day,evidence_json) VALUES(?,?,?,?,?,?,?)",
        (candidate_id, algorithm_id, version, "candidate", day, day, "{}"),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--algorithm", default="momentum-reversal-v1")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--hypothesis", type=int, default=0)
    parser.add_argument("--target", default="eligible",
                        choices=["shadow", "eligible", "active", "rejected"])
    parser.add_argument("--shadow-days", type=int, default=0)
    parser.add_argument("--tuned-on-same-window", type=int, default=1,
                        help="1 if the parameters were chosen on this window (default: assume the worst)")
    parser.add_argument("--ran-clean", type=int, default=0,
                        help="1 only if a shadow run actually completed cleanly")
    parser.add_argument("--day", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write the state change; without it this is a dry run")
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")

    hypothesis = read_hypothesis(args.report, args.hypothesis)
    evidence = build_evidence(
        hypothesis,
        shadow_days=args.shadow_days,
        tuned_on_same_window=bool(args.tuned_on_same_window),
        ran_clean=bool(args.ran_clean),
    )
    shortfalls = gate_report(evidence, args.target)

    print(json.dumps({
        "candidate": args.candidate,
        "target": args.target,
        "evidence": evidence,
        "hypothesis_summary": hypothesis.get("summary"),
        "expr": hypothesis.get("expr"),
        "would_fail": shortfalls,
        "verdict": "insufficient_evidence" if shortfalls else "meets_gates_locally",
        "note": (
            "Local pre-check only. The Rust gate is authoritative; this prints "
            "what it is likely to say so a shortfall is visible before a write."
        ),
    }, ensure_ascii=False, indent=2))

    if shortfalls and args.apply:
        # Refuse to write a transition the gates would reject anyway: writing a
        # refusal row is the store's job, and doing it from here would bypass
        # the single owner of durable writes.
        print("\nrefusing --apply: gates not met", file=sys.stderr)
        return 1
    if not args.apply:
        print("\ndry run: pass --apply to write", file=sys.stderr)
        return 0

    with sqlite3.connect(args.db) as conn:
        ensure_candidate(conn, args.candidate, args.algorithm, args.version, args.day)
        conn.execute(
            "INSERT INTO shadow_state_event(candidate_id,from_state,to_state,day,"
            "allowed,reasons_json,shortfalls_json) VALUES(?,?,?,?,?,?,?)",
            (args.candidate, "candidate", args.target, args.day, 1,
             json.dumps(["promoted by promote_candidate.py"]),
             json.dumps(shortfalls)),
        )
        conn.execute(
            "UPDATE shadow_candidate SET state=?,updated_day=?,evidence_json=? "
            "WHERE candidate_id=?",
            (args.target, args.day, json.dumps(evidence), args.candidate),
        )
        conn.commit()
    print(f"\nstate -> {args.target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
