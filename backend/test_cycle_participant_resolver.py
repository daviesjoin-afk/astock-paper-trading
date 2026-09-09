# -*- coding: utf-8 -*-
"""PR-38：周期快照是执行层的权威参与者集合（single-owner）。

背景（修复前）：执行层的参与资格来自 Registry——
``ACTIVE_ACCOUNT_IDS ∪ USP.user_participant_ids()``。只要策略在注册表里
仍是 ``active``，即使它已经被从本周期的 ``enabled_strategies`` 中摘掉，
``generate_signals`` / ``open`` / ``auction`` / ``intraday`` 仍会继续为它
产生信号、占用共享池资金与席位、写入 ``paper_capital_reservations``。

修复后的权威口径（``PT.current_cycle_participant_ids``）：

    paper_cycles.enabled_strategies  ∩  paper_accounts.cycle_id == 当期 id

Registry active 只用于"创建下一周期"；lifecycle pause 可以从执行层临时禁
用新信号，而不必改写周期快照。

E2E 场景：
- Cycle1 = [A, B]，两个策略在注册表里都 active，都真实产生信号/成交；
- Cycle2 = [A]，B **保持 Registry active**；
- 断言 B 在 Cycle2 无 signal / 无新 order / 无 reservation、不占用共享
  资金（cycle_id=NULL、status=paused、cash=0）；
- 断言 Cycle1 的历史仍可查询（归档后仍能按 B 查到）。
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import unittest

import paper_trading as PT
import runtime_settings as RSET
import strategy_registry as SR
import strategy_runtime as SRT
import test_production_path_golden_replay as G

STRATEGY_A = "cyc_part_alpha"
STRATEGY_B = "cyc_part_beta"
BOTH = (STRATEGY_A, STRATEGY_B)


def _table_count(conn, table, where, params=()):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    except sqlite3.Error:
        return 0


class CycleParticipantResolverTests(G.OfflinePaperEnv, unittest.TestCase):
    """Cycle1=[A,B] → Cycle2=[A]，B 保持 Registry active。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        G.QUOTE_PRICES.clear()
        G.QUOTE_SCENARIOS.clear()

    def setUp(self):
        # 环境（行情/路径补丁）是类级共享的；账本必须每个用例独立，否则
        # 周期 1 的信号与账户状态会串到下一个用例。Windows 下删不掉被占用
        # 的临时库，所以直接换一个新的库文件。
        self._db_index = getattr(self.__class__, "_db_seq", 0)
        self.__class__._db_seq = self._db_index + 1
        PT.DB_PATH = os.path.join(self._tmp, f"paper_trading_{self._db_index}.sqlite3")
        SRT.clear_cache()
        PT.init_db()

    @contextlib.contextmanager
    def _conn(self):
        """基类 _conn 不关闭连接；这里显式关闭，避免临时库被长期占用。"""
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _activate(self, strategy_id):
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, strategy_id, "周期参与者策略 " + strategy_id, dsl_ast=G.RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="cycle-participant-test",
            )
            SR.transition(conn, strategy_id, "validated", expected_status="draft",
                          reason="validate", actor="cycle-participant-test")
            SR.transition(conn, strategy_id, "active", expected_status="validated",
                          reason="activate", actor="cycle-participant-test")

    def _enable(self, strategy_ids):
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": list(strategy_ids)},
                        actor="cycle-participant-test")

    def _start_cycle(self):
        PT.init_db()
        return PT.start_new_cycle(capital=G.CAPITAL, include_dashboard=False)

    def _pause_current_cycle(self):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.execute("UPDATE paper_cycles SET status='paused' WHERE id=?", (row["id"],))

    def _signal_count(self, strategy_id):
        """活跃信号数（周期切换会归档清空旧表，所以这就是"本周期新增"数）。"""
        with self._conn() as conn:
            return _table_count(conn, "paper_signals", "account_id=?", (strategy_id,))

    def _order_count(self, strategy_id):
        with self._conn() as conn:
            return _table_count(conn, "paper_orders", "account_id=?", (strategy_id,))

    def _order_ids(self, strategy_id):
        with self._conn() as conn:
            return {
                row[0] for row in conn.execute(
                    "SELECT id FROM paper_orders WHERE account_id=?", (strategy_id,),
                ).fetchall()
            }

    def _archived_order_ids(self, strategy_id):
        """从 paper_archives 的周期快照里解析历史委托 id（归档 ≠ 删除）。"""
        import json
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT snapshot FROM paper_archives ORDER BY id DESC"
            ).fetchall()
        found = set()
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(snapshot, dict):
                continue
            for order in snapshot.get("paper_orders") or []:
                if str(order.get("account_id")) == strategy_id and order.get("id") is not None:
                    found.add(int(order["id"]))
        return found

    def _reservations(self, cycle_id, strategy_id=None):
        with self._conn() as conn:
            if strategy_id is None:
                return _table_count(conn, "paper_capital_reservations", "cycle_id=?", (cycle_id,))
            return _table_count(
                conn, "paper_capital_reservations", "cycle_id=? AND account_id=?",
                (cycle_id, strategy_id),
            )

    def _run_one_round(self):
        """收盘生成信号 + 开盘执行（与生产 slot 完全相同的 Service 链）。"""
        close_result = PT.generate_signals(G.D0)
        self.assertNotEqual(close_result.get("status"), "failed", close_result)
        opened = PT.run_slot("open", G.D1, force=True)
        self.assertNotEqual(opened.get("status"), "failed", opened)
        return close_result, opened

    def test_cycle_snapshot_is_the_canonical_participant_set(self):
        # ---------- Cycle1 = [A, B]：两个策略都真实产生信号 ----------
        self._activate(STRATEGY_A)
        self._activate(STRATEGY_B)
        self._enable(BOTH)
        _, cycle1 = self._start_cycle()

        self.assertEqual(tuple(cycle1["enabled_strategies"]), BOTH)
        close1, _ = self._run_one_round()
        scanned1 = {row["id"] for row in close1["accounts"]}
        self.assertEqual(scanned1, set(BOTH), close1["accounts"])
        cycle1_a_signals = self._signal_count(STRATEGY_A)
        cycle1_b_signals = self._signal_count(STRATEGY_B)
        cycle1_b_orders = self._order_ids(STRATEGY_B)
        self.assertGreater(cycle1_a_signals, 0, "Cycle1：A 必须产生信号")
        self.assertGreater(cycle1_b_signals, 0, "Cycle1：B 必须产生信号")
        self.assertTrue(cycle1_b_orders, "Cycle1：B 必须落单（signal → order）")

        with self._conn() as conn:
            self.assertEqual(
                set(PT.current_cycle_participant_ids(conn, cycle1["id"])), set(BOTH),
            )

        # ---------- Cycle2 = [A]：B 保持 Registry active ----------
        self._pause_current_cycle()
        self._enable([STRATEGY_A])
        _, cycle2 = self._start_cycle()
        self.assertEqual(tuple(cycle2["enabled_strategies"]), (STRATEGY_A,))

        with self._conn() as conn:
            # 权威口径：B 不在 Cycle2 的参与者里。
            self.assertEqual(
                tuple(PT.current_cycle_participant_ids(conn, cycle2["id"])), (STRATEGY_A,),
            )
            # B 在注册表里仍然是 active —— 这正是修复前的漏点。
            self.assertEqual(SR.get(STRATEGY_B, conn=conn).status, "active")
            # B 被显式摘出 Cycle2：不挂接、暂停、资金清零。
            row_b = self._one(conn, "SELECT * FROM paper_accounts WHERE id=?", (STRATEGY_B,))
            self.assertIsNone(row_b["cycle_id"])
            self.assertEqual(row_b["status"], "paused")
            self.assertEqual(float(row_b["initial_cash"] or 0), 0.0)
            self.assertEqual(float(row_b["cash"] or 0), 0.0)
            # 共享池资金全部属于 A，B 不占一分钱。
            pool = conn.execute(
                "SELECT COALESCE(SUM(initial_cash),0) FROM paper_accounts WHERE cycle_id=?",
                (cycle2["id"],),
            ).fetchone()[0]
            self.assertAlmostEqual(float(pool), float(cycle2["capital"]), places=2)
            row_a = self._one(conn, "SELECT * FROM paper_accounts WHERE id=?", (STRATEGY_A,))
            self.assertEqual(row_a["cycle_id"], cycle2["id"])
            self.assertAlmostEqual(float(row_a["initial_cash"]), float(cycle2["capital"]), places=2)

        # ---------- Cycle2 跑一轮：B 完全不参与 ----------
        close2, _ = self._run_one_round()
        scanned2 = {row["id"] for row in close2["accounts"]}
        self.assertNotIn(STRATEGY_B, scanned2, close2["accounts"])
        self.assertIn(STRATEGY_A, scanned2, close2["accounts"])

        # B 在 Cycle2 完全没有新信号 / 新委托 / 资金预占。
        self.assertEqual(self._signal_count(STRATEGY_B), 0, "Cycle2：B 不得产生新信号")
        self.assertEqual(self._order_count(STRATEGY_B), 0, "Cycle2：B 不得产生新委托")
        self.assertEqual(self._reservations(cycle2["id"], STRATEGY_B), 0,
                         "Cycle2：B 不得占用共享池资金预占")
        # A 仍然正常参与（本周期的启用策略不受影响）。
        self.assertGreater(self._signal_count(STRATEGY_A), 0, "Cycle2：A 必须继续产生信号")

        # ---------- 历史 Cycle1 仍然可查（归档快照，不是删除） ----------
        self.assertTrue(cycle1_b_orders, "Cycle1 必须留下 B 的委托")
        self.assertTrue(
            cycle1_b_orders.issubset(self._archived_order_ids(STRATEGY_B)),
            "Cycle1 的 B 历史委托必须能从周期快照里查回",
        )
        with self._conn() as conn:
            archived = _table_count(conn, "paper_cycles", "status='archived'")
            self.assertGreaterEqual(archived, 1, "Cycle1 必须被归档而不是删除")

    def test_registry_pause_temporarily_disables_execution(self):
        """lifecycle pause 从执行层临时禁用新信号，但不摘出周期、不动资金。"""
        self._activate(STRATEGY_A)
        self._activate(STRATEGY_B)
        self._enable(BOTH)
        _, cycle = self._start_cycle()

        with self._conn() as conn:
            self.assertEqual(
                set(PT.current_cycle_participant_ids(conn, cycle["id"])), set(BOTH),
            )
            before = self._one(conn, "SELECT * FROM paper_accounts WHERE id=?", (STRATEGY_B,))

        with self._conn() as conn:
            SR.transition(conn, STRATEGY_B, "paused", expected_status="active",
                          reason="lifecycle pause", actor="cycle-participant-test")
        SRT.clear_cache()

        with self._conn() as conn:
            self.assertEqual(SR.get(STRATEGY_B, conn=conn).status, "paused")
            # 执行层立即失效，但账户仍挂在本周期、资金与历史未改写。
            self.assertEqual(
                tuple(PT.current_cycle_participant_ids(conn, cycle["id"])), (STRATEGY_A,),
            )
            after = self._one(conn, "SELECT * FROM paper_accounts WHERE id=?", (STRATEGY_B,))
            self.assertEqual(after["cycle_id"], before["cycle_id"])
            self.assertEqual(after["cycle_id"], cycle["id"])
            self.assertAlmostEqual(float(after["cash"]), float(before["cash"]), places=2)

    def test_resolver_falls_back_to_builtin_set_without_cycle_config(self):
        """周期未声明启用集合时保持旧口径，避免迁移/早期数据库整轮空转。"""
        self._activate(STRATEGY_A)
        self._enable([STRATEGY_A])
        self._start_cycle()
        with self._conn() as conn:
            conn.execute("UPDATE paper_cycles SET enabled_strategies=NULL")
        with self._conn() as conn:
            ids = PT.current_cycle_participant_ids(conn)
            resolution = PT._cycle_participant_resolution(conn)
            self.assertEqual(resolution["source"], "cycle_not_configured")
            self.assertTrue(set(PT.ACTIVE_ACCOUNT_IDS).issubset(set(ids)))


if __name__ == "__main__":
    unittest.main()
