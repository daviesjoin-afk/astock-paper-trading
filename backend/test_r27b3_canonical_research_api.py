"""R27-B3 canonical research read API and operational timeline contracts."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as adaptive  # noqa: E402
import ai_analysis  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import ai_research_repository as repository  # noqa: E402
import api_adaptive  # noqa: E402
import market_data_contract as MDC  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


DAY = "2026-08-27"
OBSERVED_AT = f"{DAY}T10:30:00+08:00"


def _hypothesis(*, purpose_subject="600000", as_of=DAY, confidence=0.8, supported=True):
    evidence = ()
    if supported:
        snapshot = MDC.symbol_quote_snapshot(
            {"code": purpose_subject, "price": 10.5, "quote_at": OBSERVED_AT,
             "quote_source": "eastmoney", "quote_validation": "cross_source_checked"},
            asof_day=as_of,
        )
        reading = MDC.classify(
            snapshot, MDC.policy_named("live_market"), now=OBSERVED_AT, asof_day=as_of,
        )
        ref = ARC.evidence_ref_from_market_reading(reading)
        evidence = (ARC.HypothesisEvidence(ref=ref, relation=ARC.RELATION_SUPPORTS),)
    return ARC.ResearchHypothesis(
        hypothesis_id=f"H-{purpose_subject}", as_of=as_of, subject=purpose_subject,
        thesis="只是一条研究假设", evidence=evidence, confidence=confidence,
    )


@contextlib.contextmanager
def _connection(path):
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()


class CanonicalResearchAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = os.path.join(self.temp.name, "adaptive.sqlite3")
        self.path_patch = mock.patch.object(adaptive, "DB_PATH", self.db_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        app = FastAPI()
        app.include_router(api_adaptive.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def append(self, *, purpose="candidate_challenge", subject="600000", as_of=DAY,
               confidence=0.8, supported=True):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            repository.ensure_schema(conn)
            run_id = repository.append_run(
                conn, hypothesis=_hypothesis(
                    purpose_subject=subject, as_of=as_of, confidence=confidence, supported=supported,
                ), purpose=purpose, trigger="api-test", created_at=f"{as_of}T11:00:00+08:00",
                provider_slot="ai2", provider_model="test-model",
                narrative="研究叙述", counter_arguments=("反方说明",),
            )
            conn.commit()
            return run_id
        finally:
            conn.close()

    def test_B3_API_01_fresh_database_returns_empty_and_ensures_schema(self):
        result = api_adaptive.canonical_research_runs(50, None, None, None)
        self.assertEqual({"status": "ok", "runs": []}, result)
        with _connection(self.db_path) as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_research_runs'"
            ).fetchone())

    def test_B3_API_01b_routes_are_bounded_and_detail_path_is_registered(self):
        listed = self.client.get("/api/adaptive/research/runs?limit=201")
        self.assertEqual(422, listed.status_code)
        too_long = self.client.get("/api/adaptive/research/runs?purpose=" + "x" * 81)
        self.assertEqual(422, too_long.status_code)
        missing = self.client.get("/api/adaptive/research/runs/999")
        self.assertEqual(404, missing.status_code)

    def test_B3_API_02_list_uses_repository_filters_order_and_limit(self):
        old = self.append(purpose="candidate_challenge", subject="600000", as_of=DAY)
        wanted = self.append(purpose="event_evidence", subject="000001", as_of="2026-08-28")
        self.append(purpose="event_evidence", subject="000002", as_of="2026-08-28")

        all_runs = api_adaptive.canonical_research_runs(2, None, None, None)["runs"]
        self.assertEqual([3, 2], [row["id"] for row in all_runs])
        filtered = api_adaptive.canonical_research_runs(
            10, "event_evidence", "2026-08-28", "000001",
        )["runs"]
        self.assertEqual([wanted], [row["id"] for row in filtered])
        self.assertNotIn(old, [row["id"] for row in filtered])

    def test_B3_API_03_detail_preserves_canonical_values_and_authority(self):
        run_id = self.append(confidence=0.8, supported=True)
        result = api_adaptive.canonical_research_run(run_id)
        run = result["run"]
        self.assertEqual("supported", run["status"])
        self.assertEqual(0.8, run["confidence"])
        self.assertEqual("research", run["authority"])
        self.assertFalse(run["is_authoritative"])
        self.assertEqual("test-model", run["provider_model"])
        self.assertEqual("研究叙述", run["narrative"])
        self.assertEqual("600000", run["hypothesis"]["subject"])
        self.assertNotIn("approved", run)
        self.assertNotIn("actionable", run)

    def test_B3_API_04_missing_run_is_404_not_empty_success(self):
        with self.assertRaises(HTTPException) as caught:
            api_adaptive.canonical_research_run(999)
        self.assertEqual(404, caught.exception.status_code)
        self.assertEqual("research_run_not_found", caught.exception.detail)

    def test_B3_API_05_corrupt_rows_fail_closed_with_stable_reason(self):
        run_id = self.append()
        with _connection(self.db_path) as conn:
            conn.execute("UPDATE ai_research_runs SET narrative='tampered' WHERE id=?", (run_id,))
            conn.commit()
        with self.assertRaises(HTTPException) as caught:
            api_adaptive.canonical_research_runs(50, None, None, None)
        self.assertEqual(500, caught.exception.status_code)
        self.assertEqual("corrupt_research_record", caught.exception.detail)
        self.assertNotIn("tampered", str(caught.exception.detail))

    def test_B3_API_06_repository_read_connection_is_query_only(self):
        run_id = self.append()
        original = repository.recent_runs

        def attempt_write(conn, **kwargs):
            conn.execute("DELETE FROM ai_research_runs WHERE id=?", (run_id,))
            return original(conn, **kwargs)

        with mock.patch.object(repository, "recent_runs", side_effect=attempt_write):
            with self.assertRaises(sqlite3.OperationalError):
                api_adaptive.canonical_research_runs(50, None, None, None)
        with _connection(self.db_path) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM ai_research_runs").fetchone()[0])


class OperationalTimelineTests(unittest.TestCase):
    def test_B3_TIMELINE_01_returns_only_run_reference_and_never_promotes_legacy_result(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        ai_analysis.ensure_schema(conn)
        conn.executemany(
            "INSERT INTO adaptive_ai_analysis_runs(business_key,trade_date,analysis_window,scope,"
            "trigger,status,evidence_hash,deterministic_status,result,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("canonical", DAY, "morning", "all", "test", "completed", "hash", "valid",
                 '{"canonical_run_id":7,"authority":"canonical_research_ledger"}', DAY, DAY),
                ("legacy", DAY, "afternoon", "all", "test", "completed", "hash", "valid",
                 '{"verdict":"supported","thesis":"legacy free-form"}', DAY, DAY),
            ],
        )
        conn.commit()

        @contextlib.contextmanager
        def factory():
            yield conn

        with mock.patch.object(repository, "get_run", side_effect=AssertionError("timeline must not read conclusions")):
            result = ai_analysis.timeline(factory, trade_date=DAY)
        by_key = {row["business_key"]: row for row in result["runs"]}
        self.assertEqual(7, by_key["canonical"]["canonical_run_id"])
        self.assertNotIn("canonical_research", by_key["canonical"])
        self.assertIsNone(by_key["canonical"]["result"])
        self.assertIsNone(by_key["legacy"]["canonical_run_id"])
        self.assertEqual("legacy_compatibility_history", by_key["legacy"]["source"])
        self.assertIsNone(by_key["legacy"]["result"])
        self.assertNotIn("legacy free-form", str(result))


if __name__ == "__main__":
    unittest.main()
