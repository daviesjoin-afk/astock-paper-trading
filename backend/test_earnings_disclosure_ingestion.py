# -*- coding: utf-8 -*-
"""``earnings_disclosure_store`` / ingestion / runner 的回归（全离线）。

外部公告源由**可注入 transport** 代替，测试不发任何网络请求；真实网络只在人工
``--dry-run`` 时使用。夹具正文取自真实公告原文（截断）。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import earnings_disclosure_ingestion as ING
import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_runner as RUNNER
import earnings_disclosure_store as STORE

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


def _list_payload(rows, total=None):
    return {"data": {"list": rows, "total_hits": len(rows) if total is None else total}}


def _row(art_code, title, code="002230", display="2026-07-16 09:30:00"):
    return {
        "art_code": art_code,
        "title": title,
        "display_time": display,
        "codes": [{"stock_code": code}],
    }


class FakeSource:
    """按 URL 分流的假公告源。"""

    def __init__(self, *, rows, bodies=None, list_error=None, body_error=(), total=None):
        self.rows = rows
        self.bodies = dict(bodies or {})
        self.list_error = list_error
        self.body_error = set(body_error)
        self.total = total
        self.list_calls = 0
        self.body_calls = []

    def __call__(self, url, params=None, timeout=None):
        if url == ING.MARKET_ANNOUNCEMENT_URL:
            self.list_calls += 1
            if self.list_error is not None:
                raise self.list_error
            page = int((params or {}).get("page_index") or 1)
            size = int((params or {}).get("page_size") or 100)
            start = (page - 1) * size
            return _list_payload(self.rows[start:start + size], self.total)
        art = (params or {}).get("art_code")
        self.body_calls.append(art)
        if art in self.body_error:
            raise TimeoutError("body timeout")
        return {"data": {"notice_content": self.bodies.get(art, UNPARSABLE_BODY)}}


class StoreTest(unittest.TestCase):
    def test_db_path_honours_the_data_dir_override(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"ASTOCK_DATA_DIR": directory}):
                self.assertEqual(STORE.db_path(),
                                 os.path.join(directory, STORE.DB_FILENAME))
                self.assertEqual(os.path.dirname(STORE.ensure()),
                                 os.path.abspath(directory))

    def test_readonly_connection_never_creates_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, STORE.DB_FILENAME)
            with self.assertRaises(LEDGER.EarningsLedgerError) as ctx:
                with STORE.connect_readonly(target):
                    pass
            self.assertEqual(ctx.exception.reason, "ledger_unavailable")
            self.assertFalse(os.path.exists(target), "读路径不得创建数据库文件")

    def test_ensure_is_idempotent_and_readonly_blocks_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, STORE.DB_FILENAME)
            STORE.ensure(target)
            STORE.ensure(target)
            with STORE.connect_readonly(target) as conn:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                self.assertIn(LEDGER.EARNINGS_EVENTS_TABLE, tables)
                with self.assertRaises(sqlite3.Error):
                    conn.execute(
                        "INSERT INTO %s (event_key) VALUES ('x')"
                        % LEDGER.EARNINGS_EVENTS_TABLE
                    )

    def test_writable_connection_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, STORE.DB_FILENAME)
            STORE.ensure(target)
            with self.assertRaises(RuntimeError):
                with STORE.connect_writable(target) as conn:
                    LEDGER.record_announcement(
                        conn, code="002230", title="某公司:2026年半年度业绩预告",
                        announcement_id="AN-ROLLBACK", source_name=STORE.DB_FILENAME,
                        source_type="public_aggregator", body=FORECAST_BODY,
                        observed_at=OBSERVED,
                    )
                    raise RuntimeError("boom")
            with STORE.connect_readonly(target) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM %s" % LEDGER.EARNINGS_EVENTS_TABLE).fetchone()[0]
            self.assertEqual(0, rows)


class IngestionTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        LEDGER.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _ingest(self, source, **overrides):
        payload = dict(conn=self.conn, days=1, observed_at=OBSERVED, transport=source)
        payload.update(overrides)
        return ING.ingest_window(**payload)

    def test_records_a_supported_announcement_end_to_end(self):
        source = FakeSource(
            rows=[_row("AN-1", "科大讯飞:2026年半年度业绩预告")],
            bodies={"AN-1": FORECAST_BODY},
        )
        summary = self._ingest(source)
        self.assertEqual(1, summary["counters"]["candidates"])
        self.assertEqual(1, summary["counters"]["recorded"])
        self.assertEqual(0, summary["counters"]["unparsable"])
        self.assertEqual(1, source.list_calls)
        self.assertEqual(["AN-1"], source.body_calls)
        projections = LEDGER.earnings_event_projections(self.conn, as_of="2026-07-16")
        self.assertEqual(1, len(projections))
        self.assertEqual(projections[0].low_wan, LEDGER.Decimal("-22800"))
        self.assertEqual(projections[0].first_seen_at, "2026-07-16T10:00:00+08:00")
        self.assertEqual(summary["window"],
                         {"begin_day": "2026-07-16", "end_day": "2026-07-16"})

    def test_window_spans_the_requested_lookback(self):
        source = FakeSource(rows=[])
        summary = self._ingest(source, days=3)
        self.assertEqual(summary["window"],
                         {"begin_day": "2026-07-14", "end_day": "2026-07-16"})

    def test_unsupported_titles_are_counted_with_their_reason(self):
        source = FakeSource(rows=[
            _row("AN-1", "贵州茅台:关于召开2026年半年度业绩说明会的公告"),
            _row("AN-2", "海康威视:2025年度业绩快报(英文版)"),
            _row("AN-3", "中信证券:关于华夏基金2026年半年度业绩快报的公告"),
        ])
        summary = self._ingest(source)
        self.assertEqual(0, summary["counters"]["candidates"])
        self.assertEqual([], source.body_calls, "拒绝的公告不该去抓正文")
        self.assertEqual({"not_earnings_announcement": 1, "english_mirror": 1,
                          "third_party_subject": 1}, summary["rejected"])

    def test_body_failure_is_retried_on_the_next_run(self):
        # 先失败一次：不落库（否则空正文占位会让真实正文永远进不来 —— 内容冲突 fail closed）。
        broken = FakeSource(rows=[_row("AN-1", "科大讯飞:2026年半年度业绩预告")],
                            bodies={"AN-1": FORECAST_BODY}, body_error={"AN-1"})
        summary = self._ingest(broken)
        self.assertEqual(1, summary["counters"]["body_failures"])
        self.assertEqual(0, summary["counters"]["recorded"])
        self.assertEqual((), LEDGER.earnings_event_projections(self.conn, as_of="2026-07-16"))

        healthy = FakeSource(rows=[_row("AN-1", "科大讯飞:2026年半年度业绩预告")],
                             bodies={"AN-1": FORECAST_BODY})
        summary = self._ingest(healthy)
        self.assertEqual(1, summary["counters"]["recorded"])
        self.assertEqual(0, summary["counters"]["conflicts"])

    def test_multi_code_announcement_is_not_guessed(self):
        row = _row("AN-1", "某公司:2026年半年度业绩预告")
        row["codes"] = [{"stock_code": "002230"}, {"stock_code": "600036"}]
        summary = self._ingest(FakeSource(rows=[row], bodies={"AN-1": FORECAST_BODY}))
        self.assertEqual(1, summary["counters"]["ambiguous_code"])
        self.assertEqual(0, summary["counters"]["recorded"])
        self.assertEqual((), LEDGER.earnings_event_projections(self.conn, as_of="2026-07-16"))

    def test_missing_art_code_is_counted_not_recorded(self):
        row = _row("", "某公司:2026年半年度业绩预告")
        summary = self._ingest(FakeSource(rows=[row]))
        self.assertEqual(1, summary["counters"]["missing_id"])
        self.assertEqual(0, summary["counters"]["recorded"])

    def test_unreadable_body_is_recorded_as_unparsable(self):
        summary = self._ingest(FakeSource(
            rows=[_row("AN-1", "科大讯飞:2026年半年度业绩预告")],
            bodies={"AN-1": UNPARSABLE_BODY},
        ))
        self.assertEqual(1, summary["counters"]["recorded"])
        self.assertEqual(1, summary["counters"]["unparsable"])
        (projection,) = LEDGER.earnings_event_projections(self.conn, as_of="2026-07-16")
        self.assertEqual(LEDGER.VERIFICATION_UNPARSABLE, projection.verification_status)

    def test_second_run_is_idempotent(self):
        rows = [_row("AN-1", "科大讯飞:2026年半年度业绩预告")]
        bodies = {"AN-1": FORECAST_BODY}
        self._ingest(FakeSource(rows=rows, bodies=bodies))
        later = OBSERVED + dt.timedelta(hours=6)
        second = FakeSource(rows=rows, bodies=bodies)
        summary = self._ingest(second, observed_at=later)
        # 已知公告在**花正文预算之前**就被跳过：既不重复抓正文，也不推进 first_seen_at。
        self.assertEqual(1, summary["counters"]["already_recorded"])
        self.assertEqual(0, summary["counters"]["recorded"])
        self.assertEqual([], second.body_calls)
        (projection,) = LEDGER.earnings_event_projections(self.conn, as_of="2026-07-16")
        self.assertEqual("2026-07-16T10:00:00+08:00", projection.first_seen_at)

    def test_body_budget_truncates_the_run(self):
        rows = [_row("AN-%d" % index, "某公司:2026年半年度业绩预告")
                for index in range(5)]
        bodies = {row["art_code"]: FORECAST_BODY for row in rows}
        summary = self._ingest(FakeSource(rows=rows, bodies=bodies), max_bodies=2)
        self.assertTrue(summary["truncated_bodies"])
        self.assertEqual(2, summary["bodies_fetched"])
        self.assertEqual(2, summary["counters"]["recorded"])

    def test_budget_makes_progress_across_runs_instead_of_refetching(self):
        # 评审发现 P2 的复现：候选数超过单轮正文预算时，每轮都必须向前推进，
        # 而不是把预算重新花在最新那几条已落库的公告上。
        rows = [_row("AN-%d" % index, "某公司:2026年半年度业绩预告")
                for index in range(5)]
        bodies = {row["art_code"]: FORECAST_BODY for row in rows}

        first = FakeSource(rows=rows, bodies=bodies)
        self._ingest(first, max_bodies=2)
        self.assertEqual(["AN-0", "AN-1"], first.body_calls)

        second = FakeSource(rows=rows, bodies=bodies)
        summary = self._ingest(second, max_bodies=2)
        # 第二轮不再重复抓已经落库的那两条，而是继续抓 AN-2 / AN-3。
        self.assertEqual(["AN-2", "AN-3"], second.body_calls)
        self.assertEqual(2, summary["counters"]["already_recorded"])
        self.assertEqual(2, summary["counters"]["recorded"])

        third = FakeSource(rows=rows, bodies=bodies)
        summary = self._ingest(third, max_bodies=2)
        self.assertEqual(["AN-4"], third.body_calls)
        self.assertEqual(4, summary["counters"]["already_recorded"])
        self.assertEqual(1, summary["counters"]["recorded"])
        self.assertEqual(5, len(LEDGER.earnings_event_projections(
            self.conn, as_of="2026-07-16")))

    def test_already_recorded_announcements_do_not_consume_the_budget(self):
        rows = [_row("AN-OLD-%d" % index, "某公司:2026年半年度业绩预告")
                for index in range(3)]
        bodies = {row["art_code"]: FORECAST_BODY for row in rows}
        self._ingest(FakeSource(rows=rows, bodies=bodies))

        fresh = [_row("AN-NEW", "某公司:2026年半年度业绩预告")] + rows
        source = FakeSource(rows=fresh, bodies={**bodies, "AN-NEW": FORECAST_BODY})
        summary = self._ingest(source, max_bodies=1)
        # 预算 1 条，但 3 条已知不占预算，所以新公告这一轮就能落库。
        self.assertEqual(1, summary["counters"]["recorded"])
        self.assertEqual(3, summary["counters"]["already_recorded"])
        self.assertEqual(["AN-NEW"], source.body_calls)

    def test_list_outage_raises_instead_of_looking_like_an_empty_day(self):
        with self.assertRaises(ING.IngestionError):
            self._ingest(FakeSource(rows=[], list_error=TimeoutError("down")))


class RunnerTest(unittest.TestCase):
    def test_status_on_a_missing_ledger_is_unavailable_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = RUNNER._status(os.path.join(directory, STORE.DB_FILENAME))
        self.assertFalse(payload["available"])
        self.assertEqual("ledger_unavailable", payload["reason"])

    def test_runner_writes_the_ledger_and_reports_the_summary(self):
        source = FakeSource(rows=[_row("AN-1", "科大讯飞:2026年半年度业绩预告")],
                            bodies={"AN-1": FORECAST_BODY})
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, STORE.DB_FILENAME)
            buffer = io.StringIO()
            with mock.patch.object(ING, "_request_json", source):
                with redirect_stdout(buffer):
                    code = RUNNER.main(["--days", "1", "--db", target,
                                        "--observed-at", OBSERVED.isoformat()])
            self.assertEqual(0, code)
            summary = json.loads(buffer.getvalue().strip().splitlines()[-1])
            self.assertEqual(1, summary["counters"]["recorded"])
            status = RUNNER._status(target)
        self.assertTrue(status["available"])
        self.assertEqual({"verified": 1}, status["by_status"])
        self.assertEqual(1, len(status["latest"]))

    def test_runner_reports_failure_when_the_list_is_unreachable(self):
        source = FakeSource(rows=[], list_error=TimeoutError("down"))
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, STORE.DB_FILENAME)
            buffer = io.StringIO()
            with mock.patch.object(ING, "_request_json", source):
                with redirect_stdout(buffer):
                    code = RUNNER.main(["--db", target])
            self.assertEqual(1, code)
            payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
        self.assertEqual("list_unavailable", payload["error"])

    def test_dry_run_never_touches_the_production_path(self):
        source = FakeSource(rows=[_row("AN-1", "科大讯飞:2026年半年度业绩预告")],
                            bodies={"AN-1": FORECAST_BODY})
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            production = os.path.join(directory, "production.sqlite")
            with mock.patch.object(ING, "_request_json", source), \
                    mock.patch.object(STORE, "db_path", return_value=production):
                with redirect_stdout(buffer):
                    code = RUNNER.main(["--dry-run", "--days", "1"])
            self.assertEqual(0, code)
            self.assertFalse(os.path.exists(production),
                             "--dry-run 不得写生产库")
        first = json.loads(buffer.getvalue().strip().splitlines()[0])
        self.assertTrue(first["dry_run"])
        self.assertTrue(os.path.exists(first["path"]))


if __name__ == "__main__":
    unittest.main()
