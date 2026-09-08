"""Deterministic strategy-level risk-profile templates.

Templates describe how a strategy should be reviewed and tuned.  They are not
an execution-policy override: ``paper_trading_rules`` remains the sole owner
of securities, T+1, limit, fee and slippage constraints.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from strategy_risk_fingerprint import (
    StrategyRiskFingerprint,
    compile_strategy_risk_fingerprint,
)


class SystemHardRuleOverrideError(ValueError):
    """Raised when a strategy template tries to alter execution safeguards."""


@dataclass(frozen=True)
class StrategyRiskProfile:
    """A review/tuning profile with intentionally disjoint parameter layers."""

    template: str
    hard_rules: dict[str, Any]
    soft_limits: dict[str, Any]
    evolvable_params: dict[str, Any]
    user_locked_params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# These names are owned by backend/paper_trading_rules.py.  A profile template
# may add strategy-level discipline, but it must never relax or replace them.
# Every top-level name owned by backend/paper_trading_rules.py.  A profile
# template may add strategy-level discipline, but it must never relax or
# replace any of them — including the board/ETF prefix tuples and the ST/
# delisting gate, which define the tradable security scope (review P2).
_SYSTEM_HARD_RULE_KEYS = frozenset({
    "paper_trading_rules", "commission", "min_commission", "stamp_sell",
    "slippage", "security_scope", "asset_type", "limit_pct", "next_weekday",
    "is_trade_weekday", "t_plus_one", "t1", "t0_etf", "t0_etf_prefixes",
    "main_board_prefixes", "chinext_prefixes", "star_prefixes",
    "is_st_or_delisting",
})

_ARCHETYPE_TO_TEMPLATE = {
    "momentum": "Momentum",
    "breakout": "Momentum",
    "trend": "Trend",
    "mean_reversion": "MeanReversion",
    "rotation": "Rotation",
    "event": "Event",
    "event_driven": "Event",
    "flow": "Flow",
    "flow_momentum": "Flow",
    "composite": "Composite",
}

# All values are strategy-level defaults.  Execution-time system safeguards do
# not appear in these payloads, so there is no accidental control-plane path.
_TEMPLATES = {
    "Momentum": {
        "hard_rules": {"signal_confirmation": "realtime", "entry_evidence": "price_and_volume"},
        "soft_limits": {"max_positions": 3, "max_weight": 0.30, "max_exposure": 0.88},
        "evolvable_params": {"entry_score": 0.76, "trail_after": 0.04, "trail_stop": 0.05},
    },
    "Trend": {
        "hard_rules": {"signal_confirmation": "daily_close", "entry_evidence": "trend_and_pullback"},
        "soft_limits": {"max_positions": 4, "max_weight": 0.28, "max_exposure": 0.85},
        "evolvable_params": {"entry_score": 0.72, "holding_days": 10, "trail_stop": 0.06},
    },
    "MeanReversion": {
        "hard_rules": {"signal_confirmation": "daily_close", "entry_evidence": "oversold_and_reversal"},
        "soft_limits": {"max_positions": 4, "max_weight": 0.24, "max_exposure": 0.75},
        "evolvable_params": {"entry_score": 0.70, "holding_days": 8, "take_profit_target": 0.06},
    },
    "Rotation": {
        "hard_rules": {"signal_confirmation": "sector_relative_strength", "entry_evidence": "sector_and_stock"},
        "soft_limits": {"max_positions": 3, "max_weight": 0.30, "max_exposure": 0.82},
        "evolvable_params": {"entry_score": 0.74, "holding_days": 7, "sector_breadth_min": 0.55},
    },
    "Event": {
        "hard_rules": {"signal_confirmation": "disclosed_event", "entry_evidence": "event_and_price"},
        "soft_limits": {"max_positions": 3, "max_weight": 0.26, "max_exposure": 0.72},
        "evolvable_params": {"entry_score": 0.75, "holding_days": 5, "event_decay_days": 3},
    },
    "Flow": {
        "hard_rules": {"signal_confirmation": "realtime", "entry_evidence": "flow_persistence"},
        "soft_limits": {"max_positions": 3, "max_weight": 0.28, "max_exposure": 0.80},
        "evolvable_params": {"entry_score": 0.76, "flow_min": 0.65, "confirmation_window": 3},
    },
    "Composite": {
        "hard_rules": {"signal_confirmation": "independent_evidence", "entry_evidence": "two_or_more_factors"},
        "soft_limits": {"max_positions": 3, "max_weight": 0.22, "max_exposure": 0.65},
        "evolvable_params": {"entry_score": 0.78, "holding_days": 5, "confirmation_window": 2},
    },
}


def _normalize_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _reject_system_rule_overrides(value: Any) -> None:
    """Reject protected keys at every nesting level before copying user input."""
    if not isinstance(value, Mapping):
        if isinstance(value, (list, tuple)):
            for item in value:
                _reject_system_rule_overrides(item)
        return
    for key, item in value.items():
        normalized = _normalize_key(key)
        if normalized in _SYSTEM_HARD_RULE_KEYS:
            raise SystemHardRuleOverrideError(
                f"{key!r} belongs to paper_trading_rules and cannot be overridden"
            )
        _reject_system_rule_overrides(item)


def _template_name(profile: str | StrategyRiskFingerprint) -> str:
    if isinstance(profile, StrategyRiskFingerprint):
        archetype = profile.archetype
    else:
        archetype = _normalize_key(profile)
    try:
        return _ARCHETYPE_TO_TEMPLATE[archetype]
    except KeyError as exc:
        supported = ", ".join(_TEMPLATES)
        raise ValueError(f"unsupported strategy risk profile {profile!r}; expected one of {supported}") from exc


def compile_strategy_risk_profile(
    profile: str | StrategyRiskFingerprint,
    *,
    user_locked_params: Mapping[str, Any] | None = None,
) -> StrategyRiskProfile:
    """Compile one named template without changing any system trading rule.

    User locks may pin only a template's soft limit or evolvable parameter.
    A locked value is removed from its original layer, making the four output
    sections mutually exclusive and preventing downstream tuning from changing
    it by accident.
    """
    template_name = _template_name(profile)
    source = deepcopy(_TEMPLATES[template_name])
    locks = dict(user_locked_params or {})
    _reject_system_rule_overrides(locks)

    allowed = set(source["soft_limits"]) | set(source["evolvable_params"])
    unknown = sorted(set(locks) - allowed)
    if unknown:
        raise ValueError(f"user_locked_params are not template parameters: {', '.join(unknown)}")

    for key in locks:
        source["soft_limits"].pop(key, None)
        source["evolvable_params"].pop(key, None)

    return StrategyRiskProfile(
        template=template_name,
        hard_rules=source["hard_rules"],
        soft_limits=source["soft_limits"],
        evolvable_params=source["evolvable_params"],
        user_locked_params=deepcopy(locks),
    )


def compile_strategy_risk_profile_from_strategy(
    dsl_ast: Mapping[str, Any] | list[Any] | tuple[Any, ...] | None,
    strategy_config: Mapping[str, Any] | None = None,
    *,
    user_locked_params: Mapping[str, Any] | None = None,
) -> StrategyRiskProfile:
    """Compile a profile directly from the PR-03 DSL/config fingerprint."""
    fingerprint = compile_strategy_risk_fingerprint(dsl_ast, strategy_config)
    return compile_strategy_risk_profile(fingerprint, user_locked_params=user_locked_params)
