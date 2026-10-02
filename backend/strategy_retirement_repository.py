# -*- coding: utf-8 -*-
"""Append-only persistence for canonical ``StrategyRetirementDecision`` evidence.

The repository appends one immutable decision and reads one **explicitly named**
decision back. It has no latest/current operation, it never touches anything
except its own table, and it performs no business interpretation — the decision
is already the policy's own output. ``created_at`` is persistence metadata and is
deliberately not part of the deterministic fingerprint.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import strategy_retirement_policy as RP


class StrategyRetirementRepositoryError(ValueError):
    pass


def _payload(decision: RP.StrategyRetirementDecision) -> str:
    return json.dumps(decision.projection(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _from_material(material: dict) -> RP.StrategyRetirementDecision:
    return RP.StrategyRetirementDecision(
        decision_id=material["decision_id"],
        decision_fingerprint=material["decision_fingerprint"],
        snapshot_id=material["snapshot_id"],
        snapshot_fingerprint=material["snapshot_fingerprint"],
        strategy_id=material["strategy_id"],
        strategy_version=int(material["strategy_version"]),
        strategy_checksum=material["strategy_checksum"],
        decision=material["decision"],
        policy_version=material["policy_version"],
        evidence_summary=material.get("evidence_summary") or {},
        blocking_reasons=tuple(material.get("blocking_reasons") or ()),
        required_evidence=tuple(material.get("required_evidence") or ()),
        satisfied_evidence=tuple(material.get("satisfied_evidence") or ()),
        schema_version=material.get("schema_version", RP.RETIREMENT_SCHEMA_VERSION),
    )


def append_decision(conn: sqlite3.Connection,
                    decision: RP.StrategyRetirementDecision, *,
                    created_at: str | None = None) -> RP.StrategyRetirementDecision:
    """Append one decision idempotently; only touches ``strategy_retirement_decisions``."""
    if not isinstance(decision, RP.StrategyRetirementDecision):
        raise TypeError("canonical retirement decision evidence is required")
    if not RP.verify_decision_fingerprint(decision):
        raise StrategyRetirementRepositoryError("retirement_decision_fingerprint_mismatch")
    payload = _payload(decision)
    conn.execute(
        """INSERT OR IGNORE INTO strategy_retirement_decisions
           (decision_id,decision_fingerprint,snapshot_id,snapshot_fingerprint,strategy_id,
            strategy_version,decision_type,policy_version,evidence_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (decision.decision_id, decision.decision_fingerprint, decision.snapshot_id,
         decision.snapshot_fingerprint, decision.strategy_id,
         int(decision.strategy_version), decision.decision, decision.policy_version,
         payload,
         created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
    )
    row = conn.execute(
        "SELECT evidence_json FROM strategy_retirement_decisions WHERE decision_id=?",
        (decision.decision_id,),
    ).fetchone()
    if row is None or str(row[0]) != payload:
        # Same identity, different content is a conflict, never a silent overwrite.
        raise StrategyRetirementRepositoryError("retirement_decision_idempotency_conflict")
    return decision


def get_decision(conn: sqlite3.Connection,
                 decision_id: str) -> RP.StrategyRetirementDecision | None:
    """Read only the exact decision ID supplied by the caller."""
    if not isinstance(decision_id, str) or len(decision_id) != 64:
        raise StrategyRetirementRepositoryError("explicit_retirement_decision_id_required")
    row = conn.execute(
        "SELECT decision_id,decision_fingerprint,evidence_json"
        " FROM strategy_retirement_decisions WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        material = json.loads(row[2])
        decision = _from_material(material)
    except (TypeError, ValueError, KeyError) as exc:
        raise StrategyRetirementRepositoryError("retirement_decision_evidence_invalid") from exc
    if (decision.decision_id != str(row[0])
            or decision.decision_fingerprint != str(row[1])
            or not RP.verify_decision_fingerprint(decision)):
        raise StrategyRetirementRepositoryError("retirement_decision_fingerprint_mismatch")
    return decision


__all__ = ["StrategyRetirementRepositoryError", "append_decision", "get_decision"]
