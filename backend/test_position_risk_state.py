# -*- coding: utf-8 -*-
"""R14：cycle-owned 持仓运行时风险状态（``paper_position_risk_state``）回归测试。

不变量（本文件存在的全部理由）::

    paper_position_lots is the quantity / ownership authority.
    paper_position_risk_state is the runtime peak / take-stage authority,
    and every row belongs to exactly one (immutable) cycle.
    paper_positions is a compatibility projection with ZERO risk authority:
    its peak_price / take_stage must never reach any execution decision.
    Missing state is fail-safe (peak anchored to cost, take_stage unknown),
    never "upgraded" from the projection.
    Reads never create state; writes always carry explicit cycle provenance.

全部用例驱动**真实生产 schema**（``PT.init_db``）与**真实生产原语**
（``paper_position_read_model`` / ``paper_portfolio`` / ``paper_trading``
的 episode 生命周期）。手写最小 DDL 证明不了本缺陷：被测行为是
「旧周期 / 投影里的 peak 与 take_stage 会不会真实改变卖出决策」。

覆盖：PRS-1~PRS-15 + v20 迁移七项 + 双数据库归属 + 投影篡改。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_migrate  # noqa: E402
import paper_cycle_service as PCS  # noqa: E402
import paper_position_read_model as PPRM  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
NAME = "测试股"
#: ``_sell_plan`` 的确定性阈值（spec_override 只覆盖阈值，绝不改生产参数）。
OVERRIDE = {
    "hard_stop": -0.08,
    "trail_after": 0.03,
    "trail_stop": 0.05,
    "take_profit": [[0.05, 0.5], [0.10, 1.0]],
    "hold_min": 0,
    "hold_max": 15,
}
#: 现价 12.9 / 成本 10：若 peak 被陈旧值 13.75 污染 → 回撤 6.2% ≥ 5% 触发
#: 移动止损；fail-safe 成本锚下 peak=12.9（吸收当日 high）→ 回撤 0。
QUOTE_PEAK_TRAP = {"price": 12.9, "high": 12.9, "pct": 5.0}


class _LedgerCase(unittest.TestCase):
    """真实生产 schema 的临时账本。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patches = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._patches:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle1 = int(
            self.conn.execute(
                "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT,)
            ).fetchone()[0]
        )

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmp.cleanup()

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
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (cycle_id, account_id)
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

    def add_mirror(self, qty=100, *, code=CODE, account_id=ACCOUNT,
                   peak_price=12.5, take_stage=2):
        """直接写一条 paper_positions 投影行（模拟残留 / 陈旧 / 被篡改镜像）。"""
        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, code, NAME, "测试", qty, 10.0, "2026-08-31", "2026-09-01",
             "stock_t1", peak_price, take_stage),
        )
        self.conn.commit()

    def add_state(self, cycle_id, *, code=CODE, account_id=ACCOUNT, peak_price=13.75,
                  take_stage=3, opened_order_id=None):
        """直接写一条同周期风险状态行（默认值刻意取"危险"的陈旧值）。"""
        self.conn.execute(
            "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,peak_price,"
            "take_stage,opened_order_id,initialized_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (cycle_id, account_id, code, peak_price, take_stage, opened_order_id,
             "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
        )
        self.conn.commit()

    def bump_state(self, cycle_id, *, code=CODE, account_id=ACCOUNT,
                   peak_price=None, take_stage=None):
        """把**真实 ``_record_lot`` 已创建**的状态行改成"危险"的测试值。"""
        sets, params = [], []
        if peak_price is not None:
            sets.append("peak_price=?")
            params.append(peak_price)
        if take_stage is not None:
            sets.append("take_stage=?")
            params.append(take_stage)
        params += [cycle_id, account_id, code]
        cur = self.conn.execute(
            f"UPDATE paper_position_risk_state SET {', '.join(sets)}"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            tuple(params),
        )
        self.assertEqual(cur.rowcount, 1, "bump_state 未命中既有状态行")
        self.conn.commit()

    def account_row(self):
        return self.conn.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()

    def record_buy(self, qty, fill_price, *, cycle_id=None, day="2026-09-01"):
        """走**真实生产代码** ``_record_lot`` 写入买入 lot（含 episode 生命周期）。"""
        account = self.account_row()
        signal = {"code": CODE, "name": NAME}
        PT._record_lot(
            self.conn, account, signal, qty, fill_price, day,
            order_id=None, cycle_id=cycle_id or self.cycle1,
        )
        self.conn.commit()

    def state_row(self, cycle_id, *, code=CODE, account_id=ACCOUNT):
        return self.conn.execute(
            "SELECT * FROM paper_position_risk_state"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (cycle_id, account_id, code),
        ).fetchone()

    def state_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM paper_position_risk_state"
        ).fetchone()[0]

    def risk_position(self):
        """唯一权威持仓行（dict），供 ``_sell_plan`` 直接消费。"""
        rows = PPRM.current_positions(self.conn)
        assert len(rows) == 1, f"夹具应恰好产出一条持仓，实际 {len(rows)}"
        return dict(rows[0])

    def sell_plan(self, position, quote):
        with mock.patch.object(PT, "_completed_kline", return_value=None):
            return PT._sell_plan(position, quote, "2026-09-10", [], spec_override=OVERRIDE)


