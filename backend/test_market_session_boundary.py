"""Session closing facts and read-only factor readiness regressions."""
import datetime as dt
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import pandas as pd
import market_data_contract as C
import market_data_service as S
import close_snapshot_runner as R
import main as M
import paper_trading as P

ZONE = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 14, 18, tzinfo=ZONE)
CLOSE = NOW.replace(hour=15)


def rows(stamp=CLOSE, count=4000, start=600000):
    return [{"code": str(start+i), "price": 10., "pct": 1., "quote_at": stamp.isoformat()}
            for i in range(count)]


def snapshot(data, price_only=False):
    return S._full_market_snapshot(rows=data, observed_at=max((r.get("quote_at") or "" for r in data), default=None),
        saved_at=NOW.isoformat(), complete=True, expected_rows=len(data),
        kind=S.KIND_FULL_MARKET_SNAPSHOT,
        source=S.TENCENT_SNAPSHOT_SOURCE if price_only else "eastmoney_clist_full_snapshot")


class ClosingWindowTests(unittest.TestCase):
    def test_close_price_remains_a_closing_fact_but_not_live(self):
        s = snapshot(rows())
        self.assertEqual("fresh", C.classify(s, C.CLOSE_SNAPSHOT_POLICY, now=NOW).freshness)
        self.assertEqual("stale", C.classify(s, C.LIVE_MARKET_POLICY, now=NOW).freshness)
        self.assertEqual(10860, C.provider_max_age(C.CLOSE_SNAPSHOT_POLICY, NOW))
        self.assertEqual(240, C.provider_max_age(C.LIVE_MARKET_POLICY, NOW))

    def test_one_minute_close_grace_is_inclusive(self):
        for seconds, expected in [(60,"fresh"),(61,"stale")]:
            with self.subTest(seconds=seconds):
                r=C.classify(snapshot(rows(CLOSE-dt.timedelta(seconds=seconds))),C.CLOSE_SNAPSHOT_POLICY,now=NOW)
                self.assertEqual(expected,r.freshness)

    def test_preclose_future_quotes_cannot_create_a_close(self):
        now = CLOSE-dt.timedelta(seconds=30)
        self.assertIsNone(C.provider_max_age(C.CLOSE_SNAPSHOT_POLICY, now))
        self.assertEqual("stale",C.classify(snapshot(rows()),C.CLOSE_SNAPSHOT_POLICY,now=now).freshness)

    def test_prior_day_and_next_day_do_not_count(self):
        for delta in [-1,1]:
            with self.subTest(delta=delta):
                self.assertEqual([],C.close_snapshot_rows(rows(CLOSE+dt.timedelta(days=delta)),NOW))

    def test_missing_time_and_old_majority_count_against_coverage(self):
        for data in [rows(CLOSE-dt.timedelta(minutes=5),3999)+rows(CLOSE,1,700000),
                     rows(CLOSE,3599)+[{"code":str(700000+i)} for i in range(401)]]:
            self.assertEqual("stale",C.classify(snapshot(data),C.CLOSE_SNAPSHOT_POLICY,now=NOW).freshness)

    def test_ninety_percent_and_future_skew_boundaries(self):
        self.assertEqual("fresh",C.classify(snapshot(rows(CLOSE,3600)+rows(CLOSE-dt.timedelta(minutes=5),400,700000)),
                         C.CLOSE_SNAPSHOT_POLICY,now=NOW).freshness)
        for delta,expected in [(120,"fresh"),(121,"stale")]:
            self.assertEqual(expected,C.classify(snapshot(rows(NOW+dt.timedelta(seconds=delta))),
                             C.CLOSE_SNAPSHOT_POLICY,now=NOW).freshness)

    def test_midnight_drift_cannot_enter_the_previous_close_day(self):
        late=NOW.replace(hour=23,minute=59,second=30)
        self.assertEqual([],C.close_snapshot_rows(rows(late+dt.timedelta(seconds=60)),late))

    def test_explicit_previous_session_can_be_read_next_morning(self):
        morning=NOW.replace(hour=8)+dt.timedelta(days=1)
        s=snapshot(rows())
        self.assertEqual("fresh",C.classify(s,C.CLOSE_SNAPSHOT_POLICY,now=morning,asof_day=NOW.date()).freshness)
        self.assertEqual("stale",C.classify(s,C.CLOSE_SNAPSHOT_POLICY,now=morning).freshness)
        self.assertEqual([],C.close_snapshot_rows(rows(morning),morning,NOW.date()))

    def test_price_only_close_keeps_unknown_fields_and_source_status(self):
        data=rows()
        for r in data:r.update(industry=None,main_net=None,pe=None)
        r=C.classify(snapshot(data,True),C.CLOSE_SNAPSHOT_POLICY,now=NOW)
        self.assertEqual("degraded",r.status)
        self.assertEqual("price_only_coverage",r.reason)
        self.assertFalse(C.is_cross_source_verified(r.snapshot))
        self.assertIsNone(r.rows()[0]["pe"])

    def test_policy_observations_do_not_change_the_fact_fingerprint(self):
        s=snapshot(rows())
        close=C.classify(s,C.CLOSE_SNAPSHOT_POLICY,now=NOW)
        later=C.classify(s,C.CLOSE_SNAPSHOT_POLICY,now=NOW+dt.timedelta(minutes=30))
        self.assertEqual(C.snapshot_fingerprint(s),C.snapshot_fingerprint(close.snapshot))
        self.assertEqual(C.snapshot_fingerprint(s),C.snapshot_fingerprint(later.snapshot))

    def test_paper_close_reuses_the_same_source_time_rule(self):
        data=rows()+rows(CLOSE-dt.timedelta(minutes=5),1,700000)
        actual=P._validated_live_universe(C.close_snapshot_rows(data,NOW),NOW.date())
        self.assertEqual(4000,len(actual))


