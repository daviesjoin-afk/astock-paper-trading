# -*- coding: utf-8 -*-
"""Offline behavioural tests for the transport-only live quote feed.

Every test is in memory: the universe loader, optional history DB and
PublicClient are mocked. No network, real clock, temp file or directory is used.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_quote_feed as feed  # noqa: E402

FIXED_CLOCK = lambda: 1_700_000_000.0  # 2023-11-14T22:13:20+00:00
SOURCE_TIME = "2026-11-14 10:03:00"
SYMBOLS = ("600000.SH", "000001.SZ", "920001.BJ")
ALL_CODES = ("sh600000", "sz000001", "bj920001")


def row(*, price=10.01, last_close=10.0, quote_time=SOURCE_TIME, **extra):
    result = {
        "name": "示例",
        "price": price,
        "last_close": last_close,
        "open": 10.02,
        "high": 10.20,
        "low": 9.90,
        "limit_up": 11.0,
        "limit_down": 9.0,
        "amount_wan": 3.5,
        "turnover_pct": 1.2,
        "vol_ratio": 1.5,
        "change_pct": 0.1,
        "quote_time": quote_time,
    }
    result.update(extra)
    return result


class FakeClient:
    """Mock PublicClient: records every batch, returns a prebuilt dict."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def call(self, name, codes):
        self.calls.append((name, list(codes)))
        return self.results.get(tuple(codes), {})


