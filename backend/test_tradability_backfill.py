# -*- coding: utf-8 -*-
"""回填编排层（:mod:`tradability_backfill`）的回归契约测试。

本文件只 import ``backend`` 内的模块，**绝不 import ``work/``**：这些测试必须
能在只有 Docker 运行时文件（``backend/frontend/deploy``）的环境中 import 并运行
（P-D1）。回填编排逻辑已从 ``work/backfill_tradability_archive.py`` 移入
:mod:`tradability_backfill`，``work/`` 只剩薄 CLI（P-D2）。

覆盖的契约：

* P-F1/P-F2/P-F3/P-F4 —— run_fingerprint 只由事实输入 + scope + cutoff +
  provider/provenance identity 决定，与 write/dry-run、插入与否无关；
* P-S1/P-S2/P-S3/P-S4 —— 日期范围按 A 股交易日历解析，coverage denominator
  是 requested code × trading session，绝不超 100%；
* P-DR1..P-DR6 —— dry-run 真正无数据库副作用（不建表、不改 user_version、
  不改事实行/审计行），但仍正常产出 summary / coverage / fingerprint。
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tradability_archive as TA  # noqa: E402
import tradability_backfill as TB  # noqa: E402
import tradability_ingestion as TI  # noqa: E402


def _listing_provider(*, observed_at="2025-01-01T09:00:00+08:00"):
    return TI.ListingStatusProvider(
        {"000001": {"listing_date": "2010-01-01"}},
        observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
        observed_at=observed_at,
    )


def _make_service(conn, providers, *, cutoff):
    repo = TA.TradabilityArchiveRepository(conn)
    return TI.IngestionService(providers, repo, cutoff=cutoff, audit_conn=conn)


def _table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _index_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _db_snapshot(conn):
    """逻辑快照：表 / 索引 / user_version / 事实行 / 审计行 / 事实内容指纹。

    "失败的 run 不改变数据库"必须逐项验证，而不是只看行数——行数相同但内容被
    改写同样是副作用。
    """

    def _count(table):
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return None

    def _rows(table):
        try:
            cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        except sqlite3.OperationalError:
            return None
        names = [column[0] for column in cursor.description]
        return [tuple(zip(names, row, strict=False)) for row in cursor.fetchall()]

    return {
        "tables": _table_names(conn),
        "indexes": _index_names(conn),
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "archive_rows": _count(TA.ARCHIVE_TABLE),
        "audit_rows": _count(TI.INGESTION_RUNS_TABLE),
        "archive_content": _rows(TA.ARCHIVE_TABLE),
        "audit_content": _rows(TI.INGESTION_RUNS_TABLE),
    }


class FingerprintDryRunEqualsWrite(unittest.TestCase):
    """P-F1：同一 input + scope + cutoff，dry-run fingerprint == write fingerprint。"""

    def test_dry_run_and_write_share_the_same_fingerprint(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn_dry = sqlite3.connect(":memory:")
        conn_write = sqlite3.connect(":memory:")
        try:
            for conn in (conn_dry, conn_write):
                TA.ensure_schema(conn)
                TI.ensure_ingestion_schema(conn)
            dry = _make_service(
                conn_dry, [_listing_provider()], cutoff=cutoff
            ).ingest(["000001"], ["2024-01-10"], write=False, run_id="same-run")
            write = _make_service(
                conn_write, [_listing_provider()], cutoff=cutoff
            ).ingest(["000001"], ["2024-01-10"], write=True, run_id="same-run")
            self.assertEqual(dry.run_fingerprint, write.run_fingerprint)
            # write 确实落了库，dry-run 没有——但 fingerprint 不因此改变。
            self.assertEqual(1, len(write.persisted))
            self.assertEqual(0, len(dry.persisted))
        finally:
            conn_dry.close()
            conn_write.close()


class FingerprintReplayStable(unittest.TestCase):
    """P-F2：首次 write 与完全相同 replay 指纹相同。"""

    def test_first_write_and_replay_share_the_same_fingerprint(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            service = _make_service(conn, [_listing_provider()], cutoff=cutoff)
            first = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="r")
            replay = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="r")
            self.assertEqual(first.run_fingerprint, replay.run_fingerprint)
            # 重放不产生新行。
            self.assertEqual(1, len(first.persisted))
            self.assertEqual(0, len(replay.persisted))
        finally:
            conn.close()


class FingerprintChangesWithEvidence(unittest.TestCase):
    """P-F3：仅改变真实 evidence，fingerprint 改变。"""

    def test_different_evidence_changes_fingerprint(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            a = _make_service(conn, [_listing_provider()], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10"], write=False, run_id="r"
            )
            # 不同的 code → 不同的真实证据。
            b = _make_service(conn, [_listing_provider()], cutoff=cutoff).ingest(
                ["000002"], ["2024-01-10"], write=False, run_id="r"
            )
            self.assertNotEqual(a.run_fingerprint, b.run_fingerprint)
        finally:
            conn.close()


class _VersionedProvider(TI.TradabilityFactProvider):
    """P-F4 用：provider/provenance version 可注入，其余事实完全一致。"""

    provider_id = "versioned"

    def __init__(self, version):
        self.provider_version = version

    def fetch(self, code, session):
        return TI.ProviderResult(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status=TI.OUTCOME_EVIDENCE,
            evidence={"is_listed": True},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )


class FingerprintChangesWithProviderVersion(unittest.TestCase):
    """P-F4：仅改变 provider/provenance version，fingerprint 改变。"""

    def test_provider_version_change_changes_fingerprint(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            v1 = _make_service(conn, [_VersionedProvider("1")], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10"], write=False, run_id="r"
            )
            v2 = _make_service(conn, [_VersionedProvider("2")], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10"], write=False, run_id="r"
            )
            self.assertNotEqual(v1.run_fingerprint, v2.run_fingerprint)
        finally:
            conn.close()


class FingerprintIndependentOfRunIdentity(unittest.TestCase):
    """P-F5：run_id 是 audit identity，**绝不**参与内容指纹。

    同一份 facts/scope/cutoff/provider 用不同 run_id 重放，指纹必须相同。
    """

    def test_different_run_id_shares_the_same_fingerprint(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            a = _make_service(conn, [_listing_provider()], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10"], write=False, run_id="run-a"
            )
            b = _make_service(conn, [_listing_provider()], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10"], write=False, run_id="run-b"
            )
            self.assertNotEqual(a.run_id, b.run_id)
            self.assertEqual(a.run_fingerprint, b.run_fingerprint)
        finally:
            conn.close()


class FingerprintChangesWithScope(unittest.TestCase):
    """P-F6：requested session scope 改变 → fingerprint 改变。"""

    def test_scope_change_changes_fingerprint(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            a = _make_service(conn, [_listing_provider()], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10"], write=False, run_id="r"
            )
            b = _make_service(conn, [_listing_provider()], cutoff=cutoff).ingest(
                ["000001"], ["2024-01-10", "2024-01-11"], write=False, run_id="r"
            )
            self.assertNotEqual(a.run_fingerprint, b.run_fingerprint)
        finally:
            conn.close()


class FingerprintChangesWithCutoff(unittest.TestCase):
    """P-F7：cutoff 改变 → fingerprint 改变。"""

    def test_cutoff_change_changes_fingerprint(self):
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            a = _make_service(
                conn, [_listing_provider()], cutoff="2025-06-01T09:00:00+08:00"
            ).ingest(["000001"], ["2024-01-10"], write=False, run_id="r")
            b = _make_service(
                conn, [_listing_provider()], cutoff="2025-07-01T09:00:00+08:00"
            ).ingest(["000001"], ["2024-01-10"], write=False, run_id="r")
            self.assertNotEqual(a.run_fingerprint, b.run_fingerprint)
        finally:
            conn.close()


class SessionResolutionHonorsCalendar(unittest.TestCase):
    """P-S1/P-S2：日期范围按交易日历过滤，周末与法定休市日不进入 sessions。"""

    def test_weekend_days_are_excluded(self):
        # 2026-09-11(五) → 2026-09-14(一)：中间 12(六)/13(日) 是周末。
        sessions = TB.resolve_sessions(
            "2026-09-11", "2026-09-14", calendar=lambda d: d.weekday() < 5
        )
        self.assertEqual(["2026-09-11", "2026-09-14"], sessions)

    def test_statutory_holiday_is_excluded(self):
        # 2026-09-14 → 2026-09-18，把 09-16(三) 标记为法定休市。
        holiday = {"2026-09-16"}

        def calendar(day):
            if day.isoformat() in holiday:
                return False
            return day.weekday() < 5

        sessions = TB.resolve_sessions("2026-09-14", "2026-09-18", calendar=calendar)
        self.assertEqual(
            ["2026-09-14", "2026-09-15", "2026-09-17", "2026-09-18"], sessions
        )


class CoverageBoundedBySessionPairs(unittest.TestCase):
    """P-S3/P-S4：coverage 以 requested code × session 为分母，绝不超 100%。"""

    def test_multi_session_coverage_does_not_exceed_100(self):
        cutoff = "2025-06-01T09:00:00+08:00"
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            service = _make_service(conn, [_listing_provider()], cutoff=cutoff)
            result = service.ingest(
                ["000001"], ["2024-01-10", "2024-01-11", "2024-01-12"], write=True
            )
            cov = result.coverage
            self.assertEqual(1, cov["requested_codes"])
            self.assertEqual(3, cov["requested_sessions"])
            self.assertEqual(3, cov["requested_pairs"])
            self.assertLessEqual(cov["coverage_ratio"], 100.0)
            self.assertEqual(0.0, cov["coverage_ratio"])  # listing-only：非 fully proven
            self.assertEqual(100.0, cov["evidence_presence_ratio"])
            # P-S4：code_count × session_count == requested_pairs == denominator。
            self.assertEqual(
                cov["requested_codes"] * cov["requested_sessions"],
                cov["requested_pairs"],
            )
        finally:
            conn.close()


class DryRunHasNoDatabaseSideEffect(unittest.TestCase):
    """P-DR1..P-DR6：dry-run 对目标 DB 零副作用。"""

    def _snapshot(self, conn):
        def _count(table):
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                return None  # 表不存在（fresh DB）

        return {
            "tables": _table_names(conn),
            "indexes": _index_names(conn),
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "fact_rows": _count(TA.ARCHIVE_TABLE),
            "audit_rows": _count(TI.INGESTION_RUNS_TABLE),
        }

    def test_dry_run_on_fresh_db_creates_no_schema(self):
        # 全新 DB（无任何业务表）：dry-run 不得隐式建表。
        conn = sqlite3.connect(":memory:")
        try:
            before = self._snapshot(conn)
            result = TB.run_backfill(
                conn, [_listing_provider()], ["000001"], ["2024-01-10"],
                write=False, run_id="dry-fresh",
            )
            after = self._snapshot(conn)
            # P-DR1：table list 一致（dry-run 前只有 sqlite 内部表，之后不新增）。
            self.assertEqual(before["tables"], after["tables"])
            # P-DR2：index list 一致。
            self.assertEqual(before["indexes"], after["indexes"])
            # P-DR3：user_version 不变。
            self.assertEqual(before["user_version"], after["user_version"])
            # P-DR6：dry-run 仍产出 summary / coverage / fingerprint。
            self.assertEqual(1, result.coverage["requested_codes"])
            self.assertIsNotNone(result.run_fingerprint)
            self.assertEqual(0, len(result.persisted))
        finally:
            conn.close()

    def test_dry_run_on_populated_db_changes_nothing(self):
        # 已有 schema + 事实 + 审计的 DB：dry-run 前后逐项一致。
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            TB.run_backfill(
                conn, [_listing_provider()], ["000001"], ["2024-01-10"],
                write=True, run_id="seed",
            )
            before = self._snapshot(conn)
            result = TB.run_backfill(
                conn, [_listing_provider()], ["000001"], ["2024-01-10"],
                write=False, run_id="dry-populated",
            )
            after = self._snapshot(conn)
            # P-DR1..P-DR5：全部不变。
            self.assertEqual(before["tables"], after["tables"])
            self.assertEqual(before["indexes"], after["indexes"])
            self.assertEqual(before["user_version"], after["user_version"])
            self.assertEqual(before["fact_rows"], after["fact_rows"])
            self.assertEqual(before["audit_rows"], after["audit_rows"])
            # P-DR6：summary / coverage / fingerprint 仍生成。
            self.assertEqual(1, result.coverage["evidence_present"])
            self.assertIsNotNone(result.run_fingerprint)
        finally:
            conn.close()


class ExplicitCodeScopeIsHonored(unittest.TestCase):
    """P2-2（deferred）：``codes is None`` 与"显式给出但解析为空"必须严格区分。

    绝不允许"显式 targeted input 解析失败 → 自动 fallback 全市场"：在 ``--write``
    下，一次 ``--codes ,`` 会从小范围命令放大成全市场写入。
    """

    UNIVERSE = {"000001": {}, "000002": {}, "600000": {}}

    def setUp(self):
        patcher = mock.patch.object(TB, "load_listing_records", lambda: dict(self.UNIVERSE))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_code1_missing_codes_uses_default_universe(self):
        self.assertEqual(["000001", "000002", "600000"], TB.resolve_codes(None))

    def test_code1b_limit_applies_to_default_universe(self):
        self.assertEqual(["000001"], TB.resolve_codes(None, limit=1))

    def test_code2_comma_only_is_rejected(self):
        with self.assertRaises(TB.CodeScopeError):
            TB.resolve_codes(",")

    def test_code3_whitespace_only_is_rejected(self):
        with self.assertRaises(TB.CodeScopeError):
            TB.resolve_codes("   ")

    def test_code3b_separators_only_is_rejected(self):
        with self.assertRaises(TB.CodeScopeError):
            TB.resolve_codes(", , ,")

    def test_code4_all_invalid_is_rejected(self):
        for value in ("abc", "12345", "1234567", "600000abc", "00000X"):
            with self.subTest(value=value):
                with self.assertRaises(TB.CodeScopeError):
                    TB.resolve_codes(value)

    def test_code4b_one_invalid_among_valid_is_rejected(self):
        # 静默丢弃非法 token 会让操作员以为"这批全处理了"。
        with self.assertRaises(TB.CodeScopeError):
            TB.resolve_codes("000001,abc")

    def test_code5_explicit_subset_returns_exactly_that_subset(self):
        self.assertEqual(["000001", "600000"], TB.resolve_codes("000001,600000"))
        self.assertEqual(["600000"], TB.resolve_codes("600000"))

    def test_code5b_exchange_prefixes_are_normalized(self):
        self.assertEqual(["000001", "600000"], TB.resolve_codes("SZ000001,SH600000"))

    def test_code5c_duplicates_are_collapsed_in_order(self):
        self.assertEqual(["000001", "600000"], TB.resolve_codes("000001,600000,000001"))

    def test_code6_invalid_explicit_codes_plus_write_leaves_db_untouched(self):
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            before = _db_snapshot(conn)
            with self.assertRaises(TB.CodeScopeError):
                codes = TB.resolve_codes("000001,abc")
                TB.run_backfill(
                    conn, [_listing_provider()], codes, ["2024-01-10"],
                    write=True, run_id="code6",
                )
            self.assertEqual(before, _db_snapshot(conn))
        finally:
            conn.close()

    def test_code6b_empty_explicit_codes_plus_write_leaves_db_untouched(self):
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            before = _db_snapshot(conn)
            with self.assertRaises(TB.CodeScopeError):
                codes = TB.resolve_codes("  ")
                TB.run_backfill(
                    conn, [_listing_provider()], codes, ["2024-01-10"],
                    write=True, run_id="code6b",
                )
            self.assertEqual(before, _db_snapshot(conn))
        finally:
            conn.close()


class SessionScopeMustResolveToTradingSessions(unittest.TestCase):
    """P2-3（deferred）：零交易日范围必须失败，不得报告 completed。"""

    def test_session1_reversed_range_is_rejected(self):
        with self.assertRaises(TB.SessionScopeError):
            TB.resolve_sessions("2026-09-18", "2026-09-14")

    def test_session2_weekend_only_is_rejected(self):
        # 2026-09-12/13 是周六 / 周日。
        with self.assertRaises(TB.SessionScopeError):
            TB.resolve_sessions("2026-09-12", "2026-09-13")

    def test_session3_holiday_only_is_rejected(self):
        # 注入一个把整段都判为休市的日历（模拟全部落在法定休市日）。
        with self.assertRaises(TB.SessionScopeError):
            TB.resolve_sessions("2026-09-14", "2026-09-16", calendar=lambda day: False)

    def test_session4_malformed_date_is_rejected(self):
        for value in ("2026-13-99", "not-a-date", "20260914", "", "2026-09"):
            with self.subTest(value=value):
                with self.assertRaises(TB.SessionScopeError):
                    TB.resolve_sessions(value, "2026-09-18")

    def test_session4b_malformed_explicit_session_is_rejected(self):
        with self.assertRaises(TB.SessionScopeError):
            TB.resolve_sessions(session="2026-13-99")

    def test_session4c_closed_explicit_session_is_rejected(self):
        with self.assertRaises(TB.SessionScopeError):
            TB.resolve_sessions(session="2026-09-13", calendar=lambda day: day.weekday() < 5)

    def test_session5_normal_range_returns_only_trading_sessions(self):
        sessions = TB.resolve_sessions(
            "2026-09-11", "2026-09-15", calendar=lambda day: day.weekday() < 5
        )
        self.assertEqual(["2026-09-11", "2026-09-14", "2026-09-15"], sessions)
        for session in sessions:
            # 每个 session 都是 ``YYYY-MM-DD``（不是自然日枚举出来的别的东西）。
            self.assertRegex(session, r"^\d{4}-\d{2}-\d{2}$")
            self.assertEqual(session, _dt.date.fromisoformat(session).isoformat())

    def test_session5b_explicit_session_is_returned_as_single(self):
        self.assertEqual(
            ["2026-09-14"],
            TB.resolve_sessions(session="2026-09-14", calendar=lambda day: day.weekday() < 5),
        )

    def test_session6_zero_session_write_leaves_db_untouched(self):
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            before = _db_snapshot(conn)
            with self.assertRaises(TB.SessionScopeError):
                sessions = TB.resolve_sessions("2026-09-12", "2026-09-13")
                TB.run_backfill(
                    conn, [_listing_provider()], ["000001"], sessions,
                    write=True, run_id="session6",
                )
            self.assertEqual(before, _db_snapshot(conn))
        finally:
            conn.close()

    def test_session6b_ingestion_rejects_an_empty_session_scope_directly(self):
        # 即使绕过 scope 解析层直接调用编排层，零 session 也必须被拒绝。
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            before = _db_snapshot(conn)
            with self.assertRaises(TI.IngestionError):
                TB.run_backfill(
                    conn, [_listing_provider()], ["000001"], [],
                    write=True, run_id="session6b",
                )
            self.assertEqual(before, _db_snapshot(conn))
        finally:
            conn.close()

    def test_session6c_ingestion_rejects_an_empty_code_scope_directly(self):
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            before = _db_snapshot(conn)
            with self.assertRaises(TI.IngestionError):
                TB.run_backfill(
                    conn, [_listing_provider()], [], ["2024-01-10"],
                    write=True, run_id="session6c",
                )
            self.assertEqual(before, _db_snapshot(conn))
        finally:
            conn.close()


class _VersionedProvider(TI.TradabilityFactProvider):
    """provider/provenance version 可注入，其余事实完全一致。"""

    provider_id = "versioned"

    def __init__(self, version):
        self.provider_version = version

    def fetch(self, code, session):
        return TI.ProviderResult(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status=TI.OUTCOME_EVIDENCE,
            evidence={"is_listed": True},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )


class IngestionTestCase(unittest.TestCase):
    """带 archive + audit schema 的内存库夹具。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        TA.ensure_schema(self.conn)
        TI.ensure_ingestion_schema(self.conn)
        self.repo = TA.TradabilityArchiveRepository(self.conn)

    def make_service(self, providers, *, cutoff=None):
        return TI.IngestionService(
            providers, self.repo, cutoff=cutoff, audit_conn=self.conn
        )


