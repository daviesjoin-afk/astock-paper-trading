"""Owner-issued, versioned lineage from canonical samples to raw financial facts.

Only exact persisted dataset identities and an immutable financial archive are
accepted. Callers cannot supply feature values or source records as proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence

try:
    import financial_point_in_time as FPIT
    import historical_financial_archive as HFA
    import learning_dataset as LD
except ImportError:  # pragma: no cover
    from . import financial_point_in_time as FPIT
    from . import historical_financial_archive as HFA
    from . import learning_dataset as LD

SCHEMA_VERSION = "financial-feature-evidence-v1"
_QUANTUM = Decimal("0.00000001")
_DERIVATIONS = {
    "roe": "roe_reported_v1",
    "gross_margin": "gross_margin_percent_v1",
    "debt_ratio": "debt_ratio_percent_v1",
    "revenue_yoy": "revenue_yoy_comparable_period_v1",
    "profit_yoy": "profit_yoy_comparable_period_v1",
}


class FinancialFeatureEvidenceError(ValueError):
    """A feature lineage is missing, unsupported, or conflicts with its identity."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _decimal_value(value: Decimal) -> float:
    return float(value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN))


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def _record_projection(record: HFA.FinancialRecordRef) -> dict[str, Any]:
    return record.projection()


def _visible(record: HFA.FinancialRecordRef, decision_at: str) -> tuple[bool, str]:
    row = record.projection()
    row.update(row.pop("financial_fields"))
    view = FPIT.financial_visibility(
        row, decision_at, value_keys=tuple(record.financial_fields),
    )
    if view.get("visible") and view.get("profit_source") == "reported":
        return True, ""
    if view.get("profit_source") == "future":
        return False, "financial_record_unavailable"
    return False, "financial_publication_unproven"


def _period_pairs(records: Sequence[HFA.FinancialRecordRef], field: str,
                  decision_at: str) -> tuple[HFA.FinancialRecordRef | None,
                                             HFA.FinancialRecordRef | None, str | None]:
    visible: dict[str, list[HFA.FinancialRecordRef]] = {}
    saw_unproven = False
    for record in records:
        if field not in record.financial_fields:
            continue
        ok, reason = _visible(record, decision_at)
        if ok:
            visible.setdefault(record.report_period, []).append(record)
        elif reason == "financial_publication_unproven":
            saw_unproven = True
    selected = {}
    for period, candidates in visible.items():
        newest = max(record.published_at for record in candidates)
        latest = [record for record in candidates if record.published_at == newest]
        fingerprints = {record.record_fingerprint for record in latest}
        if len(fingerprints) != 1:
            return None, None, "financial_record_unavailable"
        selected[period] = latest[0]
    if not selected:
        return None, None, "financial_publication_unproven" if saw_unproven else "financial_record_unavailable"
    current_period = max(selected)
    try:
        current_date = date.fromisoformat(current_period)
        prior_period = current_date.replace(year=current_date.year - 1).isoformat()
    except ValueError:
        return None, None, "financial_feature_derivation_unsupported"
    current = selected[current_period]
    if prior_period not in selected:
        return current, None, "financial_record_unavailable"
    return current, selected[prior_period], None


def _derive(feature_name: str, records: Sequence[HFA.FinancialRecordRef],
            decision_at: str) -> tuple[float | None, tuple[HFA.FinancialRecordRef, ...], str]:
    version = _DERIVATIONS.get(feature_name)
    if version is None:
        return None, (), "financial_feature_derivation_unsupported"
    if feature_name in {"roe", "gross_margin", "debt_ratio"}:
        field_map = {"roe": ("roe",), "gross_margin": ("gross_profit", "revenue"),
                     "debt_ratio": ("total_liabilities", "total_assets")}
        numerator_name, *rest = field_map[feature_name]
        required = (numerator_name, *rest)
        eligible = []
        saw_unproven = False
        for record in records:
            if not all(name in record.financial_fields for name in required):
                continue
            ok, reason = _visible(record, decision_at)
            if ok:
                eligible.append(record)
            elif reason == "financial_publication_unproven":
                saw_unproven = True
        if not eligible:
            return None, (), "financial_publication_unproven" if saw_unproven else "financial_record_unavailable"
        latest_period = max(record.report_period for record in eligible)
        latest = [record for record in eligible if record.report_period == latest_period]
        newest_at = max(record.published_at for record in latest)
        latest = [record for record in latest if record.published_at == newest_at]
        if len(latest) != 1:
            return None, (), "financial_record_unavailable"
        record = latest[0]
        values = [_number(record.financial_fields[name]) for name in required]
        if any(value is None for value in values):
            return None, (), "financial_record_unavailable"
        if feature_name == "roe":
            result = values[0]
        else:
            denominator = values[1]
            if denominator == 0:
                return None, (), "financial_feature_derivation_unsupported"
            result = values[0] / denominator * Decimal(100)
        return _decimal_value(result), (record,), version

    underlying = "revenue" if feature_name == "revenue_yoy" else "net_profit"
    current, prior, reason = _period_pairs(records, underlying, decision_at)
    if reason:
        return None, tuple(item for item in (current, prior) if item is not None), reason
    if current is None or prior is None:
        return None, (), "financial_record_unavailable"
    current_value = _number(current.financial_fields.get(underlying))
    prior_value = _number(prior.financial_fields.get(underlying))
    if current_value is None or prior_value is None or prior_value == 0:
        return None, (current, prior), "financial_feature_derivation_unsupported"
    return _decimal_value((current_value / prior_value - 1) * Decimal(100)), (current, prior), version


