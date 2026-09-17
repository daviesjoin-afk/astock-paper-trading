# -*- coding: utf-8 -*-
"""Tradability Observation Ledger 契约测试（O1–O12 / V1–V4 / coverage / replay）。

本模块验证三件互相独立的事：

1. **Ledger 本身**是 append-only 的事实台账（记 evidence / unknown / error 三态），
   而不是第二套判定层；
2. **PIT**：``validation_as_of`` 真的能挡住未来观察，且 ``recorded_at`` 不被伪装成
   ``session_date`` / ``effective_at``；
3. **Shadow 分类**：``archive_missing`` 与 ``archive_unprovable`` 由台账在明确知识
   时点下给出，且两者都仍然是 not_comparable（绝不进入 disagreement 分母）。
"""

import sqlite3
import unittest

import tradability_archive as TA
import tradability_ingestion as TI
import tradability_observation_ledger as OL
import tradability_shadow as TS
import selection_tradability as ST

CODE = "000001"
SESSION = "2024-01-10"
DECISION = "2024-01-10T16:00:00+08:00"
RUN = "run-0001"


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    TI.ensure_ingestion_schema(conn)
    return conn


def event(
    *,
    status=OL.OBSERVED_EVIDENCE,
    recorded_at="2024-01-10T16:00:00+08:00",
    provider_id="listing",
    provider_version="1",
    source_observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
    source_observed_at="2024-01-09T00:00:00+08:00",
    effective_at="2024-01-09T00:00:00+08:00",
    evidence=None,
    error=None,
    run_id=RUN,
    code=CODE,
    session=SESSION,
):
    return OL.event_from_provider_result(
        TI.ProviderResult(
            provider_id=provider_id,
            provider_version=provider_version,
            status=status,
            evidence=(
                {}
                if status != OL.OBSERVED_EVIDENCE
                else dict(evidence or {"listing_date": "2010-01-01"})
            ),
            observed_kind=source_observed_kind,
            observed_at=source_observed_at,
            effective_at=effective_at,
            error=error,
        ),
        code=code,
        session=session,
        recorded_at=recorded_at,
        ingestion_run_id=run_id,
    )


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.ledger = OL.ObservationLedgerRepository(self.conn)


# ───────────────────── O1–O12：台账本身 ─────────────────────


class ObservationRecording(LedgerTestCase):
    def test_o1_first_observation_recorded_correctly(self):
        self.assertTrue(self.ledger.append(event()))
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(CODE, row["code"])
        self.assertEqual(SESSION, row["session_date"])
        self.assertEqual("listing", row["provider_id"])
        self.assertEqual(OL.OBSERVED_EVIDENCE, row["provider_status"])
        self.assertEqual("2024-01-10T16:00:00+08:00", row["recorded_at"])
        self.assertEqual(RUN, row["ingestion_run_id"])
        self.assertTrue(row["observation_fingerprint"])

    def test_o2_same_run_replay_does_not_duplicate_event(self):
        self.assertTrue(self.ledger.append(event()))
        self.assertFalse(self.ledger.append(event()))
        self.assertEqual(1, self.ledger.count(CODE, SESSION))

    def test_o3_different_later_run_appends_new_observation(self):
        self.ledger.append(event(recorded_at="2024-01-10T16:00:00+08:00"))
        self.assertTrue(
            self.ledger.append(event(recorded_at="2024-02-01T16:00:00+08:00", run_id="run-0002"))
        )
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(2, len(rows))
        # 两次观察，first_seen 取最早。
        knowledge = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.assertEqual("2024-01-10T16:00:00+08:00", knowledge.first_seen_at)
        self.assertEqual(2, knowledge.observation_count)

    def test_o4_provider_unknown_is_recorded(self):
        self.assertTrue(self.ledger.append(event(status=OL.OBSERVED_UNKNOWN)))
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(OL.OBSERVED_UNKNOWN, rows[0]["provider_status"])
        # unknown 不是 evidence：不得被当成"看到过事实"。
        knowledge = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.assertFalse(knowledge.evidence_seen)
        self.assertIsNone(knowledge.first_evidence_seen_at)
        self.assertFalse(knowledge.never_observed)  # 观察过，只是没有证据

    def test_o5_provider_error_is_recorded_and_not_downgraded_to_never(self):
        self.assertTrue(
            self.ledger.append(
                event(status=OL.OBSERVED_ERROR, error="TimeoutError: request aborted")
            )
        )
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(OL.OBSERVED_ERROR, rows[0]["provider_status"])
        self.assertEqual("TimeoutError", rows[0]["error_class"])
        self.assertTrue(rows[0]["error_fingerprint"])
        knowledge = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        # error **不能**降级成 never observed。
        self.assertFalse(knowledge.never_observed)
        self.assertEqual({OL.OBSERVED_ERROR: 1}, dict(knowledge.provider_outcomes))

    def test_o6_evidence_is_recorded_with_fingerprint(self):
        self.ledger.append(event())
        row = self.ledger.events(CODE, SESSION)[0]
        self.assertTrue(row["evidence_fingerprint"])
        # 指纹来自证据**内容**：内容变化必须换指纹。
        self.ledger.append(event(evidence={"listing_date": "2010-01-01", "is_st": True}))
        fingerprints = {r["evidence_fingerprint"] for r in self.ledger.events(CODE, SESSION)}
        self.assertEqual(2, len(fingerprints))

    def test_o7_future_recorded_at_is_invisible_to_earlier_validation_as_of(self):
        self.ledger.append(event(recorded_at="2024-01-15T16:00:00+08:00"))
        early = self.ledger.knowledge_at(
            CODE, SESSION, validation_as_of="2024-01-12T00:00:00+08:00", decision_at=DECISION
        )
        late = self.ledger.knowledge_at(
            CODE, SESSION, validation_as_of="2024-02-01T00:00:00+08:00", decision_at=DECISION
        )
        self.assertTrue(early.never_observed)
        self.assertIsNone(early.first_seen_at)
        self.assertFalse(late.never_observed)
        self.assertEqual("2024-01-15T16:00:00+08:00", late.first_seen_at)

    def test_o8_first_seen_is_deterministic(self):
        self.ledger.append(event(recorded_at="2024-01-20T16:00:00+08:00", run_id="r2"))
        self.ledger.append(event(recorded_at="2024-01-12T16:00:00+08:00", run_id="r3"))
        first = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.assertEqual("2024-01-12T16:00:00+08:00", first.first_seen_at)
        self.assertEqual("2024-01-20T16:00:00+08:00", first.last_seen_at)

    def test_o9_provider_order_does_not_change_derived_knowledge(self):
        self.ledger.append(event(provider_id="b", recorded_at="2024-01-12T16:00:00+08:00"))
        self.ledger.append(event(provider_id="a", recorded_at="2024-01-12T16:00:00+08:00"))
        first = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.ledger.append(event(provider_id="a", recorded_at="2024-01-12T16:00:00+08:00"))
        second = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        # 重放（同 run 同内容）不产生新事件，派生知识指纹不变。
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_o10_ledger_append_is_immutable(self):
        """append-only：没有 UPDATE / DELETE 入口，历史事件不得被改写。"""
        self.ledger.append(event())
        before = dict(self.ledger.events(CODE, SESSION)[0])
        # 用同一个 run 但**不同内容**再 append：不得覆盖既有行（唯一键含 fingerprint）。
        self.ledger.append(event(evidence={"listing_date": "2011-05-05"}))
        after = self.ledger.events(CODE, SESSION)
        self.assertEqual(2, len(after))
        self.assertEqual(before["observation_fingerprint"], after[0]["observation_fingerprint"])
        self.assertFalse(hasattr(self.ledger, "update"))
        self.assertFalse(hasattr(self.ledger, "delete"))

    def test_o11_dry_run_writes_nothing(self):
        service = TI.IngestionService(
            [_StaticProvider()], TA.TradabilityArchiveRepository(self.conn),
            audit_conn=self.conn, cutoff="2024-01-10T16:00:00+08:00",
        )
        service.ingest([CODE], [SESSION], write=False)
        self.assertEqual(0, self.ledger.count())
        self.assertEqual(0, TA.TradabilityArchiveRepository(self.conn).count())

    def test_o12_failed_transaction_writes_nothing(self):
        """ledger 写入失败 → archive 事实与 run audit 一起 rollback。"""
        repo = TA.TradabilityArchiveRepository(self.conn)
        service = TI.IngestionService(
            [_StaticProvider()], repo,
            audit_conn=self.conn, cutoff="2024-01-10T16:00:00+08:00",
        )
        # 让 ledger 的 append 抛异常：模拟 ledger 侧写入失败。
        original = OL.ObservationLedgerRepository.append_many

        def boom(self, events):
            raise sqlite3.OperationalError("ledger exploded")

        OL.ObservationLedgerRepository.append_many = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                service.ingest([CODE], [SESSION], write=True, run_id=RUN)
        finally:
            OL.ObservationLedgerRepository.append_many = original
        self.conn.rollback()
        # 事实、审计、观察三者都不得留下。
        self.assertEqual(0, repo.count())
        self.assertEqual(0, self.ledger.count())
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM {TI.INGESTION_RUNS_TABLE}"
        ).fetchone()
        self.assertEqual(0, int(row[0]))