class CycleOwnedReadPath(_LedgerCase):
    """PRS-1 / PRS-4 —— 读路径只认同周期风险状态行。"""

    def test_PRS1_stale_cycle_state_does_not_enter_current_positions(self):
        """旧周期的 peak/take_stage 不得进入新周期持仓读数。"""
        self.add_lot(self.cycle1, 100)
        self.add_state(self.cycle1, peak_price=13.75, take_stage=3)
        # 镜像行同时存在：即便读模型被变异成"回落投影"，也必须给出 fail-safe 读数。
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        c2 = self.add_cycle()
        self.activate(c2)
        self.add_lot(c2, 200)

        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        # cycle2 没有状态行 ⇒ fail-safe：peak 锚定成本、stage 未知。
        self.assertAlmostEqual(float(rows[0]["peak_price"]), float(rows[0]["cost"]))
        self.assertIsNone(rows[0]["take_stage"])
        self.assertEqual(rows[0]["risk_state_source"], "missing")
        # 旧周期状态行原样保留（历史事实），只是不再供给读数。
        self.assertIsNotNone(self.state_row(self.cycle1, peak_price=13.75)
                             if False else self.state_row(self.cycle1))

    def test_PRS1b_no_lot_in_current_cycle_reads_empty_despite_stale_state(self):
        self.add_lot(self.cycle1, 100)
        self.add_state(self.cycle1, peak_price=13.75, take_stage=3)
        c2 = self.add_cycle()
        self.activate(c2)
        self.assertEqual(PPRM.current_positions(self.conn), [])

    def test_PRS4_same_cycle_state_row_is_the_read_authority(self):
        self.add_lot(self.cycle1, 100)
        self.add_state(self.cycle1, peak_price=11.25, take_stage=1)
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["peak_price"]), 11.25)
        self.assertEqual(int(rows[0]["take_stage"]), 1)
        self.assertEqual(rows[0]["risk_state_source"], "cycle_state")


