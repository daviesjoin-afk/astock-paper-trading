# -*- coding: utf-8 -*-
"""PR-8 contract tests: point-in-time reproducible learning dataset foundation.

Every test here is hermetic: an in-memory SQLite database, no network, and no
execution path.  The suite deliberately does not just assert "the happy path is
green" -- it asserts that the *refusals* happen, because a dataset builder that
silently accepts unprovable evidence is worse than one that returns nothing.
"""

import ast
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(__file__))
import learning_dataset as LD  # noqa: E402
import neural_shadow as NS  # noqa: E402


FEATURES = LD.DEFAULT_ALPHA_FEATURES
CUTOFF = "2026-03-01"

# The production (pre-PR-8) schema: no provenance columns at all.  Migration
# must be able to upgrade exactly this shape.
LEGACY_SCHEMA = """
CREATE TABLE adaptive_alpha_samples(
    profile_date TEXT NOT NULL, code TEXT NOT NULL, industry TEXT,
    close_price REAL NOT NULL, regime TEXT NOT NULL, price_momentum REAL NOT NULL,
    main_flow REAL NOT NULL, turnover REAL NOT NULL, volume_ratio REAL NOT NULL,
    small_size REAL NOT NULL, value REAL NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(profile_date,code)
);
CREATE TABLE adaptive_alpha_returns(
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, horizon INTEGER NOT NULL,
    code TEXT NOT NULL, forward_return_pct REAL NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(start_date,end_date,horizon,code)
);
"""


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    LD.ensure_schema(conn)
    return conn


class DbTestCase(unittest.TestCase):
    """Base class that guarantees every in-memory database is closed."""

    def new_db(self):
        conn = open_db()
        self.addCleanup(conn.close)
        return conn

    def raw_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn


def seed_sample(conn, profile_date, code="000001", *, asof="auto", available="auto",
                pit=None, close=10.0, features=None, industry="测试行业", regime="neutral",
                provenance=None):
    """Insert one evidence row.  ``available=None`` means "no proof recorded".

    ``provenance`` models the *raw* evidence provenance blob a historical row
    may carry; it must never override the canonical layer's own fields.
    """
    asof_value = profile_date if asof == "auto" else asof
    availability = f"{profile_date}T15:15:00" if available == "auto" else available
    if pit is None:
        pit = LD.PIT_VERIFIED if availability else LD.PIT_LEGACY_UNPROVEN
    values = features if features is not None else {name: 0.5 for name in FEATURES}
    conn.execute(
        """INSERT OR REPLACE INTO adaptive_alpha_samples(
               profile_date,code,industry,close_price,regime,price_momentum,main_flow,
               turnover,volume_ratio,small_size,value,created_at,
               feature_asof,feature_available_at,pit_status,source,source_version,
               contract_version,provenance_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (profile_date, code, industry, close, regime,
         *[values.get(name) for name in FEATURES], f"{profile_date}T16:00:00",
         asof_value, availability, pit, "market_snapshot_full.json", "test-v1",
         LD.CONTRACT_VERSION, _json(provenance)),
    )


def _json(value):
    return None if value is None else json.dumps(value, ensure_ascii=False)


def seed_label(conn, start, end, horizon=1, code="000001", ret=1.0, available="auto", pit="auto",
               provenance=None):
    """Insert one forward label.  ``pit='auto'`` means "verified"; pass an
    explicit status (or ``None``) to model an endpoint whose provenance is not
    proven."""
    availability = f"{end}T15:15:00" if available == "auto" else available
    status = LD.PIT_VERIFIED if pit == "auto" else pit
    conn.execute(
        """INSERT OR IGNORE INTO adaptive_alpha_returns(
               start_date,end_date,horizon,code,forward_return_pct,created_at,
               label_available_at,horizon_semantics,pit_status,source,source_version,
               contract_version,provenance_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (start, end, horizon, code, ret, f"{end}T16:00:00", availability,
         LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS, status,
         LD.DATASET_KIND_ADAPTIVE_ALPHA, "test-v1", LD.CONTRACT_VERSION,
         _json(provenance)),
    )


def series(days, codes=("000001",), horizons=(1,), close_base=10.0):
    """Feature days plus mature forward labels over a consecutive date series."""
    return days, codes, horizons, close_base


def seed_series(conn, days, *, codes=("000001",), horizons=(1,), close_step=0.1):
    for index, day in enumerate(days):
        for code in codes:
            seed_sample(conn, day, code, close=close_base_for(index, close_step))
    for horizon in horizons:
        for index in range(len(days) - horizon):
            for code in codes:
                seed_label(conn, days[index], days[index + horizon], horizon, code)


def close_base_for(index, close_step=0.1):
    return round(10.0 + index * close_step, 4)


def build(conn, cutoff=CUTOFF, **kwargs):
    return LD.build_dataset(conn, cutoff=cutoff, **kwargs)


def reasons(built):
    return {key: count for key, count in built.manifest["exclusion_reasons"].items() if count}


def all_rows(built):
    return [row for name in LD.PARTITIONS for row in (built.partitions.get(name) or [])]


def dates(count, start_day=1):
    import datetime as dt

    first = dt.date(2026, 1, start_day)
    return [(first + dt.timedelta(days=offset)).isoformat() for offset in range(count)]


class PitContractTests(DbTestCase):
    """feature_available_at <= cutoff, and nothing may be upgraded to available."""

    def test_availability_equal_to_cutoff_is_allowed(self):
        conn = self.new_db()
        seed_series(conn, dates(4))
        built = build(conn, cutoff="2026-01-02")
        self.assertTrue(all_rows(built), reasons(built))

    def test_availability_before_cutoff_is_allowed(self):
        conn = self.new_db()
        seed_series(conn, dates(4))
        built = build(conn, cutoff="2026-01-04")
        self.assertTrue(built.eligible_rows, reasons(built))

    def test_availability_after_cutoff_is_excluded_as_future_feature(self):
        conn = self.new_db()
        # asof is comfortably inside the cutoff, but the feature was only
        # proven available later: future feature != available feature.
        seed_sample(conn, "2026-01-01", available="2026-01-20T15:15:00")
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn, cutoff="2026-01-10")
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("future_feature"), 1)

    def test_feature_asof_after_cutoff_is_excluded(self):
        conn = self.new_db()
        day_list = dates(4)
        seed_series(conn, day_list, horizons=(1,))
        built = build(conn, cutoff="2026-01-02")
        # 01-03 and 01-04 describe an asof past the cutoff.
        self.assertEqual(reasons(built).get("future_or_invalid_asof"), 2)

    def test_sample_without_any_label_is_audited_not_dropped(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_sample(conn, "2026-01-09")
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        # The unlabeled sample is visible in the source count and audited.
        self.assertEqual(built.manifest["source_row_count"], 2)
        self.assertEqual(reasons(built).get("missing_label"), 1)

    def test_missing_availability_is_excluded_from_strict_dataset(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available=None)
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("legacy_unproven_pit"), 1)

    def test_unknown_availability_is_not_available(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available=None, pit=LD.PIT_UNKNOWN)
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("unknown_feature_availability"), 1)

    def test_legacy_pit_is_not_upgraded_to_verified(self):
        """Availability present but provenance unproven must still be refused."""
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available="2026-01-01T15:15:00", pit=LD.PIT_LEGACY_UNPROVEN)
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("legacy_unproven_pit"), 1)

    def test_financial_unknown_publication_cannot_enter_strict_dataset(self):
        """Delegates to financial_point_in_time; a report period is not a date."""
        available_at, status = LD.financial_availability(
            {"report_date": "2024-03-31", "net_profit": 10}, "2024-06-30"
        )
        self.assertIsNone(available_at)
        self.assertEqual(status, LD.PIT_UNPROVEN)

    def test_financial_future_publication_is_classified_future(self):
        available_at, status = LD.financial_availability(
            {"report_date": "2024-06-30", "report_published_at": "2024-08-31", "net_profit": 10},
            "2024-06-30",
        )
        self.assertEqual(status, LD.PIT_FUTURE)