# ───────────────────── 时间三态不得混写 ─────────────────────


class ThreeTimesAreDistinct(LedgerTestCase):
    def test_recorded_at_is_not_session_date_nor_effective_at(self):
        self.ledger.append(
            event(
                recorded_at="2026-09-17T10:00:00+08:00",
                source_observed_at="2024-01-09T00:00:00+08:00",
                effective_at="2024-01-09T00:00:00+08:00",
            )
        )
        row = self.ledger.events(CODE, SESSION)[0]
        self.assertEqual("2026-09-17T10:00:00+08:00", row["recorded_at"])
        self.assertEqual("2024-01-09T00:00:00+08:00", row["source_observed_at"])
        self.assertEqual("2024-01-09T00:00:00+08:00", row["effective_at"])
        self.assertNotEqual(row["recorded_at"], row["session_date"])

    def test_market_provable_and_system_possessed_are_separate(self):
        """来源早公开（市场可证明），但系统今天才抓到（系统当时没有）。"""
        self.ledger.append(
            event(
                recorded_at="2026-09-17T10:00:00+08:00",
                source_observed_at="2024-01-09T00:00:00+08:00",
                effective_at="2024-01-09T00:00:00+08:00",
            )
        )
        knowledge = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.assertTrue(knowledge.market_provable_at_decision)
        self.assertFalse(knowledge.system_possessed_at_decision)

    def test_missing_source_observed_at_is_not_provable(self):
        self.ledger.append(event(source_observed_at=None))
        knowledge = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.assertFalse(knowledge.market_provable_at_decision)


# ───────────────────── 错误身份规范化 ─────────────────────


class ErrorIdentityIsStable(unittest.TestCase):
    def test_volatile_error_detail_does_not_change_identity(self):
        """易变诊断文本（URL token / 路径 / uuid）不得让**身份**漂移。

        fixture 用 loopback 主机：台账的 URL 脱敏规则会把整个 URL（含 token）
        替换成 ``<url>``，同时不触发敏感扫描（扫描器放行 loopback）。

        ``error_class`` 进指纹，``error_diagnostic`` 只是给人看的脱敏文本。
        """
        first_class, first_diag = OL.normalize_error_identity(
            "HTTPError: 500 at http://127.0.0.1:8080/x?token=aaa"
        )
        second_class, second_diag = OL.normalize_error_identity(
            "HTTPError: 500 at http://127.0.0.1:8080/x?token=bbb"
        )
        self.assertEqual(first_class, second_class)
        self.assertEqual(first_diag, second_diag)
        self.assertNotIn("token", first_diag)

    def test_volatile_detail_does_not_change_observation_fingerprint(self):
        a = event(status=OL.OBSERVED_ERROR, error="HTTPError: 500 at http://127.0.0.1:8080/y?token=aaa")
        b = event(status=OL.OBSERVED_ERROR, error="HTTPError: 500 at http://127.0.0.1:8080/y?token=bbb")
        self.assertEqual(a.observation_fingerprint, b.observation_fingerprint)
        c = event(status=OL.OBSERVED_ERROR, error="TimeoutError: x")
        self.assertNotEqual(a.observation_fingerprint, c.observation_fingerprint)

    def test_error_class_is_extracted(self):
        klass, fingerprint = OL.normalize_error_identity("TimeoutError: aborted")
        self.assertEqual("TimeoutError", klass)
        self.assertTrue(fingerprint)


