"""Same-round provider reuse must preserve the authority's freshness decisions."""
import unittest
from unittest import mock

import data_fetcher as dfc
import market_data_contract as MDC
import paper_trading as PT


class OpeningSnapshotReuseTests(unittest.TestCase):
    def test_opening_reuses_only_a_reading_fresh_under_its_own_policy(self):
        row = {"code": "600001", "price": 10.0, "pct": 1.0,
               "quote_at": "2026-11-03T10:00:00+08:00"}
        reading = mock.Mock(available=True, freshness=MDC.FRESHNESS_FRESH)
        reading.rows.return_value = (row,)
        with mock.patch.object(PT, "init_db"), \
                mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT.dfc, "check_data_source_health", return_value={"healthy": True}), \
                mock.patch.object(PT, "_entry_freeze_status"), \
                mock.patch.object(PT.MDSvc, "read_snapshot", return_value=reading) as read, \
                mock.patch.object(PT.MDSvc, "refresh_rows") as refresh, \
                mock.patch.object(PT, "_validated_live_universe", return_value=[]), \
                mock.patch.object(PT, "_live_scan_gate", return_value={
                    "ready": False, "covered_codes": 0, "eligible_codes": 5000,
                    "coverage_pct": 0.0, "required_codes": 4500,
                }):
            result = PT.execute_open("2026-11-03")
        self.assertEqual(result["status"], "blocked")
        refresh.assert_not_called()
        self.assertIs(read.call_args.args[0], MDC.OPENING_EVENT_POLICY)

    def test_stale_opening_reading_still_refreshes_instead_of_reusing_240s_data(self):
        reading = mock.Mock(available=True, freshness=MDC.FRESHNESS_STALE)
        with mock.patch.object(PT, "init_db"), \
                mock.patch.object(PT, "monitor_risk", return_value={"orders": []}), \
                mock.patch.object(PT.dfc, "check_data_source_health", return_value={"healthy": True}), \
                mock.patch.object(PT, "_entry_freeze_status"), \
                mock.patch.object(PT.MDSvc, "read_snapshot", return_value=reading), \
                mock.patch.object(PT.MDSvc, "refresh_rows", return_value=[]) as refresh, \
                mock.patch.object(PT, "_live_scan_gate", return_value={
                    "ready": False, "covered_codes": 0, "eligible_codes": 5000,
                    "coverage_pct": 0.0, "required_codes": 4500,
                }):
            result = PT.execute_open("2026-11-03")
        self.assertEqual(result["status"], "blocked")
        refresh.assert_called_once()
        self.assertIs(refresh.call_args.kwargs["policy"], MDC.OPENING_EVENT_POLICY)
        self.assertIsNotNone(refresh.call_args.kwargs["now"])


if __name__ == "__main__":
    unittest.main()
