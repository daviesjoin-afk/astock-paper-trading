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
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_variant(path: Path, variant: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get(variant)
    if not data:
        raise SystemExit(f"{path} has no {variant} variant")
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
    }


def summarise(row: dict) -> dict:
    account = row["account"]
    return {
        "equity_cents": account.get("equity_cents"),
        "return_bps": account.get("return_bps"),
        "max_drawdown_bps": account.get("max_drawdown_bps"),
        "profit_cents": account.get("profit_cents"),
        "fills": row["fills"],
        "fees_cents": row["fees_cents"],
        "rebalances": row["rebalances"],
        "days": len(row["windows"]),
        "first_day": row["windows"][0]["day"] if row["windows"] else None,
        "last_day": row["windows"][-1]["day"] if row["windows"] else None,
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

    # The honest read: a held-out window is one sample. Agreement in sign is
    # weak support; disagreement is strong evidence against.
    train_return = train_summary["return_bps"]
    test_return = test_summary["return_bps"]
    if train_return is None or test_return is None:
        verdict = "incomplete"
        why = "one of the windows produced no account curve"
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
        "windows": windows,
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
          f"dd {train_summary['max_drawdown_bps']} bps, fills {train_summary['fills']}")
    print(f"  test  {args.test_window}: return {test_return} bps, "
          f"dd {test_summary['max_drawdown_bps']} bps, fills {test_summary['fills']}")
    print(f"  verdict: {verdict} — {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
