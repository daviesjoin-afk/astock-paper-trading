# -*- coding: utf-8 -*-
"""Strict, declarative schema for executable strategy rules.

The DSL intentionally has no names, calls, attributes, or executable source
language.  It is a small JSON AST that can be stored and hashed safely.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

MAX_AST_DEPTH = 12
MAX_AST_NODES = 128
MAX_ROLLING_WINDOW = 250

PRICE_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount"})
FINANCIAL_FIELDS = frozenset({
    "pe", "pb", "roe", "revenue_yoy", "profit_yoy", "gross_margin", "debt_ratio",
})
FLOW_FIELDS = frozenset({
    "main_net_inflow", "main_net_inflow_pct", "northbound_net_inflow", "turnover_rate",
})
FIELD_NAMES = PRICE_FIELDS | FINANCIAL_FIELDS | FLOW_FIELDS
INDICATORS = frozenset({"ma", "ema", "rsi", "atr", "volume_mean"})
COMPARISONS = frozenset({"gt", "gte", "lt", "lte"})
BOOLEAN_OPS = frozenset({"and", "or", "not"})
CROSS_OPS = frozenset({"cross_above", "cross_below"})
ARITHMETIC_OPS = frozenset({"mul"})
PARAMETER_TYPES = frozenset({"integer", "number"})
PARAMETER_IDS = frozenset({
    "ma_period", "rsi_threshold", "volume_multiplier", "atr_stop_multiplier",
    "holding_days", "entry_threshold", "risk_per_trade", "entry_slices",
})
RISK_DIRECTIONS = frozenset({"higher_is_riskier", "lower_is_riskier", "neutral"})


class StrategyDslValidationError(ValueError):
    """Raised for malformed, over-complex, or non-declarative DSL ASTs."""


def _number(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyDslValidationError(f"{label} must be a finite number")
    if not math.isfinite(float(value)):
        raise StrategyDslValidationError(f"{label} must be a finite number")
    return value


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StrategyDslValidationError(f"{label} must be an object")
    return value


def _only_keys(node: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(node) - allowed
    if unknown:
        raise StrategyDslValidationError(f"unsupported DSL key: {sorted(unknown)[0]}")


def _parameter(node: Mapping[str, Any], seen_ids: set[str]) -> dict[str, Any]:
    _only_keys(node, {
        "op", "parameter_id", "type", "value", "min", "max", "max_step",
        "locked", "risk_direction", "min_evidence",
    })
    parameter_id = node.get("parameter_id")
    if not isinstance(parameter_id, str) or parameter_id not in PARAMETER_IDS:
        raise StrategyDslValidationError("DSL parameter_id is not allowlisted")
    if parameter_id in seen_ids:
        raise StrategyDslValidationError(f"duplicate DSL parameter_id: {parameter_id}")
    parameter_type = str(node.get("type") or "").strip().lower()
    if parameter_type == "float":
        parameter_type = "number"
    if parameter_type not in PARAMETER_TYPES:
        raise StrategyDslValidationError("DSL parameter type must be integer or number")
    value = _number(node.get("value"), "parameter value")
    minimum = _number(node.get("min"), "parameter min")
    maximum = _number(node.get("max"), "parameter max")
    max_step = _number(node.get("max_step"), "parameter max_step")
    if minimum > maximum or not minimum <= value <= maximum or max_step <= 0:
        raise StrategyDslValidationError("DSL parameter bounds or max_step are invalid")
    if parameter_type == "integer":
        for label, number in (("value", value), ("min", minimum), ("max", maximum), ("max_step", max_step)):
            if int(number) != number:
                raise StrategyDslValidationError(f"integer parameter {label} must be an integer")
        value, minimum, maximum, max_step = map(int, (value, minimum, maximum, max_step))
    locked = node.get("locked")
    if not isinstance(locked, bool):
        raise StrategyDslValidationError("DSL parameter locked must be a boolean")
    risk_direction = str(node.get("risk_direction") or "").strip().lower()
    if risk_direction not in RISK_DIRECTIONS:
        raise StrategyDslValidationError("DSL parameter risk_direction is invalid")
    min_evidence = node.get("min_evidence")
    if isinstance(min_evidence, bool) or not isinstance(min_evidence, int) or min_evidence < 0:
        raise StrategyDslValidationError("DSL parameter min_evidence must be a non-negative integer")
    seen_ids.add(parameter_id)
    return {
        "op": "parameter", "parameter_id": parameter_id, "type": parameter_type,
        "value": value, "min": minimum, "max": maximum, "max_step": max_step,
        "locked": locked, "risk_direction": risk_direction, "min_evidence": min_evidence,
    }


def _normalize(node: Any, *, depth: int, counter: list[int],
               seen_ids: set[str]) -> tuple[dict[str, Any], str]:
    if depth > MAX_AST_DEPTH:
        raise StrategyDslValidationError(f"DSL AST exceeds max depth {MAX_AST_DEPTH}")
    counter[0] += 1
    if counter[0] > MAX_AST_NODES:
        raise StrategyDslValidationError(f"DSL AST exceeds max node count {MAX_AST_NODES}")
    raw = _object(node, "DSL node")
    op = raw.get("op")
    if not isinstance(op, str):
        raise StrategyDslValidationError("DSL node op must be a string")
    op = op.strip().lower()

    if op == "strategy":
        _only_keys(raw, {"op", "rule", "parameters"})
        rule, expression_type = _normalize(
            raw.get("rule"), depth=depth + 1, counter=counter, seen_ids=seen_ids,
        )
        if expression_type != "boolean":
            raise StrategyDslValidationError("strategy rule must be a boolean expression")
        parameters = raw.get("parameters", [])
        if not isinstance(parameters, Sequence) or isinstance(parameters, (str, bytes)):
            raise StrategyDslValidationError("strategy parameters must be a list")
        normalized_parameters = []
        for item in parameters:
            parameter_raw = _object(item, "DSL parameter")
            if str(parameter_raw.get("op") or "").strip().lower() != "parameter":
                raise StrategyDslValidationError("strategy parameters must contain parameter nodes")
            counter[0] += 1
            if counter[0] > MAX_AST_NODES:
                raise StrategyDslValidationError(f"DSL AST exceeds max node count {MAX_AST_NODES}")
            normalized_parameters.append(_parameter(parameter_raw, seen_ids))
        return {"op": "strategy", "rule": rule, "parameters": normalized_parameters}, "boolean"
    if op == "parameter":
        return _parameter(raw, seen_ids), "scalar"
    if op == "const":
        _only_keys(raw, {"op", "value"})
        return {"op": "const", "value": _number(raw.get("value"), "const value")}, "scalar"
    if op == "field":
        _only_keys(raw, {"op", "name"})
        name = raw.get("name")
        if not isinstance(name, str) or name not in FIELD_NAMES:
            raise StrategyDslValidationError("DSL field is not allowlisted")
        return {"op": "field", "name": name}, "scalar"
    if op == "indicator":
        _only_keys(raw, {"op", "name", "window"})
        name = raw.get("name")
        if not isinstance(name, str) or name not in INDICATORS:
            raise StrategyDslValidationError("DSL indicator is not allowlisted")
        window = raw.get("window")
        if isinstance(window, Mapping):
            normalized_window, window_type = _normalize(
                window, depth=depth + 1, counter=counter, seen_ids=seen_ids,
            )
            if (window_type != "scalar" or normalized_window.get("op") != "parameter"
                    or normalized_window["type"] != "integer"):
                raise StrategyDslValidationError("indicator window parameter must be an integer parameter")
            if not 1 <= normalized_window["min"] <= normalized_window["max"] <= MAX_ROLLING_WINDOW:
                raise StrategyDslValidationError(
                    f"indicator window must be between 1 and {MAX_ROLLING_WINDOW}"
                )
            window = normalized_window
        else:
            if isinstance(window, bool) or not isinstance(window, int):
                raise StrategyDslValidationError("indicator window must be an integer")
            if not 1 <= window <= MAX_ROLLING_WINDOW:
                raise StrategyDslValidationError(
                    f"indicator window must be between 1 and {MAX_ROLLING_WINDOW}"
                )
        return {"op": "indicator", "name": name, "window": window}, "scalar"
    if op in BOOLEAN_OPS:
        if op == "not":
            _only_keys(raw, {"op", "arg"})
            arg, arg_type = _normalize(
                raw.get("arg"), depth=depth + 1, counter=counter, seen_ids=seen_ids,
            )
            if arg_type != "boolean":
                raise StrategyDslValidationError("not requires a boolean arg")
            return {"op": op, "arg": arg}, "boolean"
        _only_keys(raw, {"op", "args"})
        args = raw.get("args")
        if not isinstance(args, Sequence) or isinstance(args, (str, bytes)) or len(args) < 2:
            raise StrategyDslValidationError(f"{op} requires at least two args")
        normalized_args = []
        for item in args:
            normalized, expression_type = _normalize(
                item, depth=depth + 1, counter=counter, seen_ids=seen_ids,
            )
            if expression_type != "boolean":
                raise StrategyDslValidationError(f"{op} requires boolean args")
            normalized_args.append(normalized)
        return {"op": op, "args": normalized_args}, "boolean"
    if op in COMPARISONS | CROSS_OPS | ARITHMETIC_OPS:
        _only_keys(raw, {"op", "left", "right"})
        left, left_type = _normalize(
            raw.get("left"), depth=depth + 1, counter=counter, seen_ids=seen_ids,
        )
        right, right_type = _normalize(
            raw.get("right"), depth=depth + 1, counter=counter, seen_ids=seen_ids,
        )
        if left_type != "scalar" or right_type != "scalar":
            raise StrategyDslValidationError(f"{op} requires scalar operands")
        return {
            "op": op,
            "left": left,
            "right": right,
        }, "scalar" if op in ARITHMETIC_OPS else "boolean"
    raise StrategyDslValidationError(f"unsupported DSL op: {op or '<empty>'}")


def normalize(ast: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an AST without evaluating it."""
    normalized, expression_type = _normalize(ast, depth=1, counter=[0], seen_ids=set())
    if expression_type != "boolean":
        raise StrategyDslValidationError("DSL root must be a boolean expression")
    return normalized


def canonical_json(ast: Mapping[str, Any]) -> str:
    return json.dumps(normalize(ast), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checksum(ast: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(ast).encode("utf-8")).hexdigest()


def canonicalize(ast: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    normalized = normalize(ast)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()
