"""Fail-closed checks for self-evolution evidence and model proposals.

This module is intentionally dependency-free.  It is used at the adaptive
engine boundary, before a proposal can be promoted or treated as authoritative.
The checks are conservative: malformed, stale, poisoned, non-finite, or
outlier-shaped input is shadowed/rejected rather than guessed around.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence


SHADOW_ONLY = "shadow_only"
MAX_EVIDENCE_AGE_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
MAX_MODEL_ITEMS = 12
MAX_MODEL_DEPTH = 8
MAX_NUMBER_ABS = 1_000_000.0
_POISON_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|<\s*script|javascript:|\x00)",
    re.IGNORECASE,
)
_TIME_KEYS = (
    "observed_at", "source_at", "snapshot_at", "quote_at", "asof", "created_at",
    "decision_at", "order_at", "fill_at", "published_at", "effective_at",
)


class AdversarialValidationError(ValueError):
    """Raised when a proposal cannot safely enter an apply path."""


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _walk(value, *, depth=0):
    """Yield values while bounding hostile recursive model output."""
    if depth > MAX_MODEL_DEPTH:
        raise AdversarialValidationError("model_output_depth_exceeded")
    if isinstance(value, Mapping):
        if len(value) > MAX_MODEL_ITEMS * 4:
            raise AdversarialValidationError("model_output_object_too_large")
        for key, item in value.items():
            yield key
            yield from _walk(item, depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_MODEL_ITEMS * 4:
            raise AdversarialValidationError("model_output_list_too_large")
        for item in value:
            yield from _walk(item, depth=depth + 1)
    else:
        yield value


def canonical_json(value) -> str:
    """Canonical representation used for evidence fingerprints."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def make_idempotency_key(action: str, candidate_id, actor: str = "") -> str:
    """Return a stable key for a human action/candidate pair."""
    raw = f"{str(action).strip().lower()}:{int(candidate_id)}:{str(actor).strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def require_human_confirmation(approved_by=None, *, confirmed: bool = True) -> str:
    """Validate an explicit human actor; automated identities are rejected."""
    actor = str(approved_by or "").strip()[:80]
    lowered = actor.lower()
    if not confirmed or not actor:
        raise AdversarialValidationError("human_confirmation_required")
    if lowered in {"auto", "system", "bounded-auto", "conservative-auto", "deepseek-bounded-realtime"}:
        raise AdversarialValidationError("automated_actor_not_allowed")
    if any(token in lowered for token in ("bot", "llm", "deepseek", "model", "agent")):
        raise AdversarialValidationError("automated_actor_not_allowed")
    return actor


def validate_evidence(evidence, *, reference_at=None, max_age_seconds=MAX_EVIDENCE_AGE_SECONDS):
    """Check evidence freshness, finite values, poison markers and hashes.

    ``reference_at`` is normally the order/decision timestamp, not wall clock;
    this lets historical replay remain valid while preventing a stale snapshot
    from being used for a newer decision.
    """
    flags = []
    if not isinstance(evidence, Mapping):
        return {"ok": False, "status": "invalid", "flags": ["not_object"]}
    try:
        values = list(_walk(evidence))
    except AdversarialValidationError as exc:
        return {"ok": False, "status": "poisoned", "flags": [str(exc)]}
    for value in values:
        if _finite_number(value) and abs(float(value)) > MAX_NUMBER_ABS:
            flags.append("numeric_outlier")
        if isinstance(value, float) and not math.isfinite(value):
            flags.append("non_finite")
        if isinstance(value, str) and _POISON_RE.search(value):
            flags.append("poison_marker")
    expected_hash = evidence.get("evidence_hash") or evidence.get("hash")
    if expected_hash:
        unsigned = {key: value for key, value in evidence.items() if key not in {"evidence_hash", "hash"}}
        if str(expected_hash).lower() != fingerprint(unsigned).lower():
            flags.append("hash_mismatch")
    ref = _parse_time(reference_at) or _dt.datetime.now(_dt.timezone.utc)
    stamps = []
    for key in _TIME_KEYS:
        stamp = _parse_time(evidence.get(key))
        if evidence.get(key) not in (None, "") and stamp is None:
            flags.append(f"invalid_timestamp:{key}")
        if stamp is not None:
            stamps.append((key, stamp))
            age = (ref - stamp).total_seconds()
            if age < -MAX_FUTURE_SKEW_SECONDS:
                flags.append(f"future_timestamp:{key}")
            elif age > float(max_age_seconds):
                flags.append(f"stale:{key}")
    # Deduplicate while keeping deterministic order for audit logs/tests.
    flags = list(dict.fromkeys(flags))
    stale = any(item.startswith("stale:") for item in flags)
    poisoned = any(item in {"poison_marker", "hash_mismatch", "non_finite", "numeric_outlier"} or item.startswith("future_timestamp") for item in flags)
    return {
        "ok": not flags,
        "status": "fresh" if not flags else ("poisoned" if poisoned else ("stale" if stale else "invalid")),
        "flags": flags,
        "timestamps": {key: stamp.isoformat() for key, stamp in stamps},
    }