class DivergentReplayIsRejected(IngestionTestCase):
    """P2-1（deferred）：``run_id`` 是审计身份，``run_fingerprint`` 是内容身份。

    contract::

        same run_id + same run_fingerprint  => 幂等重放，允许
        same run_id + different fingerprint => IngestionError，整个事务 rollback
                                               archive 不变、audit 不变
    """

    def _service(self, observed_at):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at=observed_at,
        )
        return self.make_service([provider], cutoff="2025-06-01T09:00:00+08:00")

    def _audit(self, run_id):
        cursor = self.conn.execute(
            "SELECT * FROM tradability_ingestion_runs WHERE run_id=?", (run_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        names = [column[0] for column in cursor.description]
        return dict(zip(names, row, strict=False))

    # R1 —— 同 run_id + 同指纹：允许，且不产生重复事实。
    def test_r1_same_run_id_same_fingerprint_is_idempotent(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        first = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R1")
        count_after_first = self.repo.count("000001")
        audit_after_first = self._audit("R1")
        replay = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R1")

        self.assertEqual(first.run_fingerprint, replay.run_fingerprint)
        self.assertEqual(1, count_after_first)
        self.assertEqual(count_after_first, self.repo.count("000001"))
        self.assertEqual(0, len(replay.persisted))
        # audit 行也不得被重写。
        self.assertEqual(audit_after_first, self._audit("R1"))

    # R2 —— 同 run_id + 改变的 code scope：拒绝。
    def test_r2_changed_code_scope_is_rejected(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R2")
        before = _db_snapshot(self.conn)
        with self.assertRaises(TI.IngestionError):
            service.ingest(["000002"], ["2024-01-10"], write=True, run_id="R2")
        self.assertEqual(before, _db_snapshot(self.conn))

    # R3 —— 同 run_id + 改变的 sessions：拒绝。
    def test_r3_changed_sessions_is_rejected(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R3")
        before = _db_snapshot(self.conn)
        with self.assertRaises(TI.IngestionError):
            service.ingest(["000001"], ["2024-01-10", "2024-01-11"], write=True, run_id="R3")
        self.assertEqual(before, _db_snapshot(self.conn))

    # R4 —— 同 run_id + 改变的 evidence：拒绝。
    def test_r4_changed_evidence_is_rejected(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R4")
        before = _db_snapshot(self.conn)
        # 默认 provider 每次运行都会生成新的 retrieved_at —— 这正是"再跑一次不是重放"
        # 的真实来源，必须被拒绝，而不是让 archive 保存新 revision。
        changed = self._service("2025-02-02T09:00:00+08:00")
        with self.assertRaises(TI.IngestionError):
            changed.ingest(["000001"], ["2024-01-10"], write=True, run_id="R4")
        self.assertEqual(before, _db_snapshot(self.conn))

    # R5 —— 同 run_id + 改变的 provider_version：拒绝。
    def test_r5_changed_provider_version_is_rejected(self):
        service = self.make_service([_VersionedProvider("1")], cutoff="2025-06-01T09:00:00+08:00")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R5")
        before = _db_snapshot(self.conn)
        bumped = self.make_service([_VersionedProvider("2")], cutoff="2025-06-01T09:00:00+08:00")
        with self.assertRaises(TI.IngestionError):
            bumped.ingest(["000001"], ["2024-01-10"], write=True, run_id="R5")
        self.assertEqual(before, _db_snapshot(self.conn))

    # R6 —— divergent replay 之后 archive 行数不变。
    def test_r6_divergent_replay_leaves_archive_row_count_unchanged(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R6")
        rows_before = self.repo.count()
        with self.assertRaises(TI.IngestionError):
            self._service("2025-02-02T09:00:00+08:00").ingest(
                ["000001"], ["2024-01-10"], write=True, run_id="R6"
            )
        self.assertEqual(rows_before, self.repo.count())

    # R7 —— divergent replay 之后 audit 行不变。
    def test_r7_divergent_replay_leaves_audit_row_unchanged(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        first = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R7")
        audit_before = self._audit("R7")
        self.assertIsNotNone(audit_before)
        self.assertEqual(first.run_fingerprint, audit_before["run_fingerprint"])
        with self.assertRaises(TI.IngestionError):
            self._service("2025-02-02T09:00:00+08:00").ingest(
                ["000001"], ["2024-01-10"], write=True, run_id="R7"
            )
        self.assertEqual(audit_before, self._audit("R7"))
        self.assertEqual(
            1,
            self.conn.execute(
                "SELECT COUNT(*) FROM tradability_ingestion_runs WHERE run_id='R7'"
            ).fetchone()[0],
        )

    # 特别审计：新的 retrieved_at 不得冒充旧 run_id 的 replay。
    def test_new_retrieved_at_cannot_impersonate_an_old_run(self):
        first = self._service("2025-01-01T09:00:00+08:00").ingest(
            ["000001"], ["2024-01-10"], write=True, run_id="R-impersonate"
        )
        second = self._service("2025-03-03T09:00:00+08:00")
        with self.assertRaises(TI.IngestionError):
            second.ingest(["000001"], ["2024-01-10"], write=True, run_id="R-impersonate")
        # 第一次的指纹仍然描述 audit 行里那一次运行。
        self.assertEqual(first.run_fingerprint, self._audit("R-impersonate")["run_fingerprint"])

    # 校验必须发生在持久化**之前**：连 dry-run 语义都不受影响。
    def test_rejection_happens_before_any_persistence(self):
        service = self._service("2025-01-01T09:00:00+08:00")
        service.ingest(["000001"], ["2024-01-10"], write=True, run_id="R-order")
        archive_before = _db_snapshot(self.conn)["archive_content"]
        with self.assertRaises(TI.IngestionError):
            self._service("2025-02-02T09:00:00+08:00").ingest(
                ["000001"], ["2024-01-10"], write=True, run_id="R-order"
            )
        # 事实内容逐行相同（不是"行数相同但内容被改写"）。
        self.assertEqual(archive_before, _db_snapshot(self.conn)["archive_content"])

    # 不同 run_id + 相同内容：不是冲突，指纹相同。
    def test_different_run_id_same_content_is_not_a_conflict(self):
        first = self._service("2025-01-01T09:00:00+08:00").ingest(
            ["000001"], ["2024-01-10"], write=True, run_id="R-a"
        )
        second = self._service("2025-01-01T09:00:00+08:00").ingest(
            ["000001"], ["2024-01-10"], write=True, run_id="R-b"
        )
        self.assertEqual(first.run_fingerprint, second.run_fingerprint)
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM tradability_ingestion_runs"
        ).fetchone()[0])


class ReplayIdentityNeedsDurableAudit(IngestionTestCase):
    """P1（review）：replay identity 的判据只存在于审计表里。

    ``IngestionService`` 的默认构造是 ``audit_conn=None``。那种情况下 ``_persist_run``
    不写任何东西，本次运行的指纹**不会**被记录，于是第二次同 run_id 的写入必然读到
    "无既有行"而放行——正好是 P2-1 要挡的事故。因此没有持久审计时必须 fail closed。
    """

    def _service_without_audit(self, observed_at):
        provider = TI.ListingStatusProvider(
            {"000001": {"listing_date": "2010-01-01"}},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at=observed_at,
        )
        repo = TA.TradabilityArchiveRepository(self.conn)
        return TI.IngestionService(
            [provider], repo, cutoff="2025-06-01T09:00:00+08:00"
        )

    def test_write_without_audit_conn_is_rejected(self):
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
            before = _db_snapshot(conn)
            service = TI.IngestionService(
                [TI.ListingStatusProvider({"000001": {}},
                                          observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                                          observed_at="2025-01-01T09:00:00+08:00")],
                TA.TradabilityArchiveRepository(conn),
                cutoff="2025-06-01T09:00:00+08:00",
            )
            with self.assertRaises(TI.IngestionError):
                service.ingest(["000001"], ["2024-01-10"], write=True, run_id="no-audit")
            # 拒绝发生在持久化之前：DB 逻辑状态逐项不变。
            self.assertEqual(before, _db_snapshot(conn))
        finally:
            conn.close()

    def test_divergent_replay_is_impossible_without_durable_audit(self):
        """没有审计表可读 → fail closed，而不是"读不到就当没冲突"。"""
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)  # 故意不建 ingestion_runs 表
            provider = TI.ListingStatusProvider(
                {"000001": {}},
                observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                observed_at="2025-01-01T09:00:00+08:00",
            )
            service = TI.IngestionService(
                [provider],
                TA.TradabilityArchiveRepository(conn),
                cutoff="2025-06-01T09:00:00+08:00",
                audit_conn=conn,
            )
            with self.assertRaises(TI.IngestionError):
                service.ingest(["000001"], ["2024-01-10"], write=True, run_id="no-table")
        finally:
            conn.close()

    def test_dry_run_without_audit_conn_is_still_allowed(self):
        """dry-run 不写任何东西，因此不需要审计存储。"""
        conn = sqlite3.connect(":memory:")
        try:
            TA.ensure_schema(conn)
            provider = TI.ListingStatusProvider(
                {"000001": {}},
                observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                observed_at="2025-01-01T09:00:00+08:00",
            )
            service = TI.IngestionService(
                [provider],
                TA.TradabilityArchiveRepository(conn),
                cutoff="2025-06-01T09:00:00+08:00",
            )
            result = service.ingest(["000001"], ["2024-01-10"], write=False, run_id="dry")
            self.assertIsNotNone(result.run_fingerprint)
            self.assertEqual(0, len(result.persisted))
        finally:
            conn.close()


class _OutcomeFlippingProvider(TI.TradabilityFactProvider):
    """先返回 error、后返回 unknown —— 两次运行都不产生任何 normalized evidence。"""

    provider_id = "flaky"

    def __init__(self):
        self.provider_version = "1"
        self.mode = "error"

    def fetch(self, code, session):
        if self.mode == "error":
            return TI.ProviderResult(
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                status=TI.OUTCOME_ERROR,
                error="boom",
                observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                observed_at="2025-01-01T09:00:00+08:00",
            )
        return TI.ProviderResult(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status=TI.OUTCOME_UNKNOWN,
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2025-01-01T09:00:00+08:00",
        )


class ReplayIdentityCoversProviderOutcomes(IngestionTestCase):
    """P1（review）：error→unknown 的翻转必须改变 replay 身份。

    两种结果都**不产生 normalized evidence**，若指纹只看 evidence，两次运行指纹相同
    会被判成幂等重放，``INSERT OR IGNORE`` 保留第一行 audit —— 本次报告 ``completed``
    而审计仍是 ``completed_with_gaps``。凡是会写进审计的状态/计数差异都属于内容身份。
    """

    def test_error_then_unknown_is_not_an_idempotent_replay(self):
        provider = _OutcomeFlippingProvider()
        service = self.make_service([provider], cutoff="2025-06-01T09:00:00+08:00")
        first = service.ingest(["000001"], ["2024-01-10"], write=True, run_id="flip")
        self.assertEqual(TI.STATUS_COMPLETED_WITH_GAPS, first.status)
        audit_before = self.conn.execute(
            "SELECT status, run_fingerprint, error_records, unknown_records "
            "FROM tradability_ingestion_runs WHERE run_id='flip'"
        ).fetchone()
        self.assertEqual(TI.STATUS_COMPLETED_WITH_GAPS, audit_before[0])

        provider.mode = "unknown"
        with self.assertRaises(TI.IngestionError):
            service.ingest(["000001"], ["2024-01-10"], write=True, run_id="flip")

        after = self.conn.execute(
            "SELECT status, run_fingerprint, error_records, unknown_records "
            "FROM tradability_ingestion_runs WHERE run_id='flip'"
        ).fetchone()
        self.assertEqual(tuple(audit_before), tuple(after))

    def test_outcome_distribution_is_part_of_the_fingerprint(self):
        provider = _OutcomeFlippingProvider()
        service = self.make_service([provider], cutoff="2025-06-01T09:00:00+08:00")
        error_run = service.ingest(["000001"], ["2024-01-10"], write=False, run_id="a")
        provider.mode = "unknown"
        unknown_run = service.ingest(["000001"], ["2024-01-10"], write=False, run_id="b")
        self.assertNotEqual(error_run.run_fingerprint, unknown_run.run_fingerprint)


if __name__ == "__main__":
    unittest.main()
