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
        c2 = self.stale_mirror_scenario()
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
        self.assertEqual(PPRM.current_positions(conn), [])
        conn.close()


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
    """§20 —— 镜像只能补展示元数据，不能覆盖权威数量/成本/日期。"""

    def test_peak_and_take_stage_come_from_mirror(self):
        self.add_lot(self.cycle1, 100)
        self.add_mirror(qty=100, peak_price=13.75, take_stage=3)
        self.conn.commit()
        rows = PPRM.current_positions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["peak_price"]), 13.75)
        self.assertEqual(int(rows[0]["take_stage"]), 3)

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
    """§14 PC4–PC5 —— 替补候选排除必须用权威 holdings。"""

    def test_PC4_stale_mirror_does_not_exclude(self):
        self.stale_mirror_scenario()
        self.assertEqual(PPRM.current_held_codes(self.conn), set(),
                         "陈旧镜像把代码错误排除在替补候选外")

    def test_PC5_current_lot_is_excluded(self):
        self.add_lot(self.cycle1, 100)
        self.conn.commit()
        self.assertEqual(PPRM.current_held_codes(self.conn), {CODE})

    def test_rebalance_scanner_uses_authoritative_reader(self):
        """源码级：rebalance_scanner 不再直接读 paper_positions。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "backend/rebalance_scanner.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("FROM paper_positions", src,
                         "rebalance_scanner 仍直接读 paper_positions 投影")


class AdaptiveShadowPortfolio(_LedgerCase):
    """§15 PC6–PC7 —— 影子组合必须看到正确的当前事实。"""

    def test_PC6_stale_mirror_excluded_from_shadow_portfolio(self):
        self.stale_mirror_scenario()
        self.assertEqual(PPRM.current_positions(self.conn), [],
                         "影子组合会包含陈旧镜像持仓")

    def test_PC7_authoritative_lot_included(self):
        self.add_lot(self.cycle1, 100)
        self.conn.commit()
        rows = PPRM.current_positions(self.conn)
        self.assertEqual([r["code"] for r in rows], [CODE])

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
    """§11 —— paper_positions 不得作为 current-position authority。"""

    #: 允许直接读取 paper_positions 的场景（显式白名单）。
    #: 每个条目都是「为什么这个读取不是 current-position authority」。
    ALLOWED = {
        # 兼容投影的唯一写者/读点：由 lot 聚合后重建镜像。
        "backend/paper_trading.py": "projection writer + legacy metadata enrichment",
        # 兼容元数据（peak_price / take_stage）与镜像重建。
        "backend/paper_position_read_model.py": "read model enriches metadata from mirror",
        # 归档 / 清表清单。
        "backend/paper_cycle_service.py": "archive/purge table list",
        # schema 迁移。
        "backend/paper_schema_migrations.py": "schema migration column list",
        # 一致性自检（把投影与 lot 对账）。
        "backend/paper_replay_regression.py": "projection-vs-lot consistency check",
        # 开发/测试种子数据。
        "backend/demo_seed.py": "dev/test seed",
        # 展示 / 只读面板 / AI 上下文。
        "backend/api_adaptive.py": "display read model",
        "backend/ai_analysis.py": "display/AI context",
        "backend/deepseek_advisor.py": "display/diagnostics count",
        "backend/deepseek_research.py": "display/research aggregate",
        # 历史符号发现（明确非 holding）。
        "backend/news_learning.py": "recent symbol discovery only (documented non-holding)",
        # legacy helper（已登记 dead，且已改为 lot 证据）。
        "backend/strategy_champion.py": "legacy helper, lot-evidence based",
    }

    def test_no_unaudited_direct_projection_consumer(self):
        import ast

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        backend = os.path.join(root, "backend")
        offenders = []
        for name in sorted(os.listdir(backend)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            rel = f"backend/{name}"
            if rel in self.ALLOWED:
                continue
            with open(os.path.join(backend, name), encoding="utf-8") as fh:
                src = fh.read()
            if "paper_positions" not in src:
                continue
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value.upper()
                    if "PAPER_POSITIONS" in text and ("FROM " in text or "JOIN " in text):
                        offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            f"这些位置未列入白名单却直接读 paper_positions 投影：{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
