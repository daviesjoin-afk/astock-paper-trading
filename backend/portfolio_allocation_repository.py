# -*- coding: utf-8 -*-
"""Append-only persistence for immutable ``PortfolioAllocationPlan`` records.

The plan is evidence, not state: this repository offers ``append_plan()`` and an
exact ``get_plan(plan_id)`` only. There is deliberately no ``get_latest_plan``,
no ``get_current_plan``, no overwrite, and no delete. A repeated append of the
same ``plan_id`` is idempotent; the same ``plan_id`` with different content is a
conflict. Schema creation belongs to the formal migration owner, never to this
repository or to the application service.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import portfolio_allocation_policy as PAP


class PortfolioAllocationRepositoryError(ValueError):
    pass


def _payload(plan: PAP.PortfolioAllocationPlan) -> str:
    return json.dumps(plan.projection(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def append_plan(conn: sqlite3.Connection, plan: PAP.PortfolioAllocationPlan, *,
                created_at: str | None = None) -> PAP.PortfolioAllocationPlan:
    """Append one plan. ``created_at`` is persistence metadata, never fingerprint material."""
    if not isinstance(plan, PAP.PortfolioAllocationPlan):
        raise TypeError("canonical portfolio allocation plan is required")
    if not PAP.verify_plan_fingerprint(plan):
        raise PortfolioAllocationRepositoryError("allocation_plan_fingerprint_mismatch")
    payload = _payload(plan)
    conn.execute(
        "INSERT OR IGNORE INTO portfolio_allocation_plans"
        "(plan_id,plan_fingerprint,plan_json,created_at) VALUES(?,?,?,?)",
        (plan.plan_id, plan.plan_fingerprint, payload,
         created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
    )
    row = conn.execute(
        "SELECT plan_json,plan_fingerprint FROM portfolio_allocation_plans"
        " WHERE plan_id=?", (plan.plan_id,)).fetchone()
    if row is None or str(row[0]) != payload or str(row[1]) != plan.plan_fingerprint:
        raise PortfolioAllocationRepositoryError("allocation_plan_idempotency_conflict")
    return plan


def get_plan(conn: sqlite3.Connection, plan_id: str):
    """Read only the exact immutable plan named by its fingerprint ID."""
    if not isinstance(plan_id, str) or len(plan_id) != 64:
        raise PortfolioAllocationRepositoryError("explicit_plan_id_required")
    row = conn.execute(
        "SELECT plan_id,plan_fingerprint,plan_json FROM portfolio_allocation_plans"
        " WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        return None
    try:
        plan = PAP.plan_from_projection(json.loads(str(row[2])))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise PortfolioAllocationRepositoryError("allocation_plan_evidence_invalid") from exc
    if (plan.plan_id != str(row[0]) or plan.plan_fingerprint != str(row[1])
            or not PAP.verify_plan_fingerprint(plan)):
        raise PortfolioAllocationRepositoryError("allocation_plan_fingerprint_mismatch")
    return plan


__all__ = ["PortfolioAllocationRepositoryError", "append_plan", "get_plan"]