class LabelContractTests(DbTestCase):
    """label_end_date > feature_asof, and the label must be provably mature."""

    def test_label_end_equal_to_feature_asof_is_rejected(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-01")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("invalid_label_time"), 1)

    def test_label_end_before_feature_asof_is_rejected(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-05")
        seed_label(conn, "2026-01-05", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("invalid_label_time"), 1)

    def test_immature_label_is_excluded(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02", available=None)
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("immature_label"), 1)

    def test_label_available_before_label_end_is_invalid(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-05", available="2026-01-04T15:15:00")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("invalid_label_time"), 1)

    # ── A. a label that matures after the cutoff is a *future* label ──

    def test_label_end_after_cutoff_is_a_future_label(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-20")
        built = build(conn, cutoff="2026-01-10")
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("future_label"), 1)

    def test_label_availability_after_cutoff_is_a_future_label(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        # Endpoint is inside the cutoff, but it only became available later.
        seed_label(conn, "2026-01-01", "2026-01-05", available="2026-01-20T15:15:00")
        built = build(conn, cutoff="2026-01-10")
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("future_label"), 1)

    def test_label_available_equal_to_cutoff_is_allowed(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-05", available="2026-01-10T15:15:00")
        built = build(conn, cutoff="2026-01-10")
        self.assertEqual(built.eligible_rows, 1, reasons(built))

    def test_future_label_is_purged_not_clamped(self):
        """The builder must exclude, never back-date the endpoint to cutoff."""
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-20")
        built = build(conn, cutoff="2026-01-10")
        self.assertEqual(all_rows(built), [])
        self.assertIsNone(built.manifest["max_label_end_date"])

    # ── B. label provenance must be independently proven ──

    def test_unproven_label_pit_is_excluded(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02", pit=LD.PIT_LEGACY_UNPROVEN)
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("unproven_label_pit"), 1)

    def test_every_non_verified_label_pit_state_is_excluded(self):
        for status in (LD.PIT_LEGACY_UNPROVEN, LD.PIT_UNPROVEN, LD.PIT_UNKNOWN,
                       LD.PIT_FUTURE, None, "bogus"):
            with self.subTest(status=status):
                conn = self.new_db()
                seed_sample(conn, "2026-01-01")
                seed_label(conn, "2026-01-01", "2026-01-02", pit=status)
                built = build(conn)
                self.assertEqual(built.eligible_rows, 0, status)
                self.assertEqual(reasons(built).get("unproven_label_pit"), 1, status)

    def test_label_availability_timestamp_alone_does_not_imply_verified(self):
        """A present timestamp is not provenance: verified is required."""
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02",
                   available="2026-01-02T15:15:00", pit=None)
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("unproven_label_pit"), 1)

    def test_missing_target_is_excluded_not_zero_filled(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02", ret=None)
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("missing_label"), 1)

    def test_future_label_cannot_appear_in_feature_columns(self):
        conn = self.new_db()
        seed_series(conn, dates(4), horizons=(1,))
        built = build(conn)
        self.assertTrue(built.eligible_rows)
        for row in all_rows(built):
            canonical = row.canonical(FEATURES)
            self.assertEqual(sorted(canonical["features"]), sorted(FEATURES))
            for name in ("forward_return_pct", "target", "label_end_date", "horizon"):
                self.assertNotIn(name, canonical["features"])

    def test_unsupported_horizon_semantics_is_excluded(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02")
        conn.execute("UPDATE adaptive_alpha_returns SET horizon_semantics='certified_trading_days'")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("unsupported_horizon_semantics"), 1)

    def test_observed_step_semantics_is_recorded_not_claimed_as_sessions(self):
        conn = self.new_db()
        seed_series(conn, dates(4))
        built = build(conn)
        self.assertEqual(built.manifest["horizon_semantics"], "observed_profile_steps")
        label_spec = built.manifest["label_spec"]
        self.assertTrue(label_spec["observed_step_is_not_certified_session"])
        self.assertTrue(label_spec["requires_mature_label"])


class SplitPurgeTests(DbTestCase):
    """Chronological split with overlapping-label purge."""

    def _split_fixture(self):
        conn = self.new_db()
        day_list = dates(12)
        seed_series(conn, day_list, horizons=(1,))
        return conn, day_list

    def test_train_overlapping_validation_is_purged(self):
        conn, day_list = self._split_fixture()
        built = build(conn)
        train = built.partitions["train"]
        validation = built.partitions["validation"]
        self.assertTrue(train and validation)
        validation_start = min(row.label_start_date for row in validation)
        self.assertTrue(all(row.label_end_date < validation_start for row in train))
        self.assertGreater(built.purge_counts["train"], 0)
        self.assertEqual(reasons(built).get("overlapping_label_purged"), sum(built.purge_counts.values()))

    def test_validation_overlapping_test_is_purged(self):
        conn, day_list = self._split_fixture()
        built = build(conn)
        validation = built.partitions["validation"]
        test = built.partitions["test"]
        self.assertTrue(validation and test)
        test_start = min(row.label_start_date for row in test)
        self.assertTrue(all(row.label_end_date < test_start for row in validation))

    def test_no_temporal_overlap_survives(self):
        conn, _ = self._split_fixture()
        built = build(conn)
        self.assertFalse(LD.has_temporal_overlap(built.partitions))

    def test_split_is_chronological_not_shuffled(self):
        conn, _ = self._split_fixture()
        built = build(conn)
        boundaries = [
            max(row.label_start_date for row in built.partitions["train"]),
            min(row.label_start_date for row in built.partitions["validation"]),
            max(row.label_start_date for row in built.partitions["validation"]),
            min(row.label_start_date for row in built.partitions["test"]),
        ]
        self.assertLess(boundaries[0], boundaries[1])
        self.assertLess(boundaries[2], boundaries[3])

    def test_small_dataset_reports_empty_partition_instead_of_pretending_ready(self):
        conn = self.new_db()
        seed_series(conn, dates(2))
        built = build(conn)
        self.assertTrue(built.eligible_rows)
        empty = [name for name in LD.PARTITIONS if not built.partitions[name]]
        self.assertTrue(empty)


class DeterminismTests(DbTestCase):
    """Same inputs -> same fingerprint, regardless of storage order."""

    def _fingerprint(self, conn, day_list):
        built = build(conn)
        return built

    def _fingerprint_of(self, partitions, built):
        return LD.dataset_fingerprint(
            partitions,
            cutoff=built.cutoff,
            feature_names=built.feature_names,
            horizon_semantics=built.horizon_semantics,
            split_spec=built.split_spec,
            source_digest_value=built.manifest["source_digest"],
        )

    def test_fingerprint_is_row_order_independent_by_construction(self):
        """Same logical rows, different list order -> same SHA-256.

        The split already emits sorted partitions, so this exercises the
        fingerprint's own canonical ordering: callers that assemble partitions
        themselves must still get a stable digest.
        """
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,), codes=("000001", "000002"))
        built = build(conn)
        reordered = {
            "test": list(reversed(built.partitions["test"])),
            "validation": list(reversed(built.partitions["validation"])),
            "train": list(reversed(built.partitions["train"])),
        }
        self.assertEqual(self._fingerprint_of(reordered, built), built.fingerprint)

    def test_fingerprint_is_stable_across_repeated_calls(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,), codes=("000001", "000002"))
        built = build(conn)
        self.assertEqual(self._fingerprint_of(built.partitions, built), built.fingerprint)
        self.assertEqual(self._fingerprint_of(built.partitions, built), built.fingerprint)

    def test_insertion_order_does_not_change_fingerprint(self):
        forward, reverse = self.new_db(), self.new_db()
        day_list = dates(10)
        seed_series(forward, day_list, horizons=(1,), codes=("000001", "000002", "000003"))
        for day in reversed(day_list):
            for code in ("000003", "000002", "000001"):
                seed_sample(reverse, day, code, close=close_base_for(day_list.index(day)))
        for index in reversed(range(len(day_list) - 1)):
            for code in ("000003", "000002", "000001"):
                seed_label(reverse, day_list[index], day_list[index + 1], 1, code)
        self.assertEqual(
            build(forward).fingerprint, build(reverse).fingerprint
        )

    def test_rebuild_is_idempotent_and_manifest_is_append_only(self):
        conn = self.new_db()
        seed_series(conn, dates(10), horizons=(1,))
        first = build(conn, persist=True)
        second = build(conn, persist=True)
        self.assertEqual(first.fingerprint, second.fingerprint)
        rows = conn.execute("SELECT COUNT(*) FROM learning_dataset_manifests").fetchone()[0]
        self.assertEqual(rows, 1)
        third = build(conn, persist=True)
        self.assertEqual(third.fingerprint, first.fingerprint)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM learning_dataset_manifests").fetchone()[0], 1)

    def test_persist_is_opt_in_and_never_writes_a_manifest_by_default(self):
        conn = self.new_db()
        seed_series(conn, dates(10), horizons=(1,))
        build(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM learning_dataset_manifests").fetchone()[0], 0
        )

    def test_manifest_round_trip(self):
        conn = self.new_db()
        seed_series(conn, dates(10), horizons=(1,))
        built = build(conn, persist=True)
        stored = LD.read_manifest(conn, built.fingerprint)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["dataset_fingerprint"], built.fingerprint)
        self.assertEqual(stored["eligible_row_count"], built.eligible_rows)
        self.assertEqual(stored["exclusion_reasons"], built.manifest["exclusion_reasons"])


