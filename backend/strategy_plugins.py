# -*- coding: utf-8 -*-
"""Unified strategy plugin contract over the existing versioned runtime.

The registry/runtime remain authoritative for strategy metadata and risk. This
module binds them to candidate selectors and, when the production caller opts
into the replay contract, records a privacy-bounded replay artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import strategy_policies as SPOL
import strategy_registry as SR
import strategy_runtime as SRT
import strategy_trace as STRACE

CandidateRunner = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class StrategyPlugin:
    """One strategy's candidate, lifecycle, parameter and risk contract."""

    strategy_id: str
    selector_id: str
    factor_inputs: tuple[str, ...]
    candidate_runner: CandidateRunner | None = None
    candidate_output_keys: tuple[str, ...] = ("strategy", "count", "picks")
    entry_policy_key: str | None = None
    exit_policy_key: str | None = None

    def __post_init__(self) -> None:
        strategy_id = str(self.strategy_id or "").strip()
        selector_id = str(self.selector_id or "").strip()
        if not strategy_id:
            raise ValueError("strategy plugin id is required")
        if not selector_id and self.candidate_runner is None:
            raise ValueError("strategy plugin requires selector_id or candidate_runner")
        if not self.factor_inputs:
            raise ValueError("strategy plugin factor_inputs must not be empty")
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "selector_id", selector_id)
        if self.entry_policy_key is not None:
            object.__setattr__(self, "entry_policy_key", str(self.entry_policy_key).strip())
        if self.exit_policy_key is not None:
            object.__setattr__(self, "exit_policy_key", str(self.exit_policy_key).strip())

    @property
    def entry_key(self) -> str:
        return str(self.entry_policy_key or self.strategy_id)

    @property
    def exit_key(self) -> str:
        return str(self.exit_policy_key or self.strategy_id)

    def spec(self, *, conn=None):
        spec = SR.get(self.strategy_id, conn=conn)
        if spec is None:
            raise ValueError(f"strategy is not registered: {self.strategy_id}")
        return spec

    def runtime(self, conn):
        return SRT.get_context(conn, self.strategy_id)

    def manifest(self, *, conn=None) -> dict[str, Any]:
        spec = self.spec(conn=conn)
        payload: dict[str, Any] = {
            "strategy_id": spec.id,
            "name": spec.name,
            "status": spec.status,
            "enabled": bool(spec.status == "active" and spec.supports_new_cycle),
            "origin": spec.origin,
            "implementation_key": spec.implementation_key,
            "selector_id": self.selector_id,
            "factor_inputs": list(self.factor_inputs),
            "candidate_output_keys": list(self.candidate_output_keys),
            "entry_policy_key": self.entry_key,
            "exit_policy_key": self.exit_key,
            "version": spec.current_version,
            "checksum": spec.current_checksum,
            "parameter_schema": None,
        }
        if conn is not None:
            context = self.runtime(conn)
            payload.update({
                "version": context.version,
                "checksum": context.checksum,
                "parameter_schema": context.parameter_schema.to_dict(),
                "lifecycle_stage": context.lifecycle_stage,
            })
        return payload

    def _run_candidate(self, table, **kwargs):
        if self.candidate_runner is not None:
            return self.candidate_runner(table, **kwargs)
        import strategies as S
        return S._run_strategy_legacy(self.selector_id, table, **kwargs)

    def _validate_candidate_output(self, result) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise TypeError("strategy candidate runner must return an object")
        missing = [key for key in self.candidate_output_keys if key not in result]
        if missing:
            raise ValueError(f"strategy candidate output missing key: {missing[0]}")
        if not isinstance(result.get("picks"), list):
            raise ValueError("strategy candidate output picks must be a list")
        return dict(result)

    def select_candidates(
        self,
        table,
        *,
        replay_data_date=None,
        replay_required: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        """Run a selector; persist replay data only when the caller requests it.

        Low-level strategy evaluation stays a pure algorithm boundary. Production
        selection/paper paths opt into ``replay_required`` and must supply the
        actual factor cutoff. This prevents a test/backtest helper from gaining
        filesystem side effects and prevents production from inventing a date.
        """
        result = self._validate_candidate_output(self._run_candidate(table, **kwargs))
        should_trace = bool(replay_required or replay_data_date is not None)
        if not should_trace:
            return result
        trace = STRACE.persist_snapshot(
            strategy_id=self.strategy_id,
            selector_id=self.selector_id,
            table=table,
            factor_inputs=self.factor_inputs,
            selection_kwargs=kwargs,
            explicit_data_date=replay_data_date,
        )
        return STRACE.attach_candidate_traces(
            result,
            table=table,
            factor_inputs=self.factor_inputs,
            manifest=trace,
        )

    def replay_candidates(self, snapshot_id: str) -> dict[str, Any]:
        """Re-run this selector from one immutable factor/input snapshot."""
        snapshot = STRACE.load_snapshot(snapshot_id)
        if snapshot.get("strategy_id") != self.strategy_id:
            raise ValueError("strategy replay snapshot belongs to another strategy")
        if snapshot.get("selector_id") != self.selector_id:
            raise ValueError("strategy replay snapshot selector mismatch")
        table, kwargs = STRACE.replay_inputs(snapshot)
        return self._validate_candidate_output(self._run_candidate(table, **kwargs))

    def entry_contract(self, conn) -> dict[str, Any]:
        context = self.runtime(conn)
        return {
            "strategy_id": self.strategy_id,
            "version": context.version,
            "risk_profile": context.risk_profile.to_dict(),
            "execution_profile": dict(context.execution_profile),
            "opening_event": dict(SPOL.opening_event_policy(self.entry_key)),
            "entry_economics": dict(SPOL.entry_economics_policy(self.entry_key)),
        }

    def exit_contract(self, conn) -> dict[str, Any]:
        context = self.runtime(conn)
        recovery = dict(SPOL.RECOVERY_POLICIES.get(self.exit_key) or {})
        min_hold = SPOL.POSITION_REVIEW_MIN_HOLD_DAYS_BY_STRATEGY.get(self.exit_key)
        cooldown = SPOL.RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES.get(self.exit_key)
        return {
            "strategy_id": self.strategy_id,
            "version": context.version,
            "risk_profile": context.risk_profile.to_dict(),
            "intraday_downside": dict(SPOL.intraday_downside_policy(self.exit_key)),
            "recovery": recovery,
            "position_review_min_hold_days": min_hold,
            "risk_reject_cooldown_minutes": cooldown,
        }

    def validate_parameters(
        self,
        conn,
        adjustments: Mapping[str, Any],
        *,
        evidence_count: int | None,
    ) -> dict[str, Any]:
        context = self.runtime(conn)
        if context.compiled_dsl is None:
            if adjustments:
                raise ValueError("strategy has no executable DSL parameter schema")
            return {"strategy_id": self.strategy_id, "version": context.version, "changed": {}}
        applied = context.parameter_schema.apply(
            context.compiled_dsl,
            adjustments,
            evidence_count=evidence_count,
        )
        return {
            "strategy_id": self.strategy_id,
            "version": context.version,
            "changed": applied.changed,
            "structure_checksum": applied.structure_checksum,
        }

    def apply_parameters(
        self,
        conn,
        adjustments: Mapping[str, Any],
        *,
        evidence_count: int | None,
        actor: str = "strategy_plugin",
        change_note: str = "strategy plugin parameter adjustment",
        challenger_win: bool = False,
    ) -> dict[str, Any]:
        return SRT.apply_parameter_adjustments(
            conn,
            self.strategy_id,
            dict(adjustments),
            evidence_count=evidence_count,
            actor=actor,
            change_note=change_note,
            challenger_win=challenger_win,
        )

    def transition(
        self,
        conn,
        to_status: str,
        *,
        reason: str = "",
        actor: str = "strategy_plugin",
    ):
        return SR.transition(
            conn,
            self.strategy_id,
            to_status,
            reason=reason,
            actor=actor,
        )


_PLUGINS: dict[str, StrategyPlugin] = {}
_PLUGINS_BY_SELECTOR: dict[str, StrategyPlugin] = {}


def register_plugin(plugin: StrategyPlugin, *, replace: bool = False) -> StrategyPlugin:
    if not isinstance(plugin, StrategyPlugin):
        raise TypeError("plugin must be StrategyPlugin")
    current = _PLUGINS.get(plugin.strategy_id)
    if current is not None and not replace:
        raise ValueError(f"strategy plugin already registered: {plugin.strategy_id}")
    selector_owner = _PLUGINS_BY_SELECTOR.get(plugin.selector_id)
    if selector_owner is not None and selector_owner.strategy_id != plugin.strategy_id:
        raise ValueError(f"strategy selector already registered: {plugin.selector_id}")
    if current is not None and current.selector_id != plugin.selector_id:
        if _PLUGINS_BY_SELECTOR.get(current.selector_id) is current:
            _PLUGINS_BY_SELECTOR.pop(current.selector_id, None)
    _PLUGINS[plugin.strategy_id] = plugin
    _PLUGINS_BY_SELECTOR[plugin.selector_id] = plugin
    return plugin


def unregister_plugin(strategy_id: str) -> StrategyPlugin | None:
    plugin = _PLUGINS.pop(str(strategy_id or "").strip(), None)
    if plugin is not None and _PLUGINS_BY_SELECTOR.get(plugin.selector_id) is plugin:
        _PLUGINS_BY_SELECTOR.pop(plugin.selector_id, None)
    return plugin


def get_plugin(strategy_id: str) -> StrategyPlugin:
    strategy_id = str(strategy_id or "").strip()
    plugin = _PLUGINS.get(strategy_id)
    if plugin is None:
        raise ValueError(f"strategy plugin is not registered: {strategy_id}")
    return plugin


def plugin_for_selector(selector_id: str) -> StrategyPlugin | None:
    return _PLUGINS_BY_SELECTOR.get(str(selector_id or "").strip())


def plugin_ids() -> tuple[str, ...]:
    return tuple(_PLUGINS)


def selector_ids() -> tuple[str, ...]:
    return tuple(_PLUGINS_BY_SELECTOR)


def manifests(*, conn=None) -> tuple[dict[str, Any], ...]:
    return tuple(plugin.manifest(conn=conn) for plugin in _PLUGINS.values())


def select_candidates(strategy_id: str, table, **kwargs) -> dict[str, Any]:
    return get_plugin(strategy_id).select_candidates(table, **kwargs)


def select_by_selector(selector_id: str, table, **kwargs) -> dict[str, Any]:
    plugin = plugin_for_selector(selector_id)
    if plugin is None:
        raise ValueError(f"strategy selector is not registered: {str(selector_id or '').strip()}")
    return plugin.select_candidates(table, **kwargs)


def replay_candidates(strategy_id: str, snapshot_id: str) -> dict[str, Any]:
    return get_plugin(strategy_id).replay_candidates(snapshot_id)


_BUILTIN_PLUGINS: Sequence[StrategyPlugin] = (
    StrategyPlugin(
        "tq_breakout", "one_to_two",
        ("mom_short", "flow", "volsurge", "sentiment"),
    ),
    StrategyPlugin(
        "trend_pullback", "bottom_reversal",
        ("value", "quality", "volsurge", "flow", "mom_short", "rsi"),
    ),
    StrategyPlugin(
        "sector_rotation", "sentiment_pioneer",
        ("sentiment", "flow", "mom_short", "volsurge", "sector_heat_score"),
    ),
    StrategyPlugin(
        "reported_profit_breakout", "reported_profit_breakout",
        (
            "three_up", "boll_mid_breakout", "above_ma5_5d", "price", "ma5", "ma10",
            "ma20", "ma60", "annual_net_profit", "report_date", "super_net_raw",
        ),
    ),
    StrategyPlugin(
        "main_force_top10", "main_force_top10",
        (
            "amount", "turnover", "pct", "main_pct", "super_net_raw", "main_net",
            "mom20_raw", "sector_heat_score", "float_cap",
        ),
    ),
)

for _plugin in _BUILTIN_PLUGINS:
    register_plugin(_plugin)