# -*- coding: utf-8 -*-
"""研究运行期消费业绩公告事实的回归。

验证的是**接线**而不是解析：``event_evidence`` 这一路是否真的把公告 owner 事实
变成了 typed 事件、PIT 是否仍然由 owner 说了算、以及公告库缺席时研究是否还能跑。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import ai_research_contract as ARC
import deepseek_research as DR
import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_store as STORE
import news_learning as NL

FORECAST_BODY = """
    一、本期业绩预计情况

    （一）业绩预告期间

    2026 年 1 月 1 日至 2026 年 6 月 30 日

    项  目                          本报告期                            上年同期

归属于上市公司 亏损：-22,800 万元～-18,000 万元            亏损：-23,892.72 万元
股东的净利润    比上年同期增长：5%～25%
"""

UNPARSABLE_BODY = "本公司预计本期业绩与上年同期相比发生较大变动，详见定期报告。"

OBSERVED = dt.datetime(2026, 7, 16, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
LATER = dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
MARKET_NOW = dt.datetime(2026, 7, 16, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _context(asof_day="2026-07-16"):
    return DR.ResearchAsOfContext(asof_day=asof_day, market_now=MARKET_NOW, targets=())


class DisclosureConsumptionTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = mock.patch.dict(os.environ, {"ASTOCK_DATA_DIR": self._directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 研究侧还会读 news owner；没有它的表会先抛错，与本轮改动无关，所以建出来。
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        NL.ensure_schema(self.conn)

    def _seed(self):
        STORE.ensure()
        with STORE.connect_writable() as conn:
            LEDGER.record_announcement(
                conn, code="002230", title="科大讯飞:2026年半年度业绩预告",
                announcement_id="AN-1", source_name="fixture",
                source_type="public_aggregator", body=FORECAST_BODY,
                observed_at=OBSERVED,
            )
            LEDGER.record_announcement(
                conn, code="002415", title="海康威视:2026年半年度业绩预告",
                announcement_id="AN-2", source_name="fixture",
                source_type="public_aggregator", body=UNPARSABLE_BODY,
                observed_at=OBSERVED,
            )
            LEDGER.record_announcement(
                conn, code="600036", title="招商银行:2026年半年度业绩预告",
                announcement_id="AN-FUTURE", source_name="fixture",
                source_type="public_aggregator", body=FORECAST_BODY,
                observed_at=LATER,
            )

    def _events(self, asof_day="2026-07-16"):
        return DR._collect_typed_events(
            "event_evidence", self.conn, None, _context(asof_day))

    def test_disclosure_facts_become_typed_events(self):
        self._seed()
        events = self._events()
        disclosure = [event for event in events
                      if event.evidence_ref.source_type == ARC.EVIDENCE_SOURCE_DISCLOSURE]
        self.assertEqual(2, len(disclosure))
        for event in disclosure:
            self.assertEqual(ARC.EVENT_DISCLOSURE_OBSERVED, event.kind)
            self.assertTrue(event.source.startswith("disclosure_owner."))
            self.assertEqual("2026-07-16", event.as_of)
        verified = next(event for event in disclosure
                        if "AN-1" in event.evidence_ref.source_id)
        self.assertTrue(verified.evidence_ref.owner_verification.is_verified)
        self.assertEqual("-22800", verified.payload["net_profit_wan"]["low"])
        self.assertEqual("-18000", verified.payload["net_profit_wan"]["high"])
        self.assertEqual("万元", verified.payload["net_profit_wan"]["unit"])
        self.assertEqual("5", verified.payload["yoy_pct"]["low"])
        self.assertEqual("25", verified.payload["yoy_pct"]["high"])
        unreadable = next(event for event in disclosure
                          if "AN-2" in event.evidence_ref.source_id)
        self.assertFalse(unreadable.evidence_ref.owner_verification.is_verified)
        self.assertEqual(ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
                         unreadable.evidence_ref.owner_verification.outcome)

    def test_payload_does_not_leak_verification_back_into_the_facts(self):
        self._seed()
        events = self._events()
        for event in events:
            if event.evidence_ref.source_type != ARC.EVIDENCE_SOURCE_DISCLOSURE:
                continue
            for leaked in ("verification_status", "verification", "is_verified",
                           "outcome", "latest_claim", "verification_method"):
                self.assertNotIn(leaked, event.payload,
                                 "核验结论必须只留在 evidence ref 里")

    def test_owner_pit_hides_announcements_observed_after_the_as_of_day(self):
        self._seed()
        disclosure = [event for event in self._events()
                      if event.evidence_ref.source_type == ARC.EVIDENCE_SOURCE_DISCLOSURE]
        self.assertNotIn("AN-FUTURE",
                         " ".join(event.evidence_ref.source_id for event in disclosure))
        later = [event for event in self._events(asof_day="2026-07-20")
                 if event.evidence_ref.source_type == ARC.EVIDENCE_SOURCE_DISCLOSURE]
        self.assertEqual(3, len(later))

    def test_missing_ledger_is_not_an_error_for_research(self):
        # 采集还没上线 / 库被清掉时，研究必须照跑：缺事实 = 当时没有证据。
        events = self._events()
        self.assertEqual([], [event for event in events
                              if event.evidence_ref.source_type
                              == ARC.EVIDENCE_SOURCE_DISCLOSURE])

    def test_collection_is_ordered_and_repeatable(self):
        self._seed()
        first = [event.evidence_ref.source_id for event in self._events()]
        second = [event.evidence_ref.source_id for event in self._events()]
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), first)


if __name__ == "__main__":
    unittest.main()
