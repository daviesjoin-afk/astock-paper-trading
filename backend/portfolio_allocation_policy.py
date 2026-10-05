# -*- coding: utf-8 -*-
"""Deterministic multi-strategy allocation & conflict policy (R34-B).

This module is a **pure** policy layer. It receives one exact
``PortfolioRuntimeSnapshot``, the exact cycle-pinned strategy resource
declarations, the explicit canonical allocation weights, and the explicit
resource intents, and it returns one immutable ``PortfolioAllocationPlan``.

It reads no database, no network, no clock, no provider, and no current/latest
lookup. It never recomputes a Risk ALLOW/BLOCK decision and never produces a
strategy signal.

Authority boundaries kept intact::

    Strategy  = decides what it wants to do
    Allocator = decides how much resource, and who uses a contended resource first
    Risk      = decides whether the action is permitted

Allocation arithmetic is **not** reimplemented here. Slot allocation delegates
to ``paper_allocation.position_limits_from_weights``, which is the single
arithmetic owner shared with the legacy ``position_limits`` adapter. Capital
allocation delegates to ``paper_allocation.strategy_pool_budget`` /
``paper_allocation.pool_headroom``.

Evidence discipline (the point of this stage): a missing owner fact is never
turned into a usable number. Missing capacity is **not** zero pending; missing
market valuation is **not** cost basis; missing correlation is **not** zero;
missing concentration classification is **not** a concentration fact; a
dataclass default of ``1.0`` is **not** owner-issued evidence. When a plan
component cannot be computed from exact evidence it reports
``INSUFFICIENT_EVIDENCE`` or ``UNAVAILABLE`` with the exact blocking reason.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import paper_allocation as PA
import portfolio_runtime as PR

POLICY_VERSION = "portfolio-allocation-policy-v2"
PLAN_SCHEMA_VERSION = "portfolio-allocation-plan-v1"

# ─── plan component status ───────────────────────────────────────────────────
PLANNED = "PLANNED"
PARTIAL = "PARTIAL"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NO_ELIGIBLE_STRATEGIES = "NO_ELIGIBLE_STRATEGIES"
UNAVAILABLE = "UNAVAILABLE"
COMPONENT_STATUSES = (PLANNED, INSUFFICIENT_EVIDENCE, NO_ELIGIBLE_STRATEGIES,
                      UNAVAILABLE)
PLAN_STATUSES = (PLANNED, PARTIAL, INSUFFICIENT_EVIDENCE, NO_ELIGIBLE_STRATEGIES)

# ─── canonical intent vocabulary ─────────────────────────────────────────────
# Sourced from the canonical policy contract, NOT from the legacy coordinator.
# Ordering is resource *execution* priority. It is never a Risk approval.
INTENT_KINDS = (
    "RISK_EXIT",
    "TAKE_PROFIT_EXIT",
    "MANUAL_EXIT",
    "RISK_REDUCE",
    "NEW_ENTRY",
    "ADD_POSITION",
)
INTENT_PRIORITY = {kind: index for index, kind in enumerate(INTENT_KINDS)}
EXIT_INTENT_KINDS = ("RISK_EXIT", "TAKE_PROFIT_EXIT", "MANUAL_EXIT", "RISK_REDUCE")
ENTRY_INTENT_KINDS = ("NEW_ENTRY", "ADD_POSITION")

# Vocabulary intentionally free of any text-derivation helper: an intent's kind
# must be supplied explicitly by the upstream owner.
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PortfolioAllocationPolicyError(ValueError):
    """Invalid identity, declaration, weight set, or intent on the canonical path."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(v) for v in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PortfolioAllocationPolicyError("allocation_plan_not_json_serializable")


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _finite_number(value, *, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioAllocationPolicyError(f"canonical_{what}_invalid") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise PortfolioAllocationPolicyError(f"canonical_{what}_invalid")
    return number


# ─── exact declarations ──────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class StrategyResourceDeclaration:
    """One strategy's exact, cycle-pinned allocation declaration.

    Every field here is an owner-issued declaration. This is the *only* source
    of canonical allocation weights and caps; no dynamic factor is consulted.
    """

    account_id: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    max_positions: int
    min_positions: int = 0
    priority_floor_pct: float | None = None
    own_exposure_cap_pct: float | None = None
    lifecycle_stage: str = "standard"
    capital_scale: float | None = None

    def __post_init__(self):
        account = str(self.account_id or "")
        if not account:
            raise PortfolioAllocationPolicyError("canonical_declaration_account_required")
        if not str(self.strategy_id or ""):
            raise PortfolioAllocationPolicyError("canonical_declaration_strategy_required")
        try:
            version = int(self.strategy_version)
        except (TypeError, ValueError) as exc:
            raise PortfolioAllocationPolicyError(
                "canonical_declaration_version_invalid") from exc
        if version < 1:
            raise PortfolioAllocationPolicyError("canonical_declaration_version_invalid")
        if not _SHA256.fullmatch(str(self.strategy_checksum or "")):
            raise PortfolioAllocationPolicyError("canonical_declaration_checksum_invalid")
        try:
            cap = int(self.max_positions)
            floor = int(self.min_positions)
        except (TypeError, ValueError) as exc:
            raise PortfolioAllocationPolicyError(
                "canonical_declaration_slots_invalid") from exc
        if cap < 0 or floor < 0:
            # A declared floor above the declared cap is permitted and clamped by
            # the arithmetic owner, matching legacy ``position_limits`` behavior.
            raise PortfolioAllocationPolicyError("canonical_declaration_slots_invalid")
        for name in ("priority_floor_pct", "own_exposure_cap_pct"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= _finite_number(
                    value, what=name) <= 1.0:
                raise PortfolioAllocationPolicyError(f"canonical_declaration_{name}_invalid")
        if self.capital_scale is not None and not 0.0 <= _finite_number(
                self.capital_scale, what="capital_scale") <= 1.0:
            raise PortfolioAllocationPolicyError("canonical_declaration_capital_scale_invalid")

    def projection(self) -> dict:
        return {"account_id": str(self.account_id), "strategy_id": str(self.strategy_id),
                "strategy_version": int(self.strategy_version),
                "strategy_checksum": str(self.strategy_checksum),
                "max_positions": int(self.max_positions),
                "min_positions": int(self.min_positions),
                "priority_floor_pct": self.priority_floor_pct,
                "own_exposure_cap_pct": self.own_exposure_cap_pct,
                "lifecycle_stage": str(self.lifecycle_stage),
                "capital_scale": self.capital_scale}

    def as_runtime(self, weight: float) -> PA.StrategyRuntime:
        """Declared declaration -> the arithmetic owner's declarative input.

        Only declared fields are set. The dynamic factor fields keep their
        library defaults because the arithmetic core here is weight-driven and
        never reads them; nothing claims they are owner-issued evidence.
        """
        return PA.StrategyRuntime(
            strategy_id=str(self.account_id),
            base_priority=max(_finite_number(weight, what="weight"), 1e-9),
            max_positions=int(self.max_positions),
            min_positions=int(self.min_positions),
            priority_floor_pct=self.priority_floor_pct,
            own_exposure_cap_pct=self.own_exposure_cap_pct,
            lifecycle_stage=str(self.lifecycle_stage),
            capital_scale=self.capital_scale,
        )


@dataclass(frozen=True, slots=True)
class ResourceIntent:
    """One explicit resource intent. ``intent_kind`` must be supplied, not inferred."""

    intent_id: str
    account_id: str
    intent_kind: str
    symbol: str
    requested_amount: float | None = None
    source_identity: str = ""

    def __post_init__(self):
        if not str(self.intent_id or ""):
            raise PortfolioAllocationPolicyError("explicit_intent_id_required")
        if not str(self.account_id or ""):
            raise PortfolioAllocationPolicyError("explicit_intent_account_required")
        if not str(self.symbol or ""):
            raise PortfolioAllocationPolicyError("explicit_intent_symbol_required")
        kind = str(self.intent_kind or "")
        if kind not in INTENT_PRIORITY:
            # Free text, reasons, purposes, and audit strings cannot become a
            # canonical intent kind.
            raise PortfolioAllocationPolicyError("canonical_intent_kind_required")
        if self.requested_amount is not None:
            amount = _finite_number(self.requested_amount, what="intent_amount")
            if amount < 0.0:
                raise PortfolioAllocationPolicyError("canonical_intent_amount_invalid")

    def projection(self) -> dict:
        return {"intent_id": str(self.intent_id), "account_id": str(self.account_id),
                "intent_kind": str(self.intent_kind), "symbol": str(self.symbol),
                "requested_amount": self.requested_amount,
                "source_identity": str(self.source_identity)}


# ─── evidence gates ──────────────────────────────────────────────────────────
def _dimension(snapshot: PR.PortfolioRuntimeSnapshot, name: str) -> PR.PortfolioDimension:
    for dimension in snapshot.dimensions:
        if dimension.name == name:
            return dimension
    raise PortfolioAllocationPolicyError("canonical_snapshot_dimension_missing")


def _gate_reasons(dimension: PR.PortfolioDimension, required_facts) -> tuple[str, ...]:
    """A fact that is not exactly AVAILABLE is a blocking reason, never a zero."""
    reasons = []
    if dimension.status != PR.AVAILABLE:
        reasons.extend(dimension.blocking_reasons or (f"{dimension.name}_not_available",))
    for fact in required_facts:
        if dimension.facts.get(fact) is None:
            reasons.append(f"exact_{fact}_unavailable")
    return tuple(sorted({str(reason) for reason in reasons}))


def _capital_plan(snapshot: PR.PortfolioRuntimeSnapshot, declarations,
                  weights, *, shared_pool_max_exposure,
                  strategy_pool_floor_ratio) -> dict:
    """Compute exact allowances through paper_allocation's single arithmetic owner."""
    exposure = _dimension(snapshot, "strategy_exposure")
    capacity = _dimension(snapshot, "capacity")
    capital = _dimension(snapshot, "capital")
    missing = (list(_gate_reasons(exposure, ("market_value_by_account",)))
               + list(_gate_reasons(capacity, ("pending_by_account", "pending_total",
                                               "headroom")))
               + list(_gate_reasons(capital, ("nav",))))
    if shared_pool_max_exposure is None:
        missing.append("explicit_shared_pool_exposure_cap_unavailable")
    if strategy_pool_floor_ratio is None:
        missing.append("explicit_strategy_pool_floor_ratio_unavailable")
    unique = sorted({str(reason) for reason in missing})
    if unique:
        return {
            "status": INSUFFICIENT_EVIDENCE,
            "authority": "resource_allowance_only_not_trade_permission",
            "allowance_by_strategy": None,
            "blocking_reasons": unique,
            "cost_basis_used_as_market_value": False,
            "market_value_by_account": (
                _thaw(exposure.facts.get("market_value_by_account"))
                if exposure.status == PR.AVAILABLE else None),
            "required_evidence": ["market_valued_strategy_exposure",
                                  "asof_pending_capacity", "explicit_nav",
                                  "explicit_pool_allocation_limits"],
        }

    try:
        nav = _finite_number(capital.facts["nav"], what="nav")
        pending_total = _finite_number(capacity.facts["pending_total"],
                                       what="pending_total")
        pool_cap = _finite_number(shared_pool_max_exposure,
                                  what="shared_pool_max_exposure")
        floor_ratio = _finite_number(strategy_pool_floor_ratio,
                                     what="strategy_pool_floor_ratio")
        if nav <= 0 or not 0 <= pool_cap <= 1 or not 0 <= floor_ratio <= 1:
            raise ValueError
        values = {str(key): _finite_number(value, what="market_value")
                  for key, value in dict(
                      exposure.facts["market_value_by_account"]).items()}
        pending_by_account = {
            str(key): _finite_number(value, what="pending_by_account")
            for key, value in dict(capacity.facts["pending_by_account"]).items()
        }
        # Exposure belongs to every economic owner, including paused or
        # risk-exit-only participants. Only the pending map and weights are
        # scoped to eligible new-resource participants.
        if (set(values) != set(snapshot.economic_owner_ids)
                or not set(weights).issubset(values)
                or set(pending_by_account) != set(weights)):
            raise ValueError
        cash_headroom = _finite_number(capacity.facts.get("headroom"),
                                       what="capacity_headroom")
        used = sum(values.values()) + pending_total
        cash_limited_pool_cap = min(pool_cap, max(used + max(cash_headroom, 0.0), 0.0) / nav)
        runtime_by_account = {
            declaration.account_id: declaration.as_runtime(weights[declaration.account_id])
            for declaration in declarations
        }
        arithmetic = PA.allocation_plan(
            tuple(runtime_by_account.values()), nav=nav, values=values,
            pending_by_account=pending_by_account, pending_total=pending_total,
            prices_by_strategy={}, shared_pool_max_exposure=cash_limited_pool_cap,
            strategy_pool_floor_ratio=floor_ratio,
        )
        rows = {str(row["strategy_id"]): row for row in arithmetic["plan"]}
        allowance = {
            account: _finite_number(row["scaled_budget_amount"],
                                    what="scaled_budget_amount")
            for account, row in rows.items()
        }
        stages = {account: str(row["lifecycle_stage"])
                  for account, row in rows.items()}
        scales = {account: _finite_number(row["capital_scale"],
                                           what="capital_scale")
                  for account, row in rows.items()}
        return {
            "status": PLANNED,
            "authority": "paper_allocation:allocation_plan",
            "allowance_by_strategy": allowance,
            "lifecycle_stage_by_strategy": stages,
            "capital_scale_by_strategy": scales,
            "raw_allowance_by_strategy": {
                account: _finite_number(row["raw_allowance_amount"],
                                        what="raw_allowance_amount")
                for account, row in rows.items()},
            "market_value_by_account": values,
            "pending_by_account": pending_by_account,
            "pending_total": pending_total,
            "nav": nav,
            "capacity_headroom": cash_headroom,
            "pool_exposure_cap": pool_cap,
            "cash_limited_pool_exposure_cap": cash_limited_pool_cap,
            "arithmetic_engine": arithmetic["engine"],
            "blocking_reasons": [],
            "cost_basis_used_as_market_value": False,
            "required_evidence": ["market_valued_strategy_exposure",
                                  "asof_pending_capacity", "explicit_nav",
                                  "explicit_pool_allocation_limits"],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PortfolioAllocationPolicyError(
            "canonical_capital_evidence_invalid") from exc


def _capacity_plan(snapshot: PR.PortfolioRuntimeSnapshot) -> dict:
    """Absent capacity evidence must not become ``pending = 0`` or unlimited.

    With exact owner-issued capacity facts the plan echoes them verbatim; it
    never derives them and never substitutes a default when they are missing.
    """
    capacity = _dimension(snapshot, "capacity")
    reasons = _gate_reasons(capacity, ("used", "pending", "headroom"))
    if reasons:
        return {
            "status": INSUFFICIENT_EVIDENCE,
            "used_amount": None,
            "pending_amount": None,
            "headroom_amount": None,
            "blocking_reasons": list(reasons),
            "missing_pending_treated_as_zero": False,
            "missing_capacity_treated_as_unlimited": False,
        }
    return {
        "status": PLANNED,
        "authority": "owner_issued_capacity_facts_echoed_not_computed",
        "used_amount": _finite_number(capacity.facts["used"], what="capacity_used"),
        "pending_amount": _finite_number(capacity.facts["pending"], what="capacity_pending"),
        "headroom_amount": _finite_number(capacity.facts["headroom"], what="capacity_headroom"),
        "blocking_reasons": [],
        "missing_pending_treated_as_zero": False,
        "missing_capacity_treated_as_unlimited": False,
    }


def _concentration_adjustment(snapshot: PR.PortfolioRuntimeSnapshot) -> dict:
    """No owner issues a concentration adjustment rule; classification absent."""
    concentration = _dimension(snapshot, "concentration")
    if concentration.status != PR.AVAILABLE:
        reasons = list(concentration.blocking_reasons) or ["concentration_not_available"]
    else:
        reasons = ["no_owner_issued_concentration_adjustment_rule"]
    return {"status": UNAVAILABLE, "adjustment": None,
            "blocking_reasons": sorted({str(reason) for reason in reasons}),
            "classification_guessed_from_name_or_sector": False}


def _correlation_term(snapshot: PR.PortfolioRuntimeSnapshot) -> dict:
    """Return correlation is UNAVAILABLE; overlap/sector never stand in for it."""
    correlation = _dimension(snapshot, "correlation")
    reasons = (list(correlation.blocking_reasons) if correlation.status != PR.AVAILABLE
               else ["exact_strategy_version_return_series_unavailable"])
    return {"status": UNAVAILABLE, "correlation": None,
            "blocking_reasons": sorted({str(reason) for reason in reasons}),
            "overlap_or_sector_substituted": False}


def _slot_plan(snapshot: PR.PortfolioRuntimeSnapshot, declarations, weights, *,
               hard_pool_cap, strategy_max_positions, strategy_min_positions,
               protected_slot_floor, account_order, baseline_exposure) -> dict:
    """Slot allocation delegates to the single arithmetic owner."""
    if not snapshot.execution_participant_ids:
        return {
            "status": NO_ELIGIBLE_STRATEGIES,
            "eligible_source": "PortfolioRuntimeSnapshot.execution_participant_ids",
            "limits": {}, "total_slots": 0, "total_cap": 0,
            "declared_caps": {}, "declared_mins": {},
            "applied_allocation_weights": {},
            "blocking_reasons": ["no_execution_participant_has_new_resource_eligibility"],
        }
    caps = {declaration.account_id: int(declaration.max_positions)
            for declaration in declarations}
    mins = {declaration.account_id: int(declaration.min_positions)
            for declaration in declarations}
    arithmetic = PA.position_limits_from_weights(
        dict(weights), caps, mins,
        hard_pool_cap=hard_pool_cap,
        strategy_max_positions=int(strategy_max_positions),
        strategy_min_positions=int(strategy_min_positions),
        protected_slot_floor=protected_slot_floor,
        account_order=account_order,
        baseline_exposure=baseline_exposure,
    )
    return {
        "status": PLANNED,
        "eligible_source": "PortfolioRuntimeSnapshot.execution_participant_ids",
        "engine": arithmetic["engine"],
        "hard_pool_cap": int(hard_pool_cap),
        "total_cap": arithmetic["total_cap"],
        "total_slots": sum(arithmetic["limits"].values()),
        "risk_scale": arithmetic["risk_scale"],
        "protected_slot_floor": arithmetic["protected_slot_floor"],
        "limits": arithmetic["limits"],
        "declared_caps": caps,
        "declared_mins": mins,
        "applied_allocation_weights": arithmetic["effective_weights"],
        "blocking_reasons": [],
    }


def _conflict_plan(snapshot: PR.PortfolioRuntimeSnapshot, intents) -> dict:
    """Deterministic arbitration over *explicit* intents only.

    Opposite intents on one symbol are never netted away: both keep their own
    provenance and their own arbitration result. A RISK_EXIT is never deferred
    behind an entry; entries behind an in-flight RISK_EXIT are recorded as
    deferred rather than dropped.
    """
    eligible = set(snapshot.execution_participant_ids)
    exit_scope = set(snapshot.risk_exit_participant_ids)
    conflict_evidence = _dimension(snapshot, "signal_conflicts")
    unknown_orders = list(conflict_evidence.facts.get("unknown_orders") or ())
    seen = set()
    for intent in intents:
        if intent.intent_id in seen:
            raise PortfolioAllocationPolicyError("duplicate_explicit_intent_id")
        seen.add(intent.intent_id)
    rows = []
    for intent in intents:
        kind = str(intent.intent_kind)
        account = str(intent.account_id)
        entry = kind in ENTRY_INTENT_KINDS
        new_ok = bool(entry and account in eligible)
        exit_ok = bool((not entry) and account in exit_scope)
        denial = None
        if entry and not new_ok:
            denial = "new_resource_eligibility_requires_execution_participant"
        elif not entry and not exit_ok:
            denial = "exit_right_requires_risk_exit_scope"
        rows.append({
            "intent_id": str(intent.intent_id), "account_id": account,
            "intent_kind": kind, "symbol": str(intent.symbol),
            "requested_amount": intent.requested_amount,
            "priority": INTENT_PRIORITY[kind],
            "new_resource_eligible": new_ok,
            "exit_right_eligible": exit_ok,
            "denial_reason": denial,
            "source_identity": str(intent.source_identity),
        })
    rows.sort(key=lambda row: (row["priority"], row["symbol"], row["account_id"],
                               row["intent_id"]))
    by_symbol: dict[str, list] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    arbitration = []
    deferred: list[str] = []
    unknown_order_blocks = []
    unscoped_unknown = any(not str(item.get("symbol") or "").strip()
                           for item in unknown_orders)
    if unscoped_unknown:
        blocked_ids = [row["intent_id"] for row in rows
                       if row["intent_kind"] in ENTRY_INTENT_KINDS
                       and row["new_resource_eligible"]]
        deferred.extend(blocked_ids)
        unknown_order_blocks.append({
            "symbol": None, "blocking_order_ids": sorted(
                int(item["order_id"]) for item in unknown_orders
                if item.get("order_id") is not None),
            "deferred_intent_ids": sorted(blocked_ids),
            "rule": "unscoped_unknown_pending_order_blocks_entries",
        })
    else:
        for symbol in sorted({str(item.get("symbol") or "")
                              for item in unknown_orders}):
            blockers = [item for item in unknown_orders
                        if str(item.get("symbol") or "") == symbol]
            blocked_ids = [row["intent_id"] for row in rows
                           if row["symbol"] == symbol
                           and row["intent_kind"] in ENTRY_INTENT_KINDS
                           and row["new_resource_eligible"]]
            if blocked_ids:
                deferred.extend(blocked_ids)
                unknown_order_blocks.append({
                    "symbol": symbol,
                    "blocking_order_ids": sorted(
                        int(item["order_id"]) for item in blockers
                        if item.get("order_id") is not None),
                    "deferred_intent_ids": sorted(blocked_ids),
                    "rule": "unknown_pending_order_blocks_same_symbol_entry",
                })
    for symbol in sorted(by_symbol):
        group = by_symbol[symbol]
        # Only intents the policy actually grants may contend for a symbol. An
        # already-denied intent must not defer a valid one.
        blockers = [row for row in group if row["intent_kind"] == "RISK_EXIT"
                    and row["exit_right_eligible"]]
        entries = [row for row in group if row["intent_kind"] in ENTRY_INTENT_KINDS
                   and row["new_resource_eligible"]]
        if blockers and entries:
            blocking_ids = [row["intent_id"] for row in blockers]
            deferred_ids = [row["intent_id"] for row in entries]
            deferred.extend(deferred_ids)
            arbitration.append({
                "symbol": symbol,
                "blocking_intent_ids": blocking_ids,
                "deferred_intent_ids": deferred_ids,
                "rule": "risk_exit_outranks_new_entry_and_add_position",
            })
    return {
        "status": (INSUFFICIENT_EVIDENCE if unscoped_unknown else
                   PARTIAL if unknown_orders else PLANNED),
        "authority": "resource_execution_priority_only_not_risk_approval",
        "intent_ordering": list(INTENT_KINDS),
        "ordered_intents": rows,
        "arbitration": arbitration,
        "unknown_order_blocks": unknown_order_blocks,
        "blocking_reasons": (["unscoped_unknown_pending_order"] if unscoped_unknown
                             else ["unknown_pending_order_conflict"] if unknown_orders
                             else []),
        "deferred_intent_ids": sorted(set(deferred)),
        "opposite_intents_netted": False,
        "intent_kind_source": "explicit_upstream_declaration",
    }


# ─── plan ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PortfolioAllocationPlan:
    plan_id: str
    plan_fingerprint: str
    portfolio_snapshot_id: str
    portfolio_snapshot_fingerprint: str
    allocation_policy_version: str
    cycle_id: int
    asof_day: str
    decision_at: str
    strategy_pins: tuple
    eligible_resource_strategy_ids: tuple
    strategy_resource_declarations: tuple
    allocation_weights: Mapping
    slot_plan: Mapping
    capital_plan: Mapping
    conflict_plan: Mapping
    capacity_plan: Mapping
    concentration_adjustment: Mapping
    correlation_term: Mapping
    plan_status: str
    blocking_reasons: tuple
    source_identities: Mapping
    source_fingerprints: Mapping
    plan_schema_version: str = PLAN_SCHEMA_VERSION

    def projection(self) -> dict:
        return {
            "plan_schema_version": self.plan_schema_version,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "allocation_policy_version": self.allocation_policy_version,
            "portfolio_snapshot_id": self.portfolio_snapshot_id,
            "portfolio_snapshot_fingerprint": self.portfolio_snapshot_fingerprint,
            "cycle_id": self.cycle_id,
            "asof_day": self.asof_day,
            "decision_at": self.decision_at,
            "strategy_pins": [_thaw(pin) for pin in self.strategy_pins],
            "eligible_resource_strategy_ids": list(self.eligible_resource_strategy_ids),
            "strategy_resource_declarations": [_thaw(item)
                                               for item in self.strategy_resource_declarations],
            "allocation_weights": _thaw(self.allocation_weights),
            "plan_status": self.plan_status,
            "slot_plan": _thaw(self.slot_plan),
            "capital_plan": _thaw(self.capital_plan),
            "conflict_plan": _thaw(self.conflict_plan),
            "capacity_plan": _thaw(self.capacity_plan),
            "concentration_adjustment": _thaw(self.concentration_adjustment),
            "correlation_term": _thaw(self.correlation_term),
            "blocking_reasons": list(self.blocking_reasons),
            "source_identities": _thaw(self.source_identities),
            "source_fingerprints": _thaw(self.source_fingerprints),
        }

    def fingerprint_material(self) -> dict:
        value = self.projection()
        value.pop("plan_id")
        value.pop("plan_fingerprint")
        return value


def verify_plan_fingerprint(plan: PortfolioAllocationPlan) -> bool:
    return (isinstance(plan, PortfolioAllocationPlan)
            and plan.plan_id == plan.plan_fingerprint
            and _sha(plan.fingerprint_material()) == plan.plan_fingerprint)


def plan_from_projection(value: Mapping) -> PortfolioAllocationPlan:
    """Rebuild one plan from its persisted projection so it can be verified.

    Reconstruction is deliberately structural: every component is read back as
    stored data, and :func:`verify_plan_fingerprint` then re-derives the
    fingerprint. Tampering with any persisted component therefore fails.
    """
    if not isinstance(value, Mapping):
        raise PortfolioAllocationPolicyError("allocation_plan_projection_invalid")
    try:
        plan = PortfolioAllocationPlan(
            plan_id=str(value["plan_id"]),
            plan_fingerprint=str(value["plan_fingerprint"]),
            portfolio_snapshot_id=str(value["portfolio_snapshot_id"]),
            portfolio_snapshot_fingerprint=str(value["portfolio_snapshot_fingerprint"]),
            allocation_policy_version=str(value["allocation_policy_version"]),
            cycle_id=int(value["cycle_id"]),
            asof_day=str(value["asof_day"]),
            decision_at=str(value["decision_at"]),
            strategy_pins=tuple(_freeze(item) for item in value["strategy_pins"]),
            eligible_resource_strategy_ids=tuple(
                str(item) for item in value["eligible_resource_strategy_ids"]),
            strategy_resource_declarations=tuple(
                _freeze(item) for item in value["strategy_resource_declarations"]),
            allocation_weights=_freeze(value["allocation_weights"]),
            slot_plan=_freeze(value["slot_plan"]),
            capital_plan=_freeze(value["capital_plan"]),
            conflict_plan=_freeze(value["conflict_plan"]),
            capacity_plan=_freeze(value["capacity_plan"]),
            concentration_adjustment=_freeze(value["concentration_adjustment"]),
            correlation_term=_freeze(value["correlation_term"]),
            plan_status=str(value["plan_status"]),
            blocking_reasons=tuple(str(item) for item in value["blocking_reasons"]),
            source_identities=_freeze(value["source_identities"]),
            source_fingerprints=_freeze(value["source_fingerprints"]),
            plan_schema_version=str(value.get("plan_schema_version", PLAN_SCHEMA_VERSION)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PortfolioAllocationPolicyError("allocation_plan_projection_invalid") from exc
    return plan


def _plan_status(eligible, components) -> str:
    if not eligible:
        return NO_ELIGIBLE_STRATEGIES
    plannable = [components["slot_plan"], components["capital_plan"],
                 components["conflict_plan"], components["capacity_plan"]]
    planned = sum(1 for item in plannable if item["status"] == PLANNED)
    if planned == len(plannable):
        return PLANNED
    return PARTIAL if planned else INSUFFICIENT_EVIDENCE


def build_portfolio_allocation_plan(
    *,
    snapshot: PR.PortfolioRuntimeSnapshot,
    declarations: Sequence[StrategyResourceDeclaration],
    weights: Mapping[str, float],
    intents: Sequence[ResourceIntent] = (),
    hard_pool_cap,
    strategy_max_positions,
    strategy_min_positions,
    protected_slot_floor,
    account_order: Mapping[str, int] | None = None,
    baseline_exposure: float | None = None,
    source_identities: Mapping[str, str] | None = None,
    source_fingerprints: Mapping[str, str] | None = None,
    shared_pool_max_exposure=None,
    strategy_pool_floor_ratio=None,
) -> PortfolioAllocationPlan:
    """Build one immutable allocation plan from exact, fully explicit inputs.

    Fails closed for a missing, corrupt, or non-exact portfolio snapshot; for a
    canonical weight set that does not exactly cover the eligible resource
    strategy ids; and for declarations that do not exactly cover them or that
    disagree with the snapshot's exact strategy pins.
    """
    if not isinstance(snapshot, PR.PortfolioRuntimeSnapshot):
        raise PortfolioAllocationPolicyError("explicit_portfolio_snapshot_required")
    if not PR.verify_snapshot_fingerprint(snapshot):
        raise PortfolioAllocationPolicyError("portfolio_snapshot_fingerprint_mismatch")

    eligible = tuple(sorted(str(item) for item in snapshot.execution_participant_ids))

    validated_weights: dict[str, float] = {}
    for key, value in dict(weights or {}).items():
        weight = _finite_number(value, what="weight")
        # A negative weight is a malformed declaration, not a small allocation:
        # it would distort the pool share of every other strategy.
        if weight < 0.0:
            raise PortfolioAllocationPolicyError("canonical_weight_invalid")
        validated_weights[str(key)] = weight
    # Exact coverage both ways: no silent fallback to a dynamic factor and no
    # stray strategy that is not an eligible execution participant.
    if set(validated_weights) - set(eligible):
        raise PortfolioAllocationPolicyError(
            "canonical_allocation_weight_set_exceeds_eligible")
    if set(eligible) - set(validated_weights):
        raise PortfolioAllocationPolicyError(
            "canonical_allocation_weight_set_incomplete")

    declared = tuple(declarations or ())
    by_account: dict[str, StrategyResourceDeclaration] = {}
    for declaration in declared:
        if not isinstance(declaration, StrategyResourceDeclaration):
            raise PortfolioAllocationPolicyError("canonical_declaration_required")
        if declaration.account_id in by_account:
            raise PortfolioAllocationPolicyError("canonical_declaration_duplicate_account")
        by_account[declaration.account_id] = declaration
    if set(by_account) - set(eligible):
        raise PortfolioAllocationPolicyError(
            "canonical_allocation_declaration_set_exceeds_eligible")
    if set(eligible) - set(by_account):
        raise PortfolioAllocationPolicyError(
            "canonical_allocation_declaration_set_incomplete")
    pins_by_account = {str(pin.get("account_id")): pin for pin in snapshot.strategy_pins}
    for account in eligible:
        pin = pins_by_account.get(account)
        item = by_account[account]
        # The snapshot pin is authoritative; a current registry head may not
        # replace it.
        if pin is None or (str(pin.get("strategy_id")) != item.strategy_id
                           or int(pin.get("strategy_version")) != int(item.strategy_version)
                           or str(pin.get("strategy_checksum")) != item.strategy_checksum):
            raise PortfolioAllocationPolicyError(
                "canonical_allocation_declaration_pin_mismatch")

    explicit_intents = tuple(intents or ())
    for intent in explicit_intents:
        if not isinstance(intent, ResourceIntent):
            raise PortfolioAllocationPolicyError("canonical_intent_required")

    # Pool and slot bounds are owner-declared resource facts. A negative bound is
    # not a smaller budget: it would emit a PLANNED plan with negative slots, so
    # it fails closed instead of reaching the arithmetic.
    bounds = {}
    for name, value in (("hard_pool_cap", hard_pool_cap),
                        ("strategy_max_positions", strategy_max_positions),
                        ("strategy_min_positions", strategy_min_positions),
                        ("protected_slot_floor", protected_slot_floor)):
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise PortfolioAllocationPolicyError(f"canonical_{name}_invalid") from exc
        if number < 0:
            raise PortfolioAllocationPolicyError(f"canonical_{name}_invalid")
        bounds[name] = number
    # Fingerprint material must be input-order independent: declarations are
    # ordered by account and intents by their explicit id.
    declared = tuple(sorted(declared, key=lambda item: item.account_id))
    explicit_intents = tuple(sorted(explicit_intents, key=lambda item: item.intent_id))

    slot_plan = _slot_plan(
        snapshot, declared, validated_weights, hard_pool_cap=bounds["hard_pool_cap"],
        strategy_max_positions=bounds["strategy_max_positions"],
        strategy_min_positions=bounds["strategy_min_positions"],
        protected_slot_floor=bounds["protected_slot_floor"], account_order=account_order,
        baseline_exposure=baseline_exposure)
    capital_plan = _capital_plan(
        snapshot, declared, validated_weights,
        shared_pool_max_exposure=shared_pool_max_exposure,
        strategy_pool_floor_ratio=strategy_pool_floor_ratio)
    capacity_plan = _capacity_plan(snapshot)
    conflict_plan = _conflict_plan(snapshot, explicit_intents)
    concentration = _concentration_adjustment(snapshot)
    correlation = _correlation_term(snapshot)

    components = {"slot_plan": slot_plan, "capital_plan": capital_plan,
                  "conflict_plan": conflict_plan, "capacity_plan": capacity_plan}
    status = _plan_status(eligible, components)
    reasons = sorted({str(reason) for reason in
                      (list(slot_plan["blocking_reasons"])
                       + list(capital_plan["blocking_reasons"])
                       + list(capacity_plan["blocking_reasons"])
                       + list(concentration["blocking_reasons"])
                       + list(correlation["blocking_reasons"])
                       + [row["denial_reason"] for row in conflict_plan["ordered_intents"]
                          if row["denial_reason"]])})

    identities = {
        "portfolio_snapshot": f"portfolio_runtime:{snapshot.snapshot_id}",
        "strategy_declarations": "cycle_pinned_strategy_resource_declarations:v1",
        "allocation_weights": "explicit_canonical_allocation_weights:v1",
        "resource_intents": "explicit_resource_intents:v1",
        "allocation_arithmetic": f"paper_allocation:{PA.ALLOCATION_ENGINE_VERSION}",
    }
    identities.update({str(k): str(v) for k, v in dict(source_identities or {}).items()})
    fingerprints = {
        "portfolio_snapshot": snapshot.snapshot_fingerprint,
        "strategy_declarations": _sha([item.projection() for item in declared]),
        "allocation_weights": _sha(validated_weights),
        "resource_intents": _sha([intent.projection() for intent in explicit_intents]),
    }
    for key, value in dict(source_fingerprints or {}).items():
        fingerprint = str(value)
        if not _SHA256.fullmatch(fingerprint):
            raise PortfolioAllocationPolicyError("canonical_source_fingerprint_invalid")
        fingerprints[str(key)] = fingerprint

    material = {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "allocation_policy_version": POLICY_VERSION,
        "portfolio_snapshot_id": snapshot.snapshot_id,
        "portfolio_snapshot_fingerprint": snapshot.snapshot_fingerprint,
        "cycle_id": int(snapshot.cycle_id),
        "asof_day": str(snapshot.asof_day),
        "decision_at": str(snapshot.decision_at),
        "strategy_pins": [_thaw(pin) for pin in snapshot.strategy_pins],
        "eligible_resource_strategy_ids": list(eligible),
        "strategy_resource_declarations": [item.projection() for item in declared],
        "allocation_weights": validated_weights,
        "plan_status": status,
        "slot_plan": slot_plan,
        "capital_plan": capital_plan,
        "conflict_plan": conflict_plan,
        "capacity_plan": capacity_plan,
        "concentration_adjustment": concentration,
        "correlation_term": correlation,
        "blocking_reasons": reasons,
        "source_identities": identities,
        "source_fingerprints": fingerprints,
    }
    fingerprint = _sha(material)
    return PortfolioAllocationPlan(
        plan_id=fingerprint, plan_fingerprint=fingerprint,
        portfolio_snapshot_id=snapshot.snapshot_id,
        portfolio_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        allocation_policy_version=POLICY_VERSION, cycle_id=int(snapshot.cycle_id),
        asof_day=str(snapshot.asof_day), decision_at=str(snapshot.decision_at),
        strategy_pins=tuple(_freeze(pin) for pin in snapshot.strategy_pins),
        eligible_resource_strategy_ids=eligible,
        strategy_resource_declarations=tuple(_freeze(item.projection()) for item in declared),
        allocation_weights=_freeze(validated_weights),
        slot_plan=_freeze(slot_plan), capital_plan=_freeze(capital_plan),
        conflict_plan=_freeze(conflict_plan), capacity_plan=_freeze(capacity_plan),
        concentration_adjustment=_freeze(concentration),
        correlation_term=_freeze(correlation), plan_status=status,
        blocking_reasons=tuple(reasons), source_identities=_freeze(identities),
        source_fingerprints=_freeze(fingerprints))


__all__ = [
    "POLICY_VERSION", "PLAN_SCHEMA_VERSION", "PLANNED", "PARTIAL",
    "INSUFFICIENT_EVIDENCE", "NO_ELIGIBLE_STRATEGIES", "UNAVAILABLE",
    "COMPONENT_STATUSES", "PLAN_STATUSES", "INTENT_KINDS", "INTENT_PRIORITY",
    "EXIT_INTENT_KINDS", "ENTRY_INTENT_KINDS", "PortfolioAllocationPolicyError",
    "StrategyResourceDeclaration", "ResourceIntent", "PortfolioAllocationPlan",
    "build_portfolio_allocation_plan", "verify_plan_fingerprint",
    "plan_from_projection",
]