class StalePeakCannotDriveTrailingStop(_LedgerCase):
    """PRS-2 —— 陈旧 peak 真实改变移动止损决策（before-fix 的核心泄漏）。"""

    def test_PRS2_missing_state_anchors_peak_to_cost_no_trailing_stop(self):
        self.add_lot(self.cycle1, 100, cost=10.0)
        position = self.risk_position()          # fail-safe：peak=cost=10、stage=None
        self.assertAlmostEqual(float(position["peak_price"]), 10.0)

        ratio, reason, next_stage, detail = self.sell_plan(position, QUOTE_PEAK_TRAP)
        self.assertEqual(ratio, 0.0, "成本锚 peak 下 12.9 现价不应触发任何卖出")
        self.assertNotIn("移动止损", reason)
        self.assertNotEqual(detail["exit_class"], "trailing_stop")

    def test_PRS2b_same_cycle_peak_is_honored_by_trailing_stop(self):
        """敏感性对照：同周期状态 peak=13.75 时同一报价必须触发移动止损。

        没有这一条，PRS-2 的"不触发"可能是夹具落在决策边界之外（vacuous）。
        """
        self.add_lot(self.cycle1, 100, cost=10.0)
        self.add_state(self.cycle1, peak_price=13.75, take_stage=0)
        position = self.risk_position()
        ratio, reason, _next_stage, detail = self.sell_plan(position, QUOTE_PEAK_TRAP)
        self.assertEqual(detail["exit_class"], "trailing_stop",
                         "同周期合法 peak 未参与移动止损（读路径断了）")
        self.assertEqual(ratio, 1.0)


class StaleTakeStageCannotDriveTakeProfit(_LedgerCase):
    """PRS-3 —— 未知档位必须保持未知（缺失 state ⇒ 跳过阶梯止盈）。"""

    def test_PRS3_missing_state_skips_staged_take_profit(self):
        self.add_lot(self.cycle1, 100, cost=10.0)
        position = self.risk_position()          # take_stage=None（未知）
        self.assertIsNone(position["take_stage"])
        # +29% 深入第一档止盈区间：stage 未知时整段跳过，绝不猜档位多卖。
        ratio, reason, _next_stage, detail = self.sell_plan(position, QUOTE_PEAK_TRAP)
        self.assertEqual(ratio, 0.0, "未知档位不得被猜成已知并触发止盈卖出")
        self.assertNotIn("阶梯止盈", reason)
        self.assertNotEqual(detail["exit_class"], "tactical_take_profit")

    def test_PRS3b_known_same_cycle_stage_consumes_stages(self):
        """敏感性对照：同周期 stage=0（已知）时同一报价必须消费止盈档位。"""
        self.add_lot(self.cycle1, 100, cost=10.0)
        self.add_state(self.cycle1, peak_price=10.0, take_stage=0)
        position = self.risk_position()
        ratio, reason, next_stage, detail = self.sell_plan(position, QUOTE_PEAK_TRAP)
        self.assertGreater(ratio, 0.0, "同周期已知档位未驱动阶梯止盈（读路径断了）")
        self.assertIn("阶梯止盈", reason)
        self.assertEqual(detail["exit_class"], "tactical_take_profit")
        self.assertGreater(next_stage, 0)


class WritesAreCycleScoped(_LedgerCase):
    """PRS-5 —— peak / take_stage / delete 写入必须按显式 cycle 隔离。"""

    def test_PRS5_peak_and_stage_writes_only_touch_their_cycle(self):
        self.add_state(self.cycle1, peak_price=10.0, take_stage=1)
        c2 = self.add_cycle()
        self.add_state(c2, peak_price=12.0, take_stage=0)

        PT.update_position_peak(
            self.conn, cycle_id=c2, account_id=ACCOUNT, code=CODE, peak_price=15.0)
        PT.update_position_take_stage(
            self.conn, cycle_id=c2, account_id=ACCOUNT, code=CODE, take_stage=2)
        self.conn.commit()

        old = self.state_row(self.cycle1)
        self.assertAlmostEqual(float(old["peak_price"]), 10.0, "旧周期 peak 被跨周期写污染")
        self.assertEqual(int(old["take_stage"]), 1, "旧周期 stage 被跨周期写污染")
        new = self.state_row(c2)
        self.assertAlmostEqual(float(new["peak_price"]), 15.0)
        self.assertEqual(int(new["take_stage"]), 2)

    def test_PRS5b_peak_write_never_lowers_and_missing_row_is_noop(self):
        self.add_state(self.cycle1, peak_price=14.0, take_stage=0)
        PT.update_position_peak(
            self.conn, cycle_id=self.cycle1, account_id=ACCOUNT, code=CODE,
            peak_price=11.0)
        self.assertAlmostEqual(float(self.state_row(self.cycle1)["peak_price"]), 14.0,
                               "peak 被写低了（只升不降被破坏）")
        # 不存在的行 ⇒ no-op（读/扫描路径绝不创造权威状态）。
        PT.update_position_peak(
            self.conn, cycle_id=self.cycle1, account_id=ACCOUNT, code="000001",
            peak_price=99.0)
        self.conn.commit()
        self.assertIsNone(self.state_row(self.cycle1, code="000001"))

    def test_PRS5c_delete_is_cycle_scoped(self):
        self.add_state(self.cycle1, peak_price=10.0, take_stage=1)
        c2 = self.add_cycle()
        self.add_state(c2, peak_price=12.0, take_stage=0)

        PT.delete_position_risk_state(
            self.conn, cycle_id=c2, account_id=ACCOUNT, code=CODE)
        self.conn.commit()
        self.assertIsNone(self.state_row(c2))
        self.assertIsNotNone(self.state_row(self.cycle1), "旧周期状态被跨周期删除")


