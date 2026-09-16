# -*- coding: utf-8 -*-
"""历史可交易性证据摄取层契约测试。

这些测试只为一件事：**证据从哪里来、当时是否可知、是否足以支持历史决策**。

因此它们刻意大量断言**拒绝**与**未知**：Provider 缺失 → unknown（不是 False/True）；
冲突 → conflict（不是 last-write-wins）；今天的快照 → 不伪造过去 observed_at；
重复 ingest → 幂等。一个把"不知道"当成"可交易"、把"今天才知道"当成"当年已知"、
把"冲突"当成"随便选一个"的摄取层，比没有摄取层更糟。

夹具全部是内存 SQLite + 注入的 fake provider / fake reader，无网络、不触碰执行路径。
"""

import ast
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tradability_archive as TA  # noqa: E402
import tradability_ingestion as TI  # noqa: E402

_WORK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "work")
if _WORK_DIR not in sys.path:
    sys.path.insert(0, _WORK_DIR)

import backfill_tradability_archive as BF  # noqa: E402


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    TI.ensure_ingestion_schema(conn)
    return conn


class IngestionTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.repo = TA.TradabilityArchiveRepository(self.conn)
        self.audit_conn = self.conn

    def make_service(self, providers, *, cutoff=None):
        return TI.IngestionService(
            providers, self.repo, cutoff=cutoff, audit_conn=self.audit_conn
        )


# ───────────────────────────── 1. Listing 摄取 ─────────────────────────────


class ListingIngestion(IngestionTestCase):
    def test_listing_and_delisting_evidence_written(self):
        provider = TI.ListingStatusProvider(
            {
                "000001": {"listing_date": "2010-01-01"},
                "000002": {"listing_date": "2010-01-01", "delisting_date": "2024-06-30"},
            },
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        result = self.make_service([provider]).ingest(
            ["000001", "000002"], ["2024-01-10"], write=True
        )
        del result
        # 2024-01-10：000001 已上市；000002 已上市（未到退市日）
        ev1 = self.repo.evidence_at("000001", "2024-01-10", "2025-01-02T09:00:00+08:00")
        ev2 = self.repo.evidence_at("000002", "2024-01-10", "2025-01-02T09:00:00+08:00")
        self.assertIsNotNone(ev1)
        self.assertIsNotNone(ev2)
        self.assertTrue(ev1.is_listed)
        self.assertTrue(ev2.is_listed)

    def test_session_before_listing_is_not_listed(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-06-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2009-01-10"], write=True)
        ev = self.repo.evidence_at("000001", "2009-01-10", "2025-01-02T09:00:00+08:00")
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_listed)

    def test_session_at_or_after_delisting_is_not_listed(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01", "delisting_date": "2024-06-30"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2024-06-30"], write=True)
        ev = self.repo.evidence_at("000001", "2024-06-30", "2025-01-02T09:00:00+08:00")
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_listed)


# ───────────────────────────── 2. ST 历史摄取 ─────────────────────────────


class STHistoryIngestion(IngestionTestCase):
    def test_historical_st_not_inferred_from_current_name(self):
        """历史 ST 状态绝不能来自当前名称推断；provider 不给状态 → unknown。"""
        # 用一个"不提供任何 ST 记录"的 provider（等价于无历史状态源）。
        provider = TI.SecurityStateHistoryProvider(None)
        result = self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        # ST 未知 → 该字段计入 unknown_st，不落 is_st 值。
        self.assertEqual(1, result.coverage["sessions"]["2025-06-10"]["unknown_st"])

    def test_historical_st_from_state_archive(self):
        """有历史状态归档时，ST 事实来自归档行，而不是名称。"""
        import security_state_point_in_time as SSPIT

        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": True,
            "archive_source": "unit-test",
            "availability_basis": "session_close",
            "rows": [
                {
                    "code": "600001",
                    "effective_from": "2024-06-14",
                    "effective_to": "2024-06-21",
                    "name": "某某股份",
                    "risk_flag": True,
                }
            ],
        }
        archive = SSPIT.SecurityStateArchive.from_payload(payload)
        provider = TI.SecurityStateHistoryProvider(archive)
        self.make_service([provider]).ingest(["600001"], ["2024-06-14"], write=True)
        ev = self.repo.evidence_at("600001", "2024-06-14", "2024-06-15T09:00:00+08:00")
        self.assertIsNotNone(ev)
        self.assertTrue(ev.is_st)


