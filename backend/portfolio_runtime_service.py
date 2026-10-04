# -*- coding: utf-8 -*-
"""Explicit-cycle application service for portfolio runtime fact snapshots."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping

import paper_cycle_ownership as PCY
import paper_capital_reservations as PCR
import paper_portfolio_read_model as PPRM
import paper_risk_exit_eligibility as PRE
import market_data_contract as MDC
import portfolio_order_intents as POI
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


def _market_valuation_dimension(*, owners, positions, asof_day, reading,
                                expected_identity=None):
    """Compose exact, R24-owned quote rows with bounded position quantities.

    This function never substitutes acquisition cost. A missing/duplicate/invalid
    quote leaves that account's market value unknown and the whole dimension
    partial, so capital planning remains gated.
    """
    snapshot = getattr(reading, "snapshot", None)
    if (not isinstance(reading, MDC.MarketDataReading) or snapshot is None
            or snapshot.kind != "full_market_snapshot"
            or snapshot.as_of != str(asof_day)
            or reading.status != MDC.STATUS_FRESH
            or snapshot.verification != MDC.VERIFICATION_VERIFIED
            or snapshot.verification_method not in {
                MDC.VERIFICATION_METHOD_CROSS_SOURCE,
                MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
            }
            or not str(snapshot.source or "").strip()):
        return _unavailable(
            "strategy_exposure", "exact_market_quote_evidence_unavailable",
            "market_data_service:full_market_snapshot",
        )
    try:
        identity = MDC.snapshot_fingerprint(snapshot)
    except (TypeError, ValueError):
        return _unavailable(
            "strategy_exposure", "exact_market_quote_identity_unavailable",
            "market_data_service:full_market_snapshot",
        )
    if expected_identity is not None and str(expected_identity) != identity:
        raise PR.PortfolioRuntimeError("market_evidence_identity_mismatch")

    by_code = {}
    for row in snapshot.rows:
        if isinstance(row, Mapping) and row.get("code"):
            by_code.setdefault(str(row["code"]), []).append(row)
    values = {str(owner): 0.0 for owner in owners}
    priced = []
    missing = {str(owner): set() for owner in owners}
    for position in positions or ():
        account = str(position.get("account_id") or "")
        code = str(position.get("code") or "")
        if account not in values or not code:
            continue
        try:
            quantity = float(position.get("qty"))
        except (TypeError, ValueError):
            missing[account].add(code or "<unknown>")
            continue
        rows = by_code.get(code, ())
        row = rows[0] if len(rows) == 1 else None
        quote_day = MDC.canonical_day(
            (row or {}).get("quote_at") or snapshot.observed_at)
        source = str((row or {}).get("source") or snapshot.source or "").strip()
        try:
            price = float((row or {}).get("price"))
        except (TypeError, ValueError):
            price = float("nan")
        if (quantity < 0 or not math.isfinite(quantity) or not math.isfinite(price)
                or price <= 0 or quote_day != str(asof_day) or not source):
            missing[account].add(code)
            continue
        amount = quantity * price
        if not math.isfinite(amount):
            missing[account].add(code)
            continue
        values[account] += amount
        priced.append({"account_id": account, "symbol": code,
                       "quantity": quantity, "price": price,
                       "market_value": amount, "quote_day": quote_day,
                       "quote_source": source})
    market_by_account = {
        account: (round(amount, 2) if not missing[account] else None)
        for account, amount in sorted(values.items())
    }
    facts = {
        "market_value_by_account": market_by_account,
        "market_value_by_position": sorted(
            priced, key=lambda row: (row["account_id"], row["symbol"])),
        "missing_quote_symbols_by_account": {
            account: sorted(codes) for account, codes in sorted(missing.items())
            if codes
        },
        "market_evidence_identity": identity,
        "market_asof_day": str(snapshot.as_of),
        "market_observed_at": str(snapshot.observed_at or ""),
        "market_source": str(snapshot.source),
        "market_verification": snapshot.verification,
        "market_verification_method": snapshot.verification_method,
    }
    incomplete = any(missing.values())
    return _dimension(
        "strategy_exposure", PR.PARTIAL if incomplete else PR.AVAILABLE,
        facts, f"market_data_service:full_market_snapshot:{identity}",
        reasons=(("exact_position_quote_missing_or_invalid",) if incomplete else ()),
    )


def _reservation_order_identity(reservations, pending_orders):
    """Validate reservation-to-order identity without coupling either owner."""
    if reservations is None:
        return None
    order_rows = (pending_orders or {}).get("order_identities", ())
    by_id = {str(item.get("order_id")): item for item in order_rows}
    mismatches = []
    for row in reservations.get("reservations", ()):
        order = by_id.get(str(row.get("order_id")))
        if order is None or any(
            str(order.get(left) or "") != str(row.get(right) or "")
            for left, right in (("cycle_id", "cycle_id"),
                                ("account_id", "account_id"),
                                ("symbol", "symbol"), ("side", "side"))
        ):
            mismatches.append({
                "reservation_id": row.get("reservation_id"),
                "order_id": row.get("order_id"),
                "reason": "reservation_order_identity_mismatch",
            })
    result = dict(reservations)
    result["order_identity_validation"] = {
        "status": "AVAILABLE" if not mismatches else "UNAVAILABLE",
        "source_identity": (pending_orders or {}).get("source_identity"),
        "mismatches": mismatches,
    }
    if mismatches:
        result["status"] = "UNAVAILABLE"
        result["unknown_reservations"] = [
            *result.get("unknown_reservations", ()), *mismatches]
    return result


def _pending_reservations_by_symbol(reservations):
    """Project only verified formal reservation amounts into symbol groups."""
    if not isinstance(reservations, Mapping) or reservations.get("status") != "AVAILABLE":
        return None
    totals = defaultdict(float)
    try:
        for row in reservations["reservations"]:
            symbol = str(row["symbol"])
            amount = float(row["amount"])
            fees = float(row["fees"])
            if not symbol or not math.isfinite(amount) or not math.isfinite(fees):
                return None
            totals[symbol] += amount + fees
    except (KeyError, TypeError, ValueError):
        return None
    return {symbol: round(amount, 2) for symbol, amount in sorted(totals.items())}


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
             market_evidence_identity: str | None, market_reading=None,
             builtin_scope):
    expected_market_evidence_identity = market_evidence_identity
    # A caller-provided fingerprint is only an equality constraint. It is not
    # evidence by itself and must not be copied into the captured snapshot.
    market_evidence_identity = None
    try:
        ownership = PCY.exact_cycle_owner_snapshot(
            conn, cycle_id, asof_day=asof_day,
            attachment_prover=PPRM.account_attached_by_asof,
            builtin_scope=builtin_scope)
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
    market_positions = []
    lots_complete = True
    positions_complete = True
    for account_id in owners:
        accounting.extend(_fact_projection(fact) for fact in
                          PPRM.accounting_fact_projections(conn, context,
                                                          account_id=account_id))
        lots, lot_status = PPRM.bounded_lots_with_status(
            conn, context, account_id=account_id)
        lots_complete = lots_complete and lot_status == PPRM.STATUS_VERIFIED
        exact_positions, quantity_status = PPRM.positions_for_context_with_status(
            conn, context, account_id=account_id)
        positions_complete = positions_complete and quantity_status == PPRM.STATUS_VERIFIED
        market_positions.extend(exact_positions)
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
    try:
        pending_intents = POI.pending_resource_intents(
            conn, cycle_id=cycle_id, asof_day=asof_day,
            decision_at=decision_at)
    except POI.PendingIntentEvidenceUnavailable:
        pending_intents = None
    try:
        reservations = PCR.pending_reservation_evidence(
            conn, asof_day=asof_day, decision_at=decision_at)
    except PCR.CapitalReservationEvidenceUnavailable:
        reservations = None
    reservations = _reservation_order_identity(reservations, pending_intents)
    capital_status = PR.PARTIAL if unknown else PR.AVAILABLE
    if reservations is None or reservations["status"] != "AVAILABLE":
        capital_status = PR.PARTIAL
    capital = _dimension(
        "capital", capital_status,
        {"cycle_capital": ownership["cycle_capital"], "accounting_facts": accounting,
         "cash_is_asof_reconstructed": True,
         "mutable_current_account_cash_used": False,
         "pending_reservations": reservations},
        f"paper_portfolio_read_model:cycle:{cycle_id}:asof:{asof_day}",
        reasons=tuple(reason for reason, present in (
            ("bounded_accounting_facts_incomplete", bool(unknown)),
            ("pending_reservation_evidence_unavailable",
             reservations is None or reservations["status"] != "AVAILABLE"),
        ) if present))
    costs = [item for item in accounting if item["fact_kind"] ==
             PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY]
    if market_reading is not None and positions_complete:
        exposure = _market_valuation_dimension(
            owners=owners, positions=market_positions, asof_day=asof_day,
            reading=market_reading,
            expected_identity=expected_market_evidence_identity)
        if exposure.status == PR.UNAVAILABLE:
            market_evidence_identity = None
        elif exposure.source_identity:
            market_evidence_identity = str(
                exposure.facts.get("market_evidence_identity") or "")
    else:
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
    if not positions_complete and exposure.status == PR.AVAILABLE:
        exposure = _dimension(
            "strategy_exposure", PR.PARTIAL, exposure.facts,
            exposure.source_identity,
            reasons=("bounded_position_quantity_evidence_incomplete",),
        )
    nav_by_account = {}
    portfolio_nav = None
    nav_complete = False
    nav_source = None
    total_portfolio = None
    if (market_reading is not None and exposure.status == PR.AVAILABLE
            and positions_complete):
        prices = {
            str(item["symbol"]): float(item["price"])
            for item in exposure.facts["market_value_by_position"]
        }
        nav_complete = True
        try:
            total_portfolio = PPRM.portfolio_for_context(
                conn, context, valuations=prices)
        except PPRM.PortfolioReadUnavailable:
            total_portfolio = {"nav": None, "nav_status": PPRM.STATUS_UNKNOWN}
        portfolio_nav = (total_portfolio["nav"]
                         if total_portfolio["nav_status"] == PPRM.STATUS_VERIFIED
                         else None)
        nav_complete = portfolio_nav is not None
        for account_id in owners:
            try:
                portfolio = PPRM.portfolio_for_context(
                    conn, context, account_id=account_id, valuations=prices)
            except PPRM.PortfolioReadUnavailable:
                portfolio = {"cash": None, "cash_status": PPRM.STATUS_UNKNOWN,
                             "market_value": None,
                             "market_value_status": PPRM.STATUS_UNKNOWN,
                             "nav": None, "nav_status": PPRM.STATUS_UNKNOWN}
            nav_by_account[str(account_id)] = {
                "cash": portfolio["cash"],
                "cash_status": portfolio["cash_status"],
                "market_value": portfolio["market_value"],
                "market_value_status": portfolio["market_value_status"],
                "nav": portfolio["nav"], "nav_status": portfolio["nav_status"],
            }
            nav_complete = nav_complete and (
                portfolio["nav_status"] == PPRM.STATUS_VERIFIED)
        nav_source = (
            f"paper_portfolio_read_model:nav:cycle:{cycle_id}:asof:{asof_day}:"
            f"market:{exposure.facts['market_evidence_identity']}"
        )
    else:
        nav_by_account = {str(account): {"cash": None,
                                        "cash_status": PPRM.STATUS_UNKNOWN,
                                        "market_value": None,
                                        "market_value_status": PPRM.STATUS_UNKNOWN,
                                        "nav": None,
                                        "nav_status": PPRM.STATUS_UNKNOWN}
                          for account in owners}
    capital_facts = PR._thaw(capital.facts)
    capital_facts["portfolio_valuation_by_account"] = nav_by_account
    capital_facts["nav"] = portfolio_nav
    capital_facts["nav_uses_portfolio_read_model_composer"] = True
    capital_reasons = list(capital.blocking_reasons)
    if not nav_complete:
        capital_reasons.append("exact_cycle_asof_nav_unavailable")
    capital = _dimension(
        "capital", PR.AVAILABLE if not capital_reasons else PR.PARTIAL,
        capital_facts,
        nav_source or f"paper_portfolio_read_model:cycle:{cycle_id}:asof:{asof_day}",
        reasons=capital_reasons,
    )
    pending_by_symbol = _pending_reservations_by_symbol(reservations)
    if (pending_by_symbol is not None
            and total_portfolio is not None
            and total_portfolio.get("nav_status") == PPRM.STATUS_VERIFIED
            and total_portfolio.get("cash_status") == PPRM.STATUS_VERIFIED
            and total_portfolio.get("market_value_status") == PPRM.STATUS_VERIFIED
            and exposure.status == PR.AVAILABLE
            and reservations is not None
            and reservations.get("status") == "AVAILABLE"):
        capacity_facts = {
            "used": round(float(total_portfolio["market_value"]), 2),
            "pending": float(reservations["pending_total"]),
            "pending_total": float(reservations["pending_total"]),
            "headroom": round(float(total_portfolio["cash"])
                              - float(reservations["pending_total"]), 2),
            "used_by_account": dict(exposure.facts["market_value_by_account"]),
            # A verified empty reservation query means zero for each eligible
            # account. Existing obligations from paused/non-execution owners
            # still count in the shared pending_total above.
            "pending_by_account": {
                str(account): float(reservations["pending_by_account"].get(account, 0.0))
                for account in execution
            },
            "pending_by_symbol": pending_by_symbol,
            "nav": round(float(total_portfolio["nav"]), 2),
            "basis": "market_value_plus_reserved_buy_amount_against_read_model_cash",
            "reservation_order_identity_validation":
                reservations["order_identity_validation"],
        }
        capacity = _dimension(
            "capacity", PR.AVAILABLE, capacity_facts,
            "paper_portfolio_read_model:capacity:"
            f"cycle:{cycle_id}:asof:{asof_day}:"
            f"market:{exposure.facts['market_evidence_identity']}:"
            f"reservations:{reservations['source_fingerprint']}",
        )
    else:
        capacity = _unavailable(
            "capacity", "exact_cycle_asof_pending_capacity_unavailable",
            f"paper_portfolio_read_model:capacity:cycle:{cycle_id}:asof:{asof_day}",
        )
    if pending_intents is None:
        conflicts = _unavailable(
            "signal_conflicts", "pending_order_intent_evidence_unavailable",
            f"paper_orders:pending_resource_intents:cycle:{cycle_id}:asof:{asof_day}")
    else:
        if pending_intents["status"] == "AVAILABLE":
            conflicts = _dimension(
                "signal_conflicts", PR.AVAILABLE,
                {"pending_resource_intents": pending_intents["intents"],
                 "unknown_orders": [],
                 "cycle_id": cycle_id, "asof_day": asof_day,
                 "decision_at": decision_at},
                pending_intents["source_identity"])
        else:
            conflicts = _dimension(
                "signal_conflicts", PR.PARTIAL,
                {"pending_resource_intents": pending_intents["intents"],
                 "unknown_orders": pending_intents["unknown_orders"],
                 "cycle_id": cycle_id, "asof_day": asof_day,
                 "decision_at": decision_at},
                pending_intents["source_identity"],
                reasons=("pending_order_intent_evidence_incomplete",))
    dimensions = (
        capital, exposure,
        _unavailable("concentration", "exact_market_valued_classification_unavailable"),
        _unavailable("turnover", "explicit_verified_turnover_window_unavailable"),
        _unavailable("risk_consumption", "exact_asof_risk_consumption_owner_unavailable"),
        conflicts,
        capacity,
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
                           **({"market_evidence":
                              f"market_data_service:full_market_snapshot:{market_evidence_identity}"}
                              if market_evidence_identity else {}),
                           "risk_exit": "paper_risk_exit_eligibility:cycle_scoped"},
        market_evidence_identity=market_evidence_identity,
        dimensions=dimensions)


def capture_portfolio_runtime_snapshot(conn, *, cycle_id, asof_day: str,
                                       decision_at: str,
                                       builtin_scope,
                                       market_evidence_identity: str | None = None,
                                       market_reading=None):
    try:
        cycle = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise PR.PortfolioRuntimeError("explicit_cycle_id_required") from exc
    snapshot = PRRepo.append_snapshot(conn, _capture(
        conn, cycle_id=cycle, asof_day=asof_day, decision_at=decision_at,
        market_evidence_identity=market_evidence_identity,
        market_reading=market_reading, builtin_scope=builtin_scope))
    return snapshot.projection()


def get_portfolio_runtime_snapshot(conn, snapshot_id: str):
    snapshot = PRRepo.get_snapshot(conn, snapshot_id)
    if snapshot is None:
        raise PR.PortfolioRuntimeError("portfolio_snapshot_not_found")
    return snapshot.projection()


__all__ = ["capture_portfolio_runtime_snapshot", "get_portfolio_runtime_snapshot"]
