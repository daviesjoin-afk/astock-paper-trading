import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

import market_data_contract as MDC
import paper_risk_scan_state as SC
import paper_trading as PT


class QuoteFallbackTests(unittest.TestCase):
    def quote(self, *, status=MDC.STATUS_FRESH, age=0, cross_price=10.0, primary=None):
        day = dt.date(2026, 9, 3)
        stamp = (dt.datetime(2026, 9, 3, 10) - dt.timedelta(days=age)).isoformat()
        row = dict(code="600001", price=10.0, pct=1.0, quote_at=stamp,
                   super_net=1.0, main_pct=0.1)
        snapshot = MDC.MarketDataSnapshot(kind="full_market_snapshot", rows=(row,),
                                          as_of=day.isoformat(), observed_at=stamp,
                                          complete=True, expected_rows=1)
        reading = MDC.MarketDataReading(availability="available", freshness=(
            "fresh" if status == "fresh" else "stale"), status=status,
            policy_name="test", snapshot=snapshot)
        with patch.object(PT.dfc, "fetch_realtime_for_codes", return_value=primary or []), \
                patch.object(PT.MDSvc, "read_snapshot", return_value=reading) as read, \
                patch.object(PT.dfc, "fetch_independent_realtime_for_codes", return_value=[
                    {**row, "price": cross_price, "source": "independent"}]), \
                patch.object(PT, "_latest_price_map", return_value={}), \
                patch.object(PT, "_universe_snapshot_time", return_value=None):
            return PT._quotes(["600001"], asof_date=day)["600001"], read

    def test_invalid_primary_uses_fresh_owner_snapshot_with_cross_check(self):
        row, _ = self.quote(primary=[dict(code="600001", quote_at="2026-09-03T10:00:00")])
        self.assertEqual(row["price"], 10.0)
        self.assertEqual(row["primary_quote_origin"], "verified_full_market_snapshot")
        self.assertEqual(row["quote_validation"], "cross_source_checked")
        self.assertTrue(row["primary_owner_fingerprint"])
        self.assertEqual(row["quote_at"], "2026-09-03T10:00:00")

    def test_fallback_never_bypasses_independent_disagreement(self):
        row, _ = self.quote(cross_price=12.0)
        self.assertEqual(row["quote_validation"], "cross_source_failed")

    def test_stale_or_wrong_day_owner_is_not_promoted(self):
        for kwargs in ({"status": MDC.STATUS_STALE}, {"age": 1}):
            row, _ = self.quote(**kwargs)
            self.assertEqual(row["quote_source"], "local_cache")
            self.assertNotIn("primary_quote_origin", row)

    def test_complete_primary_does_not_read_owner_fallback(self):
        row, read = self.quote(primary=[dict(code="600001", price=10.0, pct=1.0,
                                           quote_at="2026-09-03T10:00:00",
                                           super_net=1.0, main_pct=0.1)])
        self.assertEqual(row["quote_validation"], "cross_source_checked")
        read.assert_not_called()


class ScanSummaryTests(unittest.TestCase):
    def test_latest_is_cycle_and_day_scoped_and_read_only(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE paper_risk_scan_runs(cycle_id INTEGER,asof_date TEXT,"
                     "scan_minute TEXT,status TEXT)")
        conn.executemany("INSERT INTO paper_risk_scan_runs VALUES(?,?,?,?)", [
            (1, "2026-09-03", "14:21", "completed"),
            (1, "2026-09-03", "14:24", "failed"),
            (2, "2026-09-03", "14:27", "completed"),
            (1, "2026-09-04", "14:30", "completed")])
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        latest = SC.latest_scan(conn, cycle_id=1, asof_date="2026-09-03")
        self.assertEqual((latest["scan_minute"], latest["status"]), ("14:24", "failed"))
        self.assertIsNone(SC.latest_scan(conn, cycle_id=3, asof_date="2026-09-03"))
