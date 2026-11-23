#!/usr/bin/env python3
"""Compare a training-window replay against a held-out window, and say what it means.

The distinction this script exists to enforce:

* **In-sample** — the window the strategy's parameters were chosen on. Any
  number from here is a description of the past, not evidence of skill.
* **Out-of-sample** — a window the parameters never saw. This is the only place
  a result can count as new evidence.

Neither is allowed to change a parameter. If the held-out window is bad, the
answer is "this configuration is not supported", not "let's retune".

Usage:
    python scripts/oos_report.py \
        --train work/train.json --test work/test.json \
        --train-window 2023-07-05:2025-12-31 \
        --test-window 2026-01-02:2026-11-11 \
        --out work/oos-report.json

The declared windows are checked against the artifacts, not trusted. Two
replays can be handed in as `train` and `test` with two tidy non-overlapping
date ranges typed on the command line while both artifacts cover the same
days; the old version accepted that and printed `overlap: false` with
`consistent_positive`. Now the first and last *settled* day are read out of
each artifact and compared with what was declared. A mismatch, or artifact
spans that overlap, marks the report invalid and no positive verdict is
produced.

Exit codes: 0 when the comparison was performed (whatever the verdict),
2 when the report is invalid — the file is still written so the reason is
readable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


def load_variant(path: Path, variant: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get(variant)
    if not data:
        raise SystemExit(f"{path} has no {variant} variant")
    input_block = (data.get("input") or {}) or (payload.get("input") or {})
    return {
        "account": data.get("account") or {},
        "fills": data.get("fills"),
        "fees_cents": data.get("fees_cents"),
        "rebalances": len(data.get("rebalances") or []),
        "slots": data.get("slots") or [],
        # `regime_ab` writes `input.windows` at the top level; a hand-built
        # report may nest it inside the variant. Read both rather than pick.
        "windows": ((data.get("input") or {}).get("windows")
                    or (payload.get("input") or {}).get("windows") or []),
        # Identity of the run that produced the artifact, where it is recorded.
        "engine_version": payload.get("engine_version"),
        "declared_from": input_block.get("from"),
        "declared_through": input_block.get("through"),
        "declared_fills": input_block.get("fills"),
        "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "path": str(path),
    }


def actual_span(row: dict) -> tuple[str | None, str | None]:
    """The first and last settled trading day the artifact actually contains.

    Taken from the sorted day list, not from the order the rows happen to be
    written in: a report whose windows were appended out of order would
    otherwise claim a span it never traded.
    """
    days = sorted(window.get("day") for window in row["windows"]
                  if isinstance(window, dict) and window.get("day"))
    if not days:
        return None, None
    return days[0], days[-1]


def summarise(row: dict) -> dict:
    account = row["account"]
    first_day, last_day = actual_span(row)
    return {
        "equity_cents": account.get("equity_cents"),
        "return_bps": account.get("return_bps"),
        "max_drawdown_bps": account.get("max_drawdown_bps"),
        "profit_cents": account.get("profit_cents"),
        "fills": row["fills"],
        "fills_mode": row.get("declared_fills"),
        "fees_cents": row["fees_cents"],
        "rebalances": row["rebalances"],
        "days": len(row["windows"]),
        "first_day": first_day,
        "last_day": last_day,
        "artifact": row.get("path"),
        "artifact_sha256": row.get("digest"),
        "engine_version": row.get("engine_version"),
    }


def parse_window(raw: str) -> tuple[str, str]:
    parts = raw.split(":")
    if len(parts) != 2:
        raise SystemExit(f"window must be START:END, got {raw!r}")
    start, end = parts[0].strip(), parts[1].strip()
    if not start or not end:
        raise SystemExit(f"window bounds must both be given, got {raw!r}")
    return start, end


def check_no_leak(train_window: str, test_window: str) -> dict:
    """Refuse a comparison whose windows overlap in time.

    Borrowed from Qlib's `trunc_segments`: the training period is truncated at
    the test start so no test-day information can reach the parameters. Without
    this check a mis-typed window would silently produce a flattering "out of
    sample" number from data the parameters had already seen.
    """
    train_start, train_end = parse_window(train_window)
    test_start, test_end = parse_window(test_window)
    if not train_start <= train_end:
        raise SystemExit(f"train window is reversed: {train_window}")
    if not test_start <= test_end:
        raise SystemExit(f"test window is reversed: {test_window}")
    if train_end >= test_start:
        raise SystemExit(
            f"windows overlap: train ends {train_end}, test starts {test_start}. "
            "The held-out window must begin after the training window ends, "
            "otherwise the result is not out of sample."
        )
    return {
        "train": [train_start, train_end],
        "test": [test_start, test_end],
        "overlap": False,
    }


def verify_artifacts(train: dict, test: dict,
                     train_window: str, test_window: str) -> dict:
    """Check the declared windows against what the artifacts actually contain.

    The check this script used to be missing. Validating only the strings typed
    on the command line meant the same replay could be passed as `train` and
    `test`, with two tidy non-overlapping ranges declared, and the report would
    print `overlap: false` and `consistent_positive` — a held-out result made
    entirely of days the parameters had already seen.

    Three things are compared, all of them facts from the artifact rather than
    from the command line:

    * the first and last *settled* day the run produced, against the declared
      range;
    * the run's own recorded `input.from`/`input.through`, when it has one;
    * whether the two artifacts' real spans touch each other.

    Run identity is compared too: two returns produced by different engine
    versions, or under different fill models, are not two windows of one
    configuration.
    """
    problems: list[str] = []
    spans: dict[str, dict] = {}
    for label, row, window in (("train", train, train_window),
                               ("test", test, test_window)):
        declared_start, declared_end = parse_window(window)
        first_day, last_day = actual_span(row)
        span = {
            "declared": [declared_start, declared_end],
            "actual_first_day": first_day,
            "actual_last_day": last_day,
            "settled_days": len(row["windows"]),
        }
        spans[label] = span
        if first_day is None or last_day is None:
            problems.append(
                f"{label}: the artifact contains no settled day, so the declared "
                f"window {declared_start}..{declared_end} cannot be verified"
            )
            continue
        if first_day != declared_start or last_day != declared_end:
            problems.append(
                f"{label}: the artifact settles {first_day}..{last_day}, but the "
                f"declared window is {declared_start}..{declared_end}. The verdict "
                "would describe the declaration, not the numbers in the report."
            )
        recorded_from = row.get("declared_from")
        recorded_through = row.get("declared_through")
        if recorded_from and recorded_through:
            span["artifact_input"] = [recorded_from, recorded_through]
            if recorded_from != declared_start or recorded_through != declared_end:
                problems.append(
                    f"{label}: the run itself records {recorded_from}..{recorded_through}, "
                    f"but {declared_start}..{declared_end} was declared"
                )

    train_span, test_span = spans["train"], spans["test"]
    overlapped = False
    if all(span["actual_first_day"] and span["actual_last_day"]
           for span in (train_span, test_span)):
        latest_start = max(train_span["actual_first_day"], test_span["actual_first_day"])
        earliest_end = min(train_span["actual_last_day"], test_span["actual_last_day"])
        overlapped = latest_start <= earliest_end
        if overlapped:
            problems.append(
                "the artifacts' real trading spans overlap "
                f"({latest_start}..{earliest_end}): the test window is not held out, "
                "whatever dates were declared"
            )

    if train.get("engine_version") and test.get("engine_version") \
            and train["engine_version"] != test["engine_version"]:
        problems.append(
            "the two artifacts were produced by different engine versions "
            f"({train['engine_version']} vs {test['engine_version']})"
        )
    if train.get("declared_fills") and test.get("declared_fills") \
            and train["declared_fills"] != test["declared_fills"]:
        problems.append(
            "the two artifacts used different fill models "
            f"({train['declared_fills']} vs {test['declared_fills']})"
        )

    return {
        "valid": not problems,
        "problems": problems,
        "spans": spans,
        "actual_overlap": overlapped,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", required=True, type=Path)
    parser.add_argument("--test", required=True, type=Path)
    parser.add_argument("--train-window", required=True)
    parser.add_argument("--test-window", required=True)
    parser.add_argument("--variant", default="filtered",
                        choices=["filtered", "unfiltered"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    windows = check_no_leak(args.train_window, args.test_window)
    train = load_variant(args.train, args.variant)
    test = load_variant(args.test, args.variant)
    train_summary = summarise(train)
    test_summary = summarise(test)
    verification = verify_artifacts(train, test, args.train_window, args.test_window)

    # The honest read: a held-out window is one sample. Agreement in sign is
    # weak support; disagreement is strong evidence against.
    train_return = train_summary["return_bps"]
    test_return = test_summary["return_bps"]
    if train_return is None or test_return is None:
        verdict = "incomplete"
        why = "one of the windows produced no account curve"
    elif not verification["valid"]:
        # No sign agreement is reported from artifacts whose declared windows
        # do not describe them. `consistent_positive` here was the bug.
        verdict = "invalid"
        why = ("the declared windows do not match the artifacts: "
               + "; ".join(verification["problems"]))
    elif train_return > 0 and test_return > 0:
        verdict = "consistent_positive"
        why = "both windows positive; still one held-out sample, not a forecast"
    elif train_return <= 0 and test_return <= 0:
        verdict = "consistent_negative"
        why = "both windows negative; the configuration is not supported"
    elif train_return > 0 >= test_return:
        verdict = "in_sample_only"
        why = "positive only where the parameters were chosen — the classic overfit signature"
    else:
        verdict = "sign_flip"
        why = "negative in-sample, positive held-out; too small a sample to celebrate"

    report = {
        "variant": args.variant,
        "train": {"window": args.train_window, **train_summary},
        "test": {"window": args.test_window, **test_summary},
        "verdict": verdict,
        "why": why,
        "valid": verification["valid"],
        "declaration_problems": verification["problems"],
        "artifact_spans": verification["spans"],
        # `overlap` answers the question the reader actually asks — do the two
        # windows share a trading day — using the artifacts' real spans. It
        # stays false only when the declared *and* the actual spans are disjoint.
        "windows": {
            "train": windows["train"],
            "test": windows["test"],
            "declared_overlap": windows["overlap"],
            "actual_overlap": verification["actual_overlap"],
            "overlap": windows["overlap"] or verification["actual_overlap"],
        },
        "caveats": [
            "Parameters were chosen on the training window; the test window is the only new evidence.",
            "One held-out window is one sample. It cannot establish a distribution of outcomes.",
            "No dividends, no liquidity, no ST limits, no intraday stop gaps are modelled.",
            "The test window is not 'the future' — it is history the parameters did not see.",
        ],
        "note": "Neither window may be used to retune. A bad held-out result is a finding, not a prompt.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{args.variant}")
    print(f"  train {args.train_window}: return {train_return} bps, "
          f"dd {train_summary['max_drawdown_bps']} bps, fills {train_summary['fills']} "
          f"(settled {train_summary['first_day']}..{train_summary['last_day']})")
    print(f"  test  {args.test_window}: return {test_return} bps, "
          f"dd {test_summary['max_drawdown_bps']} bps, fills {test_summary['fills']} "
          f"(settled {test_summary['first_day']}..{test_summary['last_day']})")
    print(f"  verdict: {verdict} — {why}")
    if not verification["valid"]:
        # A written-but-invalid report must not read as a success downstream.
        print(f"  INVALID: {len(verification['problems'])} declaration problem(s); "
              "no out-of-sample claim is supported", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
