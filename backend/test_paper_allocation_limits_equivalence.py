# -*- coding: utf-8 -*-
"""R34-B STEP 7: differential proof that the slot-allocation refactor is equivalent.

``paper_allocation.position_limits()`` was split into a legacy adapter plus the
weight-agnostic core :func:`paper_allocation.position_limits_from_weights`.
This module keeps the **pre-refactor arithmetic verbatim** as a reference
implementation and proves, over a deterministic sweep, that both the legacy
public entry point and the new core reproduce it exactly:

* 0 / 1 / N strategies
* varied weights, caps, mins
* ``account_order`` absent / partial / full
* varied ``protected_slot_floor``, ``strategy_max_positions``,
  ``strategy_min_positions``, ``baseline_exposure``, ``hard_pool_cap``

compared field by field on ``total_cap``, ``risk_scale``,
``protected_slot_floor``, ``limits`` and ``effective_weights``.

The reference is intentionally a copy rather than a wrapper: a wrapper would
inherit the change under test and prove nothing. It is a *test* reference only
and is never imported by production code.
"""
from __future__ import annotations

import os
import random
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_allocation as PA


def _reference_runtime_map(runtimes):
    """Pre-refactor resolution: first occurrence wins, empty ids dropped."""
    result = {}
    for runtime in list(runtimes or ()):
        key = str(runtime.strategy_id or "")
        if key:
            result.setdefault(key, runtime)
    return result


def _reference_weights(runtimes):
    runtime_map = _reference_runtime_map(runtimes)
    ids = sorted(runtime_map)
    return {key: runtime_map[key].effective_weight() for key in ids}


def _reference_caps(runtimes, strategy_max_positions):
    runtime_map = _reference_runtime_map(runtimes)
    ids = sorted(runtime_map)
    return {
        key: max(
            1,
            int(
                runtime_map[key].max_positions
                if runtime_map[key].max_positions is not None
                else strategy_max_positions
            ),
        )
        for key in ids
    }


def _reference_mins(runtimes, strategy_min_positions):
    runtime_map = _reference_runtime_map(runtimes)
    ids = sorted(runtime_map)
    return {
        key: max(
            0,
            int(
                runtime_map[key].min_positions
                if runtime_map[key].min_positions is not None
                else strategy_min_positions
            ),
        )
        for key in ids
    }


