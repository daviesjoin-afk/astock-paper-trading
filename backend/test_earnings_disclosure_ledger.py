# -*- coding: utf-8 -*-
"""``earnings_disclosure_ledger`` 的回归。

夹具正文都取自**真实公告原文**（截断，未改数字），与
``test_earnings_disclosure_facts`` 同一来源。本文件只验证 owner 层的
身份 / PIT / 修正链 / 复核记录，不重复验证解析器本身。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import unittest
from decimal import Decimal

import earnings_disclosure_facts as EDF
import earnings_disclosure_ledger as L

# 科大讯飞 2026 半年度业绩预告（表格式亏损区间，原文截断）
FORECAST_BODY = """
    一、本期业绩预计情况

    （一）业绩预告期间

    2026 年 1 月 1 日至 2026 年 6 月 30 日

    项  目                          本报告期                            上年同期

归属于上市公司 亏损：-22,800 万元～-18,000 万元            亏损：-23,892.72 万元
股东的净利润    比上年同期增长：5%～25%
"""

# 招商银行 2025 年度业绩快报（单位百万元，原文截断）
EXPRESS_BODY = """
  一、主要财务数据和指标

                                            （除特别说明外，单位人民币百万元）

        主要财务数据          2025 年        2024 年      增减变动幅度

 归属于本行股东的净利润          150,181        148,391          1.21%
