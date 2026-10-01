# -*- coding: utf-8 -*-
"""Append-only persistence for canonical ``StrategyHealthSnapshot`` evidence.

The repository appends one immutable snapshot and reads one **explicitly named**
snapshot back. It has no latest/list operation, it never touches anything except
its own table, and it performs no business interpretation: every value it stores
is already the health owner's own projection. ``created_at`` is persistence
metadata and is deliberately not part of the deterministic fingerprint.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import strategy_health as SH


class StrategyHealthRepositoryError(ValueError):
    pass


def _payload(snapshot: SH.StrategyHealthSnapshot) -> str:
    return json.dumps(snapshot.projection(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _from_material(material: dict) -> SH.StrategyHealthSnapshot:
    coverage = material["coverage"]
    dimensions = tuple(
        SH.HealthDimension(
            name=item["name"], status=item["status"], facts=item.get("facts") or {},
            provenance=item["provenance"], source_identity=item.get("source_identity"),
            source_fingerprint=item.get("source_fingerprint"),
            blocking_reasons=tuple(item.get("blocking_reasons") or ()),
        ) for item in material["dimensions"]
    )
    return SH.StrategyHealthSnapshot(
        snapshot_id=material["snapshot_id"],
        snapshot_fingerprint=material["snapshot_fingerprint"],
        strategy_id=material["strategy_id"], strategy_version=int(material["strategy_version"]),
        strategy_checksum=material["strategy_checksum"],
        observation_start=material["observation_start"],
        observation_end=material["observation_end"],
        window_identity=material["window_identity"],
        lifecycle_state=material.get("lifecycle_state"),
        dimensions=dimensions,
        coverage=SH.HealthCoverage(
            expected_dimensions=coverage["expected_dimensions"],
            available_dimensions=coverage["available_dimensions"],
            partial_dimensions=coverage["partial_dimensions"],
            unavailable_dimensions=coverage["unavailable_dimensions"],
            not_applicable_dimensions=coverage["not_applicable_dimensions"],
            coverage_ratio=coverage["coverage_ratio"],
        ),
        source_identities=material.get("source_identities") or {},
        blocking_reasons=tuple(material.get("blocking_reasons") or ()),
        health_contract_version=material.get("health_contract_version",
                                             SH.HEALTH_CONTRACT_VERSION),
        schema_version=material.get("schema_version", SH.HEALTH_SCHEMA_VERSION),
    )


def append_snapshot(conn: sqlite3.Connection, snapshot: SH.StrategyHealthSnapshot, *,
                    created_at: str | None = None) -> SH.StrategyHealthSnapshot:
    """Append one snapshot idempotently; only touches ``strategy_health_snapshots``."""
    if not isinstance(snapshot, SH.StrategyHealthSnapshot):
        raise TypeError("canonical strategy health snapshot evidence is required")
    if not SH.verify_snapshot_fingerprint(snapshot):
        raise StrategyHealthRepositoryError("health_snapshot_fingerprint_mismatch")
    payload = _payload(snapshot)
    conn.execute(
        """INSERT OR IGNORE INTO strategy_health_snapshots
           (snapshot_id,snapshot_fingerprint,health_contract_version,strategy_id,
            strategy_version,strategy_checksum,observation_start,observation_end,
            window_identity,lifecycle_state,coverage_ratio,evidence_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (snapshot.snapshot_id, snapshot.snapshot_fingerprint,
         snapshot.health_contract_version, snapshot.strategy_id, int(snapshot.strategy_version),
         snapshot.strategy_checksum, snapshot.observation_start, snapshot.observation_end,
         snapshot.window_identity, snapshot.lifecycle_state,
         float(snapshot.coverage.coverage_ratio), payload,
         created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
    )
    row = conn.execute(
        "SELECT evidence_json FROM strategy_health_snapshots WHERE snapshot_id=?",
        (snapshot.snapshot_id,),
    ).fetchone()
    if row is None or str(row[0]) != payload:
        # Same identity, different content is a conflict, never a silent overwrite.
        raise StrategyHealthRepositoryError("health_snapshot_idempotency_conflict")
    return snapshot


def get_snapshot(conn: sqlite3.Connection,
                 snapshot_id: str) -> SH.StrategyHealthSnapshot | None:
    """Read only the exact snapshot ID supplied by the caller."""
    if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
        raise StrategyHealthRepositoryError("explicit_health_snapshot_id_required")
    row = conn.execute(
        "SELECT snapshot_id,snapshot_fingerprint,evidence_json"
        " FROM strategy_health_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        material = json.loads(row[2])
        snapshot = _from_material(material)
    except (TypeError, ValueError, KeyError) as exc:
        raise StrategyHealthRepositoryError("health_snapshot_evidence_invalid") from exc
    if (snapshot.snapshot_id != str(row[0])
            or snapshot.snapshot_fingerprint != str(row[1])
            or not SH.verify_snapshot_fingerprint(snapshot)):
        # Stored evidence must re-verify its own fingerprint: a drifted row is
        # corruption, never a quietly different snapshot.
        raise StrategyHealthRepositoryError("health_snapshot_fingerprint_mismatch")
    return snapshot


__all__ = ["StrategyHealthRepositoryError", "append_snapshot", "get_snapshot"]