def _reference_slot_arithmetic(
    weights,
    caps,
    mins,
    *,
    hard_pool_cap,
    strategy_max_positions,
    strategy_min_positions,
    protected_slot_floor,
    account_order=None,
    baseline_exposure=None,
):
    """Verbatim copy of the pre-refactor ``position_limits`` arithmetic core."""
    ids = sorted(weights)
    count = len(ids)
    if count == 0:
        return {
            "engine": PA.ALLOCATION_ENGINE_VERSION,
            "risk_scale": 1.0,
            "protected_slot_floor": 0,
            "total_cap": 0,
            "limits": {},
            "effective_weights": {},
        }
    order = {key: int(value) for key, value in (account_order or {}).items()}
    hard_cap = min(int(hard_pool_cap), sum(caps.values()))
    if baseline_exposure is None:
        baseline = sum(weights.values()) / count
    else:
        baseline = float(baseline_exposure)
    current = sum(weights.values()) / count
    risk_scale = max(0.60, min(1.0, current / max(baseline, 0.01)))
    base_floor_total = min(hard_cap, strategy_min_positions * count)
    total_cap = max(base_floor_total, min(hard_cap, int(round(hard_cap * risk_scale))))
    total_cap = min(total_cap, hard_cap)
    protected = (
        min(int(protected_slot_floor), strategy_max_positions)
        if total_cap >= int(protected_slot_floor) * count
        else strategy_min_positions
    )
    minimum = {
        key: min(mins[key], caps[key], total_cap // count if count else 0)
        for key in ids
    }
    while sum(minimum.values()) > total_cap and any(minimum[key] > 0 for key in ids):
        heaviest = max(ids, key=lambda item: (minimum[item], -order.get(item, 99), item))
        minimum[heaviest] -= 1
    weight_total = sum(weights.values()) or 1.0
    raw = {key: total_cap * weights[key] / weight_total for key in ids}
    limits = {key: max(minimum[key], min(caps[key], int(raw[key]))) for key in ids}
    while sum(limits.values()) < total_cap:
        candidates = [key for key in ids if limits[key] < caps[key]]
        if not candidates:
            break
        key = max(
            candidates,
            key=lambda item: (
                raw[item] - limits[item], weights[item], -order.get(item, 99), item
            ),
        )
        limits[key] += 1
    while sum(limits.values()) > total_cap:
        candidates = [key for key in ids if limits[key] > minimum[key]]
        if not candidates:
            break
        key = max(
            candidates,
            key=lambda item: (
                limits[item] - raw[item], -weights[item], order.get(item, 99), item
            ),
        )
        limits[key] -= 1
    while sum(limits.values()) > hard_cap:
        removable = [key for key in ids if limits[key] > minimum[key]]
        if not removable:
            removable = [key for key in ids if limits[key] > 0]
            if not removable:
                break
        key = max(removable, key=lambda item: (limits[item], order.get(item, 99), item))
        limits[key] -= 1
    return {
        "engine": PA.ALLOCATION_ENGINE_VERSION,
        "risk_scale": risk_scale,
        "protected_slot_floor": protected,
        "total_cap": total_cap,
        "limits": limits,
        "effective_weights": {key: round(value, 6) for key, value in weights.items()},
    }


def _reference_from_runtimes(runtimes, **kwargs):
    return _reference_slot_arithmetic(
        _reference_weights(runtimes),
        _reference_caps(runtimes, kwargs["strategy_max_positions"]),
        _reference_mins(runtimes, kwargs["strategy_min_positions"]),
        **kwargs,
    )


COMPARED_KEYS = ("engine", "risk_scale", "protected_slot_floor", "total_cap",
                 "limits", "effective_weights")


def _make_runtime(index, rng):
    """Build a StrategyRuntime with declared caps and arbitrary factor values."""
    factors = {name: rng.choice([0.0, 0.25, 0.5, 1.0, 1.0, 1.0])
               for name in PA.FACTOR_FIELDS}
    return PA.StrategyRuntime(
        strategy_id=f"s{index:02d}",
        max_positions=rng.choice([None, None, 0, 1, 2, 3, 8]),
        min_positions=rng.choice([None, None, 0, 1, 2]),
        priority_floor_pct=rng.choice([None, 0.05, 0.2]),
        own_exposure_cap_pct=rng.choice([None, 0.3, 0.65]),
        lifecycle_stage=rng.choice(list(PA.LIFECYCLE_STAGES)),
        **factors,
    )


class SlotAllocationEquivalenceTests(unittest.TestCase):
    """Both the legacy entry point and the core must match the reference."""

    def _assert_same(self, expected, actual, context):
        for key in COMPARED_KEYS:
            self.assertEqual(expected[key], actual[key],
                             msg=f"{key} diverged for {context}")

    def test_zero_strategies(self):
        kwargs = dict(hard_pool_cap=6, strategy_max_positions=6,
                      strategy_min_positions=0, protected_slot_floor=2)
        for runtimes in (None, [], ()):
            self._assert_same(_reference_from_runtimes(runtimes, **kwargs),
                              PA.position_limits(runtimes, **kwargs), f"zero {runtimes!r}")

    def test_single_strategy(self):
        rng = random.Random(11)
        for _ in range(40):
            runtimes = [_make_runtime(1, rng)]
            kwargs = dict(
                hard_pool_cap=rng.choice([0, 1, 3, 6, 12]),
                strategy_max_positions=rng.choice([3, 6]),
                strategy_min_positions=rng.choice([0, 1, 2]),
                protected_slot_floor=rng.choice([0, 1, 2, 5]),
                account_order=rng.choice([None, {"s01": 0}, {"s01": 7}]),
                baseline_exposure=rng.choice([None, 0.2, 1.0, 3.0]),
            )
            self._assert_same(_reference_from_runtimes(runtimes, **kwargs),
                              PA.position_limits(runtimes, **kwargs), f"single {kwargs}")

    def test_sweep_over_n_and_inputs(self):
        rng = random.Random(20261002)
        checked = 0
        for n in (2, 3, 4, 5, 7):
            for _ in range(60):
                runtimes = [_make_runtime(index, rng) for index in range(1, n + 1)]
                ids = [runtime.strategy_id for runtime in runtimes]
                case = rng.random()
                if case < 0.34:
                    account_order = None
                elif case < 0.67:
                    account_order = {ids[0]: rng.randint(0, 9)}
                else:
                    account_order = {key: rng.randint(0, 9) for key in ids}
                kwargs = dict(
                    hard_pool_cap=rng.choice([0, 1, 2, 5, 8, 14, 21, 40]),
                    strategy_max_positions=rng.choice([2, 3, 6]),
                    strategy_min_positions=rng.choice([0, 1, 2, 3]),
                    protected_slot_floor=rng.choice([0, 1, 2, 3, 6]),
                    account_order=account_order,
                    baseline_exposure=rng.choice([None, 0.1, 0.5, 1.0, 2.5]),
                )
                context = f"n={n} {kwargs} runtimes={[(r.strategy_id, r.max_positions, r.min_positions) for r in runtimes]}"
                self._assert_same(_reference_from_runtimes(runtimes, **kwargs),
                                  PA.position_limits(runtimes, **kwargs), context)
                # The weight-agnostic core fed the same resolved inputs must match too.
                self._assert_same(
                    _reference_slot_arithmetic(
                        _reference_weights(runtimes),
                        _reference_caps(runtimes, kwargs["strategy_max_positions"]),
                        _reference_mins(runtimes, kwargs["strategy_min_positions"]),
                        **kwargs),
                    PA.position_limits_from_weights(
                        _reference_weights(runtimes),
                        _reference_caps(runtimes, kwargs["strategy_max_positions"]),
                        _reference_mins(runtimes, kwargs["strategy_min_positions"]),
                        **kwargs),
                    f"core {context}")
                checked += 1
        self.assertGreaterEqual(checked, 300)

    def test_duplicate_ids_and_empty_ids_match_reference(self):
        rng = random.Random(5)
        first = _make_runtime(1, rng)
        duplicate = PA.StrategyRuntime(strategy_id="s01", base_priority=0.1, max_positions=9)
        blank = PA.StrategyRuntime(strategy_id="", max_positions=4)
        runtimes = [first, duplicate, blank, _make_runtime(2, rng)]
        kwargs = dict(hard_pool_cap=10, strategy_max_positions=6,
                      strategy_min_positions=1, protected_slot_floor=2)
        self._assert_same(_reference_from_runtimes(runtimes, **kwargs),
                          PA.position_limits(runtimes, **kwargs), "duplicates")

    def test_runtime_input_order_does_not_change_legacy_result(self):
        rng = random.Random(77)
        runtimes = [_make_runtime(index, rng) for index in range(1, 6)]
        kwargs = dict(hard_pool_cap=11, strategy_max_positions=6,
                      strategy_min_positions=1, protected_slot_floor=2,
                      account_order={"s02": 0})
        expected = PA.position_limits(runtimes, **kwargs)
        for seed in range(12):
            shuffled = list(runtimes)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(expected, PA.position_limits(shuffled, **kwargs))

    def test_canonical_weight_dict_order_does_not_change_core_result(self):
        rng = random.Random(303)
        weights = {f"s{index:02d}": rng.choice([0.3, 0.75, 1.0, 1.4])
                   for index in range(1, 7)}
        caps = {key: rng.choice([1, 2, 3, 5]) for key in weights}
        mins = {key: rng.choice([0, 1, 2]) for key in weights}
        kwargs = dict(hard_pool_cap=12, strategy_max_positions=6,
                      strategy_min_positions=1, protected_slot_floor=2)
        expected = PA.position_limits_from_weights(weights, caps, mins, **kwargs)
        items = list(weights.items())
        for seed in range(12):
            random.Random(seed).shuffle(items)
            shuffled = dict(items)
            self.assertEqual(
                expected,
                PA.position_limits_from_weights(shuffled, caps, mins, **kwargs))

    def test_invariants_hold_across_the_sweep(self):
        rng = random.Random(4242)
        for n in (1, 2, 3, 6, 9):
            runtimes = [_make_runtime(index, rng) for index in range(1, n + 1)]
            kwargs = dict(hard_pool_cap=rng.choice([3, 7, 15, 30]),
                          strategy_max_positions=6, strategy_min_positions=1,
                          protected_slot_floor=2)
            result = PA.position_limits(runtimes, **kwargs)
            caps = _reference_caps(runtimes, kwargs["strategy_max_positions"])
            total = sum(result["limits"].values())
            self.assertLessEqual(total, kwargs["hard_pool_cap"])
            self.assertLessEqual(total, sum(caps.values()))
            self.assertEqual(total, result["total_cap"])
            for key, value in result["limits"].items():
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, caps[key])
            self.assertGreaterEqual(result["risk_scale"], 0.60)
            self.assertLessEqual(result["risk_scale"], 1.0)

    def test_core_is_weight_agnostic_and_never_reads_runtime_factors(self):
        """The core must depend only on the weights it is handed."""
        caps = {"a": 4, "b": 4}
        mins = {"a": 0, "b": 0}
        kwargs = dict(hard_pool_cap=6, strategy_max_positions=4,
                      strategy_min_positions=0, protected_slot_floor=0)
        explicit = PA.position_limits_from_weights({"a": 1.0, "b": 1.0}, caps, mins, **kwargs)
        runtimes = [
            PA.StrategyRuntime(strategy_id="a", health=0.0, regime_fit=0.0),
            PA.StrategyRuntime(strategy_id="b", confidence=0.0, data_quality=0.0,
                               diversification=0.0),
        ]
        legacy = PA.position_limits(runtimes, **kwargs)
        # Legacy still applies its dynamic factors (weights become 0 and 1e-4-ish).
        self.assertNotEqual(explicit["effective_weights"], legacy["effective_weights"])
        # The core result is exactly the explicit-weight result, untouched by factors.
        self.assertEqual(
            explicit,
            PA.position_limits_from_weights({"b": 1.0, "a": 1.0}, caps, mins, **kwargs))


if __name__ == "__main__":
    unittest.main()
