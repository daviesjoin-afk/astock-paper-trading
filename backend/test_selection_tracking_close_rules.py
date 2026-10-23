# -*- coding: utf-8 -*-
"""选股跟踪的两条真实缺陷回归：收盘规则用反了、只读页面在改结构。

缺陷 1（B2）：``update_observations`` 走的取数规则是"报价距当前 ≤240 秒"的
**实时**规则，而定时任务在 15:45 / 16:15 / 16:45 / 17:15 运行 —— 正常的 15:00
收盘报价早已超窗，于是"正常收盘被跳过"；同时页面按钮
（``POST /api/selection-evaluation/refresh``）在盘中也能写一条"收盘收益"，
``UNIQUE(pick_id, observed_date)`` + ``INSERT OR IGNORE`` 使它永久无法纠正。

缺陷 2（B5）：``dashboard()`` 每次都执行 ``ensure_schema()``，后者无条件
删除并重建两个触发器 —— 于是一次 GET 需要写锁、并反复推进
``schema_version``。另一连接持有写事务时，普通 SELECT 能读，dashboard 却报
``database is locked``。

运行：python -m pytest backend/test_selection_tracking_close_rules.py
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest import mock

import market_data_contract as MDC
import selection_tracking as ST

CHINA_TZ = ST.CHINA_TZ


def _frozen_datetime(instant):
    """把 ``ST.dt.datetime.now()`` 冻结在指定时点（保留其它 datetime 语义）。"""

    class _Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    return _Frozen


def _frozen_module(instant):
    return SimpleNamespace(
        datetime=_frozen_datetime(instant),
        date=dt.date,
        time=dt.time,
        timedelta=dt.timedelta,
        timezone=dt.timezone,
    )


def _quote(code, price, quote_at):
    return {"code": code, "name": code, "price": price, "quote_at": quote_at}


class _TrackingDbCase(unittest.TestCase):
    """共享的临时跟踪库（真实 schema + 一组活跃候选）。"""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        os.unlink(self.db_path)  # let SQLite create it in WAL mode
        self.addCleanup(lambda: [os.unlink(p) for p in (
            self.db_path, self.db_path + "-wal", self.db_path + "-shm")
            if os.path.exists(p)])
        patches = [
            mock.patch.object(ST, "DB_PATH", self.db_path),
            mock.patch.object(ST, "refresh_benchmark", return_value=True),
            mock.patch.object(ST, "_benchmark_price_on_or_before", return_value=4000.0),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        ST.ensure_schema()

    def _seed_picks(self, day="2026-10-20", codes=("600000", "000001")):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO selection_runs(
                    run_date, generated_at, strategy, strategy_name, data_asof_date,
                    benchmark_entry_price, universe_size, candidate_count,
                    selected_count, executable_count, source, result_json,
                    provenance_status, provenance_key, asof_day, scope, cycle_id,
                    strategy_id, strategy_version, strategy_checksum
                ) VALUES (?, ?, 'three_day', '3 day', ?, 4000.0, 5000, 10, 2, 2,
                          'test', '{}', 'not_applicable', ?, ?, 'research',
                          NULL, NULL, NULL, NULL)
                """,
                (day, f"{day}T15:05:00+08:00", day, f"k|{day}", day),
            )
            run_id = int(cursor.lastrowid)
            for rank, code in enumerate(codes, start=1):
                conn.execute(
                    """
                    INSERT INTO selection_picks(
                        run_id, rank_no, code, name, industry, entry_price,
                        score, super_net, decision_tier, decision_action, snapshot_json
                    ) VALUES (?, ?, ?, ?, 'bank', 10.0, 1.0, 0.0, 'A', 'buy', '{}')
                    """,
                    (run_id, rank, code, code),
                )
        return run_id

    def _observations(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT pick_id, observed_date, price, holding_days, return_pct "
                "FROM selection_observations ORDER BY pick_id"
            ).fetchall()]