# ───────────────────────────── 3. Suspension 区间 ─────────────────────────────


class SuspensionIngestion(IngestionTestCase):
    def test_suspended_during_interval_and_resumed_after(self):
        provider = TI.SuspensionHistoryProvider(
            [
                {
                    "code": "000001",
                    "effective_from": "2024-06-10",
                    "effective_to": "2024-06-15",
                    "reason": "重大事项",
                    "available_at": "2024-06-10T09:00:00+08:00",
                }
            ],
            complete=True,
            availability_basis="session_close",
        )
        self.make_service([provider]).ingest(
            ["000001"], ["2024-06-12", "2024-06-20"], write=True
        )
        suspended = self.repo.evidence_at("000001", "2024-06-12", "2024-06-13T09:00:00+08:00")
        resumed = self.repo.evidence_at("000001", "2024-06-20", "2024-06-21T09:00:00+08:00")
        self.assertTrue(suspended.is_suspended)
        self.assertFalse(resumed.is_suspended)

    def test_no_source_is_unknown_not_not_suspended(self):
        """没有停牌源 → unknown，绝不能推广成"历史任何日期都没停牌"。"""
        provider = TI.SuspensionHistoryProvider([], complete=False)
        result = self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        self.assertEqual(1, result.coverage["sessions"]["2025-06-10"]["unknown_suspension"])


# ───────────────────────────── 4. Quote / Volume 分开 ─────────────────────────────


class QuoteVolumeIngestion(IngestionTestCase):
    def _reader(self, bars):
        return lambda code: bars

    def test_quote_and_volume_are_separate_facts(self):
        provider = TI.HistoricalMarketSessionProvider(
            self._reader({"2025-06-10": {"close": 10.0, "volume": 0}}),
            observed_at="2025-06-11T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        ev = self.repo.evidence_at("000001", "2025-06-10", "2025-06-12T09:00:00+08:00")
        self.assertTrue(ev.has_market_quote)
        # volume == 0：无法区分真实零成交与缺失 → None（未知），不是 False。
        self.assertIsNone(ev.has_trade_volume)

    def test_positive_volume_is_has_trade_volume(self):
        provider = TI.HistoricalMarketSessionProvider(
            self._reader({"2025-06-10": {"close": 10.0, "volume": 1000}}),
            observed_at="2025-06-11T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        ev = self.repo.evidence_at("000001", "2025-06-10", "2025-06-12T09:00:00+08:00")
        self.assertTrue(ev.has_trade_volume)


# ───────────────────────────── 5. Missing provider data → unknown ─────────────


class MissingProviderData(IngestionTestCase):
    def test_missing_does_not_default_false_or_true(self):
        """Provider 对某 (code, session) 无数据 → unknown，绝不当 False/True。"""
        provider = TI.HistoricalMarketSessionProvider(
            lambda code: {}, observed_at="2025-06-11T09:00:00+08:00"
        )
        result = self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        stats = result.coverage["sessions"]["2025-06-10"]
        self.assertEqual(0, stats["evidence_present"])
        self.assertEqual(1, stats["unknown_quote"])
        self.assertEqual(1, stats["unknown_volume"])


# ───────────────────────────── 6. PIT publication ─────────────────────────────


class PITPublication(IngestionTestCase):
    def test_future_observation_cannot_enter_past_decision(self):
        """未来才观察到的状态不能进入过去 decision_time。"""
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-06-01T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2024-01-10"], write=True)
        # 决策时点 2024-03-01：observed_at=2025-06-01 > decision → 不可见。
        decision = TA.tradability_at(
            "000001", "2024-01-10",
            decision_time="2024-03-01T09:00:00+08:00",
            repository=self.repo,
        )
        self.assertFalse(decision.evidence_present)


# ───────────────────────────── 7. 反时间穿越 ─────────────────────────────


class AntiTimeTravel(IngestionTestCase):
    def test_todays_historical_fact_cannot_claim_past_observed(self):
        """今天抓到的历史事实，若无历史 observed timestamp，不得伪造成当年已知。

        用 retrieved_at（今天）作为 observed_kind 的 listing provider：它对
        "2024-01-10 是否上市" 给出了历史参考事实，但 observed_at 是今天，
        因此不能用于 2024 年的 decision_time。
        """
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_RETRIEVED_AT,
            observed_at="2026-09-16T09:00:00+08:00",
        )
        result = self.make_service([provider]).ingest(["000001"], ["2024-01-10"], write=True)
        self.assertEqual(1, result.coverage["unprovable_observed_at"])
        decision = TA.tradability_at(
            "000001", "2024-01-10",
            decision_time="2024-03-01T09:00:00+08:00",
            repository=self.repo,
        )
        self.assertFalse(decision.evidence_present)


# ───────────────────────────── 8. Provider conflict ─────────────────────────────


class ProviderConflict(IngestionTestCase):
    def test_conflicting_st_is_conflict_not_last_write_wins(self):
        # 用一个直接给 is_st 的假 provider 制造冲突。
        class StaticProvider(TI.TradabilityFactProvider):
            provider_id = "static_a"
            provider_version = "1"

            def __init__(self, value):
                self._value = value

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence={"is_st": self._value},
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at="2024-06-14T09:00:00+08:00",
                )

        a = StaticProvider(True)
        b = StaticProvider(False)
        # 换掉 provider_id 以制造两个不同来源的冲突。
        b.provider_id = "static_b"
        result = self.make_service([a, b]).ingest(["000001"], ["2024-06-14"], write=True)
        self.assertEqual(1, len(result.conflicts))
        self.assertEqual("is_st", result.conflicts[0].field)
        # 冲突字段落库为 None（未知），不偏向任何一方。
        ev = self.repo.evidence_at("000001", "2024-06-14", "2024-06-15T09:00:00+08:00")
        self.assertIsNone(ev.is_st)