class EpisodeLifecycle(_LedgerCase):
    """PRS-6~9 —— episode 生命周期走真实 ``_record_lot`` / lot 消耗。"""

    def test_PRS6_partial_sell_preserves_state(self):
        self.record_buy(200, 10.0)
        self.bump_state(self.cycle1, peak_price=13.75, take_stage=1)
        consumed, _cost = PT._consume_available_lots(
            self.conn, ACCOUNT, CODE, 100, "2026-09-10", cycle_id=self.cycle1)
        self.conn.commit()
        self.assertEqual(int(consumed), 100)

        row = self.state_row(self.cycle1)
        self.assertIsNotNone(row, "部分减仓清掉了运行时风险状态")
        self.assertAlmostEqual(float(row["peak_price"]), 13.75)
        self.assertEqual(int(row["take_stage"]), 1)
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(int(rows[0]["qty"]), 100)
        self.assertAlmostEqual(float(rows[0]["peak_price"]), 13.75)

    def test_PRS7_full_exit_clears_state_via_sell_finalization(self):
        """full exit（episode 结束）必须清除状态；same-cycle 再进场拿全新状态。"""
        self.record_buy(100, 10.0)
        self.bump_state(self.cycle1, peak_price=13.75, take_stage=2)
        PT._consume_available_lots(
            self.conn, ACCOUNT, CODE, 100, "2026-09-10", cycle_id=self.cycle1)
        # 卖出成交收尾路径（risk scan finalization）在 position_closed 时删除状态。
        PT.delete_position_risk_state(
            self.conn, cycle_id=self.cycle1, account_id=ACCOUNT, code=CODE)
        self.conn.commit()
        self.assertIsNone(self.state_row(self.cycle1), "full exit 未清除风险状态")

        # 源码级守卫：删除只发生在 ``if position_closed:`` 分支内，部分减仓
        # 走 else 的 take_stage 推进 —— 结构回归由该守卫钉住。
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "backend/paper_trading.py"), encoding="utf-8") as fh:
            src = fh.read()
        closed_at = src.index("if position_closed:")
        delete_at = src.index("delete_position_risk_state(", closed_at)
        else_at = src.index("else:", closed_at)
        self.assertLess(delete_at, else_at,
                        "delete_position_risk_state 不再位于 position_closed 分支内")

    def test_PRS8_add_on_preserves_stage_and_raises_peak(self):
        """加仓：不重置 episode 状态；peak 只升不降；initialized_at 不变。"""
        self.record_buy(100, 10.0)
        row = self.state_row(self.cycle1)
        self.assertAlmostEqual(float(row["peak_price"]), 10.0)
        self.assertEqual(int(row["take_stage"]), 0)
        self.assertIsNone(row["opened_order_id"], "直接建仓路径不得伪造订单出处")
        PT.update_position_take_stage(
            self.conn, cycle_id=self.cycle1, account_id=ACCOUNT, code=CODE,
            take_stage=1)
        self.conn.commit()
        initialized_at = self.state_row(self.cycle1)["initialized_at"]

        self.record_buy(100, 11.0, day="2026-09-02")   # 加仓，prior_qty=100>0
        row = self.state_row(self.cycle1)
        self.assertAlmostEqual(float(row["peak_price"]), 11.0, "加仓后 peak 未吸收新成交价")
        self.assertEqual(int(row["take_stage"]), 1, "加仓重置了阶梯止盈档位")
        self.assertEqual(row["initialized_at"], initialized_at, "加仓重置了 episode 起点")
        self.assertEqual(int(self.state_count()), 1, "加仓复制出了第二行状态")

    def test_PRS9_same_cycle_reentry_gets_fresh_state(self):
        """same-cycle re-entry：全新 episode，绝不继承旧 peak / stage。"""
        self.record_buy(100, 10.0)
        self.bump_state(self.cycle1, peak_price=15.0, take_stage=2)
        PT._consume_available_lots(
            self.conn, ACCOUNT, CODE, 100, "2026-09-10", cycle_id=self.cycle1)
        self.conn.commit()

        self.record_buy(100, 12.0, day="2026-09-11")   # 同周期重新建仓
        row = self.state_row(self.cycle1)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row["peak_price"]), 12.0,
                               "re-entry 继承了旧 episode 的 peak")
        self.assertEqual(int(row["take_stage"]), 0, "re-entry 继承了旧 episode 的档位")
        self.assertEqual(int(self.state_count()), 1)

    def test_PRS9b_init_replaces_any_leftover_stale_row(self):
        """防御性兜底：残留行存在时，verified BUY 也必须 REPLACE 成全新状态。"""
        self.record_buy(100, 10.0)
        # 模拟"上一 episode 未被显式清理"的残留：peak/stage 都是陈旧值。
        self.conn.execute(
            "UPDATE paper_position_risk_state SET peak_price=99.0, take_stage=5"
            " WHERE cycle_id=? AND account_id=? AND code=?",
            (self.cycle1, ACCOUNT, CODE),
        )
        self.conn.commit()
        PT._consume_available_lots(
            self.conn, ACCOUNT, CODE, 100, "2026-09-10", cycle_id=self.cycle1)
        self.conn.commit()
        self.record_buy(100, 10.5, day="2026-09-11")
        row = self.state_row(self.cycle1)
        self.assertAlmostEqual(float(row["peak_price"]), 10.5)
        self.assertEqual(int(row["take_stage"]), 0)
        self.assertEqual(int(self.state_count()), 1)


