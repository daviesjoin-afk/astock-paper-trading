# -*- coding: utf-8 -*-
"""Strict resolver for explicit adaptive allocation-weight declarations."""
from __future__ import annotations

import hashlib
import json
import math
import datetime as dt
from collections.abc import Mapping


class AllocationWeightEvidenceUnavailable(ValueError):
    """The exact eligible set has no complete declared canonical weights."""


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


def resolve_canonical_allocation_weights(rows, *, eligible_account_ids,
                                         cycle_id, asof_day, strategy_pins):
    """Resolve exact owner-issued weights without risk-cap or equal-weight fallback.

    The only currently supported declaration is an active, as-of-valid
    ``adaptive_allocation.weight_pct`` on the exact cycle account row. A missing
    declaration blocks the whole canonical set; ``max_exposure`` is a risk cap,
    not an allocation preference.
    """
    try:
        cycle = int(cycle_id)
        day = str(asof_day)
        expected = {str(value) for value in eligible_account_ids}
        if cycle <= 0 or not day or not expected:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise AllocationWeightEvidenceUnavailable(
            "explicit_weight_cycle_asof_and_eligible_set_required") from exc
    by_account = {}
    for raw in rows or ():
        row = dict(raw)
        account = str(row.get("id") or "")
        if not account or account in by_account:
            raise AllocationWeightEvidenceUnavailable(
                "allocation_weight_owner_identity_invalid")
        by_account[account] = row
    if set(by_account) != expected:
        raise AllocationWeightEvidenceUnavailable(
            "allocation_weight_owner_coverage_mismatch")
    pins_by_account = {str(item.get("account_id")): dict(item)
                       for item in strategy_pins or ()}
    if set(pins_by_account) != expected:
        raise AllocationWeightEvidenceUnavailable(
            "allocation_weight_strategy_pin_coverage_mismatch")

    weights = {}
    declarations = []
    for account in sorted(expected):
        row = by_account[account]
        try:
            if int(row.get("cycle_id")) != cycle:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise AllocationWeightEvidenceUnavailable(
                "allocation_weight_cycle_identity_mismatch") from exc
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
            "strategy_id": str(pin.get("strategy_id") or ""),
            "strategy_version": int(pin.get("strategy_version") or 0),
            "strategy_checksum": str(pin.get("strategy_checksum") or ""),
            "effective_date": effective, "weight_pct": weight_pct,
            "status": "active",
        })
    material = {"cycle_id": cycle, "asof_day": day,
                "declarations": declarations}
    fingerprint = hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    return {
        "weights": weights,
        "source_identity": f"paper_accounts:adaptive_allocation:cycle:{cycle}:asof:{day}",
        "source_fingerprint": fingerprint,
        "declarations": declarations,
    }


__all__ = ["AllocationWeightEvidenceUnavailable",
           "resolve_canonical_allocation_weights"]