class SensitivityTests(DbTestCase):
    """Every material change must fork the fingerprint."""

    def _baseline(self):
        conn = self.new_db()
        day_list = dates(10)
        seed_series(conn, day_list, horizons=(1,), codes=("000001", "000002"))
        return conn, day_list, build(conn).fingerprint

    def _fingerprint(self, conn, **kwargs):
        return build(conn, **kwargs).fingerprint

    def test_one_feature_value_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        conn.execute("UPDATE adaptive_alpha_samples SET main_flow=0.123456 WHERE code='000002'")
        self.assertNotEqual(self._fingerprint(conn), baseline)

    def test_availability_timestamp_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        conn.execute("UPDATE adaptive_alpha_samples SET feature_available_at='2026-01-11T15:15:00'")
        self.assertNotEqual(self._fingerprint(conn), baseline)

    def test_target_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        conn.execute("UPDATE adaptive_alpha_returns SET forward_return_pct=9.5 WHERE code='000002'")
        self.assertNotEqual(self._fingerprint(conn), baseline)

    def test_label_end_date_changes_fingerprint(self):
        conn, day_list, baseline = self._baseline()
        conn.execute(
            "UPDATE adaptive_alpha_returns SET end_date=? WHERE code='000001' AND start_date=?",
            (day_list[2], day_list[0]),
        )
        self.assertNotEqual(self._fingerprint(conn), baseline)

    def test_feature_list_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        self.assertNotEqual(self._fingerprint(conn, feature_names=FEATURES[:4]), baseline)

    def test_cutoff_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        self.assertNotEqual(self._fingerprint(conn, cutoff="2026-01-08"), baseline)

    def test_split_spec_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        self.assertNotEqual(
            self._fingerprint(conn, split_spec={"train": 0.5, "validation": 0.25, "test": 0.25}),
            baseline,
        )

    def test_contract_version_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        self.assertNotEqual(self._fingerprint(conn, contract_version="learning-dataset-v0"), baseline)

    def test_partition_membership_changes_fingerprint(self):
        conn, _, baseline = self._baseline()
        built = build(conn)
        moved = {
            "train": [row.with_partition("test") for row in list(built.partitions["train"])[:1]],
            "validation": list(built.partitions["validation"]),
            "test": [row.with_partition("train") for row in list(built.partitions["train"])[1:]]
            + list(built.partitions["test"]),
        }
        self.assertNotEqual(
            LD.dataset_fingerprint(
                moved,
                cutoff=built.cutoff,
                feature_names=built.feature_names,
                horizon_semantics=built.horizon_semantics,
                split_spec=built.split_spec,
                source_digest_value=built.manifest["source_digest"],
            ),
            baseline,
        )

    def test_created_at_is_not_part_of_the_content_fingerprint(self):
        conn, _, baseline = self._baseline()
        built = build(conn)
        recomputed = LD.dataset_fingerprint(
            built.partitions,
            cutoff=built.cutoff,
            feature_names=built.feature_names,
            horizon_semantics=built.horizon_semantics,
            split_spec=built.split_spec,
            source_digest_value=built.manifest["source_digest"],
        )
        self.assertEqual(recomputed, baseline)
        self.assertNotIn("created_at", built.manifest["label_spec"])
        self.assertNotIn(built.manifest["created_at"], str(recomputed))


class MissingDataTests(DbTestCase):
    """Missing is not zero, and the layer never imputes."""

    def test_missing_feature_is_not_converted_to_zero(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02")
        # A blank / unparseable stored value is "missing", not "0.0".
        conn.execute("UPDATE adaptive_alpha_samples SET value=''")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("missing_feature"), 1)

    def test_nan_is_never_silently_zero(self):
        evidence = {
            "sample": {
                "profile_date": "2026-01-01",
                "code": "000001",
                "feature_asof": "2026-01-01",
                "feature_available_at": "2026-01-01T15:15:00",
                "pit_status": LD.PIT_VERIFIED,
                "sample_features": {**{name: 0.5 for name in FEATURES}, "turnover": float("nan")},
            },
            "label": {
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "horizon": 1,
                "forward_return_pct": 1.0,
                "label_available_at": "2026-01-02T15:15:00",
                "horizon_semantics": LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
            },
        }
        sample, reason = LD._classify(evidence, cutoff="2026-03-01", feature_names=FEATURES)
        self.assertIsNone(sample)
        self.assertEqual(reason, "non_finite_feature")

    def test_nan_stored_in_sqlite_is_excluded(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02")
        # A stored non-numeric token is non-finite, not zero (the column is
        # NOT NULL, so a real NULL cannot even be written here).
        conn.execute("UPDATE adaptive_alpha_samples SET turnover='NaN'")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("non_finite_feature"), 1)

    def test_infinity_is_rejected_not_normalized(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02")
        conn.execute("UPDATE adaptive_alpha_samples SET turnover=?", (float("inf"),))
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("non_finite_feature"), 1)

    def test_genuine_zero_is_preserved(self):
        conn = self.new_db()
        features = {name: 0.5 for name in FEATURES}
        features["turnover"] = 0.0
        seed_sample(conn, "2026-01-01", features=features)
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 1)
        row = all_rows(built)[0]
        self.assertEqual(row.features["turnover"], 0.0)

    def test_canonical_number_normalizes_negative_zero(self):
        self.assertEqual(LD._canon_number(-0.0), LD._canon_number(0.0))

    def test_canonical_number_rejects_non_finite(self):
        self.assertIsNone(LD._canon_number(float("inf")))
        self.assertIsNone(LD._canon_number(float("nan")))


class ExclusionAuditTests(DbTestCase):
    """Nothing is dropped silently; the manifest counts every refusal."""

    def test_manifest_counts_every_exclusion_reason(self):
        conn = self.new_db()
        seed_series(conn, dates(6), horizons=(1,))
        seed_sample(conn, "2026-01-03", "999999", available=None)
        seed_label(conn, "2026-01-03", "2026-01-04", 1, "999999")
        built = build(conn)
        audit = built.manifest["exclusion_reasons"]
        self.assertEqual(set(audit), set(LD.EXCLUSION_REASONS))
        self.assertEqual(audit["legacy_unproven_pit"], 1)
        self.assertEqual(built.manifest["excluded_row_count"], sum(audit.values()))

    def test_conflicting_label_endpoints_fail_closed(self):
        """Same logical identity, two endpoints -> refuse both.

        The previous assertion here (``assertGreaterEqual(count, 0)``) was
        vacuously true, so it proved nothing.  This fixes real behaviour:
        neither endpoint may enter the dataset, and the audit must say why.
        """
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02", 1, ret=1.0)
        seed_label(conn, "2026-01-01", "2026-01-03", 1, ret=2.0)
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("ambiguous_label"), 2)
        self.assertEqual(built.manifest["exclusion_reasons"]["duplicate_sample"], 0)

    def test_source_row_count_matches_evidence_read(self):
        conn = self.new_db()
        seed_series(conn, dates(6), horizons=(1,))
        built = build(conn)
        self.assertEqual(built.manifest["source_row_count"], len(LD._read_alpha_evidence(conn)))


