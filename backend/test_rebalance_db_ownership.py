# -*- coding: utf-8 -*-
"""Round-11 §11–§18：调仓引擎的**数据库归属**契约（E2E-RB1 … E2E-RB6）。

不变量（本文件存在的全部理由）::

    paper_position_lots is the current executable position authority.
    The rebalance engine must read paper facts from the paper ledger.
    A rebalance scan, its risk checks, its current positions, and its persisted
    rebalance state must share one coherent paper-ledger transaction.
    adaptive_learning.sqlite3 must never be mistaken for paper_trading.sqlite3.
    A green unit suite is not proof of database ownership unless the test uses
    two physically separate SQLite files.

**为什么必须两个物理文件**：只要 ``adaptive.DB_PATH`` 与
``adaptive.PAPER_DB_PATH`` 指向同一个文件，`scan 写错库` 与 `scan 写对库`
就是同一件事 —— 这个缺陷**永远测不出来**。所以本文件每个用例都先断言两个路径
``os.path.realpath`` 不同。

**为什么不能只 mock scanner**：``mock.patch("rebalance_scanner.daily_close_scan")``
之后只剩"被调用过"这一个断言，数据库接错仍然全绿。因此本文件的用例驱动真实
``ensure_schema`` / ``PPRM.current_positions`` / ``daily_close_scan`` /
``verify_all_plans``，并且**经 HTTP handler 入口**（``api_adaptive`` 的路由函数）
而不是绕过它直接调 scanner。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adaptive_engine as AE  # noqa: E402
import api_adaptive as API  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"          # max_hold = 4 天（REBALANCE_THRESHOLDS）
OTHER_ACCOUNT = "trend_pullback"
CODE = "600519"
CODE_B = "000001"
NAME = "测试股"

#: 一个必然触发调仓计划的确定性报价（收益 0% < 2% 且持仓远超上限）。
QUOTE_FLAT = {"code": CODE, "price": 10.0, "pct": 0.0, "super_net": 0.0,
              "high": 10.0, "low": 10.0}
QUOTE_FLAT_B = {"code": CODE_B, "price": 10.0, "pct": 0.0, "super_net": 0.0,
                "high": 10.0, "low": 10.0}


class _TwoDatabaseCase(unittest.TestCase):
    """两个**物理分离**的 SQLite：paper ledger 与 adaptive learning。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paper_path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.adaptive_path = os.path.join(self.tmp.name, "adaptive.sqlite3")

        # paper DB：真实生产 schema（paper_accounts / paper_cycles / lots / orders …）
        self._pt_patches = (
            mock.patch.object(PT, "DB_PATH", self.paper_path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._pt_patches:
            patcher.start()
        PT.init_db()

        # adaptive DB：真实 adaptive schema（只有 adaptive 自己的表）
        self._ae_patches = (
            mock.patch.object(AE, "DB_PATH", self.adaptive_path),
            mock.patch.object(AE, "PAPER_DB_PATH", self.paper_path),
            mock.patch.object(AE, "CACHE_DIR", self.tmp.name),
        )
        for patcher in self._ae_patches:
            patcher.start()
        with AE._connect():
            pass

        # 路由层缓存会把 status 结果钉住 30 秒，测试间必须清掉。
        API._cache_clear()

        # 承重前提：两个库必须真的是两个文件，否则本文件证明不了任何事。
        self.assertNotEqual(
            os.path.realpath(self.paper_path), os.path.realpath(self.adaptive_path),
            "paper/adaptive DB 指向同一文件 ⇒ 数据库归属缺陷不可能被测出",
        )
        self.assertFalse(self._tables(self.adaptive_path) & self._rebalance_tables(),
                         "adaptive DB 在测试开始前就含 rebalance 状态")
        self.conn = sqlite3.connect(self.paper_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        API._cache_clear()
        for patcher in reversed(self._ae_patches):
            patcher.stop()
        for patcher in reversed(self._pt_patches):
            patcher.stop()
        self.tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _tables(path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        finally:
            conn.close()

    @staticmethod
    def _rebalance_tables():
        return {"rebalance_scans", "rebalance_plans", "rebalance_cooldown"}

    def _rows(self, path, sql, params=()):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _paper_rows(self, sql, params=()):
        return self._rows(self.paper_path, sql, params)

    def _adaptive_rows(self, sql, params=()):
        return self._rows(self.adaptive_path, sql, params)

    # ── 夹具 ────────────────────────────────────────────────────────────────
    def add_cycle(self, status="running", started_at="2026-09-20 09:30:00"):
        cur = self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (f"c-{started_at[:10]}", status, 100000.0, "shared_pool",
             started_at, started_at, started_at if status == "running" else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def activate(self, cycle_id, account_id=ACCOUNT):
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?",
            (cycle_id, account_id),
        )
        self.conn.commit()

    def add_lot(self, cycle_id, qty, *, code=CODE, account_id=ACCOUNT, cost=10.0,
                acquired_at="2026-08-31 10:00:00", available_date="2026-09-01"):
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
            "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, account_id, code, NAME, "测试", qty, qty, cost,
             acquired_at, available_date, "stock_t1", 1, 1, 4242),
        )
        self.conn.commit()

    def add_mirror(self, qty=100, *, code=CODE, account_id=ACCOUNT, entry_date="2026-08-31"):
        """直接写一条 paper_positions 投影行（模拟旧周期残留镜像）。"""
        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, code, NAME, "测试", qty, 10.0, entry_date, "2026-09-01",
             "stock_t1", 12.5, 2),
        )
        self.conn.commit()

    def current_cycle(self):
        return int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def running_account(self):
        self.conn.execute(
            "UPDATE paper_accounts SET status='running' WHERE id=?", (ACCOUNT,))
        self.conn.commit()

    # ── 驱动真实 HTTP handler ───────────────────────────────────────────────
    def scan(self, quotes):
        """经真实路由函数跑一次调仓扫描（行情已 patch，禁止真实网络）。"""
        with mock.patch.object(API, "_fetch_rebalance_quotes",
                               return_value=(quotes, {"source": "fixture"})):
            return API.run_rebalance_scan(confirmed=True)

    def verify(self, quotes):
        with mock.patch.object(API, "_fetch_rebalance_quotes",
                               return_value=(quotes, {"source": "fixture"})):
            return API.verify_rebalance_plans(confirmed=True)


