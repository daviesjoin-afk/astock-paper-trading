"""One valid provider must reach execution without being labelled cross-verified."""
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

import data_fetcher as D
import execution_planner as E
import market_data_contract as M
import market_data_service as MS
import marketdata_normalizers as N
import paper_trading as P
import portfolio_runtime as PR
import portfolio_runtime_service as PRS

TZ = dt.timezone(dt.timedelta(hours=8))


class SingleSourceExecutionTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(TZ)
        self.day = self.now.date().isoformat()
        self.quote = {"code": "600001", "price": 10., "pct": 1., "amount": 10000000.,
                      "quote_at": self.now.isoformat(), "source": "tencent_public_quote"}

    def quotes(self, primary, independent, *, sina=()):
        with mock.patch.object(P.dfc, "fetch_realtime_for_codes", return_value=primary), \
                mock.patch.object(P.dfc, "fetch_independent_realtime_for_codes", return_value=independent), \
                mock.patch.object(P.dfc, "_fetch_sina_realtime_for_codes", return_value=list(sina)), \
                mock.patch.object(P.dfc, "enrich_live_flow_details", return_value={}), \
                mock.patch.object(P.PLTrad, "prefetch", return_value={}), \
                mock.patch.object(P.MDSvc, "read_snapshot", return_value=M.unavailable_reading(
                    M.LIVE_MARKET_POLICY, M.REASON_MISSING)), \
                mock.patch.object(P, "_latest_price_map", return_value={}), \
                mock.patch.object(P, "_universe_snapshot_time", return_value=None):
            return P._quotes(["600001"], asof_date=self.day)["600001"]

    def test_primary_failure_promotes_valid_backup_to_executable_single_source(self):
        row = self.quotes([], [self.quote])
        self.assertEqual(row["source"], "tencent_public_quote")
        self.assertEqual(row["quote_validation"], "range_timestamp_checked")
        self.assertFalse(row["quote_cross_check"]["passed"])
        self.assertTrue(P._execution_quote_status(row, self.day)["fresh"])
        reading = E.market_reading_for_execution(row, asof_day=self.day,
                                                 execution_asof=self.now.isoformat())
        self.assertTrue(M.is_execution_quote_usable(reading))
        self.assertFalse(M.is_cross_source_verified(reading.snapshot))
        intent = E.PersistedOrderIntent(1, "test", 1, None, None, None, None,
                                        "600001", "buy", 100, self.now.isoformat(), 10.)
        context = E.ExecutionContext(self.day, self.now.isoformat(), row, reading,
            SimpleNamespace(evidence_present=True, can_buy=True, to_dict=lambda: {}),
            session_phase="continuous_morning", available_liquidity=10000, buying_power=100000.)
        decision = E.evaluate_simulated_execution(intent, context)
        self.assertNotIn("MARKET_UNVERIFIED", decision.reasons)
        self.assertNotIn("MARKET_UNAVAILABLE", decision.reasons)
        self.assertTrue(decision.executable_now, decision.reasons)

    def test_valid_primary_survives_missing_or_stale_secondary(self):
        for cross in ([], [{**self.quote, "quote_at": "2000-01-01T10:00:00+08:00"}],
                      [{**self.quote, "pct": float("nan")}], [{**self.quote, "pct": None}]):
            with self.subTest(cross=cross):
                row = self.quotes([{**self.quote, "source": "eastmoney_ulist"}], cross)
                self.assertEqual(row["quote_validation"], "range_timestamp_checked")
                self.assertTrue(P._execution_quote_status(row, self.day)["fresh"])

    def test_conflicting_fresh_sources_still_block_entry(self):
        row = self.quotes([{**self.quote, "source": "eastmoney_ulist"}],
                          [{**self.quote, "price": 12., "pct": 5.}])
        self.assertEqual(row["quote_validation"], "cross_source_failed")
        self.assertFalse(P._execution_quote_status(row, self.day)["fresh"])

    def test_stale_primary_is_replaced_without_retimestamping_its_price(self):
        old = {**self.quote, "price": 99., "quote_at": "2000-01-01T10:00:00+08:00"}
        row = self.quotes([old], [self.quote])
        self.assertEqual(row["price"], 10.)
        self.assertNotEqual(row["quote_at"], old["quote_at"])

    def test_stale_or_invalid_sources_never_execute(self):
        for changes in ({"quote_at": "2000-01-01T10:00:00+08:00"},
                        {"price": float("nan")}, {"pct": float("inf")}, {"price": True}):
            row = {**self.quote, **changes, "quote_source": "live", "quote_validation": "range_timestamp_checked"}
            self.assertFalse(P._execution_quote_status(row, self.day)["fresh"], changes)

    def test_production_chain_retries_another_provider_after_stale_response(self):
        old = mock.Mock(name="old");new = mock.Mock(name="new")
        old.fetch_realtime.return_value = [{**self.quote, "quote_at": "2000-01-01T10:00:00+08:00"}]
        new.fetch_realtime.return_value = [self.quote]
        with mock.patch.object(D, "_tencent_realtime_feed", return_value=old), \
                mock.patch.object(D, "_sina_realtime_feed", return_value=new):
            rows = D.fetch_independent_realtime_for_codes(["600001"])
        self.assertEqual(rows, [self.quote])
        new.fetch_realtime.assert_called_once_with(["600001"])

    def test_health_accepts_either_source_but_rejects_old_quotes(self):
        for primary, backup, expected in (([], [self.quote], True),
                ([{"f12": "600001", "f2": 10., "f3": 1., "f124": self.now.timestamp()}], [], True),
                ([{"f12": "600001", "f2": 10., "f3": 1., "f124": 1}],
                 [{**self.quote, "quote_at": "2000-01-01T10:00:00+08:00"}], False)):
            # Health probes use their own three representative codes.
            backup = [{**r, "code": "300214"} for r in backup]
            with self.subTest(expected=expected), mock.patch.object(D, "load_source_health", return_value={}), \
                    mock.patch.object(D, "_save_source_health"), \
                    mock.patch.object(D, "_fetch_clist", return_value=primary), \
                    mock.patch.object(D, "fetch_independent_realtime_for_codes", return_value=backup), \
                    mock.patch.object(D, "http_get", return_value=""), \
                    mock.patch.object(D, "reset_data_source"), mock.patch.object(D.time, "sleep"):
                self.assertEqual(D.check_data_source_health(force=True)["healthy"], expected)

    def test_source_freshness_rejects_future_and_bad_time(self):
        for stamp in ("bad", "", (self.now + dt.timedelta(hours=1)).isoformat()):
            self.assertFalse(N.source_quote_is_fresh({**self.quote, "quote_at": stamp}, now=self.now))

    def test_price_only_source_can_value_positions_without_forging_complete_fields(self):
        snapshot = MS._full_market_snapshot(rows=[self.quote], observed_at=self.now.isoformat(),
            saved_at=self.now.isoformat(), complete=True, expected_rows=1,
            kind=MS.KIND_FULL_MARKET_SNAPSHOT, source=MS.TENCENT_SNAPSHOT_SOURCE)
        for seconds, expected in ((0, PR.AVAILABLE), (M.LIVE_MARKET_POLICY.max_age_seconds + 1, PR.UNAVAILABLE)):
            reading = M.classify(snapshot, M.LIVE_MARKET_POLICY,
                                 now=self.now + dt.timedelta(seconds=seconds), asof_day=self.day)
            fact = PRS._market_valuation_dimension(owners=("test",), asof_day=self.day, reading=reading,
                expected_identity=M.snapshot_fingerprint(snapshot),
                positions=({"account_id": "test", "code": "600001", "qty": 100, "cost": 9999.},))
            self.assertEqual(fact.status, expected)
            if expected == PR.AVAILABLE:
                self.assertEqual(fact.facts["market_value_by_account"]["test"], 1000.)
                self.assertEqual(fact.facts["market_verification"], M.VERIFICATION_NOT_ATTEMPTED)
        self.assertFalse(M.is_cross_source_verified(snapshot))


if __name__ == "__main__":
    unittest.main()