def is_fresh_evidence(evidence, *, reference_at=None, max_age_seconds=MAX_EVIDENCE_AGE_SECONDS) -> bool:
    return bool(validate_evidence(evidence, reference_at=reference_at, max_age_seconds=max_age_seconds)["ok"])


def validate_model_output(output, *, allowed_accounts=None, max_items=3, max_abs=MAX_NUMBER_ABS):
    """Validate an LLM/GA output without allowing it to acquire authority."""
    flags = []
    if not isinstance(output, Mapping):
        return {"ok": False, "status": "invalid", "flags": ["not_object"]}
    try:
        values = list(_walk(output))
    except AdversarialValidationError as exc:
        return {"ok": False, "status": "poisoned", "flags": [str(exc)]}
    for value in values:
        if _finite_number(value) and abs(float(value)) > float(max_abs):
            flags.append("numeric_outlier")
        if isinstance(value, float) and not math.isfinite(value):
            flags.append("non_finite")
        if isinstance(value, str) and _POISON_RE.search(value):
            flags.append("poison_marker")
    proposals = output.get("proposals")
    if proposals is not None:
        if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes, bytearray)):
            flags.append("proposals_not_list")
        elif len(proposals) > int(max_items):
            flags.append("too_many_proposals")
        elif allowed_accounts is not None:
            allowed = {str(item) for item in allowed_accounts}
            for proposal in proposals:
                if not isinstance(proposal, Mapping) or str(proposal.get("account_id") or "") not in allowed:
                    flags.append("unknown_account")
    confidence = output.get("confidence")
    if confidence is not None and (not _finite_number(confidence) or not 0 <= float(confidence) <= 100):
        flags.append("confidence_outlier")
    flags = list(dict.fromkeys(flags))
    poisoned = any(item in {"poison_marker", "non_finite", "numeric_outlier"} for item in flags)
    return {"ok": not flags, "status": "valid" if not flags else ("poisoned" if poisoned else "invalid"), "flags": flags}


def validate_candidate_output(candidate, *, baseline=None, max_relative_delta=1.0):
    """Validate a numeric candidate and reject implausible parameter jumps."""
    result = validate_model_output(candidate, max_items=MAX_MODEL_ITEMS)
    flags = list(result["flags"])
    if isinstance(candidate, Mapping) and isinstance(baseline, Mapping):
        for key, value in candidate.items():
            old = baseline.get(key)
            if _finite_number(value) and _finite_number(old):
                scale = max(abs(float(old)), 1e-9)
                if abs(float(value) - float(old)) / scale > float(max_relative_delta):
                    flags.append(f"outlier_delta:{key}")
    flags = list(dict.fromkeys(flags))
    return {"ok": not flags, "status": "valid" if not flags else "outlier", "flags": flags}


# Short aliases make the pure guard convenient for callers and tests.
validate_evidence_freshness = validate_evidence
validate_outlier = validate_candidate_output

