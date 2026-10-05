# -*- coding: utf-8 -*-
"""Strict resolver for explicit adaptive allocation-weight declarations."""
from __future__ import annotations

import hashlib
import json
import math
import datetime as dt
import sqlite3
from collections.abc import Mapping


class AllocationWeightEvidenceUnavailable(ValueError):
    """The exact eligible set has no complete declared canonical weights."""


def read_canonical_allocation_weight_owner_rows(conn, *, eligible_account_ids,
                                               cycle_id):
    """Read only the append-only parameter owner rows for an exact cycle set."""
    accounts = sorted({str(value) for value in eligible_account_ids})
    try:
        cycle = int(cycle_id)
        if cycle <= 0 or not accounts:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise AllocationWeightEvidenceUnavailable(
            "explicit_weight_cycle_and_eligible_set_required") from exc
    placeholders = ",".join("?" for _ in accounts)
    try:
        rows = conn.execute(
            """SELECT id,cycle_id,account_id,version,style,params,reason,
                      effective_date,created_at
                 FROM paper_parameter_versions
                WHERE cycle_id=? AND account_id IN (""" + placeholders + ")",
            (cycle, *accounts),
        ).fetchall()
    except (sqlite3.Error, AttributeError) as exc:
        raise AllocationWeightEvidenceUnavailable(
            "canonical_allocation_weight_declaration_unavailable") from exc
    return [dict(row) for row in rows]


def _params(value):
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise AllocationWeightEvidenceUnavailable(
            "allocation_weight_owner_payload_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise AllocationWeightEvidenceUnavailable(
            "allocation_weight_owner_payload_invalid")
    return dict(parsed)


def _aware_instant(value, *, reason):
    text = str(value or "").strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AllocationWeightEvidenceUnavailable(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AllocationWeightEvidenceUnavailable(reason)
    return parsed.astimezone(dt.timezone.utc)


def resolve_canonical_allocation_weights(rows, *, eligible_account_ids,
                                         cycle_id, asof_day, decision_at,
                                         strategy_pins):
    """Resolve weights from immutable, cycle-scoped parameter history.

    ``rows`` must come from ``paper_parameter_versions`` and include its owner
    row identity and timestamps. Current ``paper_accounts.params`` is never an
    authority for a historical plan. Missing as-of history blocks the complete
    canonical set; risk caps and equal weights are not allocation fallbacks.
    """
    try:
        cycle = int(cycle_id)
        day = str(asof_day)
        expected = {str(value) for value in eligible_account_ids}
        if cycle <= 0 or not day or not expected:
            raise ValueError
        target_day = dt.date.fromisoformat(day).isoformat()
    except (TypeError, ValueError) as exc:
        raise AllocationWeightEvidenceUnavailable(
            "explicit_weight_cycle_asof_and_eligible_set_required") from exc
    decision_instant = _aware_instant(
        decision_at, reason="explicit_weight_decision_at_required")
    by_account = {account: [] for account in expected}
    for raw in rows or ():
        row = dict(raw)
        account = str(row.get("account_id") or "")
        if not account:
            raise AllocationWeightEvidenceUnavailable(
                "allocation_weight_owner_identity_invalid")
        if account not in expected:
            continue
        try:
            row_cycle = int(row.get("cycle_id"))
        except (TypeError, ValueError):
            continue
        if row_cycle != cycle:
            continue
        try:
            row_id = int(row.get("id"))
            effective = dt.date.fromisoformat(
                str(row.get("effective_date") or "")).isoformat()
        except (TypeError, ValueError):
            continue
        try:
            created = _aware_instant(
                row.get("created_at"), reason="allocation_weight_owner_timestamp_invalid")
        except AllocationWeightEvidenceUnavailable:
            # Legacy/malformed rows have no usable decision instant and cannot
            # prove a declaration. Other valid owner rows can still be selected.
            continue
        if row_id <= 0:
            continue
        if (row_cycle == cycle and effective <= target_day
                and created <= decision_instant):
            by_account[account].append((effective, created, row_id, row))
    selected = {}
    for account, candidates in by_account.items():
        if not candidates:
            raise AllocationWeightEvidenceUnavailable(
                "canonical_allocation_weight_declaration_unavailable")
        selected[account] = max(candidates, key=lambda item: item[:3])[3]
    pins_by_account = {str(item.get("account_id")): dict(item)
                       for item in strategy_pins or ()}
    if set(pins_by_account) != expected:
        raise AllocationWeightEvidenceUnavailable(
            "allocation_weight_strategy_pin_coverage_mismatch")

    weights = {}
    declarations = []
    for account in sorted(expected):
        row = selected[account]
        params = _params(row.get("params"))
        declaration = params.get("adaptive_allocation")
        if not isinstance(declaration, Mapping) or declaration.get("status") != "active":
            raise AllocationWeightEvidenceUnavailable(
                "canonical_allocation_weight_declaration_unavailable")
        effective = str(declaration.get("effective_date") or "")[:10]
        try:
            effective = dt.date.fromisoformat(effective).isoformat()
            target_day = dt.date.fromisoformat(day).isoformat()
        except (TypeError, ValueError) as exc:
            raise AllocationWeightEvidenceUnavailable(
                "canonical_allocation_weight_effective_date_invalid") from exc
        if effective > target_day:
            raise AllocationWeightEvidenceUnavailable(
                "canonical_allocation_weight_not_effective_asof")
        try:
            weight_pct = float(declaration.get("weight_pct"))
        except (TypeError, ValueError) as exc:
            raise AllocationWeightEvidenceUnavailable(
                "canonical_allocation_weight_invalid") from exc
        if not math.isfinite(weight_pct) or not 0.0 < weight_pct <= 100.0:
            raise AllocationWeightEvidenceUnavailable(
                "canonical_allocation_weight_invalid")
        weight = weight_pct / 100.0
        weights[account] = weight
        pin = pins_by_account[account]
        declarations.append({
            "cycle_id": cycle, "account_id": account,
            "parameter_version_row_id": int(row["id"]),
            "parameter_version": str(row.get("version") or ""),
            "owner_effective_date": str(row["effective_date"]),
            "owner_created_at": str(row["created_at"]),
            "adaptive_allocation": dict(declaration),
            "strategy_id": str(pin.get("strategy_id") or ""),
            "strategy_version": int(pin.get("strategy_version") or 0),
            "strategy_checksum": str(pin.get("strategy_checksum") or ""),
            "effective_date": effective, "weight_pct": weight_pct,
            "status": "active",
        })
    canonical_decision_at = decision_instant.isoformat()
    material = {"cycle_id": cycle, "asof_day": target_day,
                "decision_at": canonical_decision_at,
                "declarations": declarations}
    fingerprint = hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    return {
        "weights": weights,
        "source_identity": (
            f"paper_parameter_versions:cycle:{cycle}:asof:{target_day}:"
            f"decision:{canonical_decision_at}"),
        "source_fingerprint": fingerprint,
        "declarations": declarations,
    }


__all__ = ["AllocationWeightEvidenceUnavailable",
           "read_canonical_allocation_weight_owner_rows",
           "resolve_canonical_allocation_weights"]
