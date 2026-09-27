"""Focused R29-FINAL regression coverage for new immutable owner boundaries."""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
import unittest

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import experiment_validation_repository as EVR
import historical_market_archive as HMA
import historical_session_calendar as HSC
import historical_universe_archive as HUA
import experiment_pit_validation as PV
import test_experiment_pit_validation as PIT_TESTS
import tradability_archive as TA


def _bar(code: str, session: str, close: float = 10.0) -> dict:
    return {"code": code, "session": session, "open": close, "high": close + 1,
            "low": close - 1, "close": close, "volume": 1000, "amount": 10000}


class HistoricalMarketArchiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.repo = HMA.HistoricalMarketArchiveRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_raw_archive_fingerprint_ignores_import_time(self):
        rows = [_bar("000001.SH", "2026-01-05")]
        first = self.repo.import_raw_market_archive(rows, source="trusted-export",
            source_revision="rev-1", adjustment="raw", imported_at="2026-02-01T00:00:00Z")
        second = self.repo.import_raw_market_archive(rows, source="trusted-export",
            source_revision="rev-1", adjustment="unadjusted", imported_at="2026-03-01T00:00:00Z")
        self.assertEqual(first.archive_fingerprint, second.archive_fingerprint)

    def test_qfq_is_rejected(self):
        with self.assertRaisesRegex(HMA.HistoricalMarketArchiveError, "adjusted_market_data_rejected"):
            self.repo.import_raw_market_archive([_bar("000001.SH", "2026-01-05")],
                source="legacy-cache", source_revision="qfq", adjustment="qfq")

    def test_conflicting_duplicate_bar_fails_closed(self):
        with self.assertRaisesRegex(HMA.HistoricalMarketArchiveError, "conflicting_duplicate_bar"):
            self.repo.import_raw_market_archive([
                _bar("000001.SH", "2026-01-05", 10),
                _bar("000001.SH", "2026-01-05", 11),
            ], source="trusted-export", source_revision="rev-1", adjustment="raw")

    def test_manifest_and_rows_are_immutable(self):
        manifest = self.repo.import_raw_market_archive([_bar("000001.SH", "2026-01-05")],
            source="trusted-export", source_revision="rev-1", adjustment="raw")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE historical_market_bars SET close=99")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM historical_market_bars")
        self.assertEqual(1, len(self.repo.read_bars(manifest.archive_fingerprint,
                                                  start="2026-01-05", end="2026-01-05")))

    def test_calendar_issues_only_from_raw_archive_and_bounded_range(self):
        rows = [_bar("000001.SH", "2026-01-05"), _bar("000001.SH", "2026-01-06")]
        manifest = self.repo.import_raw_market_archive(rows, source="trusted-benchmark-export",
            source_revision="rev-1", adjustment="raw")
        calendar = HSC.issue_from_market_archive(self.repo,
            archive_fingerprint=manifest.archive_fingerprint, benchmark_symbol="000001.SH",
            start="2026-01-05", end="2026-01-06")
        self.assertEqual(("2026-01-05", "2026-01-06"), calendar.sessions)
        with self.assertRaisesRegex(HSC.HistoricalSessionCalendarError, "incomplete_range"):
            HSC.issue_from_market_archive(self.repo,
                archive_fingerprint=manifest.archive_fingerprint, benchmark_symbol="000001.SH",
                start="2026-01-01", end="2026-01-06")

    def test_forged_calendar_dataclass_is_rejected_against_archive(self):
        manifest = self.repo.import_raw_market_archive(
            [_bar("000001.SH", "2026-01-05"), _bar("000001.SH", "2026-01-06")],
            source="trusted-benchmark-export", source_revision="rev-1", adjustment="raw")
        calendar = HSC.issue_from_market_archive(self.repo,
            archive_fingerprint=manifest.archive_fingerprint, benchmark_symbol="000001.SH",
            start="2026-01-05", end="2026-01-06")
        spec = PIT_TESTS._spec(market_data_fingerprint=manifest.archive_fingerprint,
                               start_date="2026-01-05", end_date="2026-01-06")
        forged = replace(calendar, sessions=("2026-01-05",), session_count=1)
        _, valid, report = PV._owner_calendar(spec, forged, self.repo,
                                              manifest.archive_fingerprint)
        self.assertFalse(valid)
        self.assertEqual("blocked", report["status"])


