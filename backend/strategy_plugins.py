# -*- coding: utf-8 -*-
"""Unified strategy plugin contract over the existing versioned runtime.

The repository already has authoritative strategy definitions, immutable versions,
parameter schemas, risk fingerprints and execution profiles.  This module does
not duplicate any of them.  It only binds a stable strategy id to its candidate
selector and exposes the existing runtime/policy objects through one interface.

Adding a new native strategy therefore means registering a ``StrategyPlugin``;
matching, capital-pool and order execution code remain strategy-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import strategy_policies as SPOL
import strategy_registry as SR
import strategy_runtime as SRT

CandidateRunner = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class StrategyPlugin:
    """One strategy's candidate, lifecycle, parameter and risk contract.

    ``selector_id`` is the existing candidate implementation key.  Built-ins
    deliberately keep their audited selector functions in ``strategies.py``;
    custom plugins may provide ``candidate_runner`` and need no core dispatch
    change.  Version/schema/risk values are always read from the authoritative
    registry/runtime instead of being copied into this descriptor.
    """

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

    @property
    def entry_key(self) -> str:
        return str(self.entry_policy_key or self.strategy_id)

    @property
    def exit_key(self) -> str:
        return str(self.exit_policy_key or self.strategy_id)

    def spec(self, *, conn=None):
        """Return the authoritative registry spec for this plugin."""
        spec = SR.get(self.strategy_id, conn=conn)
        if spec is None:
            raise ValueError(f"strategy is not registered: {self.strategy_id}")
        return spec

    def runtime(self, conn):
        """Compile the authoritative immutable runtime for the current version."""
        return SRT.get_context(conn, self.strategy_id)

    def manifest(self, *, conn=None) -> dict[str, Any]:
        """Expose id/name/version/schema plus the plugin's I/O declaration."""
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

    def select_candidates(self, table, **kwargs) -> dict[str, Any]:
        """Run the candidate selector and validate its shared output envelope."""
        if self.candidate_runner is not None:
            result = self.candidate_runner(table, **kwargs)
        else:
            # Lazy import avoids a strategies <-> plugin import cycle and leaves
            # audited algorithm functions in their existing module.
            import strategies as S
            result = S.run_strategy(self.selector_id, table, **kwargs)
        if not isinstance(result, Mapping):
            raise TypeError("strategy candidate runner must return an object")
        missing = [key for key in self.candidate_output_keys if key not in result]
        if missing:
            raise ValueError(f"strategy candidate output missing key: {missing[0]}")
        if not isinstance(result.get("picks"), list):
            raise ValueError("strategy candidate output picks must be a list")
        return dict(result)

    def entry_contract(self, conn) -> dict[str, Any]:
        """Return entry/risk inputs without copying strategy-specific thresholds."""
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
        """Return exit/review inputs using the existing independent policy tables."""
        context = self.runtime(conn)
        return {
            "strategy_id": self.strategy_id,
            "version": context.version,
            "risk_profile": context.risk_profile.to_dict(),
            "intraday_downside": dict(SPOL.intraday_downside_policy(self.exit_key)),
            "recovery": dict(SPOL.recovery_policy(self.exit_key)),
            "position_review_min_hold_days": SPOL.position_review_min_hold_days(self.exit_key),
            "risk_reject_cooldown_minutes": SPOL.risk_reject_cooldown_minutes(self.exit_key),
        }

    def validate_parameters(
        self,
        conn,
        adjustments: Mapping[str, Any],
        *,
        evidence_count: int | None,
    ) -> dict[str, Any]:
        """Validate parameter-only changes without persisting a new version."""
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
        """Apply a schema/risk-gated parameter change through the canonical runtime."""
        return SRT.apply_parameter_adjustments(
            conn,
            self.strategy_id,
            dict(adjustments),
            evidence_count=evidence_count,
            actor=actor,
            change_note=change_note,
            challenger_win=challenger_win,
        )

    def transition(self, conn, to_status: str, *, reason: str = "", actor: str = "strategy_plugin"):
        """Route strategy enable/disable lifecycle changes through the registry."""
        return SR.transition(
            conn,
            self.strategy_id,
            to_status,
            reason=reason,
            actor=actor,
        )


_PLUGINS: dict[str, StrategyPlugin] = {}


def register_plugin(plugin: StrategyPlugin, *, replace: bool = False) -> StrategyPlugin:
    """Register one plugin without changing matching or execution core code."""
    if not isinstance(plugin, StrategyPlugin):
        raise TypeError("plugin must be StrategyPlugin")
    current = _PLUGINS.get(plugin.strategy_id)
    if current is not None and not replace:
        raise ValueError(f"strategy plugin already registered: {plugin.strategy_id}")
    _PLUGINS[plugin.strategy_id] = plugin
    return plugin


def unregister_plugin(strategy_id: str) -> StrategyPlugin | None:
    """Remove a runtime registration; durable strategy definitions are untouched."""
    return _PLUGINS.pop(str(strategy_id or "").strip(), None)


def get_plugin(strategy_id: str) -> StrategyPlugin:
    strategy_id = str(strategy_id or "").strip()
    plugin = _PLUGINS.get(strategy_id)
    if plugin is None:
        raise ValueError(f"strategy plugin is not registered: {strategy_id}")
    return plugin


def plugin_ids() -> tuple[str, ...]:
    return tuple(_PLUGINS)


def manifests(*, conn=None) -> tuple[dict[str, Any], ...]:
    return tuple(plugin.manifest(conn=conn) for plugin in _PLUGINS.values())


def select_candidates(strategy_id: str, table, **kwargs) -> dict[str, Any]:
    return get_plugin(strategy_id).select_candidates(table, **kwargs)


# Current account->candidate bindings already used by paper_trading.ACCOUNT_SPECS.
# Keeping the bindings here makes them an explicit plugin contract instead of an
# implicit convention.  Selector algorithms and all hard execution/risk gates
# remain in their existing modules.
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