class CloseRuleTests(_TrackingDbCase):
    def test_tracking_requests_the_close_policy_for_the_target_day(self):
        captured = {}

        def fake_refresh(**kwargs):
            captured.update(kwargs)
            return []

        service = mock.Mock()
        service.refresh_rows = fake_refresh
        now = dt.datetime(2026, 10, 23, 15, 45, tzinfo=CHINA_TZ)
        with mock.patch.dict("sys.modules", {"market_data_service": service}):
            self.assertEqual(ST._close_snapshot_rows(now, "2026-10-23"), [])
        self.assertIs(captured.get("policy"), MDC.CLOSE_SNAPSHOT_POLICY)
        self.assertEqual(captured.get("asof_day"), "2026-10-23")
        self.assertEqual(captured.get("now"), now)

    def test_intraday_refresh_records_nothing(self):
        self._seed_picks()
        now = dt.datetime(2026, 10, 23, 10, 30, tzinfo=CHINA_TZ)
        with mock.patch.object(ST, "dt", _frozen_module(now)), \
                mock.patch.object(ST, "_close_snapshot_rows",
                                  return_value=[_quote("600000", 11.0, "2026-10-23 10:30:00")]):
            result = ST.update_observations()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "market has not closed yet")
        self.assertEqual(self._observations(), [], "盘中不得写入所谓收盘收益")

    def test_post_close_refresh_records_the_close_quote(self):
        self._seed_picks()
        now = dt.datetime(2026, 10, 23, 15, 45, tzinfo=CHINA_TZ)
        rows = [
            _quote("600000", 11.0, "2026-10-23 15:00:00"),
            _quote("000001", 10.5, "2026-10-23 15:00:00"),
        ]
        with mock.patch.object(ST, "dt", _frozen_module(now)), \
                mock.patch.object(ST, "_close_snapshot_rows", return_value=rows):
            result = ST.update_observations()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observed"], 2)
        observed = self._observations()
        self.assertEqual([row["price"] for row in observed], [11.0, 10.5])
        self.assertEqual([row["observed_date"] for row in observed], ["2026-10-23"] * 2)
        self.assertAlmostEqual(observed[0]["return_pct"], 10.0, places=6)

    def test_quotes_from_another_day_are_dropped_row_by_row(self):
        self._seed_picks()
        now = dt.datetime(2026, 10, 23, 15, 45, tzinfo=CHINA_TZ)
        rows = [
            _quote("600000", 11.0, "2026-10-23 15:00:00"),
            _quote("000001", 10.5, "2026-10-22 15:00:00"),  # 前一交易日的报价
        ]
        with mock.patch.object(ST, "dt", _frozen_module(now)), \
                mock.patch.object(ST, "_close_snapshot_rows", return_value=rows):
            result = ST.update_observations()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observed"], 1, "跨日报价不得被当成当日收盘")
        self.assertEqual([row["price"] for row in self._observations()], [11.0])

    def test_failure_to_prove_a_close_quote_skips_instead_of_inventing(self):
        self._seed_picks()
        now = dt.datetime(2026, 10, 23, 15, 45, tzinfo=CHINA_TZ)
        with mock.patch.object(ST, "dt", _frozen_module(now)), \
                mock.patch.object(ST, "_close_snapshot_rows", return_value=[]):
            result = ST.update_observations()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["quote_date"], None)
        self.assertEqual(self._observations(), [])

    def test_close_policy_rejects_a_stale_live_window(self):
        """权威规则的实证：15:45 时 15:00 的报价只在收盘政策下有效。"""
        now = dt.datetime(2026, 10, 23, 15, 45, tzinfo=CHINA_TZ)
        rows = [_quote("600000", 11.0, "2026-10-23 15:00:00")]
        live = MDC.close_snapshot_rows(rows, now, "2026-10-23")
        self.assertEqual(len(live), 1, "收盘政策必须接受 15:00 报价")
        # 实时政策（240s）会把同一批报价判为过期 —— 这正是 B2 的根因。
        window = MDC.fresh_ratio(rows, now, MDC.LIVE_MARKET_POLICY.max_age_seconds)
        self.assertEqual(window, 0.0)


class ReadOnlyDashboardTests(_TrackingDbCase):
    def _schema_version(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return int(conn.execute("PRAGMA schema_version").fetchone()[0])

    def test_dashboard_does_not_change_the_schema(self):
        self._seed_picks()
        before = self._schema_version()
        payload = ST.dashboard()
        self.assertEqual(self._schema_version(), before,
                         "只读页面不得重建触发器 / 推进 schema_version")
        self.assertEqual(payload["runs"][0]["run_date"], "2026-10-20")

    def test_dashboard_reads_while_another_writer_holds_the_lock(self):
        self._seed_picks()
        writer = sqlite3.connect(self.db_path, timeout=0.05)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE selection_runs SET candidate_count=candidate_count")
        try:
            payload = ST.dashboard()  # 不得等待写锁
        finally:
            writer.rollback()
        self.assertEqual(payload["runs"][0]["run_date"], "2026-10-20")

    def test_dashboard_on_a_missing_database_reports_no_evidence(self):
        missing = os.path.join(os.path.dirname(self.db_path), "absent-tracking.db")
        with mock.patch.object(ST, "DB_PATH", missing):
            payload = ST.dashboard()
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["strategies"], [])
        self.assertFalse(os.path.exists(missing), "只读页面绝不得创建数据库")

    def test_dashboard_on_an_uninitialized_database_reports_no_evidence(self):
        empty = os.path.join(os.path.dirname(self.db_path), "empty-tracking.db")
        sqlite3.connect(empty).close()  # 存在但没有任何表
        self.addCleanup(lambda: os.path.exists(empty) and os.unlink(empty))
        with mock.patch.object(ST, "DB_PATH", empty):
            payload = ST.dashboard()
        self.assertEqual(payload["runs"], [])
        with closing(sqlite3.connect(empty)) as conn:
            tables = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(), "只读页面绝不得顺手建表")

    def test_schema_upgrade_is_idempotent_and_still_repairs_triggers(self):
        self._seed_picks()
        before = self._schema_version()
        ST.ensure_schema()
        self.assertEqual(self._schema_version(), before,
                         "触发器定义未变时不得重建（否则每次初始化都推进 schema_version）")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DROP TRIGGER trg_selection_runs_provenance_immutable")
        ST.ensure_schema()
        with closing(sqlite3.connect(self.db_path)) as conn:
            names = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'")
            }
        self.assertIn("trg_selection_runs_provenance_immutable", names,
                      "定义缺失时仍必须补建（幂等不等于永不升级）")


if __name__ == "__main__":
    unittest.main()
