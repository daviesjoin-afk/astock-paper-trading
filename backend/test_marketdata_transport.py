# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import marketdata_transport as transport


class _Response:
    def __init__(self, text="", payload=None):
        self.text = text
        self.payload = payload
        self.encoding = None

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}

    def get(self, *args, **kwargs):
        return next(self.responses)

    def post(self, *args, **kwargs):
        return next(self.responses)


class MarketdataTransportTests(unittest.TestCase):
    def test_http_get_returns_text_and_applies_encoding(self):
        response = _Response("ok")
        session = _Session([response])
        with mock.patch.object(transport, "_session", return_value=session):
            self.assertEqual(transport.http_get("https://example.invalid", encoding="gbk"), "ok")
        self.assertEqual(response.encoding, "gbk")

    def test_http_get_retries_empty_response(self):
        session = _Session([_Response(""), _Response("ok")])
        with mock.patch.object(transport, "_session", return_value=session), \
                mock.patch.object(transport.time, "sleep") as sleep:
            self.assertEqual(transport.http_get("https://example.invalid", retries=1), "ok")
        self.assertEqual(sleep.call_count, 1)

    def test_http_post_json_preserves_empty_failure_contract(self):
        session = _Session([_Response('{"ok": true}', {"ok": True})])
        with mock.patch.object(transport, "_session", return_value=session):
            self.assertEqual(transport.http_post_json("https://example.invalid", {}), {"ok": True})


if __name__ == "__main__":
    unittest.main()