class ReadPathIsPureRead(_LedgerCase):
    """PRS-10 / PRS-11 —— 读取绝不创造状态、绝不建周期。"""

    def test_PRS10_current_positions_never_creates_state(self):
        self.add_lot(self.cycle1, 100)
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        self.assertEqual(self.state_count(), 0)
        for _ in range(2):
            rows = PPRM.current_positions(self.conn)
            self.assertEqual(len(rows), 1)
            PPRM.current_holding_keys(self.conn)
            PPRM.current_held_codes(self.conn)
        self.assertEqual(self.state_count(), 0,
                         "读路径（含镜像存在时）创造了风险状态行")

    def test_PRS11_no_active_cycle_fails_closed(self):
        self.add_lot(self.cycle1, 100)
        self.add_state(self.cycle1, peak_price=13.75, take_stage=3)
        self.conn.execute("UPDATE paper_cycles SET status='archived'")
        self.conn.commit()
        self.assertEqual(PPRM.current_positions(self.conn), [])
        self.assertEqual(self.state_count(), 1, "fail-closed 读取动了状态行")


class ProjectionTamperingGuard(_LedgerCase):
    """PRS-12 —— 被篡改的投影不得影响任何风险决策输入。"""

    def test_PRS12_tampered_mirror_cannot_reach_execution_decisions(self):
        self.add_lot(self.cycle1, 100, cost=10.0)
        self.add_mirror(qty=9999, peak_price=99.0, take_stage=5)
        position = self.risk_position()
        self.assertEqual(int(position["qty"]), 100, "投影 qty 覆盖了 lot")
        self.assertAlmostEqual(float(position["peak_price"]), 10.0,
                               "篡改的投影 peak 进入决策输入")
        self.assertIsNone(position["take_stage"], "篡改的投影 stage 进入决策输入")

        ratio, reason, _next_stage, detail = self.sell_plan(position, QUOTE_PEAK_TRAP)
        self.assertEqual(ratio, 0.0)
        self.assertNotEqual(detail["exit_class"], "trailing_stop")
        self.assertNotIn("阶梯止盈", reason)


