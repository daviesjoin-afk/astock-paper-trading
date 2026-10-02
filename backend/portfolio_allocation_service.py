# -*- coding: utf-8 -*-
"""Application service: evaluate and append one exact ``PortfolioAllocationPlan``.

Responsibilities, and nothing more: load the *exact named* portfolio snapshot,
verify its fingerprint, load the exact cycle-pinned strategy resource
declarations, validate the explicit resource intents, evaluate the pure policy,
and append the plan.

It never creates schema, never writes the formal ledger
(``paper_accounts`` / ``paper_cycles`` / orders / fills / position lots / risk
decisions / lifecycle), never submits an order, and offers no ``current`` or
``latest`` fallback and no apply/execute path. Schema creation stays with the
formal migration owner.
"""
from __future__ import annotations

from dataclasses import replace as dataclass_replace

import paper_allocation as PA
import paper_trading as PT
import portfolio_allocation_policy as PAP
import portfolio_allocation_repository as PAPRepo
import portfolio_runtime as PR
import portfolio_runtime_repository as PRRepo
import runtime_settings as RSET
import strategy_dsl_schema as DSL
import strategy_lifecycle as SL
import strategy_registry as SR
import strategy_runtime as SRT


def _allocation_stage(conn, strategy_id: str, lifecycle_state) -> str:
    """Map the exact pinned lifecycle *state* to the allocation *stage*.

    A lifecycle state such as ``paper`` is not an allocation stage; feeding it
    straight to ``paper_allocation.stage_capital_scale`` would fall through the
    unknown-stage branch and silently declare the quarantined scale of ``0.0``.
    The mapping therefore comes from the existing owner
    (``strategy_runtime.lifecycle_stage_for``), which is the same function the
    live allocation path uses, rather than from a second, parallel rule.
    """
    state = str(lifecycle_state or "").strip()
    if not state:
        return "quarantined"
    spec = SR.get(strategy_id, conn=conn)
    if spec is None:
        return "quarantined"
    return SRT.lifecycle_stage_for(dataclass_replace(
        spec, status=state, supports_new_cycle=SL.allows_formal_cycle(state)))


def _declarations(conn, snapshot: PR.PortfolioRuntimeSnapshot):
    """Exact cycle-pinned declarations for the eligible execution participants.

    Identity comes from the snapshot's exact strategy pins (never from the
    current registry head). Caps come from the exact pinned version's compiled
    risk profile, with the declared allocation overrides applied on top.
    """
    pins = {str(pin.get("account_id")): pin for pin in snapshot.strategy_pins}
    declarations = []
    for account in snapshot.execution_participant_ids:
        pin = pins.get(str(account))
        if pin is None:
            raise PAP.PortfolioAllocationPolicyError(
                "exact_cycle_strategy_pin_unavailable")
        strategy_id = str(pin["strategy_id"])
        version = int(pin["strategy_version"])
        checksum = str(pin["strategy_checksum"])
        record = SR.get_version(strategy_id, version, checksum=checksum, conn=conn)
        if record is None:
            raise PAP.PortfolioAllocationPolicyError("exact_strategy_version_unavailable")
        definition = dict(record.definition)
        ast = definition.get("dsl_ast")
        compiled = DSL.normalize(ast) if ast is not None else None
        _fingerprint, risk = SRT.compile_risk_policy(definition, compiled_dsl=compiled)
        soft = risk.soft_limits
        stage = _allocation_stage(conn, strategy_id, pin.get("lifecycle_state"))
        declarations.append(PAP.StrategyResourceDeclaration(
            account_id=str(account),
            strategy_id=strategy_id,
            strategy_version=version,
            strategy_checksum=checksum,
            max_positions=int(PT.ALLOCATION_SLOT_CAPS.get(
                account, soft.get("max_positions", PT.STRATEGY_MAX_POSITIONS))),
            min_positions=int(PT.STRATEGY_MIN_POSITIONS),
            priority_floor_pct=PT.ALLOCATION_PRIORITY_FLOOR_PCT.get(account),
            own_exposure_cap_pct=float(PT.ALLOCATION_OWN_EXPOSURE_CAP_PCT.get(
                account, soft.get("max_exposure", 0.65))),
            lifecycle_stage=stage,
            capital_scale=PA.stage_capital_scale(PA.StrategyRuntime(
                strategy_id=str(account), lifecycle_stage=stage))[0],
        ))
    return declarations


def _intents(values):
    result = []
    for item in values or ():
        if isinstance(item, PAP.ResourceIntent):
            result.append(item)
        else:
            result.append(PAP.ResourceIntent(**dict(item)))
    return tuple(result)


def _with_connection(work, *, initialize=False):
    if initialize:
        with PT._db(immediate=True) as conn:
            result = work(conn)
            conn.commit()
            return result
    with PT._db_readonly() as conn:
        return work(conn)


def capture_portfolio_allocation_plan(*, portfolio_snapshot_id: str,
                                      allocation_weights,
                                      resource_intents=(), hard_pool_cap=None):
    """Evaluate the policy for one explicit snapshot and append the plan.

    ``allocation_weights`` is a required explicit canonical declaration: the
    service never falls back to a dynamic factor or to ``1.0``. Coverage must be
    exactly the snapshot's eligible execution participants, which the policy
    enforces.
    """
    if not isinstance(portfolio_snapshot_id, str) or len(portfolio_snapshot_id) != 64:
        raise PAP.PortfolioAllocationPolicyError("explicit_portfolio_snapshot_id_required")
    intents = _intents(resource_intents)

    def build(conn):
        snapshot = PRRepo.get_snapshot(conn, portfolio_snapshot_id)
        if snapshot is None:
            raise PAP.PortfolioAllocationPolicyError("portfolio_snapshot_not_found")
        if not PR.verify_snapshot_fingerprint(snapshot):
            raise PAP.PortfolioAllocationPolicyError(
                "portfolio_snapshot_fingerprint_mismatch")
        cap = (int(hard_pool_cap) if hard_pool_cap is not None else
               int(RSET.get(conn, "shared_pool_position_limit",
                            PT.SHARED_POOL_MAX_POSITIONS)))
        plan = PAP.build_portfolio_allocation_plan(
            snapshot=snapshot,
            declarations=_declarations(conn, snapshot),
            weights=allocation_weights,
            intents=intents,
            hard_pool_cap=cap,
            strategy_max_positions=PT.STRATEGY_MAX_POSITIONS,
            strategy_min_positions=PT.STRATEGY_MIN_POSITIONS,
            protected_slot_floor=PT.STRATEGY_PROTECTED_SLOT_FLOOR,
            account_order={key: index for index, key in enumerate(PT.ACCOUNT_SPECS)},
            baseline_exposure=None,
        )
        return PAPRepo.append_plan(conn, plan)

    return _with_connection(build, initialize=True).projection()


def get_portfolio_allocation_plan(plan_id: str):
    """Read only the exact immutable plan named by its fingerprint ID."""

    def read(conn):
        plan = PAPRepo.get_plan(conn, plan_id)
        if plan is None:
            raise PAP.PortfolioAllocationPolicyError("portfolio_allocation_plan_not_found")
        return plan.projection()

    return _with_connection(read)


__all__ = ["capture_portfolio_allocation_plan", "get_portfolio_allocation_plan"]
