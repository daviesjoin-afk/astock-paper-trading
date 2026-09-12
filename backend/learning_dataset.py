# -*- coding: utf-8 -*-
"""Point-in-time reproducible dataset foundation for research consumers.

This module turns **already-persisted evidence** into a strict, reproducible
research dataset:

    raw evidence
        -> PIT eligibility
        -> canonical learning samples
        -> mature future labels
        -> leakage-safe chronological split
        -> immutable manifest + fingerprint
        -> offline/shadow research consumers

It is deliberately a small, pure research helper.  It never submits orders,
never mutates positions, never touches risk limits, never promotes challengers
and never calls an external provider (no market snapshot, no kline, no LLM).
Its only input is evidence that the runtime already persisted, so rebuilding
the same historical dataset on a different day cannot change the result.

Fail-closed rules are *borrowed* from :mod:`financial_point_in_time` rather
than re-implemented (see :func:`financial_availability`): a report/event
*period* is never treated as a *publication* timestamp, and unprovable
availability is never upgraded to "available", never clamped, never
back-dated, and never replaced with ``0``.

Contracts enforced here:

    feature_asof <= sample cutoff
    feature_available_at <= sample cutoff
    feature_asof < label end date <= sample cutoff
    label end date <= label availability <= sample cutoff
    label availability is independently proven (label pit_status = verified)
    conflicting labels for one logical sample identity fail closed
    future feature != available feature
    future label != matured label
    unknown availability != available
    missing != 0
    stale != current
    shadow != production
    quality != alpha
    row count != scientific readiness
    observed step != proven exchange trading day
    training data != execution authority
    a date-only cutoff means end of that exchange-local (UTC+08:00) day
    a naive availability is exchange-local, never the host machine timezone
    every PIT comparison runs on one canonical UTC instant clock
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import financial_point_in_time as PIT


CONTRACT_VERSION = "learning-dataset-v1"
FINGERPRINT_VERSION = "sha256-canonical-v1"
MANIFEST_SCHEMA_VERSION = "learning-manifest-v1"
DATASET_KIND_ADAPTIVE_ALPHA = "adaptive_alpha"

# Fixed, versioned default split.  Never bound to a UI value; a caller may pass
# an explicit spec, but the default is part of the reproducibility contract.
DEFAULT_SPLIT_SPEC = {"train": 0.60, "validation": 0.20, "test": 0.20}
PARTITIONS = ("train", "validation", "test")

# ``adaptive_alpha_returns.horizon`` counts captured profile-date steps, not
# certified exchange sessions.  Claiming otherwise would be a false statement.
HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS = "observed_profile_steps"
HORIZON_SEMANTICS_OBSERVED_CLOSE_STEPS = "observed_close_steps"
SUPPORTED_HORIZON_SEMANTICS = (
    HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
    HORIZON_SEMANTICS_OBSERVED_CLOSE_STEPS,
)

# Only these statuses may enter the *strict* dataset.
PIT_VERIFIED = "verified"
PIT_UNPROVEN = "unproven"
PIT_LEGACY_UNPROVEN = "legacy_unproven"
PIT_UNKNOWN = "unknown"
PIT_FUTURE = "future"
STRICT_PIT_STATUSES = frozenset({PIT_VERIFIED})
KNOWN_PIT_STATUSES = (
    PIT_VERIFIED,
    PIT_UNPROVEN,
    PIT_LEGACY_UNPROVEN,
    PIT_UNKNOWN,
    PIT_FUTURE,
)

EXCLUSION_REASONS = (
    "missing_feature",
    "non_finite_feature",
    "unknown_feature_availability",
    "future_feature",
    "legacy_unproven_pit",
    "missing_label",
    "immature_label",
    "invalid_label_time",
    "future_label",
    "unproven_label_pit",
    "future_or_invalid_asof",
    "duplicate_sample",
    "ambiguous_label",
    "unsupported_horizon_semantics",
    "overlapping_label_purged",
)

DEFAULT_ALPHA_FEATURES = (
    "price_momentum",
    "main_flow",
    "turnover",
    "volume_ratio",
    "small_size",
    "value",
)

# Stable, unique, deterministic row order.  Two datasets holding the same
# logical rows in a different SQLite insertion order must sort identically.
STABLE_ROW_ORDER = ("feature_asof", "code", "horizon", "source", "sample_key")

ALPHA_SAMPLE_TABLE = "adaptive_alpha_samples"
ALPHA_RETURN_TABLE = "adaptive_alpha_returns"
MANIFEST_TABLE = "learning_dataset_manifests"

# Provenance columns added to the existing adaptive-alpha schema.  Additive and
# nullable only: old rows keep their facts and are marked, never rewritten.
SAMPLE_PROVENANCE_COLUMNS = (
    ("feature_asof", "TEXT"),
    ("feature_available_at", "TEXT"),
    ("pit_status", "TEXT"),
    ("source", "TEXT"),
    ("source_version", "TEXT"),
    ("contract_version", "TEXT"),
    ("provenance_json", "TEXT"),
    ("quality_json", "TEXT"),
)
RETURN_PROVENANCE_COLUMNS = (
    ("label_available_at", "TEXT"),
    ("horizon_semantics", "TEXT"),
    ("pit_status", "TEXT"),
    ("source", "TEXT"),
    ("source_version", "TEXT"),
    ("contract_version", "TEXT"),
    ("provenance_json", "TEXT"),
)

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_END_OF_DAY = "T23:59:59"
_START_OF_DAY = "T00:00:00"

# ── PIT instant contract ──────────────────────────────────────────────────
# Every availability / cutoff comparison happens on ONE canonical clock: UTC.
# A-share evidence is exchange-local, so a date-only cutoff denotes the end of
# that calendar day in Asia/Shanghai (UTC+08:00) -- *not* the end of day UTC.
# A naive stored timestamp has no offset of its own; per the PR-8 provenance
# contract it is exchange-local, and it is never read in the host machine's
# timezone (which would make the dataset depend on where it was rebuilt).
_EXCHANGE_TZ = _dt.timezone(_dt.timedelta(hours=8), "UTC+08:00")
_UTC = _dt.timezone.utc
_AVAILABILITY_CLOCK = "canonical_utc"

# Modules that must never appear in this module's namespace.  Enforced by
# :func:`forbidden_dependencies` and asserted in the dependency guard test.
_FORBIDDEN_DEPENDENCY_PREFIXES = (
    "adaptive_engine",
    "adaptive_learning_dispatch",
    "adaptive_learning_worker",
    "adaptive_risk",
    "adaptive_selection",
    "adaptive_shadow_risk",
    "asymmetric_risk",
    "paper_trading",
    "paper_cycle_service",
    "paper_repository",
    "execution",
    "order_intent",
    "strategy_registry",
    "risk_center",
    "deepseek",
    "ai_analysis",
    "news_learning",
    "requests",
    "urllib",
    "http",
    "socket",
    "httpx",
    "aiohttp",
    "market_data",
    "alt_data",
)


# ────────────────────────────── small helpers ──────────────────────────────


def _blank(value: Any) -> bool:
    """True only for genuinely absent values -- not for NaN/Infinity.

    Keeping these separate matters: ``missing`` and ``non-finite`` are
    different audit reasons, and neither may be repaired into ``0``.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"none", "null", "nat", "-"}
    return False