# ───────────────────────────── 9. Deterministic composition ─────────────────────────────


class DeterministicComposition(IngestionTestCase):
    def test_provider_order_does_not_change_result(self):
        class StaticProvider(TI.TradabilityFactProvider):
            def __init__(self, pid, fields):
                self.provider_id = pid
                self.provider_version = "1"
                self._fields = fields

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence=dict(self._fields),
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at="2024-06-14T09:00:00+08:00",
                )

        a = StaticProvider("alpha", {"is_listed": True})
        b = StaticProvider("beta", {"is_st": False})
        s1 = self.make_service([a, b])
        r1 = s1.ingest(["000001"], ["2024-06-14"], write=False)
        s2 = self.make_service([b, a])
        r2 = s2.ingest(["000001"], ["2024-06-14"], write=False)
        # source 与 coverage fingerprint 必须与 provider 顺序无关。
        self.assertEqual(r1.coverage["fingerprint"], r2.coverage["fingerprint"])

    def test_composed_source_is_sorted_and_deterministic(self):
        class StaticProvider(TI.TradabilityFactProvider):
            def __init__(self, pid, fields):
                self.provider_id = pid
                self.provider_version = "1"
                self._fields = fields

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence=dict(self._fields),
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at="2024-06-14T09:00:00+08:00",
                )

        a = StaticProvider("zeta", {"is_listed": True})
        b = StaticProvider("alpha", {"is_st": False})
        self.make_service([a, b]).ingest(["000001"], ["2024-06-14"], write=True)
        ev = self.repo.evidence_at("000001", "2024-06-14", "2024-06-15T09:00:00+08:00")
        self.assertEqual("alpha+zeta", ev.source)


# ───────────────────────────── 10. Idempotent replay ─────────────────────────────


class IdempotentReplay(IngestionTestCase):
    def test_replay_does_not_grow_archive(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        service = self.make_service([provider])
        run_id = "fixed-run-id"
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id=run_id)
        count1 = self.repo.count("000001")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id=run_id)
        count2 = self.repo.count("000001")
        # 同一份数据重复 ingest 不产生新的逻辑 Evidence。
        self.assertEqual(count1, count2)
        self.assertEqual(1, count1)


# ───────────────────────────── 11. Source failure → unknown ─────────────────────────────