class ClosingRefreshTests(unittest.TestCase):
    def test_historical_close_cannot_request_today_from_realtime_providers(self):
        morning = NOW.replace(hour=8) + dt.timedelta(days=1)
        with mock.patch.object(S, "_load_cached_snapshot", return_value=snapshot(rows())), \
                mock.patch.object(S.dfc_module, "fetch_market_snapshot_full", side_effect=AssertionError("future facts")), \
                mock.patch.object(S.dfc_module, "fetch_tencent_market_snapshot", side_effect=AssertionError("future fallback")):
            reading = S.refresh_snapshot(C.CLOSE_SNAPSHOT_POLICY, now=morning, asof_day=NOW.date())
            self.assertEqual("fresh", reading.freshness)
            forced = S.refresh_rows(policy=C.CLOSE_SNAPSHOT_POLICY, now=morning,
                                    asof_day=NOW.date(), force=True)
            self.assertEqual([], forced)

    def test_historical_close_rejects_a_cached_current_day(self):
        tomorrow = NOW + dt.timedelta(days=1)
        with mock.patch.object(S, "_load_cached_snapshot", return_value=snapshot(rows(tomorrow))), \
                mock.patch.object(S.dfc_module, "fetch_market_snapshot_full", side_effect=AssertionError("future facts")):
            actual = S.refresh_rows(policy=C.CLOSE_SNAPSHOT_POLICY, now=tomorrow, asof_day=NOW.date())
        self.assertEqual([], actual)

    def test_force_fetch_has_a_real_observation_window(self):
        data=rows()
        with mock.patch.object(S.dfc_module,"load_market_snapshot_full_payload",return_value={}), \
                mock.patch.object(S.dfc_module,"fetch_market_snapshot_full",return_value=data) as fetch:
            r=S.refresh_snapshot(C.CLOSE_SNAPSHOT_POLICY,now=NOW,force=True)
        fetch.assert_called_once_with(max_age=10860.,force=True)
        self.assertEqual("fresh",r.freshness)

    def test_preclose_does_not_request_or_publish_any_provider(self):
        with mock.patch.object(S.dfc_module,"load_market_snapshot_full_payload",return_value={}), \
                mock.patch.object(S.dfc_module,"fetch_market_snapshot_full",side_effect=AssertionError("early refresh")):
            r=S.refresh_snapshot(C.CLOSE_SNAPSHOT_POLICY,now=CLOSE-dt.timedelta(seconds=1),force=True)
        self.assertFalse(r.available)

    def test_tencent_fallback_receives_the_same_window(self):
        data=rows()
        for row in data:row["source"]="tencent_public_quote"
        with mock.patch.object(S.dfc_module,"load_market_snapshot_full_payload",return_value={}), \
                mock.patch.object(S.dfc_module,"fetch_market_snapshot_full",return_value=[]), \
                mock.patch("universe.load_universe",return_value=[{"code":r["code"]} for r in data]), \
                mock.patch.object(S.dfc_module,"fetch_tencent_market_snapshot",return_value=data) as fetch:
            r=S.refresh_snapshot(C.CLOSE_SNAPSHOT_POLICY,now=NOW,force=True)
        self.assertEqual(10860.,fetch.call_args.kwargs["max_age"])
        self.assertTrue(fetch.call_args.kwargs["force"])
        self.assertEqual("price_only_coverage",r.reason)

    def test_refresh_failure_cannot_promote_a_valid_old_close(self):
        data=rows()
        payload={"rows":data,"complete":True,"expected_rows":4000,"saved_at":NOW.isoformat()}
        with mock.patch.object(S.dfc_module,"load_market_snapshot_full_payload",return_value=payload), \
                mock.patch.object(S.dfc_module,"fetch_market_snapshot_full",return_value=[]), \
                mock.patch("universe.load_universe",return_value=[]):
            r=S.refresh_snapshot(C.CLOSE_SNAPSHOT_POLICY,now=NOW,force=True)
        self.assertEqual("refresh_failed",r.reason)
        self.assertEqual("stale",r.freshness)
        self.assertEqual(4000,len(r.rows()))

    def test_legacy_rows_drop_preclose_tail_even_when_ratio_passes(self):
        data=rows(CLOSE,4000)+rows(CLOSE-dt.timedelta(minutes=5),400,700000)
        r=C.classify(snapshot(data),C.CLOSE_SNAPSHOT_POLICY,now=NOW)
        with mock.patch.object(S,"refresh_snapshot",return_value=r):
            actual=S.refresh_rows(policy=C.CLOSE_SNAPSHOT_POLICY,now=NOW)
        self.assertEqual(4000,len(actual))
        self.assertTrue(all(row["quote_at"]==CLOSE.isoformat() for row in actual))


