# -*- coding: utf-8 -*-
"""PR-56：hard delete 只属于**从未离开 draft 生命周期**的草稿。

修复前的语义漏洞：``hard_delete_unused_draft`` 只检查

    origin == user AND lifecycle_status == draft AND 无历史(经济/执行)引用

而"历史引用"的 canonical helper 不把**生命周期迁移本身**当作"已离开 scratch"
的证据。于是

    draft -> validated -> draft

的策略（从未进过 cycle、没有任何 signal/order/fill）会被当成"从没用过的草稿"
物理删除，删除路径还会先把 ``strategy_definition_events`` 抹掉——生命周期审计
被自己擦除。

修复后的形式化规则：

    hard_delete_allowed =
        origin == user
        AND current_status == draft
        AND never_left_draft == true
        AND 无历史/经济/执行引用

``never_left_draft`` 由 canonical 谓词 ``_ever_left_draft``（事件表判定，
``NULL -> draft`` 的创建事件不算，``validated -> draft`` 的回退算）表达。

本文件逐条锁住 A~F；G（PR-50 的数据库级保护）由
``test_strategy_version_immutability.py`` 继续覆盖，本文件只做交叉引用回归。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry  # noqa: E402
import strategy_service as SVC  # noqa: E402

DSL = {
    "op": "gt",
    "left": {"op": "field", "name": "close"},
    "right": {"op": "indicator", "name": "ma", "window": 20},
}


class _HardDeleteFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pr56-hard-delete-")
        self.path = os.path.join(self.directory, "paper.sqlite3")
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        registry.ensure_schema(self.conn)
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - teardown must not mask failures
            pass
        shutil.rmtree(self.directory, ignore_errors=True)

    # ---------- helpers ----------

    def _draft(self, strategy_id):
        spec = registry.create_user_definition(
            self.conn, strategy_id, f"{strategy_id} 名称", dsl_ast=DSL,
        )
        self.conn.commit()
        return spec

    def _promote(self, strategy_id, *statuses):
        current = registry.get(strategy_id, conn=self.conn).status
        for status in statuses:
            registry.transition(self.conn, strategy_id, status, expected_status=current)
            current = status
        self.conn.commit()

    def _events(self, strategy_id):
        return registry.lifecycle_events(self.conn, strategy_id)

    def _versions(self, strategy_id):
        return [
            row[0] for row in self.conn.execute(
                "SELECT version FROM paper_strategy_versions WHERE strategy_id=? ORDER BY version",
                (strategy_id,),
            )
        ]

    def _checksums(self, strategy_id):
        return [
            row[0] for row in self.conn.execute(
                "SELECT checksum FROM paper_strategy_versions WHERE strategy_id=? ORDER BY version",
                (strategy_id,),
            )
        ]

    def _token_rows(self):
        return self.conn.execute(
            "SELECT strategy_id FROM paper_strategy_version_purge_tokens"
        ).fetchall()


class ScratchDraftStillDeletableTests(_HardDeleteFixture):
    """A/B：真正的 scratch 草稿（含多版本编辑）仍然可删。"""

    def test_a_true_scratch_draft_is_deletable(self):
        self._draft("pr56_scratch")

        result = registry.hard_delete_unused_draft(self.conn, "pr56_scratch")

        self.assertTrue(result["deleted"])
        self.assertIsNone(registry.get("pr56_scratch", conn=self.conn))
        self.assertEqual([], self._versions("pr56_scratch"))

    def test_b_edited_scratch_draft_v2_v3_is_deletable(self):
        """编辑出 v2/v3 不是"正式使用"——只要没离开过 draft 就能删。"""
        self._draft("pr56_edited")
        registry.save_definition(self.conn, "pr56_edited", {"description": "second"})
        registry.save_definition(self.conn, "pr56_edited", {"description": "third"})
        self.conn.commit()
        self.assertEqual([1, 2, 3], self._versions("pr56_edited"))
        self.assertEqual("draft", registry.get("pr56_edited", conn=self.conn).status)

        self.assertTrue(
            registry.hard_delete_unused_draft(self.conn, "pr56_edited")["deleted"]
        )
        self.assertIsNone(registry.get("pr56_edited", conn=self.conn))
        self.assertEqual([], self._versions("pr56_edited"))

    def test_b2_multiple_draft_versions_are_not_formal_use(self):
        """多版本草稿在删除前仍可继续被编辑（不得禁止编辑）。"""
        self._draft("pr56_editable")
        registry.save_definition(self.conn, "pr56_editable", {"description": "v2"})
        self.conn.commit()

        # 不是"正式使用" ⇒ 谓词为假 ⇒ 仍可删
        self.assertFalse(registry._ever_left_draft(self.conn, "pr56_editable"))
        self.assertTrue(
            registry.hard_delete_unused_draft(self.conn, "pr56_editable")["deleted"]
        )


class LifecycleRollbackTests(_HardDeleteFixture):
    """C/D：任何证明"已正式使用"的生命周期都不得被 hard delete。"""

    def test_c_draft_validated_draft_is_rejected(self):
        self._draft("pr56_rollback")
        self._promote("pr56_rollback", "validated", "draft")
        self.assertEqual("draft", registry.get("pr56_rollback", conn=self.conn).status)
        self.assertEqual([1], self._versions("pr56_rollback"))

        with self.assertRaises(ValueError) as ctx:
            registry.hard_delete_unused_draft(self.conn, "pr56_rollback")

        self.assertIn("historical references", str(ctx.exception))
        self.assertIn("left the draft state", str(ctx.exception))
        # 拒绝后一切照旧
        self.assertIsNotNone(registry.get("pr56_rollback", conn=self.conn))
        self.assertEqual([1], self._versions("pr56_rollback"))
        self.assertTrue(self._events("pr56_rollback"))

    def test_c2_rollback_has_no_cycle_signal_order_fill(self):
        """C 的前提：库里确实没有任何 cycle/signal/order/fill 记录。"""
        self._draft("pr56_clean_rollback")
        self._promote("pr56_clean_rollback", "validated", "draft")
        existing = {
            row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("paper_accounts", "paper_signals", "paper_orders",
                      "paper_cycle_strategy_versions"):
            if table not in existing:
                continue  # registry 单独建库时不含 paper 账本表
            columns = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            column = "strategy_id" if "strategy_id" in columns else "account_id"
            self.assertIsNone(
                self.conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1",
                    ("pr56_clean_rollback",),
                ).fetchone(),
                f"{table} 不应存在记录",
            )
        with self.assertRaises(ValueError):
            registry.hard_delete_unused_draft(self.conn, "pr56_clean_rollback")

    def test_d_every_reachable_formal_lifecycle_stays_protected(self):
        """D：所有可达的"证明正式使用"的生命周期路径。"""
        cases = (
            ("pr56_to_validated", ("validated",)),
            ("pr56_to_active", ("validated", "active")),
            ("pr56_to_paused", ("validated", "active", "paused")),
            ("pr56_to_retiring", ("validated", "active", "retiring")),
            ("pr56_to_archived_via_active", ("validated", "active", "paused", "retiring", "archived")),
            ("pr56_to_archived_from_draft", ("archived",)),
            ("pr56_roundtrip_validated", ("validated", "draft")),
            ("pr56_roundtrip_deep", ("validated", "active", "paused", "retiring", "archived")),
        )
        for strategy_id, path in cases:
            with self.subTest(strategy_id=strategy_id, path=path):
                self._draft(strategy_id)
                self._promote(strategy_id, *path)
                # 非 draft 终态由 status 闸门拦；回到 draft 的由生命周期谓词拦
                with self.assertRaises(ValueError):
                    registry.hard_delete_unused_draft(self.conn, strategy_id)
                self.assertIsNotNone(registry.get(strategy_id, conn=self.conn))
                self.assertEqual([1], self._versions(strategy_id))

    def test_d2_predicate_ignores_the_creation_event(self):
        """创建事件 NULL -> draft 不得被当成"离开过 draft"。"""
        self._draft("pr56_creation_only")
        self.assertFalse(registry._ever_left_draft(self.conn, "pr56_creation_only"))
        events = self._events("pr56_creation_only")
        self.assertEqual(1, len(events))
        self.assertIsNone(events[0]["from_status"])
        self.assertEqual("draft", events[0]["to_status"])


class RejectionPreservationTests(_HardDeleteFixture):
    """E/F：拒绝后定义/版本/事件/checksum 必须原样保留，且不得铸造 purge token。"""

    def test_e_rejection_changes_nothing(self):
        self._draft("pr56_preserve")
        self._promote("pr56_preserve", "validated", "draft")
        definition = registry.get("pr56_preserve", conn=self.conn)
        versions = self._versions("pr56_preserve")
        checksums = self._checksums("pr56_preserve")
        events = self._events("pr56_preserve")

        with self.assertRaises(ValueError):
            registry.hard_delete_unused_draft(self.conn, "pr56_preserve")

        after = registry.get("pr56_preserve", conn=self.conn)
        self.assertEqual(definition.status, after.status)
        self.assertEqual(definition.current_version, after.current_version)
        self.assertEqual(definition.current_checksum, after.current_checksum)
        self.assertEqual(versions, self._versions("pr56_preserve"))
        self.assertEqual(checksums, self._checksums("pr56_preserve"))
        self.assertEqual(events, self._events("pr56_preserve"))
        self.assertGreaterEqual(len(self._events("pr56_preserve")), 3)

    def test_f_no_purge_token_is_minted_on_rejection(self):
        self._draft("pr56_token")
        self._promote("pr56_token", "validated", "draft")
        self.assertEqual([], self._token_rows())

        with self.assertRaises(ValueError):
            registry.hard_delete_unused_draft(self.conn, "pr56_token")

        self.assertEqual([], self._token_rows(), "拒绝路径不得铸造清除授权")
        self.assertEqual([1], self._versions("pr56_token"))

    def test_e2_rollback_draft_can_still_be_edited_after_rejection(self):
        """拒绝删除 ≠ 禁止编辑：回退草稿仍可继续编辑/再次进 validated。"""
        self._draft("pr56_still_editable")
        self._promote("pr56_still_editable", "validated", "draft")

        updated = registry.save_definition(
            self.conn, "pr56_still_editable", {"description": "edited after rejection"},
        )
        self.conn.commit()
        self.assertEqual(2, updated.version)
        self._promote("pr56_still_editable", "validated")
        self.assertEqual("validated", registry.get("pr56_still_editable", conn=self.conn).status)


class DomainMappingTests(_HardDeleteFixture):
    """生命周期历史拒绝必须翻译成既有 domain 异常（HTTP 层只看类型）。"""

    def test_lifecycle_rejection_maps_to_historical_reference_error(self):
        message = (
            "strategy has historical references and must be archived: "
            "lifecycle history shows it already left the draft state (draft -> validated)"
        )
        translated = SVC.translate_registry_error(ValueError(message))
        self.assertIsInstance(translated, SVC.StrategyHistoricalReferenceError)


if __name__ == "__main__":
    unittest.main()