class SourceFailure(IngestionTestCase):
    def test_provider_exception_is_not_tradable_fact(self):
        class BrokenProvider(TI.TradabilityFactProvider):
            provider_id = "broken"
            provider_version = "1"

            def fetch(self, code, session):
                raise RuntimeError("network down")

        result = self.make_service([BrokenProvider()]).ingest(
            ["000001"], ["2025-06-10"], write=True
        )
        # provider 异常 → error，绝不转成 is_st=False / has_quote=True 等事实。
        self.assertEqual(TI.STATUS_COMPLETED_WITH_GAPS, result.status)
        self.assertEqual(0, result.coverage["evidence_present"])


# ───────────────────────────── 12. Daily-bar timing ─────────────────────────────


class DailyBarTiming(IngestionTestCase):
    def test_eod_volume_not_visible_intraday(self):
        provider = TI.HistoricalMarketSessionProvider(
            lambda code: {"2025-06-10": {"close": 10.0, "volume": 1000}},
            observed_at="2025-06-11T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        # effective_at 是 session 收盘（15:00），盘中 09:30 不可见。
        decision = TA.tradability_at(
            "000001", "2025-06-10",
            decision_time="2025-06-10T09:30:00+08:00",
            repository=self.repo,
        )
        self.assertFalse(decision.evidence_present)


# ───────────────────────────── 13. volume missing ≠ zero ─────────────────────────────


class VolumeMissingVsZero(IngestionTestCase):
    def test_missing_volume_is_unknown_not_zero(self):
        provider = TI.HistoricalMarketSessionProvider(
            lambda code: {"2025-06-10": {"close": 10.0}},  # 无 volume 字段
            observed_at="2025-06-11T09:00:00+08:00",
        )
        self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        ev = self.repo.evidence_at("000001", "2025-06-10", "2025-06-12T09:00:00+08:00")
        self.assertIsNone(ev.has_trade_volume)


# ───────────────────────────── 14. Limit direction ─────────────────────────────


class LimitDirection(IngestionTestCase):
    def test_up_down_preserve_archive_semantics(self):
        provider = TI.PriceLimitProvider(
            {
                "000001": {
                    "2025-06-10": {
                        "locked": True,
                        "direction": "up",
                        "observed_at": "2025-06-10T09:00:00+08:00",
                        "effective_at": "2025-06-10T09:00:00+08:00",
                    }
                }
            }
        )
        self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        ev = self.repo.evidence_at("000001", "2025-06-10", "2025-06-10T10:00:00+08:00")
        self.assertTrue(ev.is_price_limit_locked)
        self.assertEqual("up", ev.price_limit_direction)


# ───────────────────────────── 15. Limit lock inference ─────────────────────────────


class LimitLockInference(IngestionTestCase):
    def test_touching_limit_is_not_locked(self):
        """仅触及 limit price 不自动认定 locked。PriceLimitProvider 无记录 → unknown。"""
        provider = TI.PriceLimitProvider({})  # 无封单证据
        result = self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        self.assertEqual(1, result.coverage["sessions"]["2025-06-10"]["unknown_limit_state"])


# ───────────────────────────── 16. Source provenance ─────────────────────────────


class SourceProvenance(IngestionTestCase):
    def test_all_persisted_evidence_has_source(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        result = self.make_service([provider]).ingest(["000001"], ["2024-01-10"], write=True)
        for evidence in result.persisted:
            self.assertTrue(evidence.source)


# ───────────────────────────── 17. Run audit ─────────────────────────────


class RunAudit(IngestionTestCase):
    def test_success_and_gap_counts_recorded(self):
        good = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )

        class Broken(TI.TradabilityFactProvider):
            provider_id = "broken"
            provider_version = "1"

            def fetch(self, code, session):
                raise RuntimeError("boom")

        service = self.make_service([good, Broken])
        result = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="audit-run")
        self.assertEqual(TI.STATUS_COMPLETED_WITH_GAPS, result.status)
        row = self.conn.execute(
            "SELECT * FROM tradability_ingestion_runs WHERE run_id=?", ("audit-run",)
        ).fetchone()
        self.assertIsNotNone(row)
        data = dict(row)
        self.assertEqual(1, data["error_records"])
        self.assertEqual(1, data["requested_codes"])


# ───────────────────────────── 18. Coverage ─────────────────────────────