class AmbiguousLabelTests(DbTestCase):
    """Conflicting label endpoints must fail closed, order-independently.

    ``adaptive_alpha_returns`` is keyed by ``(start_date, end_date, horizon,
    code)``, so the database legitimately allows two rows that describe the
    *same* logical sample (same source/asof/code/horizon) with different
    endpoints.  Neither may win by accident of storage order.
    """

    def _seed_conflict(self, conn, order=("A", "B")):
        seed_sample(conn, "2026-01-01")
        endpoints = {"A": ("2026-01-02", 1.0), "B": ("2026-01-03", 2.0)}
        for key in order:
            end, ret = endpoints[key]
            seed_label(conn, "2026-01-01", end, 1, ret=ret)

    def test_conflict_is_never_resolved_by_picking_an_endpoint(self):
        conn = self.new_db()
        self._seed_conflict(conn)
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(built.manifest["eligible_row_count"], 0)
        self.assertIsNone(built.manifest["max_label_end_date"])
        self.assertIsNone(built.manifest["min_label_end_date"])

    def test_conflicting_endpoints_are_all_audited(self):
        conn = self.new_db()
        self._seed_conflict(conn)
        built = build(conn)
        # Counting unit: one per refused *candidate evidence row* (two rows
        # here), not one per identity.
        self.assertEqual(reasons(built).get("ambiguous_label"), 2)

    def test_insertion_order_does_not_change_the_verdict(self):
        forward, reverse = self.new_db(), self.new_db()
        self._seed_conflict(forward, order=("A", "B"))
        self._seed_conflict(reverse, order=("B", "A"))
        first, second = build(forward), build(reverse)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(reasons(first), reasons(second))
        self.assertEqual(all_rows(first), [])
        self.assertEqual(all_rows(second), [])

    def test_ambiguous_identity_does_not_leak_into_a_sibling_identity(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        # Same start/horizon, different code -> a *different* logical sample,
        # so the conflict on 000001 must not poison 000002.
        seed_label(conn, "2026-01-01", "2026-01-02", 1, code="000001")
        seed_label(conn, "2026-01-01", "2026-01-03", 1, code="000001")
        seed_sample(conn, "2026-01-01", "000002")
        seed_label(conn, "2026-01-01", "2026-01-02", 1, code="000002")
        built = build(conn)
        self.assertEqual(reasons(built).get("ambiguous_label"), 2)
        self.assertEqual(built.eligible_rows, 1)
        self.assertEqual([row.code for row in all_rows(built)], ["000002"])


class LabelPitInheritanceTests(DbTestCase):
    """A matured label may only inherit provenance from a *proven* endpoint."""

    def _engine(self):
        try:
            import adaptive_engine as AE  # noqa: E402
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"adaptive_engine unavailable: {exc}") from exc
        return AE

    def _mature(self, conn):
        self._engine()._mature_alpha_returns(conn)
        return conn.execute(
            """SELECT pit_status, label_available_at FROM adaptive_alpha_returns
                WHERE start_date='2026-01-01' AND end_date='2026-01-02' AND horizon=1"""
        ).fetchone()

    def test_verified_endpoint_yields_a_verified_label(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_sample(conn, "2026-01-02")
        row = self._mature(conn)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], LD.PIT_VERIFIED)

    def test_unproven_endpoint_cannot_yield_a_verified_label(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        # Availability timestamp present, provenance only legacy.
        seed_sample(conn, "2026-01-02", available="2026-01-02T15:15:00",
                    pit=LD.PIT_LEGACY_UNPROVEN)
        row = self._mature(conn)
        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], LD.PIT_VERIFIED)
        self.assertEqual(row[0], LD.PIT_LEGACY_UNPROVEN)

    def test_endpoint_without_availability_stays_unproven(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_sample(conn, "2026-01-02", available=None)
        row = self._mature(conn)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], LD.PIT_LEGACY_UNPROVEN)
        self.assertIsNone(row[1])

    def test_unproven_label_is_refused_by_the_dataset_builder(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_sample(conn, "2026-01-02", available="2026-01-02T15:15:00",
                    pit=LD.PIT_LEGACY_UNPROVEN)
        self._mature(conn)
        built = build(conn)
        self.assertEqual(reasons(built).get("unproven_label_pit"), 1)
        self.assertEqual(built.eligible_rows, 0)


def evidence(
    *,
    asof="2026-09-11",
    feature_available="2026-09-12T15:59:59+00:00",
    feature_pit=None,
    label_start="2026-09-11",
    label_end="2026-09-12",
    label_available="2026-09-12T15:59:59+00:00",
    label_pit=None,
    horizon=1,
    target=1.0,
):
    """Assemble one synthetic candidate without touching a database.

    Defaults sit exactly on the ``cutoff="2026-09-12"`` boundary so a test can
    move a single field across it.
    """
    return {
        "sample": {
            "profile_date": asof,
            "code": "000001",
            "feature_asof": asof,
            "feature_available_at": feature_available,
            "pit_status": LD.PIT_VERIFIED if feature_pit is None else feature_pit,
            "source": LD.DATASET_KIND_ADAPTIVE_ALPHA,
            "source_version": "test-v1",
            "contract_version": LD.CONTRACT_VERSION,
            "sample_features": {name: 0.5 for name in FEATURES},
        },
        "label": {
            "start_date": label_start,
            "end_date": label_end,
            "horizon": horizon,
            "forward_return_pct": target,
            "label_available_at": label_available,
            "horizon_semantics": LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
            "pit_status": LD.PIT_VERIFIED if label_pit is None else label_pit,
        },
    }


class TimezoneContractTests(DbTestCase):
    """PIT comparisons run on one canonical UTC clock.

    A date-only cutoff is the end of that *exchange-local* (UTC+08:00) day, so
    an availability that is still 2026-09-12 in Shanghai is inside the cutoff
    even though it is already 2026-09-12T16:00Z.
    """

    CUTOFF = "2026-09-12"

    def classify(self, **kwargs):
        return LD._classify(evidence(**kwargs), cutoff=self.CUTOFF, feature_names=FEATURES)

    # ── A. feature availability ──

    def test_feature_availability_at_local_day_end_is_allowed(self):
        sample, reason = self.classify(feature_available="2026-09-12T23:59:59+08:00")
        self.assertIsNotNone(sample, reason)

    def test_feature_availability_at_utc_equivalent_of_local_day_end_is_allowed(self):
        sample, reason = self.classify(feature_available="2026-09-12T15:59:59+00:00")
        self.assertIsNotNone(sample, reason)

    def test_feature_availability_at_160000Z_is_future(self):
        # == 2026-09-13T00:00:00+08:00, i.e. the next Shanghai calendar day.
        sample, reason = self.classify(feature_available="2026-09-12T16:00:00+00:00")
        self.assertIsNone(sample)
        self.assertEqual(reason, "future_feature")

    def test_feature_availability_at_local_next_midnight_is_future(self):
        sample, reason = self.classify(feature_available="2026-09-13T00:00:00+08:00")
        self.assertIsNone(sample)
        self.assertEqual(reason, "future_feature")

    # ── B. label availability ──

    def test_label_availability_at_local_day_end_is_allowed(self):
        sample, reason = self.classify(label_available="2026-09-12T23:59:59+08:00")
        self.assertIsNotNone(sample, reason)

    def test_label_availability_at_utc_equivalent_of_local_day_end_is_allowed(self):
        sample, reason = self.classify(label_available="2026-09-12T15:59:59+00:00")
        self.assertIsNotNone(sample, reason)

    def test_label_availability_at_160000Z_is_a_future_label(self):
        sample, reason = self.classify(label_available="2026-09-12T16:00:00+00:00")
        self.assertIsNone(sample)
        self.assertEqual(reason, "future_label")

    def test_label_availability_at_local_next_midnight_is_a_future_label(self):
        sample, reason = self.classify(label_available="2026-09-13T00:00:00+08:00")
        self.assertIsNone(sample)
        self.assertEqual(reason, "future_label")

    # ── C. offset equivalence ──

    def test_equal_instants_in_different_offsets_canonicalize_identically(self):
        self.assertEqual(
            LD._timestamp_text("2026-09-12T15:00:00+00:00"),
            LD._timestamp_text("2026-09-12T23:00:00+08:00"),
        )
        self.assertEqual(
            LD._instant("2026-09-12T15:00:00+00:00"),
            LD._instant("2026-09-12T23:00:00+08:00"),
        )

    def test_zulu_offset_and_naive_forms_share_one_instant(self):
        for text in (
            "2026-09-12T15:59:59Z",
            "2026-09-12T15:59:59+00:00",
            "2026-09-12T23:59:59+08:00",
            "2026-09-12T23:59:59",  # naive == exchange-local
        ):
            with self.subTest(text=text):
                self.assertEqual(LD._timestamp_text(text), "2026-09-12T15:59:59+00:00")

    def test_canonicalization_is_idempotent(self):
        once = LD._timestamp_text("2026-09-12T23:59:59+08:00")
        self.assertEqual(LD._timestamp_text(once), once)
        self.assertEqual(LD._instant(once), once)

    # ── D. date-only cutoff ──

    def test_date_only_cutoff_is_the_exchange_local_day_end(self):
        self.assertEqual(LD._cutoff_instant(self.CUTOFF), "2026-09-12T15:59:59+00:00")

    def test_date_only_cutoff_is_not_the_utc_day_end(self):
        self.assertNotEqual(LD._cutoff_instant(self.CUTOFF), "2026-09-12T23:59:59+00:00")
        self.assertNotEqual(LD._cutoff_instant(self.CUTOFF), "2026-09-12T23:59:59")

    def test_date_only_instant_is_the_exchange_local_day_start(self):
        self.assertEqual(LD._instant(self.CUTOFF), "2026-09-11T16:00:00+00:00")

    def test_cutoff_is_never_read_in_the_host_timezone(self):
        # The canonical cutoff must be a fixed UTC instant, independent of the
        # machine that rebuilds the dataset.
        self.assertTrue(LD._cutoff_instant(self.CUTOFF).endswith("+00:00"))

    # ── E. naive compatibility ──

    def test_naive_availability_is_read_as_exchange_local(self):
        self.assertEqual(LD._timestamp_text("2026-09-12T23:59:59"), "2026-09-12T15:59:59+00:00")
        sample, reason = self.classify(feature_available="2026-09-12T23:59:59")
        self.assertIsNotNone(sample, reason)

    def test_timezone_fix_does_not_upgrade_unproven_naive_rows(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available="2026-01-01T15:15:00",
                    pit=LD.PIT_LEGACY_UNPROVEN)
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("legacy_unproven_pit"), 1)

    def test_timezone_fix_does_not_upgrade_unproven_aware_rows(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available="2026-01-01T15:15:00+00:00",
                    pit=LD.PIT_LEGACY_UNPROVEN)
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)
        self.assertEqual(reasons(built).get("legacy_unproven_pit"), 1)

    def test_equivalent_offsets_produce_the_same_fingerprint(self):
        local, utc = self.new_db(), self.new_db()
        seed_sample(local, "2026-01-01", available="2026-01-01T15:15:00")            # exchange-local
        seed_sample(utc, "2026-01-01", available="2026-01-01T07:15:00+00:00")       # same instant
        seed_label(local, "2026-01-01", "2026-01-02")
        seed_label(utc, "2026-01-01", "2026-01-02")
        self.assertEqual(build(local).fingerprint, build(utc).fingerprint)

    # ── provenance ──

    def test_captured_provenance_declares_the_canonical_clock(self):
        provenance = LD.capture_provenance(available_at="2026-09-12T23:59:59+08:00")
        self.assertEqual(provenance["feature_available_at"], "2026-09-12T15:59:59+00:00")
        self.assertEqual(provenance["pit_status"], LD.PIT_VERIFIED)
        self.assertEqual(
            json.loads(provenance["provenance_json"])["availability_clock"], "canonical_utc"
        )

    def test_captured_provenance_without_a_timestamp_stays_unproven(self):
        provenance = LD.capture_provenance(available_at=None)
        self.assertIsNone(provenance["feature_available_at"])
        self.assertEqual(provenance["pit_status"], LD.PIT_LEGACY_UNPROVEN)
        self.assertEqual(
            json.loads(provenance["provenance_json"])["availability_clock"], "canonical_utc"
        )

    def test_dataset_provenance_declares_the_canonical_clock(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available="2026-01-01T15:15:00")
        seed_label(conn, "2026-01-01", "2026-01-02")
        built = build(conn)
        row = all_rows(built)[0]
        self.assertEqual(row.provenance["availability_clock"], "canonical_utc")


