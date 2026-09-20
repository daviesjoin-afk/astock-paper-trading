# -*- coding: utf-8 -*-
"""Round-10：当前持仓消费者必须走权威 lot（§13–§20）。

不变量（本文件存在的全部理由）::

    paper_position_lots is the current executable position authority.
    paper_positions is never evidence that a position is currently held.
    Current-position consumers must be cycle-scoped and lot-backed.
    A stale mirror may enrich metadata only for an already-proven lot position.
    No stale mirror may affect current candidate exclusion, learning holding
    classification, portfolio shadow input, risk exposure, or execution authority.
    Current-position reads are read-only.

全部用例驱动**真实生产 schema**（``PT.init_db``）与**真实生产原语**
（``paper_position_read_model`` / ``news_learning.candidate_pool`` /
``rebalance_scanner`` / ``adaptive_engine``）。手写最小 DDL 证明不了本轮的缺陷：
被测行为是「一次普通读取会不会把投影当成当前持仓」，由生产代码持有。
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

import paper_position_read_model as PPRM  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
OTHER_ACCOUNT = "trend_pullback"
CODE = "600519"
CODE_B = "000001"
NAME = "测试股"


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

    def add_mirror(self, qty=100, *, code=CODE, account_id=ACCOUNT, entry_date="2026-08-31",
                   peak_price=12.5, take_stage=2):
        """直接写一条 paper_positions 投影行（模拟残留 / 陈旧镜像）。"""
        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, code, NAME, "测试", qty, 10.0, entry_date, "2026-09-01",
             "stock_t1", peak_price, take_stage),
        )
        self.conn.commit()

    def lot_fingerprint(self):
        return [
            (r["id"], r["cycle_id"], r["remaining_qty"], r["source_order_id"])
            for r in self.conn.execute(
                "SELECT id,cycle_id,remaining_qty,source_order_id FROM paper_position_lots ORDER BY id"
            )
        ]

    def stale_mirror_scenario(self):
        """§12 主 adversarial fixture：cycle N 有权威 lot + 镜像，cycle N+1 无 lot。"""
        self.add_lot(self.cycle1, 100)
        PT._sync_positions(self.conn)
        self.conn.commit()
        c2 = self.add_cycle()
        self.activate(c2)
        self.assertEqual(len(self.conn.execute(
            "SELECT 1 FROM paper_position_lots WHERE cycle_id=?", (c2,)).fetchall()), 0)
        self.assertEqual(len(self.conn.execute(
            "SELECT 1 FROM paper_positions").fetchall()), 1, "镜像应仍存在（陈旧）")
        return c2


class UnifiedReader(_LedgerCase):
    """§3/§4/§12 —— 唯一权威只读 reader。"""

    def test_reader_is_cycle_scoped_and_lot_backed(self):
        self.stale_mirror_scenario()
        self.assertEqual(PPRM.current_positions(self.conn), [], "陈旧镜像被当成当前持仓")
        self.assertEqual(PPRM.current_held_codes(self.conn), set())
        self.assertEqual(PPRM.current_holding_keys(self.conn), set())

    def test_reader_sees_current_cycle_lot(self):
        c2 = self.stale_mirror_scenario()
        self.add_lot(c2, 200)
        codes = PPRM.current_held_codes(self.conn)
        self.assertEqual(codes, {CODE})
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["qty"]), 200, "数量必须来自 lot")

    def test_paper_trading_position_rows_uses_same_reader(self):
        """§4：paper_trading 与 reader 必须给出同一答案（同一实现）。"""
        self.stale_mirror_scenario()
        self.assertEqual(PT._position_rows(self.conn), PPRM.current_positions(self.conn))


class NoActiveCycleFailsClosed(_LedgerCase):
    """§17 —— 无 active cycle ⇒ []，绝不建周期、不回落投影。"""

    def test_no_active_cycle_returns_empty_and_creates_nothing(self):
        self.conn.execute("UPDATE paper_cycles SET status='archived'")
        self.add_mirror(qty=100)          # 有投影行，但没有 active cycle
        self.conn.commit()
        before_cycles = self.conn.execute("SELECT COUNT(*) FROM paper_cycles").fetchone()[0]
        before_lots = self.lot_fingerprint()

        self.assertEqual(PPRM.current_positions(self.conn), [])
        self.assertEqual(PPRM.current_held_codes(self.conn), set())

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM paper_cycles").fetchone()[0],
            before_cycles, "读取创建了周期",
        )
        self.assertEqual(self.lot_fingerprint(), before_lots, "读取写入了 lot")

    def test_missing_cycles_table_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.assertIsNone(PPRM.active_cycle_id(conn))


class ExplicitCyclePositionReadTests(_LedgerCase):
    """R16：``positions_for_cycle`` 是只读指定周期、绝不 fallback 的持仓读取。

    这是风险扫描在外部 I/O 之后仍需精确回到**认领过的那个周期**的承重接口：
    一条从 cycle A 开始的扫描必须能读到 A 的持仓，哪怕 A 已经 paused /
    归档、B 已经是 active —— 它绝不能偷偷 fallback 成"现在 active 的周期"。

    注意 active_cycle_id 的判据是 ``status IN ('draft','running','paused')``
    （**不含** archived）取最大 id；paused 且 id 更大的周期仍算 active。
    """

    def _active_cycle_id(self):
        return int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1").fetchone()[0])

    def test_explicit_cycle_read_returns_only_requested_cycle(self):
        running = int(self.cycle1)          # 初始 running 周期（id 最小）
        old = self.add_cycle(status="paused")  # 更高 id 的 paused 周期 → active
        self.add_lot(running, 100)
        self.add_lot(old, 200)

        exp = PPRM.positions_for_cycle(self.conn, running)
        self.assertEqual(len(exp), 1)
        self.assertEqual(int(exp[0]["qty"]), 100, "显式读取串进了别的周期")

        # 显式读 paused 旧周期：即便它已不是 active，也要精确读到它的持仓
        old_rows = PPRM.positions_for_cycle(self.conn, old)
        self.assertEqual(len(old_rows), 1)
        self.assertEqual(int(old_rows[0]["qty"]), 200)

        # current_positions 只读 active（= paused 旧周期，因其 id 更大）
        cur = PPRM.current_positions(self.conn)
        self.assertEqual(len(cur), 1)
        self.assertEqual(int(cur[0]["qty"]), 200)

    def test_explicit_cycle_does_not_fallback_to_current(self):
        old = self.add_cycle(status="paused", started_at="2026-09-18 09:30:00")
        self.add_lot(old, 300)
        # 把 old 归档，再另起一个 running 周期成为当前 active
        self.conn.execute("UPDATE paper_cycles SET status='archived' WHERE id=?", (int(old),))
        active = self.add_cycle(status="running", started_at="2026-09-20 09:30:00")
        self.assertNotEqual(int(active), int(old))
        self.conn.commit()

        # active 已经是新周期，但显式读 old（已归档）仍应精确返回它的持仓
        rows = PPRM.positions_for_cycle(self.conn, old)
        self.assertEqual(
            len(rows), 1,
            "explicit cycle 读取 fallback 成了当前 active cycle（应该精确读旧周期）",
        )
        self.assertEqual(int(rows[0]["qty"]), 300)

    def test_positions_for_cycle_rejects_none(self):
        self.assertEqual(PPRM.positions_for_cycle(self.conn, None), [])

    def test_positions_for_cycle_missing_cycle_is_empty(self):
        self.assertEqual(PPRM.positions_for_cycle(self.conn, 99999), [])

    def test_current_positions_delegates_to_same_aggregation(self):
        """current_positions 与 positions_for_cycle 共用同一套聚合（只有周期来源不同）。"""
        cid = self._active_cycle_id()
        self.add_lot(cid, 150, code=CODE_B)
        cur = PPRM.current_positions(self.conn)
        exp = PPRM.positions_for_cycle(self.conn, cid)
        self.assertEqual(len(cur), len(exp))
        # 同一周期下两者逐行同构
        by_code_c = {p["code"]: p for p in cur}
        by_code_e = {p["code"]: p for p in exp}
        for code in by_code_e:
            self.assertEqual(int(by_code_c[code]["qty"]), int(by_code_e[code]["qty"]))


class AccountIsolation(_LedgerCase):
    """§18 —— account 过滤不得串数据。"""

    def test_account_filter_returns_only_that_account(self):
        self.add_lot(self.cycle1, 100, code=CODE, account_id=ACCOUNT)
        self.add_lot(self.cycle1, 300, code=CODE_B, account_id=OTHER_ACCOUNT)
        self.conn.commit()

        a_keys = PPRM.current_holding_keys(self.conn, account_id=ACCOUNT)
        self.assertEqual(a_keys, {(ACCOUNT, CODE)})
        a_rows = PPRM.current_positions(self.conn, account_id=ACCOUNT)
        self.assertEqual({r["account_id"] for r in a_rows}, {ACCOUNT})

    def test_mirror_from_other_account_does_not_leak(self):
        self.add_lot(self.cycle1, 100, code=CODE, account_id=ACCOUNT)
        self.add_mirror(qty=500, code=CODE_B, account_id=OTHER_ACCOUNT)
        self.conn.commit()

        rows = PPRM.current_positions(self.conn, account_id=ACCOUNT)
        self.assertEqual([r["code"] for r in rows], [CODE])


class CycleIsolation(_LedgerCase):
    """§19 —— 只返回当前周期，旧周期 lot 与镜像都不得混入。"""

    def test_only_current_cycle_positions(self):
        c2 = self.stale_mirror_scenario()
        self.add_lot(c2, 200, code=CODE_B)
        rows = PPRM.current_positions(self.conn)
        self.assertEqual([r["code"] for r in rows], [CODE_B], "旧周期持仓混入")
        self.assertEqual(PPRM.current_held_codes(self.conn), {CODE_B})


class ReadOnlyInvariant(_LedgerCase):
    """§16 —— 所有 current-position 读取必须零写入。"""

    def _snapshot(self):
        return {
            "lots": self.lot_fingerprint(),
            "mirror": [tuple(r) for r in self.conn.execute(
                "SELECT account_id,code,qty,cost FROM paper_positions ORDER BY account_id,code")],
            "orders": self.conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0],
            "fills": self.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
            "cash": [tuple(r) for r in self.conn.execute(
                "SELECT id,cash FROM paper_accounts ORDER BY id")],
            "cycles": [tuple(r) for r in self.conn.execute(
                "SELECT id,status FROM paper_cycles ORDER BY id")],
        }

    def test_reader_writes_nothing(self):
        self.stale_mirror_scenario()
        before = self._snapshot()
        PPRM.current_positions(self.conn)
        PPRM.current_held_codes(self.conn)
        PPRM.current_holding_keys(self.conn)
        PPRM.current_holding_rows(self.conn)
        self.assertEqual(self._snapshot(), before)

    def test_reader_with_authoritative_lot_writes_nothing(self):
        self.add_lot(self.cycle1, 100)
        self.add_mirror(qty=999)
        self.conn.commit()
        before = self._snapshot()
        PPRM.current_positions(self.conn)
        PPRM.current_holding_rows(self.conn)
        self.assertEqual(self._snapshot(), before)


class LegacyMetadataCompatibility(_LedgerCase):
    """§20 / R14 —— 镜像不能覆盖权威数量/成本/日期，也**不再供给**风险状态。"""

    def test_stale_mirror_peak_and_take_stage_have_zero_execution_authority(self):
        """R14：peak_price / take_stage 的权威在 cycle-owned 风险状态表。

        镜像里的 13.75 / 3 不得进入持仓读数：缺失状态走显式 fail-safe
        （peak 锚定成本、take_stage=None 未知），执行判定绝不读投影。
        """
        self.add_lot(self.cycle1, 100)
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        self.conn.commit()
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertNotAlmostEqual(float(rows[0]["peak_price"]), 13.75)
        self.assertAlmostEqual(float(rows[0]["peak_price"]), float(rows[0]["cost"]))
        self.assertIsNone(rows[0]["take_stage"], "未知档位不得被镜像冒充成已知")
        self.assertEqual(rows[0]["risk_state_source"], "missing")

    def test_cycle_state_row_is_the_only_peak_stage_authority(self):
        """R14：同周期风险状态行存在时，读数来自它而不是镜像。"""
        self.add_lot(self.cycle1, 100)
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        self.conn.execute(
            "INSERT INTO paper_position_risk_state(cycle_id,account_id,code,peak_price,"
            "take_stage,opened_order_id,initialized_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.cycle1, ACCOUNT, CODE, 11.25, 1, None,
             "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
        )
        self.conn.commit()
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["peak_price"]), 11.25)
        self.assertEqual(int(rows[0]["take_stage"]), 1)
        self.assertEqual(rows[0]["risk_state_source"], "cycle_state")

    def test_mirror_qty_cost_entry_date_cannot_override_lot(self):
        self.add_lot(self.cycle1, 200, cost=7.0, acquired_at="2026-07-01 10:00:00")
        self.add_mirror(qty=999, entry_date="2020-01-01")
        self.conn.commit()
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["qty"]), 200, "镜像数量覆盖了 lot")
        self.assertAlmostEqual(float(rows[0]["settlement_cost"]), 7.0, places=6)
        self.assertEqual(str(rows[0]["entry_date"])[:10], "2026-07-01", "镜像日期覆盖了 lot")


class NewsLearningHoldingTier(_LedgerCase):
    """§13 PC1–PC3 —— holding tier 必须来自权威 lot。"""

    def setUp(self):
        super().setUp()
        import news_learning as NL
        self.NL = NL
        self._nl_patch = mock.patch.object(NL, "PAPER_DB_PATH", self.path)
        self._nl_patch.start()
        self.addCleanup(self._nl_patch.stop)

    def _pool(self):
        return self.NL.candidate_pool()

    def test_PC1_stale_mirror_is_not_holding(self):
        self.stale_mirror_scenario()
        holdings = [r for r in self._pool() if r.get("pool_tier") == "holding"]
        self.assertEqual(holdings, [], "陈旧镜像被标成 holding tier")

    def test_PC2_current_cycle_lot_is_holding(self):
        c2 = self.stale_mirror_scenario()
        self.add_lot(c2, 200)
        holdings = [r for r in self._pool() if r.get("pool_tier") == "holding"]
        self.assertEqual([r["code"] for r in holdings], [CODE])

    def test_PC3_lot_quantity_is_the_current_truth(self):
        self.add_lot(self.cycle1, 200)
        self.add_mirror(qty=1000)
        self.conn.commit()
        keys = PPRM.current_holding_keys(self.conn)
        self.assertEqual(keys, {(ACCOUNT, CODE)})
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(int(rows[0]["qty"]), 200, "数量被镜像 1000 覆盖")


class RebalanceHeldCodes(_LedgerCase):
    """§14 PC4–PC5 —— 替补候选排除必须用权威 holdings。

    承重测试驱动**真实路径** ``find_replacement_candidates``，而不是只调 reader
    原语：只调原语时，把 rebalance_scanner 改回读 mirror 的变异不会被抓到
    （非空性实测暴露过这一点）。
    """

    def _factor_table(self):
        import pandas as pd
        return pd.DataFrame(
            [{"name": "候选A", "score": 99.0}, {"name": "候选B", "score": 98.0}],
            index=[CODE, "601398"],
        )

    def _candidate_codes(self, sold_code="000002"):
        import rebalance_scanner as RS
        rows = RS.find_replacement_candidates(
            self.conn, ACCOUNT, sold_code, {}, factor_table=self._factor_table()
        )
        return {str(r.get("code")) for r in rows}

    def test_PC4_stale_mirror_does_not_exclude(self):
        """陈旧镜像里的代码不得被排除在替补候选之外（它并不被持有）。"""
        self.stale_mirror_scenario()
        self.assertIn(CODE, self._candidate_codes(),
                      "陈旧镜像把代码错误排除在替补候选外")

    def test_PC5_current_lot_is_excluded(self):
        """当前周期确实持有 ⇒ 必须被排除。"""
        self.add_lot(self.cycle1, 100)
        self.conn.commit()
        self.assertNotIn(CODE, self._candidate_codes(),
                         "当前周期持仓未被排除出替补候选")

    def test_rebalance_scanner_uses_authoritative_reader(self):
        """源码级：rebalance_scanner 不再直接读 paper_positions。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "backend/rebalance_scanner.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("FROM paper_positions", src,
                         "rebalance_scanner 仍直接读 paper_positions 投影")


