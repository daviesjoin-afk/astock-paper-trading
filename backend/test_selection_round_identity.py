"""选股轮次缓存的输入身份校验。

审计发现（plans/source-shared-layer-slimming-audit-20261028.md 第 3 项）：
``SelectionRound`` 在同一轮直接存 ``snap_f`` / ``fund_f``，命中时不比较
股票集合、财务输入或截止时间，因此同轮内时间截点跨交易日、输入来源变化
时可能误复用。

这些用例覆盖：输入变化、跨截止日、结果修改、失败恢复。
"""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

import selection_application as S


def _codes(prefix=600000, count=5):
    return [str(prefix + index) for index in range(count)]


def _market_codes(count=4000):
    """够量的主板代码（选股要求 ≥4000 行合规因子）。"""
    bases = ("600", "601", "603", "605")
    per = count // len(bases)
    codes = []
    for base in bases:
        codes.extend(f"{base}{index:03d}" for index in range(per))
    return codes


class SelectionRoundIdentityTests(unittest.TestCase):
    def test_live_snapshot_identity_follows_quote_day(self):
        rows = [{"code": "600000", "price": 10.0, "pct": 1.0,
                 "quote_at": "2026-10-27T15:00:00+08:00"}]
        identity = S._live_snapshot_identity(rows)
        self.assertEqual(identity[0], ("2026-10-27",))
        self.assertEqual(identity[1], ("600000",))
        self.assertTrue(S._live_rows_belong_to_day(rows, "2026-10-27"))
        self.assertFalse(S._live_rows_belong_to_day(rows, "2026-10-28"))

    def test_live_snapshot_identity_is_none_for_empty_rows(self):
        self.assertIsNone(S._live_snapshot_identity([]))
        self.assertFalse(S._live_rows_belong_to_day([], "2026-10-27"))

    def test_frame_code_identity_tracks_index(self):
        frame = pd.DataFrame(index=_codes())
        self.assertEqual(S._frame_code_identity(frame),
                         tuple(sorted(_codes())))
        self.assertIsNone(S._frame_code_identity(None))
        self.assertIsNone(S._frame_code_identity(pd.DataFrame()))

    def test_snapshot_must_still_cover_expected_codes(self):
        live_map = {code: {"price": 10.0} for code in _codes()}
        snap_f = [{"code": code} for code in _codes()]
        self.assertTrue(S._snapshot_covers(snap_f, _codes(), live_map))
        # 少一只股票就不能复用上一轮的截面。
        self.assertFalse(S._snapshot_covers(snap_f[:-1], _codes(), live_map))

    def test_fundamentals_without_identity_are_not_reusable(self):
        round_inputs = S.SelectionRound()
        round_inputs.fund_f = pd.DataFrame(index=_codes())
        # 没有登记财务输入标识 → 身份不明 → 不可复用。
        self.assertIsNone(round_inputs.fund_finance_identity)

    def test_forget_clears_published_fundamentals(self):
        round_inputs = S.SelectionRound()
        round_inputs.publish_fundamentals(pd.DataFrame(index=_codes()), "2026Q3")
        self.assertEqual(round_inputs.fund_finance_identity, "2026Q3")
        round_inputs.forget("fund")
        self.assertIsNone(round_inputs.fund_f)
        self.assertIsNone(round_inputs.fund_finance_identity)