class CanonicalProvenanceTests(DbTestCase):
    """Raw evidence provenance is audit data; it may not override the contract.

    The canonical row is what enters the fingerprint, so a stale
    ``availability_clock`` in a historical row must not be able to contradict
    the canonical UTC timestamps stored right next to it.
    """

    def _one_row(self, conn):
        built = build(conn)
        rows = all_rows(built)
        self.assertEqual(len(rows), 1, reasons(built))
        return rows[0]

    def test_stale_sample_clock_cannot_override_canonical_clock(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01",
                    provenance={"availability_clock": "exchange_local_naive",
                                "sample_note": "keep-me"})
        seed_label(conn, "2026-01-01", "2026-01-02")
        row = self._one_row(conn)
        self.assertEqual(row.provenance["availability_clock"], "canonical_utc")
        self.assertEqual(row.provenance["sample_note"], "keep-me")

    def test_label_provenance_cannot_override_canonical_clock(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01")
        seed_label(conn, "2026-01-01", "2026-01-02",
                   provenance={"availability_clock": "bogus", "label_note": "keep-label-note"})
        row = self._one_row(conn)
        self.assertEqual(row.provenance["availability_clock"], "canonical_utc")
        self.assertEqual(row.provenance["label_note"], "keep-label-note")

    def test_conflicting_sample_and_label_clocks_resolve_to_the_canonical_one(self):
        conn = self.new_db()
        seed_sample(conn, "2026-01-01",
                    provenance={"availability_clock": "exchange_local_naive"})
        seed_label(conn, "2026-01-01", "2026-01-02",
                   provenance={"availability_clock": "bogus"})
        row = self._one_row(conn)
        # Not first-wins and not last-wins: the canonical layer wins.
        self.assertEqual(row.provenance["availability_clock"], "canonical_utc")

    def test_reserved_contract_fields_cannot_be_spoofed(self):
        conn = self.new_db()
        spoof = {
            "sample_contract_version": "spoofed-v0",
            "label_source": "spoofed_source",
            "label_source_version": "spoofed-v0",
            "industry": "spoofed_industry",
            "regime": "spoofed_regime",
            "audit_note": "keep-audit",
        }
        seed_sample(conn, "2026-01-01", provenance=spoof)
        seed_label(conn, "2026-01-01", "2026-01-02", provenance=spoof)
        row = self._one_row(conn)
        self.assertEqual(row.provenance["sample_contract_version"], LD.CONTRACT_VERSION)
        self.assertEqual(row.provenance["label_source"], LD.DATASET_KIND_ADAPTIVE_ALPHA)
        self.assertEqual(row.provenance["label_source_version"], "test-v1")
        self.assertEqual(row.provenance["industry"], "测试行业")
        self.assertEqual(row.provenance["regime"], "neutral")
        # Non-reserved audit provenance survives untouched.
        self.assertEqual(row.provenance["audit_note"], "keep-audit")

    def test_reserved_keys_are_exactly_the_documented_set(self):
        self.assertEqual(
            set(LD.CANONICAL_RESERVED_PROVENANCE_KEYS),
            {
                "industry",
                "regime",
                "label_source",
                "label_source_version",
                "sample_contract_version",
                "availability_clock",
            },
        )

    def test_reserved_spoof_does_not_fork_the_fingerprint(self):
        """Only the *reserved derived* field is normalized away.

        Two databases whose only difference is a spoofed reserved clock value
        must canonicalize to the same row and therefore the same fingerprint.
        """
        first, second = self.new_db(), self.new_db()
        seed_sample(first, "2026-01-01", provenance={"availability_clock": "exchange_local_naive"})
        seed_sample(second, "2026-01-01", provenance={"availability_clock": "bogus"})
        seed_label(first, "2026-01-01", "2026-01-02")
        seed_label(second, "2026-01-01", "2026-01-02")
        self.assertEqual(
            self._one_row(first).provenance["availability_clock"],
            self._one_row(second).provenance["availability_clock"],
        )
        self.assertEqual(build(first).fingerprint, build(second).fingerprint)

    def test_non_reserved_provenance_difference_still_forks_the_fingerprint(self):
        """Audit provenance is content: a real difference must change the hash."""
        first, second = self.new_db(), self.new_db()
        seed_sample(first, "2026-01-01", provenance={"sample_note": "a"})
        seed_sample(second, "2026-01-01", provenance={"sample_note": "b"})
        seed_label(first, "2026-01-01", "2026-01-02")
        seed_label(second, "2026-01-01", "2026-01-02")
        self.assertNotEqual(build(first).fingerprint, build(second).fingerprint)


class SchemaMigrationTests(DbTestCase):
    """Old DB, empty DB, test DB -- all must migrate idempotently."""

    def test_empty_database_migrates(self):
        conn = self.raw_db()
        conn.executescript(LEGACY_SCHEMA)
        first = LD.ensure_schema(conn)
        second = LD.ensure_schema(conn)
        self.assertTrue(first["manifest_table"])
        self.assertGreater(first["added_columns"], 0)
        self.assertEqual(second["added_columns"], 0)

    def test_minimal_test_database_migrates_without_crashing(self):
        conn = self.raw_db()
        conn.executescript(
            "CREATE TABLE adaptive_alpha_samples(profile_date TEXT, code TEXT);"
            "CREATE TABLE adaptive_alpha_returns(start_date TEXT, horizon INTEGER);"
        )
        LD.ensure_schema(conn)
        self.assertEqual(NS.readiness(conn)["admitted"], False)

    def test_database_without_alpha_tables_is_tolerated(self):
        conn = self.raw_db()
        LD.ensure_schema(conn)
        status = LD.contract_status(conn)
        self.assertEqual(status["dataset_blockers"], ["dataset_evidence_table_missing"])

    def test_legacy_rows_are_marked_not_invented(self):
        conn = self.raw_db()
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO adaptive_alpha_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-01-01", "000001", "A", 10.0, "neutral", .5, .5, .5, .5, .5, .5, "2026-01-01T16:00:00"),
        )
        conn.execute(
            "INSERT INTO adaptive_alpha_returns VALUES(?,?,?,?,?,?)",
            ("2026-01-01", "2026-01-02", 1, "000001", 1.0, "2026-01-02T16:00:00"),
        )
        LD.ensure_schema(conn)
        self.assertEqual(
            conn.execute("SELECT pit_status FROM adaptive_alpha_samples").fetchone()[0],
            LD.PIT_LEGACY_UNPROVEN,
        )
        self.assertIsNone(
            conn.execute("SELECT feature_available_at FROM adaptive_alpha_samples").fetchone()[0]
        )
        built = build(conn)
        self.assertEqual(built.eligible_rows, 0)

    def test_legacy_unknown_pit_does_not_become_verified(self):
        conn = self.raw_db()
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO adaptive_alpha_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-01-01", "000001", "A", 10.0, "neutral", .5, .5, .5, .5, .5, .5, "x"),
        )
        conn.execute(
            "INSERT INTO adaptive_alpha_returns VALUES(?,?,?,?,?,?)",
            ("2026-01-01", "2026-01-02", 1, "000001", 1.0, "x"),
        )
        LD.ensure_schema(conn)
        status = LD.contract_status(conn)
        self.assertIn("dataset_cutoff_unprovable", status["dataset_blockers"])
        self.assertEqual(status["pit_eligible_rows"], 0)


