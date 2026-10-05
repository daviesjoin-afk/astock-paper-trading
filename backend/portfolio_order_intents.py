# -*- coding: utf-8 -*-
"""Strict read owner for typed pending paper-order resource intents.

This module reads the formal order ledger only. It never classifies prose,
turns unknown history into an empty set, or grants Risk permission.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

import market_data_contract as MDC
import portfolio_allocation_policy as PAP
import portfolio_allocation_repository as PAPRepo

__all__ = ["PendingIntentEvidenceUnavailable", "pending_resource_intents"]

_PENDING_STATUSES = (
    "pending_limit", "pending_execution", "partially_filled", "pending",
    "recheck_capacity", "execution_retry",
    "manual_execution_retry", "awaiting_batch", "pending_verification",
    "unfilled_limit_down",
)
_BUY_INTENTS = frozenset({"NEW_ENTRY", "ADD_POSITION"})
_SELL_INTENTS = frozenset({"RISK_EXIT", "TAKE_PROFIT_EXIT", "MANUAL_EXIT",
                           "RISK_REDUCE"})


class PendingIntentEvidenceUnavailable(RuntimeError):
    """The persisted pending-order set cannot be proven complete and exact."""


def _instant(value, *, reason):
    parsed = MDC._parse_instant(value)
    if parsed is None:
        raise PendingIntentEvidenceUnavailable(reason)
    return parsed.astimezone(dt.timezone.utc)


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unknown(row, reason):
    return {"order_id": int(row["id"]), "account_id": row.get("account_id"),
            "symbol": str(row.get("code") or ""), "cycle_id": row.get("cycle_id"),
            "side": str(row.get("side") or "").lower(),
            "reason": str(reason)}


def _verified_plan_for_order(conn, row, *, asof_day):
    required = ("portfolio_snapshot_id", "allocation_plan_id",
                "allocation_plan_fingerprint", "allocation_policy_version")
    if any(not str(row.get(key) or "").strip() for key in required):
        return None, "pending_order_allocation_provenance_unavailable"
    try:
        plan = PAPRepo.get_plan(conn, str(row["allocation_plan_id"]))
    except (PAPRepo.PortfolioAllocationRepositoryError, sqlite3.Error):
        return None, "pending_order_allocation_plan_unavailable"
    if plan is None or not PAP.verify_plan_fingerprint(plan):
        return None, "pending_order_allocation_plan_invalid"
    if (plan.plan_id != str(row["allocation_plan_id"])
            or plan.plan_fingerprint != str(row["allocation_plan_fingerprint"])
            or plan.portfolio_snapshot_id != str(row["portfolio_snapshot_id"])
            or plan.allocation_policy_version != str(row["allocation_policy_version"])
            or int(plan.cycle_id) != int(row.get("cycle_id") or 0)
            or str(plan.asof_day) != str(asof_day)
            or str(row.get("account_id") or "")
            not in set(plan.eligible_resource_strategy_ids)):
        return None, "pending_order_allocation_identity_mismatch"
    pin = next((item for item in plan.strategy_pins
                if str(item.get("account_id")) == str(row.get("account_id"))), None)
    if pin is None or any(
            str(pin.get(source) or "") != str(row.get(target) or "")
            for source, target in (("strategy_id", "strategy_id"),
                                   ("strategy_checksum", "strategy_checksum"))):
        return None, "pending_order_strategy_pin_mismatch"
    try:
        if int(pin.get("strategy_version")) != int(row.get("strategy_version")):
            return None, "pending_order_strategy_pin_mismatch"
    except (TypeError, ValueError):
        return None, "pending_order_strategy_pin_mismatch"
    return plan, None


def pending_resource_intents(conn, *, cycle_id, asof_day, decision_at):
    """Read typed pending orders and make untyped/invalid rows explicit unknowns.

    The database query is deliberately strict: schema or SQLite errors raise a
    controlled evidence failure instead of returning an empty conflict set.
    Open orders from other cycles retain their own exact cycle identity because
    the legacy shared portfolio reservation owner counts them economically.
    """
    try:
        cycle = int(cycle_id)
        day = dt.date.fromisoformat(str(asof_day)).isoformat()
    except (TypeError, ValueError) as exc:
        raise PendingIntentEvidenceUnavailable(
            "explicit_cycle_and_asof_required") from exc
    if cycle <= 0:
        raise PendingIntentEvidenceUnavailable("explicit_cycle_and_asof_required")
    decision = _instant(decision_at, reason="explicit_decision_at_required")
    placeholders = ",".join("?" for _ in _PENDING_STATUSES)
    columns = (
        "id,account_id,side,code,status,created_at,cycle_id,strategy_id,"
        "strategy_version,strategy_checksum,allocation_intent_kind,"
        "portfolio_snapshot_id,allocation_plan_id,allocation_plan_fingerprint,"
        "allocation_policy_version"
    )
    try:
        rows = conn.execute(
            f"SELECT {columns} FROM paper_orders WHERE status IN ({placeholders}) "
            "ORDER BY id", _PENDING_STATUSES,
        ).fetchall()
    except (sqlite3.Error, AttributeError) as exc:
        raise PendingIntentEvidenceUnavailable(
            "pending_order_query_unavailable") from exc

    intents = []
    unknown = []
    evidence_rows = []
    order_identities = []
    for raw in rows:
        row = dict(raw) if hasattr(raw, "keys") else dict(zip(
            [value.strip() for value in columns.split(",")], raw, strict=True))
        code = str(row.get("code") or "")
        created = MDC._parse_instant(row.get("created_at"))
        if created is None:
            unknown.append(_unknown(row, "pending_order_created_at_unavailable"))
            continue
        created = created.astimezone(dt.timezone.utc)
        created_day = MDC.canonical_day(row.get("created_at"))
        if created > decision or created_day is None:
            unknown.append(_unknown(row, "pending_order_created_at_unavailable"))
            continue
        if created_day > day:
            continue
        cycle_value = row.get("cycle_id")
        if not code or not str(row.get("account_id") or ""):
            unknown.append(_unknown(row, "pending_order_identity_unavailable"))
            continue
        try:
            order_cycle = int(cycle_value)
        except (TypeError, ValueError):
            unknown.append(_unknown(row, "pending_order_cycle_unavailable"))
            continue
        side = str(row.get("side") or "").lower()
        order_identities.append({
            "order_id": int(row["id"]), "cycle_id": order_cycle,
            "account_id": str(row["account_id"]), "symbol": code,
            "side": side, "intent_kind": str(row.get("allocation_intent_kind") or "") or None,
            "source_identity": f"paper_orders:{int(row['id'])}:cycle:{order_cycle}",
        })
        kind = str(row.get("allocation_intent_kind") or "")
        allowed = _BUY_INTENTS if side == "buy" else _SELL_INTENTS if side == "sell" else ()
        if kind not in allowed:
            unknown.append(_unknown(row, "legacy_or_invalid_pending_intent_kind"))
            continue
        plan, reason = _verified_plan_for_order(conn, row, asof_day=day)
        if reason:
            unknown.append(_unknown(row, reason))
            continue
        source = (f"paper_orders:{int(row['id'])}:cycle:{order_cycle}:"
                  f"allocation_plan:{plan.plan_id}")
        try:
            intent = PAP.ResourceIntent(
                intent_id=f"paper_order:{int(row['id'])}",
                account_id=str(row["account_id"]), intent_kind=kind,
                symbol=code, source_identity=source,
            )
        except PAP.PortfolioAllocationPolicyError:
            unknown.append(_unknown(row, "pending_order_typed_intent_invalid"))
            continue
        intents.append(intent.projection())
        evidence_rows.append({"order_id": int(row["id"]),
                              "cycle_id": order_cycle,
                              "source_identity": source,
                              "allocation_plan_fingerprint": plan.plan_fingerprint})

    intents.sort(key=lambda item: item["intent_id"])
    unknown.sort(key=lambda item: (str(item.get("symbol") or ""), item["order_id"]))
    material = {"cycle_id": cycle, "asof_day": day,
                "decision_at": str(decision_at), "intents": intents,
                "unknown_orders": unknown, "orders": evidence_rows}
    status = "AVAILABLE" if not unknown else "UNAVAILABLE"
    return {
        "status": status,
        "intents": intents,
        "unknown_orders": unknown,
        "order_identities": order_identities,
        "source_identity": f"paper_orders:pending_resource_intents:cycle:{cycle}:asof:{day}",
        "source_fingerprint": _fingerprint(material),
        "cycle_id": cycle,
        "asof_day": day,
        "decision_at": str(decision_at),
    }
