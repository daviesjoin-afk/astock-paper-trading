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

import os
import sqlite3
import sys
import unittest

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
            self.assertEqual(100.0, cov["coverage_ratio"])
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


if __name__ == "__main__":
    unittest.main()
