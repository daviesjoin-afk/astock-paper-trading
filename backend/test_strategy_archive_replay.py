# -*- coding: utf-8 -*-
"""PR-36：策略归档与历史回放。

验收目标（PR19 最大测试缺口）：
- 真实创建策略 v1→v2→交易→持仓→pause→retiring→archive；
- archive 后不能再产生新信号，但已有 T+1 持仓可以被风控安全退出；
- 历史 order/fill 能解析回原 strategy version/checksum；
- 重启（重新打开数据库）后仍可回放，账本不变式保持；
- 只允许无引用 draft 硬删除，有历史引用的策略必须走 archive；
- 全库无 orphan strategy_id/version。

与黄金回放共用同一套离线环境（OfflinePaperEnv）：行情、健康工件全部
依赖注入，不 INSERT 任何 paper_orders/paper_fills，全程调用生产 Service。
"""
from __future__ import annotations

import sqlite3
import unittest

import strategy_registry as SR  # noqa: F401  (经由 G 暴露，显式导入便于阅读)
import test_production_path_golden_replay as G

STRATEGY_ID = "arch_replay_beta"

# v2 规则：把放量窗口从 5 日收紧到 8 日（纯条件变更，无风险方向参数）。
RULE_V2 = {
    "op": "and",
    "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": 20}},
        {"op": "gt", "left": {"op": "field", "name": "volume"},
         "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 8},
                   "right": {"op": "const", "value": 1.2}}},
    ],
}


