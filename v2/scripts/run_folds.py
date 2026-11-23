#!/usr/bin/env python3
"""Run every fold and report the distribution, not a single verdict.

`oos_report.py` answers "did this one window hold up". This answers "over
several windows, is the result consistent" — which is the only form of the
question that can distinguish skill from a lucky year.

What it refuses to do:

* Compare folds against each other and declare a winner. Folds are samples of
  the same configuration, not competing candidates.
* Average the returns into one number. A mean hides the spread, and the spread
  is the finding: four folds at +2%, -3%, +1%, -9% and one fold at -2% are
  different claims.
* Retune between folds. Every fold runs the same parameters; that is the point.
* Call the folds independent when they are not. If a fold file was built with a
  step shorter than its test window, later folds re-test days an earlier fold
  already tested. The run is still produced, but the summary says
  `test_windows_overlap: true` and `independent_samples: false`.

The execution model is carried through rather than assumed. `--fills` is
forwarded to the replay binary and the mode the binary reports back is written
into every fold row and into the summary, so a `same-close` batch can never be
mistaken for a next-session one.

Usage:
    python scripts/run_folds.py --folds folds.json --binary regime_ab.exe \
        --db market.sqlite3 --out folds-report.json --workdir runs \
        [--fills same-close|next-session]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys


FILL_MODES = ("same-close", "next-session")


def run_fold(binary: Path, db: Path, fold: dict, workdir: Path,
             variant: str, timeout: float, fills: str = "same-close") -> dict:
    """Replay one fold end to end and read the account out of the report."""
    if fills not in FILL_MODES:
        raise ValueError(f"unknown fills mode {fills!r}; expected one of {FILL_MODES}")
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / f"fold-{fold['fold']}.json"
    cmd = [
        str(binary), "--db", str(db),
        "--from", fold["test"][0], "--through", fold["test"][1],
        "--initial-cash-cents", "10000000",
        # Without this the binary always settles on the close it decided on, and
        # the batch silently reports a same-close result whatever was intended.
        "--fills", fills,
    ]
    try:
        with out_path.open("w", encoding="utf-8") as handle:
            result = subprocess.run(cmd, stdout=handle, stderr=subprocess.PIPE,
                                    text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        # A binary that cannot run is a failed fold, not a zero return. Raising
        # here would abort the whole sweep and lose the folds that did run.
        return {
            "fold": fold["fold"], "test": fold["test"],
            "status": "failed",
            "error": [f"{type(error).__name__}: {error}"],
        }
    if result.returncode != 0:
        # Keep the whole tail, not just the last line. A replay that dies on
        # `missing_close_price` prints the offending day and the held symbol
        # above the summary line, and that context is the difference between
        # "the fold failed" and "the fold failed because of a data gap on
        # 2024-12-30" -- which is a data problem, not a strategy problem.
        lines = [line for line in (result.stderr or "").strip().splitlines() if line.strip()]
        tail = lines[-6:] or ["exit code %d" % result.returncode]
        return {
            "fold": fold["fold"],
            "test": fold["test"],
            "status": "failed",
            "error": tail,
            "exit_code": result.returncode,
        }
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return {
            "fold": fold["fold"], "test": fold["test"],
            "status": "failed", "error": [f"report unreadable: {error}"],
        }
    data = payload.get(variant)
    if not data:
        return {
            "fold": fold["fold"], "test": fold["test"],
            "status": "failed", "error": [f"no {variant} variant in report"],
        }
    account = data.get("account") or {}
    declared = (payload.get("input") or {}).get("fills")
    result = {
        "fold": fold["fold"],
        # `train` is descriptive: the replay only settles the test window. A
        # fold spec without it is still runnable, so it is not required.
        "train": fold.get("train"),
        "test": fold["test"],
        "status": "ok",
        "return_bps": account.get("return_bps"),
        "max_drawdown_bps": account.get("max_drawdown_bps"),
        "fills": data.get("fills"),
        "days": len((payload.get("input") or {}).get("windows") or []),
        # What was asked for, and what the report says it actually did. Both
        # travel with the row so a reader never has to assume.
        "fills_mode_requested": fills,
        "fills_mode_reported": declared,
    }
    if declared and declared != fills:
        # A binary that ignored `--fills` produces numbers under a different
        # execution model than the batch claims. That is not a fold result.
        result["status"] = "failed"
        result["error"] = [
            f"fills mode mismatch: requested {fills}, report says {declared}"
        ]
        result.pop("return_bps", None)
    if fold.get("reused_test_days"):
        result["reused_test_days"] = fold["reused_test_days"]
    return result


def fill_modes(results: list[dict]) -> dict:
    """Which execution model the batch actually ran under.

    One mode per batch is the contract: mixing them would make the fold returns
    incomparable. So the set is reported, and a batch with no recorded mode is
    labelled `unknown` rather than being described as same-close by default.
    """
    requested = sorted({row.get("fills_mode_requested") for row in results
                        if row.get("fills_mode_requested")})
    reported = sorted({row.get("fills_mode_reported") for row in results
                       if row.get("fills_mode_reported")})
    return {
        "fills_mode": reported[0] if len(reported) == 1 else (
            "unknown" if not reported else "mixed"),
        "fills_mode_requested": requested[0] if len(requested) == 1 else (
            None if not requested else requested),
        "fills_mode_reported": reported,
    }


def test_windows_overlap(results: list[dict]) -> dict:
    """Do adjacent test windows share trading days?

    A fold file built with `--step-days` shorter than `--test-days` produces
    folds that re-test days an earlier fold already tested. The returns are
    still real, but they are not independent observations, and a "5 of 6 folds
    positive" count over states the evidence.
    """
    pairs = []
    ordered = sorted((row for row in results if row.get("test")),
                     key=lambda row: row["fold"])
    for earlier, later in zip(ordered, ordered[1:]):
        reused = later.get("reused_test_days")
        if reused is None:
            # No per-fold count in the file: fall back to whether the windows
            # touch at all. Unknown is enough to refuse the independence claim.
            reused = 0
            if later["test"][0] <= earlier["test"][1]:
                reused = 1
        pairs.append({"earlier_fold": earlier["fold"], "later_fold": later["fold"],
                      "reused_days": reused})
    total = sum(pair["reused_days"] for pair in pairs)
    return {
        "test_windows_overlap": total > 0,
        "reused_test_days": total,
        "overlap_pairs": [pair for pair in pairs if pair["reused_days"]],
    }


def summarise(results: list[dict]) -> dict:
    """Describe the distribution without collapsing it into a verdict."""
    overlap = test_windows_overlap(results)
    modes = fill_modes(results)
    ok = [row for row in results if row["status"] == "ok" and row["return_bps"] is not None]
    failed = [row for row in results if row["status"] != "ok"]
    if not ok:
        return {
            "folds_run": len(results), "folds_ok": 0, "folds_failed": len(failed),
            "verdict": "no_usable_fold",
            "note": "No fold produced an account curve; nothing can be said.",
            "independent_samples": False,
            **modes, **overlap,
        }
    returns = [row["return_bps"] for row in ok]
    positive = sum(1 for value in returns if value > 0)
    summary = {
        "folds_run": len(results),
        "folds_ok": len(ok),
        "folds_failed": len(failed),
        "returns_bps": returns,
        "positive_folds": positive,
        "negative_folds": len(returns) - positive,
        "mean_bps": round(statistics.fmean(returns)),
        "median_bps": round(statistics.median(returns)),
        "worst_bps": min(returns),
        "best_bps": max(returns),
        # Spread is the finding, so it is reported next to the mean rather than
        # left for a reader to subtract.
        "spread_bps": max(returns) - min(returns),
        "note": (
            "Folds are samples of one configuration, not competing candidates. "
            "A mean without the spread invites a conclusion the data does not support."
        ),
        **modes,
        **overlap,
    }
    if len(returns) < 3:
        summary["verdict"] = "too_few_folds"
        summary["why"] = (
            f"{len(returns)} usable fold(s). Three is the minimum before a spread "
            "means anything; below that it is one observation."
        )
    elif positive == len(returns):
        summary["verdict"] = "all_positive"
        summary["why"] = "Every fold positive. Stronger than one window, still not a forecast."
    elif positive == 0:
        summary["verdict"] = "all_negative"
        summary["why"] = "Every fold negative. The configuration is not supported."
    else:
        summary["verdict"] = "mixed"
        summary["why"] = (
            f"{positive} of {len(returns)} folds positive — the sign depends on the "
            "window, which is the definition of an unstable edge."
        )
    # Independence is a separate claim from the sign, and it is the one that was
    # being made by omission. Never let `independent_samples` be true by default.
    summary["independent_samples"] = (not overlap["test_windows_overlap"]
                                      and len(returns) > 1)
    if overlap["test_windows_overlap"]:
        summary["why"] = (
            summary["why"] + " Test windows overlap, so the fold count is not a "
            f"count of independent samples ({overlap['reused_test_days']} reused days)."
        )
    if failed:
        summary["failed_folds"] = [row["fold"] for row in failed]
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folds", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--variant", default="filtered", choices=["filtered", "unfiltered"])
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--only", default="", help="comma separated fold numbers")
    # Default preserved so existing callers keep the same behaviour; what
    # changes is that the mode is now forwarded, checked and reported instead
    # of being whatever the binary did by default.
    parser.add_argument("--fills", default="same-close", choices=list(FILL_MODES),
                        help="execution model forwarded to the replay binary "
                             "(default same-close, matching the binary default)")
    args = parser.parse_args(argv)

    for path in (args.folds, args.binary, args.db):
        if not path.exists():
            raise SystemExit(f"not found: {path}")

    spec = json.loads(args.folds.read_text(encoding="utf-8"))
    folds = spec.get("folds") or []
    if not folds:
        raise SystemExit("fold file contains no folds")
    if args.only:
        wanted = {int(part) for part in args.only.split(",") if part.strip()}
        folds = [fold for fold in folds if fold["fold"] in wanted]

    results = []
    for fold in folds:
        print(f"fold {fold['fold']}: test {fold['test'][0]}..{fold['test'][1]} "
              f"fills={args.fills}", file=sys.stderr)
        results.append(run_fold(args.binary, args.db, fold, args.workdir,
                                args.variant, args.timeout, fills=args.fills))

    summary = summarise(results)
    # The fold file's own overlap flags are the primary source; the result rows
    # carry them forward, so a hand-written result file without them is checked
    # against its declared windows instead of being trusted.
    if not any(row.get("reused_test_days") for row in results):
        spec_overlap = (spec.get("summary") or {}).get("test_windows_overlap")
        if spec_overlap:
            summary["test_windows_overlap"] = True
            summary["independent_samples"] = False
            summary["why"] = (
                summary.get("why", "") +
                " The fold file itself declares overlapping test windows."
            ).strip()

    report = {
        "variant": args.variant,
        "fills": summary["fills_mode"],
        "folds_file": str(args.folds),
        "mode": spec.get("mode"),
        "summary": summary,
        "results": results,
        "caveats": [
            "Every fold runs the same parameters; nothing is retuned between folds.",
            ("Folds may share training history (expanding window); only the test windows are "
             "independent."),
            "Unmodelled: slippage, order-book depth, ST liquidity beyond the turnover cap.",
        ],
    }
    if summary["test_windows_overlap"]:
        report["caveats"].append(
            "Test windows overlap: the folds re-test days an earlier fold already tested, so "
            "the fold count is not a count of independent samples."
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