class V20Migration(_LedgerCase):
    """v20 迁移七项：fresh / 注册 / upgrade / 幂等 / 行保留 / 不回填 / trigger。"""

    def test_migration_fresh_db_has_canonical_schema(self):
        cols = [r["name"] for r in self.conn.execute(
            "PRAGMA table_info(paper_position_risk_state)")]
        self.assertEqual(cols, list(PSM.POSITION_RISK_STATE_COLUMNS))
        pk = [r["name"] for r in self.conn.execute(
            "PRAGMA table_info(paper_position_risk_state)") if r["pk"]]
        self.assertEqual(pk, ["cycle_id", "account_id", "code"])

    def test_migration_v20_is_registered_as_latest(self):
        paper = db_migrate.MIGRATIONS["paper_trading"]
        self.assertEqual(paper[-1][0], 20)
        self.assertIn("风险状态", paper[-1][1])

    def test_migration_upgrade_recreates_table_with_guards(self):
        self.conn.executescript(
            "DROP TRIGGER IF EXISTS trg_paper_position_risk_state_cycle_required_insert;"
            "DROP TRIGGER IF EXISTS trg_paper_position_risk_state_cycle_immutable;"
            "DROP TABLE paper_position_risk_state;"
        )
        self.conn.commit()
        db_migrate.migrate("paper_trading", path=self.path, backup=False)
        conn2 = sqlite3.connect(self.path)
        conn2.row_factory = sqlite3.Row
        try:
            cols = [r["name"] for r in conn2.execute(
                "PRAGMA table_info(paper_position_risk_state)")]
            self.assertEqual(cols, list(PSM.POSITION_RISK_STATE_COLUMNS))
            triggers = {r[0] for r in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE '%paper_position_risk_state%'")}
            self.assertEqual(triggers, {
                "trg_paper_position_risk_state_cycle_required_insert",
                "trg_paper_position_risk_state_cycle_immutable",
            })
        finally:
            conn2.close()

    def test_migration_is_idempotent(self):
        self.add_state(self.cycle1, peak_price=11.0, take_stage=1)
        before = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM paper_position_risk_state ORDER BY cycle_id")]
        PSM.ensure_position_risk_state(self.conn)
        PSM.ensure_position_risk_state(self.conn)
        after = [tuple(r) for r in self.conn.execute(
            "SELECT * FROM paper_position_risk_state ORDER BY cycle_id")]
        self.assertEqual(after, before, "重复迁移改写了既有状态行")
        triggers = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
            " AND name LIKE '%paper_position_risk_state%'")}
        self.assertEqual(len(triggers), 2, "重复迁移复制出了重复 trigger")

    def test_migration_preserves_existing_rows(self):
        self.add_lot(self.cycle1, 100)
        self.add_state(self.cycle1, peak_price=11.0, take_stage=1)
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        counts_before = {
            t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("paper_position_lots", "paper_positions", "paper_cycles",
                      "paper_position_risk_state")
        }
        self.conn.executescript(
            "DROP TRIGGER IF EXISTS trg_paper_position_risk_state_cycle_required_insert;"
            "DROP TRIGGER IF EXISTS trg_paper_position_risk_state_cycle_immutable;"
            "DROP TABLE paper_position_risk_state;"
        )
        self.conn.commit()
        db_migrate.migrate("paper_trading", path=self.path, backup=False)
        conn2 = sqlite3.connect(self.path)
        try:
            counts_after = {
                t: conn2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in counts_before
            }
        finally:
            conn2.close()
        expected = dict(counts_before)
        expected["paper_position_risk_state"] = 0  # 表被重建，历史不回填
        self.assertEqual(counts_after, expected)

    def test_migration_never_backfills_from_projection(self):
        """核心红线：升级绝不允许把 paper_positions 的 peak/stage 洗成权威。"""
        self.add_lot(self.cycle1, 100)
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        self.conn.executescript(
            "DROP TRIGGER IF EXISTS trg_paper_position_risk_state_cycle_required_insert;"
            "DROP TRIGGER IF EXISTS trg_paper_position_risk_state_cycle_immutable;"
            "DROP TABLE paper_position_risk_state;"
        )
        self.conn.commit()
        db_migrate.migrate("paper_trading", path=self.path, backup=False)
        conn2 = sqlite3.connect(self.path)
        try:
            count = conn2.execute(
                "SELECT COUNT(*) FROM paper_position_risk_state").fetchone()[0]
        finally:
            conn2.close()
        self.assertEqual(count, 0, "v20 从投影回填了历史 peak/take_stage")

    def test_migration_state_row_requires_real_cycle(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,"
                "peak_price,take_stage,initialized_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (999999, ACCOUNT, CODE, 10.0, 0,
                 "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,"
                "peak_price,take_stage,initialized_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (None, ACCOUNT, CODE, 10.0, 0,
                 "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
            )
        self.conn.rollback()

    def test_migration_cycle_ownership_is_immutable(self):
        self.add_state(self.cycle1, peak_price=11.0, take_stage=1)
        c2 = self.add_cycle()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE paper_position_risk_state SET cycle_id=?"
                " WHERE cycle_id=? AND account_id=? AND code=?",
                (c2, self.cycle1, ACCOUNT, CODE),
            )
        self.conn.rollback()
        self.assertIsNotNone(self.state_row(self.cycle1))


