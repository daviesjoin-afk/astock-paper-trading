# -*- coding: utf-8 -*-
"""Round-9：legacy ``paper_positions`` 镜像不得被重物化成 executable position。

规格 §20（LP1–LP8）、§13/§14（读路径零写入）、§9（legacy-only mirror）、
§8/§10（兼容投影契约）、§30（architecture guard）。

不变量（本文件存在的全部理由）::

    paper_position_lots is the executable position authority.
    paper_positions is a compatibility projection only.

    A read path must never manufacture executable position facts.

    A stale position mirror from an old cycle must never become a lot in the
    current cycle.

    Unknown historical position ownership must remain unknown.
    Current active cycle is not evidence of historical ownership.

    Position projection flows one way: lots -> paper_positions,
    never the reverse during normal runtime.

全部用例驱动**真实生产 schema**（``PT.init_db``）与**真实生产原语**
（``_position_rows`` / ``_sync_positions`` / ``_shared_account_exposure`` /
``aggregate_positions``）。手写最小 DDL 证明不了本轮的缺陷：被测行为恰恰是
「一次普通读取会不会往 lot 表里写一行」，而那个行为由生产代码持有。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as PT  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
NAME = "测试股"


class _LedgerCase(unittest.TestCase):
    """真实生产 schema 的临时账本（一个 active cycle + 一个账户）。"""

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
            (f"c-{started_at[:10]}-{started_at[11:13]}", status, 100000.0, "shared_pool",
             started_at, started_at, started_at if status == "running" else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def activate(self, cycle_id):
        """把账户绑到该周期（``_active_cycle`` 按 id DESC 解析，故 id 更大即生效）。"""
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (cycle_id, ACCOUNT)
        )
        self.conn.commit()

    def active_cycle(self):
        return int(
            self.conn.execute(
                "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )

    def add_lot(self, cycle_id, qty, *, available_date="2026-09-01", source_order_id=4242):
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,cost_fee_included,"
            "is_t_base,source_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ACCOUNT, CODE, NAME, "测试", qty, qty, 10.0,
             "2026-08-31 10:00:00", available_date, "stock_t1", 1, 1, source_order_id),
        )
        self.conn.commit()

    def add_mirror_only(self, qty=100, *, account_id=ACCOUNT, code=CODE):
        """直接写一条 legacy 聚合镜像行（模拟升级前遗留 / 陈旧投影）。"""
        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, code, NAME, "测试", qty, 10.0, "2026-08-31", "2026-09-01",
             "stock_t1", 12.5, 2),
        )
        self.conn.commit()

    def lots(self, cycle_id=None, *, only_null_source=False):
        sql = "SELECT * FROM paper_position_lots"
        params = []
        clauses = []
        if cycle_id is not None:
            clauses.append("cycle_id=?")
            params.append(cycle_id)
        if only_null_source:
            clauses.append("source_order_id IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, tuple(params)).fetchall()

    def lot_fingerprint(self):
        """lot 表的完整身份：id / cycle / remaining / source_order_id。"""
        return [
            (r["id"], r["cycle_id"], r["remaining_qty"], r["source_order_id"])
            for r in self.conn.execute(
                "SELECT id,cycle_id,remaining_qty,source_order_id FROM paper_position_lots ORDER BY id"
            )
        ]

    def mirror_rows(self):
        return [
            dict(r) for r in self.conn.execute("SELECT * FROM paper_positions ORDER BY account_id,code")
        ]


class NewCycleMustNotInheritStaleMirror(_LedgerCase):
    """§12 / §20 LP1 / LP8 / §21 —— 本轮的主承重用例。"""

    def _seed_cycle_with_mirror(self):
        self.add_lot(self.cycle1, 100)
        PT._sync_positions(self.conn)
        self.conn.commit()
        self.assertEqual(len(self.mirror_rows()), 1, "夹具应已写出聚合镜像")

    def test_LP1_new_cycle_gets_no_inferred_lot(self):
        """LP1: 旧周期镜像 + 新 active cycle ⇒ 不得产生当前周期 lot。"""
        self._seed_cycle_with_mirror()
        c2 = self.add_cycle()
        self.activate(c2)
        self.assertEqual(self.active_cycle(), c2)
        self.assertEqual(len(self.lots(c2)), 0, "新周期起点必须没有 lot")

        PT._position_rows(self.conn, readonly=False)

        self.assertEqual(
            len(self.lots(c2)), 0,
            "旧周期的 paper_positions 镜像被重物化成了新周期的可成交 lot",
        )
        self.assertEqual(
            len(self.lots(c2, only_null_source=True)), 0,
            "不得产生 source_order_id IS NULL 的推断 lot",
        )

    def test_LP8_first_risk_read_does_not_inherit_previous_cycle_position(self):
        """LP8: 新周期后的第一次风控/敞口读取不得继承上一周期持仓。"""
        self._seed_cycle_with_mirror()
        c2 = self.add_cycle()
        self.activate(c2)

        positions = PT._position_rows(self.conn, readonly=False)

        self.assertEqual(positions, [], "新周期不应看到任何持仓")
        self.assertEqual(len(self.lots(c2)), 0)

    def test_cross_cycle_adversarial_sequence(self):
        """§21: 连续调用多个入口后，新周期仍为 0，旧周期不变。"""
        self._seed_cycle_with_mirror()
        c2 = self.add_cycle()
        self.activate(c2)
        old_fingerprint = self.lot_fingerprint()

        PT._position_rows(self.conn, readonly=False)
        try:
            PT._shared_account_exposure(self.conn, {})
        except Exception:  # noqa: BLE001 - 敞口计算依赖行情；被测的是 lot 状态
            pass
        PT._pending_position_slots(self.conn) if hasattr(PT, "_pending_position_slots") else None
        PT._sync_positions(self.conn)
        PT._position_rows(self.conn, readonly=False)

        self.assertEqual(len(self.lots(c2)), 0, "新周期不得获得任何 lot")
        self.assertEqual(
            self.lot_fingerprint(), old_fingerprint,
            "旧周期 lot 身份被改动",
        )


class RepeatedReadsAreStable(_LedgerCase):
    """§20 LP2 / §14 —— 读路径零写入。"""

    def test_LP2_repeated_position_rows_leave_lot_state_unchanged(self):
        self.add_lot(self.cycle1, 100)
        PT._sync_positions(self.conn)
        self.conn.commit()
        c2 = self.add_cycle()
        self.activate(c2)

        PT._position_rows(self.conn, readonly=False)
        first = self.lot_fingerprint()
        PT._position_rows(self.conn, readonly=False)
        PT._position_rows(self.conn, readonly=False)
        self.assertEqual(self.lot_fingerprint(), first, "重复读取改变了 lot 表")

    def test_LP2b_readonly_and_writable_paths_agree(self):
        """readonly 与普通读取必须是同一份读模型（不得一条写一条不写）。"""
        self.add_lot(self.cycle1, 100)
        PT._sync_positions(self.conn)
        self.conn.commit()
        c2 = self.add_cycle()
        self.activate(c2)

        writable = PT._position_rows(self.conn, readonly=False)
        after_writable = self.lot_fingerprint()
        readonly = PT._position_rows(self.conn, readonly=True)

        self.assertEqual(self.lot_fingerprint(), after_writable, "readonly 读取改动了 lot 表")
        self.assertEqual(
            [p["code"] for p in writable], [p["code"] for p in readonly],
            "两条读取路径给出了不同的持仓视图",
        )


class ExposureReadCreatesNoLot(_LedgerCase):
    """§13 / §20 LP3 —— 生产风控/资金链可达路径。"""

    def test_LP3_shared_account_exposure_does_not_write_lots(self):
        self.add_lot(self.cycle1, 100)
        PT._sync_positions(self.conn)
        self.conn.commit()
        c2 = self.add_cycle()
        self.activate(c2)
        before = self.lot_fingerprint()

        try:
            PT._shared_account_exposure(self.conn, {})
        except Exception:  # noqa: BLE001 - 行情依赖；断言的是 lot 状态
            pass

        self.assertEqual(self.lot_fingerprint(), before, "敞口计算写入了 lot")
        self.assertEqual(len(self.lots(c2)), 0)

    def test_LP3b_exposure_before_after_lot_identity_unchanged(self):
        """§13 要求 before/after 的 lot 计数、id、数量、周期全部不变。"""
        self.add_lot(self.cycle1, 100)
        self.add_lot(self.cycle1, 50, available_date="2026-09-05")
        PT._sync_positions(self.conn)
        self.conn.commit()
        c2 = self.add_cycle()
        self.activate(c2)

        before = {
            "count": len(self.lots()),
            "ids": sorted(r["id"] for r in self.lots()),
            "qtys": sorted(r["remaining_qty"] for r in self.lots()),
            "cycles": sorted({r["cycle_id"] for r in self.lots()}),
        }
        try:
            PT._shared_account_exposure(self.conn, {})
        except Exception:  # noqa: BLE001
            pass
        after = {
            "count": len(self.lots()),
            "ids": sorted(r["id"] for r in self.lots()),
            "qtys": sorted(r["remaining_qty"] for r in self.lots()),
            "cycles": sorted({r["cycle_id"] for r in self.lots()}),
        }
        self.assertEqual(before, after)


class SyncPositionsFlowsOneWay(_LedgerCase):
    """§11 / §20 LP4 —— 只允许 lots -> paper_positions。"""

    def test_LP4_sync_rebuilds_mirror_from_lots(self):
        self.add_lot(self.cycle1, 100)
        self.conn.commit()

        PT._sync_positions(self.conn)
        self.conn.commit()

        mirror = self.mirror_rows()
        self.assertEqual(len(mirror), 1)
        self.assertEqual(mirror[0]["code"], CODE)
        self.assertEqual(int(mirror[0]["qty"]), 100)

    def test_LP4b_sync_never_builds_lots_from_mirror(self):
        """镜像里有、lot 里没有 ⇒ 同步后仍然没有 lot（不得反向生成）。"""
        self.add_mirror_only(qty=100)
        before = self.lot_fingerprint()

        PT._sync_positions(self.conn)
        self.conn.commit()

        self.assertEqual(self.lot_fingerprint(), before, "同步把镜像反向变成了 lot")
        self.assertEqual(len(self.lots()), 0)

    def test_LP4c_sync_removes_mirror_row_without_lots(self):
        """镜像必须由 lot 重建：无 lot 支撑的镜像行不得残留成持仓。"""
        self.add_mirror_only(qty=100)

        positions = PT._sync_positions(self.conn)
        self.conn.commit()

        self.assertEqual(positions, [], "无 lot 支撑的镜像被当成持仓输出")
        self.assertEqual(self.mirror_rows(), [], "陈旧镜像行未被清理")


class LegacyOnlyMirrorIsNotExecutable(_LedgerCase):
    """§9 / §20 LP5 —— legacy-only 镜像不得成为 executable position。"""

    def test_LP5_legacy_only_mirror_creates_nothing(self):
        self.add_mirror_only(qty=100)
        before = self.lot_fingerprint()

        positions = PT._position_rows(self.conn, readonly=False)

        self.assertEqual(len(self.lots()), 0, "legacy-only 镜像造出了 lot")
        self.assertEqual(self.lot_fingerprint(), before)
        self.assertEqual(positions, [], "legacy-only 镜像被当成持仓输出")

    def test_LP5b_legacy_row_remains_for_audit(self):
        """§9: legacy 行可以留在库里供审计 —— 不删除、不改周期、不造来源。"""
        self.add_mirror_only(qty=100)
        PT._position_rows(self.conn, readonly=False)

        rows = self.mirror_rows()
        self.assertEqual(len(rows), 1, "legacy 镜像行被删除了（禁止清洗历史数据）")
        self.assertEqual(int(rows[0]["qty"]), 100)
        self.assertEqual(rows[0]["account_id"], ACCOUNT)


class LegacyMetadataCompatibility(_LedgerCase):
    """§8 / §20 LP6 / LP7 —— 兼容投影契约。"""

    def test_LP6_authoritative_lot_gets_legacy_metadata(self):
        """LP6（R14 修订）: 有权威 lot 时，镜像的 peak_price / take_stage
        **不再被采用** —— 它们的权威在 cycle-owned 风险状态表。

        缺失状态走显式 fail-safe（peak 锚定成本、take_stage=None 未知），
        执行判定绝不读投影："未知"不能被镜像冒充成"已知"。
        """
        self.add_lot(self.cycle1, 100)
        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, NAME, "测试", 100, 10.0, "2026-08-31", "2026-09-01",
             "stock_t1", 13.75, 3),
        )
        self.conn.commit()

        positions = PT._position_rows(self.conn, readonly=False)
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(float(positions[0]["peak_price"]), 10.0)
        self.assertIsNone(positions[0]["take_stage"])
        self.assertEqual(positions[0]["risk_state_source"], "missing")

    def test_LP7_authoritative_lot_qty_beats_stale_mirror_qty(self):
        """LP7: 数量必须来自 lot，而不是陈旧的镜像。"""
        self.add_lot(self.cycle1, 100)
        self.add_mirror_only(qty=999)          # 陈旧 / 不一致的镜像数量

        positions = PT._position_rows(self.conn, readonly=False)

        self.assertEqual(len(positions), 1)
        self.assertEqual(int(positions[0]["qty"]), 100, "数量被陈旧镜像覆盖了")
        self.assertEqual(len(self.lots()), 1)
        self.assertEqual(int(self.lots()[0]["remaining_qty"]), 100)

    def test_LP7b_legacy_row_without_lot_contributes_no_metadata(self):
        """§8: legacy 只能**丰富**已有 lot 的元数据，不能单独产出持仓。"""
        self.add_mirror_only(qty=100, code="000001")
        self.add_lot(self.cycle1, 100)          # 另一个 code 的权威 lot

        positions = PT._position_rows(self.conn, readonly=False)

        self.assertEqual([p["code"] for p in positions], [CODE],
                         "无 lot 支撑的 legacy 行产出了持仓")


class LegacyMigrationIsGone(unittest.TestCase):
    """§5/§6A —— runtime auto-migration 已整体移除，不留危险 helper。"""

    def test_migrate_legacy_positions_no_longer_exists(self):
        self.assertFalse(
            hasattr(PT, "_migrate_legacy_positions"),
            "runtime 自动迁移 helper 仍存在；§6 方案 A 要求整体删除",
        )

    def test_position_rows_never_creates_lots_cycles_orders_fills(self):
        """§5: _position_rows 两条分支都不得写这五类事实。"""
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "paper.sqlite3")
        patches = (
            mock.patch.object(PT, "DB_PATH", path),
            mock.patch.object(PT, "_benchmark_close", return_value=None),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for p in patches:
            p.start()
        try:
            PT.init_db()
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
                "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ACCOUNT, CODE, NAME, "测试", 100, 10.0, "2026-08-31", "2026-09-01",
                 "stock_t1", 0.0, 0),
            )
            conn.commit()

            def counts():
                return (
                    conn.execute("SELECT COUNT(*) FROM paper_position_lots").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM paper_cycles").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
                    conn.execute("SELECT COALESCE(SUM(cash),0) FROM paper_accounts").fetchone()[0],
                )

            for readonly in (False, True):
                before = counts()
                PT._position_rows(conn, readonly=readonly)
                self.assertEqual(counts(), before, f"readonly={readonly} 的读取写入了事实表")
            conn.close()
        finally:
            for p in reversed(patches):
                p.stop()
            tmp.cleanup()


class PaperPositionsIsNotAuthority(unittest.TestCase):
    """§30 —— architecture guard：paper_positions 不得成为权威来源。"""

    def _source(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_no_runtime_legacy_position_rematerialization(self):
        """用 AST 检查真实引用，而不是文本匹配。

        文本匹配会把「注释里说明这个 helper 已被删除」也算成违规（本测试第一
        版就是这样误报的）。架构守卫要禁的是**可执行的调用**，所以解析成语法树
        后只看 ``Name`` / ``Attribute`` 节点。
        """
        import ast

        tree = ast.parse(self._source("backend/paper_trading.py"))
        referenced = sorted({
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "_migrate_legacy_positions"
        } | {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "_migrate_legacy_positions"
        })
        self.assertEqual(
            referenced, [],
            "paper_trading.py 仍存在对已禁止的 runtime 迁移的可执行引用",
        )

    def test_position_read_helpers_do_not_call_migration(self):
        """§5: 读模型函数体内不得出现任何迁移调用。"""
        import ast

        tree = ast.parse(self._source("backend/paper_trading.py"))
        targets = {"_position_rows", "_sync_positions", "_shared_account_exposure"}
        banned = {"_migrate_legacy_positions", "_ensure_cycle"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in targets:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name in banned:
                    offenders.append(f"{node.name} -> {name}")
        self.assertEqual(
            offenders, [],
            f"读模型函数仍在写账本（{offenders}）：读取不得创建周期或 lot",
        )

    def test_lot_insert_statements_do_not_read_from_paper_positions(self):
        """任何 INSERT INTO paper_position_lots 都不得以 paper_positions 为来源。

        这是 §30「paper_positions 不能成为 lot creator」的可执行形式：逐个看
        每个 lot 写入语句所在函数体，函数体内不得同时出现 paper_positions 读取。
        """
        src = self._source("backend/paper_trading.py")
        lines = src.splitlines()
        offenders = []
        for idx, line in enumerate(lines):
            if "INSERT INTO paper_position_lots" not in line and "INSERT OR REPLACE INTO paper_position_lots" not in line:
                continue
            window = "\n".join(lines[max(0, idx - 60): idx + 20])
            if "FROM paper_positions" in window:
                offenders.append(idx + 1)
        self.assertEqual(
            offenders, [],
            f"这些 lot 写入语句附近出现了 paper_positions 读取（第 {offenders} 行）："
            "投影不得成为 lot 的来源",
        )


if __name__ == "__main__":
    unittest.main()
