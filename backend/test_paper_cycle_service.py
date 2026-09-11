# -*- coding: utf-8 -*-
"""PR-58：``paper_cycle_service`` 的直接边界测试 + 架构守卫。

只覆盖**被抽出的边界**（资金校验 / 归档快照格式 / 归档副作用），
不复制既有的端到端回归（那由 917 例全量套件承担）。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_cycle_service as PCS
import paper_trading as PT
import strategy_registry as SR

BACKEND = os.path.dirname(os.path.abspath(__file__))
SERVICE_PATH = os.path.join(BACKEND, "paper_cycle_service.py")


class ArchitectureGuardTests(unittest.TestCase):
    """新服务不得依赖单体（否则等于制造循环依赖）。"""

    @classmethod
    def setUpClass(cls):
        with open(SERVICE_PATH, encoding="utf-8") as fh:
            cls.source = fh.read()

    def test_service_does_not_import_paper_trading(self):
        self.assertNotRegex(self.source, r"(?m)^\s*(?:import|from)\s+paper_trading\b",
                            "paper_cycle_service 不得 import paper_trading")

    def test_service_imports_only_lower_layers_and_stdlib(self):
        imported = set(re.findall(r"(?m)^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", self.source))
        # 标准库不算项目内部的"更底层模块"，要排除。
        stdlib = set(sys.stdlib_module_names)
        local = {name for name in imported
                 if name not in stdlib and os.path.exists(os.path.join(BACKEND, name + ".py"))}
        allowed_local = {"paper_repository"}
        offenders = sorted(local - allowed_local)
        self.assertEqual([], offenders, "服务只允许依赖更底层的模块：%s" % offenders)

    def test_paper_trading_exposes_facade_delegation(self):
        with open(os.path.join(BACKEND, "paper_trading.py"), encoding="utf-8") as fh:
            facade = fh.read()
        for call in ("PCS.validate_capital", "PCS.cycle_snapshot", "PCS.archive_cycle"):
            self.assertIn(call, facade, "façade 未委托 %s" % call)


class _DbCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="pr58-cycle-")
        cls._old_path = PT.DB_PATH
        PT.DB_PATH = os.path.join(cls._tmp, "paper.sqlite3")
        PT.init_db()
        cls.conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        PT.DB_PATH = cls._old_path
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self.conn.execute("DELETE FROM paper_cycles")
        self.conn.execute("DELETE FROM paper_archives")
        self.conn.execute("DELETE FROM paper_audit")
        for table in PCS.PURGED_TABLES:
            self.conn.execute("DELETE FROM %s" % table)
        self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,updated_at,"
            "duration_days,enabled_strategies) VALUES(?,?,?,?,?,?,?,?)",
            ("cycle-test", "running", 300000.0, "shared_pool", PCS.now(), PCS.now(), 14, "[]"),
        )
        self.conn.commit()
        self.cycle = dict(self.conn.execute(
            "SELECT * FROM paper_cycles WHERE cycle_key='cycle-test'").fetchone())


class ValidateCapitalTests(unittest.TestCase):
    def test_boundaries_are_inclusive(self):
        self.assertEqual(1000.0, PCS.validate_capital(1000))
        self.assertEqual(10_000_000.0, PCS.validate_capital(10_000_000))

    def test_out_of_range_is_rejected(self):
        for bad in (999.99, 10_000_001, 0):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    PCS.validate_capital(bad)


class CycleSnapshotTests(_DbCase):
    def test_snapshot_carries_format_and_counts(self):
        _cycle, snapshot = PCS.cycle_snapshot(self.conn, self.cycle)
        self.assertEqual(PCS.ARCHIVE_FORMAT, snapshot["_archive_format"])
        counted = snapshot["_table_counts"]
        for table in PCS.LEDGER_TABLES + PCS.COUNTED_TABLES:
            self.assertIn(table, counted, "快照缺少 %s 的行数" % table)

    def test_orders_snapshot_excludes_bulky_risk_payload(self):
        # paper_orders 上有策略版本戳触发器：必须带与注册表一致的 version stamp。
        stamp = SR.stamp_for_account(self.conn, "tq_breakout")
        self.conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,qty,status,risk_payload,created_at,"
            "strategy_id,strategy_version,strategy_checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (1, "tq_breakout", "buy", "600901", 100, "pending", "{}", PCS.now()) + stamp,
        )
        self.conn.commit()
        _cycle, snapshot = PCS.cycle_snapshot(self.conn, self.cycle)
        for row in snapshot["paper_orders"]:
            self.assertNotIn("risk_payload", row, "归档不得搬运大体积 risk_payload")


class ArchiveCycleTests(_DbCase):
    def _archive(self):
        stamp = SR.stamp_for_account(self.conn, "tq_breakout")
        self.conn.execute(
            "INSERT INTO paper_signals(account_id,signal_date,intended_date,code,payload,status,created_at,"
            "strategy_id,strategy_version,strategy_checksum) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("tq_breakout", "2026-09-11", "2026-09-12", "600901", "{}", "pending", PCS.now()) + stamp,
        )
        self.conn.commit()
        return PCS.archive_cycle(self.conn, self.cycle, "单元测试归档")

    def test_archive_writes_archive_row_and_flips_status(self):
        self._archive()
        archived = self.conn.execute(
            "SELECT * FROM paper_archives WHERE cycle_id=?", (self.cycle["id"],)).fetchone()
        self.assertIsNotNone(archived, "未写入 paper_archives")
        self.assertEqual("单元测试归档", archived["reason"])
        cycle = self.conn.execute(
            "SELECT status, ended_at FROM paper_cycles WHERE id=?", (self.cycle["id"],)).fetchone()
        self.assertEqual("archived", cycle["status"])
        self.assertTrue(cycle["ended_at"], "归档后必须写入 ended_at")

    def test_archive_purges_active_ledger_tables(self):
        self._archive()
        for table in PCS.PURGED_TABLES:
            count = self.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            self.assertEqual(0, count, "%s 归档后应清空" % table)

    def test_archive_records_audit_event(self):
        self._archive()
        event = self.conn.execute(
            "SELECT * FROM paper_audit WHERE event='cycle_archived'").fetchone()
        self.assertIsNotNone(event, "归档必须留下审计事件")

    def test_archive_returns_the_cycle_row(self):
        returned = self._archive()
        self.assertEqual(self.cycle["id"], returned["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