def _text(value: Any) -> Optional[str]:
    """Return a trimmed string, or ``None`` for any missing/blank value."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "-"}:
        return None
    return text


def _date_text(value: Any) -> Optional[str]:
    """Extract a ``YYYY-MM-DD`` date without ever inventing one.

    A longer timestamp is truncated to its date part only when that prefix is
    unambiguous; anything unparseable returns ``None`` so callers fail closed
    instead of guessing midnight.
    """
    text = _text(value)
    if text is None:
        return None
    candidate = text[:10].replace("/", "-")
    if not _DATE_ONLY.match(candidate):
        return None
    try:
        _dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _canonical_instant(value: _dt.datetime) -> str:
    """Return ``YYYY-MM-DDTHH:MM:SS+00:00`` on the canonical PIT clock.

    A naive input carries no offset, so it is read as *exchange-local*
    (Asia/Shanghai) rather than the host's timezone.  The output always keeps
    an explicit ``+00:00`` so the function is idempotent: feeding a canonical
    value back in cannot shift it a second time.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_EXCHANGE_TZ)
    return value.astimezone(_UTC).isoformat(timespec="seconds")


def _exchange_day_edge(day: str, edge: str) -> Optional[str]:
    """Canonical UTC instant for an edge of an exchange-local calendar day."""
    try:
        naive = _dt.datetime.fromisoformat(day + edge)
    except ValueError:
        return None
    return _canonical_instant(naive)


def _timestamp_text(value: Any) -> Optional[str]:
    """Normalize a stored availability timestamp onto the canonical UTC clock.

    Never synthesizes a timestamp and never drops a real offset: a tz-aware
    input is converted to UTC, a naive input is read as exchange-local, and an
    unparseable input returns ``None`` so callers fail closed.
    """
    text = _text(value)
    if text is None:
        return None
    if _DATE_ONLY.match(text):
        # Day-precise evidence keeps its day; _instant() picks the edge.
        return text
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = _dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    return _canonical_instant(parsed)


def _instant(value: Any) -> Optional[str]:
    """Canonical UTC instant.  A date-only value means day granularity.

    Day granularity is encoded as the *start* of the exchange-local day: the
    source proved only the day, so we never claim intraday evidence it never
    gave us.
    """
    text = _text(value)
    if text is None:
        return None
    if len(text) == 10 and _DATE_ONLY.match(text):
        day = _date_text(text)
        return None if day is None else _exchange_day_edge(day, _START_OF_DAY)
    return _timestamp_text(text)


def _cutoff_instant(value: Any) -> Optional[str]:
    """Cutoff instant on the canonical UTC clock.

    A date-only cutoff denotes the end of that calendar day in the exchange's
    own timezone (UTC+08:00), not the end of the UTC day.
    """
    text = _text(value)
    if text is None:
        return None
    if len(text) == 10 and _DATE_ONLY.match(text):
        day = _date_text(text)
        return None if day is None else _exchange_day_edge(day, _END_OF_DAY)
    return _timestamp_text(text)


def _finite(value: Any) -> Optional[float]:
    """Return a finite float, or ``None``.  Never coerces missing to ``0``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"nan", "none", "null", "-", "inf", "-inf", "+inf"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) else None


def _canon_number(value: Any) -> Optional[str]:
    """Versioned numeric normalization for canonical serialization.

    Numbers become a fixed-precision decimal string -- never ``repr`` of a
    Python object.  ``-0.0`` collapses to ``0.0`` so a sign flip cannot fork
    the fingerprint.  Non-finite values are rejected upstream; this raises if
    one slips through, because silently normalizing NaN would be its own leak.
    """
    number = _finite(value)
    if number is None:
        return None
    if number == 0.0:
        number = 0.0
    return format(number, ".12g")


def _canon_json(value: Any) -> Any:
    """Canonical, deterministic JSON shape: sorted keys, explicit nulls."""
    if isinstance(value, Mapping):
        return {str(key): _canon_json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canon_json(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return _canon_number(value)
    return str(value)


def _loads(value: Any, default: Optional[Mapping] = None) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    text = _text(value)
    if text is None:
        return dict(default or {})
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return dict(default or {})
    return dict(parsed) if isinstance(parsed, Mapping) else dict(default or {})


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _rows_as_dicts(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Row-factory independent read, so a bare connection works too."""
    try:
        cursor = conn.execute(sql, args)
    except sqlite3.Error:
        return []
    names = [str(description[0]) for description in cursor.description or ()]
    return [dict(zip(names, row, strict=False)) for row in cursor.fetchall()]