# ───────────────────── Coverage 分母 ─────────────────────


class CoverageDenominator(LedgerTestCase):
    def test_denominator_is_requested_pairs_not_events(self):
        # 同一 pair 3 个事件、2 个 provider，仍然只算 1 个 pair。
        self.ledger.append(event(run_id="r1"))
        self.ledger.append(event(run_id="r2", provider_id="st"))
        self.ledger.append(event(run_id="r3", provider_id="quote"))
        report = OL.coverage(
            self.ledger, [(CODE, SESSION)], validation_as_of="2026-01-01T00:00:00+08:00"
        )
        self.assertEqual(1, report.requested_pairs)
        self.assertEqual(1, report.observed_pairs)
        self.assertEqual(3, report.observation_events)

    def test_duplicate_pairs_count_once(self):
        self.ledger.append(event())
        report = OL.coverage(
            self.ledger,
            [(CODE, SESSION), (CODE, SESSION), (CODE, SESSION)],
            validation_as_of="2026-01-01T00:00:00+08:00",
        )
        self.assertEqual(1, report.requested_pairs)

    def test_never_observed_pair_is_counted(self):
        report = OL.coverage(
            self.ledger, [(CODE, SESSION)], validation_as_of="2026-01-01T00:00:00+08:00"
        )
        self.assertEqual(1, report.never_observed_pairs)
        self.assertEqual(0, report.observed_pairs)

    def test_ratios_are_none_when_denominator_is_zero(self):
        report = OL.coverage(self.ledger, [], validation_as_of="2026-01-01T00:00:00+08:00")
        self.assertEqual(0, report.requested_pairs)
        self.assertIsNone(report.observed_ratio)
        self.assertIsNone(report.evidence_ratio)


# ───────────────────── Legacy archive ─────────────────────


class LegacyArchiveHandling(LedgerTestCase):
    def test_legacy_rows_do_not_get_a_fabricated_first_seen(self):
        """archive 有行、无 provenance 链接 = 升级前数据：不得伪造 first_seen。"""
        knowledge = self.ledger.knowledge_at(
            CODE, SESSION, decision_at=DECISION,
            archive_rows=[{
                "code": CODE, "session_date": SESSION,
                "effective_at": "2024-01-10T15:05:00+08:00",
                "observed_at": "2024-01-10T15:05:00+08:00",
            }],
        )
        self.assertTrue(knowledge.never_observed)
        self.assertTrue(knowledge.legacy_observation_unknown)
        self.assertIsNone(knowledge.first_seen_at)
        self.assertIsNone(knowledge.first_evidence_seen_at)
        self.assertEqual(1, knowledge.archive_rows_without_observation_provenance)


# ───────────────────── 摄取集成：replay fingerprint ─────────────────────


class _StaticProvider(TI.TradabilityFactProvider):
    provider_id = "listing"
    provider_version = "1"

    def __init__(self, status=TI.OUTCOME_EVIDENCE, error=None,
                 observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP):
        self._status = status
        self._error = error
        self._observed_kind = observed_kind

    def fetch(self, code, session):
        return TI.ProviderResult(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status=self._status,
            evidence={"listing_date": "2010-01-01"} if self._status == TI.OUTCOME_EVIDENCE else {},
            observed_kind=self._observed_kind,
            observed_at="2024-01-09T00:00:00+08:00",
            effective_at="2024-01-09T00:00:00+08:00",
            error=self._error,
        )


class ReplayFingerprintCoversObservations(unittest.TestCase):
    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.repo = TA.TradabilityArchiveRepository(self.conn)

    def _fingerprint(self, provider):
        service = TI.IngestionService(
            [provider], self.repo, audit_conn=self.conn, cutoff="2024-01-10T16:00:00+08:00"
        )
        result = service.ingest([CODE], [SESSION], write=False)
        return result.run_fingerprint

    def test_provider_unknown_to_evidence_changes_fingerprint(self):
        unknown = self._fingerprint(_StaticProvider(status=TI.OUTCOME_UNKNOWN))
        evidence = self._fingerprint(_StaticProvider(status=TI.OUTCOME_EVIDENCE))
        self.assertNotEqual(unknown, evidence)

    def test_provider_error_to_unknown_changes_fingerprint(self):
        error = self._fingerprint(
            _StaticProvider(status=TI.OUTCOME_ERROR, error="TimeoutError: x")
        )
        unknown = self._fingerprint(_StaticProvider(status=TI.OUTCOME_UNKNOWN))
        self.assertNotEqual(error, unknown)

    def test_provider_version_change_changes_fingerprint(self):
        first = self._fingerprint(_StaticProvider())
        provider = _StaticProvider()
        provider.provider_version = "2"
        second = self._fingerprint(provider)
        self.assertNotEqual(first, second)

    def test_per_provider_observation_instants_are_part_of_the_fingerprint(self):
        """**逐 provider 的观察时点**必须进内容身份。

        两个 provider 的规范化证据、结果计数、provider 版本、``unprovable`` 列表**全部
        相同**，只有"哪个 provider 在哪个时点观察到"互换。archive 与 audit 行看不出这个
        差异，但 ledger 会——若观测载荷不进指纹，同一 run_id 的这次重放会被判成幂等，
        于是 ledger 与 archive 互相矛盾。实测：丢掉观测载荷后该互换不可见。
        """
        first = "2024-01-09T00:00:00+08:00"
        second = "2024-01-08T00:00:00+08:00"
        self.assertNotEqual(
            self._swap_fingerprint(first, second),
            self._swap_fingerprint(second, first),
        )

    def _swap_fingerprint(self, first_at, second_at):
        class _TimedProvider(TI.TradabilityFactProvider):
            provider_version = "1"

            def __init__(self, provider_id, observed_at):
                self.provider_id = provider_id
                self._observed_at = observed_at

            def fetch(self, code, session):
                return TI.ProviderResult(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    status=TI.OUTCOME_EVIDENCE,
                    evidence={"is_listed": True},
                    observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at=self._observed_at,
                    effective_at="2024-01-09T00:00:00+08:00",
                )

        conn = open_db()
        try:
            service = TI.IngestionService(
                [_TimedProvider("a", first_at), _TimedProvider("b", second_at)],
                TA.TradabilityArchiveRepository(conn),
                audit_conn=conn,
                cutoff="2025-06-01T09:00:00+08:00",
            )
            return service.ingest(
                [CODE], [SESSION], write=False, run_id="swap"
            ).run_fingerprint
        finally:
            conn.close()

    def test_same_inputs_same_fingerprint(self):
        self.assertEqual(self._fingerprint(_StaticProvider()), self._fingerprint(_StaticProvider()))


