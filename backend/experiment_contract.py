"""Pure, immutable identity and result contracts for strategy experiments.

This module deliberately does not execute experiments or discover application
state. Callers must supply every identity and assumption explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

CONTRACT_VERSION = "r28-a.v1"
RESULT_STATUSES = frozenset({"completed", "failed", "unavailable"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,119}$")
_UNSTABLE_IDENTITIES = frozenset({
    "current", "current_snapshot", "latest", "latest_snapshot", "today", "now",
})
_REQUIRED_COMPLETED_METRICS = (
    "total_return", "max_drawdown", "volatility", "turnover", "trade_count",
    "total_cost", "data_coverage",
)
_COST_FIELDS = (
    "commission_rate", "minimum_commission", "stamp_duty_rate", "slippage_model", "version",
)
_UNSET_RESULT_FINGERPRINT = object()
_EXECUTION_FIELDS = (
    "execution_profile_version", "fill_assumptions", "t_plus_one_semantics",
    "price_limit_semantics", "partial_fill_semantics", "capacity_assumptions",
)


def _text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _stable_fingerprint(value: Any, *, name: str) -> str:
    text = _text(value, name=name)
    if text.lower() in _UNSTABLE_IDENTITIES:
        raise ValueError(f"{name} must not use a current/latest identity")
    if not _SHA256_PATTERN.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


def _explicit_label(value: Any, *, name: str) -> str:
    text = _text(value, name=name)
    if text.lower() in _UNSTABLE_IDENTITIES:
        raise ValueError(f"{name} must not use an implicit current/latest policy")
    return text


def _freeze_json(value: Any, *, path: str = "value") -> Any:
    """Validate JSON-like values and recursively freeze mappings and sequences."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            frozen[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path=f"{path}[]") for item in value)
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw_json(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _date(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an explicit YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an explicit YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use canonical YYYY-MM-DD format")
    return value


def _asof_cutoff(value: Any) -> str:
    text = _explicit_label(value, name="asof_policy.cutoff")
    try:
        return _date(text, name="asof_policy.cutoff")
    except ValueError:
        pass
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        instant = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("asof_policy.cutoff must be an explicit ISO date or timezone-aware instant") from exc
    if instant.tzinfo is None or instant.utcoffset() is None or "T" not in text:
        raise ValueError("asof_policy.cutoff must be an explicit ISO date or timezone-aware instant")
    return text


def _finite_number(value: Any, *, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _required_mapping(value: Any, *, name: str, fields: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an explicit object")
    missing = [field for field in fields if field not in value]
    if missing:
        raise ValueError(f"{name} is missing required fields: {', '.join(missing)}")
    frozen = _freeze_json(value, path=name)
    if not isinstance(frozen, Mapping):  # for static type narrowing
        raise ValueError(f"{name} must be an object")
    return frozen


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    """An explicit pinned strategy version compatible with StrategyVersion."""

    strategy_id: str
    version: int
    checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", _text(self.strategy_id, name="strategy_id"))
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("strategy version must be an integer >= 1")
        checksum = _text(self.checksum, name="strategy checksum")
        if not _SHA256_PATTERN.fullmatch(checksum):
            raise ValueError("strategy checksum must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "checksum", checksum)

    def projection(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "version": self.version, "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """Complete immutable identity for one declared strategy experiment."""

    strategy: StrategyIdentity
    code_revision: str
    dataset_fingerprint: str
    universe_fingerprint: str
    tradability_fingerprint: str
    market_data_fingerprint: str
    parameter_set: Mapping[str, Any]
    start_date: str
    end_date: str
    asof_policy: Mapping[str, Any]
    execution_assumptions: Mapping[str, Any]
    cost_model: Mapping[str, Any]
    random_seed: int
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, StrategyIdentity):
            raise ValueError("strategy must be a pinned StrategyIdentity")
        code_revision = _text(self.code_revision, name="code_revision")
        if not _REVISION_PATTERN.fullmatch(code_revision):
            raise ValueError("code_revision must be an explicit 40 or 64 character lowercase git SHA")
        object.__setattr__(self, "code_revision", code_revision)
        for name in (
            "dataset_fingerprint", "universe_fingerprint", "tradability_fingerprint",
            "market_data_fingerprint",
        ):
            object.__setattr__(self, name, _stable_fingerprint(getattr(self, name), name=name))
        object.__setattr__(self, "contract_version", _explicit_label(self.contract_version, name="contract_version"))
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ValueError("random_seed must be an explicit integer")
        start = _date(self.start_date, name="start_date")
        end = _date(self.end_date, name="end_date")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)

        parameter_set = _freeze_json(self.parameter_set, path="parameter_set")
        if not isinstance(parameter_set, Mapping):
            raise ValueError("parameter_set must be an explicit JSON-like object")
        object.__setattr__(self, "parameter_set", parameter_set)
        asof = _required_mapping(self.asof_policy, name="asof_policy", fields=("policy_id", "cutoff"))
        normalized_asof = dict(asof)
        normalized_asof["policy_id"] = _explicit_label(
            asof["policy_id"], name="asof_policy.policy_id",
        )
        normalized_asof["cutoff"] = _asof_cutoff(asof["cutoff"])
        asof = _freeze_json(normalized_asof, path="asof_policy")
        object.__setattr__(self, "asof_policy", asof)
        execution = _required_mapping(
            self.execution_assumptions, name="execution_assumptions", fields=_EXECUTION_FIELDS,
        )
        normalized_execution = dict(execution)
        for field in ("execution_profile_version", "t_plus_one_semantics",
                      "price_limit_semantics", "partial_fill_semantics"):
            normalized_execution[field] = _explicit_label(
                execution[field], name=f"execution_assumptions.{field}",
            )
        for field in ("fill_assumptions", "capacity_assumptions"):
            if not isinstance(execution[field], Mapping) or not execution[field]:
                raise ValueError(f"execution_assumptions.{field} must be a non-empty object")
        execution = _freeze_json(normalized_execution, path="execution_assumptions")
        object.__setattr__(self, "execution_assumptions", execution)
        cost = _required_mapping(self.cost_model, name="cost_model", fields=_COST_FIELDS)
        normalized_cost = dict(cost)
        for field in ("commission_rate", "minimum_commission", "stamp_duty_rate"):
            amount = _finite_number(cost[field], name=f"cost_model.{field}")
            if amount < 0:
                raise ValueError(f"cost_model.{field} must be >= 0")
            normalized_cost[field] = amount
        for field in ("slippage_model", "version"):
            normalized_cost[field] = _explicit_label(cost[field], name=f"cost_model.{field}")
        cost = _freeze_json(normalized_cost, path="cost_model")
        object.__setattr__(self, "cost_model", cost)

    def projection(self) -> dict[str, Any]:
        """Return the full canonicalizable experiment identity."""
        return {
            "contract_version": self.contract_version,
            "strategy": self.strategy.projection(),
            "code_revision": self.code_revision,
            "dataset_fingerprint": self.dataset_fingerprint,
            "universe_fingerprint": self.universe_fingerprint,
            "tradability_fingerprint": self.tradability_fingerprint,
            "market_data_fingerprint": self.market_data_fingerprint,
            "parameter_set": _thaw_json(self.parameter_set),
            "date_range": {"start": self.start_date, "end": self.end_date},
            "asof_policy": _thaw_json(self.asof_policy),
            "execution_assumptions": _thaw_json(self.execution_assumptions),
            "cost_model": _thaw_json(self.cost_model),
            "random_seed": self.random_seed,
        }

    @property
    def fingerprint(self) -> str:
        return _digest(self.projection())

    @property
    def experiment_id(self) -> str:
        """The experiment id is its sole canonical identity."""
        return self.fingerprint


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """Standard result bound to one exact experiment fingerprint."""

    experiment_fingerprint: str
    status: str
    total_return: int | float | None = None
    max_drawdown: int | float | None = None
    volatility: int | float | None = None
    turnover: int | float | None = None
    trade_count: int | None = None
    total_cost: int | float | None = None
    exposure: int | float | None = None
    capacity_proxy: int | float | None = None
    regime_breakdown: Mapping[str, Any] | None = None
    data_coverage: int | float | None = None
    failure_reason: str | None = None
    result_fingerprint: Any = dataclass_field(default=_UNSET_RESULT_FINGERPRINT, repr=False)

    def __post_init__(self) -> None:
        experiment_fingerprint = _text(self.experiment_fingerprint, name="experiment_fingerprint")
        if not _SHA256_PATTERN.fullmatch(experiment_fingerprint):
            raise ValueError("experiment_fingerprint must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "experiment_fingerprint", experiment_fingerprint)
        if not isinstance(self.status, str) or self.status not in RESULT_STATUSES:
            raise ValueError(f"unsupported experiment result status: {self.status!r}")

        numeric_fields = (
            "total_return", "max_drawdown", "volatility", "turnover", "total_cost",
            "exposure", "capacity_proxy", "data_coverage",
        )
        for name in numeric_fields:
            value = getattr(self, name)
            if value is not None:
                value = _finite_number(value, name=name)
                object.__setattr__(self, name, value)
        if self.trade_count is not None and (
            isinstance(self.trade_count, bool) or not isinstance(self.trade_count, int)
            or self.trade_count < 0
        ):
            raise ValueError("trade_count must be an integer >= 0")
        for name in ("turnover", "total_cost"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.data_coverage is not None and not 0 <= self.data_coverage <= 1:
            raise ValueError("data_coverage must be a ratio within [0, 1]")
        if self.regime_breakdown is not None:
            breakdown = _freeze_json(self.regime_breakdown, path="regime_breakdown")
            if not isinstance(breakdown, Mapping):
                raise ValueError("regime_breakdown must be a JSON-like object")
            object.__setattr__(self, "regime_breakdown", breakdown)

        reason = self.failure_reason
        if reason is not None:
            reason = _text(reason, name="failure_reason")
            if not _REASON_PATTERN.fullmatch(reason):
                raise ValueError("failure_reason must be a stable lowercase reason code")
            object.__setattr__(self, "failure_reason", reason)
        if self.status == "completed":
            missing = [name for name in _REQUIRED_COMPLETED_METRICS if getattr(self, name) is None]
            if missing:
                raise ValueError(f"completed result is missing metrics: {', '.join(missing)}")
            if reason is not None:
                raise ValueError("completed result must not have failure_reason")
        else:
            if reason is None:
                raise ValueError(f"{self.status} result requires a stable failure_reason")
            populated = [name for name in numeric_fields + ("trade_count", "regime_breakdown")
                         if getattr(self, name) is not None]
            if populated:
                raise ValueError(f"{self.status} result cannot claim metrics: {', '.join(populated)}")

        expected = _digest(self.projection(include_fingerprint=False))
        supplied = self.result_fingerprint
        if supplied is _UNSET_RESULT_FINGERPRINT:
            object.__setattr__(self, "result_fingerprint", expected)
        else:
            if not isinstance(supplied, str) or not _SHA256_PATTERN.fullmatch(supplied):
                raise ValueError("result_fingerprint must be a lowercase SHA-256 hex digest")
            if supplied != expected:
                raise ValueError("result_fingerprint does not match the canonical result")

    def projection(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {
            "experiment_fingerprint": self.experiment_fingerprint,
            "status": self.status,
            "metrics": {
                "return": self.total_return,
                "drawdown": self.max_drawdown,
                "volatility": self.volatility,
                "turnover": self.turnover,
                "trade_count": self.trade_count,
                "cost": self.total_cost,
                "exposure": self.exposure,
                "capacity_proxy": self.capacity_proxy,
                "regime_breakdown": _thaw_json(self.regime_breakdown),
                "data_coverage": self.data_coverage,
            },
            "failure_reason": self.failure_reason,
        }
        if include_fingerprint:
            value["result_fingerprint"] = self.result_fingerprint
        return value

    def assert_for(self, spec: ExperimentSpec) -> None:
        """Fail closed when a caller pairs this result with a different spec."""
        if not isinstance(spec, ExperimentSpec):
            raise ValueError("result validation requires an ExperimentSpec")
        if self.experiment_fingerprint != spec.fingerprint:
            raise ValueError("experiment result is bound to a different ExperimentSpec")