class AdaptiveShadowPortfolio(_LedgerCase):
    """§15 PC6–PC7 —— 影子组合必须看到正确的当前事实。

    承重测试驱动**真实路径** ``_portfolio_shadow_arbitration``（影子/advisory，
    无执行 authority），而不是只调 reader 原语。

    影子路径里持仓的作用有两处：① ``_portfolio_shadow_risk(positions)`` 的组合
    风险指标；② 对候选信号的 ``held_codes`` 去重惩罚。因此「权威持仓是否被看到」
    要用这两者断言 —— 而不是断言持仓出现在 ``candidates`` 里（那来自
    ``paper_signals``，持仓从不作为 candidate 输出）。
    """

    def _shadow(self):
        import adaptive_engine as AE
        with mock.patch.object(AE, "PAPER_DB_PATH", self.path):
            return AE._portfolio_shadow_arbitration()

    def test_PC6_stale_mirror_excluded_from_shadow_portfolio(self):
        self.stale_mirror_scenario()
        result = self._shadow()
        # 持仓在影子输出里的可观测面：held_slots 计数 + risk_metrics 的持仓聚合。
        self.assertEqual(int(result.get("held_slots") or 0), 0,
                         "陈旧镜像被算进了影子组合持仓")
        self.assertEqual(int((result.get("risk_metrics") or {}).get("positions", {}).get("total") or 0), 0,
                         "陈旧镜像进入了影子风险持仓聚合")
        self.assertEqual(str(result.get("mode")), "shadow",
                         "影子模式不得因本改动获得执行 authority")

    def test_PC7_authoritative_lot_is_seen_by_shadow(self):
        """权威持仓必须被影子路径看到（作为组合风险输入）。"""
        self.add_lot(self.cycle1, 100)
        self.conn.commit()
        result = self._shadow()
        self.assertEqual(int(result.get("held_slots") or 0), 1,
                         "权威持仓未被影子组合看到")
        self.assertEqual(int((result.get("risk_metrics") or {}).get("positions", {}).get("total") or 0), 1,
                         "权威持仓未进入影子风险持仓聚合")
        self.assertEqual(str(result.get("mode")), "shadow")

    def test_PC7b_shadow_duplicate_penalty_follows_authoritative_holdings(self):
        """候选信号的去重惩罚必须基于权威持仓，而不是陈旧镜像。"""
        import adaptive_engine as AE
        import strategy_registry as registry
        today = dt.datetime.now(AE.TZ).date().isoformat()
        # 用仓库既有的策略版本戳机制写信号：paper_signals 有不可绕过的
        # strategy stamp 触发器，手写 INSERT 会被 fail closed 拒绝。
        strategy_id, version, checksum = registry.stamp_for_account(self.conn, ACCOUNT)
        self.conn.execute(
            "INSERT INTO paper_signals(account_id,code,name,status,intended_date,"
            "signal_date,t_score,rank_score,payload,created_at,"
            "strategy_id,strategy_version,strategy_checksum) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, NAME, "pending", today, today, 60.0, 60.0, "{}", today,
             strategy_id, version, checksum),
        )
        self.conn.commit()

        # 只有陈旧镜像（不在当前周期）⇒ 不应触发同股惩罚
        self.stale_mirror_scenario()
        stale = self._shadow()
        stale_row = next((r for r in stale.get("candidates") or [] if r["code"] == CODE), None)
        self.assertIsNotNone(stale_row, "夹具未产出候选")
        self.assertEqual(stale_row["penalties"]["duplicate"], 0.0,
                         "陈旧镜像触发了同股惩罚")

        # 当前周期放入权威 lot ⇒ 应触发同股惩罚
        active = int(self.conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.add_lot(active, 100)
        held = self._shadow()
        held_row = next((r for r in held.get("candidates") or [] if r["code"] == CODE), None)
        self.assertIsNotNone(held_row, "夹具未产出候选")
        self.assertGreater(held_row["penalties"]["duplicate"], 0.0,
                           "权威持仓未触发同股惩罚")

    def test_adaptive_engine_uses_authoritative_reader(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "backend/adaptive_engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("FROM paper_positions", src,
                         "adaptive_engine 仍直接读 paper_positions 投影")


class NewsLearningSourceGuard(unittest.TestCase):
    """§13 源码级：news_learning 不得把投影当 holding 来源。"""

    def test_news_learning_does_not_read_paper_positions_for_holding(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "backend/news_learning.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("FROM paper_positions WHERE qty>0", src,
                         "news_learning 仍用投影的 qty>0 当 holding 真相")


class ProjectionContractGuard(unittest.TestCase):
    """§11 / Round-11 §19–§20 —— ``paper_positions`` 不得作为 current-position authority。

    Round-10 的白名单是**模块级**的：``"backend/api_adaptive.py": "display read model"``
    把整个模块豁免成"展示用途"。但同一个模块里 ``/rebalance/scan`` 是
    **decision-adjacent 写路径** —— 它会据持仓生成真实调仓计划。同文件里存在一个
    展示函数，不能成为整个文件获豁免的理由。

    因此本守卫改成 **函数/查询粒度**：每一条豁免必须指明「哪个文件的哪个函数」，
    并给出「为什么这个读取不是 current-position authority」。

    ``api_adaptive.py`` 已**从白名单整体移除**：Round-11 修完后它不再有任何直接
    ``FROM paper_positions``，所以不需要豁免（行为证据见
    ``test_rebalance_db_ownership.py``）。

    文档字符串里的 ``SELECT ... FROM paper_positions`` 是**散文**不是查询，由
    ``_projection_reads`` 显式跳过 —— 这样就不必为注释/文档字符串留任何豁免。
    """

    #: 允许直接读取 ``paper_positions`` 的**函数**（显式白名单，函数粒度）。
    #: 键 = ``backend/<file>.py::<function>``；``<module>`` 表示模块级语句。
    #: 每个条目都是「为什么这个读取不是 current-position authority」。
    ALLOWED_FUNCTIONS = {
        # 投影的唯一写者：由 lot 聚合后重建镜像（DELETE 旧行再写）。
        "backend/paper_trading.py::_sync_positions": "projection writer",
        # 展示兜底：只取 name 用于风险审计展示，不参与任何持仓判定。
        "backend/paper_trading.py::risk_audit": "display-only name fallback",
        # 一致性自检：把投影与 lot 对账（发现不一致，不产生持仓）。
        "backend/paper_replay_regression.py::validate": "projection-vs-lot consistency check",
        # 开发/测试种子数据。
        "backend/demo_seed.py::_drop_position": "dev/test seed teardown",
        # 展示 / 只读面板 / AI 上下文（均非决策路径）。
        "backend/deepseek_advisor.py::collect_evidence": "display/diagnostics count",
        "backend/deepseek_research.py::_pnl_evidence": "display/research aggregate",
        "backend/deepseek_research.py::_event_evidence": "display/research aggregate",
        # 历史符号发现（明确非 holding）。
        "backend/news_learning.py::_paper_codes":
            "recent symbol discovery only (documented non-holding)",
    }

    #: 曾经存在、现已不需要豁免的模块 —— 再被加回白名单即失败。
    #: 这是 §19 的承重断言：decision-adjacent 模块不得整体获豁免。
    MODULES_THAT_MUST_NOT_BE_WHITELISTED = ("backend/api_adaptive.py",)

    @staticmethod
    def _projection_reads(path):
        """返回 ``[(function_name, lineno), ...]``：真实的投影查询所在函数。

        刻意用 ``ast`` 解析而不是字符串匹配：
        * **跳过文档字符串** —— ``_position_rows`` 的 docstring 里写着
          ``SELECT ... FROM paper_positions``，那是解释文字，不是查询；
        * 注释天然不进 AST，因此也不会被误报。
        """
        import ast

        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if "paper_positions" not in src:
            return []
        tree = ast.parse(src)

        # 收集所有文档字符串节点，稍后跳过。
        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstring_nodes.add(id(body[0].value))

        found = []

        def walk(node, function):
            for child in ast.iter_child_nodes(node):
                child_function = function
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_function = child.name
                if isinstance(child, ast.Constant) and isinstance(child.value, str) \
                        and id(child) not in docstring_nodes:
                    text = child.value.upper()
                    if "PAPER_POSITIONS" in text and ("FROM " in text or "JOIN " in text):
                        found.append((child_function or "<module>", child.lineno))
                walk(child, child_function)

        walk(tree, None)
        return found

    def _scan_backend(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        backend = os.path.join(root, "backend")
        offenders = []
        for name in sorted(os.listdir(backend)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            rel = f"backend/{name}"
            for function, lineno in self._projection_reads(os.path.join(backend, name)):
                key = f"{rel}::{function}"
                if key not in self.ALLOWED_FUNCTIONS:
                    offenders.append(f"{key}:{lineno}")
        return offenders

    def test_no_unaudited_direct_projection_consumer(self):
        offenders = self._scan_backend()
        self.assertEqual(
            offenders, [],
            f"这些位置未列入函数级白名单却直接读 paper_positions 投影：{offenders}",
        )

    def test_allowlist_has_no_module_level_exemption(self):
        """白名单键必须精确到函数 —— 不接受整模块豁免。"""
        module_level = sorted(
            key for key in self.ALLOWED_FUNCTIONS if key.endswith("::<module>")
        )
        self.assertEqual(
            module_level, [],
            f"白名单存在模块级豁免（应按函数收窄）：{module_level}",
        )

    def test_api_adaptive_is_not_whitelisted(self):
        """§19 承重：``api_adaptive`` 不得再整体获豁免。

        它同时含 ``/rebalance/scan`` 这条 decision-adjacent 写路径，用
        "display read model" 把整个模块豁免掉，会让这条写路径的投影读取
        永久免检。
        """
        for module in self.MODULES_THAT_MUST_NOT_BE_WHITELISTED:
            self.assertNotIn(
                module, self.ALLOWED_FUNCTIONS,
                f"{module} 被整体加入投影白名单",
            )
            for key in self.ALLOWED_FUNCTIONS:
                self.assertFalse(
                    key.startswith(module + "::"),
                    f"{module} 仍以函数级形式获豁免：{key}",
                )

    def test_allowlist_entries_are_not_stale(self):
        """白名单不得留下"已经不需要"的条目 —— 过宽的白名单就是下一个漏洞。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        live = set()
        for name in sorted(os.listdir(os.path.join(root, "backend"))):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            rel = f"backend/{name}"
            for function, _lineno in self._projection_reads(
                    os.path.join(root, "backend", name)):
                live.add(f"{rel}::{function}")
        stale = sorted(set(self.ALLOWED_FUNCTIONS) - live)
        self.assertEqual(stale, [], f"白名单含不再需要的条目：{stale}")

    def test_docstrings_are_not_treated_as_queries(self):
        """文档字符串里的 SQL 是散文，不该逼出一条豁免。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        reads = self._projection_reads(os.path.join(root, "backend", "paper_trading.py"))
        functions = {fn for fn, _ln in reads}
        self.assertNotIn(
            "_position_rows", functions,
            "_position_rows 的 docstring 被误判成了真实投影查询",
        )


class RebalanceDatabaseOwnership(unittest.TestCase):
    """Round-11 §4/§5/§26 —— 调仓 endpoint 的数据库归属（源码级守卫）。

    行为证据在 ``test_rebalance_db_ownership.py``（两个物理分离的 SQLite）。
    这里锁住"调用点形状"，防止有人把连接改回去而测试夹具恰好没覆盖到。
    """

    def _src(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_api_adaptive_rebalance_routes_never_use_adaptive_connect(self):
        """四个 rebalance endpoint 都不得再用 ``adaptive._connect()``。

        ``adaptive._connect()`` = ``adaptive_learning.sqlite3``，而调仓扫描要读
        ``paper_accounts`` / 当前持仓 lot / ``paper_orders`` / ``paper_signals``，
        并写 ``rebalance_*`` —— 全部属于 paper ledger。
        """
        import ast

        src = self._src("backend/api_adaptive.py")
        tree = ast.parse(src)

        def decorator_path(node):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    args = dec.args
                    if args and isinstance(args[0], ast.Constant):
                        return str(args[0].value)
            return None

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            path = decorator_path(node)
            if not path or not path.startswith("/rebalance/"):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                fn = inner.func
                if isinstance(fn, ast.Attribute) and fn.attr == "_connect" \
                        and isinstance(fn.value, ast.Name) and fn.value.id == "adaptive":
                    offenders.append(f"{path}:{inner.lineno}")
        self.assertEqual(
            offenders, [],
            f"这些 rebalance endpoint 仍在 adaptive DB 上执行：{offenders}",
        )

    def test_paper_rebalance_db_helper_targets_paper_db_path(self):
        """唯一连接入口必须指向 ``PAPER_DB_PATH``，且用项目既有的写 helper。"""
        import ast

        src = self._src("backend/api_adaptive.py")
        tree = ast.parse(src)
        helper = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_paper_rebalance_db"),
            None,
        )
        self.assertIsNotNone(helper, "缺少 _paper_rebalance_db 连接入口")
        body = ast.get_source_segment(src, helper) or ""
        self.assertIn("PST.db(", body, "未使用项目既有的 paper 写 helper")
        self.assertIn("PAPER_DB_PATH", body, "未指向 paper ledger")

    def test_rebalance_scanner_does_not_query_missing_risk_log_table(self):
        """§8：``risk_log`` 表不存在，任何查询都会让扫描 100% 失败。"""
        src = self._src("backend/rebalance_scanner.py")
        self.assertNotIn("FROM risk_log", src,
                         "rebalance_scanner 仍查询不存在的 risk_log 表")


if __name__ == "__main__":
    unittest.main()