class Coverage(IngestionTestCase):
    def test_unknown_and_conflict_not_fully_proven(self):
        class StaticProvider(TI.TradabilityFactProvider):
            def __init__(self, pid, fields):
                self.provider_id = pid
                self.provider_version = "1"
                self._fields = fields

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence=dict(self._fields),
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at="2024-06-14T09:00:00+08:00",
                )

        # is_st 冲突 + 其余字段未知 → 绝不计入 fully_proven。
        a = StaticProvider("a", {"is_listed": True, "is_st": True})
        b = StaticProvider("b", {"is_st": False})
        result = self.make_service([a, b]).ingest(["000001"], ["2024-06-14"], write=True)
        stats = result.coverage["sessions"]["2024-06-14"]
        self.assertEqual(0, stats["fully_proven"])
        self.assertEqual(1, stats["conflicts"])

    def test_fully_proven_when_all_core_fields_known(self):
        class StaticProvider(TI.TradabilityFactProvider):
            def __init__(self, pid, fields):
                self.provider_id = pid
                self.provider_version = "1"
                self._fields = fields

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence=dict(self._fields),
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at="2024-06-14T09:00:00+08:00",
                )

        a = StaticProvider(
            "a",
            {
                "is_listed": True,
                "is_st": False,
                "is_suspended": False,
                "has_market_quote": True,
                "has_trade_volume": True,
            },
        )
        result = self.make_service([a]).ingest(["000001"], ["2024-06-14"], write=True)
        stats = result.coverage["sessions"]["2024-06-14"]
        self.assertEqual(1, stats["fully_proven"])


# ───────────────────────────── 19. Coverage denominator ─────────────────────────────


class CoverageDenominator(IngestionTestCase):
    def test_requested_code_scope_not_whole_market(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        result = self.make_service([provider]).ingest(["000001"], ["2024-01-10"], write=True)
        self.assertEqual("requested_code_coverage", result.coverage["scope"])
        self.assertEqual(1, result.coverage["requested_symbols"])


# ───────────────────────────── 20. Current-state contamination ─────────────────────────────


class CurrentStateContamination(IngestionTestCase):
    def test_current_st_or_delisting_not_written_to_history(self):
        """当前 ST / 退市状态不得反写历史 PIT。这里验证：没有历史 ST 源时，
        即使代码今天仍存在，历史 session 的 ST 也必须是 unknown，而不是用今天的
        名称/状态去填。"""
        # listing provider 只给"今天在册"的弱事实（无 listing_date）。
        listing = TI.ListingStatusProvider(
            {"600001": {}},  # 空：无 listing_date/delisting_date
            observed_kind=TI.OBSERVED_RETRIEVED_AT,
            observed_at="2026-09-16T09:00:00+08:00",
        )
        st = TI.SecurityStateHistoryProvider(None)  # 无历史状态源
        result = self.make_service([listing, st]).ingest(["600001"], ["2020-01-10"], write=True)
        # 无上市日期证据 + 无 ST 证据 → 不能伪造"当时已上市且非 ST"。
        self.assertEqual(1, result.coverage["sessions"]["2020-01-10"]["unknown_listing"])
        self.assertEqual(1, result.coverage["sessions"]["2020-01-10"]["unknown_st"])


# ───────────────────────────── TTI2：ST 不得由名称推断 ─────────────────────────────


class STNameInference(IngestionTestCase):
    def test_st_from_archive_not_from_name(self):
        """risk_flag=False 但名称含 "ST" → is_st 必须 False，不因名称推断。"""
        import security_state_point_in_time as SSPIT

        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": True,
            "archive_source": "unit-test",
            "availability_basis": "session_close",
            "rows": [
                {
                    "code": "600001",
                    "effective_from": "2024-06-14",
                    "effective_to": "2024-06-21",
                    "name": "ST某某",
                    "risk_flag": False,
                }
            ],
        }
        archive = SSPIT.SecurityStateArchive.from_payload(payload)
        provider = TI.SecurityStateHistoryProvider(archive)
        self.make_service([provider]).ingest(["600001"], ["2024-06-14"], write=True)
        ev = self.repo.evidence_at("600001", "2024-06-14", "2024-06-15T09:00:00+08:00")
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_st)


# ───────────────────────────── TTI3/TTI4：缺失字段不默认 False/True ─────────────────────────────


class MissingFieldNotDefaulted(IngestionTestCase):
    def test_missing_bool_field_stays_unknown(self):
        """listing provider 只给 is_listed，缺 is_st → is_st 必须 None（未知），
        不默认 False，也不默认 True。"""
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        result = self.make_service([provider]).ingest(["000001"], ["2024-01-10"], write=True)
        self.assertEqual(1, result.coverage["sessions"]["2024-01-10"]["unknown_st"])