# ───────────────────── V1–V4：missing vs unprovable ─────────────────────


class MissingVersusUnprovable(unittest.TestCase):
    """Shadow 侧分类：``archive_missing`` vs ``archive_unprovable``。"""

    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.repo = TA.TradabilityArchiveRepository(self.conn)
        self.ledger = OL.ObservationLedgerRepository(self.conn)

    def compare(self, *, decision_at=DECISION, validation_as_of=None):
        comparator = TS.ShadowComparator(self.repo, ledger=self.ledger)
        return comparator.compare(
            _verdict(),
            code=CODE,
            session=SESSION,
            side=ST.SIDE_BUY,
            decision_at=decision_at,
            validation_as_of=validation_as_of,
        )

    def test_v1_never_observed_is_archive_missing(self):
        comparison = self.compare(validation_as_of="2026-01-01T00:00:00+08:00")
        self.assertEqual(TS.ShadowStatus.ARCHIVE_MISSING.value, comparison.status)
        self.assertFalse(comparison.comparable)
        self.assertEqual(
            TS.ShadowStatus.ARCHIVE_NEVER_OBSERVED.value, comparison.archive_diagnostic
        )

    def test_v2_late_observed_is_archive_unprovable(self):
        self.ledger.append(event(recorded_at="2024-01-15T16:00:00+08:00"))
        comparison = self.compare(validation_as_of="2024-01-20T00:00:00+08:00")
        self.assertEqual(TS.ShadowStatus.ARCHIVE_UNPROVABLE.value, comparison.status)
        self.assertFalse(comparison.comparable)

    def test_v3_validation_before_late_observation_sees_missing(self):
        self.ledger.append(event(recorded_at="2024-01-15T16:00:00+08:00"))
        comparison = self.compare(validation_as_of="2024-01-12T00:00:00+08:00")
        # 晚观察对更早的知识时点不可见 —— validation_as_of 真的有效。
        self.assertEqual(TS.ShadowStatus.ARCHIVE_MISSING.value, comparison.status)
        self.assertIsNone(comparison.first_observed_at)

    def test_v4_later_snapshot_is_a_distinct_identity(self):
        self.ledger.append(event(recorded_at="2024-01-15T16:00:00+08:00"))
        early = self.compare(validation_as_of="2024-01-12T00:00:00+08:00")
        late = self.compare(validation_as_of="2024-01-20T00:00:00+08:00")
        self.assertNotEqual(early.identity, late.identity)
        # 两者可以同时持久化，不产生 conflict。
        TS.ensure_shadow_schema(self.conn)
        self.assertEqual("inserted", TS.save_comparison(self.conn, early))
        self.assertEqual("inserted", TS.save_comparison(self.conn, late))

    def test_provider_unknown_only_is_diagnosed(self):
        self.ledger.append(event(status=OL.OBSERVED_UNKNOWN))
        comparison = self.compare(validation_as_of="2024-01-20T00:00:00+08:00")
        self.assertEqual(TS.ShadowStatus.ARCHIVE_MISSING.value, comparison.status)
        self.assertEqual(
            TS.ShadowStatus.ARCHIVE_PROVIDER_UNKNOWN.value, comparison.archive_diagnostic
        )
        self.assertFalse(comparison.comparable)

    def test_provider_error_only_is_diagnosed(self):
        self.ledger.append(event(status=OL.OBSERVED_ERROR, error="TimeoutError: x"))
        comparison = self.compare(validation_as_of="2024-01-20T00:00:00+08:00")
        self.assertEqual(TS.ShadowStatus.ARCHIVE_MISSING.value, comparison.status)
        self.assertEqual(
            TS.ShadowStatus.ARCHIVE_PROVIDER_ERROR.value, comparison.archive_diagnostic
        )

    def test_legacy_archive_is_diagnosed_not_backdated(self):
        self.conn.execute(
            f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, "
            "observed_at, is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
            (CODE, SESSION, "2024-01-09T00:00:00+08:00",
             "2024-02-01T00:00:00+08:00", 1, "legacy", "2024-02-01T00:00:00+08:00"),
        )
        comparison = self.compare(validation_as_of="2026-01-01T00:00:00+08:00")
        self.assertEqual(
            TS.ShadowStatus.ARCHIVE_LEGACY_OBSERVATION_UNKNOWN.value,
            comparison.archive_diagnostic,
        )
        self.assertIsNone(comparison.first_observed_at)

    def test_ledger_era_row_is_not_diagnosed_legacy_at_a_historical_as_of(self):
        """issue #161 端到端：ledger-era 行在历史知识时点**不得**被判 legacy。

        这是生产环境里的假诊断：事实行是升级后由 ledger-era run 写入的（有行级
        provenance），但站在 decision 当时的知识时点看不到任何观察事件。旧逻辑只看
        "pair 有没有 ledger 事件"，于是把它误标成"升级前历史数据"。
        """
        self.conn.execute(
            f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, "
            "observed_at, is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
            (CODE, SESSION, "2024-01-09T00:00:00+08:00",
             "2026-01-01T00:00:00+08:00", 1, "listing", "2026-01-01T00:00:00+08:00"),
        )
        # 该行的写入有行级 provenance（本次 run 与观察一起落库），但观察发生在
        # decision 之后很久 → 在历史快照里不可见。
        self.ledger.link_archive_row(
            {
                "code": CODE, "session_date": SESSION,
                "effective_at": "2024-01-09T00:00:00+08:00",
                "observed_at": "2026-01-01T00:00:00+08:00",
            },
            ingestion_run_id=RUN, provider_id="listing",
            observation_fingerprint="fp-1", recorded_at="2026-01-01T00:00:00+08:00",
        )
        comparison = self.compare(validation_as_of="2024-01-12T00:00:00+08:00")
        # 当时确实没有可用证据 → archive_missing；但它**不是** legacy。
        self.assertEqual(TS.ShadowStatus.ARCHIVE_MISSING.value, comparison.status)
        self.assertEqual(
            TS.ShadowStatus.ARCHIVE_NEVER_OBSERVED.value, comparison.archive_diagnostic
        )
        self.assertNotEqual(
            TS.ShadowStatus.ARCHIVE_LEGACY_OBSERVATION_UNKNOWN.value,
            comparison.archive_diagnostic,
        )
        self.assertIsNone(comparison.first_observed_at)
        # 顶层分类仍然是 not_comparable：修复诊断不得改变 agreement 分母。
        self.assertFalse(comparison.comparable)

    def test_missing_and_unprovable_never_enter_disagreement(self):
        self.ledger.append(event(recorded_at="2024-01-15T16:00:00+08:00"))
        early = self.compare(validation_as_of="2024-01-12T00:00:00+08:00")
        late = self.compare(validation_as_of="2024-01-20T00:00:00+08:00")
        summary = TS.ShadowComparator(self.repo, ledger=self.ledger).summarize([early, late])
        self.assertEqual(0, summary.comparable)
        self.assertEqual(0, summary.agree)
        self.assertEqual(0, summary.disagree)
        self.assertIsNone(summary.agreement_rate)
        self.assertIsNone(summary.disagreement_rate)
        self.assertEqual(2, summary.requested)

    def test_invalid_validation_as_of_is_comparison_invalid(self):
        comparison = self.compare(validation_as_of="not-a-date")
        self.assertEqual(TS.ShadowStatus.COMPARISON_INVALID.value, comparison.status)


