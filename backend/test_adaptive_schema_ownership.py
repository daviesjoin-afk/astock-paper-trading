# -*- coding: utf-8 -*-
"""adaptive 库 schema 所有权：建表编排归 ``adaptive_schema_migrations``。

2026-10-29 收口前的状态：``db_migrate.MIGRATIONS["adaptive_learning"]`` 只有 **1**
步（建 ``schema_version`` 表），而该库 40 余张表由 8 个 owner 在建立连接时各自
``CREATE TABLE IF NOT EXISTS`` 建出来，编排点挂在业务引擎 ``_init_schema`` 里。
于是部署日志那句"adaptive_learning 已是最新 v1"与真实 schema 无关——这和 PR #290
在 paper 库修掉的"升级链漂移 → 升级后缺表"是同一个根因。

本文件钉住收口后的三条契约：
1. 引擎不再逐个点名 owner 的 ``ensure_schema``（编排权已交给 schema owner）；
2. 迁移链里每个 owner 都有独立步骤，版本号连续且可定位；
3. 编排本身幂等、未登记标签 fail closed。
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

import adaptive_schema_migrations as ASM  # noqa: E402
import db_migrate  # noqa: E402


class AdaptiveSchemaOwnershipTests(unittest.TestCase):
    def test_engine_no_longer_names_owner_schema_entries(self):
        """``adaptive_engine`` 里不得再出现 ``<owner>.ensure_schema(conn)``。"""
        source = (BACKEND / "adaptive_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "ensure_schema":
                continue
            owner = getattr(node.func.value, "id", "")
            if owner in _owner_module_names():
                offenders.append(f"{owner}.ensure_schema@L{node.lineno}")
        self.assertEqual(
            offenders, [],
            "建表编排必须走 adaptive_schema_migrations.ensure_feature_tables")

    def test_every_owner_has_its_own_migration_step(self):
        steps = db_migrate.MIGRATIONS["adaptive_learning"]
        versions = [step[0] for step in steps]
        self.assertEqual(list(range(1, len(steps) + 1)), versions,
                         "自适应库迁移版本必须从 1 连续递增")
        descriptions = [step[1] for step in steps]
        for label in ASM.OWNER_LABELS:
            with self.subTest(owner=label):
                self.assertTrue(
                    any(label in text for text in descriptions),
                    f"owner {label} 没有独立迁移步骤，失败时无法定位")
        for version, description, payload in steps:
            if callable(payload):
                continue
            self.assertIsInstance(payload, str, f"步骤 {version} 既不是可调用也不是 SQL")

    def test_owner_steps_are_callable_and_defaulted(self):
        """第 3 步起必须都是可调用对象（不是空 SQL），否则版本只是账面数字。"""
        steps = db_migrate.MIGRATIONS["adaptive_learning"]
        self.assertGreaterEqual(len(steps), 10, "自适应库至少应有 10 步（核心 + 8 owner）")
        for version, _description, payload in steps[1:]:
            with self.subTest(version=version):
                self.assertTrue(callable(payload), f"步骤 {version} 不可调用")

    def test_switching_schema_is_idempotent_on_a_fresh_database(self):
        """连跑两次不得报错，且核心表与 owner 表都真的建出来了。"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "adaptive.sqlite3")
            conn = sqlite3.connect(path)
            try:
                import adaptive_engine

                adaptive_engine.init_schema(conn)
                adaptive_engine.init_schema(conn)  # 幂等
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            finally:
                conn.close()
        for table in ("adaptive_rewards", "adaptive_runs", "news_events",
                      "evolution_params", "adaptive_risk_candidates"):
            with self.subTest(table=table):
                self.assertIn(table, names, f"ensure_current 没建出 {table}")

    def test_every_registered_owner_really_creates_tables(self):
        """登记的 owner 必须真的建表：否则"版本步骤"只是账面数字。"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "adaptive.sqlite3")
            conn = sqlite3.connect(path)
            try:
                before = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                for label in ASM.OWNER_LABELS:
                    ASM.ensure_owner(conn, label)
                after = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            finally:
                conn.close()
        self.assertGreater(len(after - before), 20,
                           "8+ 个 owner 合计建出的表太少，登记表可能已失效")

    def test_unknown_owner_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(KeyError):
                ASM.ensure_owner(conn, "not_a_registered_owner")
        finally:
            conn.close()

    def test_owner_list_is_not_empty(self):
        self.assertGreaterEqual(len(ASM.OWNER_LABELS), 8)
        self.assertEqual(len(set(ASM.OWNER_LABELS)), len(ASM.OWNER_LABELS),
                         "owner 标签不得重复（重复会导致建表顺序失去意义）")


def _owner_module_names() -> set[str]:
    """引擎里对 owner 使用的别名（``risk_evolution`` 等）。"""
    return {
        "risk_evolution", "selection_evolution", "deepseek_advisor", "news_learning",
        "trade_attribution", "ai_analysis", "learning_dataset", "learning_evaluation",
    }


if __name__ == "__main__":
    unittest.main()
