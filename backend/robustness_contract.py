"""Immutable identities and result contracts for R30 adversarial validation."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any

PLAN_VERSION = "r30-robustness-plan-v1"
SCENARIO_VERSION = "r30-adversarial-scenario-v1"
REPORT_VERSION = "r30-robustness-runner-v1"
SCENARIO_CATEGORIES = (
    "regime", "cost", "slippage", "execution_delay", "signal_delay",
    "liquidity", "data_missingness", "parameter", "start_date", "end_date", "universe",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLAN_LISTS = (
    "cost_stresses", "slippage_stresses", "execution_delay_stresses",
    "signal_delay_stresses", "liquidity_stresses", "missing_data_stresses",
    "parameter_stresses", "start_date_stresses", "end_date_stresses", "universe_stresses",
)
_RESULT_METRICS = (
    "return", "drawdown", "volatility", "turnover", "trade_count", "cost",
    "exposure", "capacity_proxy", "data_coverage",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze(value: Any, *, path: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key for key in value):
            raise ValueError(f"{path} keys must be non-empty strings")
        return MappingProxyType({key: _freeze(item, path=f"{path}.{key}")
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, path=f"{path}[]") for item in value)
    raise ValueError(f"{path} must contain JSON values")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _number(value: Any, *, name: str, minimum: float | None = None,
            maximum: float | None = None, strict_minimum: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and (value <= minimum if strict_minimum else value < minimum):
        raise ValueError(f"{name} is below its allowed range")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} is above its allowed range")
    return int(value) if isinstance(value, float) and value.is_integer() else value


class RobustnessPlan:
    """Full explicit plan; creation time is descriptive and outside its identity."""

    __slots__ = ("plan_version", "baseline_run_key", "baseline_experiment_fingerprint",
                 "random_seed", "regime_policy", "created_at", "allowed_parameter_paths",
                 "max_drawdown_limit", "max_return_degradation", *_PLAN_LISTS)

    def __setattr__(self, _name, _value):
        raise AttributeError("RobustnessPlan is immutable")

    def __init__(self, *, baseline_run_key: str, baseline_experiment_fingerprint: str,
                 random_seed: int, regime_policy: Mapping[str, Any],
                 cost_stresses=(), slippage_stresses=(), execution_delay_stresses=(),
                 signal_delay_stresses=(), liquidity_stresses=(), missing_data_stresses=(),
                 parameter_stresses=(), start_date_stresses=(), end_date_stresses=(),
                 universe_stresses=(), plan_version: str = PLAN_VERSION,
                 allowed_parameter_paths=(), max_drawdown_limit=None,
                 max_return_degradation=None, created_at: str | None = None):
        if plan_version != PLAN_VERSION:
            raise ValueError("unsupported_robustness_plan_version")
        if not isinstance(baseline_run_key, str) or not _SHA256.fullmatch(baseline_run_key):
            raise ValueError("baseline_run_key_invalid")
        if (not isinstance(baseline_experiment_fingerprint, str)
                or not _SHA256.fullmatch(baseline_experiment_fingerprint)):
            raise ValueError("baseline_experiment_fingerprint_invalid")
        if isinstance(random_seed, bool) or not isinstance(random_seed, int):
            raise ValueError("random_seed_must_be_integer")
        frozen_policy = _freeze(regime_policy, path="regime_policy")
        if not isinstance(frozen_policy, Mapping):
            raise ValueError("regime_policy_required")
        policy_required = {
            "policy_version", "benchmark_symbol", "trend_window_sessions",
            "bull_threshold", "bear_threshold", "volatility_window_sessions",
            "high_vol_threshold", "low_vol_threshold",
        }
        if not policy_required.issubset(frozen_policy):
            raise ValueError("regime_policy_incomplete")
        for name in ("trend_window_sessions", "volatility_window_sessions"):
            value = frozen_policy[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError("regime_window_invalid")
        for name in ("bull_threshold", "bear_threshold", "high_vol_threshold", "low_vol_threshold"):
            _number(frozen_policy[name], name=name, minimum=0)
        if frozen_policy["bear_threshold"] > frozen_policy["bull_threshold"]:
            raise ValueError("regime_threshold_order_invalid")
        if frozen_policy["low_vol_threshold"] > frozen_policy["high_vol_threshold"]:
            raise ValueError("volatility_threshold_order_invalid")
        if created_at is not None and not isinstance(created_at, str):
            raise ValueError("created_at_must_be_text")
        parameter_paths = tuple(sorted(set(allowed_parameter_paths)))
        if any(not isinstance(path, str) or not path.startswith("strategy_parameters.")
               for path in parameter_paths):
            raise ValueError("parameter_path_not_allowlisted")
        object.__setattr__(self, "plan_version", plan_version)
        object.__setattr__(self, "baseline_run_key", baseline_run_key)
        object.__setattr__(self, "baseline_experiment_fingerprint", baseline_experiment_fingerprint)
        object.__setattr__(self, "random_seed", random_seed)
        object.__setattr__(self, "regime_policy", frozen_policy)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "allowed_parameter_paths", parameter_paths)
        object.__setattr__(self, "max_drawdown_limit", None if max_drawdown_limit is None else
                           _number(max_drawdown_limit, name="max_drawdown_limit", minimum=0))
        object.__setattr__(self, "max_return_degradation", None if max_return_degradation is None else
                           _number(max_return_degradation, name="max_return_degradation", minimum=0))
        for name in _PLAN_LISTS:
            raw = tuple(getattr_value(locals(), name))
            if any(not isinstance(item, Mapping) for item in raw):
                raise ValueError(f"{name}_entries_must_be_objects")
            frozen = tuple(_freeze(item, path=f"{name}[]") for item in raw)
            object.__setattr__(self, name, frozen)
        if len(self.scenarios()) < 1 or len(self.scenarios()) > 100:
            raise ValueError("robustness_scenario_count_must_be_1_to_100")

    def projection(self, *, include_created_at: bool = False) -> dict[str, Any]:
        value = {"plan_version": self.plan_version,
                 "baseline_run_key": self.baseline_run_key,
                 "baseline_experiment_fingerprint": self.baseline_experiment_fingerprint,
                 "random_seed": self.random_seed,
                 "regime_policy": _thaw(self.regime_policy),
                 "allowed_parameter_paths": list(self.allowed_parameter_paths),
                 "max_drawdown_limit": self.max_drawdown_limit,
                 "max_return_degradation": self.max_return_degradation}
        value.update({name: _thaw(getattr(self, name)) for name in _PLAN_LISTS})
        if include_created_at:
            value["created_at"] = self.created_at
        return value

    @property
    def fingerprint(self) -> str:
        return _sha(self.projection())

    def scenarios(self) -> list[dict[str, Any]]:
        categories = (
            ("cost", "cost_stresses"), ("slippage", "slippage_stresses"),
            ("execution_delay", "execution_delay_stresses"),
            ("signal_delay", "signal_delay_stresses"), ("liquidity", "liquidity_stresses"),
            ("data_missingness", "missing_data_stresses"), ("parameter", "parameter_stresses"),
            ("start_date", "start_date_stresses"), ("end_date", "end_date_stresses"),
            ("universe", "universe_stresses"),
        )
        result = []
        for category, name in categories:
            for index, raw in enumerate(getattr(self, name)):
                parameters = _thaw(raw)
                _validate_scenario(category, parameters, self.allowed_parameter_paths)
                seed = parameters.pop("seed", self.random_seed)
                if isinstance(seed, bool) or not isinstance(seed, int):
                    raise ValueError("scenario_seed_must_be_integer")
                result.append(RobustnessScenario(
                    scenario_id=str(parameters.pop("scenario_id", f"{category}-{index + 1:03d}")),
                    category=category, scenario_version=SCENARIO_VERSION,
                    parameters={**parameters, "seed": seed}, seed=seed,
                    baseline_run_key=self.baseline_run_key,
                    baseline_experiment_fingerprint=self.baseline_experiment_fingerprint,
                ).projection())
        return result



ROBUSTNESS_POLICY_VERSION = "r30-robustness-policy-v1"
_POLICY_FIELDS = ("random_seed", "regime_policy", "allowed_parameter_paths",
                  "max_drawdown_limit", "max_return_degradation", *_PLAN_LISTS)


class RobustnessPolicy:
    """Reusable immutable stress policy; ``bind`` pins one exact R29 baseline.

    It holds exactly the stress policy of :class:`RobustnessPlan` minus the baseline
    identity and creation metadata, so one search can share a single policy across
    candidates while every candidate still binds its own exact baseline.
    """

    __slots__ = ("policy_version", *_POLICY_FIELDS)

    def __setattr__(self, _name, _value):
        raise AttributeError("RobustnessPolicy is immutable")

    def __init__(self, *, random_seed: int, regime_policy: Mapping[str, Any],
                 cost_stresses=(), slippage_stresses=(), execution_delay_stresses=(),
                 signal_delay_stresses=(), liquidity_stresses=(), missing_data_stresses=(),
                 parameter_stresses=(), start_date_stresses=(), end_date_stresses=(),
                 universe_stresses=(), policy_version: str = ROBUSTNESS_POLICY_VERSION,
                 allowed_parameter_paths=(), max_drawdown_limit=None,
                 max_return_degradation=None):
        if policy_version != ROBUSTNESS_POLICY_VERSION:
            raise ValueError("unsupported_robustness_policy_version")
        # Reuse the existing canonical plan validation instead of a second rule set.
        probe = RobustnessPlan(
            baseline_run_key="0" * 64, baseline_experiment_fingerprint="0" * 64,
            random_seed=random_seed, regime_policy=regime_policy,
            cost_stresses=cost_stresses, slippage_stresses=slippage_stresses,
            execution_delay_stresses=execution_delay_stresses,
            signal_delay_stresses=signal_delay_stresses, liquidity_stresses=liquidity_stresses,
            missing_data_stresses=missing_data_stresses, parameter_stresses=parameter_stresses,
            start_date_stresses=start_date_stresses, end_date_stresses=end_date_stresses,
            universe_stresses=universe_stresses, allowed_parameter_paths=allowed_parameter_paths,
            max_drawdown_limit=max_drawdown_limit,
            max_return_degradation=max_return_degradation)
        object.__setattr__(self, "policy_version", policy_version)
        for name in _POLICY_FIELDS:
            object.__setattr__(self, name, getattr(probe, name))

    def projection(self) -> dict[str, Any]:
        value = {"policy_version": self.policy_version,
                 "random_seed": self.random_seed,
                 "regime_policy": _thaw(self.regime_policy),
                 "allowed_parameter_paths": list(self.allowed_parameter_paths),
                 "max_drawdown_limit": self.max_drawdown_limit,
                 "max_return_degradation": self.max_return_degradation}
        value.update({name: _thaw(getattr(self, name)) for name in _PLAN_LISTS})
        return value

    @property
    def fingerprint(self) -> str:
        return _sha(self.projection())

    def bind(self, baseline_run_key: str,
             baseline_experiment_fingerprint: str) -> RobustnessPlan:
        """Bind one exact baseline identity; the plan keeps the existing authority."""
        return RobustnessPlan(
            baseline_run_key=baseline_run_key,
            baseline_experiment_fingerprint=baseline_experiment_fingerprint,
            random_seed=self.random_seed,
            regime_policy=_thaw(self.regime_policy),
            cost_stresses=[_thaw(item) for item in self.cost_stresses],
            slippage_stresses=[_thaw(item) for item in self.slippage_stresses],
            execution_delay_stresses=[_thaw(item) for item in self.execution_delay_stresses],
            signal_delay_stresses=[_thaw(item) for item in self.signal_delay_stresses],
            liquidity_stresses=[_thaw(item) for item in self.liquidity_stresses],
            missing_data_stresses=[_thaw(item) for item in self.missing_data_stresses],
            parameter_stresses=[_thaw(item) for item in self.parameter_stresses],
            start_date_stresses=[_thaw(item) for item in self.start_date_stresses],
            end_date_stresses=[_thaw(item) for item in self.end_date_stresses],
            universe_stresses=[_thaw(item) for item in self.universe_stresses],
            allowed_parameter_paths=self.allowed_parameter_paths,
            max_drawdown_limit=self.max_drawdown_limit,
            max_return_degradation=self.max_return_degradation)


def getattr_value(values: Mapping[str, Any], name: str) -> Any:
    return values[name]


def _validate_scenario(category: str, value: dict[str, Any], allowlist: tuple[str, ...]) -> None:
    if not isinstance(value, dict):
        raise ValueError("scenario_parameters_invalid")
    common = {"scenario_id", "seed"}
    allowed = {
        "cost": common | {"commission_multiplier", "minimum_commission_multiplier", "stamp_duty_multiplier"},
        "slippage": common | {"multiplier"},
        "execution_delay": common | {"execution_delay_sessions"},
        "signal_delay": common | {"signal_delay_sessions"},
        "liquidity": common | {"liquidity_multiplier"},
        "data_missingness": common | {"missing_fraction", "field_scope"},
        "parameter": common | {"path", "operation", "value"},
        "start_date": common | {"shift_sessions"},
        "end_date": common | {"shift_sessions"},
        "universe": common | {"drop_fraction"},
    }[category]
    if set(value) - allowed:
        raise ValueError("scenario_parameter_unknown")
    if "scenario_id" in value and (not isinstance(value["scenario_id"], str)
                                    or not value["scenario_id"].strip()):
        raise ValueError("scenario_id_required")
    if category == "cost":
        for key in ("commission_multiplier", "minimum_commission_multiplier", "stamp_duty_multiplier"):
            if key in value:
                _number(value[key], name=key, minimum=0)
        if not any(key in value for key in
                   ("commission_multiplier", "minimum_commission_multiplier", "stamp_duty_multiplier")):
            raise ValueError("cost_stress_empty")
    elif category == "slippage":
        _number(value.get("multiplier"), name="slippage_multiplier", minimum=0)
    elif category in {"execution_delay", "signal_delay"}:
        key = "execution_delay_sessions" if category == "execution_delay" else "signal_delay_sessions"
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("session_delay_invalid")
    elif category == "liquidity":
        _number(value.get("liquidity_multiplier"), name="liquidity_multiplier",
                minimum=0, maximum=1, strict_minimum=True)
    elif category == "data_missingness":
        _number(value.get("missing_fraction"), name="missing_fraction", minimum=0, maximum=1)
        if value.get("field_scope") != "whole_bar":
            raise ValueError("unsupported_missingness_scope")
    elif category == "parameter":
        path = value.get("path")
        if path not in allowlist:
            raise ValueError("parameter_path_not_allowlisted")
        operation = value.get("operation")
        if operation not in {"delta", "multiplier"}:
            raise ValueError("parameter_operation_invalid")
        _number(value.get("value"), name="parameter_perturbation")
    elif category in {"start_date", "end_date"}:
        key = "shift_sessions"
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("date_shift_invalid")
    elif category == "universe":
        _number(value.get("drop_fraction"), name="drop_fraction", minimum=0, maximum=1)


class RobustnessScenario:
    __slots__ = ("scenario_id", "category", "scenario_version", "parameters", "seed",
                 "baseline_run_key", "baseline_experiment_fingerprint")

    def __setattr__(self, _name, _value):
        raise AttributeError("RobustnessScenario is immutable")

    def __init__(self, *, scenario_id: str, category: str, scenario_version: str,
                 parameters: Mapping[str, Any], seed: int,
                 baseline_run_key: str,
                 baseline_experiment_fingerprint: str):
        if category not in SCENARIO_CATEGORIES:
            raise ValueError("scenario_category_invalid")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ValueError("scenario_id_required")
        if scenario_version != SCENARIO_VERSION:
            raise ValueError("scenario_version_unsupported")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("scenario_seed_must_be_integer")
        if not _SHA256.fullmatch(baseline_run_key or ""):
            raise ValueError("baseline_run_key_invalid")
        if not _SHA256.fullmatch(baseline_experiment_fingerprint or ""):
            raise ValueError("baseline_experiment_fingerprint_invalid")
        object.__setattr__(self, "scenario_id", scenario_id.strip())
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "scenario_version", scenario_version)
        object.__setattr__(self, "parameters", _freeze(parameters))
        object.__setattr__(self, "seed", seed)
        object.__setattr__(self, "baseline_run_key", baseline_run_key)
        object.__setattr__(self, "baseline_experiment_fingerprint", baseline_experiment_fingerprint)

    def identity_projection(self) -> dict[str, Any]:
        return {"scenario_id": self.scenario_id, "category": self.category,
                "scenario_version": self.scenario_version, "parameters": _thaw(self.parameters),
                "seed": self.seed, "baseline_run_key": self.baseline_run_key,
                "baseline_experiment_fingerprint": self.baseline_experiment_fingerprint}

    def projection(self) -> dict[str, Any]:
        value = self.identity_projection()
        value["scenario_fingerprint"] = self.fingerprint
        return value

    @property
    def fingerprint(self) -> str:
        return _sha({"baseline_experiment_fingerprint": self.baseline_experiment_fingerprint,
                     "scenario": self.identity_projection()})


def case_result(*, scenario_fingerprint: str, status: str, metrics: Mapping[str, Any] | None,
                baseline_delta: Mapping[str, Any] | None, reason_code: str | None = None) -> dict[str, Any]:
    if status not in {"completed", "unavailable", "failed"}:
        raise ValueError("robustness_case_status_invalid")
    if status == "completed":
        if not isinstance(metrics, Mapping) or not set(_RESULT_METRICS).issubset(metrics):
            raise ValueError("completed_case_metrics_required")
        for key in _RESULT_METRICS:
            value = metrics[key]
            if key == "trade_count":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("trade_count_invalid")
            elif value is not None:
                _number(value, name=key)
        if not isinstance(baseline_delta, Mapping):
            raise ValueError("baseline_delta_required")
    elif metrics is not None or baseline_delta is not None or not reason_code:
        raise ValueError("unavailable_case_cannot_claim_metrics")
    return {"scenario_fingerprint": scenario_fingerprint, "status": status,
            "metrics": None if metrics is None else dict(metrics),
            "baseline_delta": None if baseline_delta is None else dict(baseline_delta),
            "reason_code": reason_code}


def report_fingerprint(*, baseline_identity: Mapping[str, Any], plan_fingerprint: str,
                       cases: list[Mapping[str, Any]], report_version: str = REPORT_VERSION) -> str:
    return _sha({"report_version": report_version, "baseline_identity": dict(baseline_identity),
                 "plan_fingerprint": plan_fingerprint, "cases": list(cases)})
