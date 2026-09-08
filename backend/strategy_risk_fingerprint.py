"""Compile a conservative, deterministic risk profile from a strategy DSL.

The compiler deliberately accepts plain mappings/lists rather than a runtime
strategy object.  It performs no I/O and does not evaluate a rule: callers can
therefore use its result in review, versioning, and offline tooling.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class StrategyRiskFingerprint:
    """A coarse risk contract inferred from a strategy's declared inputs."""

    archetype: str
    holding_horizon: str
    signal_half_life: str
    entry_urgency: str
    turnover: str
    volatility_sensitivity: str
    data_freshness: str
    natural_stop: str
    concentration_risk: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_CONSERVATIVE = StrategyRiskFingerprint(
    archetype="composite",
    holding_horizon="conservative",
    signal_half_life="conservative",
    entry_urgency="conservative",
    turnover="conservative",
    volatility_sensitivity="conservative",
    data_freshness="conservative",
    natural_stop="conservative",
    concentration_risk="conservative",
)


def _tokens(value: Any) -> set[str]:
    """Return lower-cased lexical evidence without interpreting the DSL.

    Inactive declarations are deliberately excluded: ``{"realtime": false}``
    or ``{"stop_loss": false}`` disables a feature, so its key must not be
    counted as positive evidence for that feature.  Empty strings, ``None``
    and empty collections carry no evidence either.
    """
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key, item in value.items():
            if item is None or item is False:
                continue
            if isinstance(item, str) and not item.strip():
                continue
            if isinstance(item, (list, tuple, frozenset, set, Mapping)) and not len(item):
                continue
            result.update(_tokens(key))
            result.update(_tokens(item))
        return result
    if isinstance(value, (list, tuple, frozenset, set)):
        result: set[str] = set()
        for item in value:
            result.update(_tokens(item))
        return result
    if isinstance(value, str):
        normalized = value.lower().replace("-", "_").replace("/", "_")
        return {part for part in normalized.replace(" ", "_").split("_") if part}
    return set()


def _numbers(value: Any, names: frozenset[str]) -> list[float]:
    """Collect numeric configuration values whose key has a recognised name."""
    if not isinstance(value, Mapping):
        return []
    result = []
    for key, item in value.items():
        key_tokens = _tokens(key)
        if key_tokens & names:
            try:
                result.append(float(item))
            except (TypeError, ValueError):
                pass
        if isinstance(item, Mapping):
            result.extend(_numbers(item, names))
    return result


def _has(tokens: set[str], *words: str) -> bool:
    return bool(tokens.intersection(words))


def _archetype(tokens: set[str]) -> str:
    scores = {
        "breakout": sum(word in tokens for word in ("breakout", "突破", "surge")),
        "trend": sum(word in tokens for word in ("trend", "趋势", "ma", "pullback")),
        "mean_reversion": sum(word in tokens for word in ("mean", "reversion", "oversold", "超跌")),
        "rotation": sum(word in tokens for word in ("sector", "rotation", "板块", "轮动")),
        "event_driven": sum(word in tokens for word in ("earnings", "report", "event", "公告", "财报")),
        "flow_momentum": sum(word in tokens for word in ("flow", "momentum", "资金", "主力")),
    }
    winner, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    # A tie means that the DSL describes multiple styles rather than one
    # sufficiently dominant style.  Do not manufacture a precise label.
    return winner if score and list(scores.values()).count(score) == 1 else "composite"


# Only semantically recognised holding/horizon keys may contribute to the
# horizon classification.  Generic ``*_days`` keys such as cooldown_days,
# lookback_days or validation windows describe something else entirely and
# must not turn a swing strategy into a position strategy (review P2).
_HOLDING_KEYS = frozenset({"hold", "holding", "horizon"})


def _horizon(config: Mapping[str, Any], tokens: set[str]) -> tuple[str, str]:
    days = _numbers(config, _HOLDING_KEYS)
    if _has(tokens, "intraday", "日内"):
        return "intraday", "minutes"
    if days:
        # A declared range such as hold_min=1 / hold_max=8 must be classified
        # by its applicable upper bound: on a T+1 market a one-day holding
        # floor is not evidence of intraday execution (review P2).
        representative = max(days)
        if representative <= 1:
            return "intraday", "minutes"
        if representative <= 5:
            return "short", "days"
        if representative <= 20:
            return "swing", "weeks"
        return "position", "weeks"
    if _has(tokens, "realtime", "minute", "盘中"):
        return "intraday", "minutes"
    return "conservative", "conservative"


def compile_strategy_risk_fingerprint(
    dsl_ast: Mapping[str, Any] | list[Any] | tuple[Any, ...] | None,
    strategy_config: Mapping[str, Any] | None = None,
) -> StrategyRiskFingerprint:
    """Compile ``dsl_ast`` and ``strategy_config`` into a stable risk fingerprint.

    When no single style can be supported by the supplied declarations, every
    uncertain field deliberately remains ``conservative`` and the archetype is
    ``composite``.  This makes absence of evidence safe for downstream users.
    """
    config = strategy_config if isinstance(strategy_config, Mapping) else {}
    tokens = _tokens(dsl_ast) | _tokens(config)
    if not tokens:
        return _CONSERVATIVE

    archetype = _archetype(tokens)
    horizon, half_life = _horizon(config, tokens)
    real_time = _has(tokens, "realtime", "real", "quote", "tick", "minute", "intraday", "盘中")
    daily = _has(tokens, "daily", "close", "eod", "日线", "收盘")
    urgency = "immediate" if real_time and _has(tokens, "breakout", "surge", "trigger", "突破") else (
        "same_session" if real_time else "next_session" if daily else "conservative"
    )
    turnover = "high" if real_time or archetype in {"breakout", "flow_momentum"} else (
        "low" if archetype in {"trend", "mean_reversion"} else "conservative"
    )
    volatility = "high" if _has(tokens, "atr", "volatility", "boll", "band", "波动") else (
        "medium" if _has(tokens, "stop", "drawdown", "止损", "回撤") else "conservative"
    )
    freshness = "realtime" if real_time else "daily_close" if daily else "conservative"
    natural_stop = "price" if _has(tokens, "stop", "stoploss", "止损") else (
        "technical" if _has(tokens, "ma", "support", "breakdown", "均线") else (
            "time" if _has(tokens, "horizon", "holding", "hold") else "conservative"
        )
    )
    # Concentration is a position-COUNT property.  Percentages (max_weight,
    # max_exposure), day counts (hold_max) and ratios (max_drawdown) must not
    # be compared with position-count thresholds (review P2).
    limits = _numbers(config, frozenset({
        "positions", "position", "stocks", "positions_count", "position_limit",
    }))
    concentration = "high" if limits and min(limits) <= 3 else (
        "medium" if limits and min(limits) <= 10 else "low" if limits else "conservative"
    )

    return StrategyRiskFingerprint(
        archetype=archetype,
        holding_horizon=horizon,
        signal_half_life=half_life,
        entry_urgency=urgency,
        turnover=turnover,
        volatility_sensitivity=volatility,
        data_freshness=freshness,
        natural_stop=natural_stop,
        concentration_risk=concentration,
    )
