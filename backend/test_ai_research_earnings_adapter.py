# -*- coding: utf-8 -*-
"""``ai_research_earnings_adapter`` 的回归。

验证的是**边界**而不是字符串：owner 的三态怎么归到 research 的三态、身份与业务日是否
真的由 owner 派生（而不是调用方自述）、以及哪些维度**不得**进 evidence。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import unittest
from decimal import Decimal
from unittest import mock

import ai_research_contract as ARC
import ai_research_earnings_adapter as ADAPTER
import earnings_disclosure_ledger as LEDGER

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
LATER = dt.datetime(2026, 7, 20, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    LEDGER.ensure_schema(conn)
    return conn


def _record(conn, **overrides) -> None:
    payload = dict(
        code="002230",
        title="科大讯飞:2026年半年度业绩预告",
        announcement_id="AN202607141826962804",
        source_name="eastmoney-public-notice",
        source_type="public_aggregator",
        published_at="2026-07-14T20:41:29+08:00",
        body=FORECAST_BODY,
        observed_at=OBSERVED,
    )
    payload.update(overrides)
    LEDGER.record_announcement(conn, **payload)
    conn.commit()


def _projections(conn) -> dict:
    return {
        item.announcement_id: item
        for item in LEDGER.earnings_event_projections(conn, as_of="2026-12-31")
    }


class EvidenceMappingTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_verified_projection_maps_to_owner_verified_evidence(self):
        _record(self.conn)
        projection = _projections(self.conn)["AN202607141826962804"]
        ref = ADAPTER.evidence_ref_from_earnings_projection(projection)
        self.assertIsInstance(ref, ARC.ResearchEvidenceRef)
        self.assertEqual(ref.source_type, ARC.EVIDENCE_SOURCE_DISCLOSURE)
        self.assertEqual(ref.source_id, projection.identity)
        self.assertEqual(ref.as_of, "2026-07-16")
        self.assertEqual(ref.owner_verification.outcome, ARC.OWNER_OUTCOME_VERIFIED)
        self.assertEqual(ref.owner_verification.status, LEDGER.VERIFICATION_VERIFIED)
        self.assertTrue(ref.owner_verification.is_verified)
        detail = dict(ref.detail)
        self.assertEqual(detail["announcement_id"], "AN202607141826962804")
        self.assertEqual(detail["kind"], "forecast")
        self.assertEqual(detail["contract_version"], LEDGER.EARNINGS_LEDGER_VERSION)
        self.assertEqual(detail["body_sha256"], projection.body_sha256)
        self.assertEqual(len(detail["content_fingerprint"]), 64)

    def test_as_of_never_comes_from_the_source_publication_time(self):
        _record(self.conn)  # 来源声称 7/14 发布，系统 7/16 才观测到
        projection = _projections(self.conn)["AN202607141826962804"]
        ref = ADAPTER.evidence_ref_from_earnings_projection(projection)
        self.assertEqual(projection.published_at, "2026-07-14T20:41:29+08:00")
        self.assertEqual(ref.as_of, "2026-07-16")
        self.assertNotEqual(ref.as_of, "2026-07-14")

    def test_unparsable_projection_is_source_unusable_not_unverified(self):
        _record(self.conn, announcement_id="AN-UNREADABLE", body=UNPARSABLE_BODY)
        projection = _projections(self.conn)["AN-UNREADABLE"]
        ref = ADAPTER.evidence_ref_from_earnings_projection(projection)
        self.assertEqual(ref.owner_verification.outcome, ARC.OWNER_OUTCOME_SOURCE_UNUSABLE)
        self.assertEqual(ref.owner_verification.status, LEDGER.VERIFICATION_UNPARSABLE)
        self.assertFalse(ref.owner_verification.is_verified)
        # 障碍在核验过程，不在事实质量 —— 这两者必须给出不同的 outcome。
        self.assertNotEqual(ref.owner_verification.outcome, ARC.OWNER_OUTCOME_UNVERIFIED)

    def test_superseded_projection_is_unverified(self):
        _record(self.conn)
        _record(self.conn, announcement_id="AN-CORRECTION",
                title="某公司:2026年半年度业绩预告修正公告",
                body="归属于上市公司股东的净利润 8,000 万元到 9,000 万元\n",
                observed_at=LATER, published_at=None)
        projection = _projections(self.conn)["AN202607141826962804"]
        self.assertEqual(projection.superseded_by, "AN-CORRECTION")
        ref = ADAPTER.evidence_ref_from_earnings_projection(projection)
        self.assertEqual(ref.owner_verification.outcome, ARC.OWNER_OUTCOME_UNVERIFIED)
        self.assertEqual(ref.owner_verification.status, LEDGER.VERIFICATION_SUPERSEDED)
        self.assertFalse(ref.owner_verification.is_verified)

    def test_attributes_carry_only_time_invariant_dimensions(self):
        _record(self.conn)
        projection = _projections(self.conn)["AN202607141826962804"]
        ref = ADAPTER.evidence_ref_from_earnings_projection(projection)
        attributes = dict(ref.owner_verification.attributes)
        self.assertEqual(attributes["kind"], "forecast")
        self.assertEqual(attributes["report_period"], "2026-06-30")
        self.assertFalse(attributes["is_correction"])
        self.assertTrue(attributes["has_figures"])
        # 会随外部事件变化的维度不得进 attributes —— 否则同一份事实在两次读取之间
        # 会被冲突检测报成互相矛盾。
        self.assertNotIn("superseded_by", attributes)
        self.assertNotIn("latest_claim_status", attributes)
        self.assertNotIn("low_wan", attributes)

    def test_fingerprint_is_deterministic_and_content_sensitive(self):
        _record(self.conn)
        _record(self.conn, announcement_id="AN-OTHER", code="600036",
                title="招商银行:2025年度业绩快报公告",
                body="单位：元\n归属于本行股东的净利润 1,501,810 1,483,910 1.21%\n",
                observed_at=LATER, published_at=None)
        items = _projections(self.conn)
        first = ADAPTER.evidence_ref_from_earnings_projection(
            items["AN202607141826962804"]).detail["content_fingerprint"]
        again = ADAPTER.evidence_ref_from_earnings_projection(
            items["AN202607141826962804"]).detail["content_fingerprint"]
        other = ADAPTER.evidence_ref_from_earnings_projection(
            items["AN-OTHER"]).detail["content_fingerprint"]
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)


class ForgedProjectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        _record(self.conn)
        self.projection = _projections(self.conn)["AN202607141826962804"]

    def tearDown(self):
        self.conn.close()

    def test_mapping_rejects_dicts_and_duck_typed_objects(self):
        with self.assertRaises(TypeError):
            ADAPTER.evidence_ref_from_earnings_projection(dict(self.projection.as_dict()))

        class _Fake:
            identity = "earnings|forged"
            availability_day = "2026-07-16"
            verification_status = LEDGER.VERIFICATION_VERIFIED
            kind = "forecast"
            report_period = "2026-06-30"
            is_correction = False
            verification_method = LEDGER.VERIFICATION_METHOD_BODY_RECOMPUTE
            low_wan = Decimal("1")
            announcement_id = "AN-FAKE"
            body_sha256 = "0" * 64
            version = LEDGER.EARNINGS_LEDGER_VERSION

            def as_dict(self):
                return {}

        with self.assertRaises(TypeError):
            ADAPTER.evidence_ref_from_earnings_projection(_Fake())

    def test_mapping_rejects_subclasses(self):
        class _Sub(LEDGER.EarningsEventProjection):
            pass

        fake = object.__new__(_Sub)
        with self.assertRaises(TypeError):
            ADAPTER.evidence_ref_from_earnings_projection(fake)


class VocabularyDriftTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        _record(self.conn)
        self.projection = _projections(self.conn)["AN202607141826962804"]

    def tearDown(self):
        self.conn.close()

    def test_missing_owner_status_fails_closed(self):
        # owner 新增一个状态但 adapter 没登记 → 必须拒绝，而不是静默归成某一态。
        drifted = dict(ADAPTER._EARNINGS_OUTCOME_BY_STATUS)
        drifted.pop(LEDGER.VERIFICATION_UNPARSABLE)
        with mock.patch.object(ADAPTER, "_EARNINGS_OUTCOME_BY_STATUS", drifted):
            with self.assertRaises(ValueError):
                ADAPTER.evidence_ref_from_earnings_projection(self.projection)

    def test_stale_mapping_entry_fails_closed(self):
        drifted = dict(ADAPTER._EARNINGS_OUTCOME_BY_STATUS)
        drifted["partially_verified"] = ARC.OWNER_OUTCOME_VERIFIED
        with mock.patch.object(ADAPTER, "_EARNINGS_OUTCOME_BY_STATUS", drifted):
            with self.assertRaises(ValueError):
                ADAPTER.evidence_ref_from_earnings_projection(self.projection)

    def test_mapping_agrees_with_owner_closed_set_exactly(self):
        self.assertEqual(
            [], ADAPTER._earnings_outcome_mapping_problems(
                ADAPTER._EARNINGS_OUTCOME_BY_STATUS, LEDGER.VERIFICATION_STATUSES,
            ),
        )


class ContractRegistrationTest(unittest.TestCase):
    def test_disclosure_source_type_is_registered_everywhere(self):
        self.assertIn(ARC.EVIDENCE_SOURCE_DISCLOSURE, ARC.EVIDENCE_SOURCE_TYPES)
        self.assertIn(ARC.EVIDENCE_SOURCE_DISCLOSURE, ARC.SUPPORTED_OWNER_ADAPTERS)
        self.assertIn(ARC.EVENT_DISCLOSURE_OBSERVED, ARC.EVENT_KINDS)
        self.assertEqual(
            ARC._KIND_BY_SOURCE_TYPE[ARC.EVIDENCE_SOURCE_DISCLOSURE],
            ARC.EVENT_DISCLOSURE_OBSERVED,
        )
        self.assertEqual(len(ARC._KIND_BY_SOURCE_TYPE), len(ARC.EVIDENCE_SOURCE_TYPES))

    def test_disclosure_is_not_smuggled_in_as_news(self):
        # 两类事实的核验语义不同，共用标签会让 news 的三态闭集被污染。
        self.assertNotEqual(ARC.EVIDENCE_SOURCE_DISCLOSURE, ARC.EVIDENCE_SOURCE_NEWS)
        self.assertIn("evidence_ref_from_earnings_projection", ADAPTER.__all__)
        self.assertNotIn("evidence_ref_from_news_projection", ADAPTER.__all__)


if __name__ == "__main__":
    unittest.main()