class E2E_RB1_ScanUsesPaperDB(_TwoDatabaseCase):
    """§12 E2E-RB1：scan 必须在 paper DB 上运行。"""

    def test_RB1_scan_reads_paper_ledger_not_adaptive(self):
        self.running_account()
        self.add_lot(self.current_cycle(), 100)
        self.conn.commit()

        # 旧实现在这里抛 ``no such table: paper_accounts``（adaptive 库没有该表）。
        result = self.scan({CODE: QUOTE_FLAT})

        self.assertIsInstance(result, dict, "scan 未返回结果")
        self.assertGreaterEqual(int(result.get("total_positions") or 0), 1,
                                "paper DB 的 running 账户未被扫描到")

    def test_RB1_scan_does_not_create_rebalance_state_in_adaptive_db(self):
        self.running_account()
        self.add_lot(self.current_cycle(), 100)
        self.conn.commit()
        before = self._tables(self.adaptive_path)

        self.scan({CODE: QUOTE_FLAT})

        after = self._tables(self.adaptive_path)
        created = (after - before) & self._rebalance_tables()
        self.assertEqual(created, set(),
                         f"scan 在 adaptive DB 建出了 rebalance 状态：{created}")


class E2E_RB2_StaleMirrorExcluded(_TwoDatabaseCase):
    """§13 E2E-RB2：陈旧镜像不得进入真实 API 扫描。"""

    def test_RB2_stale_mirror_does_not_produce_scan_row(self):
        # cycle 8：有权威 lot + 镜像；cycle 9：active 但**没有** lot；镜像仍在。
        c8 = self.current_cycle()
        self.add_lot(c8, 100)
        PT._sync_positions(self.conn)
        self.conn.commit()
        self.assertEqual(len(self._paper_rows("SELECT 1 FROM paper_positions")), 1,
                         "夹具未产出陈旧镜像")

        c9 = self.add_cycle()
        self.activate(c9)
        self.running_account()
        self.assertEqual(len(self._paper_rows(
            "SELECT 1 FROM paper_position_lots WHERE cycle_id=?", (c9,))), 0,
            "cycle 9 不应有 lot")

        result = self.scan({CODE: QUOTE_FLAT})

        # 陈旧镜像不得被当成"现在持有" ⇒ 既不进入扫描，也不生成计划。
        self.assertEqual(int(result.get("total_positions") or 0), 0,
                         "陈旧镜像被当成当前持仓进入了扫描")
        scans = self._paper_rows(
            "SELECT code FROM rebalance_scans WHERE account_id=?", (ACCOUNT,))
        self.assertEqual([r["code"] for r in scans], [],
                         "陈旧镜像生成了 rebalance_scan 行")
        plans = self._paper_rows(
            "SELECT code FROM rebalance_plans WHERE account_id=?", (ACCOUNT,))
        self.assertEqual([r["code"] for r in plans], [],
                         "陈旧镜像生成了 rebalance_plan 行")


