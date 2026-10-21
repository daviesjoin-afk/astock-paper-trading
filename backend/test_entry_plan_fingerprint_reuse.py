"""Entry planning hashes current market facts once per independent capture."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market_data_contract as MDC
import paper_timing as PTM
import paper_trading as PT
import portfolio_runtime as PR


class EntryPlanFingerprintTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="entry-plan-fingerprint-")
        self.addCleanup(folder.cleanup)
        patch = mock.patch.object(PT, "DB_PATH", os.path.join(folder.name, "paper.sqlite3"))
        patch.start()
        self.addCleanup(patch.stop)
        PT.init_db()
        PT.start_new_cycle(capital=100_000, include_dashboard=False)
        self.instant = dt.datetime.now().astimezone() + dt.timedelta(seconds=2)
        self.day = self.instant.date().isoformat()
        snapshot = MDC.MarketDataSnapshot(
            kind="full_market_snapshot", rows=({"code": "600001", "price": 10.0,
                "quote_at": self.instant.isoformat(), "source": "fixture"},),
            as_of=self.day, observed_at=self.instant.isoformat(), source="fixture",
            complete=True, expected_rows=1, verification=MDC.VERIFICATION_VERIFIED,
            verification_method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
        )
        self.reading = MDC.MarketDataReading(
            availability=MDC.AVAILABILITY_AVAILABLE, freshness=MDC.FRESHNESS_FRESH,
            status=MDC.STATUS_FRESH, policy_name=MDC.LIVE_MARKET_POLICY.name,
            snapshot=snapshot,
        )
        with PT._db(immediate=True) as conn:
            self.cycle = PT._active_cycle(conn)["id"]
            accounts = [row[0] for row in conn.execute(
                "SELECT id FROM paper_accounts WHERE cycle_id=? ORDER BY id", (self.cycle,))]
            self.account = accounts[0]
            for account in accounts:
                params = {"adaptive_allocation": {"status": "active",
                    "effective_date": self.day, "weight_pct": 100 / len(accounts)}}
                conn.execute(
                    "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (self.cycle, account, "fingerprint-fixture", "balanced", json.dumps(params),
                     "fixture", self.day, self.instant.isoformat()))

    def build(self, conn):
        return PT._build_portfolio_entry_plan(
            conn, cycle_id=self.cycle, asof_day=self.day,
            decision_at=self.instant.isoformat(), account_id=self.account,
            symbol="600001", intent_kind="NEW_ENTRY")

    def test_plan_and_final_revalidation_hash_once_each_and_keep_complete_result(self):
        real_capture = PT.PRS.capture_portfolio_runtime_snapshot
        with mock.patch.object(PT.MDSvc, "read_snapshot", return_value=self.reading), \
                mock.patch.object(MDC, "snapshot_fingerprint", wraps=MDC.snapshot_fingerprint) as hashes, \
                contextlib.redirect_stderr(io.StringIO()), PTM.capture("test"), \
                PT._db(immediate=True) as conn:
            entry = self.build(conn)
            self.assertEqual(hashes.call_count, 1)
            self.assertTrue(PT._revalidate_portfolio_entry_plan(conn, entry))
            self.assertEqual(hashes.call_count, 2)
            stages = PTM.snapshot("test")["stages"]
            self.assertEqual(stages["execution.plan_build"]["calls"], 1)
            self.assertEqual(stages["execution.plan_revalidate"]["calls"], 1)

            # Recreate the former caller constraint through the real service:
            # the full entry (including plan provenance) must remain identical.
            def constrained_capture(*args, **kwargs):
                snapshot = kwargs["market_reading"].snapshot
                return real_capture(*args, **kwargs,
                    market_evidence_identity=MDC.snapshot_fingerprint(snapshot))
            with mock.patch.object(PT.PRS, "capture_portfolio_runtime_snapshot",
                                   side_effect=constrained_capture):
                self.assertEqual(self.build(conn), entry)

    def test_changed_market_facts_invalidate_plan_without_reusing_old_hash(self):
        with mock.patch.object(PT.MDSvc, "read_snapshot", return_value=self.reading) as read, \
                PT._db(immediate=True) as conn:
            entry = self.build(conn)
            changed = replace(self.reading.snapshot, rows=({
                **self.reading.snapshot.rows[0], "price": 11.0},))
            read.return_value = replace(self.reading, snapshot=changed)
            self.assertFalse(PT._revalidate_portfolio_entry_plan(conn, entry))

    def test_missing_market_does_not_hash_or_inherit_previous_evidence(self):
        with mock.patch.object(PT.MDSvc, "read_snapshot", return_value=self.reading) as read, \
                PT._db(immediate=True) as conn:
            entry = self.build(conn)
            read.return_value = MDC.unavailable_reading(MDC.LIVE_MARKET_POLICY)
            with mock.patch.object(MDC, "snapshot_fingerprint", side_effect=AssertionError("missing snapshot")):
                self.assertFalse(PT._revalidate_portfolio_entry_plan(conn, entry))

    def test_external_expected_fingerprint_still_rejects_mismatch(self):
        with PT._db(immediate=True) as conn:
            with self.assertRaisesRegex(PR.PortfolioRuntimeError, "market_evidence_identity_mismatch"):
                PT.PRS.capture_portfolio_runtime_snapshot(
                    conn, cycle_id=self.cycle, asof_day=self.day,
                    decision_at=self.instant.isoformat(), builtin_scope=PT.ACTIVE_ACCOUNT_IDS,
                    market_reading=self.reading, market_evidence_identity="f" * 64)


if __name__ == "__main__":
    unittest.main()
