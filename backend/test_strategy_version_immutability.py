# -*- coding: utf-8 -*-
"""PR-50：strategy version 不可变性 与 unused draft 清理的边界。

问题根因（修复前）：``hard_delete_unused_draft`` 为了删掉 draft 的私有
version 行，会先 ``DROP TRIGGER trg_strategy_versions_no_delete`` 再删、然后
重建。那是**全局**的 schema 变更：

- 保护窗口内（同一连接、甚至跨连接可见）任何正式历史 version 都可被删除；
- 顺序被打断（进程崩溃）触发器会永久消失，不可变性静默失效；
- 删除路径依赖 DDL 权限，属于"为了让删除通过而临时关掉不变量"。

修复后的规则由数据库与 domain 共同表达，且数据库侧是**默认拒绝**：

- 触发器 ``trg_strategy_versions_no_delete`` 对 ``paper_strategy_versions`` 上的
  任何 ``DELETE`` 都 ``RAISE(ABORT)``，除非**同一事务内**存在一行一次性清除
  授权（``paper_strategy_version_purge_tokens``）且其 definition 仍是
  ``origin='user' AND lifecycle_status='draft'``；
- 因此"validated 回退到 draft、但 ledger/audit 仍引用旧 checksum"的裸 DELETE
  同样被数据库拒绝——``paper_signals`` 等表对 version 没有 FK，只靠 lifecycle
  判断会漏，默认拒绝的触发器不依赖 FK；
- domain 层（lifecycle + ``_historical_reference_exists``）再把范围收窄到
  "从未使用过的 user draft"，并由 ``hard_delete_unused_draft`` 在**调用方事务内**
  插入授权、删除、撤销授权。授权行不落盘、不跨连接可见、不改变任何其它策略。

本文件逐条锁住这两个条件的边界，以及"清理路径不再触碰触发器"。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as registry

DSL = {
    "op": "gt",
    "left": {"op": "field", "name": "close"},
    "right": {"op": "indicator", "name": "ma", "window": 20},
}
BUILTIN_ID = "tq_breakout"
REGISTRY_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_registry.py")


class _RegistryFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pr50-version-immutability-")
        self.path = os.path.join(self.directory, "paper.sqlite3")
        self.conn = self._connect()
        registry.ensure_schema(self.conn)
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - teardown must not mask failures
            pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def _connect(self, busy_timeout=5000):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
        return conn

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

    def _versions(self, strategy_id):
        return [
            row[0] for row in self.conn.execute(
                "SELECT version FROM paper_strategy_versions WHERE strategy_id=? ORDER BY version",
                (strategy_id,),
            )
        ]

    def _heads(self, strategy_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM paper_strategy_version_heads WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()[0]

    def _add_reference(self, strategy_id):
        """制造一条历史引用（与既有 CRUD 测试同式）。"""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS paper_signals(account_id TEXT, strategy_id TEXT)"
        )
        self.conn.execute("INSERT INTO paper_signals VALUES(?,?)", (strategy_id, strategy_id))
        self.conn.commit()

    def _trigger_sql(self):
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (registry.IMMUTABLE_VERSION_TRIGGER,),
        ).fetchone()
        return None if row is None else row[0]

    def _trigger_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
            (registry.IMMUTABLE_VERSION_TRIGGER,),
        ).fetchone()[0]

    def _delete_version_rows(self, strategy_id):
        self.conn.execute(
            "DELETE FROM paper_strategy_versions WHERE strategy_id=?", (strategy_id,),
        )

    def _token_count(self, strategy_id=None):
        sql = "SELECT COUNT(*) FROM paper_strategy_version_purge_tokens"
        args: tuple = ()
        if strategy_id is not None:
            sql += " WHERE strategy_id=?"
            args = (strategy_id,)
        return self.conn.execute(sql, args).fetchone()[0]

    def _grant_token(self, strategy_id):
        """伪造一次性授权（模拟被绕过的 domain 层），用于证明触发器仍拒绝。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_strategy_version_purge_tokens(strategy_id) VALUES(?)",
            (strategy_id,),
        )
        self.conn.commit()


