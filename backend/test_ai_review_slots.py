# -*- coding: utf-8 -*-
"""通用 AI 审核槽位（``ai1`` / ``ai2``）契约测试：T1–T24。

对应 PR「refactor: generalize AI review into configurable AI1/AI2 slots」的验收清单。

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

    def test_t21_dual_mode_one_failed_call_yields_no_consensus_and_no_fallback(self):
        self.configure_slots()

        def half_fail(slot_config, system_prompt, user_prompt, max_tokens=1800):
            if slot_config["slot"] == "ai1":
                raise RuntimeError("ai1-down")
            return _response(proposals=[_proposal({"mom": 0.515, "sentiment": 0.485})])

        with self.stub(half_fail):
            result = self.run_review()
        self.assertEqual(["ai1", "ai2"], sorted(self.calls))
        self.assertEqual("dual_review", result["mode"])
        self.assertEqual("no_consensus", result["status"])
        self.assertFalse(result["consensus"])
        self.assertEqual([], result["proposals"])
        self.assertEqual("failed", result["reviewers"]["ai1"]["status"])


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
