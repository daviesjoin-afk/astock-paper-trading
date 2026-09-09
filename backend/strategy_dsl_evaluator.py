# -*- coding: utf-8 -*-
"""Pure, deterministic evaluator for the declarative strategy DSL."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from strategy_dsl_schema import FINANCIAL_FIELDS, PRICE_FIELDS, StrategyDslValidationError, normalize


class StrategyDslEvaluationError(ValueError):
    """A valid AST cannot be resolved from the supplied offline factor snapshot."""


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _series(snapshot: Mapping[str, Any], name: str) -> list[float | None]:
    value = snapshot.get(name)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)):
        return [_finite(item) for item in value]
    scalar = _finite(value)
    return [scalar] if scalar is not None else []


def _at(values: list[float | None], index: int) -> float | None:
    try:
        return values[index]
    except IndexError:
        return None


def _field(snapshot: Mapping[str, Any], name: str, index: int) -> float | None:
    if name in PRICE_FIELDS:
        return _at(_series(snapshot, name), index)
    section = "financials" if name in FINANCIAL_FIELDS else "fund_flow"
    values = snapshot.get(section)
    if isinstance(values, Mapping) and name in values:
        return _at(_series(values, name), index)
    return _at(_series(snapshot, name), index)


def _window(values: list[float | None], index: int, size: int) -> list[float] | None:
    end = len(values) + index + 1 if index < 0 else index + 1
    start = end - size
    if start < 0 or end > len(values):
        return None
    sliced = values[start:end]
    return None if any(value is None for value in sliced) else [float(value) for value in sliced]


def _ema(values: list[float | None], index: int, size: int) -> float | None:
    window = _window(values, index, size)
    if window is None:
        return None
    result = window[0]
    alpha = 2.0 / (size + 1.0)
    for value in window[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values: list[float | None], index: int, size: int) -> float | None:
    window = _window(values, index, size + 1)
    if window is None:
        return None
    changes = [window[i] - window[i - 1] for i in range(1, len(window))]
    gains = sum(max(change, 0.0) for change in changes) / size
    losses = sum(max(-change, 0.0) for change in changes) / size
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _atr(snapshot: Mapping[str, Any], index: int, size: int) -> float | None:
    highs, lows, closes = (_series(snapshot, name) for name in ("high", "low", "close"))
    end = len(closes) + index + 1 if index < 0 else index + 1
    start = end - size
    if start <= 0 or end > len(highs) or end > len(lows) or end > len(closes):
        return None
    true_ranges = []
    for i in range(start, end):
        high, low, previous = highs[i], lows[i], closes[i - 1]
        if high is None or low is None or previous is None:
            return None
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return sum(true_ranges) / size


def _value(node: Mapping[str, Any], snapshot: Mapping[str, Any], index: int) -> float | None:
    op = node["op"]
    if op == "const":
        return float(node["value"])
    if op == "field":
        return _field(snapshot, node["name"], index)
    if op == "mul":
        left, right = _value(node["left"], snapshot, index), _value(node["right"], snapshot, index)
        return None if left is None or right is None else left * right
    if op != "indicator":
        raise StrategyDslEvaluationError(f"{op} is not a scalar expression")
    name, window = node["name"], node["window"]
    if name == "atr":
        return _atr(snapshot, index, window)
    source = _series(snapshot, "volume" if name == "volume_mean" else "close")
    values = _window(source, index, window)
    if name in {"ma", "volume_mean"}:
        return None if values is None else sum(values) / window
    if name == "ema":
        return _ema(source, index, window)
    return _rsi(source, index, window)


def _evaluate(node: Mapping[str, Any], snapshot: Mapping[str, Any], index: int) -> bool | None:
    op = node["op"]
    if op == "and":
        values = [_evaluate(item, snapshot, index) for item in node["args"]]
        return False if False in values else (None if None in values else True)
    if op == "or":
        values = [_evaluate(item, snapshot, index) for item in node["args"]]
        return True if True in values else (None if None in values else False)
    if op == "not":
        value = _evaluate(node["arg"], snapshot, index)
        return None if value is None else not value
    if op == "mul":
        raise StrategyDslEvaluationError("arithmetic is not a boolean expression")
    left, right = _value(node["left"], snapshot, index), _value(node["right"], snapshot, index)
    if left is None or right is None:
        return None
    if op == "gt": return left > right
    if op == "gte": return left >= right
    if op == "lt": return left < right
    if op == "lte": return left <= right
    previous_left = _value(node["left"], snapshot, index - 1)
    previous_right = _value(node["right"], snapshot, index - 1)
    if previous_left is None or previous_right is None:
        return None
    if op == "cross_above":
        return previous_left <= previous_right and left > right
    if op == "cross_below":
        return previous_left >= previous_right and left < right
    raise StrategyDslEvaluationError(f"unsupported validated op: {op}")


def evaluate(ast: Mapping[str, Any], factor_snapshot: Mapping[str, Any]) -> bool:
    """Evaluate only a validated AST against an in-memory/offline snapshot.

    Missing/insufficient values fail closed as ``False``.  This function has no
    imports of strategy code, no network access, and no dynamic execution.
    """
    if not isinstance(factor_snapshot, Mapping):
        raise StrategyDslEvaluationError("factor snapshot must be an object")
    try:
        normalized = normalize(ast)
    except StrategyDslValidationError:
        raise
    # Unknown data propagates through boolean composition and only becomes a
    # fail-closed False at the public boundary.  In particular, ``not`` may
    # never turn unavailable evidence into a trade signal.
    return _evaluate(normalized, factor_snapshot, -1) is True
