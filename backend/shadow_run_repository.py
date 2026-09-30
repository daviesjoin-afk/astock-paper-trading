"""Append-only persistence for canonical ShadowRun evidence.

The repository can read one explicitly named prior run and append an immutable
result. It intentionally has no latest/list operation used for continuation.
"""
from __future__ import annotations

import json
import sqlite3

import shadow_runtime as SR


class ShadowRunRepositoryError(ValueError):
    pass


def _run_spec(value: dict) -> SR.ShadowRunSpec:
    return SR.ShadowRunSpec(
        challenger=SR.StrategyStamp(**value["challenger"]),
        active_comparator=SR.StrategyStamp(**value["active_comparator"]),
        environment_fingerprint=value["environment_fingerprint"],
        session_date=value["session_date"], decision_at=value["decision_at"],
        reference_capital=value["reference_capital"],
        previous_shadow_run_id=value.get("previous_shadow_run_id"),
        run_schema_version=value["run_schema_version"],
    )


def _material(evidence: SR.ShadowRunEvidence) -> dict:
    return {
        "schema_version": SR.SHADOW_RUN_SCHEMA_VERSION,
        "spec": SR._plain(evidence.spec),
        "challenger_runtime_inputs": SR._plain(evidence.challenger_runtime_inputs),
        "environment": SR._plain(evidence.environment),
        "strategy_definition_fingerprint": evidence.strategy_definition_fingerprint,
        "before_state": SR._plain(evidence.before_state),
        "decisions": SR._plain(evidence.decisions),
        "after_state": SR._plain(evidence.after_state),
        "previous_run_fingerprint": evidence.previous_run_fingerprint,
    }


def append_run(conn: sqlite3.Connection, evidence: SR.ShadowRunEvidence) -> SR.ShadowRunEvidence:
    """Append evidence idempotently; only touches ``shadow_runs``."""
    if not isinstance(evidence, SR.ShadowRunEvidence):
        raise TypeError("canonical ShadowRun evidence is required")
    material = _material(evidence)
    if evidence.run_id != evidence.run_fingerprint or SR.fingerprint(material) != evidence.run_fingerprint:
        raise ShadowRunRepositoryError("shadow_run_fingerprint_mismatch")
    spec = dict(evidence.spec)
    challenger, active = spec["challenger"], spec["active_comparator"]
    previous_id = spec.get("previous_shadow_run_id")
    run_spec = _run_spec(spec)
    if previous_id is not None:
        previous = get_run(conn, previous_id)
        if previous is None or previous.run_fingerprint != evidence.previous_run_fingerprint:
            raise ShadowRunRepositoryError("explicit_previous_shadow_run_required")
        expected_before = SR._state_for_run(run_spec, previous).projection()
        if expected_before != evidence.before_state:
            raise ShadowRunRepositoryError("shadow_run_continuation_state_mismatch")
    elif evidence.previous_run_fingerprint is not None:
        raise ShadowRunRepositoryError("unexpected_previous_shadow_run")
    elif SR.ShadowRuntimeState.initial(
            run_spec.reference_capital, run_spec.session_date).projection() != evidence.before_state:
        raise ShadowRunRepositoryError("shadow_run_initial_state_mismatch")
    conn.execute(
        """INSERT OR IGNORE INTO shadow_runs
           (run_id,run_fingerprint,challenger_strategy_id,challenger_strategy_version,
            challenger_strategy_checksum,active_strategy_id,active_strategy_version,
            active_strategy_checksum,environment_fingerprint,session_date,decision_at,
            reference_capital,previous_shadow_run_id,evidence_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (evidence.run_id, evidence.run_fingerprint,
         challenger["strategy_id"], int(challenger["version"]), challenger["checksum"],
         active["strategy_id"], int(active["version"]), active["checksum"],
         spec["environment_fingerprint"], spec["session_date"], spec["decision_at"],
         float(spec["reference_capital"]), previous_id, SR.canonical_json(material)),
    )
    row = conn.execute(
        "SELECT evidence_json FROM shadow_runs WHERE run_id=?", (evidence.run_id,),
    ).fetchone()
    if row is None or str(row[0]) != SR.canonical_json(material):
        raise ShadowRunRepositoryError("shadow_run_idempotency_conflict")
    return evidence


def get_run(conn: sqlite3.Connection, run_id: str) -> SR.ShadowRunEvidence | None:
    """Read only the exact run ID supplied by the caller."""
    if not isinstance(run_id, str) or len(run_id) != 64:
        raise ShadowRunRepositoryError("explicit_shadow_run_id_required")
    row = conn.execute(
        "SELECT run_id,run_fingerprint,evidence_json FROM shadow_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        material = json.loads(row[2])
    except (TypeError, ValueError) as exc:
        raise ShadowRunRepositoryError("shadow_run_evidence_invalid") from exc
    if SR.fingerprint(material) != str(row[1]) or str(row[0]) != str(row[1]):
        raise ShadowRunRepositoryError("shadow_run_fingerprint_mismatch")
    return SR.ShadowRunEvidence(
        run_id=str(row[0]), run_fingerprint=str(row[1]), spec=material["spec"],
        environment=material["environment"],
        challenger_runtime_inputs=material["challenger_runtime_inputs"],
        strategy_definition_fingerprint=material["strategy_definition_fingerprint"],
        before_state=material["before_state"], decisions=tuple(material["decisions"]),
        after_state=material["after_state"],
        previous_run_fingerprint=material["previous_run_fingerprint"],
    )

