# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest import mock

import requests

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


class _Clock:
    """可读可推进的假时钟：只在 GET 尝试内部推进，便于精确控制本次耗时。"""

    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


class _FailingSession:
    def __init__(self, clock, advance, exc, then=None):
        self.clock = clock
        self.advance = advance
        self.exc = exc
        self.then = then
        self.calls = 0
        self.headers = {}

    def get(self, *args, **kwargs):
        self.calls += 1
        self.clock.now += self.advance
        if self.then is not None and self.calls > 1:
            return self.then
        raise self.exc


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

    def test_empty_final_response_has_no_backoff_without_another_attempt(self):
        for retries in (0, 1, 2):
            with self.subTest(retries=retries):
                session = _Session([_Response("") for _ in range(retries + 1)])
                with mock.patch.object(transport, "_session", return_value=session), \
                        mock.patch.object(transport.time, "sleep") as sleep:
                    self.assertEqual(transport.http_get("https://example.invalid", retries=retries), "")
                self.assertEqual(sleep.call_args_list,
                                 [mock.call(0.4 * (i + 1)) for i in range(retries)])

    def test_http_post_json_preserves_empty_failure_contract(self):
        session = _Session([_Response('{"ok": true}', {"ok": True})])
        with mock.patch.object(transport, "_session", return_value=session):
            self.assertEqual(transport.http_post_json("https://example.invalid", {}), {"ok": True})

    def test_dropped_connection_retries_without_backoff(self):
        clock = _Clock()
        session = _FailingSession(clock, 0.05, requests.ConnectionError("peer closed"),
                                 then=_Response("ok"))
        with mock.patch.object(transport, "_session", return_value=session), \
                mock.patch.object(transport.time, "perf_counter", clock), \
                mock.patch.object(transport.time, "sleep") as sleep:
            self.assertEqual(transport.http_get("https://example.invalid", retries=1), "ok")
        self.assertEqual(session.calls, 2)
        sleep.assert_not_called()

    def test_connect_timeout_keeps_backoff(self):
        clock = _Clock()
        exc = requests.ConnectTimeout("connect timed out")
        self.assertIsInstance(exc, requests.ConnectionError)
        session = _FailingSession(clock, 0.05, exc, then=_Response("ok"))
        with mock.patch.object(transport, "_session", return_value=session), \
                mock.patch.object(transport.time, "perf_counter", clock), \
                mock.patch.object(transport.time, "sleep") as sleep:
            self.assertEqual(transport.http_get("https://example.invalid", retries=1), "ok")
        self.assertEqual(sleep.call_args_list, [mock.call(0.5)])

    def test_slow_connection_error_keeps_backoff(self):
        clock = _Clock()
        session = _FailingSession(clock, 5.0, requests.ConnectionError("timed out at peer"),
                                  then=_Response("ok"))
        with mock.patch.object(transport, "_session", return_value=session), \
                mock.patch.object(transport.time, "perf_counter", clock), \
                mock.patch.object(transport.time, "sleep") as sleep:
            self.assertEqual(transport.http_get("https://example.invalid", retries=1), "ok")
        self.assertEqual(sleep.call_args_list, [mock.call(0.5)])

    def test_dropped_connection_classification(self):
        dropped = transport._connection_dropped
        self.assertTrue(dropped(requests.ConnectionError("reset"), 0.2))
        self.assertFalse(dropped(requests.ConnectionError("reset"), 5.0))
        self.assertFalse(dropped(requests.ConnectTimeout("timeout"), 0.05))
        self.assertFalse(dropped(requests.ReadTimeout("read"), 0.05))
        self.assertFalse(dropped(requests.HTTPError("503"), 0.05))
        self.assertFalse(dropped(ValueError("not a request error"), 0.05))


if __name__ == "__main__":
    unittest.main()
