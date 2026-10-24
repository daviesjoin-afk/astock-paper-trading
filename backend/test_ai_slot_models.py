# -*- coding: utf-8 -*-
"""上游模型列表（GET /models）的离线测试。

覆盖：缺 Key / 非法地址在发请求前就 fail closed、响应解析与去重排序、
非列表响应被拒绝。全部离线，不发起真实网络请求。
"""
import json
import unittest
from unittest import mock

import ai_provider_transport as T


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def _config(**overrides):
    base = {"api_key": "sk-test", "base_url": "https://api.example.com/v1", "slot": "ai1"}
    base.update(overrides)
    return base


class ModelsUrlTests(unittest.TestCase):
    def test_models_url_is_derived_from_base(self):
        self.assertEqual("https://api.example.com/v1/models",
                         T.models_url("https://api.example.com/v1"))

    def test_models_url_accepts_trailing_slash_and_full_path(self):
        self.assertEqual("https://api.example.com/v1/models",
                         T.models_url("https://api.example.com/v1/"))
        self.assertEqual("https://api.example.com/v1/models",
                         T.models_url("https://api.example.com/v1/chat/completions"))

    def test_empty_base_url_raises(self):
        with self.assertRaises(T.ProviderTransportError):
            T.models_url("")


class ListModelsTests(unittest.TestCase):
    def test_missing_key_fails_before_any_request(self):
        with mock.patch.object(T.urllib.request, "urlopen") as opener:
            with self.assertRaises(T.ProviderTransportError):
                T.list_models(_config(api_key=""))
            opener.assert_not_called()

    def test_invalid_base_url_fails_before_any_request(self):
        with mock.patch.object(T.urllib.request, "urlopen") as opener:
            with self.assertRaises(T.ProviderTransportError):
                T.list_models(_config(base_url="not-a-url"))
            opener.assert_not_called()

    def test_parses_dedupes_and_sorts(self):
        payload = {"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "a-model"}, "c-model"]}
        with mock.patch.object(T.urllib.request, "urlopen",
                               return_value=_FakeResponse(payload)):
            result = T.list_models(_config())
        self.assertEqual(["a-model", "b-model", "c-model"], result["models"])
        self.assertEqual(3, result["count"])

    def test_payload_without_data_list_is_rejected(self):
        with mock.patch.object(T.urllib.request, "urlopen",
                               return_value=_FakeResponse({"error": "nope"})):
            with self.assertRaises(T.ProviderTransportError):
                T.list_models(_config())

    def test_non_json_body_is_rejected(self):
        class _Bad(_FakeResponse):
            def __init__(self):
                self._body = b"<html>gateway</html>"

        with mock.patch.object(T.urllib.request, "urlopen", return_value=_Bad()):
            with self.assertRaises(T.ProviderTransportError):
                T.list_models(_config())


if __name__ == "__main__":
    unittest.main()