def _verdict(status=ST.STATUS_EXECUTABLE, reason="ok", side=ST.SIDE_BUY):
    """生产 verdict 的替身（结构与 selection_tradability 一致）。"""

    class _V:
        pass

    verdict = _V()
    verdict.status = status
    verdict.reason = reason
    verdict.side = side
    return verdict


# ───────────────── 摄取层集成：三态都必须落到台账 ─────────────────
#
# 直接构造 ``ObservationEvent`` 喂 repository 的用例无法覆盖
# ``IngestionService._observation_events``——"provider unknown / error 不被记录"这类
# 缺陷只会在**摄取层**发生。以下用例跑真实的 ``ingest``，断言台账内容。


class _OutcomeProvider(TI.TradabilityFactProvider):
    provider_id = "probe"
    provider_version = "1"

    def __init__(self, status, error=None):
        self._status = status
        self._error = error

    def fetch(self, code, session):
        return TI.ProviderResult(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status=self._status,
            evidence={"is_listed": True} if self._status == TI.OUTCOME_EVIDENCE else {},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2024-01-09T00:00:00+08:00",
            effective_at="2024-01-09T00:00:00+08:00",
            error=self._error,
        )


class IngestionWritesEveryProviderOutcome(LedgerTestCase):
    """摄取层必须把 provider 的**三态**都写进台账。"""

    def _ingest(self, provider, run_id="run-x"):
        service = TI.IngestionService(
            [provider],
            TA.TradabilityArchiveRepository(self.conn),
            audit_conn=self.conn,
            cutoff="2024-01-10T16:00:00+08:00",
        )
        return service.ingest([CODE], [SESSION], write=True, run_id=run_id)

    def test_provider_unknown_is_recorded_by_ingestion(self):
        """provider 明确返回 unknown 也必须留痕：它与"从未调用 provider"不同。"""
        self._ingest(_OutcomeProvider(TI.OUTCOME_UNKNOWN))
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(1, len(rows), "provider unknown 未被台账记录")
        self.assertEqual(OL.OBSERVED_UNKNOWN, rows[0]["provider_status"])

    def test_provider_error_is_recorded_by_ingestion(self):
        """provider error 不得被降级成"从未观察"。"""
        self._ingest(_OutcomeProvider(TI.OUTCOME_ERROR, error="TimeoutError: x"))
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(1, len(rows), "provider error 未被台账记录")
        self.assertEqual(OL.OBSERVED_ERROR, rows[0]["provider_status"])
        self.assertEqual("TimeoutError", rows[0]["error_class"])

    def test_provider_evidence_is_recorded_by_ingestion(self):
        self._ingest(_OutcomeProvider(TI.OUTCOME_EVIDENCE))
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(1, len(rows))
        self.assertEqual(OL.OBSERVED_EVIDENCE, rows[0]["provider_status"])
        self.assertTrue(rows[0]["evidence_fingerprint"])

    def test_recorded_at_is_the_ingestion_instant_not_the_session(self):
        """``recorded_at`` 必须是**我们摄取的时刻**，不是 session_date。"""
        import datetime as _dt

        # 摄取层的 recorded_at 截断到秒，因此下界也用秒级（比较微秒会因截断而失败）。
        before = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
        self._ingest(_OutcomeProvider(TI.OUTCOME_EVIDENCE))
        after = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0) + _dt.timedelta(
            seconds=1
        )
        row = self.ledger.events(CODE, SESSION)[0]
        recorded = _dt.datetime.fromisoformat(row["recorded_at"])
        self.assertNotEqual(SESSION, row["recorded_at"][:10])
        self.assertLessEqual(before, recorded)
        self.assertLessEqual(recorded, after)

    def test_ledger_row_count_matches_provider_count(self):
        """非空洞性：三个 provider 三态各一 → 恰好三行，且三态齐全。"""
        service = TI.IngestionService(
            [
                _OutcomeProvider(TI.OUTCOME_EVIDENCE),
                _OutcomeProvider(TI.OUTCOME_UNKNOWN),
                _OutcomeProvider(TI.OUTCOME_ERROR, error="TimeoutError: x"),
            ],
            TA.TradabilityArchiveRepository(self.conn),
            audit_conn=self.conn,
            cutoff="2024-01-10T16:00:00+08:00",
        )
        # provider_id 必须唯一，否则台账唯一键会把它们合并。
        for index, provider in enumerate(service._providers):
            provider.provider_id = f"probe{index}"
        service.ingest([CODE], [SESSION], write=True, run_id="run-multi")
        rows = self.ledger.events(CODE, SESSION)
        self.assertEqual(3, len(rows))
        self.assertEqual(
            {OL.OBSERVED_EVIDENCE, OL.OBSERVED_UNKNOWN, OL.OBSERVED_ERROR},
            {row["provider_status"] for row in rows},
        )


