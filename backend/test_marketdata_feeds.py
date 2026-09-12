# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import data_fetcher as dfc
import marketdata_feeds as feeds


class _FakeFeed:
    def __init__(self, name, rows_by_code, calls):
        self.name = name
        self.rows_by_code = rows_by_code
        self.calls = calls

    def fetch_realtime(self, codes):
        codes = list(codes)
        self.calls.append((self.name, codes))
        return [dict(self.rows_by_code[code]) for code in codes if code in self.rows_by_code]


class DataFeedContractTests(unittest.TestCase):
    def test_protocol_is_structural(self):
        fake = _FakeFeed("fake", {}, [])
        self.assertIsInstance(fake, feeds.DataFeed)
        self.assertIsInstance(
            feeds.EastmoneyRealtimeFeed(
                get_json=lambda *_args, **_kwargs: {},
                secid=lambda code: code,
                row_parser=lambda _row: None,
                reset_data_source=lambda *_args, **_kwargs: None,
                ut="test",
                fields="f2,f12",
            ),
            feeds.DataFeed,
        )
        self.assertIsInstance(
            feeds.TencentRealtimeFeed(
                http_get=lambda *_args, **_kwargs: "",
                parser=lambda *_args, **_kwargs: [],
                reset_data_source=lambda *_args, **_kwargs: None,
            ),
            feeds.DataFeed,
        )
        self.assertIsInstance(
            feeds.SinaRealtimeFeed(
                session_factory=lambda: None,
                headers={},
                parser=lambda *_args, **_kwargs: [],
                reset_data_source=lambda *_args, **_kwargs: None,
            ),
            feeds.DataFeed,
        )

    def test_normalize_codes_is_ordered_deduplicated_and_strict(self):
        self.assertEqual(
            feeds.normalize_codes(["000001", "000001", "600000", "bad", 123]),
            ["000001", "600000"],
        )

    def test_usable_quote_requires_finite_positive_price_and_timestamp(self):
        self.assertTrue(feeds.usable_quote({"price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00"}))
        self.assertFalse(feeds.usable_quote({"price": 0, "quote_at": "2026-09-12T10:00:00+08:00"}))
        self.assertFalse(feeds.usable_quote({"price": float("nan"), "quote_at": "2026-09-12T10:00:00+08:00"}))
        self.assertFalse(feeds.usable_quote({"price": 10.0, "quote_at": None}))

    def test_chain_only_asks_fallback_for_missing_or_unusable_codes(self):
        calls = []
        primary = _FakeFeed(
            "primary",
            {
                "000001": {"code": "000001", "price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00"},
                "000002": {"code": "000002", "price": 0, "quote_at": "2026-09-12T10:00:00+08:00"},
            },
            calls,
        )
        fallback = _FakeFeed(
            "fallback",
            {"000002": {"code": "000002", "price": 20.0, "quote_at": "2026-09-12T10:00:01+08:00"}},
            calls,
        )
        chain = feeds.DataFeedChain((primary, fallback), retry_last_feed=False)
        rows = chain.fetch_realtime(["000001", "000002"])
        self.assertEqual([row["code"] for row in rows], ["000001", "000002"])
        self.assertEqual(calls, [("primary", ["000001", "000002"]), ("fallback", ["000002"])])

    def test_adding_third_feed_requires_only_composition_change(self):
        calls = []
        first = _FakeFeed("first", {}, calls)
        second = _FakeFeed("second", {}, calls)
        third = _FakeFeed(
            "third",
            {"000001": {"code": "000001", "price": 9.5, "quote_at": "2026-09-12T10:00:00+08:00"}},
            calls,
        )
        chain = feeds.DataFeedChain((first, second, third), retry_last_feed=False)
        self.assertEqual(chain.fetch_realtime(["000001"])[0]["price"], 9.5)
        self.assertEqual(calls, [("first", ["000001"]), ("second", ["000001"]), ("third", ["000001"])])

    def test_chain_preserves_legacy_last_feed_retry(self):
        calls = []
        reset = mock.Mock()
        sleep = mock.Mock()

        class LastFeed:
            name = "last"

            def __init__(self):
                self.attempt = 0

            def fetch_realtime(self, codes):
                self.attempt += 1
                calls.append(list(codes))
                if self.attempt == 1:
                    return []
                return [{"code": "000001", "price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00"}]

        chain = feeds.DataFeedChain(
            (LastFeed(),), reset_data_source=reset, sleep=sleep, retry_last_feed=True,
        )
        rows = chain.fetch_realtime(["000001"])
        self.assertEqual([row["code"] for row in rows], ["000001"])
        self.assertEqual(calls, [["000001"], ["000001"]])
        reset.assert_called_once()
        sleep.assert_called_once_with(0.25)


