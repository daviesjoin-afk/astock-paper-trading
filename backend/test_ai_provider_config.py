# -*- coding: utf-8 -*-
"""B5：provider 配置与传输的 owner 收敛。

legacy tuner 以前自带一份厂商目录、模型/base_url 解析与 urllib 调用，
于是同一个厂商有两套默认值、两套超时和两套错误文案。B5 把配置解析放进
``ai_provider_config``，网络只由 ``ai_provider_transport`` 发出。
"""
from __future__ import annotations

import ast
import inspect
import os
import unittest
from unittest import mock

import ai_provider_config as APC
import ai_provider_transport as transport
import deepseek_advisor as DA


def _network_calls(module):
    """Return the names of network primitives a module actually calls."""
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


class ProviderConfigOwnerTests(unittest.TestCase):
    def test_catalog_defaults_match_the_legacy_values(self):
        catalog = APC.provider_catalog()
        self.assertEqual("deepseek-v4-flash", catalog["deepseek"]["default_model"])
        self.assertEqual("https://api.deepseek.com", catalog["deepseek"]["default_base_url"])
        self.assertEqual("https://api.moonshot.cn/v1", catalog["kimi"]["default_base_url"])
        self.assertEqual("https://api.mimo.ai/v1", catalog["mimo"]["default_base_url"])
        catalog["deepseek"]["default_model"] = "mutated"
        self.assertEqual(
            "deepseek-v4-flash", APC.PROVIDER_CATALOG["deepseek"]["default_model"],
        )

    def test_unknown_provider_falls_back_to_deepseek(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "not-a-provider"}):
            self.assertEqual("deepseek", APC.provider_name())

    def test_legacy_config_is_transport_shaped(self):
        with mock.patch.dict(os.environ, {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "secret-key",
            "DEEPSEEK_BASE_URL": "https://example.invalid/v1/",
            "DEEPSEEK_MODEL": "custom-model",
            "DEEPSEEK_TIMEOUT_SECONDS": "12",
        }, clear=False):
            config = APC.legacy_provider_config()
        self.assertEqual("secret-key", config["api_key"])
        self.assertEqual("https://example.invalid/v1", config["base_url"])
        self.assertEqual("custom-model", config["model"])
        self.assertEqual(12.0, config["timeout_seconds"])
        self.assertEqual(APC.LEGACY_SLOT, config["slot"])

    def test_timeout_is_clamped_to_the_transport_bounds(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_TIMEOUT_SECONDS": "99999"}):
            self.assertEqual(transport.MAX_TIMEOUT_SECONDS, APC.timeout_seconds())
        with mock.patch.dict(os.environ, {"DEEPSEEK_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(transport.MIN_TIMEOUT_SECONDS, APC.timeout_seconds())

    def test_missing_api_key_fails_before_any_network_call(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False), \
                mock.patch.object(transport, "call_json") as call:
            with self.assertRaises(RuntimeError) as raised:
                APC.call_json("system", "user")
        self.assertEqual("api_key_missing", str(raised.exception))
        call.assert_not_called()

    def test_legacy_call_json_delegates_to_the_shared_transport(self):
        config = {
            "slot": APC.LEGACY_SLOT, "api_key": "k", "base_url": "https://example.invalid",
            "model": "m", "timeout_seconds": 5.0,
        }
        with mock.patch.object(APC, "legacy_provider_config", return_value=config), \
                mock.patch.object(transport, "call_json",
                                  return_value=({"ok": True}, 3, 4, 12)) as call:
            parsed, in_tokens, out_tokens = APC.call_json("system", "user", 900)
        self.assertEqual(({"ok": True}, 3, 4), (parsed, in_tokens, out_tokens))
        self.assertEqual("system", call.call_args.args[1])
        self.assertEqual(900, call.call_args.kwargs["max_tokens"])

    def test_transport_failures_keep_a_stable_reason(self):
        config = {
            "slot": APC.LEGACY_SLOT, "api_key": "k", "base_url": "https://example.invalid",
            "model": "m", "timeout_seconds": 5.0,
        }
        with mock.patch.object(APC, "legacy_provider_config", return_value=config), \
                mock.patch.object(transport, "call_json",
                                  side_effect=transport.ProviderTransportError(
                                      transport.REASON_NETWORK_ERROR)):
            with self.assertRaises(RuntimeError) as raised:
                APC.call_json("system", "user")
        self.assertEqual(transport.REASON_NETWORK_ERROR, str(raised.exception))

    def test_advisor_exports_stay_compatible(self):
        self.assertEqual(sorted(APC.PROVIDER_CATALOG), sorted(DA.PROVIDER_CATALOG))
        self.assertIs(APC.PROVIDER_CATALOG, DA.PROVIDER_CATALOG)
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "mimo"}):
            self.assertEqual("mimo", DA.provider_name())


class NetworkOwnershipTests(unittest.TestCase):
    """B5：AI provider 的 HTTP 只能出现在共享传输层。"""

    def test_advisor_has_no_direct_network_call_left(self):
        calls = _network_calls(DA)
        self.assertNotIn("urlopen", calls)
        self.assertNotIn("Request", calls)

    def test_config_owner_does_not_perform_network_calls(self):
        calls = _network_calls(APC)
        self.assertNotIn("urlopen", calls)
        self.assertNotIn("Request", calls)

    def test_transport_is_the_only_module_talking_to_chat_completions(self):
        import pathlib
        backend = pathlib.Path(__file__).resolve().parents[1]
        callers = []
        for path in backend.glob("*.py"):
            if path.name.startswith("test_") or path.name == "ai_provider_transport.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "chat/completions" in text:
                callers.append(path.name)
        self.assertEqual([], callers)


if __name__ == "__main__":
    unittest.main()
