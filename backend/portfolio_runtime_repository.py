# -*- coding: utf-8 -*-
"""Append-only persistence for exact ``PortfolioRuntimeSnapshot`` facts."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import portfolio_runtime as PR


class PortfolioRuntimeRepositoryError(ValueError):
    pass


def _payload(snapshot: PR.PortfolioRuntimeSnapshot) -> str:
    return json.dumps(snapshot.projection(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _from_material(value: dict) -> PR.PortfolioRuntimeSnapshot:
    return PR.build_portfolio_runtime_snapshot(
        cycle_id=value["cycle_id"], asof_day=value["asof_day"],
        decision_at=value["decision_at"], cycle_identity=value["cycle_identity"],
        strategy_pins=value["strategy_pins"],
        economic_owner_ids=value["economic_owner_ids"],
        execution_participant_ids=value["execution_participant_ids"],
        risk_exit_participant_ids=value["risk_exit_participant_ids"],
        source_identities=value["source_identities"],
        market_evidence_identity=value.get("market_evidence_identity"),
        dimensions=tuple(PR.PortfolioDimension(
            name=item["name"], status=item["status"], facts=item["facts"],
            provenance=item["provenance"], source_identity=item.get("source_identity"),
            source_fingerprint=item.get("source_fingerprint"),
            blocking_reasons=tuple(item.get("blocking_reasons") or ()))
            for item in value["dimensions"]))


def append_snapshot(conn: sqlite3.Connection, snapshot: PR.PortfolioRuntimeSnapshot, *,
                    created_at: str | None = None) -> PR.PortfolioRuntimeSnapshot:
    if not isinstance(snapshot, PR.PortfolioRuntimeSnapshot):
        raise TypeError("canonical portfolio runtime snapshot is required")
    if not PR.verify_snapshot_fingerprint(snapshot):
        raise PortfolioRuntimeRepositoryError("portfolio_snapshot_fingerprint_mismatch")
    payload = _payload(snapshot)
    conn.execute(
        "INSERT OR IGNORE INTO portfolio_runtime_snapshots"
        "(snapshot_id,snapshot_fingerprint,evidence_json,created_at) VALUES(?,?,?,?)",
        (snapshot.snapshot_id, snapshot.snapshot_fingerprint, payload,
         created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
    )
    row = conn.execute(
        "SELECT evidence_json,snapshot_fingerprint FROM portfolio_runtime_snapshots"
        " WHERE snapshot_id=?", (snapshot.snapshot_id,)).fetchone()
    if row is None or str(row[0]) != payload or str(row[1]) != snapshot.snapshot_fingerprint:
        raise PortfolioRuntimeRepositoryError("portfolio_snapshot_idempotency_conflict")
    return snapshot


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str):
    if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
        raise PortfolioRuntimeRepositoryError("explicit_portfolio_snapshot_id_required")
    row = conn.execute(
        "SELECT snapshot_id,snapshot_fingerprint,evidence_json"
        " FROM portfolio_runtime_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        return None
    try:
        snapshot = _from_material(json.loads(str(row[2])))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise PortfolioRuntimeRepositoryError("portfolio_snapshot_evidence_invalid") from exc
    if (snapshot.snapshot_id != str(row[0]) or snapshot.snapshot_fingerprint != str(row[1])
            or not PR.verify_snapshot_fingerprint(snapshot)):
        raise PortfolioRuntimeRepositoryError("portfolio_snapshot_fingerprint_mismatch")
    return snapshot


__all__ = ["PortfolioRuntimeRepositoryError", "append_snapshot", "get_snapshot"]