# ───────────────────────────── TTI1：observed 缺失不用 effective 兜底 ─────────────────────────────


class ObservedMissingUsesCutoff(IngestionTestCase):
    def test_missing_observed_not_replaced_by_effective(self):
        """observed_at 缺失（但 observed_kind 可证明）→ 用 cutoff 兜底且标 unprovable，
        绝不偷偷用 effective_at 兜底（那会把生效时间当成观察时间，时间穿越）。"""

        class Provider(TI.TradabilityFactProvider):
            provider_id = "p"
            provider_version = "1"

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence={"is_listed": True},
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,  # 可证明类型
                    observed_at=None,  # 但观测值缺失
                    effective_at="2020-01-01T00:00:00+08:00",
                )

        result = self.make_service([Provider()]).ingest(["000001"], ["2024-01-10"], write=True)
        self.assertEqual(1, result.coverage["unprovable_observed_at"])


# ───────────────────────────── TTI11：EOD 盘中不可见（effective_at 门禁） ─────────────────────────────


class EODIntradayVisibility(IngestionTestCase):
    def test_eod_effective_not_visible_before_close(self):
        """observed_at 可证明（当天早盘），但 effective_at 缺省兜底 session 收盘
        15:00 → 盘中 09:30 不可见。"""

        class EODProvider(TI.TradabilityFactProvider):
            provider_id = "eod"
            provider_version = "1"

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence={"has_market_quote": True, "has_trade_volume": True},
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at="2025-06-10T09:00:00+08:00",  # 早盘可观察
                    effective_at=None,  # 无 effective → 兜底 session 收盘
                )

        self.make_service([EODProvider()]).ingest(["000001"], ["2025-06-10"], write=True)
        decision = TA.tradability_at(
            "000001", "2025-06-10",
            decision_time="2025-06-10T09:30:00+08:00",
            repository=self.repo,
        )
        self.assertFalse(decision.evidence_present)


# ───────────────────────────── TTI12：Provider 不得写 archive ─────────────────────────────


INGESTION_MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradability_ingestion.py")


