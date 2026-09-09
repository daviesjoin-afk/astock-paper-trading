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


def _normalize(node: Any, *, depth: int, counter: list[int]) -> tuple[dict[str, Any], str]:
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
            arg, arg_type = _normalize(raw.get("arg"), depth=depth + 1, counter=counter)
            if arg_type != "boolean":
                raise StrategyDslValidationError("not requires a boolean arg")
            return {"op": op, "arg": arg}, "boolean"
        _only_keys(raw, {"op", "args"})
        args = raw.get("args")
        if not isinstance(args, Sequence) or isinstance(args, (str, bytes)) or len(args) < 2:
            raise StrategyDslValidationError(f"{op} requires at least two args")
        normalized_args = []
        for item in args:
            normalized, expression_type = _normalize(item, depth=depth + 1, counter=counter)
            if expression_type != "boolean":
                raise StrategyDslValidationError(f"{op} requires boolean args")
            normalized_args.append(normalized)
        return {"op": op, "args": normalized_args}, "boolean"
    if op in COMPARISONS | CROSS_OPS | ARITHMETIC_OPS:
        _only_keys(raw, {"op", "left", "right"})
        left, left_type = _normalize(raw.get("left"), depth=depth + 1, counter=counter)
        right, right_type = _normalize(raw.get("right"), depth=depth + 1, counter=counter)
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
    normalized, expression_type = _normalize(ast, depth=1, counter=[0])
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