class FactorReadinessTests(unittest.TestCase):
    def test_after_close_selection_does_not_expand_duplicate_roster_codes(self):
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None): return cls.fromtimestamp(NOW.timestamp(), tz or ZONE)
            def astimezone(self, tz=None):
                # Model the production UTC+8 timezone even on a UTC CI host.
                return super().astimezone(tz or ZONE)
        class Calendar(dt.date):
            @classmethod
            def today(cls): return NOW.date()
        data = rows()
        for i, row in enumerate(data):
            row["code"] = f"{('600','601','603','605')[i // 1000]}{i % 1000:03d}"
        universe = [dict(r, name="ordinary") for r in data]
        # Zero is a usable quote; None/placeholders retain the existing factor,
        # while an explicit NaN clears it, as in the former scalar assignments.
        for i, value in enumerate((0, None, "--", "-", "", float("nan"))):
            data[i]["amount"] = value
        frame = pd.DataFrame({"last_date": NOW.date().isoformat(), "amount": 123.}, index=[r["code"] for r in data])
        reading = C.classify(snapshot(data, True), C.CLOSE_SNAPSHOT_POLICY, now=NOW)
        stale_flows = [dict(data[0], quote_at=(CLOSE-dt.timedelta(days=1)).isoformat(), super_net=999.)]
        def build(price, fund, *args, **kwargs):
            self.assertTrue(fund.index.is_unique)
            self.assertEqual({}, kwargs["realtime_super_flow"])
            self.assertEqual(0., price.iloc[0]["amount"])
            self.assertTrue(price.iloc[1:5]["amount"].eq(123.).all())
            self.assertTrue(pd.isna(price.iloc[5]["amount"]))
            self.assertTrue(price["price"].eq(10.).all())
            self.assertTrue(price["pct"].eq(1.).all())
            table = pd.DataFrame(index=price.index)
            table["pct"] = fund["pct_today"]
            return table
        with mock.patch.object(M.datetime, "datetime", Clock), \
                mock.patch.object(M.datetime, "date", Calendar), \
                mock.patch.object(M.U, "is_trade_day", return_value=True), \
                mock.patch.object(M.U, "latest_complete_trade_date", return_value=NOW.date()), \
                mock.patch.object(M.U, "load_universe", return_value=universe+universe[:15]), \
                mock.patch.object(M.U, "coverage_report", return_value={"fresh_selection":4000,"fresh_selection_pct":100,"expected_reference_date":NOW.date().isoformat()}), \
                mock.patch.object(M, "_load_selection_base", return_value=(frame,set(),{"state":"ready"})), \
                mock.patch.object(M.MDSvc, "read_snapshot", return_value=reading), \
                mock.patch.object(M.MDSvc, "refresh_rows", side_effect=AssertionError("valid close should be read only")), \
                mock.patch.object(M, "_load_super_flow", return_value=stale_flows), \
                mock.patch.object(M, "_schedule_super_flow_refresh") as queue, \
                mock.patch.object(M, "_refresh_super_flow") as refresh, \
                mock.patch.dict(M._LIVE_CACHE, finance={"data":{},"report_dates":[]}, sentiment={}), \
                mock.patch.dict(M._SELECTION_CONTEXT_STATE, {key:{"refreshing":False,"retry_at":0.} for key in ("finance","sentiment")}), \
                mock.patch.object(M.threading, "Thread") as workers, \
                mock.patch.object(M.dfc, "fetch_finance_latest") as finance, \
                mock.patch.object(M.F, "compute_sentiment_factors") as sentiment, \
                mock.patch.object(M.S, "build_factor_table", side_effect=build), \
                mock.patch.object(M.S, "run_strategy", return_value={"picks":[]}):
            result = M._select_uncached(strategy="three_day", topn=10)
        self.assertFalse(result.get("need_init"), result.get("message"))
        self.assertEqual("close_snapshot", result["data_quality"]["quote_policy"])
        self.assertEqual(4000, result["data_quality"]["live_quote_coverage"])
        queue.assert_called_once_with(topn=4000)
        refresh.assert_not_called()
        finance.assert_not_called()
        sentiment.assert_not_called()
        self.assertEqual(2, workers.call_count)
        self.assertEqual(["finance", "sentiment"], result["context_pending"])

    def test_universe_publication_invalidates_the_factor_readiness_signature(self):
        with mock.patch.object(M, "_health_file_signature", side_effect=lambda p: (str(p), 1)):
            first = M._selection_signature()
        with mock.patch.object(M, "_health_file_signature", side_effect=lambda p: (str(p), 2 if p == M.U.UNIVERSE_PATH else 1)):
            second = M._selection_signature()
        self.assertNotEqual(first, second)

    def context(self):
        stack=__import__("contextlib").ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(M._SELECTION_CACHE,{"signature":None,"price_factors":None,"status":None}))
        stack.enter_context(mock.patch.object(M,"_selection_signature",return_value=("unchanged",)))
        stack.enter_context(mock.patch.object(M,"_complete_daily_cutoff",return_value=NOW.date()))
        stack.enter_context(mock.patch.object(M.U,"load_universe",return_value=[]))
        return stack

    def test_invalid_snapshot_is_cached_without_request_rebuild(self):
        st=self.context()
        read=st.enter_context(mock.patch.object(M.SFS,"read",side_effect=ValueError("bad pair")))
        st.enter_context(mock.patch.object(P,"_rebuild_selection_factor_cache",side_effect=AssertionError("request rebuild")))
        for _ in range(2):
            f,_,status=M._load_selection_base(with_status=True)
            self.assertTrue(f.empty)
            self.assertEqual("factor_cache_invalid",status["reason_code"])
        self.assertEqual(1,read.call_count)

    def test_date_gate_is_exposed_without_calling_live_quotes(self):
        st=self.context()
        frame=pd.DataFrame({c:[True] for c in M._SELECTION_REQUIRED_COLUMNS},index=["600001"])
        st.enter_context(mock.patch.object(M.SFS,"read",return_value=(frame,{})))
        st.enter_context(mock.patch.object(P,"_selection_factor_freshness",return_value={"passed":False,
            "target_date":"2026-10-14","factor_date":"2026-10-13","reason":"waiting factors"}))
        st.enter_context(mock.patch.object(M.U,"coverage_report",return_value={"fresh_selection":4000,"fresh_selection_pct":100}))
        st.enter_context(mock.patch.object(M.MDSvc,"refresh_rows",side_effect=AssertionError("live request before factors")))
        r=M._select_uncached(strategy="three_day",topn=10)
        self.assertTrue(r["need_init"])
        self.assertIn("2026-10-13",r["message"])
        self.assertIn("2026-10-14",r["message"])
        self.assertEqual("factor_snapshot_not_ready",r["data_status"]["reason_code"])

    def test_missing_pair_has_a_distinct_cause(self):
        st=self.context()
        st.enter_context(mock.patch.object(M.SFS,"read",side_effect=FileNotFoundError("private path")))
        _,_,status=M._load_selection_base(with_status=True)
        self.assertEqual("factor_cache_missing",status["reason_code"])
        self.assertNotIn("private path",str(status))

    def test_new_file_signature_immediately_rechecks_a_negative_cache(self):
        st=self.context()
        frame=pd.DataFrame({c:[True] for c in M._SELECTION_REQUIRED_COLUMNS},index=["600001"])
        read=st.enter_context(mock.patch.object(M.SFS,"read",side_effect=[FileNotFoundError(),(frame,{})]))
        st.enter_context(mock.patch.object(P,"_selection_factor_freshness",return_value={"passed":True}))
        self.assertTrue(M._load_selection_base()[0].empty)
        with mock.patch.object(M,"_selection_signature",return_value=("new factors",)):
            f,_,status=M._load_selection_base(with_status=True)
        self.assertFalse(f.empty)
        self.assertEqual("ready",status["state"])
        self.assertEqual(2,read.call_count)