class E2E_RB3_AuthoritativeLotIncluded(_TwoDatabaseCase):
    """§14 E2E-RB3：当前周期权威 lot 必须真正进入扫描。"""

    def test_RB3_current_lot_reaches_scanner_and_writes_scan_row(self):
        self.running_account()
        self.add_lot(self.current_cycle(), 100)

        result = self.scan({CODE: QUOTE_FLAT})

        self.assertEqual(int(result.get("total_positions") or 0), 1,
                         "权威 lot 未进入 daily_close_scan")
        rows = self._paper_rows(
            "SELECT code,current_qty FROM rebalance_scans WHERE account_id=?", (ACCOUNT,))
        self.assertEqual(len(rows), 1, "权威 lot 未产生可验证的 rebalance_scans 行")
        self.assertEqual(rows[0]["code"], CODE)
        self.assertEqual(int(rows[0]["current_qty"]), 100, "扫描数量不是权威 lot 数量")

    def test_RB3_lot_quantity_beats_stale_mirror_quantity(self):
        """扫描用的是 lot 数量，不是镜像数量。"""
        cycle = self.current_cycle()
        self.add_lot(cycle, 200)
        self.add_mirror(qty=999)          # 陈旧镜像数量更大
        self.running_account()

        self.scan({CODE: QUOTE_FLAT})

        rows = self._paper_rows(
            "SELECT current_qty FROM rebalance_scans WHERE account_id=?", (ACCOUNT,))
        self.assertEqual(int(rows[0]["current_qty"]), 200,
                         "扫描数量取了镜像而不是权威 lot")


class E2E_RB4_WritesLandInPaperDB(_TwoDatabaseCase):
    """§15 E2E-RB4：rebalance 状态必须落在 paper DB。"""

    def test_RB4_scan_writes_to_paper_db_only(self):
        self.running_account()
        self.add_lot(self.current_cycle(), 100)
        adaptive_before = self._tables(self.adaptive_path)

        self.scan({CODE: QUOTE_FLAT})

        self.assertGreater(len(self._paper_rows("SELECT id FROM rebalance_scans")), 0,
                           "paper DB 未收到 rebalance_scans 写入")
        adaptive_after = self._tables(self.adaptive_path)
        self.assertEqual((adaptive_after - adaptive_before) & self._rebalance_tables(),
                         set(),
                         "adaptive DB 因 /rebalance/scan 建出了 rebalance 状态")

    def test_RB4_scan_writes_plans_to_paper_db(self):
        self.running_account()
        # 持仓远超 max_hold(4) 且收益 0% < 2% ⇒ 确定性触发 sell 计划。
        self.add_lot(self.current_cycle(), 100)

        result = self.scan({CODE: QUOTE_FLAT})

        self.assertGreaterEqual(int(result.get("plans_created") or 0), 1,
                                "夹具未达到计划阈值")
        plans = self._paper_rows("SELECT code,status FROM rebalance_plans")
        self.assertEqual([p["code"] for p in plans], [CODE])
        self.assertEqual(plans[0]["status"], "planned")