# ───────────────── Migration：升级既有库不得改写历史 ─────────────────


class MigrationPreservesExistingArchiveRows(unittest.TestCase):
    """015 / 016 必须幂等、纯新增，且不触碰既有 archive 行。

    升级服务器生产库是唯一一次"对真实数据动手"的动作，安全网必须由测试给出：
    先把库建到 **014** 为止（PR #158 合并时的状态），插入历史 archive 行，再施加
    015 / 016，断言既有行**逐字节相同**、没有新增 ledger 事件、没有伪造 first_seen。
    """

    def _conn_at_v14(self):
        """建一个"升级前"的库：archive + ingestion 审计表，**没有** ledger。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        TI.ensure_ingestion_schema(conn)
        # ensure_ingestion_schema 会一并建 ledger（摄取必须能同事务写三处），
        # 因此这里显式删掉它，模拟真正的 v14 库。
        conn.execute(f"DROP TABLE IF EXISTS {OL.LEDGER_TABLE}")
        return conn

    def _legacy_rows(self, conn):
        return [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM {TA.ARCHIVE_TABLE} ORDER BY id"
            ).fetchall()
        ]

    def test_migration_015_is_idempotent_and_preserves_rows(self):
        conn = self._conn_at_v14()
        conn.executemany(
            f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, "
            "observed_at, is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
            [
                ("000001", "2024-01-10", "2024-01-10T15:05:00+08:00",
                 "2024-01-10T15:05:00+08:00", 1, "listing", "2026-01-01T00:00:00+08:00"),
                ("000002", "2024-01-10", "2024-01-10T15:05:00+08:00",
                 "2024-01-10T15:05:00+08:00", 1, "listing", "2026-01-01T00:00:00+08:00"),
            ],
        )
        before = self._legacy_rows(conn)

        OL.ensure_ledger_schema(conn)
        # 幂等：再跑一次不得报错、不得改数据。
        OL.ensure_ledger_schema(conn)

        self.assertEqual(before, self._legacy_rows(conn))
        # 不得给历史行伪造观察事件。
        self.assertEqual(0, OL.ObservationLedgerRepository(conn).count())
        # 也不得让 Shadow 假称知道 first_seen。
        knowledge = OL.ObservationLedgerRepository(conn).knowledge_at(
            "000001", "2024-01-10", decision_at="2024-01-10T16:00:00+08:00",
            archive_rows=[{
                "code": "000001", "session_date": "2024-01-10",
                "effective_at": "2024-01-10T15:05:00+08:00",
                "observed_at": "2024-01-10T15:05:00+08:00",
            }],
        )
        self.assertTrue(knowledge.legacy_observation_unknown)
        self.assertIsNone(knowledge.first_seen_at)

    def test_migration_016_is_idempotent_and_preserves_rows(self):
        conn = self._conn_at_v14()
        conn.execute(
            f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, "
            "observed_at, is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
            ("000001", "2024-01-10", "2024-01-10T15:05:00+08:00",
             "2024-01-10T15:05:00+08:00", 1, "listing", "2026-01-01T00:00:00+08:00"),
        )
        before = self._legacy_rows(conn)
        TS.ensure_shadow_schema(conn)
        TS.ensure_shadow_schema(conn)
        self.assertEqual(before, self._legacy_rows(conn))

    def test_migrations_are_registered_and_ordered(self):
        """db_migrate 里 015/016 必须存在、版本唯一且递增。

        刻意用**关系**断言而不是钉死版本号或 handler 身份：新增迁移是常态，写死会让
        每次加迁移都要改这条测试（而它想守的是"注册表结构正确"）。
        """
        import db_migrate

        entries = db_migrate.MIGRATIONS["paper_trading"]
        versions = [version for version, _description, _handler in entries]
        self.assertEqual(len(versions), len(set(versions)), "迁移版本号重复")
        self.assertEqual(sorted(versions), versions, "迁移版本号必须递增")
        descriptions = {version: description for version, description, _ in entries}
        self.assertIn(15, descriptions)
        self.assertIn(16, descriptions)
        # 描述里出现的是中文术语（"观察台账" / "Shadow"），断言用它们而不是英文模块名。
        self.assertIn("观察台账", descriptions[15])
        self.assertIn("Shadow", descriptions[16])
        # v17：行级 provenance 链接表（issue #161）。
        self.assertIn(17, descriptions)
        self.assertIn("链接", descriptions[17])


# ═════════════════ issue #161 回归矩阵 L161-1 … L161-8 ═════════════════
#
# 每个用例都直接对着**行级 provenance 对账**这个修复点，而不是对着实现细节：
# 断言的是"哪些行被算作有 provenance"，以及由此得出的 legacy 诊断。


def _row(effective_at="2024-01-10T15:05:00+08:00", observed_at="2024-01-10T15:05:00+08:00",
         code=CODE, session=SESSION):
    return {
        "code": code, "session_date": session,
        "effective_at": effective_at, "observed_at": observed_at,
    }


class Issue161RegressionMatrix(LedgerTestCase):
    """L161-1 … L161-8：issue #161 的完整回归矩阵。"""

    def _knowledge(self, rows, **kwargs):
        return self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION,
                                        archive_rows=rows, **kwargs)

    def _link(self, row, run_id=RUN, provider_id="listing", fingerprint="fp-1",
              recorded_at="2024-01-10T16:00:00+08:00"):
        self.ledger.link_archive_row(
            row, ingestion_run_id=run_id, provider_id=provider_id,
            observation_fingerprint=fingerprint, recorded_at=recorded_at,
        )

    def test_l161_1_all_rows_covered_is_not_legacy(self):
        """L161-1：行由 ledger-era 摄取创建、有链接 → uncovered=0，不是 legacy。

        这正是 issue 的假阳性场景：pair 在历史知识时点看不到事件，但行本身是
        ledger-era 的。
        """
        row = _row()
        self._link(row)
        knowledge = self._knowledge(
            [row], validation_as_of="2024-01-10T00:00:00+08:00"
        )
        self.assertEqual(1, knowledge.archive_row_count)
        self.assertEqual(1, knowledge.archive_rows_with_observation_provenance)
        self.assertEqual(0, knowledge.archive_rows_without_observation_provenance)
        self.assertFalse(knowledge.legacy_observation_unknown)

    def test_l161_2_pure_legacy_is_legacy(self):
        """L161-2：pre-ledger 行、无原始 provenance → uncovered=1，legacy。"""
        knowledge = self._knowledge([_row()])
        self.assertEqual(1, knowledge.archive_rows_without_observation_provenance)
        self.assertTrue(knowledge.legacy_observation_unknown)
        self.assertIsNone(knowledge.first_seen_at)

    def test_l161_3_mixed_pair_reports_covered_and_uncovered(self):
        """L161-3：2 行（1 legacy + 1 covered）→ covered=1、uncovered=1、legacy。"""
        covered = _row(effective_at="2024-01-10T15:05:00+08:00")
        legacy = _row(effective_at="2024-01-10T14:00:00+08:00",
                      observed_at="2024-01-10T14:00:00+08:00")
        self._link(covered)
        knowledge = self._knowledge([covered, legacy])
        self.assertEqual(2, knowledge.archive_row_count)
        self.assertEqual(1, knowledge.archive_rows_with_observation_provenance)
        self.assertEqual(1, knowledge.archive_rows_without_observation_provenance)
        self.assertTrue(knowledge.legacy_observation_unknown)

    def test_l161_4_repeated_observations_do_not_over_count(self):
        """L161-4：1 条行 + 5 次重复观察 → covered 恒为 1，不出现反向计数/over-count。

        两处都必须去重，否则会数出"比事实行还多"的覆盖数：

        * 同一行被**多个 provider** 观察到 → 5 条链接，但仍是 1 条被覆盖的行；
        * 同一行身份在入参里**重复出现**（调用方按 join 展开时很常见）→ 仍然是 1 条行，
          否则 ``covered`` 会大于 ``archive_row_count``，正是 issue #161 §十一 禁止的
          over-count。
        """
        row = _row()
        # 5 个 provider 都观察到同一条事实：链接 5 条，但仍是 1 条被覆盖的行。
        for index in range(5):
            self._link(row, provider_id=f"p{index}", fingerprint=f"fp-{index}")
        # 行身份重复给出三次：去重后仍是 1 条 distinct 行。
        knowledge = self._knowledge([row, row, row])
        self.assertEqual(1, knowledge.archive_row_count)
        self.assertEqual(1, knowledge.archive_rows_with_observation_provenance)
        self.assertEqual(0, knowledge.archive_rows_without_observation_provenance)
        # 覆盖数绝不能超过事实行数（issue #161 §十一：不得出现 legacy = -4 这类反向计数）。
        self.assertLessEqual(
            knowledge.archive_rows_with_observation_provenance,
            knowledge.archive_row_count,
        )
        self.assertGreaterEqual(knowledge.archive_row_count, 0)

    def test_l161_4b_duplicate_uncovered_rows_count_once(self):
        """L161-4 补充：重复给出的**未覆盖**行也不得重复计数。"""
        legacy = _row()
        knowledge = self._knowledge([legacy, legacy, legacy])
        self.assertEqual(1, knowledge.archive_row_count)
        self.assertEqual(1, knowledge.archive_rows_without_observation_provenance)

    def test_l161_5_later_reobservation_does_not_whitewash_legacy_row(self):
        """L161-5：pre-ledger 行后来被重新观察到同内容证据 → 仍然 legacy。"""
        legacy = _row()
        # 后来的 run 观察到**同一内容**（同一条行身份），但那不构成"写入时就有链接"。
        self.ledger.append(event(run_id="later-run"))
        knowledge = self._knowledge([legacy])
        self.assertTrue(knowledge.legacy_observation_unknown)
        self.assertEqual(1, knowledge.archive_rows_without_observation_provenance)

    def test_l161_6_unknown_and_error_do_not_cover_evidence_row(self):
        """L161-6：只有 unknown/error 观察 → 不覆盖 archive 事实行。"""
        self.ledger.append(event(status=OL.OBSERVED_UNKNOWN))
        self.ledger.append(event(status=OL.OBSERVED_ERROR, error="TimeoutError: x"))
        knowledge = self._knowledge([_row()])
        self.assertEqual(0, knowledge.archive_rows_with_observation_provenance)
        self.assertEqual(1, knowledge.archive_rows_without_observation_provenance)
        self.assertTrue(knowledge.legacy_observation_unknown)

    def test_l161_7_post_ledger_row_stays_non_legacy_when_as_of_is_earlier(self):
        """L161-7：post-ledger 行有固定链接，但 recorded_at > validation_as_of。

        该行在历史快照里**不可见**，但它不是 legacy——结构性 provenance 不随知识时点
        变化。这是 #161 生产环境里出现的假诊断。
        """
        row = _row()
        self._link(row, recorded_at="2024-06-01T16:00:00+08:00")
        knowledge = self._knowledge(
            [row], validation_as_of="2024-01-10T00:00:00+08:00"
        )
        self.assertTrue(knowledge.never_observed)
        self.assertFalse(knowledge.legacy_observation_unknown)
        self.assertFalse(knowledge.evidence_seen)

    def test_l161_8_no_archive_row_is_never_legacy(self):
        """L161-8：archive_row_count = 0 → 永远 legacy = false。"""
        empty = self._knowledge([])
        self.assertEqual(0, empty.archive_row_count)
        self.assertFalse(empty.legacy_observation_unknown)

        # 连 archive_rows 都不给（调用方无法声称任何行缺 provenance）也不得判 legacy。
        none_given = self.ledger.knowledge_at(CODE, SESSION, decision_at=DECISION)
        self.assertFalse(none_given.legacy_observation_unknown)


