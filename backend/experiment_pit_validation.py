"""Fail-closed PIT input validation for the R29 canonical runner boundary.

This module composes existing evidence owners. It does not run a strategy,
fetch data, or grant selection/promotion authority.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

try:
    import experiment_contract as EC
    import financial_point_in_time as FPIT
    import learning_dataset as LD
    import point_in_time as PIT
    import strategy_registry as SR
    import tradability_archive as TA
    import walk_forward_validation as WFV
except ImportError:  # pragma: no cover - package-style import
    from . import experiment_contract as EC
    from . import financial_point_in_time as FPIT
    from . import learning_dataset as LD
    from . import point_in_time as PIT
    from . import strategy_registry as SR
    from . import tradability_archive as TA
    from . import walk_forward_validation as WFV


_DIMENSIONS = (
    "historical_universe", "historical_tradability", "market_data_pit",
    "fundamental_pit", "strategy_version", "dataset", "execution_model",
    "cost_model", "walk_forward",
)
_REASON_CODES = frozenset({
    "dataset_identity_mismatch", "evaluation_asof_invalid",
    "execution_assumptions_unproven", "fundamental_publication_unproven",
    "historical_market_data_unavailable", "historical_universe_unproven",
    "strategy_identity_mismatch", "tradability_coverage_incomplete",
    "walk_forward_explicit_sessions_required", "walk_forward_window_not_matured",
    "walk_forward_sample_session_invalid", "walk_forward_unavailable",
    "walk_forward_ready_fold_missing", "walk_forward_session_calendar_unproven",
})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _dimension(
    status: str,
    reason_code: str | None,
    identity: Any,
    provenance_status: str,
    coverage: Mapping[str, Any] | None = None,
) -> dict:
    if status not in {"proven", "blocked", "not_applicable"}:
        raise ValueError("invalid PIT dimension status")
    if reason_code is not None and reason_code not in _REASON_CODES:
        raise ValueError(f"unsupported PIT reason code: {reason_code}")
    return {
        "status": status,
        "reason_code": reason_code,
        "declared_identity": identity,
        "provenance_status": provenance_status,
        "coverage": dict(coverage or {}),
    }


@dataclass(frozen=True, slots=True)
class PITValidationEvidence:
    """Immutable input-readiness evidence; it contains no performance result."""

    experiment_fingerprint: str
    status: str
    reason_codes: tuple[str, ...]
    dimensions: Mapping[str, Any]
    walk_forward: Mapping[str, Any]
    data_coverage: Mapping[str, Any]
    pit_warnings: tuple[str, ...]
    train_period: Mapping[str, Any] | None = None
    validation_period: Mapping[str, Any] | None = None
    oos_period: Mapping[str, Any] | None = None
    windows: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.experiment_fingerprint, str)
                or len(self.experiment_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in self.experiment_fingerprint)):
            raise ValueError("experiment_fingerprint must be a SHA-256 digest")
        if self.status not in {"ready", "blocked"}:
            raise ValueError("PIT validation status must be ready or blocked")
        reasons = tuple(sorted(set(self.reason_codes)))
        if any(reason not in _REASON_CODES for reason in reasons):
            raise ValueError("reason_codes contains an unsupported code")
        if self.status == "ready" and reasons:
            raise ValueError("ready evidence cannot contain blocking reason codes")
        if self.status == "blocked" and not reasons:
            raise ValueError("blocked evidence requires a stable reason code")
        if tuple(self.dimensions) != _DIMENSIONS:
            raise ValueError("dimensions must contain the canonical ordered dimension set")
        if self.status == "ready" and any(
            self.dimensions[name]["status"] not in {"proven", "not_applicable"}
            for name in _DIMENSIONS
        ):
            raise ValueError("ready evidence requires every required dimension to be proven")
        if self.status == "ready" and int(self.walk_forward.get("ready_folds", 0)) < 1:
            raise ValueError("ready evidence requires at least one ready walk-forward fold")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "pit_warnings", tuple(sorted(set(self.pit_warnings))))
        object.__setattr__(self, "dimensions", _freeze(self.dimensions))
        object.__setattr__(self, "walk_forward", _freeze(self.walk_forward))
        object.__setattr__(self, "data_coverage", _freeze(self.data_coverage))
        object.__setattr__(self, "train_period", _freeze(self.train_period))
        object.__setattr__(self, "validation_period", _freeze(self.validation_period))
        object.__setattr__(self, "oos_period", _freeze(self.oos_period))
        object.__setattr__(self, "windows", _freeze(self.windows))

    def projection(self) -> dict[str, Any]:
        """JSON-compatible report projection for future canonical UI consumers."""
        return {
            "experiment_fingerprint": self.experiment_fingerprint,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "dimensions": _thaw(self.dimensions),
            "walk_forward": _thaw(self.walk_forward),
            "data_coverage": _thaw(self.data_coverage),
            "pit_warnings": list(self.pit_warnings),
            "periods": {
                "train": _thaw(self.train_period),
                "validation": _thaw(self.validation_period),
                "oos": _thaw(self.oos_period),
            },
            "windows": _thaw(self.windows),
        }


def _strategy_identity(spec: EC.ExperimentSpec, version: Any) -> dict:
    identity = spec.strategy.projection()
    if not isinstance(version, SR.StrategyVersion):
        return _dimension("blocked", "strategy_identity_mismatch", identity,
                          "unproven")
    matches = (
        version.strategy_id == spec.strategy.strategy_id
        and version.version == spec.strategy.version
        and version.checksum == spec.strategy.checksum
    )
    return _dimension("proven" if matches else "blocked",
                      None if matches else "strategy_identity_mismatch",
                      identity, "owner_version_match" if matches else "owner_version_mismatch")


def _manifest_dimension(spec: EC.ExperimentSpec, manifest: Any) -> dict:
    identity = {"dataset_fingerprint": spec.dataset_fingerprint}
    matches = isinstance(manifest, Mapping) and (
        manifest.get("dataset_fingerprint") == spec.dataset_fingerprint
    )
    return _dimension("proven" if matches else "blocked",
                      None if matches else "dataset_identity_mismatch", identity,
                      "exact_manifest_match" if matches else "manifest_missing_or_mismatch")


def _universe(
    spec: EC.ExperimentSpec, rows: Any, source: Any, sessions: Sequence[str], *,
    session_calendar_complete: bool,
) -> tuple[dict, dict[str, list], dict]:
    row_list = list(rows or ())
    members_by_session: dict[str, list] = {}
    session_reports: dict[str, Any] = {}
    for session in sessions:
        try:
            result = PIT.historical_universe(
                row_list, session, source=source, drop_unproven=True,
            )
        except (TypeError, ValueError):
            result = {"passed": False, "members": [], "report": {}}
        report = result.get("report", {})
        session_reports[session] = report
        if (result.get("passed")
                and report.get("historical_membership_complete")
                and result.get("members")):
            members_by_session[session] = list(result["members"])
    memberships_complete = bool(sessions) and len(members_by_session) == len(sessions)
    passed = memberships_complete and session_calendar_complete
    available_sessions = len(members_by_session)
    identity = {"universe_fingerprint": spec.universe_fingerprint}
    detail = _dimension("proven" if passed else "blocked",
                        None if passed else "historical_universe_unproven", identity,
        "historical_archive_complete" if passed else "session_calendar_unproven"
        if memberships_complete and not session_calendar_complete else next((
            str(item.get("status") or "unproven")
            for item in session_reports.values()
            if not item.get("historical_membership_complete")
        ), "unproven"),
        {"sessions_requested": len(sessions),
                         "sessions_proven": available_sessions,
                         "ratio": _ratio(available_sessions, len(sessions))
                         if session_calendar_complete else None})
    report = {"sessions": session_reports}
    return detail, members_by_session, report


def _member_code(member: Any) -> str | None:
    if isinstance(member, Mapping):
        value = member.get("code", member.get("symbol", member.get("ticker")))
    else:
        value = getattr(member, "code", None)
    text = str(value or "").strip()
    return text or None


def _tradability(
    spec: EC.ExperimentSpec,
    members_by_session: Mapping[str, Sequence[Any]],
    sessions: Sequence[str],
    repository: Any,
    *,
    universe_complete: bool,
) -> tuple[dict, dict]:
    codes_by_session = {
        session: sorted({code for code in (_member_code(row) for row in members) if code})
        for session, members in members_by_session.items()
    }
    requested = sum(len(codes_by_session.get(session, ())) for session in sessions)
    available = unknown = blocked = 0
    for session in sessions:
        for code in codes_by_session.get(session, ()):
            decision_time = PIT.bar_available_at(session)
            if repository is None or decision_time is None:
                unknown += 1
                continue
            evidence = repository.evidence_at(code, session, decision_time)
            decision = TA.tradability_at(
                code, session, decision_time=decision_time, repository=repository,
            )
            if evidence is None or not decision.evidence_present:
                unknown += 1
                continue
            if any(getattr(evidence, name, None) is None for name in (
                "is_listed", "is_st", "is_suspended", "has_market_quote",
                "has_trade_volume", "is_price_limit_locked",
            )) or (evidence.is_price_limit_locked is True and not evidence.price_limit_direction):
                unknown += 1
                continue
            if decision.buy_block_reason == TA.TradabilityReason.UNKNOWN_STATE or \
                    decision.sell_block_reason == TA.TradabilityReason.UNKNOWN_STATE:
                unknown += 1
            elif not decision.can_buy and not decision.can_sell:
                blocked += 1
            else:
                available += 1
    complete = (
        universe_complete and requested > 0
        and available + blocked + unknown == requested and unknown == 0
    )
    ratio = _ratio(available + blocked, requested) if universe_complete else None
    identity = {"tradability_fingerprint": spec.tradability_fingerprint}
    detail = _dimension("proven" if complete else "blocked",
                        None if complete else "tradability_coverage_incomplete", identity,
                        "archive_facts_complete_for_requested_pairs" if complete else "partial_or_unknown",
                        {"requested": requested, "available": available,
                         "unknown": unknown, "blocked": blocked, "ratio": ratio})
    return detail, {"requested": requested, "available": available,
                    "unknown": unknown, "blocked": blocked, "ratio": ratio,
                    "universe_sessions_unknown": len(sessions) - len(members_by_session)}


def _session_text(value: Any) -> str | None:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.date().isoformat() if isinstance(value, dt.datetime) else value.isoformat()
    text = str(value or "").strip().replace("/", "-")
    try:
        if len(text) == 10:
            return dt.date.fromisoformat(text).isoformat()
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _bounded_sessions(
    sessions: Any, *, start: str, end: str,
) -> tuple[list[str], int, int, int, int]:
    raw = list(sessions or ())
    bounded = set()
    invalid = outside_range = 0
    for item in raw:
        day = _session_text(item)
        if day is None:
            invalid += 1
        elif day < start or day > end:
            outside_range += 1
        else:
            bounded.add(day)
    unique = sorted(bounded)
    duplicates = len(raw) - outside_range - invalid - len(unique)
    return unique, len(raw), outside_range, invalid, duplicates


def _session_calendar_provenance(
    spec: EC.ExperimentSpec,
    sessions: Sequence[str],
    provenance: Any,
    *,
    requested: int,
    outside_range: int,
    invalid: int,
    duplicates: int,
) -> tuple[bool, dict]:
    """Report calendar claims without treating caller declarations as authority.

    There is no connected owner that can issue a typed, complete historical
    session-calendar projection yet. Until one exists, this dimension stays
    blocked regardless of caller-provided metadata.
    """
    source = provenance if isinstance(provenance, Mapping) else {}
    declared_count = source.get("session_count")
    complete = False
    report = {
        "status": "blocked",
        "reason_code": "walk_forward_session_calendar_unproven",
        "authority": "historical_session_calendar_owner_unavailable",
        "caller_claim_supplied": bool(source),
        "kind": source.get("kind"),
        "source": source.get("source"),
        "coverage_start": _session_text(source.get("coverage_start")),
        "coverage_end": _session_text(source.get("coverage_end")),
        "range_complete_claimed": source.get("range_complete") is True,
        "sessions_declared": declared_count if isinstance(declared_count, int) else None,
        "sessions_supplied": len(sessions),
        "sessions_requested": requested,
        "excluded_outside_experiment_range": outside_range,
        "invalid": invalid,
        "duplicates": duplicates,
        "ratio": 1.0 if complete else None,
    }
    return complete, report


def _fundamental(
    spec: EC.ExperimentSpec, records: Any, samples: Sequence[WFV.ValidationSample],
) -> tuple[dict, dict]:
    items = list(records or ())
    samples_by_key: dict[str, WFV.ValidationSample | None] = {}
    for sample in samples:
        key = str(sample.sample_key or "").strip()
        if key in samples_by_key:
            samples_by_key[key] = None
        else:
            samples_by_key[key] = sample
    total = 0
    counts = {"visible": 0, "future": 0, "publication_unproven": 0, "invalid": 0}
    for item in items:
        if not isinstance(item, Mapping):
            total += 1
            counts["invalid"] += 1
            continue
        record = item.get("record")
        sample_keys = item.get("sample_keys")
        if (not isinstance(record, Mapping) or not isinstance(sample_keys, (list, tuple))
                or not sample_keys or any(not isinstance(key, str) or not key.strip()
                                          for key in sample_keys)
                or len(set(sample_keys)) != len(sample_keys)):
            total += 1
            counts["invalid"] += 1
            continue
        for sample_key in sample_keys:
            total += 1
            sample = samples_by_key.get(sample_key)
            if sample is None:
                counts["invalid"] += 1
                continue
            decision_asof = _decision_instant(sample.decision_at)
            if decision_asof is None:
                counts["publication_unproven"] += 1
                continue
            view = FPIT.financial_visibility(record, decision_asof)
            source = view.get("profit_source")
            publication_declared = any(
                record.get(key) not in (None, "") for key in FPIT.REPORT_PUBLISHED_KEYS
            )
            if publication_declared and view.get("report_published_at") is None:
                counts["invalid"] += 1
            elif source == "future":
                counts["future"] += 1
            elif view.get("visible") and source == "reported":
                counts["visible"] += 1
            elif source in {"shadow", "unknown"}:
                counts["publication_unproven"] += 1
            else:
                counts["invalid"] += 1
    complete = total > 0 and counts["visible"] == total
    identity = {"dataset_fingerprint": spec.dataset_fingerprint}
    coverage = {**counts, "requested": total,
                "ratio": _ratio(counts["visible"], total)}
    detail = _dimension("proven" if complete else "blocked",
                        None if complete else "fundamental_publication_unproven", identity,
                        "publication_visible_at_each_linked_decision_session"
                        if complete else "sample_link_or_publication_unproven",
                        coverage)
    return detail, coverage


def _decision_instant(value: Any) -> str | None:
    """Return an exact timezone-aware decision instant; dates cannot prove PIT."""
    if not isinstance(value, str) or "T" not in value:
        return None
    text = value.strip()
    parsed = PIT.parse_asof(text)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")


def _normalize_samples(
    samples: Sequence[Any], *, start: str, end: str,
) -> tuple[list[WFV.ValidationSample], int, int]:
    normalized = []
    for row in samples or ():
        if isinstance(row, WFV.ValidationSample):
            normalized.append(row)
        elif isinstance(row, LD.CanonicalSample):
            normalized.append(WFV.ValidationSample(
                sample_key=row.sample_key, code=row.code,
                decision_session=row.feature_asof,
                decision_at=row.feature_available_at,
                label_available_at=row.label_available_at or "",
                target=row.target, horizon=row.horizon,
                label_version=row.horizon_semantics,
                features=dict(row.features or {}), pit_status=row.pit_status,
                provenance=dict(row.provenance or {}),
            ))
        else:
            raise ValueError("samples must be canonical learning or walk-forward samples")
    bounded = []
    outside_range = invalid_dates = 0
    for sample in normalized:
        day = _session_text(sample.decision_session)
        if day is None:
            invalid_dates += 1
            continue
        if day < start or day > end:
            outside_range += 1
            continue
        bounded.append(sample)
    return bounded, outside_range, invalid_dates


def _fold_projection(fold: Any) -> dict:
    metadata = dict(fold.metadata or {})
    return {
        "fold_id": fold.fold_id,
        "status": fold.status,
        "reason": fold.reason,
        "train_period": {"start": metadata.get("train_decision_start"),
                         "end": metadata.get("train_decision_end")},
        "validation_period": {"start": metadata.get("validation_decision_start"),
                              "end": metadata.get("validation_decision_end")},
        "oos_period": {"start": metadata.get("test_decision_start"),
                       "end": metadata.get("test_decision_end")},
        "train_rows": metadata.get("train_final_rows", 0),
        "validation_rows": metadata.get("validation_rows", 0),
        "oos_rows": metadata.get("test_rows", 0),
    }


def build_pit_validation_evidence(
    spec: EC.ExperimentSpec,
    *,
    strategy_version: Any = None,
    dataset_manifest: Any = None,
    universe_rows: Any = None,
    universe_source: Any = None,
    tradability_repository: Any = None,
    fundamental_records: Any = None,
    market_snapshot: Any = None,
    samples: Sequence[Any] = (),
    walk_forward_config: WFV.WalkForwardConfig | None = None,
    authoritative_sessions: Sequence[Any] | None = None,
    session_calendar_provenance: Mapping[str, Any] | None = None,
) -> PITValidationEvidence:
    """Compose explicit owner evidence into a READY/BLOCKED input gate.

    `market_snapshot` is accepted only for diagnostics and never proves arbitrary
    historical coverage. This function performs no persistence or network I/O.
    """
    if not isinstance(spec, EC.ExperimentSpec):
        raise ValueError("spec must be an ExperimentSpec")
    cutoff = spec.asof_policy.get("cutoff")
    asof_valid = PIT.parse_asof(cutoff) is not None
    if not asof_valid:
        # Invalid explicit evaluation time is a contract error, never live mode.
        raise ValueError("ExperimentSpec evaluation as-of cutoff is invalid")

    dimensions: dict[str, dict] = {}
    reasons: list[str] = []
    warnings: list[str] = []

    bounded_sessions, _sessions_requested, sessions_excluded, sessions_invalid, sessions_duplicates = _bounded_sessions(
        authoritative_sessions, start=spec.start_date, end=spec.end_date,
    )
    session_calendar_complete, session_calendar_coverage = _session_calendar_provenance(
        spec, bounded_sessions, session_calendar_provenance,
        requested=_sessions_requested, outside_range=sessions_excluded,
        invalid=sessions_invalid, duplicates=sessions_duplicates,
    )
    normalized_samples, samples_excluded, samples_invalid = _normalize_samples(
        samples, start=spec.start_date, end=spec.end_date,
    )
    universe_dim, members, _universe_report = _universe(
        spec, universe_rows, universe_source, bounded_sessions,
        session_calendar_complete=session_calendar_complete,
    )
    dimensions["historical_universe"] = universe_dim
    if universe_dim["status"] == "blocked":
        reasons.append(universe_dim["reason_code"])
        warnings.append("historical_universe_unproven")

    tradability_dim, tradability_coverage = _tradability(
        spec, members, bounded_sessions, tradability_repository,
        universe_complete=universe_dim["status"] == "proven",
    )
    dimensions["historical_tradability"] = tradability_dim
    if tradability_dim["status"] == "blocked":
        reasons.append(tradability_dim["reason_code"])
        warnings.append("tradability_coverage_incomplete")

    # A current full-market snapshot has no authority over arbitrary historical dates.
    _ = market_snapshot
    dimensions["market_data_pit"] = _dimension(
        "blocked", "historical_market_data_unavailable",
        {"market_data_fingerprint": spec.market_data_fingerprint},
        "declared_identity_only",
    )
    reasons.append("historical_market_data_unavailable")
    warnings.append("historical_market_data_unavailable")

    fundamental_dim, fundamental_coverage = _fundamental(
        spec, fundamental_records, normalized_samples,
    )
    dimensions["fundamental_pit"] = fundamental_dim
    if fundamental_dim["status"] == "blocked":
        reasons.append(fundamental_dim["reason_code"])
        warnings.append("fundamental_publication_unproven")

    dimensions["strategy_version"] = _strategy_identity(spec, strategy_version)
    if dimensions["strategy_version"]["status"] == "blocked":
        reasons.append(dimensions["strategy_version"]["reason_code"])

    dimensions["dataset"] = _manifest_dimension(spec, dataset_manifest)
    if dimensions["dataset"]["status"] == "blocked":
        reasons.append(dimensions["dataset"]["reason_code"])

    execution_identity = _thaw(spec.execution_assumptions)
    execution_complete = all(execution_identity.get(key) is not None for key in (
        "execution_profile_version", "fill_assumptions", "t_plus_one_semantics",
        "price_limit_semantics", "partial_fill_semantics", "capacity_assumptions",
    ))
    dimensions["execution_model"] = _dimension(
        "proven" if execution_complete else "blocked",
        None if execution_complete else "execution_assumptions_unproven",
        execution_identity, "experiment_spec_declared",
    )
    if not execution_complete:
        reasons.append("execution_assumptions_unproven")

    cost_identity = _thaw(spec.cost_model)
    # ExperimentSpec enforces these fields and freezes them; the gate reads no runtime globals.
    dimensions["cost_model"] = _dimension(
        "proven", None, cost_identity, "experiment_spec_declared",
    )

    walk: dict[str, Any] = {
        "timeline_source": None, "ready_folds": 0, "folds": 0,
        "evaluation_asof": cutoff, "config": None, "windows": [],
        "label_coverage": {"available": 0, "requested": 0, "ratio": None},
        "session_calendar": session_calendar_coverage,
        "sessions": {
            "requested": _sessions_requested,
            "used": len(bounded_sessions),
            "excluded_outside_experiment_range": sessions_excluded,
            "invalid": sessions_invalid,
        },
    }
    if not authoritative_sessions or not bounded_sessions or sessions_invalid:
        dimensions["walk_forward"] = _dimension(
            "blocked", "walk_forward_explicit_sessions_required", None,
            "authoritative_sessions_missing_or_invalid",
        )
        reasons.append("walk_forward_explicit_sessions_required")
    elif not isinstance(walk_forward_config, WFV.WalkForwardConfig):
        dimensions["walk_forward"] = _dimension(
            "blocked", "walk_forward_unavailable", None, "walk_forward_config_missing",
        )
        reasons.append("walk_forward_unavailable")
    else:
        result = WFV.build_walk_forward_folds(
            normalized_samples, walk_forward_config, sessions=bounded_sessions, asof=cutoff,
        )
        report = result["report"]
        folds = list(result.get("folds") or ())
        labels_available = sum(
            1 for sample in normalized_samples
            if sample.pit_status == WFV.PIT_VERIFIED
            and WFV.label_ready_by_asof(sample, asof=cutoff)
        )
        walk = {
            "timeline_source": report.get("timeline_source"),
            "ready_folds": int(report.get("ready_folds") or 0),
            "folds": int(report.get("folds") or 0),
            "evaluation_asof": cutoff,
            "config": dict(report.get("config") or {}),
            "windows": [_fold_projection(fold) for fold in folds],
            "label_coverage": {
                "available": labels_available,
                "requested": len(normalized_samples),
                "ratio": _ratio(labels_available, len(normalized_samples)),
                "samples_excluded_outside_experiment_range": samples_excluded,
                "samples_with_invalid_session": samples_invalid,
            },
            "sessions": {
                "requested": _sessions_requested,
                "used": len(bounded_sessions),
                "excluded_outside_experiment_range": sessions_excluded,
                "invalid": sessions_invalid,
            },
        }
        if any(fold.reason == WFV.REASON_WINDOW_NOT_MATURED for fold in folds):
            warnings.append("walk_forward_window_not_matured")
        ready = (session_calendar_complete
                 and walk["timeline_source"] == "explicit_sessions"
                 and walk["ready_folds"] > 0 and samples_invalid == 0)
        reason = None if ready else (
            "walk_forward_session_calendar_unproven" if not session_calendar_complete
            else "walk_forward_sample_session_invalid" if samples_invalid
            else "walk_forward_window_not_matured"
            if any(fold.reason == WFV.REASON_WINDOW_NOT_MATURED for fold in folds)
            else "walk_forward_ready_fold_missing"
        )
        dimensions["walk_forward"] = _dimension(
            "proven" if ready else "blocked", reason,
            {"start_date": spec.start_date, "end_date": spec.end_date,
             "asof": cutoff},
            "existing_validator_explicit_sessions" if ready else "existing_validator_blocked",
            {"ready_folds": walk["ready_folds"], "folds": walk["folds"]},
        )
        if reason:
            reasons.append(reason)
            warnings.append(reason)

    # Preserve stable dimension order and stable reason order.
    dimensions = {name: dimensions[name] for name in _DIMENSIONS}
    reasons = sorted(set(reasons))
    windows = tuple(walk.get("windows") or ())
    first = next((window for window in windows if window.get("status") == WFV.STATUS_READY), None)
    train_period = first.get("train_period") if first else None
    validation_period = first.get("validation_period") if first else None
    oos_period = first.get("oos_period") if first else None
    coverage = {
        "universe": universe_dim["coverage"],
        "session_calendar": session_calendar_coverage,
        "tradability": tradability_coverage,
        "market_data": None,
        "fundamental": fundamental_coverage,
        "labels": walk.get("label_coverage"),
    }
    all_proven = all(dimensions[name]["status"] in {"proven", "not_applicable"}
                     for name in _DIMENSIONS)
    ready = all_proven and walk.get("ready_folds", 0) >= 1 and not reasons
    return PITValidationEvidence(
        experiment_fingerprint=spec.fingerprint,
        status="ready" if ready else "blocked",
        reason_codes=tuple(reasons or ([] if ready else ["walk_forward_ready_fold_missing"])),
        dimensions=dimensions, walk_forward=walk, data_coverage=coverage,
        pit_warnings=tuple(warnings), train_period=train_period,
        validation_period=validation_period, oos_period=oos_period,
        windows=windows,
    )


__all__ = ["PITValidationEvidence", "build_pit_validation_evidence"]
