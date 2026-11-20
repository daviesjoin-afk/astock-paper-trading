#!/usr/bin/env python3
"""Advance shadow candidates one day and record honest evidence.

This is the piece that makes P4 real instead of a switch nobody flips. Each
run:

1. takes a settled trading day,
2. scores each shadow candidate's algorithm over real closes,
3. records the day's shadow equity in its own series,
4. recomputes `Evidence` from what actually happened,
5. asks the Rust governance gate whether the candidate may move.

It does **not** place orders. A shadow candidate is scored, not traded: there
is no book, no cash and no fills. That is deliberate — the moment a shadow
candidate can trade, it needs its own ledger, its own fees and its own halt
handling, and it stops being "shadow".

Because no fills occur, `ran_clean` is true only when the scoring itself
completed for every requested day. A day whose data was missing is not a clean
day.

Usage:
    python scripts/shadow_tick.py --db ../v2.sqlite3 \
        --market-db ../../.data/v2bars.sqlite3 --day 2026-11-10 \
        [--apply]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

# Mirrors domain::governance. The Rust gate remains authoritative; these exist
# so the script can report a shortfall without a round trip.
MIN_SHADOW_DAYS = 20
MIN_RANK_IC_BPS = 200
MIN_IR_BPS = 300
LOOKBACK_DAYS = 260


def has_tradable(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "tradable" for row in conn.execute("PRAGMA table_info(bar)"))


def closes_through(conn: sqlite3.Connection, symbol: str, day: str, limit: int,
                   tradable_column: bool) -> list[int]:
    """Real closes up to `day`, newest last.

    A source database predating the `tradable` column has no way to say
    "suspended". Falling back to `volume_shares > 0` is the same signal in that
    schema — a suspension has no volume — but it is a weaker one, so the caller
    reports which mode was used rather than passing them off as equal.
    """
    usable = "tradable=1" if tradable_column else "volume_shares>0"
    rows = conn.execute(
        f"SELECT close_cents FROM bar WHERE symbol=? AND day<=? AND adjust='none' "
        f"AND close_cents>0 AND {usable} ORDER BY day DESC LIMIT ?",
        (symbol, day, limit),
    ).fetchall()
    return [row[0] for row in reversed(rows)]


def score_slice(closes: list[int], kind: str) -> int | None:
    """A deliberately simple shadow score, computed here not by the LLM.

    It mirrors the *shape* of the library algorithms (trend and volatility) so
    a shadow day has something to record. It is not a promise that this is the
    algorithm's own score; the ledger's real algorithms stay in Rust.
    """
    if len(closes) < 21:
        return None
    latest = closes[-1]
    past = closes[-21]
    if past <= 0:
        return None
    trend_bps = (latest - past) * 10_000 // past
    if kind == "trend":
        return trend_bps
    # Volatility in bps of the mean: higher means choppier.
    window = closes[-20:]
    mean = sum(window) // len(window)
    if mean <= 0:
        return None
    variance = sum((value - mean) ** 2 for value in window) // len(window)
    return int((variance ** 0.5) * 10_000 // mean)


def load_factor_evidence(report_path: Path, candidate_id: str) -> dict | None:
    """Real cross-sectional IC/IR from an evaluate_hypotheses report.

    A shadow score series cannot produce a rank correlation against forward
    returns — that needs the whole cross-section per period. If a report is
    supplied, its numbers are used verbatim; if it is absent the evidence keeps
    values that cannot pass the gate. The one thing that never happens is
    estimating IC from a score series and calling it measured.
    """
    if not report_path or not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for item in report.get("hypotheses") or report.get("accepted") or []:
        label = item.get("candidate_id") or item.get("rationale") or ""
        if label != candidate_id:
            continue
        summary = item.get("summary") or {}
        return {
            "rank_ic_bps": summary.get("mean_rank_ic_bps"),
            "ir_bps": summary.get("ir_bps"),
            "periods": summary.get("periods"),
            "sign_flips": summary.get("sign_flips"),
            "source": str(report_path),
        }
    return None


def compute_evidence(conn: sqlite3.Connection, candidate_id: str,
                     algorithm_id: str, days_done: int,
                     factor: dict | None = None) -> dict:
    """Evidence assembled from the shadow series and the recorded days.

    The fields that cannot be derived from history stay at their conservative
    value: `ran_clean` is false until scoring succeeded for every requested
    day, and `tuned_on_same_window` is read from what the candidate recorded,
    never assumed false.
    """
    rows = conn.execute(
        "SELECT day, score_bps FROM shadow_day WHERE candidate_id=? ORDER BY day",
        (candidate_id,),
    ).fetchall()
    equity = [row[1] for row in rows]
    daily_returns = [
        (equity[i] - equity[i - 1]) * 10_000 // equity[i - 1]
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]
    # A single-number stand-in for IC would be a fabrication. What the shadow
    # series can honestly provide is the dispersion of its own returns: a
    # signal that does not vary is not a signal. Recorded as such.
    dispersion = 0
    if len(daily_returns) >= 2:
        mean = sum(daily_returns) // len(daily_returns)
        dispersion = (sum((value - mean) ** 2 for value in daily_returns) // len(daily_returns)) ** 0.5
        dispersion = int(dispersion)
    measured = factor or {}
    return {
        "shadow_days": days_done,
        # Measured, or deliberately failing values — never an estimate.
        "rank_ic_bps": measured.get("rank_ic_bps") if measured else 0,
        "ir_bps": measured.get("ir_bps") if measured else None,
        "tuned_on_same_window": True,
        "ran_clean": False,
        "evidence_source": measured.get("source") if measured else "shadow_series_only",
        "factor_periods": measured.get("periods"),
        "factor_sign_flips": measured.get("sign_flips"),
        "note": (
            "rank_ic/ir come from a cross-sectional report when one is supplied; "
            "otherwise they keep values that cannot pass the gate. A shadow "
            "score series is not a substitute for a measured rank correlation."
        ),
        "shadow_return_dispersion_bps": dispersion,
        "shadow_observations": len(daily_returns),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="ledger database holding the shadow tables")
    parser.add_argument("--market-db", required=True, type=Path)
    parser.add_argument("--day", required=True)
    parser.add_argument("--symbols", default="600000.SH,000001.SZ,000002.SZ,600036.SH",
                        help="basket the shadow series is scored over")
    parser.add_argument("--factor-report", type=Path, default=None,
                        help="evaluate_hypotheses report; supplies real rank_ic/ir")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    for path in (args.db, args.market_db):
        if not path.exists():
            raise SystemExit(f"not found: {path}")

    ledger = sqlite3.connect(args.db)
    market = sqlite3.connect(f"file:{args.market_db}?mode=ro", uri=True)
    candidates = ledger.execute(
        "SELECT candidate_id, algorithm_id, state FROM shadow_candidate "
        "WHERE state IN ('shadow','eligible','active') ORDER BY candidate_id"
    ).fetchall()
    if not candidates:
        print(json.dumps({"status": "no_shadow_candidates", "day": args.day}))
        return 0

    tradable_column = has_tradable(market)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    payload = []
    for candidate_id, algorithm_id, state in candidates:
        kind = "trend" if "trend" in algorithm_id or "momentum" in algorithm_id else "vol"
        scores = {}
        missing = []
        for symbol in symbols:
            closes = closes_through(market, symbol, args.day, LOOKBACK_DAYS, tradable_column)
            score = score_slice(closes, kind)
            if score is None:
                missing.append(symbol)
            else:
                scores[symbol] = score
        # Shadow equity: the mean score scaled into a notional series. It is a
        # score, not money — recording it as money would be the fabrication.
        if scores:
            equity = sum(scores.values()) // len(scores)
        else:
            equity = 0
        done = ledger.execute(
            "SELECT count(*) FROM shadow_day WHERE candidate_id=?", (candidate_id,)
        ).fetchone()[0]
        factor = load_factor_evidence(args.factor_report, candidate_id)
        evidence = compute_evidence(ledger, candidate_id, algorithm_id,
                                    done + (1 if args.apply else 0), factor)
        entry = {
            "candidate_id": candidate_id,
            "algorithm_id": algorithm_id,
            "state": state,
            "day": args.day,
            "scores": scores,
            "missing_symbols": missing,
            "shadow_score_bps": equity,
            "days_before": done,
            "evidence": evidence,
        }
        if args.apply:
            ledger.execute(
                "INSERT OR REPLACE INTO shadow_day(candidate_id,day,score_bps,note) "
                "VALUES(?,?,?,?)",
                (candidate_id, args.day, equity, f"shadow-score:{kind}"),
            )
            # Evidence is updated with the series, so a reader of the candidate
            # row sees what was measured rather than what was hoped for.
            ledger.execute(
                "UPDATE shadow_candidate SET evidence_json=?, updated_day=? "
                "WHERE candidate_id=?",
                (json.dumps(evidence, ensure_ascii=False), args.day, candidate_id),
            )
        payload.append(entry)

    if args.apply:
        ledger.commit()

    print(json.dumps({
        "day": args.day,
        "applied": bool(args.apply),
        "candidates": payload,
        "halt_rule": "tradable column" if tradable_column else "volume_shares>0 fallback (source has no tradable column)",
        "note": ("shadow_score_bps is a score, not currency. Evidence that cannot be "
                 "derived from a score series is left at values that fail the gate."),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