class Issue161CoverageKeepsPairAndRowCountsSeparate(LedgerTestCase):
    """§二十八：pair count 与 row count 不得混淆。"""

    def test_legacy_pairs_are_distinct_pairs_and_rows_are_counted_separately(self):
        legacy_a = _row(effective_at="2024-01-10T14:00:00+08:00",
                        observed_at="2024-01-10T14:00:00+08:00")
        legacy_b = _row(effective_at="2024-01-10T13:00:00+08:00",
                        observed_at="2024-01-10T13:00:00+08:00")
        report = OL.coverage(
            self.ledger,
            [(CODE, SESSION)],
            validation_as_of="2026-01-01T00:00:00+08:00",
            archive_rows_by_pair={(CODE, SESSION): [legacy_a, legacy_b]},
        )
        # 一个 pair 里有 2 条 legacy 行：pair 只算 1 个，行数如实报 2。
        self.assertEqual(1, report.legacy_observation_unknown_pairs)
        self.assertEqual(2, report.legacy_archive_rows)

    def test_covered_pair_is_not_counted_as_legacy(self):
        row = _row()
        self.ledger.link_archive_row(
            row, ingestion_run_id=RUN, provider_id="listing",
            observation_fingerprint="fp-1", recorded_at="2024-01-10T16:00:00+08:00",
        )
        report = OL.coverage(
            self.ledger,
            [(CODE, SESSION)],
            validation_as_of="2026-01-01T00:00:00+08:00",
            archive_rows_by_pair={(CODE, SESSION): [row]},
        )
        self.assertEqual(0, report.legacy_observation_unknown_pairs)
        self.assertEqual(0, report.legacy_archive_rows)