class UnusedDraftCleanupTests(_RegistryFixture):
    """B：真正从未使用过的 user draft 允许 hard delete。"""

    def test_unused_draft_delete_removes_definition_versions_and_heads(self):
        self._draft("pr50_scratch")
        self.assertEqual([1], self._versions("pr50_scratch"))

        result = registry.hard_delete_unused_draft(self.conn, "pr50_scratch")

        self.assertTrue(result["deleted"])
        self.assertIsNone(registry.get("pr50_scratch", conn=self.conn))
        self.assertEqual([], self._versions("pr50_scratch"))
        self.assertEqual(0, self._heads("pr50_scratch"))

    def test_multi_version_draft_without_history_can_still_be_deleted(self):
        self._draft("pr50_scratch_v2")
        registry.save_definition(self.conn, "pr50_scratch_v2", {"description": "second"})
        self.conn.commit()
        self.assertEqual([1, 2], self._versions("pr50_scratch_v2"))

        registry.hard_delete_unused_draft(self.conn, "pr50_scratch_v2")

        self.assertEqual([], self._versions("pr50_scratch_v2"))

    def test_raw_delete_of_unused_draft_is_refused_by_the_database(self):
        """默认拒绝：连"合法可清理"的 draft，裸 DELETE 也失败，唯一出口是 domain 函数。"""
        self._draft("pr50_raw_draft")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_raw_draft")

        self.assertEqual([1], self._versions("pr50_raw_draft"))
        self.assertTrue(
            registry.hard_delete_unused_draft(self.conn, "pr50_raw_draft")["deleted"]
        )

    def test_cleanup_leaves_no_purge_token_behind(self):
        """一次性授权不得落盘，也不得残留。"""
        self._draft("pr50_token_cleanup")
        self.assertEqual(0, self._token_count())

        registry.hard_delete_unused_draft(self.conn, "pr50_token_cleanup")

        self.assertEqual(0, self._token_count("pr50_token_cleanup"))
        self.assertEqual(0, self._token_count())


class FormalVersionProtectionTests(_RegistryFixture):
    """A：已有正式历史意义的版本永远不可删除。"""

    def test_validated_draft_cannot_be_hard_deleted(self):
        self._draft("pr50_validated")
        self._promote("pr50_validated", "validated")

        with self.assertRaisesRegex(ValueError, "only unused user drafts"):
            registry.hard_delete_unused_draft(self.conn, "pr50_validated")

        self.assertIsNotNone(registry.get("pr50_validated", conn=self.conn))
        self.assertEqual([1], self._versions("pr50_validated"))

    def test_active_strategy_cannot_be_hard_deleted(self):
        self._draft("pr50_active")
        self._promote("pr50_active", "validated", "active")

        with self.assertRaisesRegex(ValueError, "only unused user drafts"):
            registry.hard_delete_unused_draft(self.conn, "pr50_active")

        self.assertEqual([1], self._versions("pr50_active"))

    def test_paused_and_archived_cannot_be_hard_deleted(self):
        self._draft("pr50_paused")
        self._promote("pr50_paused", "validated", "active", "paused")
        with self.assertRaises(ValueError):
            registry.hard_delete_unused_draft(self.conn, "pr50_paused")

        self._draft("pr50_archived")
        self._promote("pr50_archived", "archived")
        with self.assertRaises(ValueError):
            registry.hard_delete_unused_draft(self.conn, "pr50_archived")

        self.assertEqual([1], self._versions("pr50_paused"))
        self.assertEqual([1], self._versions("pr50_archived"))

    def test_draft_that_returned_from_validated_with_reference_is_protected(self):
        """lifecycle 回到 draft 但存在历史引用 → 必须拒绝。"""
        self._draft("pr50_roundtrip")
        self._add_reference("pr50_roundtrip")
        self._promote("pr50_roundtrip", "validated")
        self._promote("pr50_roundtrip", "draft")
        self.assertEqual("draft", registry.get("pr50_roundtrip", conn=self.conn).status)

        with self.assertRaisesRegex(ValueError, "historical references"):
            registry.hard_delete_unused_draft(self.conn, "pr50_roundtrip")

        self.assertEqual([1], self._versions("pr50_roundtrip"))

    def test_draft_with_reference_never_leaves_draft_still_protected(self):
        self._draft("pr50_ref_only")
        self._add_reference("pr50_ref_only")

        with self.assertRaisesRegex(ValueError, "historical references"):
            registry.hard_delete_unused_draft(self.conn, "pr50_ref_only")

        self.assertEqual([1], self._versions("pr50_ref_only"))

    def test_builtin_can_never_be_hard_deleted(self):
        with self.assertRaises(ValueError):
            registry.hard_delete_unused_draft(self.conn, BUILTIN_ID)

        self.assertIsNotNone(registry.get(BUILTIN_ID, conn=self.conn))
        self.assertEqual([1], self._versions(BUILTIN_ID))