@dataclass(frozen=True, slots=True)
class FinancialFeatureEvidence:
    sample_key: str
    code: str
    feature_name: str
    feature_value: float | None
    financial_archive_fingerprint: str
    decision_at: str | None
    derivation_version: str | None
    input_record_fingerprints: tuple[str, ...]
    input_available_at: tuple[dict[str, str], ...]
    feature_available_at: str | None
    derived_value: float | None
    verification: str
    reason_code: str | None
    evidence_fingerprint: str

    def projection(self) -> dict[str, Any]:
        return {"sample_key": self.sample_key, "code": self.code,
                "feature_name": self.feature_name, "feature_value": self.feature_value,
                "financial_archive_fingerprint": self.financial_archive_fingerprint,
                "decision_at": self.decision_at,
                "derivation_version": self.derivation_version,
                "input_record_fingerprints": list(self.input_record_fingerprints),
                "input_available_at": [dict(item) for item in self.input_available_at],
                "feature_available_at": self.feature_available_at,
                "derived_value": self.derived_value,
                "verification": self.verification, "reason_code": self.reason_code,
                "evidence_fingerprint": self.evidence_fingerprint}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS financial_feature_evidence (
      evidence_fingerprint TEXT PRIMARY KEY, sample_key TEXT NOT NULL,
      feature_name TEXT NOT NULL, financial_archive_fingerprint TEXT NOT NULL,
      derivation_version TEXT NOT NULL, evidence_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_financial_feature_lookup
      ON financial_feature_evidence(sample_key,feature_name,financial_archive_fingerprint);
    CREATE TRIGGER IF NOT EXISTS financial_feature_evidence_no_update
      BEFORE UPDATE ON financial_feature_evidence BEGIN SELECT RAISE(ABORT, 'append-only financial feature evidence'); END;
    CREATE TRIGGER IF NOT EXISTS financial_feature_evidence_no_delete
      BEFORE DELETE ON financial_feature_evidence BEGIN SELECT RAISE(ABORT, 'append-only financial feature evidence'); END;
    """)


class FinancialFeatureEvidenceRepository:
    def __init__(self, conn: sqlite3.Connection,
                 archive_repository: HFA.HistoricalFinancialArchiveRepository):
        self.conn = conn
        self.archive_repository = archive_repository
        ensure_schema(conn)

    def derive_candidate(self, *, sample: LD.CanonicalSample, feature_name: str,
                         financial_archive_fingerprint: str) -> FinancialFeatureEvidence:
        """Issue field evidence while the canonical dataset builder binds the ref."""
        if not isinstance(sample, LD.CanonicalSample):
            raise FinancialFeatureEvidenceError("canonical_dataset_sample_required")
        sample_key, code = sample.sample_key, sample.code
        stored = (sample.features or {}).get(feature_name)
        decision = sample.feature_available_at
        archive = self.archive_repository.get_archive(financial_archive_fingerprint)
        archive_records = (self.archive_repository.records(financial_archive_fingerprint, code=code)
                           if archive is not None and code else [])
        reason: str | None = None
        derivation: str | None = _DERIVATIONS.get(feature_name)
        computed: float | None = None
        refs: tuple[HFA.FinancialRecordRef, ...] = ()
        if not sample_key or not code:
            reason = "financial_feature_evidence_missing"
        elif derivation is None:
            reason = "financial_feature_derivation_unsupported"
        elif stored is None:
            reason = "financial_feature_evidence_missing"
        elif archive is None:
            reason = "financial_record_unavailable"
        elif _instant(decision) is None:
            reason = "financial_feature_decision_mismatch"
        else:
            computed, refs, outcome = _derive(feature_name, archive_records, decision)
            if outcome.startswith("financial_"):
                reason = outcome
            elif computed is None:
                reason = "financial_record_unavailable"
            else:
                stored_number = _number(stored)
                if stored_number is None or _decimal_value(stored_number) != computed:
                    reason = "financial_feature_value_mismatch"
        available = tuple({"record_fingerprint": item.record_fingerprint,
                           "published_at": item.published_at,
                           "publication_precision": item.publication_precision}
                          for item in refs)
        feature_available = self._max_available(available) if available else None
        stored_number = _number(stored)
        material = {"sample_key": sample_key, "code": code, "feature_name": feature_name,
                    "feature_value": _decimal_value(stored_number) if stored_number is not None else None,
                    "financial_archive_fingerprint": financial_archive_fingerprint,
                    "decision_at": decision, "derivation_version": derivation,
                    "input_record_fingerprints": [item.record_fingerprint for item in refs],
                    "input_available_at": list(available),
                    "feature_available_at": feature_available,
                    "derived_value": computed,
                    "verification": "proven" if reason is None else "blocked",
                    "reason_code": reason}
        evidence_fp = _sha(material)
        projection = {**material, "evidence_fingerprint": evidence_fp}
        encoded = _json(projection)
        derivation_key = derivation or "unsupported_v1"
        existing = self.conn.execute("SELECT evidence_json FROM financial_feature_evidence WHERE evidence_fingerprint=?",
                                     (evidence_fp,)).fetchone()
        if existing is not None:
            if existing["evidence_json"] != encoded:
                raise FinancialFeatureEvidenceError("financial_feature_evidence_conflict")
            return self._decode(encoded)
        with self.conn:
            self.conn.execute("""INSERT INTO financial_feature_evidence
                (evidence_fingerprint,sample_key,feature_name,financial_archive_fingerprint,
                 derivation_version,evidence_json) VALUES(?,?,?,?,?,?)""",
                (evidence_fp, sample_key, feature_name, financial_archive_fingerprint,
                 derivation_key, encoded))
        return self._decode(encoded)

    def resolve_for_dataset_sample(self, *, dataset_fingerprint: str, sample_key: str,
                                   feature_name: str,
                                   financial_archive_fingerprint: str) -> FinancialFeatureEvidence:
        """Read only evidence referenced by a reproducible canonical dataset."""
        manifest = LD.read_manifest(self.conn, dataset_fingerprint)
        if manifest is None:
            raise FinancialFeatureEvidenceError("financial_feature_evidence_missing")
        try:
            build = LD.build_dataset(
                self.conn, cutoff=manifest["cutoff"], split_spec=manifest["split_spec"],
                feature_names=manifest["feature_names"],
                horizon_semantics=manifest["horizon_semantics"],
                contract_version=manifest["contract_version"],
                financial_feature_repository=self,
                financial_archive_fingerprint=financial_archive_fingerprint,
                persist=False,
            )
        except (TypeError, ValueError) as exc:
            raise FinancialFeatureEvidenceError("financial_feature_evidence_missing") from exc
        if build.fingerprint != dataset_fingerprint:
            raise FinancialFeatureEvidenceError("financial_feature_evidence_missing")
        sample = next((row for partition in LD.PARTITIONS
                       for row in build.partitions.get(partition, ())
                       if row.sample_key == sample_key), None)
        if sample is None:
            raise FinancialFeatureEvidenceError("financial_feature_evidence_missing")
        evidence_ref = (sample.financial_evidence_refs or {}).get(feature_name)
        if not evidence_ref:
            raise FinancialFeatureEvidenceError("financial_feature_evidence_missing")
        derivation_key = _DERIVATIONS.get(feature_name) or "unsupported_v1"
        row = self.conn.execute("""SELECT evidence_json,evidence_fingerprint FROM financial_feature_evidence
            WHERE evidence_fingerprint=? AND sample_key=? AND feature_name=?
              AND financial_archive_fingerprint=? AND derivation_version=?""",
            (evidence_ref, sample_key, feature_name, financial_archive_fingerprint,
             derivation_key)).fetchone()
        if row is None:
            raise FinancialFeatureEvidenceError("financial_feature_evidence_missing")
        item = self._decode(row["evidence_json"])
        if (item.sample_key != sample.sample_key or item.code != sample.code
                or item.feature_name != feature_name
                or item.decision_at != sample.feature_available_at
                or item.feature_value != _decimal_value(_number(sample.features.get(feature_name)))):
            raise FinancialFeatureEvidenceError("financial_feature_value_mismatch")
        return item

    @staticmethod
    def _max_available(items: Sequence[Mapping[str, str]]) -> str | None:
        values = []
        for item in items:
            published = item["published_at"]
            if item["publication_precision"] == "date":
                day = date.fromisoformat(published)
                values.append(datetime(day.year, day.month, day.day, 23, 59, 59, 999999,
                                       tzinfo=timezone.utc))
            else:
                parsed = _instant(published)
                if parsed is None:
                    return None
                values.append(parsed)
        if not values:
            return None
        maximum = max(values)
        return maximum.isoformat(timespec="microseconds" if maximum.microsecond else "seconds")

    @staticmethod
    def _decode(encoded: str) -> FinancialFeatureEvidence:
        try:
            row = json.loads(encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            evidence_fp = row.pop("evidence_fingerprint")
            if _sha(row) != evidence_fp:
                raise ValueError("fingerprint mismatch")
            row["evidence_fingerprint"] = evidence_fp
            row["input_record_fingerprints"] = tuple(row["input_record_fingerprints"])
            row["input_available_at"] = tuple(row["input_available_at"])
            return FinancialFeatureEvidence(**row)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FinancialFeatureEvidenceError("corrupt_financial_feature_evidence") from exc