class E2E_RB5_StatusAndPlansSameDB(_TwoDatabaseCase):
    """§16 E2E-RB5：status / plans 必须读到 scan 刚写下的状态。"""

    def test_RB5_status_sees_scan_written_in_paper_db(self):
        self.running_account()
        self.add_lot(self.current_cycle(), 100)
        self.scan({CODE: QUOTE_FLAT})

        status = API.rebalance_status()

        self.assertEqual([r["code"] for r in status["recent_scans"]], [CODE],
                         "status 未读到 scan 写在 paper DB 的扫描行")

    def test_RB5_plans_sees_scan_written_in_paper_db(self):
        self.running_account()
        self.add_lot(self.current_cycle(), 100)
        self.scan({CODE: QUOTE_FLAT})

        plans = API.get_rebalance_plans(status="all")

        self.assertEqual([p["code"] for p in plans["plans"]], [CODE],
                         "plans 未读到 scan 写在 paper DB 的计划")

    def test_RB5_split_brain_is_impossible(self):
        """承重：scan 写 A、status 读 B 的分裂必须不可表达。"""
        self.running_account()
        self.add_lot(self.current_cycle(), 100)
        self.scan({CODE: QUOTE_FLAT})

        # 断言"status 看到的那一行"与"paper DB 里那一行"是同一行。
        status = API.rebalance_status()
        paper_rows = self._paper_rows(
            "SELECT scan_date,account_id,code,action FROM rebalance_scans"
            " ORDER BY id DESC LIMIT 10")
        self.assertEqual(
            [(r["date"], r["account"], r["code"], r["action"]) for r in status["recent_scans"]],
            [(r["scan_date"], r["account_id"], r["code"], r["action"]) for r in paper_rows],
            "status 读到的扫描行与 paper DB 实际内容不一致（split-brain）",
        )
        # adaptive DB 里根本不该有这些表。
        self.assertEqual(self._tables(self.adaptive_path) & self._rebalance_tables(),
                         set())


class E2E_RB6_VerifySameDB(_TwoDatabaseCase):
    """§17 E2E-RB6：verify 必须读写 paper DB 的计划。"""

    def test_RB6_verify_reads_and_updates_paper_db(self):
        self.running_account()
        cycle = self.current_cycle()
        self.add_lot(cycle, 100)
        self.scan({CODE: QUOTE_FLAT})
        self.assertEqual(
            [p["status"] for p in self._paper_rows("SELECT status FROM rebalance_plans")],
            ["planned"], "夹具未在 paper DB 产出 planned 计划")

        # 开盘跌幅超阈值 ⇒ 确定性 verified（见 verify_opening_data 检查1）。
        quote = {"code": CODE, "price": 9.0, "pct": -5.0, "vol_ratio": 1.0,
                 "super_net": 0.0}
        result = self.verify({CODE: quote})

        self.assertTrue(result.get("plans"), "verify 未处理任何计划")
        rows = self._paper_rows("SELECT status,open_verified FROM rebalance_plans")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "verified",
                         "verify 的写回没有落在 paper DB 的计划上")
        self.assertEqual(int(rows[0]["open_verified"]), 1)
        self.assertEqual(self._tables(self.adaptive_path) & self._rebalance_tables(),
                         set(), "verify 在 adaptive DB 建出了 rebalance 状态")

    def test_RB6_verify_with_no_plans_touches_nothing(self):
        """没有待验证计划时必须早退，且不得在任一库建表。"""
        self.running_account()
        adaptive_before = self._tables(self.adaptive_path)

        result = self.verify({CODE: QUOTE_FLAT})

        self.assertEqual(result.get("plans"), [])
        self.assertEqual((self._tables(self.adaptive_path) - adaptive_before)
                         & self._rebalance_tables(), set())