class DatabaseLevelImmutabilityTests(_RegistryFixture):
    """数据库层必须独立拒绝删除正式版本。"""

    def test_direct_delete_of_builtin_version_is_rejected(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows(BUILTIN_ID)
        self.assertEqual([1], self._versions(BUILTIN_ID))

    def test_direct_delete_of_formal_user_version_is_rejected(self):
        self._draft("pr50_db_formal")
        self._promote("pr50_db_formal", "validated")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_db_formal")
        self.assertEqual([1], self._versions("pr50_db_formal"))

        self._promote("pr50_db_formal", "active")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_db_formal")

    def test_direct_delete_of_archived_version_is_rejected(self):
        self._draft("pr50_db_archived")
        self._promote("pr50_db_archived", "archived")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_db_archived")

    def test_trigger_definition_is_unchanged_across_unused_draft_cleanup(self):
        """清理前后 immutable protection 始终存在，且定义未被改动。"""
        self.assertEqual(1, self._trigger_count())
        before = self._trigger_sql()
        self.assertIn("lifecycle_status='draft'", before)

        self._draft("pr50_trigger_probe")
        registry.hard_delete_unused_draft(self.conn, "pr50_trigger_probe")

        self.assertEqual(1, self._trigger_count())
        self.assertEqual(before, self._trigger_sql())
        # 清理之后保护依然生效。
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows(BUILTIN_ID)

    def test_cleanup_path_never_disables_the_immutability_trigger(self):
        with open(REGISTRY_SOURCE, "r", encoding="utf-8") as handle:
            source = handle.read()

        start = source.index("def hard_delete_unused_draft(")
        end = source.find("\ndef ", start + 10)
        body = source[start:end if end > 0 else len(source)]
        for forbidden in ("DROP TRIGGER", "writable_schema", "foreign_keys"):
            self.assertNotIn(
                forbidden, body,
                f"清理路径不得通过 {forbidden} 绕过不变量",
            )
        # 例外用一次性授权表达，而不是关掉保护。
        self.assertIn("VERSION_PURGE_TOKEN_TABLE", body)
        # 全模块不得用 schema 改写、关 FK、或全局放开删除来绕过不变量。
        self.assertNotIn("writable_schema", source)
        self.assertNotIn("foreign_keys=OFF", source)
        self.assertNotIn("foreign_keys = OFF", source)
        self.assertNotIn("PRAGMA writable_schema", source)

        # 生产代码里 DROP TRIGGER 只允许出现在一次性定义升级器里。
        drops = [
            line.strip() for line in source.splitlines() if "DROP TRIGGER" in line
        ]
        self.assertEqual(1, len(drops), drops)
        installer_start = source.index("def _install_immutable_version_trigger(")
        installer_end = source.find("\ndef ", installer_start + 10)
        installer_body = source[installer_start:installer_end if installer_end > 0 else len(source)]
        self.assertIn(drops[0], installer_body)

    def test_cleanup_revokes_the_token_even_if_the_delete_fails(self):
        """授权必须被 finally 撤销，避免失败后留下悬空授权。"""
        with open(REGISTRY_SOURCE, "r", encoding="utf-8") as handle:
            source = handle.read()

        start = source.index("def hard_delete_unused_draft(")
        end = source.find("\ndef ", start + 10)
        body = source[start:end if end > 0 else len(source)]
        self.assertIn("try:", body)
        self.assertIn("finally:", body)
        revocation = body[body.index("finally:"):]
        self.assertIn("VERSION_PURGE_TOKEN_TABLE", revocation)
        self.assertIn("DELETE FROM", revocation)

    def test_legacy_unconditional_trigger_is_upgraded_by_schema_setup(self):
        """老库残留的"无条件拒绝"触发器在 schema 建立阶段一次性升级。"""
        self._draft("pr50_legacy")
        self.conn.execute("DROP TRIGGER IF EXISTS %s" % registry.IMMUTABLE_VERSION_TRIGGER)
        self.conn.execute(
            """CREATE TRIGGER trg_strategy_versions_no_delete
               BEFORE DELETE ON paper_strategy_versions
               BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END"""
        )
        self.conn.commit()
        legacy = self._trigger_sql()
        self.assertNotIn("lifecycle_status='draft'", legacy)
        # 旧定义连 draft 的私有版本也拒绝删除——这正是修复前必须 DROP 的原因。
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_legacy")

        registry.ensure_schema(self.conn)
        self.conn.commit()

        self.assertIn("lifecycle_status='draft'", self._trigger_sql())
        self.assertTrue(registry.hard_delete_unused_draft(self.conn, "pr50_legacy")["deleted"])
        self.assertEqual([], self._versions("pr50_legacy"))


class DefaultDenyTriggerTests(_RegistryFixture):
    """触发器是默认拒绝的：授权只对 user+draft 生效，且只活在清理事务内。"""

    def test_raw_delete_of_rolled_back_draft_with_ledger_reference_is_refused(self):
        """Codex P1 场景：validated→draft 回退，旧 checksum 仍被 ledger 引用。

        ``paper_signals`` 对 version 没有 FK，所以只看 lifecycle 会把这行当成
        "干净 draft"。默认拒绝的触发器不看 FK、也不只看 lifecycle —— 没有授权
        就没有任何 DELETE 能通过，因此这个裸删除必须在数据库层失败。
        """
        self._draft("pr50_rollback_ref")
        self._add_reference("pr50_rollback_ref")
        self._promote("pr50_rollback_ref", "validated")
        self._promote("pr50_rollback_ref", "draft")
        self.assertEqual("draft", registry.get("pr50_rollback_ref", conn=self.conn).status)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_rollback_ref")

        self.assertEqual([1], self._versions("pr50_rollback_ref"))
        with self.assertRaisesRegex(ValueError, "historical references"):
            registry.hard_delete_unused_draft(self.conn, "pr50_rollback_ref")

    def test_forged_token_cannot_unlock_a_formal_user_version(self):
        """即便伪造授权，definition 不是 draft，数据库仍然拒绝。"""
        self._draft("pr50_forged_formal")
        self._promote("pr50_forged_formal", "validated")
        self._grant_token("pr50_forged_formal")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_forged_formal")

        self.assertEqual([1], self._versions("pr50_forged_formal"))

    def test_forged_token_cannot_unlock_a_builtin_version(self):
        self._grant_token(BUILTIN_ID)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows(BUILTIN_ID)

        self.assertEqual([1], self._versions(BUILTIN_ID))

    def test_purge_token_never_leaks_outside_the_purging_transaction(self):
        """未提交的授权对其它连接不可见，回滚后即消失。"""
        self._draft("pr50_token_leak")
        other = self._connect()
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO paper_strategy_version_purge_tokens(strategy_id) "
                "VALUES(?)",
                ("pr50_token_leak",),
            )
            self.assertEqual(1, self._token_count("pr50_token_leak"))
            self.assertEqual(
                0,
                other.execute(
                    "SELECT COUNT(*) FROM paper_strategy_version_purge_tokens"
                ).fetchone()[0],
            )
        finally:
            self.conn.rollback()
            other.close()

        self.assertEqual(0, self._token_count("pr50_token_leak"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self._delete_version_rows("pr50_token_leak")
        self.assertEqual([1], self._versions("pr50_token_leak"))

    def test_trigger_body_is_default_deny_and_gated_on_user_draft(self):
        sql = self._trigger_sql()
        self.assertIn(registry.VERSION_PURGE_TOKEN_TABLE, sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("origin='user'", sql)
        self.assertIn("lifecycle_status='draft'", sql)
        self.assertIn("BEFORE DELETE ON paper_strategy_versions", sql)


class ConcurrentDraftCleanupTests(_RegistryFixture):
    """并发：清理 draft 的同时，另一连接删除正式版本必须始终失败。"""

    def test_formal_version_delete_fails_while_another_connection_purges_a_draft(self):
        self._draft("pr50_race_draft")
        self._draft("pr50_race_formal")
        self._promote("pr50_race_formal", "validated")
        registry.ensure_schema(self.conn)
        self.conn.commit()

        barrier = threading.Barrier(2)
        outcome: dict = {}

        def purge_draft():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                registry.hard_delete_unused_draft(conn, "pr50_race_draft")
                barrier.wait(timeout=15)
                time.sleep(0.2)  # 保持写事务，覆盖另一连接的删除尝试
                conn.commit()
                outcome["purge"] = "ok"
            except Exception as exc:  # noqa: BLE001 - 记录真实结果供断言
                outcome["purge"] = exc
            finally:
                conn.close()

        def delete_formal_version():
            conn = self._connect()
            try:
                barrier.wait(timeout=15)
                conn.execute(
                    "DELETE FROM paper_strategy_versions WHERE strategy_id='pr50_race_formal'"
                )
                conn.commit()
                outcome["delete"] = "ok"
            except Exception as exc:  # noqa: BLE001
                outcome["delete"] = exc
            finally:
                conn.close()

        threads = [
            threading.Thread(target=purge_draft),
            threading.Thread(target=delete_formal_version),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual("ok", outcome.get("purge"), outcome)
        self.assertIsInstance(outcome.get("delete"), sqlite3.IntegrityError, outcome)
        self.assertIn("immutable", str(outcome["delete"]))
        # 被保护版本原样保留，draft 已被清理。
        self.assertEqual([1], self._versions("pr50_race_formal"))
        self.assertEqual([], self._versions("pr50_race_draft"))


if __name__ == "__main__":
    unittest.main()