class SelectionRoundCacheReuseTests(unittest.TestCase):
    """同轮复用必须满足输入身份一致；否则必须重新取证。"""

    def _universe(self, codes):
        return [{"code": code, "name": "样本", "risk_flag": ""} for code in codes]

    def _live_rows(self, codes, day):
        return [{"code": code, "price": 10.0, "pct": 1.0,
                 "quote_at": f"{day}T15:00:00+08:00"} for code in codes]

    def _run(self, *, prepared, day, live_rows, universe, complete_cutoff=None):
        codes = [row["code"] for row in universe]
        price_f = pd.DataFrame({"last_date": day.isoformat()}, index=codes)

        def context(key, loader, **_kwargs):
            return {"data": {}, "report_dates": ["2026-09-30"]} if key == "finance" else {}

        # 收盘后路径走 ``read_snapshot``（只读缓存事实），盘中走 ``refresh_rows``。
        # 两条路径都算"重新取证"，所以两边都记录。
        reading = mock.Mock(available=True, freshness=S.MDC.FRESHNESS_FRESH)
        reading.rows.return_value = live_rows
        with mock.patch.object(S.U, "coverage_report", return_value={
                "fresh_selection": 4000, "fresh_selection_pct": 100.0,
                "expected_reference_date": day.isoformat()}), \
                mock.patch.object(S.U, "load_universe", return_value=universe), \
                mock.patch.object(S.U, "latest_complete_trade_date",
                                  return_value=complete_cutoff or day), \
                mock.patch.object(S.U, "is_trade_day", return_value=False), \
                mock.patch.object(S.U, "previous_trade_day", return_value=day), \
                mock.patch.object(S, "_load_selection_base",
                                  return_value=(price_f, set(), {"state": "ready"})), \
                mock.patch.object(S, "_load_selection_context", side_effect=context), \
                mock.patch.object(S.F, "live_snapshot_asof",
                                  return_value=f"{day.isoformat()}T15:00:00+08:00"), \
                mock.patch.object(S.F, "compute_fundamental_factors",
                                  return_value=pd.DataFrame(index=codes)), \
                mock.patch.object(S, "_load_super_flow", return_value=[]), \
                mock.patch.object(S, "_schedule_super_flow_refresh"), \
                mock.patch.object(S.S, "scoring_factor_inputs", return_value=frozenset()), \
                mock.patch.object(S.S, "build_factor_table", return_value=object()), \
                mock.patch.object(S.S, "run_strategy", return_value={"picks": []}), \
                mock.patch.object(S.MDSvc, "refresh_rows",
                                  return_value=live_rows) as refresh, \
                mock.patch.object(S.MDSvc, "read_snapshot",
                                  return_value=reading) as read, \
                mock.patch.dict(S._LIVE_CACHE, {"sector_flow": [], "news_hits": [],
                                                "gate": {"light": "green"}, "indices": []}):
            token = S._CURRENT_ROUND.set(prepared)
            try:
                result = S._select_uncached(strategy="three_day", topn=10)
            finally:
                S._CURRENT_ROUND.reset(token)
        return result, refresh.call_count + read.call_count

    def test_same_day_round_reuses_the_snapshot(self):
        codes = _market_codes()
        universe = self._universe(codes)
        live_rows = self._live_rows(codes, dt.date(2026, 10, 27))
        prepared = S.SelectionRound()
        prepared.live_rows = live_rows
        prepared.live_map = {row["code"]: row for row in live_rows}
        result, fetches = self._run(
            prepared=prepared, day=dt.date(2026, 10, 27),
            live_rows=live_rows, universe=universe)
        self.assertNotIn("need_init", result, result.get("message"))
        self.assertEqual(fetches, 0, "同一交易日的同一轮必须复用快照")

    def test_cross_day_round_refetches_the_snapshot(self):
        codes = _market_codes()
        universe = self._universe(codes)
        stale_rows = self._live_rows(codes, dt.date(2026, 10, 26))
        fresh_rows = self._live_rows(codes, dt.date(2026, 10, 27))
        prepared = S.SelectionRound()
        prepared.live_rows = stale_rows
        prepared.live_map = {row["code"]: row for row in stale_rows}
        result, fetches = self._run(
            prepared=prepared, day=dt.date(2026, 10, 27),
            live_rows=fresh_rows, universe=universe)
        self.assertGreater(fetches, 0,
                           "跨交易日的旧快照必须作废并重新取证")
        self.assertIsNotNone(prepared.live_rows)
        self.assertTrue(
            S._live_rows_belong_to_day(prepared.live_rows, "2026-10-27"),
            "重新取证后轮次缓存里必须只有当天的事实")


if __name__ == "__main__":
    unittest.main()
