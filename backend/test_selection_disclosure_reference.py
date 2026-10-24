# -*- coding: utf-8 -*-
"""「公告只是依据之一」这条边界的回归。

**核心断言是机械的**：接不接公告参考，选股结果必须逐字段一致 —— 只要这条成立，
"公告没有参与筛选/排序/否决"就不是一句承诺。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_store as STORE
import paper_selection as PSEL
import selection_disclosure_reference as SDR

FORECAST_BODY = """
    一、本期业绩预计情况

    （一）业绩预告期间

    2026 年 1 月 1 日至 2026 年 6 月 30 日

    项  目                          本报告期                            上年同期

归属于上市公司 亏损：-22,800 万元～-18,000 万元            亏损：-23,892.72 万元
股东的净利润    比上年同期增长：5%～25%
"""

DECLINE_BODY = """
（1）经财务部门初步测算，预计 2026 年前三季度实现归属于母公司所有者的净利润为
4，081.97 万元到 4,989.08 万元，与上年同期相比（法定披露数据），
将减少 4,989.08 万元到 4,081.97 万元，同比下降 45.00%到 55.00%。
"""

TRADING_DAY = "2026-07-16"
OBSERVED = dt.datetime(2026, 7, 16, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
LATER = dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class ReferenceReadTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = mock.patch.dict(os.environ, {"ASTOCK_DATA_DIR": self._directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self):
        STORE.ensure()
        with STORE.connect_writable() as conn:
            LEDGER.record_announcement(
                conn, code="002230", title="科大讯飞:2026年半年度业绩预告",
                announcement_id="AN-GROWTH", source_name="fixture",
                source_type="public_aggregator", body=FORECAST_BODY,
                observed_at=OBSERVED,
            )
            LEDGER.record_announcement(
                conn, code="688386", title="泛亚微透:2026年前三季度业绩预告",
                announcement_id="AN-DECLINE", source_name="fixture",
                source_type="public_aggregator", body=DECLINE_BODY,
                observed_at=OBSERVED,
            )
            LEDGER.record_announcement(
                conn, code="600036", title="招商银行:2026年半年度业绩预告",
                announcement_id="AN-LATE", source_name="fixture",
                source_type="public_aggregator", body=FORECAST_BODY,
                observed_at=LATER,
            )

    def test_both_directions_are_returned_as_plain_references(self):
        self._seed()
        result = SDR.references_for_codes(
            ["002230", "688386", "600036"], as_of=TRADING_DAY)
        self.assertTrue(result["available"])
        growth = result["by_code"]["002230"][0]
        decline = result["by_code"]["688386"][0]
        # 正向与负向拿到**同样的**字段形状，没有任何"方向"标记参与判断。
        self.assertEqual({"identity", "kind", "report_period", "is_correction", "title",
                          "verification_status", "is_owner_verified", "superseded_by",
                          "first_seen_at", "availability_day", "net_profit_wan", "yoy_pct"},
                         set(growth))
        self.assertEqual(set(growth), set(decline))
        self.assertEqual("-22800", growth["net_profit_wan"]["low"])
        self.assertLess(decline["yoy_pct"]["low"], "0")
        for item in (growth, decline):
            self.assertNotIn("action", item)
            self.assertNotIn("veto", item)
            self.assertNotIn("score_delta", item)

    def test_pit_uses_the_selection_business_day(self):
        self._seed()
        result = SDR.references_for_codes(
            ["002230", "600036"], as_of=TRADING_DAY)
        self.assertIn("002230", result["by_code"])
        self.assertNotIn("600036", result["by_code"],
                         "选股日之后才观测到的公告不得倒填进当日参考")

    def test_codes_outside_the_selection_are_not_returned(self):
        self._seed()
        result = SDR.references_for_codes(["000001"], as_of=TRADING_DAY)
        self.assertEqual({}, result["by_code"])

    def test_missing_ledger_degrades_instead_of_raising(self):
        result = SDR.references_for_codes(["002230"], as_of=TRADING_DAY)
        self.assertFalse(result["available"])
        self.assertEqual("ledger_unavailable", result["reason"])
        self.assertEqual({}, result["by_code"])


class SelectionResultUnchangedTest(unittest.TestCase):
    """接与不接公告参考，选股结果必须一模一样。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(mock.patch.dict(
            os.environ, {"ASTOCK_DATA_DIR": self._directory.name}).stop)
        mock.patch.dict(os.environ, {"ASTOCK_DATA_DIR": self._directory.name}).start()
        # 用文件库而不是 :memory:：``latest()`` 自己负责关闭连接，内存库一关就没了。
        self.db = os.path.join(self._directory.name, "paper_selection.sqlite")

    def _connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row  # latest() 用 dict(row) 读行
        return conn

    def _seed_selection(self):
        conn = self._connect()
        PSEL.ensure_schema(conn)
        # 一个 run 带两只 pick：``latest()`` 只挂 primary run 的 picks，
        # 拆成两个 run 会让"负向公告那一只"根本不出现在结果里。
        conn.execute(
            "INSERT INTO paper_selection_runs(id, trade_date, strategy_id, strategy_no,"
            " strategy_name, model_id, status, message, factor_date, topn, source,"
            " created_at, scope, provenance_status, provenance_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, TRADING_DAY, "tq_breakout", 1, "策略1", "tq_breakout", "ok", "",
             TRADING_DAY, 5, "test", f"{TRADING_DAY}T17:30:00+08:00",
             "research", "unknown", "k-tq-1"),
        )
        for rank, (code, score) in enumerate([("600000", 91.5), ("002230", 88.0)], start=1):
            conn.execute(
                "INSERT INTO paper_selection_picks(run_id, trade_date, strategy_id, rank_no,"
                " code, name, industry, price, pct, score, super_net, reasons, news_status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, TRADING_DAY, "tq_breakout", rank, code, "名称",
                 "行业", 10.0, 1.0, score, 1.0, "[]", "ok"),
            )
        conn.commit()
        conn.close()

    def _latest_with(self):
        with mock.patch.object(PSEL, "_connect", side_effect=self._connect):
            return PSEL.latest(TRADING_DAY)

    def test_picks_are_identical_with_and_without_the_disclosure_ledger(self):
        self._seed_selection()

        baseline = self._latest_with()
        self.assertFalse(baseline["disclosure_references"]["available"])

        STORE.ensure()
        with STORE.connect_writable() as ledger:
            # 一个正向、一个负向：两种方向都不得改变选股结果。
            LEDGER.record_announcement(
                ledger, code="002230", title="科大讯飞:2026年半年度业绩预告",
                announcement_id="AN-GROWTH", source_name="fixture",
                source_type="public_aggregator", body=FORECAST_BODY,
                observed_at=OBSERVED,
            )
            LEDGER.record_announcement(
                ledger, code="600000", title="浦发银行:2026年半年度业绩预告",
                announcement_id="AN-DECLINE", source_name="fixture",
                source_type="public_aggregator", body=DECLINE_BODY,
                observed_at=OBSERVED,
            )

        enriched = self._latest_with()
        self.assertTrue(enriched["disclosure_references"]["available"])

        def picks_of(payload):
            return [pick for item in payload["strategies"] for pick in item["picks"]]

        self.assertEqual(picks_of(baseline), picks_of(enriched),
                         "公告参考不得改变任何候选（含负向公告的那一只）")
        self.assertEqual([pick["code"] for pick in picks_of(enriched)],
                         ["600000", "002230"])
        self.assertEqual(set(enriched["disclosure_references"]["by_code"]),
                         {"600000", "002230"})


class NoMechanicalVetoTest(unittest.TestCase):
    """公告事实不得进入任何机械否决 / 触发路径。"""

    #: 决定"能不能买 / 要不要卖"的模块。它们**不得**引用公告事实层。
    DECISION_MODULES = (
        "decision_engine.py",
        "paper_entry_decision.py",
        "paper_risk_decision.py",
        "rebalance_scanner.py",
    )

    def test_decision_paths_do_not_reference_disclosure_facts(self):
        backend = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for name in self.DECISION_MODULES:
            with open(os.path.join(backend, name), encoding="utf-8") as handle:
                source = handle.read()
            for token in ("earnings_disclosure", "selection_disclosure_reference",
                          "ai_research_earnings_adapter"):
                if token in source:
                    offenders.append(f"{name}: {token}")
        self.assertEqual([], offenders,
                         "公告事实只能作为依据；机械否决/触发路径不得直接消费它")


if __name__ == "__main__":
    unittest.main()
