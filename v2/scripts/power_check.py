#!/usr/bin/env python3
"""Decide whether an experiment is worth running before running it.

Twenty-four changes were A/B tested and none reached significance. The reason
was never the experimental design; it was arithmetic. Effects ran 1-73 bps while
the 22-fold measurement system resolves no better than about 212 bps. An
experiment whose expected effect is a third of the noise floor wastes a run and
returns a foregone "not significant".

This script answers two questions up front:

* **Given the samples I have, what is the smallest effect I could detect?**
* **Given an effect I expect, how many samples would I need?**

Both use the same standard-error arithmetic, so the two directions agree.

Usage:
    python scripts/power_check.py --folds 22 --stdev-bps 507 --detect 50
    python scripts/power_check.py --folds 22 --stdev-bps 507 --effect 20 --need-samples
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


Z95 = 1.96


def detectable_effect(stdev_bps: float, samples: int, z: float = Z95) -> float:
    """Smallest effect this sample can distinguish from zero at 95%."""
    if samples <= 0:
        raise ValueError("samples must be positive")
    standard_error = stdev_bps / math.sqrt(samples)
    return z * standard_error


def required_samples(stdev_bps: float, effect_bps: float, z: float = Z95) -> int | None:
    """Samples needed to resolve `effect_bps`.

    Returns `None` when the effect is zero: no sample size distinguishes zero
    from zero, and returning a huge number would read as "just collect more".
    """
    if effect_bps <= 0:
        return None
    return math.ceil((z * stdev_bps / effect_bps) ** 2)


def verdict(effect_bps: float, detectable_bps: float) -> dict:
    """Whether an effect of this size is measurable with the samples available."""
    ratio = effect_bps / detectable_bps if detectable_bps > 0 else None
    if ratio is None:
        return {"measurable": False, "reason": "no noise estimate"}
    if ratio >= 1:
        return {
            "measurable": True,
            "ratio": round(ratio, 2),
            "reason": "the effect clears the noise floor; an A/B can resolve it",
        }
    return {
        "measurable": False,
        "ratio": round(ratio, 2),
        "reason": (
            f"the effect is {ratio:.2f}x the noise floor. Running this A/B would "
            "return 'not significant' regardless of implementation quality."
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdev-bps", type=float, required=True,
                        help="per-sample standard deviation of the metric")
    parser.add_argument("--folds", type=int, default=22,
                        help="samples available (folds, or trades for trade-level)")
    parser.add_argument("--detect", type=float, default=None,
                        help="report the smallest detectable effect at this sample size")
    parser.add_argument("--effect", type=float, default=None,
                        help="report how many samples this effect would need")
    parser.add_argument("--need-samples", action="store_true",
                        help="with --effect, print the sample requirement")
    parser.add_argument("--trading-days-available", type=int, default=933,
                        help="calendar depth available, for the feasibility line")
    parser.add_argument("--days-per-sample", type=int, default=30)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    report = {
        "samples": args.folds,
        "stdev_bps": args.stdev_bps,
        "z": Z95,
    }

    floor = detectable_effect(args.stdev_bps, args.folds)
    report["detectable_bps"] = round(floor, 1)

    if args.detect is not None:
        report["proposed_effect_bps"] = args.detect
        report["verdict"] = verdict(args.detect, floor)

    if args.effect is not None:
        needed = required_samples(args.stdev_bps, args.effect)
        report["effect_bps"] = args.effect
        report["samples_needed"] = needed
        if needed is not None:
            days = needed * args.days_per_sample
            report["trading_days_needed"] = days
            report["days_available"] = args.trading_days_available
            report["feasible"] = days <= args.trading_days_available
            if not report["feasible"]:
                report["shortfall_factor"] = round(days / args.trading_days_available, 1)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
