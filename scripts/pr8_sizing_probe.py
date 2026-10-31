"""Offline PR-8 frozen replay; run unchanged on base and proposed checkout.

Uses real BUY/manual planning and disposable production-schema test fixtures.
Only quantity, sizing audit, rejection reasons and execution outcome are projected.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import statistics
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from unittest import mock  # noqa: E402

import manual_orders as MO  # noqa: E402
import paper_sizing as PSZ  # noqa: E402
import paper_trading as PT  # noqa: E402
from test_manual_order_kline_replay import _PlanHarness, _kline  # noqa: E402
from test_strategy_buy_commit_convergence import (  # noqa: E402
    CODE, DAY, DAY_PREV, _BuyCase, _quote,
)


PROFILE = {"single_risk": .01, "max_weight": .1,
           "max_exposure": .65, "max_industry": .3}
BASE = dict(nav=100000., cash=50000., position_value=0., industry_value=0.,
            code_value=0., fill_price=10., hard_stop=-.05, profile=PROFILE)
CASES = {
    "normal": {}, "price_zero": {"fill_price": 0.},
    "price_negative": {"fill_price": -1.},
    "lot_below": {"cash": 999.99}, "lot_exact": {"cash": 1000.},
    "lot_above": {"cash": 1999.99}, "cash_zero": {"cash": 0.},
    "cash_negative": {"cash": -1.}, "price_rise": {"fill_price": 10.01},
    "risk": {"hard_stop": -.5}, "weight": {"code_value": 9500.},
    "industry": {"industry_value": 29500.},
    "pool": {"pool_cap_amount": 10000., "pending_pool_amount": 9500.},
    "strategy": {"strategy_cap_amount": 10000., "strategy_position_value": 9000.,
                 "pending_strategy_amount": 500.},
    "liquidity": {"liquidity_cap_amount": 1999.99},
    "absolute_cap": {"single_position_max_amount": 1999.99},
    "exposure_scale": {"exposure_scale": .001},
}


def replay():
    frozen = {"base_sha": "e622955deb702ae65e101ce260a0e3bea2e0a2f0",
              "unit": [], "automatic": [], "manual": []}
    for name, changes in CASES.items():
        inputs = {**BASE, **changes}
        qty, detail = PSZ.price_aware_qty(**inputs, num=PT._num)
        frozen["unit"].append({"name": name, "inputs": inputs,
                               "qty": qty, "sizing": detail})
    for name, price, payload in (
        ("normal", 10., {}), ("higher_price", 100., {}),
        ("too_expensive", 10000., {}),
        ("sliced", 10., {"entry_slice_policy": 3}),
        ("continuation", 10., {"entry_slice_policy": 3,
                               "entry_slices": {"plan": [100, 200, 300],
                                                "filled": 1, "day": DAY.isoformat()}}),
        ("intent_qty_forbidden", 10., {"pick": {
            "code": CODE, "score": .8, "price": 10., "qty": 100}}),
    ):
        harness = _BuyCase()
        harness.setUp()
        try:
            harness.quotes[CODE] = _quote(CODE, price=price)
            body = {"pick": {"code": CODE, "score": .8, "price": price},
                    "decision": {"entry_model": {"score": 90.}}, **payload}
            signal = harness.add_signal(signal_date=DAY_PREV.isoformat(), payload=body)
            result, order = harness.run_buy(signal_id=signal)
            audit = PT._loads((order or {}).get("risk_payload"), {})
            frozen["automatic"].append({
                "name": name, "result": result,
                "order": {key: (order or {}).get(key) for key in (
                    "qty", "filled_qty", "status", "reason", "planned_price")},
                "sizing": audit.get("sizing"),
            })
        finally:
            harness.tearDown()
    harness = _PlanHarness()
    harness.setUp()
    try:
        quote = harness._quote()
        market = {"light": "green", "breadth": .5, "sentiment": "neutral",
                  "overseas": {"light": "green"}}
        with mock.patch.object(MO, "_completed_kline", return_value=_kline(harness.day)), \
                mock.patch.object(PT, "_build_portfolio_entry_plan",
                                  side_effect=harness._approved_allocation_plan), \
                mock.patch.object(PT.PRS, "capture_portfolio_runtime_snapshot",
                                  side_effect=harness._same_allocation_snapshot), \
                mock.patch.object(PT, "_entry_freeze_enabled", return_value=False), \
                mock.patch.object(PT, "_market_state", return_value=market):
            for qty in (0, -100, 1, 99, 100, 101, 100.9, 100000000, "100", None):
                plan = MO._manual_order_plan(
                    harness.conn, harness.account_id, harness.code, "buy", qty=qty,
                    asof_day=harness.day, quote=quote, all_quotes={harness.code: quote},
                    live_universe=[dict(quote)],
                )
                frozen["manual"].append({
                    "input_qty": qty,
                    **{key: plan[key] for key in ("qty", "requested_qty", "recommended_qty",
                                                 "reasons", "allowed", "status")},
                    "sizing": plan["risk"].get("sizing"),
                })
    finally:
        harness.tearDown()
        harness.doCleanups()
    return frozen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--benchmark-output", type=Path)
    parser.add_argument("--benchmark-base", type=Path,
                        help="Verified base paper_sizing.py; alternate same-process batches")
    args = parser.parse_args()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = replay()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                           + "\n", encoding="utf-8")
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        if baseline != result:
            for group in ("unit", "automatic", "manual"):
                for before, after in zip(baseline[group], result[group], strict=True):
                    if before != after:
                        print(group, before.get("name", before.get("input_qty")))
            raise SystemExit("frozen replay differs")
    if args.benchmark_output:
        modules = {"current": PSZ}
        if args.benchmark_base:
            spec = importlib.util.spec_from_file_location("pr8_baseline_sizing", args.benchmark_base)
            baseline = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = baseline
            spec.loader.exec_module(baseline)
            modules["base"] = baseline
        entries = {key: getattr(module, "entry_quantity", module.price_aware_qty)
                   for key, module in modules.items()}
        call_counts = {}
        for key, module in modules.items():
            counts = {}

            def profile(frame, event, arg, module=module, counts=counts):
                if event == "call" and frame.f_code.co_filename == module.__file__:
                    name = frame.f_code.co_name
                    counts[name] = counts.get(name, 0) + 1

            sys.setprofile(profile)
            entries[key](**BASE, num=PT._num)
            sys.setprofile(None)
            call_counts[key] = counts
            for _ in range(1000):
                entries[key](**BASE, num=PT._num)
        samples = {key: [] for key in entries}
        for batch in range(30):
            order = list(entries.items())
            if batch % 2:
                order.reverse()
            for key, entry in order:
                start = time.perf_counter_ns()
                for _ in range(1000):
                    entry(**BASE, num=PT._num)
                samples[key].append((time.perf_counter_ns() - start) / 1000 / 1000)
        measured = {
            key: {"unit": "microseconds_per_call", "batches": 30, "calls_per_batch": 1000,
                  "p50": statistics.median(values), "p95": sorted(values)[28],
                  "min": min(values), "max": max(values), "samples": values,
                  "function_calls_per_quantity": call_counts[key]}
            for key, values in samples.items()
        }
        args.benchmark_output.write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")
    outcome = "match" if args.compare else "recorded"
    print(f"PR-8 replay: {len(result['unit'])} unit + {len(result['automatic'])} automatic"
          f" + {len(result['manual'])} manual samples {outcome}")


if __name__ == "__main__":
    main()
