# -*- coding: utf-8 -*-
"""通用 AI 审核槽位（``ai1`` / ``ai2``）契约测试：T1–T80（含 C1–C6）。

对应四组 PR 的验收清单：

- 「refactor: generalize AI review into configurable AI1/AI2 slots」（T1–T34）
- 「fix(ai-review): distinguish hold, disagreement, and reviewer failure」
  （T35–T52 + C1–C6：dual 结果状态机、reviewer 响应可用性校验、指标分层、apply 门禁）
- 「fix(evolution): isolate reviewer failures from strategy learning」
  （T53–T62：reviewer 可靠性证据与策略学习证据的**分层**；``failed`` 不进
  learning denominator；raw 兼容字段与 learning 字段并存）
- 「feat(ai-review): persist structured disagreement taxonomy」
  （T63–T80：结构化分歧分类法、outcome_detail 审计契约与独立性验证）

**全部离线**：不联网、不调用任何真实付费 AI API。所有"调用"都被
``ai_review_service._call_slot`` 的桩替换；需要真实 key 的地方一律用假字符串。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_review_service as S  # noqa: E402
import dual_ai_tuner as DT  # noqa: E402

ACCOUNT = "tq_breakout"
BASE_WEIGHTS = {"mom": 0.5, "sentiment": 0.5}
EVIDENCE_HASH = "evidence-hash"


def _proposal(weights=None, confidence=86, entry_score_delta=0.0, conditions=None):
    return {
        "account_id": ACCOUNT,
        "reason": "test-evidence",
        "confidence": confidence,
        "weights": dict(weights if weights is not None else BASE_WEIGHTS),
        "entry_score_delta": entry_score_delta,
        "conditions": dict(conditions or {}),
    }


def _response(decision="propose", proposals=None, confidence=86):
    return (
        {
            "decision": decision,
            "confidence": confidence,
            "market_regime": "trend",
            "summary": "stub",
            "proposals": list(proposals if proposals is not None else [_proposal()]),
        },
        11, 7, 9,
    )


class AiReviewSlotTestBase(unittest.TestCase):
    """每个用例一套临时 sqlite 库 + 离线证据桩。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ai-review-slots-")
        self.db_path = os.path.join(self._tmp, "adaptive.sqlite3")
        self.calls = []

    def tearDown(self):
        for name in os.listdir(self._tmp):
            try:
                os.remove(os.path.join(self._tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(self._tmp)
        except OSError:
            pass

    # ── 基础设施 ──
    @property
    def factory(self):
        return self._connect

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def schema(self, script):
        with self.factory() as conn:
            conn.executescript(script)

    def query(self, sql, params=()):
        with self.factory() as conn:
            return conn.execute(sql, params).fetchall()

    def scalar(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0][0] if rows else None

    def slots(self):
        with self.factory() as conn:
            return S.get_slots(conn)

    def configure_slots(self, key1="key-ai1", key2="key-ai2", enabled1=True, enabled2=True):
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key=key1, base_url="https://ai1.example.com/v1",
                          model="model-one", enabled=enabled1, display_name="主审核")
            S.update_slot(conn, "ai2", api_key=key2, base_url="https://ai2.example.com/v1",
                          model="model-two", enabled=enabled2, display_name="备用审核")

    def stub(self, side_effect):
        """替换通用调用层；返回 patcher，供 with 使用。"""
        self.calls = []

        def wrapper(slot_config, system_prompt, user_prompt, max_tokens=1800):
            self.calls.append(slot_config["slot"])
            return side_effect(slot_config, system_prompt, user_prompt, max_tokens)

        return mock.patch.object(S, "_call_slot", side_effect=wrapper)

    def agreeing(self, *args, **kwargs):
        slot = args[0]["slot"] if args else kwargs["slot_config"]["slot"]
        if slot == "ai1":
            return _response(proposals=[_proposal({"mom": 0.51, "sentiment": 0.49})])
        return _response(proposals=[_proposal({"mom": 0.515, "sentiment": 0.485})])

    def run_review(self, review_mode=None, single_reviewer_slot=None, trigger="test"):
        return S.run_ai_review(
            self.factory, self.db_path, [], self._evidence, self._accounts,
            profile={"profile_date": "2026-09-14", "regime": "trend"},
            trigger=trigger, mode="intraday",
            review_mode=review_mode, single_reviewer_slot=single_reviewer_slot,
        )

    @staticmethod
    def _evidence(conn, paper_db_path, snapshot_paths):
        return {"note": "offline-stub"}, EVIDENCE_HASH

    @staticmethod
    def _accounts(paper_db_path):
        return [{"account_id": ACCOUNT, "weights": dict(BASE_WEIGHTS), "conditions": {}}]

    def audit(self, run_id):
        rows = self.query("SELECT * FROM dual_ai_tuning_runs WHERE id=?", (run_id,))
        self.assertEqual(1, len(rows), "审计行缺失")
        return rows[0]


# ───────────────────────── T1–T4：schema 与迁移 ─────────────────────────

class SchemaTests(AiReviewSlotTestBase):
    def test_t1_ensure_schema_creates_tables_and_is_idempotent(self):
        with self.factory() as conn:
            S.ensure_schema(conn)
            conn.commit()
            first = dict(S.get_review_settings(conn))
            S.ensure_schema(conn)  # 第二次必须幂等
            conn.commit()
            second = dict(S.get_review_settings(conn))
        self.assertEqual("dual", first["review_mode"])
        self.assertEqual("ai1", first["single_reviewer_slot"])
        self.assertEqual(first["review_mode"], second["review_mode"])
        self.assertEqual(first["single_reviewer_slot"], second["single_reviewer_slot"])
        self.assertEqual(
            1, len(self.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_provider_slots'")))

    def test_t2_slot_primary_key_rejects_unknown_slot(self):
        with self.factory() as conn:
            S.ensure_schema(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ai_provider_slots(slot,updated_at) VALUES('ai3','now')")
        with self.assertRaises(ValueError):
            S.resolve_slot("ai3")

    def test_t3_migration_backfills_mimo_to_ai1_and_deepseek_to_ai2_without_drop(self):
        self.schema(
            "CREATE TABLE dual_ai_api_keys(provider TEXT PRIMARY KEY, api_key TEXT NOT NULL,"
            " base_url TEXT, model TEXT, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);"
        )
        with self.factory() as conn:
            conn.execute("INSERT INTO dual_ai_api_keys VALUES(?,?,?,?,?,?)",
                         ("mimo", "legacy-mimo-key", "https://api.mimo.example.com/v1/", "mimo-x", 1, "2026-01-01"))
            conn.execute("INSERT INTO dual_ai_api_keys VALUES(?,?,?,?,?,?)",
                         ("deepseek", "legacy-ds-key", "https://api.ds.example.com", "ds-x", 1, "2026-01-01"))
            conn.commit()
        with self.factory() as conn:
            migrated = S.migrate_legacy_providers(conn)
            conn.commit()
        self.assertEqual({"ai1": "mimo", "ai2": "deepseek"}, migrated)
        slots = self.slots()
        self.assertTrue(slots["ai1"]["configured"])
        self.assertTrue(slots["ai2"]["configured"])
        self.assertEqual("https://api.mimo.example.com/v1", slots["ai1"]["base_url"])
        # 旧表必须保留（只读，不 DROP）：回滚/审计仍需要它
        self.assertTrue(self.query(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dual_ai_api_keys'"))

    def test_t4_migration_is_one_shot_and_never_resurrects_a_cleared_key(self):
        self.schema(
            "CREATE TABLE dual_ai_api_keys(provider TEXT PRIMARY KEY, api_key TEXT NOT NULL,"
            " base_url TEXT, model TEXT, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);"
        )
        with self.factory() as conn:
            conn.execute("INSERT INTO dual_ai_api_keys VALUES(?,?,?,?,?,?)",
                         ("mimo", "legacy-mimo-key", "https://api.mimo.example.com/v1", "mimo-x", 1, "2026-01-01"))
            conn.commit()
        with self.factory() as conn:
            self.assertEqual({"ai1": "mimo"}, S.migrate_legacy_providers(conn))
            conn.commit()
        # 显式清空后，再次迁移绝不能把旧凭据悄悄复活
        with self.factory() as conn:
            S.update_slot(conn, "ai1", clear_api_key=True)
            conn.commit()
        with self.factory() as conn:
            self.assertEqual({}, S.migrate_legacy_providers(conn))
            conn.commit()
        self.assertFalse(self.slots()["ai1"]["configured"])
        # 用户管理过的槽位，迁移整体跳过，不会被旧值覆盖
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="user-owned-key", model="user-model")
            conn.commit()
        with self.factory() as conn:
            self.assertEqual({}, S.migrate_legacy_providers(conn))
            conn.commit()
        self.assertEqual("user-model", self.slots()["ai1"]["model"])


# ───────────────────────── T5–T7：槽位隔离 ─────────────────────────

class IsolationTests(AiReviewSlotTestBase):
    def test_t5_updating_ai1_does_not_touch_ai2(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="rotated-key", base_url="https://new.example.com",
                          model="model-one-v2", enabled=False, timeout_seconds=77)
            conn.commit()
        slots = self.slots()
        self.assertEqual("model-one-v2", slots["ai1"]["model"])
        self.assertEqual(77, slots["ai1"]["timeout_seconds"])
        self.assertFalse(slots["ai1"]["enabled"])
        self.assertEqual("model-two", slots["ai2"]["model"])
        self.assertEqual(40, slots["ai2"]["timeout_seconds"])
        self.assertTrue(slots["ai2"]["enabled"])
        self.assertEqual("https://ai2.example.com/v1", slots["ai2"]["base_url"])

    def test_t6_clear_api_key_on_ai1_leaves_ai2_intact(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai1", clear_api_key=True)
            conn.commit()
        slots = self.slots()
        self.assertFalse(slots["ai1"]["configured"])
        self.assertEqual("", slots["ai1"]["key_preview"])
        self.assertTrue(slots["ai2"]["configured"])
        with self.factory() as conn:
            self.assertEqual("key-ai2", S.get_slot_config(conn, "ai2")["api_key"])

    def test_t7_slots_have_independent_display_names_and_models(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai2", display_name="财报审阅", model="model-two-v9")
            conn.commit()
        slots = self.slots()
        self.assertEqual("主审核", slots["ai1"]["display_name"])
        self.assertEqual("财报审阅", slots["ai2"]["display_name"])
        self.assertEqual("model-one", slots["ai1"]["model"])
        self.assertEqual("model-two-v9", slots["ai2"]["model"])


# ───────────────────────── T8–T10：Key 脱敏与清除语义 ─────────────────────────

class KeyRedactionTests(AiReviewSlotTestBase):
    def test_t8_public_view_never_returns_plaintext_key(self):
        secret1 = "fake-slot-key-ai1-0001"
        secret2 = "fake-slot-key-ai2-0002"
        self.configure_slots(key1=secret1, key2=secret2)
        blob = json.dumps(self.slots(), ensure_ascii=False)
        self.assertNotIn(secret1, blob)
        self.assertNotIn(secret2, blob)
        self.assertTrue(self.slots()["ai1"]["configured"])
        self.assertTrue(self.slots()["ai1"]["key_preview"])
        self.assertIn("****", self.slots()["ai1"]["key_preview"])

    def test_t9_empty_api_key_preserves_old_key(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="", model="model-one-v3")
            conn.commit()
            self.assertEqual("key-ai1", S.get_slot_config(conn, "ai1")["api_key"])
        self.assertEqual("model-one-v3", self.slots()["ai1"]["model"])

    def test_t10_clear_api_key_wins_over_a_supplied_key(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="ignored-new-key", clear_api_key=True)
            conn.commit()
            self.assertEqual("", S.get_slot_config(conn, "ai1")["api_key"])
        self.assertFalse(self.slots()["ai1"]["configured"])


# ───────────────────────── T11–T13：通用调用层 ─────────────────────────

class GenericCallLayerTests(unittest.TestCase):
    def test_t11_base_url_normalization(self):
        self.assertEqual("https://a.example.com/v1", S.normalize_base_url("https://a.example.com/v1/"))
        self.assertEqual("https://a.example.com/v1", S.normalize_base_url("  https://a.example.com/v1  "))
        self.assertEqual("https://a.example.com/v1/chat/completions",
                         S.chat_completions_url("https://a.example.com/v1"))
        self.assertEqual("https://a.example.com/v1/chat/completions",
                         S.chat_completions_url("https://a.example.com/v1/"))
        self.assertEqual("https://a.example.com/chat/completions",
                         S.chat_completions_url("https://a.example.com"))
        self.assertEqual("https://a.example.com/v1/chat/completions",
                         S.chat_completions_url("https://a.example.com/v1/chat/completions"))
        with self.assertRaises(ValueError):
            S.chat_completions_url("   ")

    def test_t12_request_body_is_slot_agnostic_and_vendor_neutral(self):
        common = {"model": "m", "api_key": "k", "base_url": "https://a.example.com/v1",
                  "timeout_seconds": 30}
        body_a = S.build_request_body(dict(common, slot="ai1"), "sys", "user")
        body_b = S.build_request_body(dict(common, slot="ai2"), "sys", "user")
        self.assertEqual(body_a, body_b, "请求体不得因槽位身份而不同")
        self.assertEqual({"model", "messages", "response_format", "max_tokens", "stream"},
                         set(body_a))
        # 厂商专属字段必须彻底消失（历史上 DeepSeek 会被额外塞 thinking）
        self.assertNotIn("thinking", body_a)
        self.assertNotIn("provider", json.dumps(body_a))
        self.assertEqual([{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}],
                         body_a["messages"])

    def test_t13_missing_model_fails_closed(self):
        with self.assertRaises(RuntimeError) as ctx:
            S.build_request_body({"slot": "ai1", "model": ""}, "s", "u")
        self.assertIn("ai1_model_missing", str(ctx.exception))


# ───────────────────────── T14–T18：SINGLE 语义 ─────────────────────────

class SingleModeTests(AiReviewSlotTestBase):
    def test_t14_single_mode_uses_only_selected_slot_and_never_claims_consensus(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai2")
            conn.commit()
        with self.stub(self.agreeing):
            result = self.run_review()
        self.assertEqual(["ai2"], self.calls, "单AI模式只能调用选定槽位")
        self.assertEqual("single_review", result["mode"])
        self.assertEqual("single_review", result["status"])
        self.assertFalse(result["consensus"])
        self.assertNotEqual("consensus", result["status"])
        self.assertEqual("ai2", result["single_reviewer_slot"])
        self.assertEqual("not_selected", result["reviewers"]["ai1"]["status"])
        self.assertEqual("completed", result["reviewers"]["ai2"]["status"])

    def test_t15_single_mode_audit_row_is_invisible_to_the_consensus_apply_gate(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai1")
            conn.commit()
        with self.stub(self.agreeing):
            result = self.run_review()
        row = self.audit(result["id"])
        self.assertEqual("single_review", row["status"])
        self.assertEqual("single_review", row["consensus_result"])
        self.assertIsNone(row["merged_proposals"], "单AI结果绝不能写入 merged_proposals")
        self.assertEqual("single_review", row["result_mode"])
        self.assertEqual("single", row["review_mode"])

    def test_t16_single_mode_fails_closed_when_selected_slot_unconfigured(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai2", clear_api_key=True)
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai2")
            conn.commit()
        with self.stub(self.agreeing):
            result = self.run_review()
        self.assertEqual([], self.calls, "未配置时不得发起任何调用")
        self.assertEqual("single_slot_not_configured", result["status"])
        self.assertEqual("single_review", result["mode"])
        self.assertFalse(result["consensus"])
        self.assertIsNone(self.audit(result["id"])["merged_proposals"])

    def test_t17_single_mode_fails_closed_when_selected_slot_disabled(self):
        self.configure_slots(enabled2=False)
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai2")
            conn.commit()
        with self.stub(self.agreeing):
            result = self.run_review()
        self.assertEqual([], self.calls)
        self.assertEqual("single_slot_disabled", result["status"])
        self.assertFalse(result["consensus"])

    def test_t18_single_mode_reports_failed_call_without_consensus(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai1")
            conn.commit()

        def boom(*_args, **_kwargs):
            raise RuntimeError("upstream-500")

        with self.stub(boom):
            result = self.run_review()
        self.assertEqual("single_review_failed", result["status"])
        self.assertFalse(result["consensus"])
        self.assertEqual("failed", result["reviewers"]["ai1"]["status"])
        row = self.audit(result["id"])
        self.assertEqual("single_review_failed", row["status"])
        self.assertIsNone(row["merged_proposals"])


# ───────────────────────── T19–T21：DUAL 语义与"绝不降级" ─────────────────────────

class DualModeTests(AiReviewSlotTestBase):
    def test_t19_dual_mode_calls_both_slots_and_merges_consensus(self):
        self.configure_slots()
        with self.factory() as conn:
            self.assertEqual("dual", S.get_review_settings(conn)["review_mode"])
        with self.stub(self.agreeing):
            result = self.run_review()
        self.assertEqual(["ai1", "ai2"], sorted(self.calls))
        self.assertEqual("dual_review", result["mode"])
        self.assertEqual("consensus", result["status"])
        self.assertTrue(result["consensus"])
        self.assertTrue(result["proposals"])
        self.assertEqual("dual_ai_agreement", result["proposals"][0]["consensus_source"])
        row = self.audit(result["id"])
        self.assertEqual("consensus", row["status"])
        self.assertIsNotNone(row["merged_proposals"])

    def test_t20_dual_mode_never_degrades_to_single_when_a_slot_is_unusable(self):
        self.configure_slots()
        with self.factory() as conn:
            S.update_slot(conn, "ai1", clear_api_key=True)
            conn.commit()
        with self.stub(self.agreeing):
            result = self.run_review()
        self.assertEqual([], self.calls, "双AI缺任一个都不得只调用剩下的那个")
        self.assertEqual("ai1_not_configured", result["status"])
        self.assertEqual("dual_review", result["mode"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])

    def test_t20b_dual_mode_never_degrades_when_a_slot_is_disabled(self):
        self.configure_slots(enabled2=False)
        with self.stub(self.agreeing):
            result = self.run_review()
        self.assertEqual([], self.calls)
        self.assertEqual("ai2_disabled", result["status"])
        self.assertFalse(result["consensus"])

    def test_t21_dual_mode_one_failed_call_yields_failed_and_no_fallback(self):
        """运行失败是 ``failed``，不是"意见不一致"。

        （本用例在引入 dual 结果状态机前断言 ``no_consensus``；那个期望本身编码了
        被修复的分类 bug —— reviewer 调用失败与"双方成功但语义分歧"是两回事。）
        """
        self.configure_slots()

        def half_fail(slot_config, system_prompt, user_prompt, max_tokens=1800):
            if slot_config["slot"] == "ai1":
                raise RuntimeError("ai1-down")
            return _response(proposals=[_proposal({"mom": 0.515, "sentiment": 0.485})])

        with self.stub(half_fail):
            result = self.run_review()
        self.assertEqual(["ai1", "ai2"], sorted(self.calls))
        self.assertEqual("dual_review", result["mode"])
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        self.assertEqual("failed", result["reviewers"]["ai1"]["status"])
        # 失败原因必须指明是哪个 slot（且不含凭据/查询串）
        self.assertIn("ai1 reviewer failed", result["reason"])
        self.assertNotIn("ai2 reviewer failed", result["reason"])


# ───────────────────────── T22：审计快照 ─────────────────────────

class AuditSnapshotTests(AiReviewSlotTestBase):
    def test_t22_audit_snapshots_config_at_execution_time_and_never_the_key(self):
        secret = "fake-slot-key-audit-0003"
        self.configure_slots(key1=secret, key2=secret + "-2")
        with self.stub(self.agreeing):
            result = self.run_review()
        row = self.audit(result["id"])
        snapshot = json.loads(row["config_snapshot"])
        self.assertNotIn(secret, row["config_snapshot"])
        self.assertNotIn("api_key", json.dumps(snapshot))
        self.assertEqual("model-one", snapshot["ai1"]["model"])
        self.assertEqual("https://ai1.example.com/v1", snapshot["ai1"]["base_url"])
        self.assertEqual("主审核", snapshot["ai1"]["display_name"])
        self.assertEqual("model-two", snapshot["ai2"]["model"])
        reviewers = json.loads(row["reviewers"])
        self.assertEqual("completed", reviewers["ai1"]["status"])
        self.assertEqual("completed", reviewers["ai2"]["status"])
        # recent_runs 也必须给出同一份通用视图
        with self.factory() as conn:
            runs = S.recent_runs(conn, 5)
        self.assertEqual(result["id"], runs[0]["id"])
        self.assertEqual("dual_review", runs[0]["result_mode"])
        self.assertEqual("ai1", runs[0]["reviewers"]["ai1"]["slot"])


# ───────────────────────── T23–T24：应用边界与历史门面 ─────────────────────────

class ApplyBoundaryTests(AiReviewSlotTestBase):
    def test_t23_single_review_run_cannot_pass_the_consensus_apply_gate(self):
        import evolution_apply

        self.configure_slots()
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai1")
            conn.commit()
        with self.stub(self.agreeing):
            single = self.run_review()
        with self.assertRaises(ValueError) as ctx:
            evolution_apply.apply_tuner_proposals(
                self.factory, os.path.join(self._tmp, "unused-paper.sqlite3"),
                single["id"], confirmed=True)
        self.assertIn("consensus", str(ctx.exception))
        # 负向对照：同一行一旦被改成 consensus，拒绝原因就不再是状态门禁
        # —— 证明上面的拒绝确实来自 review_mode 无法绕过的门禁，而不是空转。
        with self.factory() as conn:
            conn.execute("UPDATE dual_ai_tuning_runs SET status='consensus' WHERE id=?", (single["id"],))
            conn.commit()
        with self.assertRaises(ValueError) as ctx2:
            evolution_apply.apply_tuner_proposals(
                self.factory, os.path.join(self._tmp, "unused-paper.sqlite3"),
                single["id"], confirmed=True)
        self.assertNotIn("只有 consensus 状态的运行可以应用", str(ctx2.exception))


class LegacyFacadeTests(AiReviewSlotTestBase):
    def test_t24_legacy_facade_maps_aliases_and_keeps_the_old_contract(self):
        with self.factory() as conn:
            DT.ensure_schema(conn)
            DT.update_api_key(conn, "mimo", api_key="legacy-alias-key", model="model-one",
                              base_url="https://ai1.example.com/v1", enabled=True)
            DT.update_api_key(conn, "deepseek", api_key="legacy-alias-key-ds", model="model-two",
                              base_url="https://ai2.example.com/v1", enabled=True)
            conn.commit()
        # 别名写入必须落到 ai1 / ai2，且旧视图仍按 mimo/deepseek 返回
        with self.factory() as conn:
            keys = DT.get_api_keys(conn)
        self.assertTrue(keys["mimo"]["configured"])
        self.assertTrue(keys["deepseek"]["configured"])
        self.assertTrue(self.slots()["ai1"]["configured"])
        with self.factory() as conn:
            with self.assertRaises(ValueError):
                DT.update_api_key(conn, "not-a-provider", api_key="x")

        with self.stub(self.agreeing):
            legacy = DT.run_dual_ai_tuning(
                self.factory, self.db_path, [], self._evidence, self._accounts,
                profile={"profile_date": "2026-09-14", "regime": "trend"},
                trigger="legacy", mode="intraday",
            )
        self.assertEqual("consensus", legacy["status"])
        self.assertTrue(legacy["consensus"])
        self.assertTrue(legacy["merged_proposals"])
        self.assertEqual("completed", legacy["mimo"]["status"])
        self.assertEqual("completed", legacy["deepseek"]["status"])
        self.assertEqual("ai-review-v1", legacy["version"])
        with self.factory() as conn:
            status = DT.dual_ai_status(conn)
        self.assertTrue(status["mimo_ready"])
        self.assertTrue(status["deepseek_ready"])
        self.assertTrue(status["dual_ready"])
        self.assertIn("providers", status)
        self.assertIn("ai1", status["slots"])


# ───────────────────────── 端点契约（并入 T14–T24 之外的接口面）─────────────────────────

class SettingsEndpointContractTests(unittest.TestCase):
    """设置页 AI 接口必须存在，且写方法都是需要确认的写路由。"""

    def test_ai_review_endpoints_are_registered(self):
        import main
        paths = main.app.openapi().get("paths") or {}
        expected = {
            "/api/settings/ai-review": {"get", "put"},
            "/api/settings/ai-review/slots/{slot}": {"put"},
            "/api/settings/ai-review/slots/{slot}/test": {"post"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths, "缺少 AI 槽位接口 %s" % path)
            available = {m.lower() for m in paths[path]}
            self.assertTrue(methods <= available,
                            "%s 缺少方法 %s（现有 %s）" % (path, methods - available, available))


# ───────── T25–T29：评审回归（调度预检模式 / 旧 env 兜底 / 就绪口径 / 响应形状）─────────


class ReviewRegressionTests(AiReviewSlotTestBase):
    """2026-09-14 代码评审发现的回归护栏。

    每条都对应一个真实缺陷：

    - 调度预检不认 ``single`` 模式（合法单槽配置被永久误判 skipped）；
    - 只靠 ``MIMO_*`` / ``DEEPSEEK_*`` 环境变量配置的部署升级后静默失去凭据；
    - "就绪"只判 Key+启用，缺 ``base_url`` / ``model`` 也报告就绪；
    - 上游返回合法 JSON 但不是对象时，``_call_reviewer`` 抛异常逃逸（单AI裸调用
      会变成服务端 500 且**不写审计行**）。
    """

    def test_t25_readiness_requires_endpoint_and_model(self):
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="fake-partial-key", enabled=True)
        view = self.slots()["ai1"]
        self.assertTrue(view["configured"])
        self.assertTrue(view["enabled"])
        self.assertFalse(view["ready"], "只有 Key+启用、缺地址/模型时不得报告就绪")
        with self.factory() as conn:
            settings = S.review_settings_view(conn)
        self.assertFalse(settings["single_ready"])
        self.assertFalse(settings["dual_ready"])
        with self.factory() as conn:
            S.update_slot(conn, "ai1", base_url="https://ai1.example.com/v1", model="model-one")
            settings = S.review_settings_view(conn)
        self.assertTrue(settings["single_ready"], "补齐地址与模型后单AI必须就绪")
        self.assertFalse(settings["dual_ready"], "另一槽位仍为空，双AI不得就绪")

    def test_t26_scheduler_preflight_follows_the_configured_review_mode(self):
        import adaptive_engine as AE

        # single：只看所选槽位——另一个槽位没配也**必须**放行
        ready, reason = AE.ai_review_preflight(
            {"review_mode": "single", "single_ready": True, "dual_ready": False})
        self.assertTrue(ready, "single 模式不得因为另一槽位未配置而被跳过")
        self.assertIn("单AI", reason)
        # dual：两个槽位缺一不可
        ready, _ = AE.ai_review_preflight(
            {"review_mode": "dual", "single_ready": True, "dual_ready": False})
        self.assertFalse(ready, "dual 模式缺槽位必须拦截")
        # 模式缺失/未知：按更严格的 dual 处理，绝不放宽
        self.assertFalse(AE.ai_review_preflight({"single_ready": True})[0])
        self.assertTrue(AE.ai_review_preflight({"review_mode": "dual", "dual_ready": True})[0])

    def test_t27_legacy_environment_is_bootstrapped_once_into_persisted_slots(self):
        legacy_env = {
            "MIMO_API_KEY": "fake-legacy-mimo-key",
            "MIMO_BASE_URL": "https://legacy-mimo.example.com/v1/",
            "MIMO_MODEL": "legacy-model-one",
            "MIMO_TIMEOUT_SECONDS": "50",
        }
        with mock.patch.dict(os.environ, legacy_env, clear=True):
            with self.factory() as conn:
                cfg = S.get_slot_config(conn, "ai1")
            self.assertEqual("fake-legacy-mimo-key", cfg["api_key"])
            self.assertEqual("https://legacy-mimo.example.com/v1", cfg["base_url"])
            self.assertEqual("legacy-model-one", cfg["model"])
            self.assertEqual(50, cfg["timeout_seconds"])
            # 显式设过的值一律沿用；bootstrap 会把它**落库**成槽位配置
            self.assertEqual("database", cfg["source"])
        self.assertEqual(1, self.scalar("SELECT COUNT(*) FROM ai_provider_slots WHERE slot='ai1'"))
        self.assertTrue(self.slots()["ai1"]["ready"])

    def test_t28_new_env_wins_and_a_db_row_disables_env_entirely(self):
        env = {
            "AI_SLOT_AI1_API_KEY": "fake-new-style-key",
            "MIMO_API_KEY": "fake-legacy-mimo-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.factory() as conn:
                self.assertEqual("fake-new-style-key", S.get_slot_config(conn, "ai1")["api_key"])
        # 只要写过一行（这里随后显式清空 Key），环境变量就必须彻底失效
        with mock.patch.dict(os.environ, env, clear=True):
            with self.factory() as conn:
                S.update_slot(conn, "ai1", api_key="fake-temp-key",
                              base_url="https://ai1.example.com/v1", model="model-one")
                S.update_slot(conn, "ai1", clear_api_key=True)
            with self.factory() as conn:
                cfg = S.get_slot_config(conn, "ai1")
            self.assertEqual("", cfg["api_key"], "显式清除的 Key 绝不能被环境变量复活")
            self.assertEqual("database", cfg["source"])

    def test_t29_non_object_json_is_a_structured_failure_and_single_mode_still_audits(self):
        cfg = {"slot": "ai1", "display_name": "主审核", "model": "model-one",
               "api_key": "fake-slot-key", "enabled": True}
        with mock.patch.object(S, "_call_slot", return_value=([], 1, 1, 5)):
            reviewer = S._call_reviewer("ai1", cfg, "sys", "user")
        self.assertEqual("failed", reviewer["status"], "_call_reviewer 绝不允许抛异常")
        self.assertIn("response_not_object", reviewer["error"])

        self.configure_slots()
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai1")
        with mock.patch.object(S, "_call_slot", return_value=([], 1, 1, 5)):
            outcome = self.run_review()
        self.assertEqual(S.MODE_SINGLE_REVIEW, outcome["mode"])
        self.assertEqual("single_review_failed", outcome["status"])
        self.assertFalse(outcome["consensus"])
        # 关键：异常已被归一为结构化失败，所以审计行必须落库（而不是 500 无记录）
        row = self.audit(outcome["id"])
        self.assertEqual("single_review_failed", row["status"])
        self.assertIn("response_not_object", str(row["reviewers"] or ""))


    def test_t30_legacy_key_only_deployment_keeps_pre_upgrade_behaviour(self):
        """阻塞回归：老部署**只设了厂商 API Key**（无 URL/model），升级后必须照旧可用。

        基线 ``dual_ai_tuner`` 对每个厂商自带默认端点/模型，所以"只有 Key"过去也能
        跑；通用化后若不 bootstrap，这类部署会静默变成 ready=false。
        """
        with mock.patch.dict(os.environ, {"MIMO_API_KEY": "fake-legacy-mimo-key"}, clear=True):
            with self.factory() as conn:
                S.ensure_schema(conn)
            slot = self.slots()["ai1"]
        self.assertEqual("https://api.mimo.ai/v1", slot["base_url"], "旧版 MiMo 默认端点必须补齐")
        self.assertEqual("mimo-v1", slot["model"], "旧版 MiMo 默认模型必须补齐")
        self.assertTrue(slot["ready"], "只配 Key 的老部署升级后仍必须就绪")

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-legacy-ds-key"}, clear=True):
            with self.factory() as conn:
                S.ensure_schema(conn)
            slot2 = self.slots()["ai2"]
        self.assertEqual("https://api.deepseek.com", slot2["base_url"])
        self.assertEqual("deepseek-v4-flash", slot2["model"])
        self.assertTrue(slot2["ready"])
        # 两个槽位都补齐后，双AI模式也应当就绪
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.factory() as conn:
                self.assertTrue(S.review_settings_view(conn)["dual_ready"])

    def test_t31_new_style_env_takes_precedence_and_is_not_persisted(self):
        env = {
            "AI_SLOT_AI1_API_KEY": "fake-new-style-key",
            "MIMO_API_KEY": "fake-legacy-mimo-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.factory() as conn:
                S.ensure_schema(conn)
                cfg = S.get_slot_config(conn, "ai1")
        self.assertEqual("fake-new-style-key", cfg["api_key"])
        self.assertEqual("environment", cfg["source"], "新式 env 提供 Key 时不得落库")
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM ai_provider_slots WHERE slot='ai1'"))
        self.assertEqual("", cfg["base_url"], "更不允许把厂商默认端点混进新式配置")

    def test_t32_connection_test_requires_a_well_formed_response(self):
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="fake-slot-key",
                          base_url="https://ai1.example.com/v1", model="model-one", enabled=True)
        # 合法 JSON 但不是对象 → 协议不合法：ok 必须为 false，并给出明确原因
        with mock.patch.object(S, "_call_slot", return_value=([], 1, 1, 5)):
            with self.factory() as conn:
                result = S.test_slot(conn, "ai1")
        self.assertFalse(result["ok"], "协议不合法的响应绝不能被显示成连接成功")
        self.assertFalse(result["response_ok"])
        self.assertEqual("invalid_response_schema", result["error"])
        # 合法对象 → ok 为 true
        with mock.patch.object(S, "_call_slot", return_value=({"ok": True}, 1, 1, 7)):
            with self.factory() as conn:
                ok_result = S.test_slot(conn, "ai1")
        self.assertTrue(ok_result["ok"])
        self.assertTrue(ok_result["response_ok"])
        self.assertIsNone(ok_result["error"])

    def test_t33_base_url_is_validated_on_save(self):
        # 全部用保留域名（example.com）承载 host，避免把真实远端主机名写进用例
        for value in ["abc", "123", "://wrong", "ftp://example.com/v1", "http://",
                      "https://example.com/a b"]:
            with self.assertRaises(ValueError, msg="%r 必须被拒绝" % value):
                with self.factory() as conn:
                    S.update_slot(conn, "ai1", api_key="fake-slot-key",
                                  base_url=value, model="model-one")
        with self.factory() as conn:
            S.update_slot(conn, "ai1", api_key="fake-slot-key",
                          base_url="https://ai1.example.com/v1/", model="model-one")
        self.assertEqual("https://ai1.example.com/v1", self.slots()["ai1"]["base_url"])
        # 直接粘贴完整接口地址也接受（归一化掉 /chat/completions 后缀）
        with self.factory() as conn:
            S.update_slot(conn, "ai1", base_url="https://ai1.example.com/v1/chat/completions")
        self.assertEqual("https://ai1.example.com/v1", self.slots()["ai1"]["base_url"])
        # 空串 = 清空该字段（允许保存，但 readiness 必须转为未就绪）
        with self.factory() as conn:
            S.update_slot(conn, "ai1", base_url="")
        self.assertEqual("", self.slots()["ai1"]["base_url"])
        self.assertFalse(self.slots()["ai1"]["ready"])

    def test_t34_readiness_rejects_a_non_url_base_url_from_legacy_rows(self):
        # 直接写库模拟历史脏数据（绕过保存校验）：readiness 也必须拦下
        with self.factory() as conn:
            S.ensure_schema(conn)
            conn.execute(
                "INSERT INTO ai_provider_slots(slot,display_name,api_key,base_url,model,"
                "enabled,timeout_seconds,updated_at) VALUES('ai1','AI 1','fake-slot-key',"
                "'abc','model-one',1,40,'2026-01-01')")
            conn.commit()
        slot = self.slots()["ai1"]
        self.assertTrue(slot["configured"])
        self.assertFalse(slot["ready"], "非 URL 的 base_url 不得报告就绪")
        with self.factory() as conn:
            self.assertFalse(S.review_settings_view(conn)["single_ready"])


# ───────────────── T35–T52：dual 结果状态机（outcome taxonomy）─────────────────

def _decide(a1, a2, proposals1=None, proposals2=None, confidence=86):
    """两端各给一个 decision 的桩。``proposals=None`` 表示该端提一个合法提案。"""
    def side_effect(slot_config, system_prompt, user_prompt, max_tokens=1800):
        if slot_config["slot"] == "ai1":
            return _response(decision=a1, proposals=proposals1, confidence=confidence)
        return _response(decision=a2, proposals=proposals2, confidence=confidence)
    return side_effect


def _reviewer(status, decision=None):
    return {"slot": "x", "status": status, "decision": decision, "error": None}


def _raw_response(payload):
    """按**原样**返回 ``payload`` 的槽位桩。

    ``_response`` 会把 ``proposals`` 强制 ``list(...)``，因此构造"畸形提案"这类
    协议级响应必须绕开它 —— 否则测不到被校验的边界。
    """
    return ({**payload, "confidence": 86, "market_regime": "trend", "summary": "stub"},
            11, 7, 9)


def _raw_by_slot(payloads):
    """每个槽位返回不同原样 payload 的桩。"""
    def side_effect(slot_config, system_prompt, user_prompt, max_tokens=1800):
        return _raw_response(payloads[slot_config["slot"]])
    return side_effect


class OutcomeClassifierContractTests(unittest.TestCase):
    """纯函数 ``classify_dual_review_outcome`` 的契约（不碰 DB、不碰网络）。"""

    def _classify(self, reviewers, decisions, consensus=False, merged=None):
        return S.classify_dual_review_outcome(reviewers, decisions, consensus, merged or [])

    def test_c1_hold_hold_is_both_hold_not_a_merged_signal(self):
        reviewers = {"ai1": _reviewer("completed", "hold"), "ai2": _reviewer("completed", "hold")}
        self.assertEqual(S.OUTCOME_BOTH_HOLD, self._classify(reviewers, ["hold", "hold"]))

    def test_c2_empty_merged_never_implies_both_hold(self):
        # hold/propose 与 propose/hold 都是 no_consensus：merged 为空 ≠ 双方 hold
        for decisions in (["hold", "propose"], ["propose", "hold"]):
            with self.subTest(decisions=decisions):
                reviewers = {"ai1": _reviewer("completed", decisions[0]),
                             "ai2": _reviewer("completed", decisions[1])}
                self.assertEqual(S.OUTCOME_NO_CONSENSUS, self._classify(reviewers, decisions))

    def test_c3_unusable_reviewer_is_failed_even_with_valid_other_side(self):
        reviewers = {"ai1": _reviewer("failed"), "ai2": _reviewer("completed", "propose")}
        self.assertEqual(S.OUTCOME_FAILED, self._classify(reviewers, [None, "propose"]))

    def test_c4_consensus_requires_agreement_and_a_merged_proposal(self):
        reviewers = {"ai1": _reviewer("completed", "propose"),
                     "ai2": _reviewer("completed", "propose")}
        self.assertEqual(S.OUTCOME_NO_CONSENSUS,
                         self._classify(reviewers, ["propose", "propose"], consensus=False))
        self.assertEqual(S.OUTCOME_NO_CONSENSUS,
                         self._classify(reviewers, ["propose", "propose"], consensus=True, merged=[]))
        self.assertEqual(S.OUTCOME_CONSENSUS,
                         self._classify(reviewers, ["propose", "propose"], consensus=True,
                                        merged=[{"account_id": "a"}]))

    def test_c5_reviewer_decision_validation_is_strict(self):
        for value in (None, "", "maybe", "unknown", "HOLD!"):
            with self.subTest(value=value):
                self.assertFalse(S.normalize_reviewer_decision(value)[1])
                self.assertFalse(S.reviewer_is_usable(_reviewer("completed", value)))
        for value in ("hold", "propose", " Hold ", "PROPOSE"):
            with self.subTest(value=value):
                self.assertTrue(S.normalize_reviewer_decision(value)[1])

    def test_c6_reviewer_proposals_validation_is_strict(self):
        """propose 必须给出**非空对象列表**；任何畸形形状都不可用。

        畸形值绝不能被静默替换成 ``[]`` —— 那会把协议失败伪装成
        "双方成功但提案谈不拢"（``no_consensus``）。
        """
        # propose：合法形状
        good = [{"account_id": "a", "weights": {"mom": 0.5}}]
        self.assertEqual((good, True), S.normalize_reviewer_proposals("propose", good))
        self.assertEqual((good, True), S.normalize_reviewer_proposals("propose", list(good)))
        # propose：畸形/空的形状
        for value in (None, "", "oops", {"account_id": "a"}, 0, [], [1, 2], ["x"], [good, "bad"]):
            with self.subTest(decision="propose", value=value):
                proposals, ok = S.normalize_reviewer_proposals("propose", value)
                self.assertFalse(ok, "propose 的畸形提案必须判为不可用")
                self.assertIsNone(proposals)
        # hold：缺省/空列表合法，给了值则仍须是对象列表
        for value in (None, []):
            with self.subTest(decision="hold", value=value):
                self.assertEqual(([], True), S.normalize_reviewer_proposals("hold", value))
        self.assertEqual((good, True), S.normalize_reviewer_proposals("hold", good))
        for value in ("oops", {"a": 1}, 3, [1], [good, "bad"]):
            with self.subTest(decision="hold", value=value):
                self.assertFalse(S.normalize_reviewer_proposals("hold", value)[1])


class OutcomeTaxonomyTests(AiReviewSlotTestBase):
    """T35–T52：真实 ``run_ai_review`` 路径上的状态机（含审计落库）。"""

    def setUp(self):
        super().setUp()
        self.configure_slots()

    def _run(self, side_effect):
        with self.stub(side_effect):
            return self.run_review()

    def test_t35_hold_hold_is_both_hold(self):
        result = self._run(_decide("hold", "hold", proposals1=[], proposals2=[]))
        self.assertEqual("both_hold", result["status"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        self.assertEqual("两个 AI 均明确建议保持当前配置", result["reason"])

    def test_t36_hold_propose_is_no_consensus_never_both_hold(self):
        result = self._run(_decide("hold", "propose", proposals1=[]))
        self.assertEqual("no_consensus", result["status"])
        self.assertNotEqual("both_hold", result["status"], "hold/propose 绝不能被归成 both_hold")
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        self.assertIn("双AI决策不一致: ai1=hold ai2=propose", result["reason"])

    def test_t37_propose_hold_is_no_consensus_never_both_hold(self):
        result = self._run(_decide("propose", "hold", proposals2=[]))
        self.assertEqual("no_consensus", result["status"])
        self.assertNotEqual("both_hold", result["status"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        self.assertIn("双AI决策不一致: ai1=propose ai2=hold", result["reason"])

    def test_t38_propose_propose_with_agreement_is_consensus(self):
        result = self._run(self.agreeing)
        self.assertEqual("consensus", result["status"])
        self.assertTrue(result["consensus"])
        self.assertTrue(result["proposals"])
        row = self.audit(result["id"])
        self.assertEqual("consensus", row["status"])
        self.assertEqual("consensus", row["consensus_result"])
        self.assertIsNotNone(row["merged_proposals"])

    def test_t39_propose_propose_without_agreement_is_no_consensus(self):
        # 方向分歧：ai1 把 mom 权重上调、ai2 下调
        direction = _decide(
            "propose", "propose",
            proposals1=[_proposal({"mom": 0.53, "sentiment": 0.47})],
            proposals2=[_proposal({"mom": 0.47, "sentiment": 0.53})],
        )
        result = self._run(direction)
        self.assertEqual("no_consensus", result["status"])
        self.assertNotEqual("both_hold", result["status"], "提案分歧绝不能降级成 both_hold")
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        # 幅度分歧：同向但幅度比过低（0.01/0.03 = 0.33 < 0.60）
        magnitude = _decide(
            "propose", "propose",
            proposals1=[_proposal({"mom": 0.53, "sentiment": 0.47})],
            proposals2=[_proposal({"mom": 0.51, "sentiment": 0.49})],
        )
        result2 = self._run(magnitude)
        self.assertEqual("no_consensus", result2["status"])
        self.assertNotEqual("both_hold", result2["status"])
        self.assertFalse(result2["consensus"])
        self.assertEqual([], result2["proposals"])

    def test_t40_ai1_runtime_failure_is_failed(self):
        def timeout(slot_config, system_prompt, user_prompt, max_tokens=1800):
            if slot_config["slot"] == "ai1":
                raise TimeoutError("upstream did not respond")
            return _response()

        result = self._run(timeout)
        self.assertEqual("failed", result["status"])
        self.assertNotEqual("no_consensus", result["status"], "调用失败不是意见分歧")
        self.assertIn("ai1 reviewer failed", result["reason"])
        self.assertIn("TimeoutError", result["reason"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])

    def test_t41_ai2_unusable_response_is_failed(self):
        # 合法 JSON 但不是对象 → 协议层不可用（既非 hold 也非 propose）
        def malformed(slot_config, system_prompt, user_prompt, max_tokens=1800):
            if slot_config["slot"] == "ai2":
                return ([], 1, 1, 5)
            return _response()

        result = self._run(malformed)
        self.assertEqual("failed", result["status"])
        self.assertIn("ai2 reviewer failed", result["reason"])
        self.assertIn("response_not_object", result["reason"])
        self.assertEqual("failed", result["reviewers"]["ai2"]["status"])

    def test_t42_both_reviewers_failing_is_failed(self):
        def both_fail(slot_config, system_prompt, user_prompt, max_tokens=1800):
            raise RuntimeError("%s-down" % slot_config["slot"])

        result = self._run(both_fail)
        self.assertEqual("failed", result["status"])
        self.assertIn("ai1 reviewer failed", result["reason"])
        self.assertIn("ai2 reviewer failed", result["reason"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])

    def test_t43_audit_persists_no_consensus(self):
        result = self._run(_decide("hold", "propose", proposals1=[]))
        row = self.audit(result["id"])
        self.assertEqual("no_consensus", row["status"])
        self.assertEqual("no_consensus", row["consensus_result"])
        self.assertIsNone(row["merged_proposals"])
        # 审计行的 reviewers 明细必须能区分"两侧都成功"
        reviewers = json.loads(row["reviewers"])
        self.assertEqual("completed", reviewers["ai1"]["status"])
        self.assertEqual("completed", reviewers["ai2"]["status"])
        self.assertEqual("hold", reviewers["ai1"]["decision"])
        self.assertEqual("propose", reviewers["ai2"]["decision"])

    def test_t44_audit_persists_both_hold_exactly(self):
        result = self._run(_decide("hold", "hold", proposals1=[], proposals2=[]))
        row = self.audit(result["id"])
        self.assertEqual("both_hold", row["status"])
        self.assertEqual("no_consensus", row["consensus_result"])
        self.assertIsNone(row["merged_proposals"])

    def test_t46_invalid_or_missing_decision_is_failed(self):
        cases = [
            {"decision": "maybe", "proposals": [_proposal()]},
            {"proposals": [_proposal()]},          # decision 缺失
            {"decision": "", "proposals": [_proposal()]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                def side_effect(slot_config, system_prompt, user_prompt, max_tokens=1800,
                                _payload=payload):
                    if slot_config["slot"] == "ai1":
                        return ({**_payload, "confidence": 86, "market_regime": "trend",
                                 "summary": "stub"}, 11, 7, 9)
                    return _response()          # ai2 是合法 propose
                result = self._run(side_effect)
                self.assertEqual("failed", result["status"],
                                 "非法 decision 的 reviewer 不可用，双AI必须判 failed")
                self.assertNotIn(result["status"], ("both_hold", "no_consensus"))
                self.assertIn("unusable_decision", result["reason"])
                self.assertFalse(result["consensus"])

    def test_t49_propose_with_object_proposals_is_failed_not_no_consensus(self):
        """propose 却给出**对象**形状的 proposals → 协议失败，必须是 ``failed``。

        回归的是 "静默替换成 ``[]``"：旧实现会把畸形提案吞成空列表，于是这次
        **协议失败**最终落成 ``no_consensus``，与"双方成功但真的谈不拢"无法区分，
        并从 ``failure_rate`` 里蒸发。
        """
        result = self._run(_raw_by_slot({
            "ai1": {"decision": "propose", "proposals": {"account_id": ACCOUNT}},
            "ai2": {"decision": "propose", "proposals": [_proposal()]},
        }))
        self.assertEqual("failed", result["status"])
        self.assertNotEqual("no_consensus", result["status"], "协议失败不是语义分歧")
        self.assertNotEqual("both_hold", result["status"])
        self.assertIn("unusable_proposals", result["reason"])
        self.assertIn("ai1 reviewer failed", result["reason"])
        self.assertEqual("failed", result["reviewers"]["ai1"]["status"])
        self.assertEqual("completed", result["reviewers"]["ai2"]["status"],
                         "另一端是好的，不得被连坐成失败")
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        self.assertEqual("failed", self.audit(result["id"])["status"])

    def test_t50_propose_with_string_proposals_on_ai2_is_failed(self):
        result = self._run(_raw_by_slot({
            "ai1": {"decision": "propose", "proposals": [_proposal()]},
            "ai2": {"decision": "propose", "proposals": "oops"},
        }))
        self.assertEqual("failed", result["status"])
        self.assertIn("unusable_proposals", result["reason"])
        self.assertIn("ai2 reviewer failed", result["reason"])
        self.assertNotIn("ai1 reviewer failed", result["reason"])
        self.assertEqual("failed", result["reviewers"]["ai2"]["status"])

    def test_t51_propose_with_empty_proposals_is_failed(self):
        """``propose`` 却没有任何提案 = 自相矛盾的响应，同样是协议失败。"""
        result = self._run(_raw_by_slot({
            "ai1": {"decision": "propose", "proposals": []},
            "ai2": {"decision": "propose", "proposals": [_proposal()]},
        }))
        self.assertEqual("failed", result["status"])
        self.assertNotEqual("no_consensus", result["status"])
        self.assertIn("unusable_proposals", result["reason"])
        self.assertFalse(result["consensus"])

    def test_t52_wellformed_proposals_are_still_accepted(self):
        """负向对照：新校验绝不能误伤合法响应。

        合法 ``propose`` 仍照常进入共识门禁（达成 → ``consensus``；谈不拢 →
        ``no_consensus``），绝不因为本次收紧而变成 ``failed``。
        """
        agreed = self._run(_raw_by_slot({
            "ai1": {"decision": "propose", "proposals": [_proposal()]},
            "ai2": {"decision": "propose", "proposals": [_proposal()]},
        }))
        self.assertNotEqual("failed", agreed["status"])
        self.assertEqual("consensus", agreed["status"])
        self.assertNotIn("unusable_proposals", agreed["reason"])

        # 合法但方向相反的提案 → 是**真正的**语义分歧，仍应是 no_consensus
        split = self._run(_raw_by_slot({
            "ai1": {"decision": "propose",
                    "proposals": [_proposal({"mom": 0.53, "sentiment": 0.47})]},
            "ai2": {"decision": "propose",
                    "proposals": [_proposal({"mom": 0.47, "sentiment": 0.53})]},
        }))
        self.assertEqual("no_consensus", split["status"])
        self.assertNotIn("unusable_proposals", split["reason"])

        # hold 侧完全不提 proposals 仍然是合法的（不是畸形响应）
        held = self._run(_raw_by_slot({
            "ai1": {"decision": "hold"},
            "ai2": {"decision": "hold"},
        }))
        self.assertEqual("both_hold", held["status"])
        self.assertNotIn("failed", held["reason"])


class SelfEvolutionMetricTests(AiReviewSlotTestBase):
    """T45：self_evolution 指标必须按状态语义分层，no_consensus 不污染任何一项。"""

    def _seed(self, statuses):
        import self_evolution as SE
        with self.factory() as conn:
            SE.ensure_schema(conn)
            for index, status in enumerate(statuses):
                conn.execute(
                    "INSERT INTO evolution_tracking(run_id, trigger, mode, status, market_regime,"
                    " applied, applied_count, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (index + 1, "test", "intraday", status, "trend", 0, 0, "2026-09-15 09:00:00"))
            conn.commit()
            return SE.get_performance_metrics(conn, 20)

    def test_t45_metric_layering(self):
        metrics = self._seed(["consensus", "both_hold", "no_consensus", "failed"])
        self.assertEqual(4, metrics["sample_count"])
        self.assertEqual(2, metrics["success_count"])
        self.assertEqual(1, metrics["hold_count"])
        self.assertEqual(1, metrics["failed_count"])
        self.assertEqual(1, metrics["no_consensus_count"])
        self.assertEqual(1, metrics["consensus_count"])
        self.assertEqual(0.5, metrics["success_rate"])
        self.assertEqual(0.25, metrics["hold_rate"])
        self.assertEqual(0.25, metrics["failure_rate"])
        self.assertEqual(0.25, metrics["no_consensus_rate"])
        # no_consensus 不得被计入 success / hold / failure
        self.assertEqual(metrics["success_count"], metrics["consensus_count"] + metrics["hold_count"])
        self.assertEqual(metrics["success_count"], 2, "no_consensus 不得算 success")
        self.assertEqual(metrics["hold_count"], 1, "no_consensus 不得算 hold")
        self.assertEqual(metrics["failed_count"], 1, "no_consensus 不得算 failure")
        # 四类互斥且完备：把 no_consensus 加回任一项都会让它不再等于样本数
        self.assertEqual(
            metrics["success_count"] + metrics["no_consensus_count"] + metrics["failed_count"],
            metrics["sample_count"],
            "success / no_consensus / failed 三类必须恰好覆盖全部样本",
        )

    def test_t45b_rates_track_their_own_buckets(self):
        metrics = self._seed(["consensus", "consensus", "both_hold",
                              "no_consensus", "no_consensus", "no_consensus", "failed"])
        self.assertEqual(7, metrics["sample_count"])
        self.assertEqual(3, metrics["success_count"])
        self.assertEqual(1, metrics["hold_count"])
        self.assertEqual(1, metrics["failed_count"])
        self.assertEqual(3, metrics["no_consensus_count"])
        self.assertAlmostEqual(3 / 7, metrics["success_rate"])
        self.assertAlmostEqual(1 / 7, metrics["hold_rate"])
        self.assertAlmostEqual(1 / 7, metrics["failure_rate"])
        self.assertAlmostEqual(3 / 7, metrics["no_consensus_rate"])
        self.assertAlmostEqual(2 / 7, metrics["consensus_rate"])


class SingleModeRegressionTests(AiReviewSlotTestBase):
    """T47：single 模式的既有语义不得被状态机改动。"""

    def setUp(self):
        super().setUp()
        self.configure_slots()
        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai1")

    def test_t47_single_success_stays_single_review(self):
        for decision, proposals in (("propose", None), ("hold", [])):
            with self.subTest(decision=decision):
                def side_effect(slot_config, system_prompt, user_prompt, max_tokens=1800,
                                _d=decision, _p=proposals):
                    return _response(decision=_d, proposals=_p)
                with self.stub(side_effect):
                    result = self.run_review(review_mode="single", single_reviewer_slot="ai1")
                self.assertEqual(S.MODE_SINGLE_REVIEW, result["mode"])
                self.assertEqual("single_review", result["status"])
                self.assertFalse(result["consensus"])
                row = self.audit(result["id"])
                self.assertEqual("single_review", row["consensus_result"])

    def test_t47b_single_failure_stays_single_review_failed(self):
        def boom(slot_config, system_prompt, user_prompt, max_tokens=1800):
            raise RuntimeError("down")
        with self.stub(boom):
            result = self.run_review(review_mode="single", single_reviewer_slot="ai1")
        self.assertEqual("single_review_failed", result["status"])
        self.assertFalse(result["consensus"])


class ApplyGateRegressionTests(AiReviewSlotTestBase):
    """T48：只有 consensus 能穿过 apply 门禁；其它四种状态一律不可 apply。"""

    def setUp(self):
        super().setUp()
        self.configure_slots()
        self.paper_path = os.path.join(self._tmp, "unused-paper.sqlite3")

    def _apply(self, run_id):
        import evolution_apply
        return evolution_apply.apply_tuner_proposals(
            self.factory, self.paper_path, run_id, confirmed=True)

    def _gate_error(self, run_id):
        with self.assertRaises(ValueError) as ctx:
            self._apply(run_id)
        return str(ctx.exception)

    def test_t48_non_consensus_states_cannot_apply(self):
        def boom(slot_config, system_prompt, user_prompt, max_tokens=1800):
            raise RuntimeError("down")

        cases = {
            "both_hold": _decide("hold", "hold", proposals1=[], proposals2=[]),
            "no_consensus": _decide("hold", "propose", proposals1=[]),
            "failed": boom,
        }
        for expected_status, side_effect in cases.items():
            with self.subTest(status=expected_status):
                with self.stub(side_effect):
                    run = self.run_review()
                self.assertEqual(expected_status, run["status"])
                self.assertEqual("只有 consensus 状态的运行可以应用", self._gate_error(run["id"]))

        with self.factory() as conn:
            S.update_review_settings(conn, review_mode="single", single_reviewer_slot="ai1")
        with self.stub(self.agreeing):
            single = self.run_review(review_mode="single", single_reviewer_slot="ai1")
        self.assertEqual("single_review", single["status"])
        self.assertEqual("只有 consensus 状态的运行可以应用", self._gate_error(single["id"]))

    def test_t48b_consensus_still_reaches_the_next_gate(self):
        # 负向对照：同一个 both_hold 行一旦被改成 consensus，拒绝原因就不再是状态门禁
        with self.stub(_decide("hold", "hold", proposals1=[], proposals2=[])):
            run = self.run_review()
        self.assertEqual("both_hold", run["status"])
        with self.factory() as conn:
            conn.execute("UPDATE dual_ai_tuning_runs SET status='consensus' WHERE id=?",
                         (run["id"],))
            conn.commit()
        message = self._gate_error(run["id"])
        self.assertNotIn("只有 consensus 状态的运行可以应用", message)
        self.assertIn("没有可应用的共识提案", message)


class ReviewerFailureIsolationTests(AiReviewSlotTestBase):
    """T53–T62：reviewer 可靠性失败不得污染策略学习。

    被测契约（``backend/self_evolution.py``）：

    ```text
    reviewer reliability evidence   -> 观测 / 诊断 / 告警（不驱动 evolution）
    learning / semantic evidence    -> 共识阈值学习（分母只含 reviewer 正常完成的样本）
    evaluation evidence             -> 真实效果学习（步长调整、回滚依据）
    ```

    这里锁的是**证据分层**，不是"把共识学习关掉"：真正的低共识仍必须能触发
    共识阈值调整（T59），真实效果证据仍必须能调整步长（T60 / T61）。
    """

    # ── 基础设施 ──
    def _init(self):
        """建立 active 指针：把库带到与生产一致的起点。"""
        import self_evolution as SE
        with self.factory() as conn:
            SE.ensure_schema(conn)
            return SE.init_params(conn, source="init")

    def _seed(self, statuses, *, params_id=None, eval_scores=None):
        """按 status 序列落 evolution_tracking 行（不触碰任何历史行）。"""
        import self_evolution as SE
        with self.factory() as conn:
            SE.ensure_schema(conn)
            scores = list(eval_scores or [])
            for index, status in enumerate(statuses):
                score = scores[index] if index < len(scores) else None
                conn.execute(
                    "INSERT INTO evolution_tracking(run_id, trigger, mode, status, market_regime,"
                    " applied, applied_count, evaluated, eval_score, evolution_params_id, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (index + 1, "test", "intraday", status, "trend", 0, 0,
                     1 if score is not None else 0, score, params_id,
                     "2026-09-15 09:00:00"),
                )
            conn.commit()

    def _metrics(self):
        import self_evolution as SE
        with self.factory() as conn:
            return SE.get_performance_metrics(conn, 20)

    def _should_evolve(self):
        import self_evolution as SE
        with self.factory() as conn:
            return SE.should_evolve(conn)

    def _evolve(self, reason="isolation-test"):
        import self_evolution as SE
        with self.factory() as conn:
            return SE.evolve(conn, reason=reason)

    def _active_params(self):
        import self_evolution as SE
        with self.factory() as conn:
            # 断言"当前生效参数"走模块公开读模型，不用私有实现。
            return SE.get_current_params(conn)["params"]

    # ─────────────────────── T53–T55：learning denominator ───────────────────
    def test_t53_failed_is_not_a_learning_sample(self):
        """``failed`` 属于 reviewer 可靠性事实，不进入 learning denominator。"""
        self._seed(["consensus", "both_hold", "no_consensus", "failed"])
        metrics = self._metrics()

        # raw / compatibility 口径：窗口内**全部** tracked run
        self.assertEqual(4, metrics["sample_count"])
        self.assertEqual(1, metrics["failed_count"])
        self.assertEqual(1, metrics["reviewer_failure_count"])
        self.assertEqual(0.25, metrics["reviewer_failure_rate"])

        # learning 口径：只有 reviewer 正常完成的 3 条
        self.assertEqual(3, metrics["learning_sample_count"])
        self.assertAlmostEqual(1 / 3, metrics["learning_consensus_rate"])
        self.assertAlmostEqual(1 / 3, metrics["learning_hold_rate"])
        self.assertAlmostEqual(1 / 3, metrics["learning_no_consensus_rate"])
        self.assertAlmostEqual(2 / 3, metrics["learning_success_rate"])
        # 关键：绝不能算成 1/4（那等于让一次 API 失败稀释语义样本）
        self.assertNotAlmostEqual(1 / 4, metrics["learning_consensus_rate"])

    def test_t54_all_failed_has_no_learning_rate_at_all(self):
        """全部 failed：learning rate 必须是 ``None``，不是 ``0.0``。

        ``没有语义样本`` ≠ ``共识率为 0%`` —— 把前者折叠成后者，等于用
        reviewer outage 冒充"AI 完全无法达成共识"。
        """
        self._seed(["failed"] * 10)
        metrics = self._metrics()

        self.assertEqual(10, metrics["sample_count"])
        self.assertEqual(10, metrics["reviewer_failure_count"])
        self.assertEqual(1.0, metrics["reviewer_failure_rate"])
        self.assertEqual(0, metrics["learning_sample_count"])
        for key in ("learning_consensus_rate", "learning_hold_rate",
                    "learning_no_consensus_rate", "learning_success_rate"):
            self.assertIsNone(metrics[key], f"{key} 必须为 None（无样本），不得为 0.0")

        should, reason = self._should_evolve()
        self.assertFalse(should, f"全失败不得触发进化：{reason}")

    def test_t55_reviewer_outage_cannot_trigger_evolution(self):
        """20 次全 failed（failure_rate = 100%）也不得触发 evolution。

        旧实现在这里命中 ``failure_rate > ROLLBACK_THRESHOLD and
        sample_count >= ROLLBACK_WINDOW`` 并返回 True —— 那正是
        ``AI API 超时 → 策略回滚/改参`` 这条错误因果链的入口。
        """
        self._seed(["failed"] * 20)
        metrics = self._metrics()
        self.assertEqual(1.0, metrics["failure_rate"])
        self.assertGreater(metrics["failure_rate"], 0.3)  # 旧 ROLLBACK_THRESHOLD
        self.assertGreaterEqual(metrics["sample_count"], 10)  # 旧 ROLLBACK_WINDOW

        should, reason = self._should_evolve()
        self.assertFalse(should, "reviewer 可靠性失败不能单独产生 should_evolve=True")
        self.assertIn("可靠性", reason)
        for forbidden in ("失败率过高", "回滚", "共识率过低"):
            self.assertNotIn(forbidden, reason)

    # ─────────────────── T56–T57：failure 不得改参 / 不得回滚 ────────────────
    def test_t56_reviewer_failure_never_raises_hold_bias(self):
        """高 reviewer failure 与 "模型应该更倾向 HOLD" 没有因果关系。"""
        self._init()
        before = self._active_params()
        self._seed(["failed"] * 20)

        result = self._evolve()
        adjustments = result.get("adjustments", []) or []

        self.assertFalse(
            any("失败率" in str(a) for a in adjustments),
            f"不得出现「失败率高 → 增大 hold 倾向」式调整：{adjustments}")
        self.assertNotIn("hold_bias", result.get("new_params", {}) or {})
        active = self._active_params()
        self.assertEqual(before["hold_bias"], active["hold_bias"], "hold_bias 必须原封不动")

    def test_t57_reviewer_failure_never_triggers_rollback(self):
        """即使 ``_find_rollback_target`` 真能找到目标，failure 也不能回滚。

        前置条件被显式断言：先造出两个真实生效过的全局版本 + 足够的成功追踪行，
        让回滚目标**确实存在** —— 否则这个用例会退化成"回滚路径本来就不可达"的假绿灯。
        """
        import self_evolution as SE

        v1 = self._init()
        with self.factory() as conn:
            created = SE.manual_adjust(conn, {"max_weight_delta": 0.027}, reason="t57-setup")
            SE.activate_params_candidate(conn, created["new_params_id"], actor="t57-setup")
            v2_id = SE.get_current_params(conn)["id"]
        self.assertNotEqual(v1["id"], v2_id, "前置条件：必须有两个生效过的版本")

        # 3 条 consensus 挂在 v1 上 → v1 满足 _find_rollback_target 的胜率门槛
        self._seed(["consensus"] * 3, params_id=v1["id"])
        # 窗口内：reviewer 失败占绝对多数，但仍有语义样本让 evolve 有正当调整对象
        self._seed(["consensus"] * 4 + ["failed"] * 16)

        with self.factory() as conn:
            target = SE._find_rollback_target(conn)
        self.assertIsNotNone(target, "前置条件：必须存在真实可回滚目标")
        self.assertEqual(v1["id"], target["id"])
        self.assertAlmostEqual(0.03, target["params"]["max_weight_delta"])

        result = self._evolve()
        adjustments = result.get("adjustments", []) or []
        self.assertFalse(
            any("回滚" in str(a) for a in adjustments),
            f"reviewer failure 绝不能触发回滚：{adjustments}")
        self.assertNotIn("max_weight_delta", result.get("new_params", {}) or {})

        with self.factory() as conn:
            row = conn.execute("SELECT params FROM evolution_params WHERE id=?",
                               (result["new_params_id"],)).fetchone()
            candidate_params = json.loads(row["params"])
            active_id = SE.get_current_params(conn)["id"]
        self.assertAlmostEqual(
            0.027, candidate_params["max_weight_delta"],
            msg="候选参数不得被回滚目标覆盖")
        self.assertEqual(v2_id, active_id, "active 指针不得因 reviewer failure 切换")

    # ─────────────────── T58–T59：denominator 正确性与负向对照 ───────────────
    def test_t58_denominator_contamination_regression(self):
        """raw 28.6% vs learning 50%：绝不能因 6 次 API failure 放宽共识门槛。"""
        self._seed(["failed"] * 6 + ["consensus"] * 4 + ["both_hold"] * 4)
        metrics = self._metrics()

        self.assertEqual(14, metrics["sample_count"])
        self.assertEqual(8, metrics["learning_sample_count"])
        self.assertEqual(0.5, metrics["learning_consensus_rate"])
        # 旧口径确实低于 30%（这就是当年会误触发"共识率过低"的原因）
        self.assertLess(metrics["consensus_rate"], 0.3)

        should, reason = self._should_evolve()
        self.assertFalse(should, f"learning 共识率 50% 不该触发任何共识调整：{reason}")
        self.assertNotIn("共识率过低", reason)

    def test_t58b_min_samples_must_count_learning_samples(self):
        """样本门槛看 ``learning_sample_count``，不是 raw ``sample_count``。

        14 条里只有 4 条是语义样本：raw 达标（>=8）不代表学习样本充足。
        """
        self._seed(["failed"] * 10 + ["consensus"] + ["no_consensus"] * 3)
        metrics = self._metrics()
        self.assertEqual(14, metrics["sample_count"])
        self.assertEqual(4, metrics["learning_sample_count"])
        self.assertAlmostEqual(0.25, metrics["learning_consensus_rate"])

        should, reason = self._should_evolve()
        self.assertFalse(should, f"语义样本仅 4 条，不构成共识学习证据：{reason}")
        self.assertNotIn("共识率过低", reason)

    def test_t59_genuine_low_consensus_still_evolves(self):
        """负向对照：真正（reviewer 全部成功）的低共识仍必须触发共识阈值调整。

        证明本 PR 是"隔离噪声"，不是"关掉共识学习"。
        """
        self._seed(["consensus"] + ["both_hold"] + ["no_consensus"] * 6)
        metrics = self._metrics()
        self.assertEqual(8, metrics["learning_sample_count"])
        self.assertEqual(0, metrics["reviewer_failure_count"])
        self.assertAlmostEqual(1 / 8, metrics["learning_consensus_rate"])

        should, reason = self._should_evolve()
        self.assertTrue(should, "真实低共识必须仍能触发进化")
        self.assertIn("共识率过低", reason)

        self._init()
        result = self._evolve()
        self.assertTrue(result.get("evolved"))
        self.assertIn("consensus_weight_ratio", result.get("new_params", {}))
        self.assertLess(result["new_params"]["consensus_weight_ratio"], 0.6)
        self.assertFalse(any("失败率" in str(a) for a in result.get("adjustments", [])))

    # ─────────────────── T60–T61：真实效果证据不得被误伤 ─────────────────────
    def test_t60_negative_evaluation_still_shrinks_step(self):
        """``avg_eval_score < -0.2`` 仍必须缩小 ``max_weight_delta``。

        reviewer failure isolation 不能误伤 #141 建立的真实 reward 反馈：
        这里刻意让窗口里**同时**存在大量 reviewer 失败 —— 真实效果证据必须在
        reviewer 不可靠时依然完整可用（两类证据彼此独立，不是互相门禁）。
        """
        self._init()
        self._seed(["consensus"] * 6 + ["failed"] * 10, eval_scores=[-0.5] * 6)
        metrics = self._metrics()
        self.assertEqual(6, metrics["evaluated_count"])
        self.assertAlmostEqual(-0.5, metrics["avg_eval_score"])
        self.assertGreater(metrics["reviewer_failure_rate"], 0.4)

        should, reason = self._should_evolve()
        self.assertTrue(should, "评估证据必须仍能触发进化")
        self.assertIn("平均评估分数过低", reason)

        result = self._evolve()
        self.assertTrue(result.get("evolved"))
        self.assertIn("max_weight_delta", result.get("new_params", {}))
        self.assertAlmostEqual(0.027, result["new_params"]["max_weight_delta"])
        self.assertTrue(any("缩小权重步长" in str(a) for a in result.get("adjustments", [])))

    def test_t61_positive_evaluation_still_widens_step(self):
        """``avg_eval_score > 0.3`` 仍可适度放大 ``max_weight_delta``。"""
        self._init()
        self._seed(["consensus"] * 6 + ["failed"] * 10, eval_scores=[0.5] * 6)
        self.assertAlmostEqual(0.5, self._metrics()["avg_eval_score"])

        result = self._evolve()
        self.assertTrue(result.get("evolved"))
        self.assertAlmostEqual(0.0315, result["new_params"]["max_weight_delta"])
        self.assertTrue(any("适度放大权重步长" in str(a) for a in result.get("adjustments", [])))

    # ─────────────────────── T62：raw 字段语义未被偷改 ───────────────────────
    def test_t58c_reviewer_failure_cannot_suppress_the_high_consensus_gate(self):
        """reviewer 失败也不得**反向**干扰学习：不得稀释掉"共识率过高"判据。

        同一份语义证据（5 次全 consensus）在"另有 5 次 reviewer 超时"时，raw
        ``propose_rate`` 只有 50%，会低于 0.6 门槛；learning 口径仍是 100%。
        两种窗口必须给出同一个进化结论 —— 否则 reviewer outage 就能反向压制
        一次本该发生的共识阈值学习。
        """
        # 对照：同一份语义证据、没有 reviewer 失败
        self._seed(["consensus"] * 5)
        clean_should, clean_reason = self._should_evolve()
        self.assertTrue(clean_should, f"5 次全 consensus 应触发共识学习：{clean_reason}")
        self.assertIn("共识率过高", clean_reason)

        # 实验组：同一份语义证据 + 5 次 reviewer 失败
        with self.factory() as conn:
            conn.execute("DELETE FROM evolution_tracking")
            conn.commit()
        self._init()
        self._seed(["consensus"] * 5 + ["failed"] * 5)
        metrics = self._metrics()
        self.assertEqual(10, metrics["sample_count"])
        self.assertEqual(5, metrics["learning_sample_count"])
        self.assertAlmostEqual(0.5, metrics["propose_rate"])          # raw 口径已被稀释
        self.assertAlmostEqual(1.0, metrics["learning_propose_rate"])  # learning 口径不受影响
        self.assertAlmostEqual(1.0, metrics["learning_consensus_rate"])

        should, reason = self._should_evolve()
        self.assertTrue(
            should,
            f"reviewer 失败不得抑制共识学习：{reason}")
        self.assertIn("共识率过高", reason)

        result = self._evolve()
        self.assertTrue(result.get("evolved"))
        self.assertGreater(result["new_params"]["consensus_weight_ratio"], 0.6)

    def test_t62_legacy_metrics_keep_their_meaning(self):
        """旧字段必须仍存在、仍是 raw 口径；新字段不得悄悄改写旧语义。"""
        self._seed(["failed"] * 6 + ["consensus"] * 4 + ["both_hold"] * 4)
        metrics = self._metrics()

        for key in ("sample_count", "failed_count", "failure_rate", "consensus_rate",
                    "hold_rate", "no_consensus_rate", "success_rate", "applied_rate"):
            self.assertIn(key, metrics, f"兼容字段 {key} 不得消失")

        # raw 口径 = 全部 tracked run
        self.assertEqual(14, metrics["sample_count"])
        self.assertEqual(6, metrics["failed_count"])
        self.assertAlmostEqual(6 / 14, metrics["failure_rate"])
        self.assertAlmostEqual(4 / 14, metrics["consensus_rate"])
        self.assertAlmostEqual(4 / 14, metrics["hold_rate"])
        self.assertAlmostEqual(8 / 14, metrics["success_rate"])

        # 新字段是旧字段的语义别名，逐值相等
        self.assertEqual(metrics["failed_count"], metrics["reviewer_failure_count"])
        self.assertEqual(metrics["failure_rate"], metrics["reviewer_failure_rate"])
        # 同一份数据在两种证据口径下结论完全不同 —— 这正是分层要守住的东西：
        # "6/14 次 reviewer 调用不可靠" 与 "语义层 8/8 次成功" 必须同时成立。
        self.assertAlmostEqual(6 / 14, metrics["reviewer_failure_rate"])
        self.assertAlmostEqual(4 / 8, metrics["learning_consensus_rate"])
        self.assertAlmostEqual(4 / 8, metrics["learning_hold_rate"])
        self.assertAlmostEqual(1.0, metrics["learning_success_rate"])
        self.assertAlmostEqual(0.0, metrics["learning_no_consensus_rate"])

    def test_t62b_empty_window_contract_is_unchanged(self):
        """无数据窗口：旧 empty 返回保持原样，且不得让 should_evolve 抛 KeyError。"""
        import self_evolution as SE
        with self.factory() as conn:
            SE.ensure_schema(conn)
            metrics = SE.get_performance_metrics(conn, 20)
        self.assertEqual({"sample_count": 0, "has_data": False}, metrics)

        should, reason = self._should_evolve()
        self.assertFalse(should)
        self.assertEqual("无历史数据", reason)


# ───────────────────────── T63–T80：Disagreement Taxonomy ─────────────────────────

class DisagreementTaxonomyTests(AiReviewSlotTestBase):
    def test_t63_decision_mismatch_hold_propose(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(decision="hold", proposals=[])
            return _response(decision="propose", proposals=[_proposal()])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIsNotNone(detail)
        self.assertEqual(S.OUTCOME_DETAIL_SCHEMA_VERSION, detail["schema_version"])
        self.assertEqual(S.OUTCOME_NO_CONSENSUS, detail["status"])
        self.assertEqual({"ai1": "hold", "ai2": "propose"}, detail["decisions"])
        self.assertEqual([S.DISAGREEMENT_DECISION_MISMATCH], detail["disagreement_codes"])
        self.assertEqual(1, len(detail["issues"]))
        issue = detail["issues"][0]
        self.assertEqual(S.DISAGREEMENT_DECISION_MISMATCH, issue["code"])
        self.assertEqual("hold", issue["left"])
        self.assertEqual("propose", issue["right"])

    def test_t64_decision_mismatch_propose_hold(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(decision="propose", proposals=[_proposal()])
            return _response(decision="hold", proposals=[])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIsNotNone(detail)
        self.assertEqual(S.OUTCOME_DETAIL_SCHEMA_VERSION, detail["schema_version"])
        self.assertEqual({"ai1": "propose", "ai2": "hold"}, detail["decisions"])
        self.assertEqual([S.DISAGREEMENT_DECISION_MISMATCH], detail["disagreement_codes"])
        self.assertEqual(1, len(detail["issues"]))
        issue = detail["issues"][0]
        self.assertEqual(S.DISAGREEMENT_DECISION_MISMATCH, issue["code"])
        self.assertEqual("propose", issue["left"])
        self.assertEqual("hold", issue["right"])

    def test_t65_account_scope_mismatch(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                p = _proposal()
                p["account_id"] = "account_beta"
                return _response(proposals=[p])
            p = _proposal()
            p["account_id"] = "account_alpha"
            return _response(proposals=[p])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertEqual([S.DISAGREEMENT_ACCOUNT_SCOPE_MISMATCH], detail["disagreement_codes"])
        self.assertEqual(1, len(detail["issues"]))
        issue = detail["issues"][0]
        self.assertEqual(S.DISAGREEMENT_ACCOUNT_SCOPE_MISMATCH, issue["code"])
        self.assertEqual(["account_beta"], issue["ai1_accounts"])
        self.assertEqual(["account_alpha"], issue["ai2_accounts"])
        self.assertEqual([], issue["common_accounts"])

    def test_t66_confidence_below_threshold(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(confidence=65.0, proposals=[_proposal(confidence=65.0)])
            return _response(confidence=82.0, proposals=[_proposal(confidence=82.0)])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertEqual([S.DISAGREEMENT_CONFIDENCE_BELOW_THRESHOLD], detail["disagreement_codes"])
        self.assertEqual(1, len(detail["issues"]))
        issue = detail["issues"][0]
        self.assertEqual(S.DISAGREEMENT_CONFIDENCE_BELOW_THRESHOLD, issue["code"])
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual(65.0, issue["ai1_confidence"])
        self.assertEqual(82.0, issue["ai2_confidence"])
        self.assertEqual(70.0, issue["threshold"])

    def test_t67_weight_direction_mismatch(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(proposals=[_proposal(weights={"mom": 0.53, "sentiment": 0.47})])
            return _response(proposals=[_proposal(weights={"mom": 0.48, "sentiment": 0.52})])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIn(S.DISAGREEMENT_WEIGHT_DIRECTION_MISMATCH, detail["disagreement_codes"])
        issue = next(i for i in detail["issues"] if i["code"] == S.DISAGREEMENT_WEIGHT_DIRECTION_MISMATCH)
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual("weights.mom", issue["field"])
        self.assertAlmostEqual(0.03, issue["ai1_delta"])
        self.assertAlmostEqual(-0.02, issue["ai2_delta"])
        self.assertAlmostEqual(0.005, issue["threshold"])

    def test_t68_weight_magnitude_mismatch_and_override(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(proposals=[_proposal(weights={"mom": 0.53, "sentiment": 0.47})])
            return _response(proposals=[_proposal(weights={"mom": 0.51, "sentiment": 0.49})])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIn(S.DISAGREEMENT_WEIGHT_MAGNITUDE_MISMATCH, detail["disagreement_codes"])
        issue = next(i for i in detail["issues"] if i["code"] == S.DISAGREEMENT_WEIGHT_MAGNITUDE_MISMATCH)
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual("weights.mom", issue["field"])
        self.assertAlmostEqual(0.3333, issue["ratio"], places=3)
        self.assertEqual(0.60, issue["required_ratio"])

        # Test account-level evolution override
        with mock.patch("ai_review_service._evolution_bounds", return_value=({}, {ACCOUNT: {"consensus_weight_ratio": 0.75}})):
            with self.stub(side_effect):
                res_override = self.run_review()

        detail_override = res_override.get("outcome_detail")
        issue_override = next(i for i in detail_override["issues"] if i["code"] == S.DISAGREEMENT_WEIGHT_MAGNITUDE_MISMATCH)
        self.assertEqual(0.75, issue_override["required_ratio"])

    def test_t69_entry_direction_mismatch(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(proposals=[_proposal(entry_score_delta=0.003)])
            return _response(proposals=[_proposal(entry_score_delta=-0.003)])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIn(S.DISAGREEMENT_ENTRY_DIRECTION_MISMATCH, detail["disagreement_codes"])
        issue = next(i for i in detail["issues"] if i["code"] == S.DISAGREEMENT_ENTRY_DIRECTION_MISMATCH)
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual("entry_score_delta", issue["field"])
        self.assertAlmostEqual(0.003, issue["ai1_delta"])
        self.assertAlmostEqual(-0.003, issue["ai2_delta"])
        self.assertAlmostEqual(0.001, issue["threshold"])

    def test_t70_condition_direction_mismatch(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(proposals=[_proposal(conditions={"vol_mult": 1.05})])
            return _response(proposals=[_proposal(conditions={"vol_mult": 0.95})])

        with mock.patch.object(self, "_accounts", return_value=[{
            "account_id": ACCOUNT, "weights": dict(BASE_WEIGHTS), "conditions": {"vol_mult": 1.0}
        }]):
            with self.stub(side_effect):
                res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIn(S.DISAGREEMENT_CONDITION_DIRECTION_MISMATCH, detail["disagreement_codes"])
        issue = next(i for i in detail["issues"] if i["code"] == S.DISAGREEMENT_CONDITION_DIRECTION_MISMATCH)
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual("conditions.vol_mult", issue["field"])
        self.assertAlmostEqual(0.05, issue["ai1_delta"])
        self.assertAlmostEqual(-0.05, issue["ai2_delta"])

    def test_t71_condition_magnitude_mismatch(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(proposals=[_proposal(conditions={"vol_mult": 1.10})])
            return _response(proposals=[_proposal(conditions={"vol_mult": 1.02})])

        with mock.patch.object(self, "_accounts", return_value=[{
            "account_id": ACCOUNT, "weights": dict(BASE_WEIGHTS), "conditions": {"vol_mult": 1.0}
        }]):
            with self.stub(side_effect):
                res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIn(S.DISAGREEMENT_CONDITION_MAGNITUDE_MISMATCH, detail["disagreement_codes"])
        issue = next(i for i in detail["issues"] if i["code"] == S.DISAGREEMENT_CONDITION_MAGNITUDE_MISMATCH)
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual("conditions.vol_mult", issue["field"])
        self.assertAlmostEqual(0.20, issue["ratio"])
        self.assertEqual(0.50, issue["required_ratio"])

    def test_t72_unknown_factor(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            if slot == "ai1":
                return _response(proposals=[_proposal(weights={"mom": 0.5, "sentiment": 0.5, "zeta": 0.1, "alpha": 0.1})])
            return _response(proposals=[_proposal()])

        with self.stub(side_effect):
            res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertEqual([S.DISAGREEMENT_UNKNOWN_FACTOR], detail["disagreement_codes"])
        issue = detail["issues"][0]
        self.assertEqual(S.DISAGREEMENT_UNKNOWN_FACTOR, issue["code"])
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual(["alpha", "zeta"], issue["ai1_unknown"])
        self.assertEqual([], issue["ai2_unknown"])

    def test_t73_normalized_weight_step_exceeded(self):
        self.configure_slots()
        base_w = {"mom": 0.10, "sentiment": 0.90}

        def side_effect(slot_config, *args, **kwargs):
            return _response(proposals=[{
                "account_id": ACCOUNT, "reason": "step-test", "confidence": 85,
                "weights": {"mom": 0.13, "sentiment": 0.50},
                "entry_score_delta": 0.0, "conditions": {},
            }])

        with mock.patch.object(self, "_accounts", return_value=[{
            "account_id": ACCOUNT, "weights": base_w, "conditions": {}
        }]):
            with self.stub(side_effect):
                res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIn(S.DISAGREEMENT_NORMALIZED_WEIGHT_STEP_EXCEEDED, detail["disagreement_codes"])
        issue = next(i for i in detail["issues"] if i["code"] == S.DISAGREEMENT_NORMALIZED_WEIGHT_STEP_EXCEEDED)
        self.assertEqual(ACCOUNT, issue["account_id"])
        self.assertEqual(0.03, issue["max_step"])
        self.assertIn("weights.mom", issue.get("affected_fields", []))

    def test_t74_multi_account_multi_issue_no_truncation(self):
        accounts = [
            {"account_id": f"acc_{i}", "weights": {"f1": 0.5, "f2": 0.5}, "conditions": {"c1": 1.0}}
            for i in range(1, 8)
        ]
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            slot = slot_config["slot"]
            props = [
                # acc_1: weight direction mismatch
                {
                    "account_id": "acc_1", "confidence": 85,
                    "weights": {"f1": 0.53, "f2": 0.50} if slot == "ai1" else {"f1": 0.47, "f2": 0.50},
                    "entry_score_delta": 0.0, "conditions": {"c1": 1.0},
                },
                # acc_2: confidence below threshold
                {
                    "account_id": "acc_2", "confidence": 60 if slot == "ai1" else 85,
                    "weights": {"f1": 0.50, "f2": 0.50},
                    "entry_score_delta": 0.0, "conditions": {"c1": 1.0},
                },
                # acc_3: unknown factor
                {
                    "account_id": "acc_3", "confidence": 85,
                    "weights": {"f1": 0.5, "f2": 0.5, "f_extra": 0.1} if slot == "ai1" else {"f1": 0.5, "f2": 0.5},
                    "entry_score_delta": 0.0, "conditions": {"c1": 1.0},
                },
                # acc_4: weight magnitude mismatch
                {
                    "account_id": "acc_4", "confidence": 85,
                    "weights": {"f1": 0.53, "f2": 0.50} if slot == "ai1" else {"f1": 0.51, "f2": 0.50},
                    "entry_score_delta": 0.0, "conditions": {"c1": 1.0},
                },
                # acc_5: entry direction mismatch
                {
                    "account_id": "acc_5", "confidence": 85,
                    "weights": {"f1": 0.50, "f2": 0.50},
                    "entry_score_delta": 0.003 if slot == "ai1" else -0.003,
                    "conditions": {"c1": 1.0},
                },
                # acc_6: condition direction mismatch
                {
                    "account_id": "acc_6", "confidence": 85,
                    "weights": {"f1": 0.50, "f2": 0.50},
                    "entry_score_delta": 0.0,
                    "conditions": {"c1": 1.05} if slot == "ai1" else {"c1": 0.95},
                },
                # acc_7: condition magnitude mismatch
                {
                    "account_id": "acc_7", "confidence": 85,
                    "weights": {"f1": 0.50, "f2": 0.50},
                    "entry_score_delta": 0.0,
                    "conditions": {"c1": 1.10} if slot == "ai1" else {"c1": 1.02},
                },
            ]
            return _response(proposals=props)

        with mock.patch.object(self, "_accounts", return_value=accounts):
            with self.stub(side_effect):
                res = self.run_review()

        self.assertEqual(S.OUTCOME_NO_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIsNotNone(detail)
        # Reason has human truncation (semicolon delimited at most 5 items)
        self.assertLessEqual(len(res["reason"].split("; ")), 5)
        # Machine issues must NOT be truncated, preserving all 7 issues
        self.assertEqual(7, len(detail["issues"]))
        # disagreement_codes must be deduplicated and sorted
        self.assertEqual(sorted(list(set(detail["disagreement_codes"]))), detail["disagreement_codes"])
        self.assertGreaterEqual(len(detail["disagreement_codes"]), 5)

    def test_t75_consensus_success(self):
        self.configure_slots()
        with self.stub(self.agreeing):
            res = self.run_review()
        self.assertEqual(S.OUTCOME_CONSENSUS, res["status"])
        detail = res.get("outcome_detail")
        self.assertIsNotNone(detail)
        self.assertEqual(S.OUTCOME_CONSENSUS, detail["status"])
        self.assertEqual({"ai1": "propose", "ai2": "propose"}, detail["decisions"])
        self.assertEqual([], detail["disagreement_codes"])
        self.assertEqual([], detail["issues"])

    def test_t76_both_hold(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            return _response(decision="hold", proposals=[])

        with self.stub(side_effect):
            res = self.run_review()
        self.assertEqual(S.OUTCOME_BOTH_HOLD, res["status"])
        detail = res.get("outcome_detail")
        self.assertIsNotNone(detail)
        self.assertEqual(S.OUTCOME_BOTH_HOLD, detail["status"])
        self.assertEqual({"ai1": "hold", "ai2": "hold"}, detail["decisions"])
        self.assertEqual([], detail["disagreement_codes"])
        self.assertEqual([], detail["issues"])

    def test_t77_reviewer_runtime_failure(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            if slot_config["slot"] == "ai1":
                raise RuntimeError("HTTP connection reset 500")
            return _response(decision="propose", proposals=[_proposal()])

        with self.stub(side_effect):
            res = self.run_review()
        self.assertEqual(S.OUTCOME_FAILED, res["status"])
        detail = res.get("outcome_detail")
        self.assertIsNotNone(detail)
        self.assertEqual(S.OUTCOME_FAILED, detail["status"])
        self.assertEqual([], detail["disagreement_codes"])
        self.assertEqual([], detail["issues"])

    def test_t78_audit_round_trip_and_historical_null(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            if slot_config["slot"] == "ai1":
                return _response(decision="hold", proposals=[])
            return _response(decision="propose", proposals=[_proposal()])

        with self.stub(side_effect):
            res = self.run_review()
        run_id = res["id"]
        row = self.audit(run_id)
        stored_json = row["outcome_detail"]
        self.assertIsNotNone(stored_json)
        stored = json.loads(stored_json)
        self.assertEqual(res["outcome_detail"], stored)

        with self.factory() as conn:
            runs = S.recent_runs(conn, 5)
        self.assertEqual(stored, runs[0]["outcome_detail"])

        # Historical row with NULL outcome_detail
        with self.factory() as conn:
            conn.execute(
                "INSERT INTO dual_ai_tuning_runs(trigger, mode, status, evidence_hash, evidence, created_at, finished_at, outcome_detail) "
                "VALUES('hist', 'intraday', 'both_hold', 'h1', '{}', '2026-01-01', '2026-01-01', NULL)"
            )
            conn.commit()
            hist_runs = S.recent_runs(conn, 5)
        self.assertIsNone(hist_runs[0]["outcome_detail"])

    def test_t79_reason_wording_independence(self):
        self.configure_slots()

        def side_effect(slot_config, *args, **kwargs):
            if slot_config["slot"] == "ai1":
                return _response(decision="hold", proposals=[])
            return _response(decision="propose", proposals=[_proposal()])

        with self.stub(side_effect):
            res1 = self.run_review()

        with mock.patch("ai_review_service._decision_disagreement_reason", return_value="自定义完全不同的人类文案"):
            with self.stub(side_effect):
                res2 = self.run_review()

        self.assertEqual("自定义完全不同的人类文案", res2["reason"])
        self.assertEqual(res1["outcome_detail"], res2["outcome_detail"])
        self.assertEqual([S.DISAGREEMENT_DECISION_MISMATCH], res2["outcome_detail"]["disagreement_codes"])

    def test_t80_taxonomy_does_not_drive_evolution(self):
        import self_evolution as SE
        with self.factory() as conn:
            S.ensure_schema(conn)
            SE.ensure_schema(conn)
            SE.init_params(conn)
            conn.execute(
                "INSERT INTO dual_ai_tuning_runs(trigger, mode, status, evidence_hash, evidence, created_at, finished_at, outcome_detail) "
                "VALUES('t', 'intraday', 'no_consensus', 'h1', '{}', '2026-01-01', '2026-01-01', ?)",
                (json.dumps({"disagreement_codes": ["weight_direction_mismatch"]}),)
            )
            r1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            SE.track_run(conn, r1, trigger="t", mode="intraday", status="no_consensus", applied=False)

            metrics1 = SE.get_performance_metrics(conn, 20)
            should1, reason1 = SE.should_evolve(conn)

        with tempfile.TemporaryDirectory(prefix="t80-") as tmp2:
            p2 = os.path.join(tmp2, "db.sqlite3")
            conn2 = sqlite3.connect(p2)
            conn2.row_factory = sqlite3.Row
            S.ensure_schema(conn2)
            SE.ensure_schema(conn2)
            SE.init_params(conn2)
            conn2.execute(
                "INSERT INTO dual_ai_tuning_runs(trigger, mode, status, evidence_hash, evidence, created_at, finished_at, outcome_detail) "
                "VALUES('t', 'intraday', 'no_consensus', 'h1', '{}', '2026-01-01', '2026-01-01', ?)",
                (json.dumps({"disagreement_codes": ["confidence_below_threshold", "unknown_factor"]}),)
            )
            r2 = conn2.execute("SELECT last_insert_rowid()").fetchone()[0]
            SE.track_run(conn2, r2, trigger="t", mode="intraday", status="no_consensus", applied=False)

            metrics2 = SE.get_performance_metrics(conn2, 20)
            should2, reason2 = SE.should_evolve(conn2)
            conn2.close()

        self.assertEqual(metrics1, metrics2)
        self.assertEqual((should1, reason1), (should2, reason2))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