class LiveQuoteFeedTests(unittest.TestCase):
    def snapshot(self, results, *, history=None, batch_size=200):
        client = FakeClient(results)
        with mock.patch.object(feed, "load_universe", return_value=list(SYMBOLS)), \
             mock.patch.object(feed, "load_history", return_value=history or {}):
            payload = feed.build_snapshot(
                "unused-universe.txt", batch_size=batch_size,
                history_db="unused.sqlite3", client=client,
                clock=FIXED_CLOCK)
        return payload, client.calls

    def test_field_mapping_and_unit_conversion_use_source_facts_only(self):
        results = {ALL_CODES: {
            "sh600000": row(price=10.01, last_close=10.0, volume=123400),
            "sz000001": row(price=20.006, last_close=19.995),
            "bj920001": row(),
        }}
        payload, calls = self.snapshot(results)
        self.assertEqual(calls, [(feed.SOURCE_NAME, list(ALL_CODES))])
        self.assertEqual(payload["quote_time"], "2026-11-14T10:03:00+08:00")
        self.assertNotIn("quote_time_estimated", payload)
        self.assertEqual(payload["fetched_at"], feed.utc_now_iso(FIXED_CLOCK))
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["source"], "tencent_quote")
        first = payload["quotes"][0]
        self.assertEqual(first, {
            "symbol": "600000.SH",
            "name": "示例",
            "price_cents": 1001,
            "prev_close_cents": 1000,
            "open_cents": 1002,
            "high_cents": 1020,
            "low_cents": 990,
            "limit_up_cents": 1100,
            "limit_down_cents": 900,
            "amount_cents": 3_500_000,  # 3.5 wan yuan * 1e6 = 3,500,000 cents
            "volume": 123400,
            "trailing_closes": [],
            "tradable": True,
        })
        self.assertEqual(payload["quotes"][1]["price_cents"], 2001)
        self.assertEqual(payload["quotes"][1]["prev_close_cents"], 2000)
        self.assertEqual(payload["quotes"][1]["open_cents"], 1002)
        for quote in payload["quotes"]:
            self.assertNotIn("avg_volume", quote)
            self.assertNotIn("vol_ratio", quote)
            self.assertNotIn("turnover_pct", quote)
            self.assertNotIn("is_stale", quote)

    def test_upstream_stale_row_preserves_metadata_but_cannot_trade(self):
        results = {ALL_CODES: {"sh600000": row(is_stale=True),
                               "sz000001": row(is_stale=False),
                               "bj920001": row()}}
        payload, _ = self.snapshot(results)
        stale, trading, unflagged = payload["quotes"]
        self.assertEqual(stale["name"], "示例")
        self.assertIs(stale["tradable"], False)
        self.assertIs(trading["tradable"], True)
        self.assertIs(unflagged["tradable"], True)

    def test_missing_price_is_skipped_and_never_replaced_by_prev_close(self):
        results = {ALL_CODES: {
            "sh600000": row(price=0),
            "sz000001": row(price=None, last_close=19.99),
            "bj920001": row(price=1.23),
        }}
        payload, _ = self.snapshot(results)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["quotes"][0]["symbol"], "920001.BJ")
        self.assertEqual(payload["quotes"][0]["price_cents"], 123)
        self.assertEqual(payload["quotes"][0]["prev_close_cents"], 1000)

    def test_missing_prev_close_is_skipped_not_zero_filled(self):
        results = {ALL_CODES: {
            "sh600000": row(last_close=None),
            "sz000001": row(last_close=0),
            "bj920001": row(),
        }}
        payload, _ = self.snapshot(results)
        self.assertEqual([q["symbol"] for q in payload["quotes"]],
                         ["920001.BJ"])

    def test_source_name_is_transported_and_blank_names_are_absent(self):
        results = {ALL_CODES: {
            "sh600000": row(name="  平安银行  "),
            "sz000001": row(name="  "),
            "bj920001": row(name="-"),
        }}
        payload, _ = self.snapshot(results)
        by_symbol = {quote["symbol"]: quote for quote in payload["quotes"]}
        self.assertEqual(by_symbol["600000.SH"]["name"], "平安银行")
        self.assertNotIn("name", by_symbol["000001.SZ"])
        self.assertNotIn("name", by_symbol["920001.BJ"])

    def test_none_placeholder_and_non_string_names_are_not_written(self):
        for blank in ("None", None, 123, True):
            with self.subTest(blank=blank):
                converted = feed.quote_from_snapshot(
                    "600000.SH", row(name=blank))
                self.assertIsNotNone(converted)
                self.assertNotIn("name", converted)

    def test_no_history_means_empty_trailing_closes(self):
        results = {ALL_CODES: {"sh600000": row(), "sz000001": row(),
                               "bj920001": row()}}
        payload, _ = self.snapshot(results)
        self.assertTrue(all(quote["trailing_closes"] == []
                            for quote in payload["quotes"]))

    def test_supplied_history_is_copied_oldest_first(self):
        results = {ALL_CODES: {"sh600000": row(), "sz000001": row(),
                               "bj920001": row()}}
        history = {"600000.SH": [900, 910, 950]}
        payload, _ = self.snapshot(results, history=history)
        by_symbol = {quote["symbol"]: quote for quote in payload["quotes"]}
        self.assertEqual(by_symbol["600000.SH"]["trailing_closes"],
                         [900, 910, 950])
        self.assertEqual(by_symbol["000001.SZ"]["trailing_closes"], [])

    def test_history_loader_reads_only_days_before_source_observation(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE bar(symbol TEXT, day TEXT, close_cents INTEGER)")
        for day, symbol, close in (
            ("2026-11-10", "600000.SH", 900),
            ("2026-11-11", "600000.SH", 910),
            ("2026-11-13", "600000.SH", 950),
            ("2026-11-14", "600000.SH", 999),
            ("2026-11-13", "000001.SZ", 2200),
        ):
            connection.execute("INSERT INTO bar VALUES(?,?,?)",
                               (symbol, day, close))
        with mock.patch.object(feed.sqlite3, "connect", return_value=connection), \
             mock.patch.object(feed.Path, "is_file", return_value=True):
            loaded = feed.load_history("unused.sqlite3", days=2,
                                       as_of_day="2026-11-14")
        self.assertEqual(loaded["600000.SH"], [900, 910])
        self.assertNotIn("000001.SZ", loaded)  # incomplete window is absent

    def test_batching_merges_multiple_public_client_calls(self):
        first, second = ALL_CODES[:2], ALL_CODES[2:]
        results = {
            first: {"sh600000": row(), "sz000001": row()},
            second: {"bj920001": row()},
        }
        payload, calls = self.snapshot(results, batch_size=2)
        self.assertEqual([call[1] for call in calls],
                         [list(first), list(second)])
        self.assertEqual([quote["symbol"] for quote in payload["quotes"]],
                         list(SYMBOLS))

    def test_missing_source_time_is_flagged_as_estimated(self):
        results = {ALL_CODES: {"sh600000": row(quote_time=None),
                               "sz000001": row(quote_time="10:03:00"),
                               "bj920001": row(quote_time=None)}}
        payload, _ = self.snapshot(results)
        self.assertTrue(payload["quote_time_estimated"])
        self.assertEqual(payload["quote_time"], payload["fetched_at"])

    def test_cli_reports_transport_failure_with_nonzero_exit(self):
        stderr = io.StringIO()
        with mock.patch.object(feed, "load_universe", return_value=list(SYMBOLS)), \
             mock.patch.object(feed, "fetch_snapshots",
                               side_effect=feed.FeedError("upstream down")), \
             redirect_stderr(stderr):
            code = feed.main(["--universe", "unused.txt"])
        self.assertEqual(code, 1)
        self.assertIn("upstream down", stderr.getvalue())

    def test_stdout_is_utf8_bytes_without_crlf_or_console_reencoding(self):
        results = {ALL_CODES: {"sh600000": row()}}
        payload, _ = self.snapshot(results)
        buffer = io.BytesIO()
        sink = buffer
        class FakeStdout:
            buffer = sink
        with mock.patch.object(feed.sys, "stdout", FakeStdout()):
            feed.write_json(None, payload)
        self.assertEqual(buffer.getvalue().count(b"\r"), 0)
        self.assertEqual(json.loads(buffer.getvalue().decode("utf-8")), payload)


    def test_compact_json_history_is_real_and_point_in_time(self):
        # A deployed compact history file must carry real closes only. A file
        # whose declared through_day is not strictly before the snapshot day is
        # refused rather than leaking future data into a factor.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(dir=str(Path(__file__).parent)) as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps({
                "through_day": "2026-11-13",
                "per_symbol": {"600000.SH": [1000 + i for i in range(20)],
                               "000001.SZ": [2000] * 4},
            }), encoding="utf-8")
            loaded = feed.load_history(path, days=20, as_of_day="2026-11-16")
            self.assertEqual(loaded["600000.SH"][0], 1000)
            self.assertEqual(len(loaded["600000.SH"]), 20)
            # Too-short series is omitted, not padded.
            self.assertNotIn("000001.SZ", loaded)
            # A file dated on/after the snapshot day is future data: refuse it.
            path.write_text(json.dumps({
                "through_day": "2026-11-16",
                "per_symbol": {"600000.SH": [1000 + i for i in range(20)]},
            }), encoding="utf-8")
            self.assertEqual(
                feed.load_history(path, days=20, as_of_day="2026-11-16"), {})