class HistoricalUniverseArchiveTests(unittest.TestCase):
    def test_current_snapshot_shape_cannot_be_imported_as_archive(self):
        conn = sqlite3.connect(":memory:")
        repo = HUA.HistoricalUniverseArchiveRepository(conn)
        with self.assertRaises(HUA.HistoricalUniverseArchiveError):
            repo.import_historical_security_master([{"code": "000001.SZ", "name": "current"}],
                coverage_start="2020-01-01", coverage_end="2026-01-01",
                source="universe.json", source_revision="current")

    def test_archive_records_are_verified_and_future_observations_are_not_visible(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        repo = HUA.HistoricalUniverseArchiveRepository(conn)
        manifest = repo.import_historical_security_master([
            {"code": "000001.SZ", "listed_from": "2020-01-01", "delisted_at": None,
             "security_type": "equity", "exchange": "SZ", "observed_at": "2020-01-01T09:00:00+08:00"},
            {"code": "000002.SZ", "listed_from": "2020-01-01", "delisted_at": None,
             "security_type": "equity", "exchange": "SZ", "observed_at": "2020-01-01T09:00:00+08:00"},
            {"code": "000002.SZ", "listed_from": "2020-01-01", "delisted_at": "2025-01-01",
             "security_type": "equity", "exchange": "SZ", "observed_at": "2024-12-01T09:00:00+08:00"},
        ], coverage_start="2020-01-01", coverage_end="2026-01-01",
           source="historical-security-master", source_revision="rev-1")
        early = repo.membership_rows(manifest.universe_archive_fingerprint,
                                     asof="2024-11-30T15:00:00+08:00")
        self.assertEqual({"000001.SZ", "000002.SZ"}, {row["code"] for row in early})
        self.assertIsNone(next(row for row in early if row["code"] == "000002.SZ")["delist_date"])
        later = repo.membership_rows(manifest.universe_archive_fingerprint,
                                     asof="2026-01-02T15:00:00+08:00")
        self.assertEqual({"000001.SZ", "000002.SZ"}, {row["code"] for row in later})
        conn.close()


class TradabilityCoverageTests(unittest.TestCase):
    def test_owner_projection_separates_available_blocked_unknown(self):
        conn = sqlite3.connect(":memory:")
        repo = TA.TradabilityArchiveRepository(conn)
        repo.ensure_schema()
        repo.save(TA.TradabilityEvidence(
            code="000001.SH", session_date="2026-01-05", is_listed=True,
            listing_date="2020-01-01", delisting_date=None, is_st=False,
            is_suspended=True, suspension_reason="suspended", has_market_quote=False,
            has_trade_volume=False, is_price_limit_locked=False,
            price_limit_direction=None, source="trusted-history",
            observed_at="2026-01-04T10:00:00+08:00",
            effective_at="2026-01-05T09:30:00+08:00"))
        repo.save(TA.TradabilityEvidence(
            code="000002.SH", session_date="2026-01-05", is_listed=True,
            listing_date="2020-01-01", delisting_date=None, is_st=False,
            is_suspended=False, suspension_reason=None, has_market_quote=True,
            has_trade_volume=True, is_price_limit_locked=False,
            price_limit_direction=None, source="trusted-history",
            observed_at="2026-01-04T10:00:00+08:00",
            effective_at="2026-01-05T09:30:00+08:00"))
        request = {("000001.SH", "2026-01-05"): "2026-01-05T15:00:00+08:00",
                   ("000002.SH", "2026-01-05"): "2026-01-05T15:00:00+08:00",
                   ("000003.SH", "2026-01-05"): "2026-01-05T15:00:00+08:00"}
        projection = repo.coverage_projection(request)
        self.assertEqual(3, projection.requested_pairs)
        self.assertEqual(2, projection.proven_pairs)
        self.assertEqual(1, projection.available_pairs)
        self.assertEqual(1, projection.blocked_pairs)
        self.assertEqual(1, projection.unknown_pairs)
        self.assertEqual(2 / 3, projection.coverage_ratio)
        conn.close()


class ValidationLedgerTests(unittest.TestCase):
    def test_run_key_is_deterministic_and_corrupt_json_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        repo = EVR.ExperimentValidationRepository(conn)
        run_key = repo.build_run_key("a" * 64, {"market": "b" * 64}, "runner-v1")
        same = repo.build_run_key("a" * 64, {"market": "b" * 64}, "runner-v1")
        self.assertEqual(run_key, same)
        changed_owner = repo.build_run_key("a" * 64, {"market": "f" * 64}, "runner-v1")
        self.assertNotEqual(run_key, changed_owner)
        repo.append_run(run_key=run_key, experiment_fingerprint="a" * 64,
            strategy_id="s", strategy_version=1, strategy_checksum="c" * 64,
            calendar_fingerprint=None, universe_archive_fingerprint=None,
            tradability_evidence_fingerprint=None, market_archive_fingerprint="b" * 64,
            dataset_fingerprint="d" * 64, validation_evidence={"status": "blocked"},
            result={"status": "unavailable"}, folds=[], runner_version="runner-v1",
            runner_code_revision="e" * 40, created_at="2026-01-01T00:00:00Z")
        # Corruption bypassing immutable triggers still cannot be returned as a valid run.
        conn.execute("DROP TRIGGER experiment_validation_runs_no_update")
        conn.execute("UPDATE experiment_validation_runs SET result_json='not-json'")
        with self.assertRaisesRegex(EVR.ExperimentValidationPersistenceError, "corrupt_validation_run"):
            repo.get_run(run_key=run_key)
        conn.close()


if __name__ == "__main__":
    unittest.main()