class StrategyArchiveReplayTests(G.OfflinePaperEnv, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 两个测试类共用模块级行情字典；清掉黄金回放类留下的按日价格，
        # 避免跨类串价（每个类都在自己的临时目录重新播种行情）。
        G.QUOTE_PRICES.clear()
        G.QUOTE_SCENARIOS.clear()

    def test_strategy_archive_and_historical_replay(self):
        # ---------- 1) create → validate → activate（v1）----------
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, STRATEGY_ID, "归档回放声明式策略", dsl_ast=G.RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="archive-test",
            )
            SR.transition(conn, STRATEGY_ID, "validated", expected_status="draft",
                          reason="archive validate", actor="archive-test")
            SR.transition(conn, STRATEGY_ID, "active", expected_status="validated",
                          reason="archive activate", actor="archive-test")
            self.assertTrue(SR.get(STRATEGY_ID, conn=conn).supports_new_cycle)

        # ---------- 2) 开户 + 周期 + 生产信号/开仓（v1 交易）----------
        G.PT.init_db()
        with self._conn() as conn:
            G.RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="archive-test")
        _, cycle = G.PT.start_new_cycle(capital=G.CAPITAL, include_dashboard=False)
        self.assertEqual(tuple(cycle["enabled_strategies"]), (STRATEGY_ID,))
        close_result = G.PT.generate_signals(G.D0)
        user_rows = [row for row in close_result["accounts"] if row["id"] == STRATEGY_ID]
        self.assertTrue(user_rows and user_rows[0]["created"] > 0, close_result)
        opened = G.PT.run_slot("open", G.D1, force=True)
        self.assertNotEqual(opened.get("status"), "failed", opened)
        with self._conn() as conn:
            buy_fills = conn.execute(
                "SELECT code,qty,price FROM paper_fills WHERE account_id=? AND side='buy'",
                (STRATEGY_ID,),
            ).fetchall()
        self.assertTrue(buy_fills, "v1 必须产生真实买入成交")
        v1_order_versions = {
            row["strategy_version"] for row in self._conn().execute(
                "SELECT DISTINCT strategy_version FROM paper_orders WHERE account_id=?",
                (STRATEGY_ID,),
            )
        }
        self.assertEqual(v1_order_versions, {1})

        # ---------- 3) v2 定义（历史订单仍指向 v1 戳）----------
        with self._conn() as conn:
            SR.save_definition(conn, STRATEGY_ID, {"dsl_ast": RULE_V2},
                               actor="archive-test")
            spec = SR.get(STRATEGY_ID, conn=conn)
            self.assertEqual(spec.current_version, 2)
            self.assertEqual(spec.status, "active")

        # ---------- 4) pause：不再产生新信号 ----------
        with self._conn() as conn:
            SR.transition(conn, STRATEGY_ID, "paused", expected_status="active",
                          reason="archive pause", actor="archive-test")
            self.assertEqual(SR.get(STRATEGY_ID, conn=conn).supports_new_cycle, 0)
            signals_before = conn.execute(
                "SELECT COUNT(*) FROM paper_signals WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchone()[0]
        pause_scan = G.PT.generate_signals(G.D1)
        self.assertFalse(
            [row for row in pause_scan["accounts"] if row["id"] == STRATEGY_ID],
            "暂停策略必须被排除出收盘扫描（无新信号通道）",
        )
        with self._conn() as conn:
            signals_after = conn.execute(
                "SELECT COUNT(*) FROM paper_signals WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchone()[0]
        self.assertEqual(signals_before, signals_after)

        # ---------- 5) retiring → archive ----------
        with self._conn() as conn:
            SR.archive_definition(conn, STRATEGY_ID, reason="archive replay",
                                  actor="archive-test")
            spec = SR.get(STRATEGY_ID, conn=conn)
            self.assertEqual(spec.status, "archived")
            self.assertEqual(spec.supports_new_cycle, 0)
            # 归档策略不能重新进入启用集合
            with self.assertRaises(ValueError):
                G.RSET.validate({"enabled_strategies": [STRATEGY_ID]}, conn=conn)

        # ---------- 6) 归档后已有 T+1 持仓必须能被风控安全退出 ----------
        with self._conn() as conn:
            position = conn.execute(
                "SELECT * FROM paper_positions WHERE account_id=? LIMIT 1",
                (STRATEGY_ID,),
            ).fetchone()
            self.assertIsNotNone(position, "归档前必须已建仓（前置断言）")
            cost = float(position["cost"])
        for code in G.PASS_CODES:
            G.QUOTE_PRICES[(code, G.D2.isoformat())] = round(cost * 0.83, 2)
        G.QUOTE_SCENARIOS[G.D2.isoformat()] = {
            "pct": -8.5, "main_pct": -5.0, "main_net": -5_000_000.0,
            "super_net": -4_000_000.0, "vol_ratio": 1.8, "open_above": True,
        }
        with self._conn() as conn:
            conn.execute("DELETE FROM paper_jobs WHERE slot='risk'")
            conn.execute("DELETE FROM paper_audit WHERE event='risk_scan_state'")
        G.PT.run_slot("risk", G.D2, force=True)
        with self._conn() as conn:
            sell_fills = conn.execute(
                "SELECT code,qty,price,fill_date FROM paper_fills "
                "WHERE account_id=? AND side='sell'",
                (STRATEGY_ID,),
            ).fetchall()
            remaining = conn.execute(
                "SELECT COALESCE(SUM(qty),0) AS n FROM paper_positions WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchone()[0]
        self.assertTrue(sell_fills, "归档策略的 T+1 持仓必须仍可经生产风控退出")
        self.assertEqual(int(remaining), 0, "崩盘退出后不允许残留持仓")

        # ---------- 7) 历史 order/fill 解析原 strategy version/checksum ----------
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT o.strategy_id, o.strategy_version, o.strategy_checksum
                   FROM paper_orders o WHERE o.account_id=?
                   UNION
                   SELECT o.strategy_id, o.strategy_version, o.strategy_checksum
                   FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id
                   WHERE f.account_id=?""",
                (STRATEGY_ID, STRATEGY_ID),
            ).fetchall()
        self.assertTrue(rows)
        with self._conn() as conn:
            for row in rows:
                self.assertIsNotNone(row["strategy_version"])
                self.assertIsNotNone(row["strategy_checksum"])
                resolved = conn.execute(
                    """SELECT 1 FROM paper_strategy_versions
                       WHERE strategy_id=? AND version=? AND checksum=?""",
                    (row["strategy_id"], row["strategy_version"], row["strategy_checksum"]),
                ).fetchone()
                self.assertIsNotNone(
                    resolved,
                    f"无法解析的历史戳：{tuple(row)}",
                )

        # ---------- 8) DB 重启后仍可回放：重开连接重放账本不变式 ----------
        conn = sqlite3.connect(G.PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            orders = conn.execute(
                "SELECT id,side,strategy_id,strategy_version,strategy_checksum "
                "FROM paper_orders WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchall()
            fills = conn.execute(
                "SELECT f.id, o.strategy_id FROM paper_fills f "
                "JOIN paper_orders o ON o.id=f.order_id WHERE f.account_id=?",
                (STRATEGY_ID,),
            ).fetchall()
            definition = SR.get(STRATEGY_ID, conn=conn)
            versions = {row[0] for row in conn.execute(
                "SELECT version FROM paper_strategy_versions WHERE strategy_id=?",
                (STRATEGY_ID,),
            )}
        finally:
            conn.close()
        self.assertEqual(definition.status, "archived")
        self.assertEqual(definition.current_version, 2)
        self.assertEqual(versions, {1, 2})
        for row in orders:
            self.assertIn(row["strategy_version"], versions)
        for row in fills:
            self.assertEqual(row["strategy_id"], STRATEGY_ID)

        # ---------- 9) 无 orphan strategy_id/version（全库扫描）----------
        with self._conn() as conn:
            known = {row[0] for row in conn.execute(
                "SELECT id FROM strategy_definitions"
            )} | set(G.PT.ACCOUNT_SPECS) | {STRATEGY_ID}
            for table in ("paper_orders", "paper_fills", "paper_risk_decisions",
                          "paper_signals"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "strategy_id" not in columns:
                    continue
                for row in conn.execute(
                    f"SELECT DISTINCT strategy_id FROM {table} WHERE strategy_id IS NOT NULL"
                ):
                    self.assertIn(row[0], known,
                                  f"{table} 存在 orphan strategy_id: {row[0]}")
            orphans = conn.execute(
                """SELECT DISTINCT o.account_id, o.strategy_version
                   FROM paper_orders o
                   LEFT JOIN paper_strategy_versions v
                     ON v.strategy_id=o.strategy_id AND v.version=o.strategy_version
                   WHERE o.strategy_version IS NOT NULL AND v.strategy_id IS NULL"""
            ).fetchall()
            self.assertFalse(orphans, f"存在 orphan strategy_version: {[tuple(r) for r in orphans]}")

        # ---------- 10) 硬删除闸门 ----------
        with self._conn() as conn:
            with self.assertRaises(ValueError):
                SR.hard_delete_unused_draft(conn, STRATEGY_ID)  # archived + 有引用
            # 无引用 draft 可以物理删除
            SR.create_user_definition(
                conn, "arch_draft_scratch", "草稿", dsl_ast=G.RULE,
                metadata={"candidate_topn": 10}, actor="archive-test",
            )
            result = SR.hard_delete_unused_draft(conn, "arch_draft_scratch")
            self.assertTrue(result["deleted"])
            self.assertIsNone(SR.get("arch_draft_scratch", conn=conn))


if __name__ == "__main__":
    unittest.main()