def _ingestion_tree():
    with open(INGESTION_MODULE_PATH, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _provider_classes(tree):
    """摄取模块里，基类名含 ``Provider`` 或类名以 ``Provider`` 结尾（事实源 adapter）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [ast.unparse(base) for base in node.bases]
        if any("Provider" in base for base in bases) or node.name.endswith("Provider"):
            yield node


def _writes_archive(node):
    """返回 Provider 类方法体里"写 archive"的违规表达式列表。"""
    offenders = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr == "save":
                offenders.append(ast.unparse(child))
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "historical_tradability_archive" in child.value:
                offenders.append(repr(child.value))
    return offenders


class ProvidersNeverWriteTheArchive(IngestionTestCase):
    def test_provider_classes_do_not_write_archive(self):
        tree = _ingestion_tree()
        offenders = []
        for cls in _provider_classes(tree):
            hits = _writes_archive(cls)
            if hits:
                offenders.append(f"{cls.name}: {hits}")
        self.assertEqual([], offenders, "Provider 不得写 archive，写 authority 只在 ingestion 编排层")

    def test_guard_detects_a_provider_writing_archive(self):
        """护栏必须真的能失败：合成一个写 archive 的 Provider，应被抓到。"""
        source = (
            "class FooProvider:\n"
            "    def fetch(self):\n"
            "        x = 'historical_tradability_archive'\n"
            "        return x\n"
        )
        tree = ast.parse(source)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        self.assertTrue(_writes_archive(cls))


# ───────────────────────────── 23. Backfill commit / rollback（fix #1/#2） ─────────────────


class BackfillCommit(IngestionTestCase):
    def _providers(self):
        return [
            TI.ListingStatusProvider(
                {"000001": {"listing_date": "2010-01-01"}},
                observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                observed_at="2025-01-01T09:00:00+08:00",
            )
        ]

    def _file_db(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_dry_run_writes_nothing(self):
        result = BF.run_backfill(
            self.conn, self._providers(), ["000001"], ["2024-01-10"],
            write=False, run_id="dry-run",
        )
        del result
        self.assertEqual(0, self.repo.count("000001"))
        row = self.conn.execute(
            "SELECT * FROM tradability_ingestion_runs WHERE run_id=?", ("dry-run",)
        ).fetchone()
        self.assertIsNone(row)

    def test_write_persists_after_reopen(self):
        path = self._file_db()
        conn = sqlite3.connect(path)
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            BF.run_backfill(
                conn, self._providers(), ["000001"], ["2024-01-10"],
                write=True, run_id="write-run",
            )
        finally:
            conn.close()
        # 重新打开数据库：commit 生效，记录仍在。
        conn2 = sqlite3.connect(path)
        conn2.row_factory = sqlite3.Row
        try:
            repo2 = TA.TradabilityArchiveRepository(conn2)
            self.assertGreater(repo2.count("000001"), 0)
            row = conn2.execute(
                "SELECT * FROM tradability_ingestion_runs WHERE run_id=?", ("write-run",)
            ).fetchone()
            self.assertIsNotNone(row)
            data = dict(row)
            # fix #2：生产 backfill 显式注入 audit_conn，run audit 字段齐全。
            self.assertEqual("write-run", data["run_id"])
            self.assertIsNotNone(data["provider_set"])
            self.assertIsNotNone(data["status"])
            self.assertIsNotNone(data["run_fingerprint"])
            self.assertGreaterEqual(data["persisted_records"], 1)
        finally:
            conn2.close()

    def test_failure_rolls_back(self):
        with mock.patch.object(TI.IngestionService, "ingest", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                BF.run_backfill(
                    self.conn, self._providers(), ["000001"], ["2024-01-10"],
                    write=True, run_id="fail-run",
                )
        # rollback 后：archive 空、audit 无残留，绝不出现 persisted>0 但库为空。
        self.assertEqual(0, self.repo.count("000001"))
        self.assertIsNone(
            self.conn.execute(
                "SELECT * FROM tradability_ingestion_runs WHERE run_id=?", ("fail-run",)
            ).fetchone()
        )


# ───────────────────────────── 24. Fingerprint replay 稳定性（fix #5） ─────────────────


class FingerprintReplayStability(IngestionTestCase):
    def test_replay_same_fingerprint_and_archive_count(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        service = self.make_service([provider])
        r1 = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="stable-run")
        count1 = self.repo.count("000001")
        r2 = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="stable-run")
        count2 = self.repo.count("000001")
        # 同一输入：第一次与第二次 replay 的指纹必须一致，archive 不增长。
        self.assertEqual(count1, count2)
        self.assertEqual(1, count1)
        self.assertEqual(r1.run_fingerprint, r2.run_fingerprint)


# ───────────────────────────── 25. Coverage denominator（fix #3） ─────────────────


class CoverageDenominatorPairs(IngestionTestCase):
    def test_multi_session_denominator_is_code_session_pairs(self):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )
        result = self.make_service([provider]).ingest(
            ["000001"], ["2024-01-10", "2024-01-11"], write=True
        )
        self.assertEqual(1, result.coverage["requested_symbols"])
        self.assertEqual(2, result.coverage["requested_sessions"])
        self.assertEqual(2, result.coverage["requested_pairs"])
        # 两个 session 均有 evidence：2/2 = 100，绝不超过 100。
        self.assertEqual(100.0, result.coverage["coverage_ratio"])


# ───────────────────────────── 26. Complete suspension archive 语义（fix #4） ───────────


class CompleteSuspensionArchive(IngestionTestCase):
    def test_complete_no_intervals_is_not_suspended(self):
        provider = TI.SuspensionHistoryProvider(
            [], complete=True, availability_basis="session_close"
        )
        result = self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        ev = self.repo.evidence_at("000001", "2025-06-10", "2025-06-11T09:00:00+08:00")
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_suspended)
        self.assertEqual(0, result.coverage["sessions"]["2025-06-10"]["unknown_suspension"])

    def test_incomplete_no_intervals_is_unknown(self):
        provider = TI.SuspensionHistoryProvider([], complete=False)
        result = self.make_service([provider]).ingest(["000001"], ["2025-06-10"], write=True)
        self.assertEqual(1, result.coverage["sessions"]["2025-06-10"]["unknown_suspension"])


if __name__ == "__main__":
    unittest.main()