class ClosingRunnerTests(unittest.TestCase):
    def test_runner_consumes_authority_status_without_a_second_time_rule(self):
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return cls.fromtimestamp(NOW.timestamp(),tz or ZONE)
        r=C.classify(snapshot(rows(),True),C.CLOSE_SNAPSHOT_POLICY,now=NOW)
        out=io.StringIO()
        with mock.patch.object(R.dt,"datetime",Clock), mock.patch("universe.is_trade_day",return_value=True), \
                mock.patch.object(R.MDSvc,"refresh_snapshot",return_value=r), redirect_stdout(out):
            code=R.main()
        value=json.loads(out.getvalue())
        self.assertEqual(0,code)
        self.assertEqual(4000,value["fresh_rows"])
        self.assertEqual("degraded",value["market_data"]["status"])
        self.assertEqual("none",value["market_data"]["verification_method"])

    def test_runner_keeps_the_absolute_fresh_row_floor(self):
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return cls.fromtimestamp(NOW.timestamp(),tz or ZONE)
        r=C.classify(snapshot(rows(CLOSE,3600)+rows(CLOSE-dt.timedelta(minutes=5),400,700000)),
                     C.CLOSE_SNAPSHOT_POLICY,now=NOW)
        out=io.StringIO()
        with mock.patch.object(R.dt,"datetime",Clock), mock.patch("universe.is_trade_day",return_value=True), \
                mock.patch.object(R.MDSvc,"refresh_snapshot",return_value=r), redirect_stdout(out):
            code=R.main()
        self.assertEqual(1,code)
        self.assertEqual("stale_rejected",json.loads(out.getvalue())["status"])


if __name__=="__main__":unittest.main()
