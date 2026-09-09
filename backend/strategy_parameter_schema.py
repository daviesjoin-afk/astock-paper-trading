"""First-class, immutable-safe parameter contracts for strategy DSLs.

Only explicit ``{"op": "parameter"}`` nodes can be changed.  The compiler
never accepts a replacement AST, so an evolution proposal cannot add/remove
conditions, change operators, or alter a field reference by accident.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

import strategy_dsl_schema as DSL


PARAMETER_SCHEMA_VERSION = "strategy-parameter-schema-v1"


class StrategyParameterAdjustmentError(ValueError):
    """A proposed value violates a declared parameter contract."""


@dataclass(frozen=True)
class StrategyParameter:
    parameter_id: str
    type: str
    value: int | float
    min: int | float
    max: int | float
    max_step: int | float
    locked: bool
    risk_direction: str
    min_evidence: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParameterApplication:
    dsl_ast: dict[str, Any]
    changed: dict[str, dict[str, int | float]]
    structure_checksum: str


def _walk_parameter_nodes(value: Any):
    if isinstance(value, Mapping):
        if value.get("op") == "parameter":
            yield value
        for item in value.values():
            yield from _walk_parameter_nodes(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_parameter_nodes(item)


def _structural_projection(value: Any) -> Any:
    """Keep every AST detail except declared parameter values for a stable hash."""
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if value.get("op") == "parameter" and key == "value":
                result[key] = "<declared-parameter-value>"
            else:
                result[key] = _structural_projection(item)
        return result
    if isinstance(value, list):
        return [_structural_projection(item) for item in value]
    return value


def _structure_checksum(ast: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _structural_projection(ast), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StrategyParameterSchema:
    """The complete set of declared, versioned values in one DSL definition."""

    version: str
    parameters: tuple[StrategyParameter, ...]
    structure_checksum: str

    @property
    def editable(self) -> tuple[str, ...]:
        """Compatibility view for callers that only need the tunable IDs."""
        return tuple(item.parameter_id for item in self.parameters if not item.locked)

    @property
    def immutable(self) -> tuple[str, ...]:
        return tuple(item.parameter_id for item in self.parameters if item.locked)

    @classmethod
    def from_dsl(cls, dsl_ast: Mapping[str, Any] | None) -> "StrategyParameterSchema":
        if dsl_ast is None:
            return cls(PARAMETER_SCHEMA_VERSION, (), "")
        normalized = DSL.normalize(dsl_ast)
        parameters = tuple(
            StrategyParameter(
                parameter_id=str(node["parameter_id"]), type=str(node["type"]),
                value=node["value"], min=node["min"], max=node["max"],
                max_step=node["max_step"], locked=bool(node["locked"]),
                risk_direction=str(node["risk_direction"]),
                min_evidence=int(node["min_evidence"]),
            )
            for node in _walk_parameter_nodes(normalized)
        )
        return cls(PARAMETER_SCHEMA_VERSION, parameters, _structure_checksum(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parameters": [item.to_dict() for item in self.parameters],
            "structure_checksum": self.structure_checksum,
        }

    def apply(
        self,
        dsl_ast: Mapping[str, Any],
        adjustments: Mapping[str, Any],
        *,
        evidence_count: int | None,
    ) -> ParameterApplication:
        """Apply declared values only; every operator and tree edge is retained."""
        if not isinstance(adjustments, Mapping) or not adjustments:
            raise StrategyParameterAdjustmentError("parameter adjustments are required")
        normalized = DSL.normalize(dsl_ast)
        if _structure_checksum(normalized) != self.structure_checksum:
            raise StrategyParameterAdjustmentError("DSL structure changed since this schema was compiled")
        by_id = {item.parameter_id: item for item in self.parameters}
        unknown = sorted(set(adjustments) - set(by_id))
        if unknown:
            raise StrategyParameterAdjustmentError(f"undeclared strategy parameter: {unknown[0]}")
        evidence = -1 if evidence_count is None else int(evidence_count)
        replacements: dict[str, int | float] = {}
        changed: dict[str, dict[str, int | float]] = {}
        for parameter_id, candidate in adjustments.items():
            parameter = by_id[parameter_id]
            if parameter.locked:
                raise StrategyParameterAdjustmentError(f"strategy parameter is locked: {parameter_id}")
            if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
                raise StrategyParameterAdjustmentError(f"strategy parameter must be numeric: {parameter_id}")
            value: int | float = int(candidate) if parameter.type == "integer" else float(candidate)
            if parameter.type == "integer" and int(candidate) != candidate:
                raise StrategyParameterAdjustmentError(f"strategy parameter must be an integer: {parameter_id}")
            if not parameter.min <= value <= parameter.max:
                raise StrategyParameterAdjustmentError(f"strategy parameter is outside bounds: {parameter_id}")
            if abs(float(value) - float(parameter.value)) > float(parameter.max_step) + 1e-12:
                raise StrategyParameterAdjustmentError(f"strategy parameter exceeds max_step: {parameter_id}")
            if evidence < parameter.min_evidence:
                raise StrategyParameterAdjustmentError(f"insufficient evidence for strategy parameter: {parameter_id}")
            if value != parameter.value:
                replacements[parameter_id] = value
                changed[parameter_id] = {"old": parameter.value, "new": value}
        if not changed:
            return ParameterApplication(deepcopy(normalized), {}, self.structure_checksum)

        def replace(value: Any) -> Any:
            if isinstance(value, Mapping):
                result = {key: replace(item) for key, item in value.items()}
                if result.get("op") == "parameter":
                    parameter_id = result["parameter_id"]
                    if parameter_id in replacements:
                        result["value"] = replacements[parameter_id]
                return result
            if isinstance(value, list):
                return [replace(item) for item in value]
            return value

        updated = DSL.normalize(replace(normalized))
        checksum = _structure_checksum(updated)
        if checksum != self.structure_checksum:
            raise StrategyParameterAdjustmentError("parameter update attempted to change DSL structure")
        return ParameterApplication(updated, changed, checksum)
