# -*- coding: utf-8 -*-
"""「策略选股」离线测试：映射、覆盖语义、分组归属、上限与空状态。"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest

import paper_selection as PS


def _picks(prefix, count):
    return [
        {
            "code": f"{prefix}{i:04d}",
            "name": f"{prefix}-{i}",
            "industry": "电子",
            "price": 10.0 + i,
            "pct": 1.0,
            "score": 0.9 - i * 0.01,
            "super_net": 1_000_000 - i,
            "reasons": [f"理由{i}"],
            "news_check": {"status": "clean", "hits": 0},
        }
        for i in range(1, count + 1)
    ]


def _payload(picks, date="2026-09-07"):
    """构造与真实 main._select_uncached 同构的返回。

    真实响应不会在顶层放日期：``historical_factor_date`` 在每只 pick 上，
    ``reference_date`` / ``complete_cutoff`` 在 ``data_quality`` 里。这里刻意
    不复刻“顶层也有日期”的假象，否则 _trade_date_of 退化到只读顶层也测不出来。
    """
    for pick in picks:
        pick["historical_factor_date"] = date
    return {"picks": picks,
            "data_quality": {"reference_date": date, "complete_cutoff": date}}


class PaperSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db = PS.DB_PATH
        PS.DB_PATH = os.path.join(self.tmp.name, "selection_tracking.db")
        self.addCleanup(setattr, PS, "DB_PATH", self.old_db)
        self.calls = []

        def fake_run(model_id, topn):
            self.calls.append((model_id, topn))
            if model_id == "one_to_two":
                return _payload(_picks("6001", 8))
            if model_id == "bottom_reversal":
                return _payload(_picks("6001", 2))
            if model_id == "sentiment_pioneer":
                return _payload([])
            if model_id == "reported_profit_breakout":
                return {"need_init": True, "message": "数据未就绪"}
            raise RuntimeError("boom")

        PS._run_one = fake_run

    # -- 映射 -------------------------------------------------------------
    def test_catalog_has_five_strategies(self):
        items = PS.catalog()
        self.assertEqual(len(items), 5)
        self.assertEqual([item["no"] for item in items], [1, 2, 3, 4, 5])
        self.assertEqual([item["label"] for item in items],
                         ["策略1", "策略2", "策略3", "策略4", "策略5"])
        self.assertTrue(all(item["strategy_name"] for item in items))
        self.assertEqual(items[0]["model_id"], "one_to_two")

    # -- 交易日来源 -------------------------------------------------------
    def test_trade_date_is_read_from_real_payload_shape(self):
        # 真实响应把日期放在 data_quality / pick 上，且顶层没有日期字段。
        self.assertEqual(PS._trade_date_of(_payload(_picks("6001", 3), "2026-09-01")),
                         "2026-09-01")
        self.assertEqual(PS._trade_date_of({"picks": [], "data_quality": {
            "reference_date": "2026-09-02", "complete_cutoff": "2026-09-02"}}),
            "2026-09-02")
        self.assertEqual(PS._trade_date_of({"picks": [], "data_quality": {}}), "")
        self.assertEqual(PS._trade_date_of(None), "")

    def test_run_persists_factor_date(self):
        result = PS.run_daily(topn=5, run_date="2026-09-07")
        self.assertEqual(result["strategies"][0]["factor_date"], "2026-09-07")

    # -- 调度守卫（休市日不得覆盖上一个真实交易日的结果） ------------------
    def _run_cli(self, argv):
        import paper_selection_runner as runner
        old_argv, old_guard = sys.argv, runner._is_trade_day
        sys.argv = ["paper_selection_runner.py"] + argv
        self.addCleanup(setattr, sys, "argv", old_argv)
        self.addCleanup(setattr, runner, "_is_trade_day", old_guard)
        runner._is_trade_day = lambda day: False   # 模拟休市日
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = runner.main()
        return code, buf.getvalue()

    def _count_runs(self):
        conn = sqlite3.connect(PS.DB_PATH)
        try:
            PS.ensure_schema(conn)
            return conn.execute("SELECT COUNT(*) FROM paper_selection_runs").fetchone()[0]
        finally:
            conn.close()

    def test_cli_skips_non_trading_day(self):
        code, output = self._run_cli(["--slot", "daily"])
        self.assertEqual(code, 0)
        self.assertIn("non_trading_day", output)
        self.assertEqual(self._count_runs(), 0)

    def test_cli_manual_date_bypasses_calendar_guard(self):
        # 本用例只关心“显式指定日期仍会执行”，把会抛异常的桩换成正常返回。
        PS._run_one = lambda model_id, topn: _payload(_picks("6001", 3))
        code, _ = self._run_cli(["--slot", "daily", "--date", "2026-09-07"])
        self.assertEqual(code, 0)
        self.assertEqual(self._count_runs(), 5)

    # -- 运行与持久化 -----------------------------------------------------
    def test_run_daily_topn_cap_and_statuses(self):
        result = PS.run_daily(topn=5, run_date="2026-09-07")
        self.assertEqual(result["trade_date"], "2026-09-07")
        by_id = {item["strategy_id"]: item for item in result["strategies"]}
        self.assertEqual(len(by_id["tq_breakout"]["picks"]), 5)          # 上限 5
        self.assertEqual(by_id["tq_breakout"]["status"], "ok")
        self.assertEqual(len(by_id["trend_pullback"]["picks"]), 2)       # 候选不足
        self.assertEqual(by_id["trend_pullback"]["status"], "ok")
        self.assertEqual(by_id["sector_rotation"]["status"], "empty")    # 空状态
        self.assertEqual(by_id["reported_profit_breakout"]["status"], "blocked")
        self.assertEqual(by_id["main_force_top10"]["status"], "error")

    def test_rerun_overwrites_same_trade_date_and_strategy(self):
        PS.run_daily(topn=5, run_date="2026-09-07")
        PS.run_daily(topn=5, run_date="2026-09-07")
        conn = sqlite3.connect(PS.DB_PATH)
        try:
            runs = conn.execute(
                "SELECT COUNT(*) FROM paper_selection_runs WHERE trade_date='2026-09-07' "
                "AND strategy_id='tq_breakout'").fetchone()[0]
            picks = conn.execute(
                "SELECT COUNT(*) FROM paper_selection_picks WHERE trade_date='2026-09-07' "
                "AND strategy_id='tq_breakout'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(runs, 1)   # 覆盖而非追加
        self.assertEqual(picks, 5)

    def test_different_trade_dates_are_kept(self):
        PS.run_daily(topn=5, run_date="2026-09-07")
        PS.run_daily(topn=5, run_date="2026-09-08")
        conn = sqlite3.connect(PS.DB_PATH)
        try:
            days = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM paper_selection_runs ORDER BY trade_date")]
        finally:
            conn.close()
        self.assertEqual(days, ["2026-09-07", "2026-09-08"])

    # -- 分组归属：同股多策略不合并 ---------------------------------------
    def test_same_code_kept_in_each_strategy(self):
        result = PS.run_daily(topn=5, run_date="2026-09-07")
        by_id = {item["strategy_id"]: item for item in result["strategies"]}
        codes_a = {p["code"] for p in by_id["tq_breakout"]["picks"]}
        codes_b = {p["code"] for p in by_id["trend_pullback"]["picks"]}
        self.assertTrue(codes_a & codes_b)  # 存在交集（同一只股票）
        stored = PS.latest(trade_date="2026-09-07")
        stored_by_id = {item["strategy_id"]: item for item in stored["strategies"]}
        # 交集股票在两个分组里各自保留，未被合并或去重
        shared = codes_a & codes_b
        for code in shared:
            self.assertIn(code, {p["code"] for p in stored_by_id["tq_breakout"]["picks"]})
            self.assertIn(code, {p["code"] for p in stored_by_id["trend_pullback"]["picks"]})

    # -- 读取 -------------------------------------------------------------
    def test_latest_returns_grouped_with_meta(self):
        PS.run_daily(topn=5, run_date="2026-09-07")
        data = PS.latest()
        self.assertTrue(data["found"])
        self.assertEqual(data["trade_date"], "2026-09-07")
        self.assertEqual(len(data["strategies"]), 5)
        first = data["strategies"][0]
        self.assertEqual(first["label"], "策略1")
        self.assertTrue(first["strategy_name"])
        self.assertEqual(first["picks"][0]["rank_no"], 1)
        self.assertEqual(first["picks"][0]["reasons"], ["理由1"])
        empty_group = [g for g in data["strategies"] if g["strategy_id"] == "sector_rotation"][0]
        self.assertEqual(empty_group["picks"], [])
        self.assertEqual(empty_group["status"], "empty")

    def test_latest_before_any_run(self):
        data = PS.latest()
        self.assertFalse(data["found"])
        self.assertEqual(data["strategies"], [])

    def test_filter_single_strategy(self):
        PS.run_daily(topn=5, run_date="2026-09-07")
        data = PS.latest(strategy_id="main_force_top10")
        self.assertEqual([g["strategy_id"] for g in data["strategies"]], ["main_force_top10"])


if __name__ == "__main__":
    unittest.main()
