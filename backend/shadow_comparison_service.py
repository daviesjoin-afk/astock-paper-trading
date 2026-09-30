"""Application boundary for explicit Active/Challenger comparison evidence.

This service is the only place that touches a database. It loads exact evidence
by explicit ID — the named Active orders and the named ShadowRun — hands the
three explicit inputs to the pure builder, and appends one immutable report.
There is no latest/current/head/provider lookup anywhere on this path, and no
formal ledger is ever written.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

import shadow_comparison as SC
import shadow_comparison_repository as SCR
import shadow_run_repository as SRR

# ``risk_payload`` is selected on purpose: it is the order's own row, so it is
# the only exactly-linked Active admission decision. It is read for the
# explicitly named orders only (never for a page or a scan).
_ORDER_COLUMNS = (
    "id", "account_id", "signal_id", "cycle_id", "strategy_id", "strategy_version",
    "strategy_checksum", "code", "side", "status", "reason", "order_type", "qty",
    "planned_price", "filled_qty", "filled_price", "amount", "fees", "realized_pnl",
    "created_at", "execution_evidence", "risk_payload",
)
_SIGNAL_COLUMNS = (
    "id", "status", "reason", "signal_date", "intended_date", "rank_score", "t_tier",
    "t_score", "close_price", "payload",
)


def _row_mapping(cursor, row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return dict(row)
    names = [item[0] for item in cursor.description or ()]
    return dict(zip(names, row, strict=True))


def _json_mapping(raw, *, label: str) -> dict:
    """Parse one persisted JSON column, keeping corruption visible."""
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise SC.ShadowComparisonError(f"{label}_invalid") from exc
    return parsed if isinstance(parsed, dict) else {}


def _execution_evidence(raw) -> dict | None:
    """The Execution Authority's persisted evidence, or unknown when unwritten."""
    evidence = _json_mapping(raw, label="active_execution_evidence")
    return evidence or None


def _admission_evidence(raw) -> dict | None:
    """The order's own persisted buy-path admission decision, when one exists.

    The linkage is exact by construction: this is the named order's own column,
    never a lookup by account/code/time. Rows written by the frozen-entry
    waitlist path carry no decision snapshot and are reported as absent rather
    than reconstructed.
    """
    payload = _json_mapping(raw, label="active_risk_payload")
    snapshot = payload.get("decision_snapshot")
    final = snapshot.get("final") if isinstance(snapshot, Mapping) else None
    if not isinstance(final, Mapping) or not str(final.get("decision") or "").strip():
        return None
    gates = {name: dict(payload[name])
             for name in ("chase_entry", "three_day_timing_gate",
                          "entry_price_gate", "execution_dispatch")
             if isinstance(payload.get(name), Mapping)}
    return {
        "source": "risk_payload.decision_snapshot.final",
        "decision": str(final.get("decision")),
        "reason": final.get("reason"),
        # Named so it can never be mistaken for a comparison-made score.
        "admission_score": final.get("score"),
        "gates": gates or None,
    }


def _signal_evidence(conn: sqlite3.Connection, signal_id) -> dict | None:
    """Read the exact signal row the order points at; never search for one."""
    if signal_id is None:
        return None
    cursor = conn.execute(
        f"SELECT {','.join(_SIGNAL_COLUMNS)} FROM paper_signals WHERE id=?",
        (int(signal_id),),
    )
    values = _row_mapping(cursor, cursor.fetchone())
    if not values:
        # No fallback lookup by (account, date, code): an absent row stays absent.
        return None
    payload_status = "ok"
    try:
        payload = _json_mapping(values.get("payload"), label="active_signal_payload")
    except SC.ShadowComparisonError:
        payload, payload_status = {}, "invalid"
    decision = payload.get("signal_decision")
    evidence = payload.get("signal_evidence")
    return {
        "signal_id": int(values["id"]),
        "status": values.get("status"),
        "reason": values.get("reason"),
        "signal_date": values.get("signal_date"),
        "intended_date": values.get("intended_date"),
        "payload_status": payload_status,
        "signal_decision": decision if isinstance(decision, dict) else None,
        "signal_evidence": evidence if isinstance(evidence, dict) else None,
        # Raw owner output from the persisted row; never a comparison-made score.
        "selection_scores": {
            "rank_score": values.get("rank_score"),
            "t_tier": values.get("t_tier"),
            "t_score": values.get("t_score"),
            "close_price": values.get("close_price"),
        },
    }


def load_active_comparison_evidence(
        conn: sqlite3.Connection, spec: SC.ComparisonSpec,
) -> SC.ActiveComparisonEvidence:
    """Load exactly the declared Active orders, one row each, by primary key."""
    orders = []
    for order_id in spec.active_order_ids:
        cursor = conn.execute(
            f"SELECT {','.join(_ORDER_COLUMNS)} FROM paper_orders WHERE id=?",
            (int(order_id),),
        )
        values = _row_mapping(cursor, cursor.fetchone())
        if not values:
            raise SC.ShadowComparisonError(
                f"active_order_evidence_unavailable:{int(order_id)}")
        orders.append(SC.ActiveOrderEvidence(
            order_id=int(values["id"]),
            account_id=values.get("account_id"),
            symbol=values.get("code"),
            side=values.get("side"),
            order_status=values.get("status"),
            created_at=values.get("created_at"),
            requested_quantity=int(values.get("qty") or 0),
            filled_quantity=int(values.get("filled_qty") or 0),
            signal_id=(int(values["signal_id"])
                       if values.get("signal_id") is not None else None),
            cycle_id=(int(values["cycle_id"])
                      if values.get("cycle_id") is not None else None),
            strategy_id=values.get("strategy_id"),
            strategy_version=(int(values["strategy_version"])
                              if values.get("strategy_version") is not None else None),
            strategy_checksum=values.get("strategy_checksum"),
            order_reason=values.get("reason"),
            order_type=values.get("order_type"),
            planned_price=values.get("planned_price"),
            filled_price=values.get("filled_price"),
            amount=values.get("amount"),
            fees=values.get("fees"),
            realized_pnl=values.get("realized_pnl"),
            signal_evidence=_signal_evidence(conn, values.get("signal_id")),
            admission_evidence=_admission_evidence(values.get("risk_payload")),
            execution_evidence=_execution_evidence(values.get("execution_evidence")),
        ))
    return SC.ActiveComparisonEvidence.build(orders)


def build_and_append_comparison(
        conn: sqlite3.Connection, *, spec: SC.ComparisonSpec,
) -> SC.ShadowComparisonReport:
    """Read the named evidence, build one report, append it idempotently.

    The declared `active_evidence_id` is checked against the fingerprint of the
    exact projection just loaded. `paper_orders` rows are updated in place as
    execution progresses, so order ids alone are not an evidence identity: a
    drifted row is a different comparison and fails closed here instead of being
    accepted as the same one.
    """
    shadow_run = SRR.get_run(conn, spec.shadow_run_id)
    if shadow_run is None:
        raise SC.ShadowComparisonError("explicit_shadow_run_unavailable")
    active_evidence = load_active_comparison_evidence(conn, spec)
    if active_evidence.source_fingerprint != spec.active_evidence_id:
        raise SC.ShadowComparisonError("active_evidence_fingerprint_mismatch")
    report = SC.build_shadow_comparison(
        spec=spec, active_evidence=active_evidence, shadow_run=shadow_run,
    )
    return SCR.append_report(conn, report)