class ContractStatusTests(DbTestCase):
    """Row count is not readiness; the contract gate is."""

    def _bulk_legacy(self, conn, day_count=30, codes=100):
        day_list = dates(day_count)
        samples = []
        labels = []
        for index, day in enumerate(day_list):
            for code_index in range(codes):
                code = f"{code_index:06d}"
                samples.append(
                    (day, code, "A", close_base_for(index), "neutral",
                     0.5, 0.5, 0.5, 0.5, 0.5, 0.5, f"{day}T16:00:00",
                     day, None, LD.PIT_LEGACY_UNPROVEN, "legacy", "legacy", LD.CONTRACT_VERSION)
                )
        conn.executemany(
            """INSERT OR REPLACE INTO adaptive_alpha_samples(
                   profile_date,code,industry,close_price,regime,price_momentum,main_flow,
                   turnover,volume_ratio,small_size,value,created_at,
                   feature_asof,feature_available_at,pit_status,source,source_version,contract_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            samples,
        )
        for horizon in (1, 3, 5):
            for index in range(len(day_list) - horizon):
                for code_index in range(codes):
                    code = f"{code_index:06d}"
                    labels.append(
                        (day_list[index], day_list[index + horizon], horizon, code, 1.0,
                         f"{day_list[index + horizon]}T16:00:00", None,
                         LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
                         LD.PIT_LEGACY_UNPROVEN, "legacy", "legacy", LD.CONTRACT_VERSION)
                    )
        conn.executemany(
            """INSERT OR IGNORE INTO adaptive_alpha_returns(
                   start_date,end_date,horizon,code,forward_return_pct,created_at,
                   label_available_at,horizon_semantics,pit_status,source,source_version,contract_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            labels,
        )
        return day_list

    def test_row_counts_alone_never_clear_the_dataset_gate(self):
        conn = self.new_db()
        self._bulk_legacy(conn)
        ready = NS.readiness(conn)
        # Numeric sample gates are satisfied by construction ...
        self.assertGreaterEqual(ready["profile_days"], NS.MIN_PROFILE_DAYS)
        self.assertGreaterEqual(ready["label_dates"], NS.MIN_LABEL_DATES)
        self.assertGreaterEqual(ready["label_rows"], NS.MIN_LABEL_ROWS)
        self.assertEqual(ready["available_horizons"], list(NS.REQUIRED_HORIZONS))
        # ... yet the dataset contract still refuses to call this research-ready.
        self.assertTrue(ready["dataset_blockers"])
        self.assertFalse(ready["admitted"])
        self.assertEqual(ready["status"], "waiting_data")
        self.assertEqual(ready["mode"], "shadow_only")
        self.assertEqual(ready["trading_impact"], "none")
        self.assertEqual(ready["execution_authority"], "none")

    def test_proven_dataset_passes_the_gate_and_stays_shadow_only(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,), codes=("000001", "000002", "000003"))
        status = LD.contract_status(conn)
        self.assertEqual(status["dataset_blockers"], [])
        self.assertTrue(status["dataset_fingerprint"])
        self.assertGreater(status["pit_eligible_rows"], 0)
        for name in LD.PARTITIONS:
            self.assertGreater(status["split_rows"][name], 0)

    def test_truncated_read_is_not_reported_as_ready(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        status = LD.contract_status(conn, max_evidence_rows=1)
        self.assertTrue(status["truncated"])
        self.assertIn("dataset_evidence_read_truncated", status["dataset_blockers"])

    # ── D. an evidence set that is exactly the limit is not truncated ──

    def test_exact_evidence_count_is_not_reported_as_truncated(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        total = len(LD._read_alpha_evidence(conn))
        self.assertGreater(total, 0)
        built = build(conn, max_evidence_rows=total)
        self.assertFalse(built.truncated)
        self.assertEqual(built.manifest["source_row_count"], total)
        status = LD.contract_status(conn, max_evidence_rows=total)
        self.assertFalse(status["truncated"])
        self.assertNotIn("dataset_evidence_read_truncated", status["dataset_blockers"])

    def test_one_row_over_the_limit_is_reported_as_truncated(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        total = len(LD._read_alpha_evidence(conn))
        built = build(conn, max_evidence_rows=total - 1)
        self.assertTrue(built.truncated)
        self.assertEqual(built.manifest["source_row_count"], total - 1)
        status = LD.contract_status(conn, max_evidence_rows=total - 1)
        self.assertTrue(status["truncated"])
        self.assertIn("dataset_evidence_read_truncated", status["dataset_blockers"])

    def test_truncation_check_does_not_depend_on_a_full_count(self):
        """The bounded read must use LIMIT max+1, not a separate COUNT(*)."""
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        total = len(LD._read_alpha_evidence(conn))
        evidence, truncated = LD._read_alpha_evidence_page(conn, max_rows=total)
        self.assertEqual(len(evidence), total)
        self.assertFalse(truncated)
        evidence, truncated = LD._read_alpha_evidence_page(conn, max_rows=total - 1)
        self.assertEqual(len(evidence), total - 1)
        self.assertTrue(truncated)

    def test_contract_status_never_writes_a_manifest(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        LD.contract_status(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM learning_dataset_manifests").fetchone()[0], 0
        )

    def test_contract_layer_error_fails_closed(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        with mock.patch.object(LD, "build_dataset", side_effect=RuntimeError("boom")):
            status = LD.contract_status(conn)
        self.assertEqual(status["dataset_blockers"], ["dataset_contract_error"])
        self.assertIsNone(status["dataset_fingerprint"])


class CutoffPrecisionContractTests(DbTestCase):
    """A cutoff keeps the precision it was given -- it is never silently widened.

    PR-8 fixed one half of the cutoff contract: a **date-only** cutoff means the
    *end* of that exchange-local day.  This class pins the other half.  A full
    timestamp is an **exact** freeze instant: it is canonicalised onto the UTC
    clock and is never expanded back to the end of its day, because doing so
    imports evidence that only became available after the freeze.

    Concretely, the regression this guards: a dataset frozen at
    ``2026-01-02T10:00:00+08:00`` used to degrade its cutoff to the bare date
    ``2026-01-02``, which reads as 23:59:59 Shanghai -- so a label published at
    14:00 that same afternoon was admitted even though the caller had frozen the
    dataset four hours earlier.  Precision is therefore part of the dataset
    identity, and an unparseable explicit cutoff fails closed.
    """

    # 2026-01-02T10:00:00+08:00 == 2026-01-02T02:00:00+00:00
    EXACT = "2026-01-02T10:00:00+08:00"
    EXACT_UTC = "2026-01-02T02:00:00+00:00"
    # The synthetic ``evidence()`` fixture lives on 2026-09-11/12, so the
    # classify-level tests need the same freeze instant on *that* day.
    EXACT_SEP = "2026-09-12T10:00:00+08:00"

    def _late_label_db(self):
        """One sample plus a label that only appears at 14:00 Shanghai time.

        The label becomes available at 06:00Z, i.e. *after* a 10:00 Shanghai
        freeze but well *before* the end of that same day -- exactly the evidence
        a widened cutoff would wrongly import.
        """
        conn = self.new_db()
        seed_sample(conn, "2026-01-01", available="2026-01-01T15:15:00")
        seed_label(conn, "2026-01-01", "2026-01-02", available="2026-01-02T14:00:00")
        return conn

    def _classify_at(self, cutoff, **overrides):
        fields = {"label_available": "2026-09-12T09:45:00+08:00"}
        fields.update(overrides)
        return LD._classify(evidence(**fields), cutoff=cutoff, feature_names=FEATURES)

    # ── A. the date-only reading is unchanged ──

    def test_date_only_cutoff_still_means_the_exchange_local_day_end(self):
        conn = self._late_label_db()
        built = build(conn, cutoff="2026-01-02")
        self.assertEqual(built.cutoff, "2026-01-02")
        self.assertEqual(built.eligible_rows, 1, reasons(built))
        self.assertEqual(built.manifest["cutoff"], "2026-01-02")

    def test_date_only_cutoff_is_not_canonicalised_into_a_timestamp(self):
        conn = self._late_label_db()
        built = build(conn, cutoff="2026-01-02")
        self.assertNotIn("T", built.cutoff)
        self.assertNotEqual(built.cutoff, "2026-01-02T15:59:59+00:00")

    # ── B. a timestamp is an exact instant ──

    def test_a_timestamp_cutoff_is_not_degraded_to_a_date(self):
        conn = self._late_label_db()
        built = build(conn, cutoff=self.EXACT)
        self.assertEqual(built.cutoff, self.EXACT_UTC)
        self.assertNotEqual(built.cutoff, "2026-01-02")

    def test_a_label_published_after_the_exact_cutoff_is_never_imported(self):
        """The leakage repro: same-day evidence, after the freeze, must be out."""
        conn = self._late_label_db()
        exact = build(conn, cutoff=self.EXACT)
        self.assertEqual(exact.eligible_rows, 0)
        self.assertEqual(reasons(exact).get("future_label"), 1)
        # The very same evidence *is* inside the day, which is why the date-only
        # reading legitimately admits it.  The two precisions stay distinct.
        self.assertNotEqual(exact.fingerprint, build(conn, cutoff="2026-01-02").fingerprint)

    def test_evidence_available_before_the_exact_cutoff_is_accepted(self):
        sample, reason = self._classify_at(
            self.EXACT_SEP, feature_available="2026-09-12T09:30:00+08:00"
        )
        self.assertIsNotNone(sample, reason)

    def test_evidence_available_after_the_exact_cutoff_is_excluded(self):
        sample, reason = self._classify_at(
            self.EXACT_SEP, feature_available="2026-09-12T10:30:00+08:00"
        )
        self.assertIsNone(sample)
        self.assertEqual(reason, "future_feature")

    def test_evidence_available_exactly_at_the_exact_cutoff_is_accepted(self):
        sample, reason = self._classify_at(
            self.EXACT_SEP, feature_available="2026-09-12T10:00:00+08:00"
        )
        self.assertIsNotNone(sample, reason)

    def test_a_label_available_exactly_at_the_exact_cutoff_is_accepted(self):
        sample, reason = self._classify_at(
            self.EXACT_SEP,
            feature_available="2026-09-12T09:30:00+08:00",
            label_available="2026-09-12T10:00:00+08:00",
        )
        self.assertIsNotNone(sample, reason)

    def test_day_precise_asof_is_not_called_future_by_an_intraday_cutoff(self):
        """``feature_asof`` is day-precision: compare it on the clock, not as text."""
        cutoff = "2026-09-12T00:00:01+08:00"
        canonical = LD._normalize_cutoff(cutoff)
        self.assertEqual(canonical, "2026-09-11T16:00:01+00:00")
        _, reason = LD._classify(
            evidence(
                asof="2026-09-12",
                feature_available="2026-09-12T00:00:00+08:00",
                label_start="2026-09-12",
                label_end="2026-09-13",
                label_available="2026-09-12T00:00:00+08:00",
            ),
            cutoff=canonical,
            feature_names=FEATURES,
        )
        # The day *began* one second before the freeze, so the asof is inside it.
        # A raw string compare of "2026-09-12" against the canonical instant
        # would have refused it here; it must instead fall through to the honest
        # reason -- the label is not mature yet.
        self.assertNotEqual(reason, "future_or_invalid_asof")
        self.assertEqual(reason, "invalid_label_time")
        # A genuinely later day is still refused, and for the asof reason.
        _, later = LD._classify(
            evidence(
                asof="2026-09-13",
                feature_available="2026-09-12T00:00:00+08:00",
                label_start="2026-09-12",
                label_end="2026-09-14",
                label_available="2026-09-12T00:00:00+08:00",
            ),
            cutoff=canonical,
            feature_names=FEATURES,
        )
        self.assertEqual(later, "future_or_invalid_asof")

    # ── C. equivalent spellings collapse to one identity ──

    def test_equivalent_timestamp_forms_share_one_dataset_identity(self):
        conn = self._late_label_db()
        forms = (self.EXACT, "2026-01-02T02:00:00Z", self.EXACT_UTC, "2026-01-02T10:00:00")
        built = [build(conn, cutoff=form) for form in forms]
        self.assertEqual({row.cutoff for row in built}, {self.EXACT_UTC})
        self.assertEqual(len({row.fingerprint for row in built}), 1)
        self.assertEqual({row.eligible_rows for row in built}, {0})

    def test_a_naive_timestamp_is_read_as_exchange_local(self):
        self.assertEqual(LD.normalize_cutoff("2026-01-02T10:00:00"), self.EXACT_UTC)
        conn = self._late_label_db()
        self.assertEqual(
            build(conn, cutoff="2026-01-02T10:00:00").fingerprint,
            build(conn, cutoff=self.EXACT).fingerprint,
        )

    def test_the_freeze_boundary_is_the_exact_instant(self):
        conn = self._late_label_db()
        # The label matures at 06:00Z: one second earlier is too soon, the exact
        # instant itself is inside the freeze.
        before = build(conn, cutoff="2026-01-02T13:59:59+08:00")   # 05:59:59Z
        on = build(conn, cutoff="2026-01-02T14:00:00+08:00")       # 06:00:00Z
        self.assertEqual(before.eligible_rows, 0)
        self.assertEqual(on.eligible_rows, 1, reasons(on))
        self.assertNotEqual(before.fingerprint, on.fingerprint)

    # ── D. manifest / persistence ──

    def test_manifest_records_the_exact_cutoff(self):
        conn = self._late_label_db()
        self.assertEqual(build(conn, cutoff=self.EXACT).manifest["cutoff"], self.EXACT_UTC)

    def test_persisted_manifest_round_trips_the_exact_cutoff(self):
        conn = self._late_label_db()
        built = build(conn, cutoff=self.EXACT, persist=True)
        stored = LD.read_manifest(conn, built.fingerprint)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["cutoff"], self.EXACT_UTC)

    # ── E. fail closed ──

    def test_an_invalid_explicit_cutoff_raises_instead_of_widening(self):
        conn = self._late_label_db()
        for bad in ("not-a-date", "2026-13-45", "2026-02-30", ""):
            with self.subTest(cutoff=bad):
                with self.assertRaises(ValueError):
                    build(conn, cutoff=bad)

    def test_an_invalid_explicit_cutoff_is_unprovable_in_the_gate(self):
        conn = self._late_label_db()
        status = LD.contract_status(conn, cutoff="not-a-date")
        self.assertIsNone(status["cutoff"])
        self.assertIsNone(status["dataset_fingerprint"])
        self.assertIn("dataset_cutoff_unprovable", status["dataset_blockers"])
        # No fallback: it must not quietly become the latest provable day.
        self.assertEqual(LD._latest_provable_cutoff(conn), "2026-01-01")
        self.assertNotEqual(status["cutoff"], LD._latest_provable_cutoff(conn))

    def test_the_gate_still_defaults_to_the_latest_provable_cutoff(self):
        conn = self._late_label_db()
        status = LD.contract_status(conn)
        self.assertEqual(status["cutoff"], "2026-01-01")
        self.assertNotIn("dataset_cutoff_unprovable", status["dataset_blockers"])

    def test_the_gate_preserves_the_exact_cutoff_instant(self):
        conn = self._late_label_db()
        status = LD.contract_status(conn, cutoff=self.EXACT)
        self.assertEqual(status["cutoff"], self.EXACT_UTC)
        self.assertNotEqual(status["cutoff"], "2026-01-02")

    def test_a_subsecond_cutoff_is_refused_rather_than_rounded(self):
        """Sub-second precision cannot be held, so it must not be rounded away.

        The PIT clock formats every instant with ``timespec="seconds"``.  If a
        finer cutoff were accepted, two *distinct* freeze instants would both
        round to the same second and share one dataset fingerprint -- silently
        collapsing distinct identities.  The contract refuses such a cutoff at
        both gates instead of choosing a precision for the caller.
        """
        conn = self._late_label_db()
        finer = (
            "2026-01-02T10:00:00.100000+08:00",
            "2026-01-02T10:00:00.900000+08:00",
        )
        for bad in finer:
            with self.subTest(cutoff=bad):
                with self.assertRaises(ValueError):
                    build(conn, cutoff=bad)
                status = LD.contract_status(conn, cutoff=bad)
                self.assertIsNone(status["cutoff"])
                self.assertIsNone(status["dataset_fingerprint"])
                self.assertIn("dataset_cutoff_unprovable", status["dataset_blockers"])

    def test_two_distinct_subsecond_cutoffs_do_not_share_an_identity(self):
        """A refused cutoff yields no identity -- never a collapsed one."""
        # These two instants differ by 0.8s, below the canonical resolution.
        left = "2026-01-02T10:00:00.100000+08:00"
        right = "2026-01-02T10:00:00.900000+08:00"
        self.assertIsNone(LD.normalize_cutoff(left))
        self.assertIsNone(LD.normalize_cutoff(right))
        # The second-precision spellings of the *same* wall time stay valid and
        # identical, so nothing about the coarse contract regressed.
        self.assertEqual(LD.normalize_cutoff(self.EXACT), self.EXACT_UTC)

    # ── F. the normalisation helper ──

    def test_normalize_cutoff_is_idempotent_and_fails_closed(self):
        for value, expected in (
            ("2026-01-02", "2026-01-02"),
            ("2026/01/02", "2026-01-02"),
            (self.EXACT, self.EXACT_UTC),
            ("2026-01-02T02:00:00Z", self.EXACT_UTC),
            ("2026-01-02T10:00:00", self.EXACT_UTC),
        ):
            with self.subTest(value=value):
                self.assertEqual(LD.normalize_cutoff(value), expected)
                self.assertEqual(LD.normalize_cutoff(expected), expected)
        for bad in ("not-a-date", "2026-13-45", "2026-02-30", "", "   ", None):
            with self.subTest(bad=bad):
                self.assertIsNone(LD.normalize_cutoff(bad))

    def test_the_public_helper_matches_the_internal_one(self):
        for value in ("2026-01-02", self.EXACT, "2026-01-02T02:00:00Z", "nonsense", None):
            self.assertEqual(LD.normalize_cutoff(value), LD._normalize_cutoff(value), repr(value))

    # ── G. the host clock never leaks in ──

    def test_cutoff_precision_does_not_depend_on_the_host_timezone(self):
        conn = self._late_label_db()
        baseline = build(conn, cutoff=self.EXACT)
        self.assertEqual(baseline.cutoff, self.EXACT_UTC)
        original_tz = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York", "Asia/Kolkata", "Pacific/Kiritimati"):
                with self.subTest(tz=zone):
                    with mock.patch.dict(os.environ, {"TZ": zone}):
                        if hasattr(time, "tzset"):
                            time.tzset()
                        observed = build(conn, cutoff=self.EXACT)
                    # ``mock.patch.dict`` has now restored the environment, so
                    # re-sync the libc clock *outside* the patch.  Calling
                    # ``tzset()`` only inside the block would leave the last
                    # patched zone installed for every later test that reads
                    # ``time.localtime()`` / ``datetime.now()``.
                    if hasattr(time, "tzset"):
                        time.tzset()
                    self.assertEqual(observed.cutoff, baseline.cutoff)
                    self.assertEqual(observed.fingerprint, baseline.fingerprint)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            if hasattr(time, "tzset"):
                time.tzset()


class NoNetworkTests(DbTestCase):
    """The builder consumes persisted evidence only."""

    def test_dataset_build_never_touches_the_network(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        with mock.patch("socket.socket", side_effect=AssertionError("network during build")):
            with mock.patch("socket.create_connection", side_effect=AssertionError("network")):
                with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                    built = build(conn)
        self.assertTrue(built.eligible_rows)

    def test_builder_has_no_provider_dependency(self):
        self.assertEqual(LD.forbidden_dependencies(), [])

    def test_source_has_no_network_module_import(self):
        tree = ast.parse(Path(LD.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        for forbidden in ("requests", "urllib", "socket", "httpx", "aiohttp", "http"):
            self.assertNotIn(forbidden, imported)
        for forbidden in ("adaptive_engine", "paper_trading", "order_intent", "strategy_registry"):
            self.assertNotIn(forbidden, imported)


class NoExecutionTests(DbTestCase):
    """Building a dataset must not mutate trading state or submit orders."""

    def test_build_is_read_only_over_the_database(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        before = conn.total_changes
        build(conn)
        self.assertEqual(conn.total_changes, before)

    def test_source_has_no_order_submission_calls(self):
        tree = ast.parse(Path(LD.__file__).read_text(encoding="utf-8"))
        forbidden = {
            "submit_order", "submit_manual_order", "cancel_manual_order",
            "execute_open", "_execute_manual_plan", "_commit_strategy_buy",
        }
        calls = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(forbidden.isdisjoint(calls), sorted(forbidden & calls))

    def test_module_keeps_research_only_identity(self):
        conn = self.new_db()
        seed_series(conn, dates(12), horizons=(1,))
        manifest = build(conn).manifest
        self.assertEqual(manifest["execution_authority"], "none")
        self.assertEqual(manifest["dataset_scope"], "research_read_only")

    def test_neural_gate_never_changes_trading_behaviour(self):
        conn = self.new_db()
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS adaptive_config(key TEXT PRIMARY KEY, value TEXT);"
        )
        seed_series(conn, dates(12), horizons=(1,))
        state = NS.control_status(conn)
        self.assertEqual(state["mode"], "shadow_only")
        self.assertEqual(state["trading_impact"], "none")
        self.assertTrue(state["hard_gates_unchanged"])


if __name__ == "__main__":
    unittest.main()