class EastmoneyRealtimeFeedTests(unittest.TestCase):
    @staticmethod
    def _row_parser(raw):
        code = str(raw.get("f12") or "")
        if not code:
            return None
        return {
            "code": code,
            "price": float(raw.get("f2") or 0),
            "quote_at": "2026-09-12T10:00:00+08:00",
        }

    def test_complete_batch_preserves_legacy_metadata_shape(self):
        reset = mock.Mock()
        sleep = mock.Mock()

        def get_json(_url, _params, **_kwargs):
            return {"data": {"diff": [{"f12": "000001", "f2": 10}, {"f12": "600000", "f2": 20}]}}

        feed = feeds.EastmoneyRealtimeFeed(
            get_json=get_json,
            secid=lambda code: ("1." if code.startswith("6") else "0.") + code,
            row_parser=self._row_parser,
            reset_data_source=reset,
            ut="test",
            fields="f2,f12",
            hosts=("test-host",),
            attempts=1,
            sleep=sleep,
        )
        result = feed.fetch_realtime_with_meta(["000001", "000001", "600000"])
        self.assertEqual([row["code"] for row in result["rows"]], ["000001", "600000"])
        self.assertEqual(result["expected"], 2)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["missing_codes"], [])
        self.assertEqual(result["batches"][0]["requested"], 2)
        self.assertEqual(result["batches"][0]["returned"], 2)
        reset.assert_not_called()
        sleep.assert_not_called()

    def test_partial_batch_reports_missing_code_and_fails_completeness(self):
        reset = mock.Mock()
        sleep = mock.Mock()

        def get_json(_url, _params, **_kwargs):
            return {"data": {"diff": [{"f12": "000001", "f2": 10}]}}

        feed = feeds.EastmoneyRealtimeFeed(
            get_json=get_json,
            secid=lambda code: "0." + code,
            row_parser=self._row_parser,
            reset_data_source=reset,
            ut="test",
            fields="f2,f12",
            hosts=("test-host",),
            attempts=1,
            sleep=sleep,
        )
        result = feed.fetch_realtime_with_meta(["000001", "000002"])
        self.assertEqual([row["code"] for row in result["rows"]], ["000001"])
        self.assertEqual(result["expected"], 2)
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["coverage_pct"], 50.0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_codes"], ["000002"])
        self.assertEqual(result["batches"][0]["missing_codes"], ["000002"])
        reset.assert_called_once_with("实时行情源空响应")
        sleep.assert_called_once_with(0.25)


