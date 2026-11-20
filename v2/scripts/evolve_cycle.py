#!/usr/bin/env python3
"""Close the loop: propose, measure, audit, decide -- and check every step happened.

The project's stated goal is that the system iterates and can be driven by an AI
without a human hand-moving parameters. Talking about that is worthless; what can
be checked is whether a candidate change went through every gate before it was
allowed to stay:

1. **proposed** -- the change exists as a named hypothesis with an expected effect
2. **power-checked** -- the expected effect clears the measurement floor, or the
   experiment is refused *before* it is run
3. **measured** -- a paired A/B over the same folds, same database
4. **audited** -- an independent implementation rebuilds the ledger and agrees
5. **isolated** -- the fill is priced no earlier than the moment the decision
   could read, so the change cannot borrow from the future
6. **decided** -- adopted only on a 95% interval excluding zero; otherwise reverted

This script does not run the experiment. It reads the record of one and reports
which gates were satisfied, refusing to call a cycle complete when a gate is
missing. The point is that an incomplete cycle cannot be presented as a finished
one -- by a human or by an AI driving the loop.

Usage:
    python scripts/evolve_cycle.py --record cycle.json --out verdict.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

Z95 = 1.96


def check_power(expected_bps: float, spread_bps: float, folds: int) -> dict:
    """Was the experiment's expected effect large enough to resolve at all?

    `spread_bps` must be the spread of the quantity actually tested. For a
    paired A/B that is the spread of the *paired differences*, which is far
    smaller than the spread of raw fold returns -- pairing removes the
    fold-to-fold level that would otherwise swamp the effect. Passing the raw
    spread here makes a perfectly resolvable experiment look hopeless: the stop
    fill had a paired spread of 31 bps against raw fold spread of 507, giving a
    13 bps floor rather than 212.
    """
    if folds <= 0:
        return {"ok": False, "why": "no folds"}
    if spread_bps <= 0:
        return {"ok": False, "why": "no spread recorded for the tested quantity"}
    floor = Z95 * spread_bps / folds ** 0.5
    return {
        "ok": expected_bps >= floor,
        "expected_bps": expected_bps,
        "floor_bps": round(floor, 1),
        "why": (
            f"expected {expected_bps} bps against a {floor:.1f} bps floor"
            + ("" if expected_bps >= floor
               else " -- this A/B could not have returned a positive verdict")
        ),
    }


def check_measurement(paired: list[float]) -> dict:
    """A paired A/B must report an interval, not just a mean."""
    if not paired:
        return {"ok": False, "why": "no paired observations"}
    mean = sum(paired) / len(paired)
    if len(paired) < 2:
        return {"ok": False, "why": "a single fold has no spread"}
    variance = sum((x - mean) ** 2 for x in paired) / (len(paired) - 1)
    standard_error = (variance / len(paired)) ** 0.5
    lo = mean - Z95 * standard_error
    hi = mean + Z95 * standard_error
    return {
        "ok": True,
        "mean_bps": round(mean, 1),
        "ci95": [round(lo, 1), round(hi, 1)],
        "folds_improved": sum(1 for x in paired if x > 0),
        "folds": len(paired),
        "excludes_zero": lo > 0 or hi < 0,
        "positive": lo > 0,
    }


def check_isolation(record: dict) -> dict:
    """Did the change use information that was not available when it decided?

    The stop-fill experiment passed every other gate and was still wrong: it
    decided from the session's close and then filled at an intraday price the
    decision could not have seen. That is look-ahead, and no amount of power,
    measurement or independent auditing detects it -- the ledger is perfectly
    consistent, it just describes an impossible sequence.

    So a cycle must state both halves explicitly:

    * `decided_at` -- the last moment the decision was allowed to read
    * `filled_at`  -- the moment the fill is priced from

    A fill priced at or before the decision moment is fine (a resting order
    that was already in the book). A fill priced *earlier* than the decision
    read is a look-ahead and fails, regardless of what the numbers say.
    """
    decided_at = record.get("decided_at")
    filled_at = record.get("filled_at")
    order = {"prior_close": 0, "session_open": 1, "session_close": 2, "intraday_line": 1}
    if not decided_at or not filled_at:
        return {
            "ok": False,
            "why": "the record does not say when it decided and when it filled",
        }
    if decided_at not in order or filled_at not in order:
        return {"ok": False, "why": f"unknown moment: {decided_at!r} / {filled_at!r}"}
    ok = order[filled_at] >= order[decided_at] or filled_at == decided_at
    return {
        "ok": ok,
        "decided_at": decided_at,
        "filled_at": filled_at,
        "why": (
            f"decided at {decided_at}, filled at {filled_at}"
            + ("" if ok else " -- the fill is priced before the decision could read it")
        ),
    }


def evaluate(record: dict) -> dict:
    """Walk the six gates and report which ones the record actually satisfies."""
    gates = {}

    gates["isolated"] = check_isolation(record)

    gates["proposed"] = {
        "ok": bool(record.get("hypothesis")),
        "why": record.get("hypothesis") or "no hypothesis named",
    }

    power = record.get("power") or {}
    if not power:
        gates["power_checked"] = {"ok": False, "why": "power was never computed"}
    else:
        gates["power_checked"] = check_power(
            power.get("expected_bps", 0),
            # Prefer the paired spread; fall back to a raw spread only if the
            # record does not say it was paired.
            power.get("paired_stdev_bps") or power.get("stdev_bps", 0),
            power.get("folds", 0),
        )

    paired = record.get("paired_bps") or []
    measurement = check_measurement(paired)
    gates["measured"] = measurement

    audit = record.get("audit") or {}
    gates["audited"] = {
        "ok": bool(audit.get("passed")) and audit.get("independent", False),
        "why": (
            f"{audit.get('passed_folds', 0)}/{audit.get('folds', 0)} folds "
            "rebuilt independently"
            if audit else "no independent audit recorded"
        ),
    }

    decision = record.get("decision") or {}
    if measurement.get("ok") and measurement.get("excludes_zero"):
        expected = "adopt" if measurement["positive"] else "revert"
    else:
        expected = "revert"
    gates["decided"] = {
        "ok": decision.get("action") == expected,
        "action": decision.get("action"),
        "should_be": expected,
        "why": (
            f"the interval {'excludes' if measurement.get('excludes_zero') else 'includes'} "
            f"zero, so the honest action is `{expected}`"
        ),
    }

    complete = all(gate.get("ok") for gate in gates.values())
    return {
        "gates": gates,
        "complete": complete,
        "missing": [name for name, gate in gates.items() if not gate.get("ok")],
        "note": (
            "A cycle is complete only when every gate passes. A change measured "
            "without a power check, or adopted without an independent audit, is "
            "not evidence -- it is a claim."
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    record = json.loads(args.record.read_text(encoding="utf-8"))
    verdict = evaluate(record)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    for name, gate in verdict["gates"].items():
        mark = "PASS" if gate.get("ok") else "FAIL"
        print(f"  [{mark}] {name:14s} {gate.get('why','')}")
    print(f"complete: {verdict['complete']}")
    if verdict["missing"]:
        print(f"missing: {', '.join(verdict['missing'])}")
    return 0 if verdict["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
