"""R29-FINAL raw financial owner and field-lineage regressions."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parent)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import experiment_contract as EC  # noqa: E402
import experiment_pit_validation as PV  # noqa: E402
import financial_feature_evidence as FFE  # noqa: E402
import historical_financial_archive as HFA  # noqa: E402
import learning_dataset as LD  # noqa: E402
import strategy_dsl_schema as DSL  # noqa: E402
import strategy_registry as SR  # noqa: E402
import walk_forward_validation as WFV  # noqa: E402


def _records(*, roe_publication="2025-12-31T16:00:00+08:00", prior_revenue=100,
             current_revenue=125, prior_profit=20, current_profit=30):
    return [
        {"code": "600000.SH", "report_period": "2024-12-31",
         "published_at": "2025-03-20T16:00:00+08:00", "publication_precision": "instant",
         "record_type": "annual_report", "financial_fields": {
             "revenue": prior_revenue, "net_profit": prior_profit,
             "roe": 8.0, "gross_profit": 40, "total_liabilities": 60, "total_assets": 100}},
        {"code": "600000.SH", "report_period": "2025-12-31",
         "published_at": roe_publication, "publication_precision": "instant",
         "record_type": "annual_report", "financial_fields": {
             "revenue": current_revenue, "net_profit": current_profit,
             "roe": 12.5, "gross_profit": 50, "total_liabilities": 75, "total_assets": 125}},
    ]


class FinancialOwnerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE adaptive_alpha_samples(
          profile_date TEXT NOT NULL, code TEXT NOT NULL, industry TEXT,
          close_price REAL NOT NULL, regime TEXT NOT NULL,
          price_momentum REAL NOT NULL, main_flow REAL NOT NULL, turnover REAL NOT NULL,
          volume_ratio REAL NOT NULL, small_size REAL NOT NULL, value REAL NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(profile_date,code));
        CREATE TABLE adaptive_alpha_returns(
          start_date TEXT NOT NULL, end_date TEXT NOT NULL, horizon INTEGER NOT NULL,
          code TEXT NOT NULL, forward_return_pct REAL NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(start_date,end_date,horizon,code));
        """)
        LD.ensure_schema(self.conn)
        for index in range(6):
            day = f"2026-01-0{index + 1}"
            self.conn.execute("""INSERT INTO adaptive_alpha_samples
              (profile_date,code,industry,close_price,regime,price_momentum,main_flow,turnover,
               volume_ratio,small_size,value,created_at,feature_asof,feature_available_at,
               pit_status,source,source_version,contract_version,provenance_json,roe,
               gross_margin,debt_ratio,revenue_yoy,profit_yoy)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (day, "600000.SH", "bank", 10.0, "neutral", 0.1, 0.2, 0.3, 0.4, 0.5, 0.6,
               day + "T16:00:00", day, day + "T15:30:00+08:00", "verified",
               "fixture", "fixture-v1", LD.CONTRACT_VERSION, "{}", 12.5, 40.0, 60.0, 25.0, 50.0))
            if index < 5:
                next_day = f"2026-01-0{index + 2}"
                self.conn.execute("""INSERT INTO adaptive_alpha_returns
                  (start_date,end_date,horizon,code,forward_return_pct,created_at,
                   label_available_at,horizon_semantics,pit_status,source,source_version,
                   contract_version,provenance_json)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (day, next_day, 1, "600000.SH", 1.0, next_day + "T16:00:00",
                   next_day + "T15:30:00+08:00", LD.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS,
                   "verified", LD.DATASET_KIND_ADAPTIVE_ALPHA, "fixture-v1", LD.CONTRACT_VERSION, "{}"))
        self.archive = HFA.HistoricalFinancialArchiveRepository(self.conn)
        self.archive_fp = self.archive.import_records(source="trusted-export", source_revision="2026-09-v1",
                                                      records=_records())
        self.features = FFE.FinancialFeatureEvidenceRepository(self.conn, self.archive)
        self.build = LD.build_dataset(self.conn, cutoff="2026-03-31T23:59:59+08:00",
                                      feature_names=("roe", "gross_margin", "debt_ratio", "revenue_yoy", "profit_yoy"),
                                      financial_feature_repository=self.features,
                                      financial_archive_fingerprint=self.archive_fp,
                                      persist=True)
        self.sample = next(sample for partition in LD.PARTITIONS
                           for sample in self.build.partitions[partition])
        self.addCleanup(self.conn.close)

    def test_FIN_01_generic_verified_sample_without_field_refs_is_not_evidence(self):
        old = LD.build_dataset(self.conn, cutoff="2026-03-31T23:59:59+08:00",
                               feature_names=("roe",), persist=True)
        sample = next(row for part in LD.PARTITIONS for row in old.partitions[part])
        self.assertEqual("verified", sample.pit_status)
        self.assertEqual({}, dict(sample.financial_evidence_refs))
        with self.assertRaises(FFE.FinancialFeatureEvidenceError):
            self.features.resolve_for_dataset_sample(
                dataset_fingerprint=old.fingerprint, sample_key=sample.sample_key,
                feature_name="roe", financial_archive_fingerprint=self.archive_fp)

    def test_FIN_02_caller_record_mapping_cannot_prove_lineage(self):
        ast = {"op": "strategy", "rule": {"op": "gt", "left": {"op": "field", "name": "roe"},
                                              "right": {"op": "const", "value": 1}}}
        version = SR.StrategyVersion("trend_pullback", 1, DSL.checksum(ast),
                                     {"dsl_ast": ast}, "2026-01-01T00:00:00Z", "test")
        spec = EC.ExperimentSpec(
            strategy=EC.StrategyIdentity("trend_pullback", 1, version.checksum),
            code_revision="b" * 40, dataset_fingerprint=self.build.fingerprint,
            universe_fingerprint="d" * 64, tradability_fingerprint="e" * 64,
            market_data_fingerprint="f" * 64,
            parameter_set={"financial_archive_fingerprint": self.archive_fp},
            start_date="2026-01-01", end_date="2026-01-06",
            asof_policy={"policy_id": "test-close-v1", "cutoff": "2026-03-31T23:59:59Z"},
            execution_assumptions={"execution_profile_version": "x", "fill_assumptions": {"signal": "close"},
                                   "t_plus_one_semantics": "x", "price_limit_semantics": "x",
                                   "partial_fill_semantics": "x", "capacity_assumptions": {"rate": 0.1}},
            cost_model={"commission_rate": 0, "minimum_commission": 0, "stamp_duty_rate": 0,
                        "slippage_model": "fixed-rate-v1", "slippage_parameters": {"rate": 0}, "version": "v1"},
            random_seed=1)
        sample = WFV.ValidationSample(
            sample_key=self.sample.sample_key, code=self.sample.code,
            decision_session=self.sample.feature_asof, decision_at=self.sample.feature_available_at,
            label_available_at=self.sample.label_available_at, target=self.sample.target,
            features=dict(self.sample.features), pit_status="verified")
        evidence = PV.build_pit_validation_evidence(
            spec, strategy_version=version, dataset_manifest=self.build.manifest,
            samples=[sample], fundamental_records=[{"record": _records()[-1],
                                                   "sample_keys": [sample.sample_key]}],
            financial_archive_fingerprint=self.archive_fp,
        )
        self.assertEqual("blocked", evidence.dimensions["fundamental_pit"]["status"])
        self.assertEqual("financial_feature_evidence_missing",
                         evidence.dimensions["fundamental_pit"]["reason_code"])
        owned = PV.build_pit_validation_evidence(
            spec, strategy_version=version, dataset_manifest=self.build.manifest,
            samples=[sample], financial_feature_repository=self.features,
            financial_archive_fingerprint=self.archive_fp,
        )
        self.assertEqual("proven", owned.dimensions["fundamental_pit"]["status"])
        mismatched_sample = WFV.ValidationSample(
            sample_key=sample.sample_key, code=sample.code,
            decision_session=sample.decision_session, decision_at="2026-01-02T15:30:00+08:00",
            label_available_at=sample.label_available_at, target=sample.target,
            features=dict(sample.features), pit_status="verified")
        mismatched = PV.build_pit_validation_evidence(
            spec, strategy_version=version, dataset_manifest=self.build.manifest,
            samples=[mismatched_sample], financial_feature_repository=self.features,
            financial_archive_fingerprint=self.archive_fp,
        )
        self.assertEqual("financial_feature_decision_mismatch",
                         mismatched.dimensions["fundamental_pit"]["reason_code"])

    def test_FIN_03_to_05_exact_sample_field_value_and_evidence_identity(self):
        evidence = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="roe", financial_archive_fingerprint=self.archive_fp)
        self.assertEqual("proven", evidence.verification)
        self.assertEqual(self.sample.sample_key, evidence.sample_key)
        self.assertEqual("roe", evidence.feature_name)
        self.assertEqual(12.5, evidence.feature_value)
        self.assertEqual(self.sample.feature_available_at, evidence.decision_at)
        with self.assertRaises(FFE.FinancialFeatureEvidenceError):
            self.features.resolve_for_dataset_sample(
                dataset_fingerprint=self.build.fingerprint, sample_key="missing-sample",
                feature_name="roe", financial_archive_fingerprint=self.archive_fp)

    def test_FIN_06_to_10_future_and_date_only_publication_fail_closed(self):
        for published, precision, expected in (
            ("2026-01-02T16:00:00+08:00", "instant", "financial_record_unavailable"),
            ("2026-01-01", "date", "financial_publication_unproven"),
        ):
            archive_fp = self.archive.import_records(
                source="trusted-export", source_revision=published,
                records=[{**_records()[-1], "published_at": published,
                          "publication_precision": precision}],
            )
            build = LD.build_dataset(self.conn, cutoff="2026-03-31T23:59:59+08:00",
                                     feature_names=("roe", "gross_margin", "debt_ratio",
                                                    "revenue_yoy", "profit_yoy"),
                                     financial_feature_repository=self.features,
                                     financial_archive_fingerprint=archive_fp, persist=True)
            sample = next(row for part in LD.PARTITIONS for row in build.partitions[part])
            evidence = self.features.resolve_for_dataset_sample(
                dataset_fingerprint=build.fingerprint, sample_key=sample.sample_key,
                feature_name="roe", financial_archive_fingerprint=archive_fp)
            self.assertEqual("blocked", evidence.verification)
            self.assertEqual(expected, evidence.reason_code)
        with self.assertRaises(HFA.HistoricalFinancialArchiveError):
            self.archive.import_records(source="trusted-export", source_revision="no-pub-v1",
                records=[{"code": "600000.SH", "report_period": "2025-12-31",
                          "record_type": "annual_report", "financial_fields": {"roe": 12.5}}])

    def test_FIN_11_deterministic_single_record_derivation(self):
        first = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="gross_margin", financial_archive_fingerprint=self.archive_fp)
        second = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="gross_margin", financial_archive_fingerprint=self.archive_fp)
        self.assertEqual("proven", first.verification)
        self.assertEqual(40.0, first.derived_value)
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)

    def test_FIN_12_to_14_yoy_requires_both_records_and_max_availability(self):
        value = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="revenue_yoy", financial_archive_fingerprint=self.archive_fp)
        self.assertEqual("revenue_yoy_comparable_period_v1", value.derivation_version)
        self.assertEqual(25.0, value.derived_value)
        self.assertEqual(2, len(value.input_record_fingerprints))
        prior_only = self.archive.import_records(source="trusted-export", source_revision="prior-only",
            records=[_records()[0]])
        build = LD.build_dataset(self.conn, cutoff="2026-03-31T23:59:59+08:00",
                                 feature_names=("roe", "gross_margin", "debt_ratio",
                                                "revenue_yoy", "profit_yoy"),
                                 financial_feature_repository=self.features,
                                 financial_archive_fingerprint=prior_only, persist=True)
        sample = next(row for part in LD.PARTITIONS for row in build.partitions[part])
        blocked = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=build.fingerprint, sample_key=sample.sample_key,
            feature_name="revenue_yoy", financial_archive_fingerprint=prior_only)
        self.assertEqual("financial_record_unavailable", blocked.reason_code)

    def test_FIN_15_to_17_derivation_version_and_archive_are_content_addressed(self):
        archive_again = HFA.HistoricalFinancialArchiveRepository(sqlite3.connect(":memory:"))
        self.addCleanup(archive_again.conn.close)
        fp = archive_again.import_records(source="trusted-export", source_revision="2026-09-v1",
                                          records=_records())
        self.assertEqual(self.archive_fp, fp)
        refs = self.archive.records(self.archive_fp)
        self.assertEqual(2, len(refs))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE historical_financial_records SET source_revision='fake'")
        self.assertNotEqual(FFE._sha({"derivation": "roe_v1"}),
                            FFE._sha({"derivation": "roe_v2"}))

    def test_FIN_18_old_dataset_without_exact_field_stays_unavailable(self):
        old = LD.build_dataset(self.conn, cutoff="2026-03-31T23:59:59+08:00",
                               feature_names=("roe",), persist=True)
        sample = next(row for part in LD.PARTITIONS for row in old.partitions[part])
        with self.assertRaises(FFE.FinancialFeatureEvidenceError):
            self.features.resolve_for_dataset_sample(
                dataset_fingerprint=old.fingerprint, sample_key=sample.sample_key,
                feature_name="roe", financial_archive_fingerprint=self.archive_fp)

    def test_FIN_19_price_only_has_no_financial_owner_requirement(self):
        deps = PV.strategy_dsl_dependencies({"op": "strategy", "rule": {
            "op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "const", "value": 1}}})
        self.assertEqual([], deps["financial_fields"])

    def test_FIN_20_pe_pb_are_explicitly_unsupported(self):
        for field in ("pe", "pb"):
            with self.subTest(field=field):
                item = FFE._derive(field, self.archive.records(self.archive_fp, code="600000.SH"),
                                   self.sample.feature_available_at)
                self.assertEqual("financial_feature_derivation_unsupported", item[2])

    def _build_for_archive(self, archive_fp, *, features=("roe", "gross_margin", "debt_ratio",
                                                          "revenue_yoy", "profit_yoy")):
        return LD.build_dataset(
            self.conn, cutoff="2026-03-31T23:59:59+08:00", feature_names=features,
            financial_feature_repository=self.features,
            financial_archive_fingerprint=archive_fp, persist=True,
        )

    def test_FIN_04_field_name_is_bound_and_cannot_be_relabelled(self):
        roe = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="roe", financial_archive_fingerprint=self.archive_fp)
        yoy = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="profit_yoy", financial_archive_fingerprint=self.archive_fp)
        self.assertEqual("roe", roe.feature_name)
        self.assertEqual("profit_yoy", yoy.feature_name)
        self.assertNotEqual(roe.evidence_fingerprint, yoy.evidence_fingerprint)

    def test_FIN_05_dataset_value_mismatch_is_blocked(self):
        self.conn.execute("UPDATE adaptive_alpha_samples SET roe=99")
        changed = self._build_for_archive(self.archive_fp)
        sample = next(row for part in LD.PARTITIONS for row in changed.partitions[part])
        evidence = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=changed.fingerprint, sample_key=sample.sample_key,
            feature_name="roe", financial_archive_fingerprint=self.archive_fp)
        self.assertEqual("blocked", evidence.verification)
        self.assertEqual("financial_feature_value_mismatch", evidence.reason_code)
        self.assertEqual(99.0, evidence.feature_value)
        self.assertEqual(12.5, evidence.derived_value)

    def test_FIN_07_same_day_later_instant_is_future(self):
        records = _records(roe_publication="2026-01-01T16:00:00+08:00")[-1:]
        archive_fp = self.archive.import_records(source="trusted-export", source_revision="same-day-later-v1",
                                                 records=records)
        build = self._build_for_archive(archive_fp)
        sample = next(row for part in LD.PARTITIONS for row in build.partitions[part])
        evidence = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=build.fingerprint, sample_key=sample.sample_key,
            feature_name="roe", financial_archive_fingerprint=archive_fp)
        self.assertEqual("financial_record_unavailable", evidence.reason_code)

    def test_FIN_08_date_only_same_day_is_unproven_at_intraday_decision(self):
        record = {**_records()[-1], "published_at": "2026-01-01", "publication_precision": "date"}
        archive_fp = self.archive.import_records(source="trusted-export", source_revision="date-only-v1",
                                                 records=[record])
        build = self._build_for_archive(archive_fp)
        sample = next(row for part in LD.PARTITIONS for row in build.partitions[part])
        evidence = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=build.fingerprint, sample_key=sample.sample_key,
            feature_name="roe", financial_archive_fingerprint=archive_fp)
        self.assertEqual("financial_publication_unproven", evidence.reason_code)

    def test_FIN_09_missing_publication_cannot_enter_archive(self):
        record = {key: value for key, value in _records()[-1].items() if key != "published_at"}
        with self.assertRaises(HFA.HistoricalFinancialArchiveError):
            self.archive.import_records(source="trusted-export", source_revision="missing-pub-v1",
                                        records=[record])

    def test_FIN_10_report_period_is_not_a_publication_fallback(self):
        record = {key: value for key, value in _records()[-1].items() if key != "published_at"}
        record["report_period"] = "2025-12-31"
        with self.assertRaises(HFA.HistoricalFinancialArchiveError):
            self.archive.import_records(source="trusted-export", source_revision="report-period-only-v1",
                                        records=[record])

    def test_FIN_13_future_yoy_input_blocks_whole_feature(self):
        rows = _records()
        rows[0]["published_at"] = "2026-01-02T16:00:00+08:00"
        archive_fp = self.archive.import_records(source="trusted-export", source_revision="future-prior-v1",
                                                 records=rows)
        build = self._build_for_archive(archive_fp)
        sample = next(row for part in LD.PARTITIONS for row in build.partitions[part])
        evidence = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=build.fingerprint, sample_key=sample.sample_key,
            feature_name="revenue_yoy", financial_archive_fingerprint=archive_fp)
        self.assertEqual("blocked", evidence.verification)
        self.assertEqual("financial_record_unavailable", evidence.reason_code)

    def test_FIN_14_feature_availability_is_latest_required_input(self):
        evidence = self.features.resolve_for_dataset_sample(
            dataset_fingerprint=self.build.fingerprint, sample_key=self.sample.sample_key,
            feature_name="revenue_yoy", financial_archive_fingerprint=self.archive_fp)
        publications = [item["published_at"] for item in evidence.input_available_at]
        self.assertEqual(2, len(publications))
        self.assertEqual("2025-12-31T08:00:00+00:00", evidence.feature_available_at)
        self.assertEqual(max(publications), "2025-12-31T08:00:00+00:00")

    def test_FIN_15_derivation_version_changes_dataset_and_evidence_identity(self):
        original_ref = self.sample.financial_evidence_refs["roe"]
        original_derivation = FFE._DERIVATIONS["roe"]
        try:
            FFE._DERIVATIONS["roe"] = "roe_reported_v2"
            changed = self._build_for_archive(self.archive_fp)
            sample = next(row for part in LD.PARTITIONS for row in changed.partitions[part])
            self.assertNotEqual(self.build.fingerprint, changed.fingerprint)
            self.assertNotEqual(original_ref, sample.financial_evidence_refs["roe"])
            evidence = self.features.resolve_for_dataset_sample(
                dataset_fingerprint=changed.fingerprint, sample_key=sample.sample_key,
                feature_name="roe", financial_archive_fingerprint=self.archive_fp)
            self.assertEqual("roe_reported_v2", evidence.derivation_version)
        finally:
            FFE._DERIVATIONS["roe"] = original_derivation

    def test_FIN_16_record_fingerprint_is_stable_across_imports(self):
        one = self.archive.records(self.archive_fp)
        other_conn = sqlite3.connect(":memory:")
        other = HFA.HistoricalFinancialArchiveRepository(other_conn)
        other_fp = other.import_records(source="trusted-export", source_revision="2026-09-v1",
                                        records=_records())
        self.assertEqual(self.archive_fp, other_fp)
        self.assertEqual([row.record_fingerprint for row in one],
                         [row.record_fingerprint for row in other.records(other_fp)])
        other_conn.close()

    def test_FIN_17_changed_content_under_saved_fingerprint_is_rejected(self):
        self.conn.execute("DROP TRIGGER historical_financial_records_no_update")
        self.conn.execute("UPDATE historical_financial_records SET financial_fields_json='{}'")
        with self.assertRaises(HFA.HistoricalFinancialArchiveError):
            self.archive.records(self.archive_fp)


if __name__ == "__main__":
    unittest.main()
