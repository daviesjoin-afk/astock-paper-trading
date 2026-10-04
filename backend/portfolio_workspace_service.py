"""Read-only projection for one explicitly named portfolio allocation plan."""
from __future__ import annotations

import sqlite3

import portfolio_allocation_policy as PAP
import portfolio_allocation_repository as PAPRepo
import portfolio_runtime as PR
import portfolio_runtime_repository as PRRepo


class PortfolioWorkspaceUnavailable(ValueError):
    """The requested exact plan/snapshot/order projection is unavailable."""


def get_portfolio_workspace(conn, *, cycle_id, plan_id):
    """Read facts for an explicit cycle and plan; never choose latest/current."""
    try:
        cycle = int(cycle_id)
    except (TypeError, ValueError) as exc:
        raise PortfolioWorkspaceUnavailable("explicit_cycle_id_required") from exc
    if cycle <= 0:
        raise PortfolioWorkspaceUnavailable("explicit_cycle_id_required")
    try:
        plan = PAPRepo.get_plan(conn, str(plan_id or ""))
    except (PAPRepo.PortfolioAllocationRepositoryError, sqlite3.Error) as exc:
        raise PortfolioWorkspaceUnavailable("allocation_plan_evidence_unavailable") from exc
    if plan is None:
        raise PortfolioWorkspaceUnavailable("portfolio_allocation_plan_not_found")
    if int(plan.cycle_id) != cycle:
        raise PortfolioWorkspaceUnavailable("portfolio_workspace_cycle_mismatch")
    try:
        snapshot = PRRepo.get_snapshot(conn, plan.portfolio_snapshot_id)
    except (PRRepo.PortfolioRuntimeRepositoryError, sqlite3.Error) as exc:
        raise PortfolioWorkspaceUnavailable("portfolio_snapshot_evidence_unavailable") from exc
    if snapshot is None:
        raise PortfolioWorkspaceUnavailable("portfolio_snapshot_not_found")
    if (not PR.verify_snapshot_fingerprint(snapshot)
            or snapshot.snapshot_id != plan.portfolio_snapshot_id
            or snapshot.snapshot_fingerprint != plan.portfolio_snapshot_fingerprint
            or int(snapshot.cycle_id) != cycle
            or str(snapshot.asof_day) != str(plan.asof_day)):
        raise PortfolioWorkspaceUnavailable("portfolio_workspace_snapshot_identity_mismatch")

    try:
        rows = conn.execute(
            """SELECT id,cycle_id,account_id,code,side,qty,status,
                      allocation_intent_kind,portfolio_snapshot_id,allocation_plan_id,
                      allocation_plan_fingerprint,allocation_policy_version,
                      strategy_id,strategy_version,strategy_checksum
                 FROM paper_orders WHERE allocation_plan_id=? ORDER BY id""",
            (plan.plan_id,),
        ).fetchall()
    except (sqlite3.Error, AttributeError) as exc:
        raise PortfolioWorkspaceUnavailable("portfolio_workspace_order_query_unavailable") from exc
    orders = []
    unknown = []
    snapshot_projection = snapshot.projection()
    pins_by_account = {
        str(pin.get("account_id") or ""): pin
        for pin in snapshot_projection.get("strategy_pins", ())
    }
    planned_intents = {
        (str(item.get("account_id") or ""), str(item.get("symbol") or ""),
         str(item.get("intent_kind") or ""))
        for item in plan.projection().get("conflict_plan", {}).get("ordered_intents", ())
    }
    for raw in rows:
        row = dict(raw) if hasattr(raw, "keys") else dict(zip(
            ("id", "cycle_id", "account_id", "code", "side", "qty", "status",
             "allocation_intent_kind", "portfolio_snapshot_id", "allocation_plan_id",
             "allocation_plan_fingerprint", "allocation_policy_version",
             "strategy_id", "strategy_version", "strategy_checksum"),
            raw, strict=True))
        try:
            row_cycle_id = int(row.get("cycle_id") or 0)
        except (TypeError, ValueError):
            row_cycle_id = 0
        pin = pins_by_account.get(str(row.get("account_id") or ""))
        intent_kind = str(row.get("allocation_intent_kind") or "")
        expected_side = ("buy" if intent_kind in PAP.ENTRY_INTENT_KINDS
                         else "sell" if intent_kind in PAP.EXIT_INTENT_KINDS else None)
        if (str(row.get("allocation_plan_id") or "") != plan.plan_id
                or str(row.get("allocation_plan_fingerprint") or "") != plan.plan_fingerprint
                or str(row.get("portfolio_snapshot_id") or "") != plan.portfolio_snapshot_id
                or str(row.get("allocation_policy_version") or "")
                != plan.allocation_policy_version
                or row_cycle_id != cycle
                or pin is None
                or str(row.get("strategy_id") or "") != str(pin.get("strategy_id") or "")
                or row.get("strategy_version") != pin.get("strategy_version")
                or str(row.get("strategy_checksum") or "")
                != str(pin.get("strategy_checksum") or "")
                or intent_kind not in PAP.INTENT_PRIORITY
                or (str(row.get("account_id") or ""), str(row.get("code") or ""),
                    intent_kind) not in planned_intents
                or str(row.get("side") or "").lower() != expected_side):
            unknown.append({"order_id": row.get("id"),
                            "reason": "order_allocation_provenance_mismatch"})
        orders.append({
            "order_id": int(row["id"]), "cycle_id": row_cycle_id,
            "account_id": str(row["account_id"] or ""),
            "symbol": str(row["code"] or ""), "side": str(row["side"] or ""),
            "quantity": int(row["qty"] or 0), "status": str(row["status"] or ""),
            "intent_kind": intent_kind or None,
            "portfolio_snapshot_id": str(row["portfolio_snapshot_id"] or "") or None,
            "allocation_plan_id": str(row["allocation_plan_id"] or "") or None,
            "allocation_plan_fingerprint": str(
                row["allocation_plan_fingerprint"] or "") or None,
            "allocation_policy_version": str(
                row["allocation_policy_version"] or "") or None,
            "strategy_id": str(row["strategy_id"] or "") or None,
            "strategy_version": row["strategy_version"],
            "strategy_checksum": str(row["strategy_checksum"] or "") or None,
        })
    return {
        "authority": "read_only_exact_plan_projection",
        "cycle_id": cycle,
        "portfolio_snapshot": snapshot.projection(),
        "allocation_plan": plan.projection(),
        "order_provenance": {
            "status": "UNAVAILABLE" if unknown else "AVAILABLE",
            "orders": orders, "unknown_orders": unknown,
            "source_identity": f"paper_orders:allocation_plan:{plan.plan_id}",
        },
        "risk_decision": None,
        "production_permission": None,
    }


__all__ = ["PortfolioWorkspaceUnavailable", "get_portfolio_workspace"]