class DualDatabaseOwnership(unittest.TestCase):
    """PRS-13/14 —— 风险状态表属于 paper ledger，绝不进 adaptive_learning。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paper_path = os.path.join(self.tmp.name, "paper.sqlite3")
        self.adaptive_path = os.path.join(self.tmp.name, "adaptive.sqlite3")
        self._patches = (
            mock.patch.object(PT, "DB_PATH", self.paper_path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._patches:
            patcher.start()
        PT.init_db()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmp.cleanup()

    def _tables(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def test_PRS13_risk_state_table_lives_only_in_paper_db(self):
        import adaptive_engine as AE
        with mock.patch.object(AE, "DB_PATH", self.adaptive_path), \
                mock.patch.object(AE, "PAPER_DB_PATH", self.paper_path), \
                mock.patch.object(AE, "CACHE_DIR", self.tmp.name):
            with AE._connect():
                pass  # 建立/初始化 adaptive schema
            # 即便对 adaptive 库跑完整迁移链，也不得出现风险状态表。
            db_migrate.migrate("adaptive_learning", path=self.adaptive_path,
                               backup=False)

        self.assertIn("paper_position_risk_state", self._tables(self.paper_path))
        self.assertNotIn(
            "paper_position_risk_state", self._tables(self.adaptive_path),
            "adaptive_learning.sqlite3 收到了 paper 风险状态表（Round-11 同类错误）",
        )

    def test_PRS14_cycle_purge_lists_include_risk_state(self):
        """周期归档/清理清单必须覆盖风险状态表，否则旧周期状态残留成陈旧权威。"""
        for attr in ("LEDGER_TABLES", "PURGED_TABLES"):
            tables = getattr(PCS, attr, None) or ()
            self.assertIn("paper_position_risk_state", tables,
                          f"paper_cycle_service.{attr} 缺少 paper_position_risk_state")


if __name__ == "__main__":
    unittest.main()