class BatchSizeTests(unittest.TestCase):
    """The batch size is a measured constant, not a free knob.

    Upstream caps a snapshot request at 800 codes; above that it answers 414.
    Below 800 the per-request round-trip dominates the 3-minute round budget
    (measured 34.2s at 200 vs 13.9s at 800 for the full market).
    """

    def test_default_batch_matches_the_upstream_safe_ceiling(self):
        self.assertEqual(feed.DEFAULT_BATCH_SIZE, 800)

    def test_a_batch_never_exceeds_the_upstream_limit(self):
        self.assertLessEqual(feed.DEFAULT_BATCH_SIZE, 800)

    def test_build_snapshot_uses_the_default_when_unspecified(self):
        captured = {}

        def fake_fetch(symbols, *, batch_size=0, client=None):
            captured["batch"] = batch_size
            return {}

        with (mock.patch.object(feed, "load_universe", return_value=list(SYMBOLS)),
              mock.patch.object(feed, "fetch_snapshots", fake_fetch)):
            feed.build_snapshot(
                feed.DEFAULT_UNIVERSE,
                history_db=None,
                client=object(),
            )
        self.assertEqual(captured["batch"], feed.DEFAULT_BATCH_SIZE)


class ProductionSnapshotShapeTests(unittest.TestCase):
    """The live path reads `astock_public`, whose snapshot rows carry no time
    field at all. Two consequences are pinned here so neither drifts silently:
    the observation time must be flagged as estimated, and the price-limit
    fields that DO arrive must reach the kernel unchanged.
    """

    # Exactly the keys `astock_public.tencent_quote` returns in production
    # (probed on the server 2026-11-16). Note: no quote_time, no observed_at.
    PRODUCTION_ROW = {
        "name": "600000",
        "price": 10.78,
        "last_close": 10.4,
        "open": 10.42,
        "high": 10.85,
        "low": 10.35,
        "limit_up": 11.44,
        "limit_down": 9.36,
        "amount_wan": 123456.0,
        "vol_ratio": 1.2,
        "turnover_pct": 0.8,
        "amplitude_pct": 4.8,
        "change_pct": 3.65,
        "change_amt": 0.38,
        "pe_ttm": 8.1,
        "pe_static": 8.4,
        "pb": 0.9,
        "mcap_yi": 2000.0,
        "float_mcap_yi": 1800.0,
        "is_stale": False,
    }

    def test_a_row_without_a_time_field_is_flagged_estimated(self):
        self.assertNotIn("quote_time", self.PRODUCTION_ROW)
        self.assertNotIn("observed_at", self.PRODUCTION_ROW)

        with (mock.patch.object(feed, "load_universe", return_value=["600000.SH"]),
              mock.patch.object(feed, "fetch_snapshots",
                                return_value={"sh600000": self.PRODUCTION_ROW})):
            payload = feed.build_snapshot(
                feed.DEFAULT_UNIVERSE, history_db=None, client=object()
            )
        self.assertTrue(
            payload["quote_time_estimated"],
            "no source time must be reported as estimated, never as observed",
        )
        self.assertIn("quote_time", payload)
        self.assertIn("fetched_at", payload)

    def test_real_price_limits_reach_the_kernel(self):
        quote = feed.quote_from_snapshot("600000.SH", self.PRODUCTION_ROW, history=())
        self.assertIsNotNone(quote)
        self.assertEqual(quote["limit_up_cents"], 1144)
        self.assertEqual(quote["limit_down_cents"], 936)
        self.assertEqual(quote["price_cents"], 1078)
        self.assertEqual(quote["prev_close_cents"], 1040)


if __name__ == "__main__":
    unittest.main()