class DataFetcherFacadeTests(unittest.TestCase):
    def test_primary_quote_facade_preserves_list_and_metadata_modes(self):
        rows = [{"code": "000001", "price": 10.0}]
        metadata = {
            "rows": rows,
            "expected": 1,
            "returned": 1,
            "coverage_pct": 100.0,
            "complete": True,
            "batches": [],
            "missing_codes": [],
        }
        feed = mock.Mock()
        feed.fetch_realtime.return_value = rows
        feed.fetch_realtime_with_meta.return_value = metadata
        with mock.patch.object(dfc, "_eastmoney_realtime_feed", return_value=feed) as factory:
            self.assertEqual(dfc.fetch_realtime_for_codes(["000001"]), rows)
            self.assertEqual(dfc.fetch_realtime_for_codes(["000001"], return_meta=True), metadata)
        factory.assert_has_calls([mock.call(dfc._REALTIME_FIELDS), mock.call(dfc._REALTIME_FIELDS)])
        feed.fetch_realtime.assert_called_once_with(["000001"])
        feed.fetch_realtime_with_meta.assert_called_once_with(["000001"])

    def test_independent_quote_facade_delegates_without_changing_public_api(self):
        expected = [{"code": "000001", "price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00"}]
        feed = mock.Mock()
        feed.fetch_realtime.return_value = expected
        with mock.patch.object(dfc, "_independent_realtime_feed", return_value=feed):
            self.assertEqual(dfc.fetch_independent_realtime_for_codes(["000001"]), expected)
        feed.fetch_realtime.assert_called_once_with(["000001"])

    def test_provider_specific_facades_delegate_to_matching_adapters(self):
        tencent = mock.Mock()
        sina = mock.Mock()
        tencent.fetch_realtime.return_value = [{"code": "000001"}]
        sina.fetch_realtime.return_value = [{"code": "000002"}]
        with mock.patch.object(dfc, "_tencent_realtime_feed", return_value=tencent):
            self.assertEqual(dfc.fetch_tencent_realtime_for_codes(["000001"]), [{"code": "000001"}])
        with mock.patch.object(dfc, "_sina_realtime_feed", return_value=sina):
            self.assertEqual(dfc._fetch_sina_realtime_for_codes(["000002"]), [{"code": "000002"}])


class TencentRealtimeFeedTests(unittest.TestCase):
    def test_retries_only_missing_codes_and_preserves_order(self):
        requested = []
        reset = mock.Mock()
        sleep = mock.Mock()

        def http_get(url, **_kwargs):
            requested.append(url.split("q=", 1)[1].split(","))
            return "attempt"

        parser_calls = []

        def parser(_text, *, attempt, allowed_codes):
            parser_calls.append((attempt, list(allowed_codes)))
            if attempt == 1:
                return [{"code": "000001", "price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00"}]
            return [{"code": "600000", "price": 20.0, "quote_at": "2026-09-12T10:00:01+08:00"}]

        feed = feeds.TencentRealtimeFeed(
            http_get=http_get,
            parser=parser,
            reset_data_source=reset,
            sleep=sleep,
        )
        rows = feed.fetch_realtime(["000001", "600000"])
        self.assertEqual([row["code"] for row in rows], ["000001", "600000"])
        self.assertEqual(parser_calls, [(1, ["000001", "600000"]), (2, ["600000"])])
        self.assertEqual(requested[1], ["sh600000"])
        reset.assert_not_called()
        sleep.assert_called_once_with(0.25)

    def test_empty_response_resets_between_attempts(self):
        reset = mock.Mock()
        sleep = mock.Mock()
        feed = feeds.TencentRealtimeFeed(
            http_get=lambda *_args, **_kwargs: "",
            parser=lambda *_args, **_kwargs: [],
            reset_data_source=reset,
            sleep=sleep,
        )
        self.assertEqual(feed.fetch_realtime(["000001"]), [])
        self.assertEqual(reset.call_count, 2)
        self.assertEqual(sleep.call_count, 2)


class SinaRealtimeFeedTests(unittest.TestCase):
    def test_retries_empty_response_and_delegates_parser(self):
        reset = mock.Mock()
        sleep = mock.Mock()
        parser = mock.Mock(return_value=[{
            "code": "000001", "price": 10.0, "quote_at": "2026-09-12T10:00:00+08:00",
        }])

        class Response:
            def __init__(self, text):
                self.text = text
                self.encoding = None

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response("" if self.calls == 1 else "quote")

        session = Session()
        feed = feeds.SinaRealtimeFeed(
            session_factory=lambda: session,
            headers={"User-Agent": "test"},
            parser=parser,
            reset_data_source=reset,
            sleep=sleep,
        )
        rows = feed.fetch_realtime(["000001"])
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(session.calls, 2)
        reset.assert_called_once()
        sleep.assert_called_once_with(0.25)
        parser.assert_called_once_with("quote", allowed_codes=["000001"])


if __name__ == "__main__":
    unittest.main()
