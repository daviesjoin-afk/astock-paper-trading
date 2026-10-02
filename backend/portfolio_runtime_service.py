# -*- coding: utf-8 -*-
"""Explicit-cycle application service for portfolio runtime fact snapshots."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math

import paper_cycle_ownership as PCY
import paper_portfolio_read_model as PPRM
import paper_risk_exit_eligibility as PRE
import paper_schema_migrations as PSM
import paper_trading as PT
import portfolio_runtime as PR
import portfolio_runtime_repository as PRRepo
import strategy_lifecycle as SL
import strategy_registry as SR


def _sha_json(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fact_projection(fact):
    value = fact.value
    if hasattr(value, "as_dict"):
        value = dict(value.as_dict())
    return {"fact_kind": fact.fact_kind, "cycle_id": fact.cycle_id,
            "account_id": fact.account_id, "asof_day": fact.asof_day,
            "status": fact.status, "value": value, "version": fact.version}


def _dimension(name, status, facts, source, *, reasons=(), provenance="OWNER_ISSUED"):
    return PR.PortfolioDimension(name=name, status=status, facts=facts,
                                  provenance=provenance, source_identity=source,
                                  source_fingerprint=_sha_json(facts),
                                  blocking_reasons=tuple(reasons))


def _unavailable(name, reason, source=None):
    return PR.PortfolioDimension(name=name, status=PR.UNAVAILABLE, facts={},
                                 provenance="UNAVAILABLE", source_identity=source,
                                 blocking_reasons=(reason,))


def _lifecycle_at(conn, strategy_id, version, checksum, decision_at):
    """Select the exact lifecycle owner event visible at the supplied instant."""
    try:
        instant = dt.datetime.fromisoformat(str(decision_at))
        if instant.tzinfo is None:
            raise ValueError
        instant = instant.astimezone(dt.timezone.utc)
    except (TypeError, ValueError) as exc:
        raise PR.PortfolioRuntimeError("explicit_decision_at_must_include_timezone") from exc
    events = SL.history(conn, strategy_id, version)
    eligible = []
    for event in events:
        if str(event.get("strategy_checksum") or "") != str(checksum):
            continue
        try:
            created = dt.datetime.fromisoformat(str(event.get("created_at")))
            if created.tzinfo is None:
                raise ValueError
            created = created.astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if created <= instant:
            eligible.append((created, int(event.get("id") or 0), event))
    if not eligible:
        raise PR.PortfolioRuntimeError("exact_strategy_lifecycle_state_unavailable")
    _at, _event_id, event = max(eligible, key=lambda row: (row[0], row[1]))
    return str(event["to_state"]), str(event["created_at"]), event


def _capture(conn, *, cycle_id: int, asof_day: str, decision_at: str,
             market_evidence_identity: str | None):
    try:
        ownership = PCY.exact_cycle_owner_snapshot(
            conn, cycle_id, builtin_scope=PT.ACTIVE_ACCOUNT_IDS)
    except ValueError as exc:
        raise PR.PortfolioRuntimeError(str(exc)) from exc
    owners = ownership["economic_owner_ids"]

    pins = []
    lifecycle_by_account = {}
    for account_id in owners:
        stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=cycle_id)
        if stamp is None:
            raise PR.PortfolioRuntimeError("exact_cycle_strategy_pin_unavailable")
        strategy_id, version, checksum = stamp
        try:
            version_row = SR.get_version(strategy_id, version, checksum=checksum, conn=conn)
        except ValueError as exc:
            raise PR.PortfolioRuntimeError("exact_cycle_strategy_pin_mismatch") from exc
        if version_row is None:
            raise PR.PortfolioRuntimeError("exact_cycle_strategy_pin_unavailable")
        lifecycle, lifecycle_at, lifecycle_event = _lifecycle_at(
            conn, strategy_id, version, checksum, decision_at)
        lifecycle_by_account[account_id] = lifecycle
        pins.append({"account_id": account_id, "strategy_id": str(strategy_id),
                     "strategy_version": int(version), "strategy_checksum": str(checksum),
                     "lifecycle_state": lifecycle,
                     "lifecycle_observed_at": lifecycle_at,
                     "lifecycle_event_fingerprint": lifecycle_event.get("event_fingerprint")})
    execution = tuple(sorted(account for account in owners
                             if SL.allows_formal_cycle(lifecycle_by_account[account])))
    # Build exact-as-of risk-exit lot participants from the existing bounded lot
    # owner; current mutable remaining_qty cannot replay a historical as-of.
    context = PPRM.PortfolioReadContext(cycle_id=cycle_id, asof_day=asof_day)
    lots, lots_status = PPRM.bounded_lots_with_status(conn, context)
    if lots_status != PPRM.STATUS_VERIFIED:
        raise PR.PortfolioRuntimeError("asof_risk_exit_lot_evidence_unavailable")
    lot_owners = {str(lot.get("account_id")) for lot in lots
                  if float(lot.get("remaining_qty") or 0) > 0 and lot.get("account_id")}
    risk_exit = PRE.risk_exit_account_ids(
        conn, execution, cycle_id=cycle_id, asof_account_ids=lot_owners)
    risk_exit = tuple(sorted(str(value) for value in risk_exit))

    accounting = []
    symbol_costs: dict[tuple[str, str], float] = {}
    lots_complete = True
    for account_id in owners:
        accounting.extend(_fact_projection(fact) for fact in
                          PPRM.accounting_fact_projections(conn, context,
                                                          account_id=account_id))
        lots, lot_status = PPRM.bounded_lots_with_status(
            conn, context, account_id=account_id)
        lots_complete = lots_complete and lot_status == PPRM.STATUS_VERIFIED
        for lot in lots:
            try:
                remaining = float(lot.get("remaining_qty") or 0)
                cost = float(lot.get("cost"))
                if (not math.isfinite(remaining) or not math.isfinite(cost)
                        or remaining < 0):
                    raise ValueError
                key = (account_id, str(lot.get("code") or ""))
                symbol_costs[key] = symbol_costs.get(key, 0.0) + remaining * cost
            except (TypeError, ValueError, OverflowError):
                lots_complete = False
    unknown = sum(item["status"] != PPRM.STATUS_VERIFIED for item in accounting)
    capital_status = PR.PARTIAL if unknown else PR.AVAILABLE
    capital = _dimension(
        "capital", capital_status,
        {"cycle_capital": ownership["cycle_capital"], "accounting_facts": accounting,
         "cash_is_asof_reconstructed": True,
         "mutable_current_account_cash_used": False},
        f"paper_portfolio_read_model:cycle:{cycle_id}:asof:{asof_day}",
        reasons=(("bounded_accounting_facts_incomplete",) if unknown else ()))
    costs = [item for item in accounting if item["fact_kind"] ==
             PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY]
    exposure = _dimension(
        "strategy_exposure", PR.PARTIAL,
        {"position_cost_by_account": costs,
         "symbol_cost_basis_by_account": [
             {"account_id": account, "symbol": symbol, "cost_basis": amount}
             for (account, symbol), amount in sorted(symbol_costs.items())],
         "market_value_by_account": None,
         "market_value_requires_exact_market_evidence": True},
        f"paper_portfolio_read_model:cost:cycle:{cycle_id}:asof:{asof_day}",
        reasons=(("exact_market_valuations_not_supplied",) if lots_complete else
                 ("bounded_lot_cost_evidence_incomplete", "exact_market_valuations_not_supplied")))
    dimensions = (
        capital, exposure,
        _unavailable("concentration", "exact_market_valued_classification_unavailable"),
        _unavailable("turnover", "explicit_verified_turnover_window_unavailable"),
        _unavailable("risk_consumption", "exact_asof_risk_consumption_owner_unavailable"),
        _unavailable("signal_conflicts", "exact_asof_strategy_signal_intents_unavailable"),
        _unavailable("capacity", "cycle_asof_pending_capacity_owner_unavailable"),
        _unavailable("correlation", "exact_strategy_version_return_series_unavailable"),
    )
    return PR.build_portfolio_runtime_snapshot(
        cycle_id=cycle_id, asof_day=asof_day, decision_at=decision_at,
        cycle_identity=ownership["cycle_identity"], strategy_pins=pins,
        economic_owner_ids=owners, execution_participant_ids=execution,
        risk_exit_participant_ids=risk_exit,
        source_identities={"cycle_owner": f"paper_cycle_ownership:{cycle_id}",
                           "strategy_pins": f"strategy_registry:cycle:{cycle_id}",
                           "lifecycle": "strategy_lifecycle:exact_pins",
                           "accounting": "paper_portfolio_read_model:v1",
                           "risk_exit": "paper_risk_exit_eligibility:cycle_scoped"},
        market_evidence_identity=market_evidence_identity,
        dimensions=dimensions)


def _with_connection(work, *, initialize=False):
    if initialize:
        with PT._db(immediate=True) as conn:
            PSM.ensure_portfolio_runtime_snapshots(conn)
            snapshot = work(conn)
            conn.commit()
            return snapshot
    with PT._db_readonly() as conn:
        return work(conn)


def capture_portfolio_runtime_snapshot(*, cycle_id, asof_day: str,
                                       decision_at: str,
                                       market_evidence_identity: str | None = None):
    try:
        cycle = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise PR.PortfolioRuntimeError("explicit_cycle_id_required") from exc
    snapshot = _with_connection(
        lambda conn: PRRepo.append_snapshot(conn, _capture(
            conn, cycle_id=cycle, asof_day=asof_day, decision_at=decision_at,
            market_evidence_identity=market_evidence_identity)), initialize=True)
    return snapshot.projection()


def get_portfolio_runtime_snapshot(snapshot_id: str):
    def read(conn):
        snapshot = PRRepo.get_snapshot(conn, snapshot_id)
        if snapshot is None:
            raise PR.PortfolioRuntimeError("portfolio_snapshot_not_found")
        return snapshot.projection()
    return _with_connection(read)


__all__ = ["capture_portfolio_runtime_snapshot", "get_portfolio_runtime_snapshot"]
