"""A strategy batch must consume one prepared market snapshot, not refetch it."""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

import selection_application as S


class SelectionRoundInputTests(unittest.TestCase):
    def test_prepared_round_bypasses_live_and_fundamental_sources(self):
        codes = [f"{prefix}{i:03d}" for prefix in ("600", "601", "603", "605")
                 for i in range(1000)]
        universe = [{"code": code, "name": "样本", "risk_flag": ""} for code in codes]
        live_rows = [{"code": code, "price": 10.0, "pct": 1.0,
                      "quote_at": "2026-10-27T15:00:00+08:00"}
                     for code in codes]
        live_map = {row["code"]: row for row in live_rows}
        snap_f = [dict(row) for row in live_rows]
        price_f = pd.DataFrame({"last_date": "2026-10-27"}, index=codes)
        today = dt.date(2026, 10, 27)
        prepared = S.SelectionRound()
        prepared.live_rows = live_rows
        prepared.live_map = live_map
        prepared.snap_f = snap_f
        # 基本面结果必须连同它的财务输入标识一起登记，否则视为来源不明，
        # 轮次缓存不得复用（fail-closed）。
        prepared.publish_fundamentals(pd.DataFrame(index=codes), "")

        def context(key, loader, **_kwargs):
            return {"data": {}} if key == "finance" else {}

        with mock.patch.object(S.U, "coverage_report", return_value={
                "fresh_selection": 4000, "fresh_selection_pct": 100.0,
                "expected_reference_date": today.isoformat()}), \
                mock.patch.object(S.U, "load_universe", return_value=universe), \
                mock.patch.object(S.U, "latest_complete_trade_date", return_value=today), \
                mock.patch.object(S.U, "is_trade_day", return_value=False), \
                mock.patch.object(S.U, "previous_trade_day", return_value=today), \
                mock.patch.object(S, "_load_selection_base",
                                  return_value=(price_f, set(), {"state": "ready"})), \
                mock.patch.object(S, "_load_selection_context", side_effect=context), \
                mock.patch.object(S.F, "live_snapshot_asof", return_value="2026-10-27T15:00:00+08:00"), \
                mock.patch.object(S.F, "compute_fundamental_factors",
                                  side_effect=AssertionError("round fundamental cache missed")), \
                mock.patch.object(S, "_load_super_flow", return_value=[]), \
                mock.patch.object(S, "_schedule_super_flow_refresh"), \
                mock.patch.object(S.S, "scoring_factor_inputs", return_value=frozenset()), \
                mock.patch.object(S.S, "build_factor_table", return_value=object()), \
                mock.patch.object(S.S, "run_strategy", return_value={"picks": []}), \
                mock.patch.object(S.MDSvc, "read_snapshot",
                                  side_effect=AssertionError("round live cache missed")), \
                mock.patch.object(S.MDSvc, "refresh_rows",
                                  side_effect=AssertionError("round live cache missed")), \
                mock.patch.dict(S._LIVE_CACHE, {"sector_flow": [], "news_hits": [],
                                                "gate": {"light": "green"}, "indices": []}):
            token = S._CURRENT_ROUND.set(prepared)
            try:
                result = S._select_uncached(strategy="three_day", topn=10)
            finally:
                S._CURRENT_ROUND.reset(token)

        self.assertNotIn("need_init", result, result.get("message"))
        self.assertEqual(4000, result["data_quality"]["live_quote_coverage"])