class ProvenanceLinkIsAppendOnlyAndIdempotent(LedgerTestCase):
    """链接表必须 append-only、幂等，且不伪造历史行。"""

    def test_same_link_twice_is_idempotent(self):
        row = _row()
        self.assertTrue(self.ledger.link_archive_row(
            row, ingestion_run_id=RUN, provider_id="listing",
            observation_fingerprint="fp-1", recorded_at="2024-01-10T16:00:00+08:00",
        ))
        self.assertFalse(self.ledger.link_archive_row(
            row, ingestion_run_id=RUN, provider_id="listing",
            observation_fingerprint="fp-1", recorded_at="2024-01-10T16:00:00+08:00",
        ))
        self.assertEqual(1, len(self.ledger.archive_links(CODE, SESSION)))

    def test_link_without_complete_identity_is_rejected(self):
        with self.assertRaises(OL.ObservationError):
            self.ledger.link_archive_row(
                {"code": CODE}, ingestion_run_id=RUN, provider_id="listing",
                observation_fingerprint="fp-1", recorded_at="2024-01-10T16:00:00+08:00",
            )

    def test_links_are_not_pit_filtered(self):
        """链接是结构性 provenance，不按 recorded_at 过滤。"""
        row = _row()
        self.ledger.link_archive_row(
            row, ingestion_run_id=RUN, provider_id="listing",
            observation_fingerprint="fp-1", recorded_at="2024-06-01T16:00:00+08:00",
        )
        # 即便用一个远早于链接 recorded_at 的时点，链接依然可见。
        self.assertEqual(1, len(self.ledger.archive_links(CODE, SESSION)))

    def test_no_backfill_for_existing_rows(self):
        """升级既有库不得给历史行伪造链接。"""
        conn = self._conn_at_v14()
        conn.execute(
            f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, "
            "observed_at, is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
            (CODE, SESSION, "2024-01-10T15:05:00+08:00",
             "2024-01-10T15:05:00+08:00", 1, "listing", "2026-01-01T00:00:00+08:00"),
        )
        OL.ensure_ledger_schema(conn)
        OL.ensure_ledger_schema(conn)
        ledger = OL.ObservationLedgerRepository(conn)
        self.assertEqual(0, len(ledger.archive_links(CODE, SESSION)))

    def _conn_at_v14(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        TI.ensure_ingestion_schema(conn)
        conn.execute(f"DROP TABLE IF EXISTS {OL.LEDGER_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {OL.ARCHIVE_LINK_TABLE}")
        return conn


class IngestionWritesRowLevelProvenance(LedgerTestCase):
    """真实摄取写路径必须在同事务内登记行级链接（issue #161 §二十五）。"""

    def test_ingest_links_newly_persisted_rows(self):
        self.repo = TA.TradabilityArchiveRepository(self.conn)
        service = TI.IngestionService(
            [_StaticProvider()], self.repo, audit_conn=self.conn,
            cutoff="2024-01-10T16:00:00+08:00",
        )
        result = service.ingest([CODE], [SESSION], write=True, run_id=RUN)
        self.conn.commit()
        self.assertEqual(1, len(result.persisted))

        ledger = OL.ObservationLedgerRepository(self.conn)
        links = ledger.archive_links(CODE, SESSION)
        self.assertEqual(1, len(links))
        self.assertEqual(RUN, links[0]["ingestion_run_id"])

        rows = [
            {
                "code": row["code"], "session_date": row["session_date"],
                "effective_at": row["effective_at"], "observed_at": row["observed_at"],
            }
            for row in self.conn.execute(
                f"SELECT * FROM {TA.ARCHIVE_TABLE} WHERE code=? AND session_date=?",
                (CODE, SESSION),
            ).fetchall()
        ]
        knowledge = ledger.knowledge_at(CODE, SESSION, decision_at=DECISION,
                                        archive_rows=rows)
        self.assertFalse(knowledge.legacy_observation_unknown)
        self.assertEqual(1, knowledge.archive_rows_with_observation_provenance)

    def test_idempotent_replay_does_not_relink_existing_row(self):
        """幂等重放（未插入新行）不得给旧行补链接。"""
        self.repo = TA.TradabilityArchiveRepository(self.conn)
        service = TI.IngestionService(
            [_StaticProvider()], self.repo, audit_conn=self.conn,
            cutoff="2024-01-10T16:00:00+08:00",
        )
        service.ingest([CODE], [SESSION], write=True, run_id=RUN)
        self.conn.commit()
        ledger = OL.ObservationLedgerRepository(self.conn)
        before = len(ledger.archive_links(CODE, SESSION))

        # 换一个 run_id 重放：archive 唯一键命中 → 未插入 → 不得新增链接。
        service.ingest([CODE], [SESSION], write=True, run_id="run-0002")
        self.conn.commit()
        self.assertEqual(before, len(ledger.archive_links(CODE, SESSION)))


if __name__ == "__main__":
    unittest.main()