def _row_as_dict(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Optional[dict]:
    rows = _rows_as_dicts(conn, sql, args)
    return rows[0] if rows else None


def forbidden_dependencies() -> list:
    """Return forbidden modules present in this module's namespace.

    Backs the source-dependency guard: the dataset builder must not be able to
    reach an execution, order-mutation or network path.
    """
    present = []
    for name, value in globals().items():
        if name.startswith("_"):
            continue
        module_name = getattr(value, "__name__", None)
        if not isinstance(module_name, str):
            continue
        root = module_name.split(".")[0]
        if any(root.startswith(prefix) for prefix in _FORBIDDEN_DEPENDENCY_PREFIXES):
            present.append(module_name)
    return sorted(set(present))


def normalize_availability(value: Any) -> Optional[str]:
    """Public wrapper used by capture code to normalize a *proven* timestamp.

    Returns ``None`` for anything unparseable, so a capture path that cannot
    prove availability writes an explicit "unproven" state instead of a guess.
    """
    return _timestamp_text(value)


def capture_provenance(
    *,
    available_at: Any,
    source: Any = None,
    source_version: Any = None,
    contract_version: str = CONTRACT_VERSION,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Build the provenance column set for a newly captured sample or label.

    ``pit_status`` is ``verified`` only when a real, parseable availability
    timestamp exists.  There is no branch that manufactures one.
    """
    normalized = _timestamp_text(available_at)
    provenance = dict(extra or {})
    provenance["capture_contract_version"] = contract_version
    provenance["availability_clock"] = _AVAILABILITY_CLOCK
    return {
        "feature_available_at": normalized,
        "pit_status": PIT_VERIFIED if normalized else PIT_LEGACY_UNPROVEN,
        "source": _text(source) or DATASET_KIND_ADAPTIVE_ALPHA,
        "source_version": _text(source_version) or contract_version,
        "contract_version": contract_version,
        "provenance_json": json.dumps(
            _canon_json(provenance), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }


# ──────────────────────────── canonical contract ────────────────────────────


@dataclass(frozen=True, slots=True)
class CanonicalSample:
    """One leakage-safe learning sample under the versioned data contract."""

    sample_key: str
    source: str
    source_version: str
    code: str
    strategy_id: str
    model_family: str
    feature_asof: str
    feature_available_at: Optional[str]
    label_start_date: str
    label_end_date: str
    label_available_at: Optional[str]
    horizon: int
    horizon_semantics: str
    features: Mapping[str, Optional[float]]
    target: float
    pit_status: str
    quality_flags: tuple = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    partition: Optional[str] = None

    def with_partition(self, partition: Optional[str]) -> "CanonicalSample":
        return _replace_partition(self, partition)

    def canonical(self, feature_names: Sequence[str]) -> dict:
        """Return the canonical dict consumed by the fingerprint."""
        return {
            "sample_key": self.sample_key,
            "source": self.source,
            "source_version": self.source_version,
            "code": self.code,
            "strategy_id": self.strategy_id,
            "model_family": self.model_family,
            "feature_asof": self.feature_asof,
            "feature_available_at": self.feature_available_at,
            "label_start_date": self.label_start_date,
            "label_end_date": self.label_end_date,
            "label_available_at": self.label_available_at,
            "horizon": int(self.horizon),
            "horizon_semantics": self.horizon_semantics,
            "features": {
                str(name): _canon_number((self.features or {}).get(name))
                for name in sorted(str(name) for name in feature_names)
            },
            "target": _canon_number(self.target),
            "pit_status": self.pit_status,
            "quality_flags": sorted(str(flag) for flag in (self.quality_flags or ())),
            "provenance": _canon_json(dict(self.provenance or {})),
            "partition": self.partition,
        }


def _replace_partition(sample: CanonicalSample, partition: Optional[str]) -> CanonicalSample:
    return CanonicalSample(
        sample_key=sample.sample_key,
        source=sample.source,
        source_version=sample.source_version,
        code=sample.code,
        strategy_id=sample.strategy_id,
        model_family=sample.model_family,
        feature_asof=sample.feature_asof,
        feature_available_at=sample.feature_available_at,
        label_start_date=sample.label_start_date,
        label_end_date=sample.label_end_date,
        label_available_at=sample.label_available_at,
        horizon=sample.horizon,
        horizon_semantics=sample.horizon_semantics,
        features=dict(sample.features or {}),
        target=sample.target,
        pit_status=sample.pit_status,
        quality_flags=tuple(sample.quality_flags or ()),
        provenance=dict(sample.provenance or {}),
        partition=partition,
    )


def sample_key(source: str, feature_asof: str, code: str, horizon: int) -> str:
    """Stable identity for one sample; never derived from row position."""
    digest = hashlib.sha256(
        f"{source}|{feature_asof}|{code}|{int(horizon)}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


def _canonical_sort_key(row: Mapping[str, Any]) -> tuple:
    return tuple(str(row.get(name)) for name in STABLE_ROW_ORDER)


def _sample_sort_key(sample: CanonicalSample) -> tuple:
    return (
        str(sample.feature_asof),
        str(sample.code),
        str(sample.horizon),
        str(sample.source),
        str(sample.sample_key),
    )


# ───────────────────────── PIT availability (reuse) ─────────────────────────


def financial_availability(record: Mapping[str, Any], cutoff: Any) -> tuple:
    """Adapter over :mod:`financial_point_in_time`; not a second PIT engine.

    Returns ``(available_at, pit_status)``.  An unknown publication timestamp
    can never become ``verified`` here, no matter how plausible the guess.
    """
    view = PIT.financial_visibility(record, cutoff)
    source = view.get("profit_source")
    published = view.get("report_published_at")
    if source == "future":
        return published, PIT_FUTURE
    if source == "reported" and view.get("visible"):
        return published, PIT_VERIFIED
    # ``shadow`` / ``unknown``: the endpoint cannot prove when this was visible.
    return published, PIT_UNPROVEN


# ──────────────────────────────── schema ────────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """Idempotently add the manifest table and provenance columns.

    Safe on an empty database, an old database, and a test database that only
    declares a couple of columns.  Additive only: no column is dropped,
    renamed or rewritten, and no historical value is fabricated.  Legacy rows
    that cannot prove availability are marked ``legacy_unproven`` so a strict
    dataset excludes them instead of silently trusting them.
    """
    report = {"added_columns": 0, "manifest_table": False, "legacy_marked": 0}

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MANIFEST_TABLE}(
            dataset_fingerprint TEXT PRIMARY KEY,
            manifest_schema_version TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            dataset_kind TEXT NOT NULL,
            source TEXT NOT NULL,
            cutoff TEXT,
            feature_names TEXT NOT NULL,
            label_spec TEXT NOT NULL,
            horizon_semantics TEXT NOT NULL,
            split_spec TEXT NOT NULL,
            source_row_count INTEGER NOT NULL,
            eligible_row_count INTEGER NOT NULL,
            excluded_row_count INTEGER NOT NULL,
            exclusion_reasons TEXT NOT NULL,
            partition_rows TEXT NOT NULL,
            min_feature_date TEXT,
            max_feature_date TEXT,
            min_label_end_date TEXT,
            max_label_end_date TEXT,
            source_digest TEXT NOT NULL,
            code_build_identity TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    report["manifest_table"] = True

    for table, definitions in (
        (ALPHA_SAMPLE_TABLE, SAMPLE_PROVENANCE_COLUMNS),
        (ALPHA_RETURN_TABLE, RETURN_PROVENANCE_COLUMNS),
    ):
        columns = _table_columns(conn, table)
        if not columns:
            continue  # table not present in this database yet
        for name, sql_type in definitions:
            if name in columns:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            report["added_columns"] += 1

    # Mark, never invent: a legacy row with no availability evidence becomes
    # explicitly unproven instead of defaulting to "trusted".
    report["legacy_marked"] = _mark_legacy_rows(conn, ALPHA_SAMPLE_TABLE)
    _mark_legacy_rows(conn, ALPHA_RETURN_TABLE)
    return report


def _mark_legacy_rows(conn: sqlite3.Connection, table: str) -> int:
    if "pit_status" not in _table_columns(conn, table):
        return 0
    try:
        pending = conn.execute(f"SELECT 1 FROM {table} WHERE pit_status IS NULL LIMIT 1").fetchone()
        if not pending:
            return 0
        cursor = conn.execute(
            f"UPDATE {table} SET pit_status=? WHERE pit_status IS NULL",
            (PIT_LEGACY_UNPROVEN,),
        )
        return int(cursor.rowcount or 0)
    except sqlite3.Error:
        return 0


# ─────────────────────────────── evidence read ───────────────────────────────


def _select(columns: set, name: str, alias: str, table_alias: str) -> str:
    """Qualified projection.  Table-qualifying is mandatory here: both alpha
    tables expose a ``code`` column, so a bare reference is an ambiguity error
    in SQLite."""
    if name in columns:
        return f"{table_alias}.{name} AS {alias}"
    return f"NULL AS {alias}"


_SAMPLE_FIELDS = (
    "profile_date",
    "code",
    "industry",
    "close_price",
    "regime",
    "feature_asof",
    "feature_available_at",
    "pit_status",
    "source",
    "source_version",
    "contract_version",
    "provenance_json",
    "quality_json",
) + DEFAULT_ALPHA_FEATURES

_RETURN_FIELDS = (
    "start_date",
    "end_date",
    "horizon",
    "code",
    "forward_return_pct",
    "label_available_at",
    "horizon_semantics",
    "pit_status",
    "source",
    "source_version",
    "contract_version",
    "provenance_json",
)


def _evidence_order_clause(sample_columns: set, return_columns: set) -> str:
    """Fully deterministic bounded-read order.

    Ambiguity is refused regardless, but a *bounded* read still has to be
    stable: a nondeterministic window would hand two runs of the same database
    a different slice of rows and quietly break reproducibility.  Only columns
    that actually exist are referenced, so a partial legacy/test database does
    not raise here.
    """
    fields = []
    if "profile_date" in sample_columns:
        fields.append("s.profile_date")
    if "code" in sample_columns:
        fields.append("s.code")
    for name in ("horizon", "end_date", "label_available_at", "forward_return_pct"):
        if name in return_columns:
            fields.append(f"r.{name}")
    return ", ".join(fields) if fields else "1"


def _read_alpha_evidence_page(
    conn: sqlite3.Connection, max_rows: Optional[int] = None
) -> tuple:
    """Read evidence with an explicit truncation verdict.

    Persistence only -- never a provider call.  Reads ``max_rows + 1`` rows so a
    dataset that is *exactly* ``max_rows`` long is not mistaken for a truncated
    one; the returned list still never exceeds ``max_rows``.  Returns
    ``(evidence, truncated)``.

    ``LEFT JOIN`` on purpose: a sample whose forward label has not been
    recorded yet must still be *audited* as excluded (``missing_label``).
    An inner join would drop it silently, which the exclusion contract forbids.
    """
    sample_columns = _table_columns(conn, ALPHA_SAMPLE_TABLE)
    return_columns = _table_columns(conn, ALPHA_RETURN_TABLE)
    if not sample_columns or not return_columns:
        return [], False

    sample_select = ", ".join(
        [_select(sample_columns, name, name, "s") for name in _SAMPLE_FIELDS]
    )
    return_select = ", ".join(
        [_select(return_columns, name, f"r_{name}", "r") for name in _RETURN_FIELDS]
    )
    order_clause = _evidence_order_clause(sample_columns, return_columns)
    limit_clause = ""
    args: tuple = ()
    fetch_limit = None
    if max_rows is not None and int(max_rows) > 0:
        fetch_limit = int(max_rows) + 1
        limit_clause = " LIMIT ?"
        args = (fetch_limit,)
    sql = f"""
        SELECT {sample_select}, {return_select}
          FROM {ALPHA_SAMPLE_TABLE} s
          LEFT JOIN {ALPHA_RETURN_TABLE} r
            ON r.start_date = s.profile_date AND r.code = s.code
         ORDER BY {order_clause}{limit_clause}
    """
    raw_rows = _rows_as_dicts(conn, sql, args)
    if not raw_rows:
        return [], False

    evidence = []
    for record in raw_rows:
        sample = {name: record.get(name) for name in _SAMPLE_FIELDS if name not in DEFAULT_ALPHA_FEATURES}
        sample["sample_features"] = {name: record.get(name) for name in DEFAULT_ALPHA_FEATURES}
        label = {name: record.get(f"r_{name}") for name in _RETURN_FIELDS}
        evidence.append({"sample": sample, "label": label})

    truncated = fetch_limit is not None and len(evidence) > int(max_rows)
    if truncated:
        evidence = evidence[: int(max_rows)]
    return evidence, truncated


def _read_alpha_evidence(conn: sqlite3.Connection, max_rows: Optional[int] = None) -> list:
    """Backwards-compatible read that returns only the evidence rows."""
    evidence, _truncated = _read_alpha_evidence_page(conn, max_rows=max_rows)
    return evidence


def _label_identity(evidence: Mapping[str, Any]) -> Optional[tuple]:
    """Logical learning-sample identity of one candidate.

    Deliberately *not* the storage primary key: the returns table keys rows by
    ``(start_date, end_date, horizon, code)``, so two rows that differ only by
    ``end_date`` are distinct storage rows describing the *same* logical sample.
    """
    sample = dict(evidence.get("sample") or {})
    label = dict(evidence.get("label") or {})
    feature_asof = _date_text(sample.get("feature_asof")) or _date_text(sample.get("profile_date"))
    if feature_asof is None:
        return None
    horizon_value = _finite(label.get("horizon"))
    if horizon_value is None or horizon_value <= 0:
        return None
    return (
        _text(sample.get("source")) or DATASET_KIND_ADAPTIVE_ALPHA,
        feature_asof,
        _text(sample.get("code")) or "",
        int(horizon_value),
    )


def _ambiguous_identities(evidence: Sequence[Mapping[str, Any]]) -> frozenset:
    """Identities whose candidates disagree about the forward-label endpoint.

    Conflict is *not* resolved by picking one -- not first, not last, not
    ``MIN``/``MAX``, not rowid.  Every candidate under such an identity is
    refused, so the verdict can never depend on SQLite row order.
    """
    endpoints: dict = {}
    for item in evidence:
        identity = _label_identity(item)
        if identity is None:
            continue
        label = dict(item.get("label") or {})
        endpoint = _date_text(label.get("end_date"))
        if endpoint is None:
            continue
        endpoints.setdefault(identity, set()).add(endpoint)
    return frozenset(identity for identity, seen in endpoints.items() if len(seen) > 1)


def _classify(evidence: Mapping[str, Any], *, cutoff: str, feature_names: Sequence[str]) -> tuple:
    """Return ``(CanonicalSample | None, exclusion_reason | None)``.

    The single place where a candidate becomes eligible or is refused.  Every
    refusal carries a machine-readable reason; nothing is dropped silently and
    nothing is repaired behind the caller's back.
    """
    sample = dict(evidence.get("sample") or {})
    label = dict(evidence.get("label") or {})
    code = _text(sample.get("code")) or ""
    source = _text(sample.get("source")) or DATASET_KIND_ADAPTIVE_ALPHA

    feature_asof = _date_text(sample.get("feature_asof")) or _date_text(sample.get("profile_date"))
    if feature_asof is None:
        return None, "future_or_invalid_asof"
    if feature_asof > cutoff:
        return None, "future_or_invalid_asof"

    horizon_value = _finite(label.get("horizon"))
    if horizon_value is None or horizon_value <= 0:
        return None, "missing_label"
    horizon = int(horizon_value)

    horizon_semantics = (
        _text(label.get("horizon_semantics")) or HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS
    )
    if horizon_semantics not in SUPPORTED_HORIZON_SEMANTICS:
        return None, "unsupported_horizon_semantics"

    # ── features: preserve, classify, exclude.  Never impute. ──
    raw_features = sample.get("sample_features") or {}
    features = {}
    for name in feature_names:
        raw = raw_features.get(name)
        if _blank(raw):
            return None, "missing_feature"
        value = _finite(raw)
        if value is None:
            # NaN / Infinity / unparseable token: classified, never coerced to 0.
            return None, "non_finite_feature"
        features[str(name)] = value

    target = _finite(label.get("forward_return_pct"))
    if target is None:
        return None, "missing_label"

    # ── PIT availability: the feature's own visibility, not the label's. ──
    availability = _timestamp_text(sample.get("feature_available_at"))
    declared_pit = _text(sample.get("pit_status"))
    if declared_pit not in KNOWN_PIT_STATUSES:
        declared_pit = PIT_LEGACY_UNPROVEN
    if declared_pit == PIT_FUTURE:
        return None, "future_feature"
    if availability is None:
        reason = (
            "legacy_unproven_pit"
            if declared_pit == PIT_LEGACY_UNPROVEN
            else "unknown_feature_availability"
        )
        return None, reason
    if (_instant(availability) or "") > (_cutoff_instant(cutoff) or ""):
        return None, "future_feature"
    if declared_pit not in STRICT_PIT_STATUSES:
        # Availability is present but never proven (e.g. an import that copied
        # the profile date back in).  Still strict-excluded.
        return None, "legacy_unproven_pit"

    # ── labels: ordering, maturity and provenance, fail closed. ──
    label_start = _date_text(label.get("start_date"))
    label_end = _date_text(label.get("end_date"))
    if label_start is None or label_end is None:
        return None, "missing_label"
    if label_end <= feature_asof:
        return None, "invalid_label_time"
    label_available = _timestamp_text(label.get("label_available_at"))
    if label_available is None:
        # Unknown maturity is not the same thing as a matured label.
        return None, "immature_label"
    if (_instant(label_available) or "") < (_instant(label_end) or ""):
        return None, "invalid_label_time"
    # A label that matures (or becomes publishable) after the cutoff did not
    # exist yet at cutoff time.  Rebuilding a past dataset must never import a
    # future outcome, and this is never repaired by clamping or back-dating.
    cutoff_instant = _cutoff_instant(cutoff) or ""
    if (_instant(label_end) or "") > cutoff_instant:
        return None, "future_label"
    if (_instant(label_available) or "") > cutoff_instant:
        return None, "future_label"
    # Availability being *present* is not proof that it was *verified*.  An
    # endpoint that inherits a timestamp from a legacy/unknown row must not
    # launder that into a trusted label.
    if _text(label.get("pit_status")) != PIT_VERIFIED:
        return None, "unproven_label_pit"

    return (
        CanonicalSample(
            sample_key=sample_key(source, feature_asof, code, horizon),
            source=source,
            source_version=_text(sample.get("source_version"))
            or _text(sample.get("contract_version"))
            or "",
            code=code,
            strategy_id=_text(sample.get("strategy_id")) or "",
            model_family=_text(sample.get("model_family")) or _text(sample.get("regime")) or "",
            feature_asof=feature_asof,
            feature_available_at=availability,
            label_start_date=label_start,
            label_end_date=label_end,
            label_available_at=label_available,
            horizon=horizon,
            horizon_semantics=horizon_semantics,
            features=features,
            target=target,
            pit_status=PIT_VERIFIED,
            quality_flags=tuple(sorted(_loads(sample.get("quality_json")).keys())),
            provenance={
                "industry": _text(sample.get("industry")),
                "regime": _text(sample.get("regime")),
                "label_source": _text(label.get("source")) or "",
                "label_source_version": _text(label.get("source_version")) or "",
                "sample_contract_version": _text(sample.get("contract_version")) or CONTRACT_VERSION,
                "availability_clock": _AVAILABILITY_CLOCK,
                **_loads(sample.get("provenance_json")),
                **_loads(label.get("provenance_json")),
            },
        ),
        None,
    )


# ─────────────────────────── chronological split ───────────────────────────


def _normalize_split_spec(split_spec: Optional[Mapping[str, float]]) -> dict:
    spec = dict(split_spec or DEFAULT_SPLIT_SPEC)
    normalized = {}
    for name in PARTITIONS:
        value = _finite(spec.get(name))
        normalized[name] = value if value is not None and value > 0 else DEFAULT_SPLIT_SPEC[name]
    total = sum(normalized.values())
    if total <= 0:
        return dict(DEFAULT_SPLIT_SPEC)
    return {name: normalized[name] / total for name in PARTITIONS}


def _split_boundaries(dates: Sequence[str], spec: Mapping[str, float]) -> dict:
    """Assign contiguous date ranges to train/validation/test."""
    count = len(dates)
    train_end = int(count * float(spec["train"]))
    validation_size = int(count * float(spec["validation"]))
    if count >= len(PARTITIONS):
        train_end = max(1, min(train_end, count - (len(PARTITIONS) - 1)))
        validation_end = max(train_end + 1, min(train_end + validation_size, count - 1))
    else:
        train_end = max(0, min(train_end, count))
        validation_end = max(train_end, min(train_end + validation_size, count))
    return {
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, count),
    }


def chronological_split(
    samples: Sequence[CanonicalSample],
    *,
    split_spec: Optional[Mapping[str, float]] = None,
) -> tuple:
    """Deterministically split by ``label_start_date`` and purge overlap.

    Returns ``(partitions, purge_counts)``.

    Purging is the point: a label that *starts* inside train but *ends* inside
    validation has already seen validation-period prices.  Any train row whose
    ``label_end_date`` reaches the validation start is therefore removed, and
    the same rule applies across validation -> test.
    """
    spec = _normalize_split_spec(split_spec)
    ordered = sorted(samples, key=_sample_sort_key)
    dates = sorted({sample.label_start_date for sample in ordered})
    partitions = {name: [] for name in PARTITIONS}
    purge_counts = {name: 0 for name in PARTITIONS}
    if not dates:
        return partitions, purge_counts

    boundaries = _split_boundaries(dates, spec)
    assignment = {}
    for name in PARTITIONS:
        start_index, end_index = boundaries[name]
        for date in dates[start_index:end_index]:
            assignment[date] = name

    grouped = {name: [] for name in PARTITIONS}
    for sample in ordered:
        partition = assignment.get(sample.label_start_date)
        if partition:
            grouped[partition].append(sample)

    validation_start = (
        dates[boundaries["validation"][0]] if boundaries["validation"][0] < len(dates) else None
    )
    test_start = dates[boundaries["test"][0]] if boundaries["test"][0] < len(dates) else None

    for sample in grouped["train"]:
        if validation_start is not None and sample.label_end_date >= validation_start:
            purge_counts["train"] += 1
            continue
        partitions["train"].append(sample.with_partition("train"))
    for sample in grouped["validation"]:
        if test_start is not None and sample.label_end_date >= test_start:
            purge_counts["validation"] += 1
            continue
        partitions["validation"].append(sample.with_partition("validation"))
    for sample in grouped["test"]:
        partitions["test"].append(sample.with_partition("test"))
    return partitions, purge_counts


def has_temporal_overlap(partitions: Mapping[str, Sequence[CanonicalSample]]) -> bool:
    """True if a forward label still reaches into the next partition."""
    train = list(partitions.get("train") or [])
    validation = list(partitions.get("validation") or [])
    test = list(partitions.get("test") or [])
    if train and validation:
        validation_start = min(sample.label_start_date for sample in validation)
        if any(sample.label_end_date >= validation_start for sample in train):
            return True
    if validation and test:
        test_start = min(sample.label_start_date for sample in test)
        if any(sample.label_end_date >= test_start for sample in validation):
            return True
    return False


# ────────────────────────── manifest & fingerprint ──────────────────────────


def label_spec(horizon_semantics: str = HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS) -> dict:
    return {
        "horizon_semantics": horizon_semantics,
        "label_start_field": "label_start_date",
        "label_end_field": "label_end_date",
        "label_available_field": "label_available_at",
        "requires_mature_label": True,
        "label_end_must_exceed_feature_asof": True,
        # Maturity is only half of the label contract: the endpoint must also
        # be inside the cutoff and its availability independently proven.
        "label_end_must_not_exceed_cutoff": True,
        "label_availability_must_not_exceed_cutoff": True,
        "label_pit_must_be_verified": True,
        "conflicting_labels_for_same_identity_fail_closed": True,
        "observed_step_is_not_certified_session": True,
    }


def source_digest(samples: Sequence[CanonicalSample]) -> str:
    """Content digest of the source evidence, independent of insertion order."""
    keys = sorted(
        f"{s.source}|{s.source_version}|{s.feature_asof}|{s.code}|{s.horizon}|{s.sample_key}"
        for s in samples
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def dataset_fingerprint(
    partitions: Mapping[str, Sequence[CanonicalSample]],
    *,
    cutoff: str,
    feature_names: Sequence[str],
    horizon_semantics: str,
    split_spec: Mapping[str, float],
    contract_version: str = CONTRACT_VERSION,
    source_digest_value: str = "",
) -> str:
    """SHA-256 over a canonical serialization of the whole dataset.

    ``created_at`` is deliberately absent: replaying the same data on a later
    day must reproduce the same fingerprint.  Anything material -- a feature
    value, availability, target, label end date, horizon semantics, the
    feature list, cutoff, split spec, contract version or provenance -- changes
    it.
    """
    rows = []
    for name in PARTITIONS:
        for sample in partitions.get(name) or []:
            rows.append(sample.canonical(feature_names))
    rows.sort(key=_canonical_sort_key)
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "contract_version": contract_version,
        "cutoff": cutoff,
        "feature_names": sorted(str(name) for name in feature_names),
        "label_spec": _canon_json(label_spec(horizon_semantics)),
        "horizon_semantics": horizon_semantics,
        "split_spec": {name: _canon_number(split_spec[name]) for name in PARTITIONS},
        "source_digest": source_digest_value,
        "rows": rows,
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class DatasetBuild:
    """Result of one dataset build; carries the manifest and its fingerprint."""

    cutoff: str
    contract_version: str
    feature_names: tuple
    horizon_semantics: str
    split_spec: dict
    partitions: dict
    exclusions: dict
    purge_counts: dict
    manifest: dict
    fingerprint: str
    truncated: bool = False

    @property
    def eligible_rows(self) -> int:
        return sum(len(self.partitions.get(name) or []) for name in PARTITIONS)


def build_manifest(
    partitions: Mapping[str, Sequence[CanonicalSample]],
    *,
    exclusions: Mapping[str, int],
    cutoff: str,
    feature_names: Sequence[str],
    horizon_semantics: str,
    split_spec: Mapping[str, float],
    source_row_count: int,
    contract_version: str = CONTRACT_VERSION,
    code_build_identity: Optional[str] = None,
) -> dict:
    """Assemble the immutable manifest for one canonical dataset."""
    eligible = [sample for name in PARTITIONS for sample in (partitions.get(name) or [])]
    digest = source_digest(eligible)
    fingerprint = dataset_fingerprint(
        partitions,
        cutoff=cutoff,
        feature_names=feature_names,
        horizon_semantics=horizon_semantics,
        split_spec=split_spec,
        contract_version=contract_version,
        source_digest_value=digest,
    )
    feature_dates = sorted({sample.feature_asof for sample in eligible})
    label_end_dates = sorted({sample.label_end_date for sample in eligible})
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "contract_version": contract_version,
        "dataset_kind": DATASET_KIND_ADAPTIVE_ALPHA,
        "source": DATASET_KIND_ADAPTIVE_ALPHA,
        "cutoff": cutoff,
        "feature_names": sorted(str(name) for name in feature_names),
        "label_spec": label_spec(horizon_semantics),
        "horizon_semantics": horizon_semantics,
        "split_spec": {name: _canon_number(split_spec[name]) for name in PARTITIONS},
        "source_row_count": int(source_row_count),
        "eligible_row_count": len(eligible),
        "excluded_row_count": int(sum(int(count) for count in exclusions.values())),
        "exclusion_reasons": {key: int(value) for key, value in sorted(dict(exclusions).items())},
        "partition_rows": {name: len(partitions.get(name) or []) for name in PARTITIONS},
        "min_feature_date": feature_dates[0] if feature_dates else None,
        "max_feature_date": feature_dates[-1] if feature_dates else None,
        "min_label_end_date": label_end_dates[0] if label_end_dates else None,
        "max_label_end_date": label_end_dates[-1] if label_end_dates else None,
        "source_digest": digest,
        "code_build_identity": _text(code_build_identity),
        "execution_authority": "none",
        "dataset_scope": "research_read_only",
        # ``created_at`` describes the run, never the content.  It is stored on
        # the persisted row only and is absent from the fingerprint.
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "dataset_fingerprint": fingerprint,
    }


def persist_manifest(conn: sqlite3.Connection, manifest: Mapping[str, Any]) -> bool:
    """Append a manifest.  Re-running the same dataset is a no-op.

    Append-only by construction: the fingerprint is the primary key and the
    insert is ``INSERT OR IGNORE``, so a rebuild can never rewrite the facts
    recorded by the first run.
    """
    ensure_schema(conn)
    cursor = conn.execute(
        f"""
        INSERT OR IGNORE INTO {MANIFEST_TABLE}(
            dataset_fingerprint, manifest_schema_version, contract_version, dataset_kind,
            source, cutoff, feature_names, label_spec, horizon_semantics, split_spec,
            source_row_count, eligible_row_count, excluded_row_count, exclusion_reasons,
            partition_rows, min_feature_date, max_feature_date, min_label_end_date,
            max_label_end_date, source_digest, code_build_identity, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            manifest["dataset_fingerprint"],
            manifest["manifest_schema_version"],
            manifest["contract_version"],
            manifest["dataset_kind"],
            manifest["source"],
            manifest.get("cutoff"),
            json.dumps(manifest["feature_names"], ensure_ascii=False),
            json.dumps(manifest["label_spec"], ensure_ascii=False, sort_keys=True),
            manifest["horizon_semantics"],
            json.dumps(manifest["split_spec"], ensure_ascii=False, sort_keys=True),
            manifest["source_row_count"],
            manifest["eligible_row_count"],
            manifest["excluded_row_count"],
            json.dumps(manifest["exclusion_reasons"], ensure_ascii=False, sort_keys=True),
            json.dumps(manifest["partition_rows"], ensure_ascii=False, sort_keys=True),
            manifest.get("min_feature_date"),
            manifest.get("max_feature_date"),
            manifest.get("min_label_end_date"),
            manifest.get("max_label_end_date"),
            manifest["source_digest"],
            manifest.get("code_build_identity"),
            manifest["created_at"],
        ),
    )
    return bool(cursor.rowcount)


def read_manifest(conn: sqlite3.Connection, fingerprint: str) -> Optional[dict]:
    manifest = _row_as_dict(
        conn, f"SELECT * FROM {MANIFEST_TABLE} WHERE dataset_fingerprint=?", (fingerprint,)
    )
    if manifest is None:
        return None
    for key in ("feature_names", "label_spec", "split_spec", "exclusion_reasons", "partition_rows"):
        if isinstance(manifest.get(key), str):
            try:
                manifest[key] = json.loads(manifest[key])
            except ValueError:
                pass
    return manifest


# ─────────────────────────────── build entry ───────────────────────────────


def build_dataset(
    conn: sqlite3.Connection,
    *,
    cutoff: Any,
    split_spec: Optional[Mapping[str, float]] = None,
    feature_names: Sequence[str] = DEFAULT_ALPHA_FEATURES,
    horizon_semantics: str = HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
    contract_version: str = CONTRACT_VERSION,
    code_build_identity: Optional[str] = None,
    max_evidence_rows: Optional[int] = None,
    persist: bool = False,
) -> DatasetBuild:
    """Build the strict canonical dataset from persisted evidence only.

    No network call, no execution call, no imputation.  Rows that cannot prove
    their point-in-time availability or label maturity are excluded and
    audited by reason.
    """
    cutoff_date = _date_text(cutoff)
    if cutoff_date is None:
        raise ValueError("a parseable cutoff date is required to build a strict dataset")
    spec = _normalize_split_spec(split_spec)
    features = tuple(str(name) for name in feature_names)

    evidence, truncated = _read_alpha_evidence_page(conn, max_rows=max_evidence_rows)
    ambiguous = _ambiguous_identities(evidence)
    exclusions = {reason: 0 for reason in EXCLUSION_REASONS}
    accepted = []
    seen = set()
    for item in evidence:
        if _label_identity(item) in ambiguous:
            # Two candidates claim the same logical sample but disagree about
            # the label endpoint.  Picking one -- first, last, MIN or MAX --
            # would make the dataset depend on SQLite row order, so all of
            # them are refused instead.
            exclusions["ambiguous_label"] = exclusions.get("ambiguous_label", 0) + 1
            continue
        sample, reason = _classify(item, cutoff=cutoff_date, feature_names=features)
        if reason is not None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        if sample.sample_key in seen:
            exclusions["duplicate_sample"] = exclusions.get("duplicate_sample", 0) + 1
            continue
        seen.add(sample.sample_key)
        accepted.append(sample)

    partitions, purge_counts = chronological_split(accepted, split_spec=spec)
    exclusions["overlapping_label_purged"] = int(purge_counts["train"] + purge_counts["validation"])

    manifest = build_manifest(
        partitions,
        exclusions=exclusions,
        cutoff=cutoff_date,
        feature_names=features,
        horizon_semantics=horizon_semantics,
        split_spec=spec,
        source_row_count=len(evidence),
        contract_version=contract_version,
        code_build_identity=code_build_identity,
    )
    if persist:
        persist_manifest(conn, manifest)
    return DatasetBuild(
        cutoff=cutoff_date,
        contract_version=contract_version,
        feature_names=features,
        horizon_semantics=horizon_semantics,
        split_spec=spec,
        partitions=partitions,
        exclusions=exclusions,
        purge_counts=purge_counts,
        manifest=manifest,
        fingerprint=manifest["dataset_fingerprint"],
        truncated=truncated,
    )


# ─────────────────── read-only readiness gate for consumers ───────────────────


def contract_status(
    conn: sqlite3.Connection,
    *,
    cutoff: Any = None,
    split_spec: Optional[Mapping[str, float]] = None,
    max_evidence_rows: Optional[int] = None,
) -> dict:
    """Lightweight, side-effect-free dataset gate for readiness consumers.

    Returns ``dataset_blockers``: the reasons this dataset is not yet a
    research-grade dataset.  A row count alone can never clear them.  This
    function reads persistence only and never writes a manifest.
    """
    status = {
        "contract_version": CONTRACT_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "dataset_kind": DATASET_KIND_ADAPTIVE_ALPHA,
        "cutoff": None,
        "dataset_fingerprint": None,
        "pit_eligible_rows": 0,
        "split_rows": {name: 0 for name in PARTITIONS},
        "source_row_count": 0,
        "excluded_row_count": 0,
        "exclusion_reasons": {},
        "horizon_semantics": HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
        "min_label_end_date": None,
        "max_label_end_date": None,
        "truncated": False,
        "dataset_blockers": [],
    }
    try:
        if not _table_columns(conn, ALPHA_SAMPLE_TABLE):
            status["dataset_blockers"].append("dataset_evidence_table_missing")
            return status
        resolved_cutoff = _date_text(cutoff) or _latest_provable_cutoff(conn)
        if resolved_cutoff is None:
            status["dataset_blockers"].append("dataset_cutoff_unprovable")
            return status
        status["cutoff"] = resolved_cutoff
        build = build_dataset(
            conn,
            cutoff=resolved_cutoff,
            split_spec=split_spec,
            max_evidence_rows=max_evidence_rows,
            persist=False,
        )
    except Exception:
        # Fail closed: any error in the contract layer means "not ready",
        # never "assume ready".
        status["dataset_blockers"].append("dataset_contract_error")
        return status

    status["dataset_fingerprint"] = build.fingerprint
    status["pit_eligible_rows"] = build.eligible_rows
    status["split_rows"] = {name: len(build.partitions.get(name) or []) for name in PARTITIONS}
    status["source_row_count"] = build.manifest["source_row_count"]
    status["excluded_row_count"] = build.manifest["excluded_row_count"]
    status["exclusion_reasons"] = dict(build.manifest["exclusion_reasons"])
    status["horizon_semantics"] = build.horizon_semantics
    status["min_label_end_date"] = build.manifest["min_label_end_date"]
    status["max_label_end_date"] = build.manifest["max_label_end_date"]
    status["truncated"] = bool(build.truncated)

    blockers = status["dataset_blockers"]
    if not build.fingerprint:
        blockers.append("dataset_fingerprint_unavailable")
    if build.eligible_rows == 0:
        blockers.append("dataset_pit_eligible_rows_empty")
    for name in PARTITIONS:
        if status["split_rows"][name] == 0:
            blockers.append(f"dataset_{name}_partition_empty")
    if has_temporal_overlap(build.partitions):
        blockers.append("dataset_temporal_overlap_after_purge")
    if status["truncated"]:
        # A bounded read cannot prove whole-dataset integrity, so it must not
        # be presented as readiness.
        blockers.append("dataset_evidence_read_truncated")
    return status


def _latest_provable_cutoff(conn: sqlite3.Connection) -> Optional[str]:
    """Latest date whose evidence rows carry a proven availability timestamp."""
    columns = _table_columns(conn, ALPHA_SAMPLE_TABLE)
    if "feature_available_at" not in columns or "feature_asof" not in columns:
        return None
    try:
        row = conn.execute(
            f"""SELECT MAX(feature_asof) FROM {ALPHA_SAMPLE_TABLE}
                 WHERE feature_available_at IS NOT NULL AND pit_status=?""",
            (PIT_VERIFIED,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _date_text(row[0]) if row else None


# ─────────────────────────────── self-check ───────────────────────────────


def _check_sample(code: str, asof: str, label_end: str, partition: str) -> CanonicalSample:
    return CanonicalSample(
        sample_key=sample_key(DATASET_KIND_ADAPTIVE_ALPHA, asof, code, 1),
        source=DATASET_KIND_ADAPTIVE_ALPHA,
        source_version="self-check",
        code=code,
        strategy_id="",
        model_family="",
        feature_asof=asof,
        feature_available_at=asof + "T15:00:00",
        label_start_date=asof,
        label_end_date=label_end,
        label_available_at=label_end + "T15:00:00",
        horizon=1,
        horizon_semantics=HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
        features={name: 0.5 for name in DEFAULT_ALPHA_FEATURES},
        target=1.0,
        pit_status=PIT_VERIFIED,
        quality_flags=(),
        provenance={},
        partition=partition,
    )


def _self_check() -> None:
    assert forbidden_dependencies() == [], forbidden_dependencies()
    partitions = {
        "train": [_check_sample("000001", "2024-01-01", "2024-01-02", "train")],
        "validation": [_check_sample("000002", "2024-01-08", "2024-01-09", "validation")],
        "test": [_check_sample("000003", "2024-01-15", "2024-01-16", "test")],
    }

    def fingerprint(target, cutoff="2024-02-01"):
        return dataset_fingerprint(
            target,
            cutoff=cutoff,
            feature_names=DEFAULT_ALPHA_FEATURES,
            horizon_semantics=HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
            split_spec=DEFAULT_SPLIT_SPEC,
        )

    baseline = fingerprint(partitions)
    reordered = {name: partitions[name] for name in reversed(PARTITIONS)}
    assert baseline == fingerprint(reordered), "insertion order must not fork the fingerprint"
    assert baseline != fingerprint(partitions, cutoff="2024-01-20"), "cutoff must matter"

    train = [_check_sample("000001", "2024-01-01", "2024-01-09", "train")]
    split, purged = chronological_split(train + partitions["validation"] + partitions["test"])
    assert purged["train"] == 1, purged
    assert all(s.label_end_date < "2024-01-08" for s in split["train"]), split["train"]
    assert not has_temporal_overlap(split)
    print("learning_dataset self-check: ok")


if __name__ == "__main__":
    _self_check()