class RiskLogTableDoesNotExist(_TwoDatabaseCase):
    """§8 附带缺陷：``risk_log`` 表在生产 schema 中不存在。

    ``rebalance_scanner._is_risk_handled`` 曾查询 ``risk_log``，而该表**从未**
    被任何迁移或 ``init_db`` 创建过（``risk_log`` 是 ``_risk_log()`` 这个函数名）。
    这一句必然抛 ``no such table: risk_log``，让调仓扫描 100% 失败 —— 即使数据库
    接对了也跑不通。修法：改用 ``paper_orders.risk_payload`` 的 ``exit_class``，
    与生产代码自己判定"当日是否已发生某类退出"的口径一致。
    """

    def test_risk_log_table_is_not_referenced_by_scanner(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "backend/rebalance_scanner.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("FROM risk_log", src,
                         "rebalance_scanner 仍在查询不存在的 risk_log 表")

    def test_scan_succeeds_on_real_production_schema(self):
        """真实生产 schema 上必须能跑通 —— 这条在修复前必然 RED。"""
        self.running_account()
        self.add_lot(self.current_cycle(), 100)

        result = self.scan({CODE: QUOTE_FLAT})

        self.assertEqual(int(result.get("total_positions") or 0), 1,
                         "真实 schema 上调仓扫描未能完成")

    def test_verified_protective_exit_marks_position_risk_handled(self):
        """被证据证明的风控退出 ⇒ risk_handled（沿用生产同一口径）。

        时间窗取**昨日**：``_is_risk_handled`` 里"今日已卖出"那一支先判
        （``created_at >= today``），只有**昨日**触发的风控退出才会走到本分支
        —— 这正是本分支比 ``filled_sell`` 宽一天的理由。用今日时间戳会命中
        前一支，测不到这里。
        """
        import rebalance_scanner as RS
        self.running_account()
        cycle = self.current_cycle()
        self.add_lot(cycle, 100)
        today = RS._date()
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,realized_pnl,created_at,"
            "executed_at,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_verified,execution_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, NAME, 100, 9.5, 9.5, 950.0, 1.0, "filled",
             "硬止损清仓", '{"exit_class":"hard_stop","exit_reason_code":"hard_stop"}',
             -51.0, f"{yesterday} 09:35:00", f"{yesterday} 09:35:00",
             *stamp, cycle, 1, "verified"),
        )
        self.conn.commit()

        status = RS._is_risk_handled(self.conn, ACCOUNT, CODE, today, cycle)

        self.assertTrue(status["handled"], "已验证的风控退出未被识别为 risk_handled")
        self.assertEqual(status["reason"][:9], "实时风控已触发卖出")
        self.assertIsNotNone(status["order_id"], "risk_handled 必须给出证据委托号")

    def test_unverified_exit_does_not_mark_risk_handled(self):
        """正对照：没有成交证据的卖出不得抑制换仓。"""
        import rebalance_scanner as RS
        self.running_account()
        cycle = self.current_cycle()
        self.add_lot(cycle, 100)
        today = RS._date()
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,realized_pnl,created_at,"
            "executed_at,strategy_id,strategy_version,strategy_checksum,cycle_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, NAME, 100, 9.5, 9.5, 950.0, 1.0, "filled",
             "硬止损清仓", '{"exit_class":"hard_stop","exit_reason_code":"hard_stop"}',
             -51.0, f"{yesterday} 09:35:00", f"{yesterday} 09:35:00",
             *stamp, cycle),
        )
        self.conn.commit()

        status = RS._is_risk_handled(self.conn, ACCOUNT, CODE, today, cycle)

        self.assertFalse(status["handled"],
                         "未验证的卖出被当成了已发生的风控退出")

    def test_non_protective_exit_does_not_mark_risk_handled(self):
        """正对照：非风控退出类别（如集中度换仓）不得抑制换仓。"""
        import rebalance_scanner as RS
        self.running_account()
        cycle = self.current_cycle()
        self.add_lot(cycle, 100)
        today = RS._date()
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,realized_pnl,created_at,"
            "executed_at,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_verified,execution_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "sell", CODE, NAME, 100, 9.5, 9.5, 950.0, 1.0, "filled",
             "集中度换仓", '{"exit_class":"none","exit_marker":"concentration_exit"}',
             -51.0, f"{yesterday} 09:35:00", f"{yesterday} 09:35:00",
             *stamp, cycle, 1, "verified"),
        )
        self.conn.commit()

        status = RS._is_risk_handled(self.conn, ACCOUNT, CODE, today, cycle)

        self.assertFalse(status["handled"],
                         "非风控退出类别被误判为 risk_handled")


if __name__ == "__main__":
    unittest.main()
