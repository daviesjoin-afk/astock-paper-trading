import copy
import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

import execution_planner as EP
import paper_live_tradability as L
import tradability_archive as TA
import tradability_ingestion as TI


class LiveTradabilityTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(L.TZ)
        self.day = self.now.date().isoformat()
        self.quote = {"code": "600001", "price": 10.0, "pct": 1.0, "volume": 100,
                      "quote_at": self.now.isoformat(), "quote_source": "live",
                      "quote_validation": "cross_source_checked"}
        self.profile = {"rc": 0, "data": {"f57": "600001", "f58": "测试股",
                        "f189": 20000101, "f51": 12.0, "f52": 8.0,
                        "f43": 10.0, "f86": self.now.timestamp()}}
        self.report = {"success": True, "result": {"pages": 1, "count": 0, "data": []}}

    def prefetch(self, quote=None, profile=None, report=None):
        def get(key, url, params):
            value = (report or self.report) if key[0] == "suspend" else (profile or self.profile)
            return value, self.now.isoformat()
        with patch.object(L, "_get", side_effect=get):
            return L.prefetch({"600001": quote or self.quote}, asof_day=self.day)

    def test_profile_and_complete_report_produce_current_evidence(self):
        item = self.prefetch()["600001"]
        self.assertEqual(item["fields"]["listing_date"], "2000-01-01")
        self.assertIs(item["fields"]["is_suspended"], False)
        self.assertEqual(item["observed_at"], self.now.isoformat())
        self.assertEqual(item["source_evidence"]["suspension_count"], 0)

    def test_missing_suspension_page_or_unavailable_response_never_means_no_halt(self):
        for result in ({"success": False}, {"success": True, "result": {
                "pages": 1, "count": 2, "data": []}}):
            self.assertEqual(self.prefetch(report=result), {})

    def test_suspended_stock_is_recorded_as_blocked(self):
        report = {"success": True, "result": {"pages": 1, "count": 1, "data": [{
            "SECURITY_CODE": "600001", "SUSPEND_START_TIME": self.day + " 00:00:00",
            "SUSPEND_END_TIME": None}]}}
        self.assertIs(self.prefetch(report=report)["600001"]["fields"]["is_suspended"], True)

    def test_unknown_volume_is_not_zero_and_limit_price_is_not_assumed_unlocked(self):
        self.assertIsNone(self.prefetch(quote={**self.quote, "volume": None})["600001"]["fields"]["has_trade_volume"])
        self.assertEqual(self.prefetch(quote={**self.quote, "price": 12.0}), {})

    def test_old_profile_price_cannot_prove_current_unlocked_state(self):
        profile = copy.deepcopy(self.profile)
        profile["data"]["f86"] = (self.now - dt.timedelta(hours=1)).timestamp()
        self.assertEqual(self.prefetch(profile=profile), {})

    def test_mismatched_or_future_listing_profile_is_rejected(self):
        for change in ({"f57": "600002"}, {"f189": 20990101}):
            profile = copy.deepcopy(self.profile)
            profile["data"].update(change)
            self.assertEqual(self.prefetch(profile=profile), {})

    def test_stale_and_unverified_quotes_do_not_fetch_status(self):
        for change in ({"quote_validation": "cross_source_unavailable"},
                       {"quote_at": (self.now - dt.timedelta(hours=2)).isoformat()}):
            with patch.object(L, "_get") as fetch:
                self.assertEqual(L.prefetch({"600001": {**self.quote, **change}}, asof_day=self.day), {})
                fetch.assert_not_called()

    def database(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        TI.ensure_ingestion_schema(conn)
        conn.commit()
        return conn

    def test_write_boundary_ingests_atomically_and_replay_is_idempotent(self):
        conn = self.database()
        observation = self.prefetch()["600001"]
        quote = {**self.quote, "tradability_observation": observation,
                 "execution_asof": (self.now + dt.timedelta(seconds=1)).isoformat()}
        conn.execute("BEGIN IMMEDIATE")
        with patch.object(L, "http_get", side_effect=AssertionError("writer did network")):
            L.persist(conn, quote, code="600001", asof_day=self.day)
            L.persist(conn, quote, code="600001", asof_day=self.day)
        decision = TA.tradability_at("600001", self.day, decision_time=quote["execution_asof"],
                                    repository=TA.TradabilityArchiveRepository(conn))
        self.assertTrue(decision.can_buy and decision.can_sell)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM historical_tradability_archive").fetchone()[0], 1)
        conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM historical_tradability_archive").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tradability_ingestion_runs").fetchone()[0], 0)

    def test_future_or_wrong_symbol_observation_is_rejected_without_writes(self):
        conn = self.database()
        observation = self.prefetch()["600001"]
        for change in ({"code": "600002"}, {"observed_at": (self.now + dt.timedelta(hours=1)).isoformat()}):
            with self.assertRaises(ValueError):
                L.persist(conn, {**self.quote, "execution_asof": self.now.isoformat(),
                          "tradability_observation": {**observation, **change}},
                          code="600001", asof_day=self.day)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM historical_tradability_archive").fetchone()[0], 0)

    def test_failed_current_refresh_cannot_reuse_an_older_available_archive(self):
        conn = self.database()
        quote = {**self.quote, "tradability_observation": self.prefetch()["600001"],
                 "execution_asof": (self.now + dt.timedelta(seconds=1)).isoformat()}
        L.persist(conn, quote, code="600001", asof_day=self.day)
        self.assertTrue(TA.tradability_at("600001", self.day,
                        decision_time=quote["execution_asof"],
                        repository=TA.TradabilityArchiveRepository(conn)).can_sell)
        conn.execute("CREATE TABLE paper_position_lots(cycle_id INTEGER,account_id TEXT,code TEXT,"
                     "remaining_qty INTEGER,available_date TEXT)")
        context = EP.execution_context_from_facts(conn=conn,
            order={"id": 1, "account_id": "a", "code": "600001", "side": "sell", "cycle_id": 13},
            quote={**self.quote, "execution_asof": (self.now + dt.timedelta(seconds=2)).isoformat(),
                   "tradability_observation_status": "unavailable"}, asof_day=self.day)
        self.assertFalse(context.tradability.evidence_present)