"""

UNPARSABLE_BODY = "本公司预计本期业绩与上年同期相比发生较大变动，详见定期报告。"

T0 = dt.datetime(2026, 7, 14, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
T1 = dt.datetime(2026, 7, 15, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
T2 = dt.datetime(2026, 7, 20, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
T3 = dt.datetime(2026, 7, 25, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _conn(with_schema: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    if with_schema:
        L.ensure_schema(conn)
    return conn


def _forecast(conn, **overrides) -> L.IngestOutcome:
    payload = dict(
        code="002230",
        title="科大讯飞:2026年半年度业绩预告",
        announcement_id="AN202607141826962804",
        source_name="eastmoney-public-notice",
        source_type="public_aggregator",
        source_url=None,
        published_at="2026-07-14T20:41:29+08:00",
        body=FORECAST_BODY,
        observed_at=T0,
    )
    payload.update(overrides)
    return L.record_announcement(conn, **payload)


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_parsed_announcement_is_recorded_and_verified(self):
        outcome = _forecast(self.conn)
        self.conn.commit()
        self.assertEqual(outcome.status, L.INGEST_RECORDED)
        self.assertEqual(outcome.verification_status, L.VERIFICATION_VERIFIED)
        self.assertEqual(outcome.event_key, "earnings|002230|2026-06-30|forecast|"
                                            "AN202607141826962804")
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-14")
        self.assertTrue(projection.is_owner_verified)
        self.assertEqual(projection.low_wan, Decimal("-22800"))
        self.assertEqual(projection.high_wan, Decimal("-18000"))
        self.assertEqual(projection.yoy_low_pct, Decimal("5"))
        self.assertEqual(projection.unit, "万元")
        self.assertEqual(projection.report_period, "2026-06-30")
        self.assertEqual(projection.availability_day, "2026-07-14")
        self.assertIsNone(projection.superseded_by)
        self.assertIsNone(projection.latest_claim)

    def test_decimal_round_trip_is_exact(self):
        _forecast(self.conn, code="600036", title="招商银行:2025年度业绩快报公告",
                  announcement_id="AN202601231818331868", body=EXPRESS_BODY)
        self.conn.commit()
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-14")
        self.assertEqual(projection.low_wan, Decimal("15018100"))
        self.assertEqual(projection.high_wan, Decimal("15018100"))
        # 文本列存的是规范十进制，读回来必须是**同一个数**，不是浮点近似。
        self.assertEqual(L._decimal_text(projection.low_wan), "15018100")

    def test_duplicate_ingest_does_not_move_first_seen(self):
        _forecast(self.conn)
        again = _forecast(self.conn, observed_at=T2)
        self.conn.commit()
        self.assertEqual(again.status, L.INGEST_DUPLICATE)
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-14")
        self.assertEqual(projection.first_seen_at, "2026-07-14T21:00:00+08:00")
        # 晚到的重复摄取不能让它在早先的日期变得可见。
        self.assertEqual(L.earnings_event_projections(self.conn, as_of="2026-07-13"), ())

    def test_conflicting_content_for_same_announcement_fails_closed(self):
        _forecast(self.conn)
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            _forecast(self.conn, body=FORECAST_BODY.replace("-18,000", "-17,000"))
        self.assertEqual(ctx.exception.reason, "announcement_content_conflict")
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-14")
        self.assertEqual(projection.high_wan, Decimal("-18000"))

    def test_unsupported_title_is_rejected_without_a_row(self):
        outcome = _forecast(
            self.conn, title="贵州茅台:关于召开2026年半年度业绩说明会的公告",
        )
        self.conn.commit()
        self.assertEqual(outcome.status, L.INGEST_REJECTED)
        self.assertEqual(outcome.reason, "not_earnings_announcement")
        self.assertIsNone(outcome.event_key)
        self.assertEqual(L.earnings_event_projections(self.conn, as_of="2026-07-14"), ())

    def test_unparsable_body_is_recorded_but_never_verified(self):
        outcome = _forecast(self.conn, body=UNPARSABLE_BODY)
        self.conn.commit()
        self.assertEqual(outcome.status, L.INGEST_RECORDED)
        self.assertEqual(outcome.verification_status, L.VERIFICATION_UNPARSABLE)
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-14")
        self.assertFalse(projection.is_owner_verified)
        self.assertIsNone(projection.low_wan)

    def test_naive_observed_at_fails_closed(self):
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            _forecast(self.conn, observed_at=dt.datetime(2026, 7, 14, 21, 0))
        self.assertEqual(ctx.exception.reason, "bad_observed_at")

    def test_missing_identity_fields_fail_closed(self):
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            _forecast(self.conn, announcement_id="  ")
        self.assertEqual(ctx.exception.reason, "missing_announcement_id")
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            _forecast(self.conn, source_type="")
        self.assertEqual(ctx.exception.reason, "missing_source")

    def test_alien_code_is_rejected(self):
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            _forecast(self.conn, code="00223")
        self.assertEqual(ctx.exception.reason, "missing_announcement_id")


class CorrectionChainTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        _forecast(self.conn, announcement_id="AN-ORIGINAL", body=(
            "归属于上市公司股东的净利润 10,000 万元到 12,000 万元\n"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _correction(self, announcement_id: str, body: str, observed_at) -> None:
        _forecast(self.conn, title="某公司:2026年半年度业绩预告修正公告",
                  announcement_id=announcement_id, body=body, observed_at=observed_at,
                  published_at=None)
        self.conn.commit()

    def _by_announcement(self) -> dict:
        return {
            item.announcement_id: item
            for item in L.earnings_event_projections(self.conn, as_of="2026-12-31")
        }

    def test_correction_supersedes_the_earlier_announcement(self):
        self._correction("AN-CORRECTION-1",
                         "归属于上市公司股东的净利润 8,000 万元到 9,000 万元\n", T2)
        items = self._by_announcement()
        original = items["AN-ORIGINAL"]
        corrected = items["AN-CORRECTION-1"]
        self.assertEqual(original.verification_status, L.VERIFICATION_SUPERSEDED)
        self.assertEqual(original.superseded_by, "AN-CORRECTION-1")
        self.assertFalse(original.is_owner_verified)
        # 过期不等于错误：当时的数字必须留着，复盘才能看到"当时拿到的是多少"。
        self.assertEqual(original.low_wan, Decimal("10000"))
        self.assertEqual(original.high_wan, Decimal("12000"))
        self.assertTrue(corrected.is_owner_verified)
        self.assertEqual(corrected.low_wan, Decimal("8000"))

    def test_supersede_is_forward_only(self):
        self._correction("AN-CORRECTION-1",
                         "归属于上市公司股东的净利润 8,000 万元到 9,000 万元\n", T2)
        self._correction("AN-CORRECTION-2",
                         "归属于上市公司股东的净利润 7,500 万元到 8,200 万元\n", T3)
        items = self._by_announcement()
        # 原始行仍然指向**第一份**修正公告：历史指向不会被后来的修正改写。
        self.assertEqual(items["AN-ORIGINAL"].superseded_by, "AN-CORRECTION-1")
        self.assertEqual(items["AN-CORRECTION-1"].superseded_by, "AN-CORRECTION-2")
        self.assertEqual(items["AN-CORRECTION-2"].verification_status,
                         L.VERIFICATION_VERIFIED)

    def test_non_correction_announcement_does_not_supersede(self):
        _forecast(self.conn, announcement_id="AN-SECOND",
                  body="归属于上市公司股东的净利润 10,000 万元到 12,000 万元\n",
                  observed_at=T2, published_at=None)
        self.conn.commit()
        items = self._by_announcement()
        self.assertEqual(items["AN-ORIGINAL"].verification_status,
                         L.VERIFICATION_VERIFIED)
        self.assertIsNone(items["AN-ORIGINAL"].superseded_by)

    def test_correction_does_not_supersede_a_later_announcement(self):
        # 一份"更晚观测到"的非修正公告不应被补标：只有更早的行才会被取代。
        _forecast(self.conn, announcement_id="AN-LATER",
                  body="归属于上市公司股东的净利润 10,000 万元到 12,000 万元\n",
                  observed_at=T3, published_at=None)
        self.conn.commit()
        _forecast(self.conn, title="某公司:2026年半年度业绩预告修正公告",
                  announcement_id="AN-CORRECTION-EARLIER",
                  body="归属于上市公司股东的净利润 8,000 万元到 9,000 万元\n",
                  observed_at=T1, published_at=None)
        self.conn.commit()
        items = self._by_announcement()
        self.assertIsNone(items["AN-LATER"].superseded_by)


class PitReadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_availability_follows_first_seen_not_published_at(self):
        # 来源声称 7/14 20:41 发布，系统 7/16 才第一次观测到。
        _forecast(self.conn, published_at="2026-07-14T20:41:29+08:00",
                  observed_at=dt.datetime(2026, 7, 16, 10, 0,
                                          tzinfo=dt.timezone(dt.timedelta(hours=8))))
        self.conn.commit()
        self.assertEqual(L.earnings_event_projections(self.conn, as_of="2026-07-14"), ())
        self.assertEqual(L.earnings_event_projections(self.conn, as_of="2026-07-15"), ())
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-16")
        self.assertEqual(projection.availability_day, "2026-07-16")
        # published_at 仍然作为描述性字段保留，供审计对照。
        self.assertEqual(projection.published_at, "2026-07-14T20:41:29+08:00")

    def test_availability_day_uses_owner_timezone(self):
        # 2026-09-20T16:30+00:00 在上海已经是 9/21 00:30。
        _forecast(self.conn, observed_at=dt.datetime(2026, 9, 20, 16, 30,
                                                     tzinfo=dt.timezone.utc))
        self.conn.commit()
        self.assertEqual(L.earnings_event_projections(self.conn, as_of="2026-09-20"), ())
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-09-21")
        self.assertEqual(projection.availability_day, "2026-09-21")

    def test_missing_ledger_fails_closed_and_creates_nothing(self):
        conn = _conn(with_schema=False)
        try:
            with self.assertRaises(L.EarningsLedgerError) as ctx:
                L.earnings_event_projections(conn, as_of="2026-07-14")
            self.assertEqual(ctx.exception.reason, "ledger_unavailable")
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertNotIn(L.EARNINGS_EVENTS_TABLE, tables)
        finally:
            conn.close()

    def test_bad_read_parameters_are_rejected(self):
        for value in ("2026-7-14", "", None, "20260714"):
            with self.subTest(as_of=value):
                with self.assertRaises(L.EarningsLedgerError) as ctx:
                    L.earnings_event_projections(self.conn, as_of=value)
                self.assertEqual(ctx.exception.reason, "bad_as_of")
        for value in (0, -1, True, "10"):
            with self.subTest(limit=value):
                with self.assertRaises(L.EarningsLedgerError) as ctx:
                    L.earnings_event_projections(self.conn, as_of="2026-07-14",
                                                 limit=value)
                self.assertEqual(ctx.exception.reason, "bad_limit")

    def test_read_is_ordered_and_limited(self):
        _forecast(self.conn, announcement_id="AN-B", observed_at=T2)
        _forecast(self.conn, code="600036", title="招商银行:2025年度业绩快报公告",
                  announcement_id="AN-A", body=EXPRESS_BODY, observed_at=T1)
        self.conn.commit()
        items = L.earnings_event_projections(self.conn, as_of="2026-12-31")
        self.assertEqual([item.announcement_id for item in items], ["AN-A", "AN-B"])
        self.assertEqual(len(L.earnings_event_projections(self.conn, as_of="2026-12-31",
                                                          limit=1)), 1)


class ClaimCheckTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        _forecast(self.conn)
        self.conn.commit()
        self.event_key = "earnings|002230|2026-06-30|forecast|AN202607141826962804"
        self.meta = EDF.classify_announcement("科大讯飞:2026年半年度业绩预告")

    def tearDown(self):
        self.conn.close()

    def _verdict(self, low="-22800", high="-18000"):
        claim = EDF.EarningsClaim(
            kind=EDF.ANNOUNCEMENT_KIND_FORECAST, report_period="2026-06-30",
            metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
            low_wan=Decimal(low), high_wan=Decimal(high),
            yoy_low_pct=Decimal("5"), yoy_high_pct=Decimal("25"))
        return claim, EDF.verify_earnings_claim(claim, self.meta, FORECAST_BODY)

    def test_latest_check_wins_and_is_attached_to_the_projection(self):
        claim, verdict = self._verdict()
        L.record_claim_check(self.conn, event_key=self.event_key, claim=claim,
                             verdict=verdict, checked_at=T1)
        claim2, verdict2 = self._verdict(high="-17500")
        fingerprint = L.record_claim_check(self.conn, event_key=self.event_key,
                                           claim=claim2, verdict=verdict2, checked_at=T2)
        self.conn.commit()
        (projection,) = L.earnings_event_projections(self.conn, as_of="2026-07-20")
        self.assertIsNotNone(projection.latest_claim)
        self.assertEqual(projection.latest_claim.checked_at, "2026-07-20T09:30:00+08:00")
        self.assertEqual(projection.latest_claim.status, EDF.STATUS_CLAIM_FIGURE_MISMATCH)
        self.assertEqual(projection.latest_claim.disagreements, ("high_wan",))
        self.assertEqual(projection.latest_claim.claim_fingerprint, fingerprint)

    def test_forged_verdict_is_rejected(self):
        claim, _ = self._verdict()
        with self.assertRaises(TypeError):
            L.record_claim_check(self.conn, event_key=self.event_key, claim=claim,
                                 verdict={"status": "verified"}, checked_at=T1)
        with self.assertRaises(TypeError):
            L.record_claim_check(self.conn, event_key=self.event_key,
                                 claim={"kind": "forecast"},
                                 verdict=self._verdict()[1], checked_at=T1)

    def test_verdict_for_another_body_is_rejected(self):
        claim = EDF.EarningsClaim(
            kind=EDF.ANNOUNCEMENT_KIND_FORECAST, report_period="2026-06-30",
            metric=EDF.METRIC_NET_PROFIT_ATTRIBUTABLE,
            low_wan=Decimal("10000"), high_wan=Decimal("12000"))
        other_verdict = EDF.verify_earnings_claim(claim, self.meta, UNPARSABLE_BODY)
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            L.record_claim_check(self.conn, event_key=self.event_key,
                                 claim=claim, verdict=other_verdict, checked_at=T1)
        self.assertEqual(ctx.exception.reason, "announcement_content_conflict")

    def test_unknown_event_fails_closed(self):
        claim, verdict = self._verdict()
        with self.assertRaises(L.EarningsLedgerError) as ctx:
            L.record_claim_check(self.conn, event_key="earnings|nope", claim=claim,
                                 verdict=verdict, checked_at=T1)
        self.assertEqual(ctx.exception.reason, "unknown_event")


class ProjectionShapeTest(unittest.TestCase):
    def test_projection_cannot_be_constructed_by_callers(self):
        with self.assertRaises(TypeError):
            L.EarningsEventProjection(
                version=L.EARNINGS_LEDGER_VERSION, identity="x", announcement_id="x",
                code="002230", kind="forecast", report_period="2026-06-30",
                is_correction=False, title="t", source_name="s", source_type="s",
                source_url=None, published_at=None,
                first_seen_at="2026-07-14T21:00:00+08:00", availability_day="",
                body_sha256="0" * 64, verification_status=L.VERIFICATION_VERIFIED,
                verification_method=L.VERIFICATION_METHOD_BODY_RECOMPUTE,
                low_wan=Decimal("1"), high_wan=Decimal("2"),
                yoy_low_pct=None, yoy_high_pct=None, unit="万元",
                superseded_by=None, latest_claim=None)

    def _issue(self, **overrides):
        fields = dict(
            version=L.EARNINGS_LEDGER_VERSION, identity="earnings|x",
            announcement_id="AN1", code="002230", kind="forecast",
            report_period="2026-06-30", is_correction=False, title="t",
            source_name="s", source_type="s", source_url=None, published_at=None,
            first_seen_at="2026-07-14T21:00:00+08:00", availability_day="",
            body_sha256="0" * 64, verification_status=L.VERIFICATION_VERIFIED,
            verification_method=L.VERIFICATION_METHOD_BODY_RECOMPUTE,
            low_wan=Decimal("1"), high_wan=Decimal("2"),
            yoy_low_pct=None, yoy_high_pct=None, unit="万元",
            superseded_by=None, latest_claim=None,
        )
        fields.update(overrides)
        return L._issue_earnings_event_projection(**fields)

    def test_verified_without_figures_is_impossible(self):
        with self.assertRaises(ValueError):
            self._issue(low_wan=None, high_wan=None)

    def test_unparsable_with_figures_is_impossible(self):
        with self.assertRaises(ValueError):
            self._issue(verification_status=L.VERIFICATION_UNPARSABLE)

    def test_superseded_must_name_the_replacement(self):
        with self.assertRaises(ValueError):
            self._issue(verification_status=L.VERIFICATION_SUPERSEDED)

    def test_superseded_by_must_match_the_status(self):
        with self.assertRaises(ValueError):
            self._issue(superseded_by="AN2")

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            self._issue(verification_status="probably_fine")

    def test_availability_day_is_derived_not_accepted(self):
        projection = self._issue(availability_day="1999-01-01")
        self.assertEqual(projection.availability_day, "2026-07-14")


if __name__ == "__main__":
    unittest.main()
