"""提供商保存、槽位引用和旧配置迁移；全离线、只使用临时数据库。"""
import json
import os
import sqlite3
import unittest
from unittest import mock

import ai_review_service as S


class SavedProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)

    def provider(self, name="自有服务", model="custom-model", **extra):
        return S.save_provider(self.conn, display_name=name, api_key="fake-provider-secret",
                               base_url="https://custom.example.com/v1", models=[model], **extra)

    def bind(self, slot, provider, model="custom-model"):
        S.update_slot(self.conn, slot, provider_id=provider["id"], model=model, enabled=True)

    def test_empty_install_has_no_vendor_defaults(self):
        view = S.review_settings_view(self.conn)
        self.assertEqual([], view["providers"])
        for cfg in view["slots"].values():
            self.assertEqual("", cfg["base_url"])
            self.assertEqual("", cfg["model"])
            self.assertFalse(cfg["ready"])

    def test_multiple_providers_and_independent_slot_selection(self):
        first = self.provider()
        second = self.provider("另一个服务", "another-model")
        S.save_provider(self.conn, second["id"], base_url="https://another.example.com/v1")
        self.bind("ai1", first)
        self.bind("ai2", second, "another-model")
        before = S.get_slot_config(self.conn, "ai2")
        self.bind("ai1", second, "another-model")
        self.assertEqual(before, S.get_slot_config(self.conn, "ai2"))
        self.assertEqual("https://another.example.com/v1", S.get_slot_config(self.conn, "ai1")["base_url"])
        self.assertEqual(2, len(S.saved_providers(self.conn)))

    def test_shared_updates_and_clear_never_revive_stale_key(self):
        provider = self.provider()
        for slot in S.AI_SLOTS:
            self.bind(slot, provider)
        S.save_provider(self.conn, provider["id"], api_key="fake-rotated-key",
                        base_url="https://rotated.example.com/v1", timeout_seconds=55)
        for slot in S.AI_SLOTS:
            cfg = S.get_slot_config(self.conn, slot)
            self.assertEqual("fake-rotated-key", cfg["api_key"])
            self.assertEqual("https://rotated.example.com/v1", cfg["base_url"])
            self.assertEqual(55, cfg["timeout_seconds"])
        S.save_provider(self.conn, provider["id"], api_key="")
        self.assertTrue(S.get_slots(self.conn)["ai1"]["ready"])
        S.save_provider(self.conn, provider["id"], clear_api_key=True)
        for _ in range(2):
            S.ensure_schema(self.conn)
            self.assertTrue(all(not v["ready"] for v in S.get_slots(self.conn).values()))
            self.assertEqual("", S.get_slot_config(self.conn, "ai1")["api_key"])

    def test_legacy_direct_update_detaches_only_target_slot(self):
        provider = self.provider()
        for slot in S.AI_SLOTS:
            self.bind(slot, provider)
        S.update_slot(self.conn, "ai1", api_key="direct-key", base_url="https://direct.example.com")
        self.assertEqual("", S.get_slot_config(self.conn, "ai1")["provider_id"])
        self.assertEqual(provider["id"], S.get_slot_config(self.conn, "ai2")["provider_id"])
        self.assertEqual("fake-provider-secret", S.get_slot_config(self.conn, "ai2")["api_key"])

    def test_migration_preserves_configs_and_runs_once(self):
        S._ensure_slot_tables(self.conn)
        for slot in S.AI_SLOTS:
            self.conn.execute("INSERT INTO ai_provider_slots VALUES(?,?,?,?,?,?,?,?)",
                              (slot, "旧配置 "+slot, "fake-old-"+slot, "https://old.example.com/v1",
                               "old-model", slot == "ai1", 80, "2026-10-20"))
        first = S.review_settings_view(self.conn)
        second = S.review_settings_view(self.conn)
        self.assertEqual(first, second)
        self.assertEqual(2, len(first["providers"]))
        for slot in S.AI_SLOTS:
            cfg = S.get_slot_config(self.conn, slot)
            self.assertEqual("fake-old-"+slot, cfg["api_key"])
            self.assertEqual("old-model", cfg["model"])
            self.assertEqual(80, cfg["timeout_seconds"])
            self.assertEqual(slot == "ai1", cfg["enabled"])
        S.save_provider(self.conn, first["providers"][0]["id"], clear_api_key=True)
        self.assertEqual("", S.get_slot_config(self.conn, "ai1")["api_key"])

    def test_public_views_never_return_key(self):
        provider = self.provider()
        self.bind("ai1", provider)
        payload = json.dumps(S.review_settings_view(self.conn))
        self.assertNotIn("fake-provider-secret", payload)
        self.assertNotIn('"api_key"', payload)
        self.assertNotIn("api_key", provider)

    def test_model_refresh_uses_saved_config_and_keeps_manual_models(self):
        provider = self.provider()
        with mock.patch.object(S.transport, "list_models", return_value={
            "models": ["remote-model"], "count": 1, "latency_ms": 2,
        }) as call:
            result = S.refresh_provider_models(self.conn, provider["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(["custom-model", "remote-model"], result["models"])
        self.assertEqual("fake-provider-secret", call.call_args.args[0]["api_key"])
        self.assertEqual(result["models"], S.saved_providers(self.conn)[0]["models"])
        self.bind("ai2", provider, "remote-model")

    def test_refresh_failure_keeps_saved_models(self):
        provider = self.provider()
        with mock.patch.object(S.transport, "list_models", side_effect=S.transport.ProviderTransportError(
            S.transport.REASON_NETWORK_ERROR)):
            result = S.refresh_provider_models(self.conn, provider["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(["custom-model"], S.saved_providers(self.conn)[0]["models"])

    def test_selected_provider_reaches_call_and_clearing_key_prevents_call(self):
        provider = self.provider("任意提供商", "generic-model")
        self.bind("ai1", provider, "generic-model")
        with mock.patch.object(S, "_call_slot", return_value=({"ok": True}, 1, 1, 3)) as call:
            self.assertTrue(S.test_slot(self.conn, "ai1")["ok"])
            cfg = call.call_args.args[0]
            self.assertEqual("generic-model", cfg["model"])
            self.assertEqual("https://custom.example.com/v1", cfg["base_url"])
            self.assertEqual("fake-provider-secret", cfg["api_key"])
        S.save_provider(self.conn, provider["id"], clear_api_key=True)
        with mock.patch.object(S, "_call_slot") as call:
            self.assertFalse(S.test_slot(self.conn, "ai1")["ok"])
            call.assert_not_called()

    def test_models_from_old_endpoint_are_not_saved_after_provider_changes(self):
        provider = self.provider()
        def upstream(_config):
            S.save_provider(self.conn, provider["id"], base_url="https://new.example.com/v1")
            return {"models": ["old-endpoint-model"]}
        with mock.patch.object(S.transport, "list_models", side_effect=upstream):
            result = S.refresh_provider_models(self.conn, provider["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(["custom-model"], S.saved_providers(self.conn)[0]["models"])

    def test_invalid_selection_does_not_change_slot(self):
        provider = self.provider()
        self.bind("ai1", provider)
        before = S.get_slot_config(self.conn, "ai1")
        for provider_id, model in (("missing", "custom-model"), (provider["id"], "unknown")):
            with self.assertRaises(ValueError):
                S.update_slot(self.conn, "ai1", provider_id=provider_id, model=model)
            self.assertEqual(before, S.get_slot_config(self.conn, "ai1"))
        with self.assertRaises(ValueError):
            S.update_slot(self.conn, "ai1", provider_id=provider["id"], api_key="mixed")

    def test_invalid_provider_updates_do_not_overwrite_saved_config(self):
        provider = self.provider()
        for updates in ({"base_url": "not-a-url"}, {"display_name": ""}, {"models": "model"},
                        {"timeout_seconds": 301}, {"models": [""]}, {"api_key": 123}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                S.save_provider(self.conn, provider["id"], **updates)
            self.assertEqual(provider, S.saved_providers(self.conn)[0])

    def test_api_routes_validate_confirmation_and_save_selection(self):
        import asyncio
        from fastapi import HTTPException
        from starlette.requests import Request
        import api_settings as API

        def request(body):
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode()}
            return Request({"type": "http", "method": "POST", "path": "/"}, receive)

        owner = mock.Mock()
        def save(provider_id=None, **body):
            provider = S.save_provider(self.conn, provider_id, **body)
            return {"provider": provider, "review": S.review_settings_view(self.conn)}
        def update(slot, **body):
            S.update_slot(self.conn, slot, **body)
            return S.review_settings_view(self.conn)
        owner.save_ai_provider_fn.side_effect = save
        owner.update_ai_slot_fn.side_effect = update
        with mock.patch.object(API, "_get_adaptive", return_value=owner):
            body = {"display_name": "独立服务", "base_url": "https://own.example.com/v1",
                    "api_key": "fake-http-key", "models": ["own-model"]}
            with self.assertRaises(HTTPException) as error:
                asyncio.run(API.create_ai_provider(request(body), confirmed=False))
            self.assertEqual(409, error.exception.status_code)
            response = asyncio.run(API.create_ai_provider(request(body), confirmed=True))
            self.assertNotIn("fake-http-key", json.dumps(response))
            provider_id = response["provider"]["id"]
            response = asyncio.run(API.update_ai_review_slot("ai1", request({
                "provider_id": provider_id, "model": "own-model", "enabled": True,
            }), confirmed=True))
            self.assertTrue(response["slots"]["ai1"]["ready"])
            for invalid in (None, 3):
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(API.update_ai_review_slot("ai1", request({"provider_id": invalid}), confirmed=True))
                self.assertEqual(422, error.exception.status_code)
