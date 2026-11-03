"""Same-round provider reuse must preserve the authority's freshness decisions."""
import unittest
from unittest import mock

import data_fetcher as dfc
import factors
import market_data_contract as MDC
import paper_trading as PT


class ProviderFailureReuseTests(unittest.TestCase):
    def test_empty_overseas_result_is_a_short_failure_not_a_success(self):
        clock = [1000.0]
        calls = []
        with mock.patch.object(dfc, "_mem_cache", {}), \
                mock.patch.object(dfc.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(dfc, "fetch_kline", side_effect=lambda *a, **k: calls.append(1) or None):
            self.assertEqual(dfc.fetch_overseas_history(), {})
            self.assertEqual(dfc.fetch_overseas_history(), {})
            self.assertEqual(len(calls), len(dfc.OVERSEAS_SECIDS))
            clock[0] += dfc.OVERSEAS_FAILURE_CACHE_SECONDS + 1
            self.assertEqual(dfc.fetch_overseas_history(), {})
            self.assertEqual(len(calls), len(dfc.OVERSEAS_SECIDS) * 2)


class OverseasRiskEvidenceTests(unittest.TestCase):
    def test_no_history_is_unknown_not_a_green_risk_signal(self):
        with mock.patch.object(factors.dfc, "fetch_overseas_history") as fetch:
            result = factors.overseas_risk_gate({})
        fetch.assert_not_called()
        self.assertEqual(result["light"], "unknown")
        self.assertIsNone(result["score"])
        self.assertNotIn("外围环境平稳", result["advice"])

    def test_explicit_history_still_uses_the_original_scoring_rules(self):
        import pandas as pd

        closes = pd.Series([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
        history = {
            "DJIA": {"name": "道琼斯", "df": {"close": closes}},
            "NDX": {"name": "纳斯达克100", "df": {"close": closes}},
        }
        result = factors.overseas_risk_gate(history)
        self.assertEqual(result["light"], "yellow")
        self.assertEqual(result["score"], 2.0)

    def test_three_or_more_points_still_raise_a_red_light(self):
        import pandas as pd

        closes = pd.Series([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
        history = {
            "DJIA": {"name": "道琼斯", "df": {"close": closes}},
            "NDX": {"name": "纳斯达克100", "df": {"close": closes}},
            "HSI": {"name": "恒生指数", "df": {"close": closes}},
        }
        result = factors.overseas_risk_gate(history)
        self.assertEqual(result["light"], "red")
        self.assertEqual(result["score"], 3.0)


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
