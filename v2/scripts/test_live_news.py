#!/usr/bin/env python3
"""Offline tests for the news block the observer receives.

The rules under test: an item with no matching symbol is dropped, a single
character name never links, a stale wire item is excluded, a failing provider
does not blind the other, and the block stays bounded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_news as news  # noqa: E402

NOW = datetime(2026, 11, 23, 14, 30, tzinfo=news.SHANGHAI)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def call(self, name, **kwargs):
        self.calls.append(name)
        value = self.rows.get(name)
        if isinstance(value, Exception):
            raise value
        return value


def stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


class LinkTests(unittest.TestCase):
    def test_an_item_naming_no_watched_symbol_is_dropped(self):
        items = [{"title": "海外市场综述", "summary": "", "time": stamp(5), "wire": "w"}]
        names = {"600000.SH": "浦发银行"}
        self.assertEqual(news.link_symbols(items, names, watch={"600000.SH"}), [])

    def test_a_name_links_the_symbol(self):
        items = [{"title": "浦发银行发布三季度报告", "summary": "", "time": stamp(5), "wire": "w"}]
        names = {"600000.SH": "浦发银行"}
        linked = news.link_symbols(items, names, watch={"600000.SH"})
        self.assertEqual(linked[0]["symbols"], ["600000.SH"])

    def test_a_single_character_name_never_links(self):
        items = [{"title": "今日市场普涨", "summary": "", "time": stamp(5), "wire": "w"}]
        names = {"000001.SZ": "涨"}
        self.assertEqual(news.link_symbols(items, names, watch={"000001.SZ"}), [])

    def test_a_bare_code_still_links(self):
        items = [{"title": "600000.SH 临时停牌", "summary": "", "time": stamp(5), "wire": "w"}]
        linked = news.link_symbols(items, {}, watch={"600000.SH"})
        self.assertEqual(linked[0]["symbols"], ["600000.SH"])


class FetchTests(unittest.TestCase):
    def snapshot(self):
        return {"quotes": [{"symbol": "600000.SH", "name": "浦发银行"}]}

    def test_stale_items_are_excluded_and_counted(self):
        client = FakeClient({"cls_telegraph": [
            {"title": "浦发银行涨停", "content": "", "time": stamp(5)},
            {"title": "浦发银行旧闻", "content": "", "time": stamp(news.FRESH_MINUTES + 30)},
        ]})
        block = news.digest_news(client, self.snapshot(),
                                 watch={"600000.SH"}, now=NOW)
        self.assertEqual(len(block["items"]), 1)
        self.assertEqual(block["counts"]["dropped_stale"], 1)

    def test_one_broken_wire_does_not_blind_the_other(self):
        client = FakeClient({
            "cls_telegraph": RuntimeError("upstream down"),
            "eastmoney_global_news": [{"title": "浦发银行获增持", "summary": "",
                                       "time": stamp(3)}],
        })
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(len(block["items"]), 1)
        self.assertEqual(block["items"][0]["wire"], "eastmoney_global_news")

    def test_the_block_is_bounded(self):
        rows = [{"title": f"浦发银行 消息{i}", "content": "", "time": stamp(1)}
                for i in range(200)]
        client = FakeClient({"cls_telegraph": rows})
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(len(block["items"]), news.MAX_ITEMS)

    def test_without_a_watch_list_only_named_symbols_link(self):
        client = FakeClient({"cls_telegraph": [
            {"title": "浦发银行获批", "content": "", "time": stamp(2)},
            {"title": "无关市场综述", "content": "", "time": stamp(2)},
        ]})
        block = news.digest_news(client, self.snapshot(), now=NOW)
        self.assertEqual(len(block["items"]), 1)


if __name__ == "__main__":
    unittest.main()


class HotListTests(unittest.TestCase):
    def test_exchange_suffix_is_attached_from_the_code_prefix(self):
        self.assertEqual(news._full_symbol("600000"), "600000.SH")
        self.assertEqual(news._full_symbol("300750"), "300750.SZ")
        self.assertEqual(news._full_symbol("830001"), "830001.BJ")
        self.assertEqual(news._full_symbol("600000.SH"), "600000.SH")

    def test_a_missing_endpoint_yields_an_empty_block(self):
        client = FakeClient({"ths_hot_list": RuntimeError("down")})
        block = news.fetch_hot_and_sectors(client)
        self.assertEqual(block["items"], [])

    def test_rows_are_normalised_and_bounded(self):
        client = FakeClient({"ths_hot_list": [
            {"code": "002242", "name": "九阳股份", "heat": "1493299.0",
             "rank_chg": 0, "concepts": ["净水概念", "绿色电力", "x", "y", "z"]}
        ] + [{"code": f"{i:06d}", "name": f"n{i}", "heat": "1"} for i in range(60)]})
        block = news.fetch_hot_and_sectors(client, limit=5)
        self.assertEqual(len(block["items"]), 5)
        first = block["items"][0]
        self.assertEqual(first["symbol"], "002242.SZ")
        self.assertEqual(first["heat"], 1493299)
        self.assertEqual(len(first["concepts"]), 4)


class Frame:
    """Stands in for the pandas DataFrame the flow endpoint returns."""

    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, _orient):
        return list(self._rows)


class NorthboundTests(unittest.TestCase):
    def test_open_and_latest_are_kept_so_a_direction_is_visible(self):
        client = FakeClient({"hsgt_realtime": Frame([
            {"time": "09:10", "sgt_yi": 0.00, "hgt_yi": 0.00},
            {"time": "13:00", "sgt_yi": 12.5, "hgt_yi": -3.2},
            {"time": "14:56", "sgt_yi": 20.0, "hgt_yi": -5.0},
        ])})
        block = news.fetch_northbound(client)
        self.assertEqual(block["as_of"], "14:56")
        self.assertEqual(block["shanghai_net_yi"], 20.0)
        self.assertEqual(block["shanghai_delta_yi"], 20.0)
        self.assertEqual(block["shenzhen_net_yi"], -5.0)

    def test_one_leg_going_nan_keeps_the_other_legs_last_valid_value(self):
        # Observed in production: sgt_yi is NaN from 14:58 while hgt_yi prints.
        client = FakeClient({"hsgt_realtime": Frame([
            {"time": "09:10", "sgt_yi": 364.81, "hgt_yi": 0.0},
            {"time": "09:11", "sgt_yi": 365.5, "hgt_yi": 0.03},
            {"time": "15:00", "sgt_yi": "nan", "hgt_yi": -9.28},
        ])})
        block = news.fetch_northbound(client)
        self.assertEqual(block["shanghai_net_yi"], 365.5, "last valid, not NaN")
        self.assertEqual(block["shenzhen_net_yi"], -9.28)
        self.assertEqual(block["as_of"], "15:00")

    def test_a_missing_series_returns_nothing_rather_than_a_zero(self):
        client = FakeClient({"hsgt_realtime": RuntimeError("down")})
        self.assertIsNone(news.fetch_northbound(client))
        self.assertIsNone(news.fetch_northbound(FakeClient({"hsgt_realtime": Frame([])})))

    def test_nan_is_rejected_not_rendered_as_a_number(self):
        client = FakeClient({"hsgt_realtime": Frame([
            {"time": "09:10", "sgt_yi": None, "hgt_yi": "nan"},
            {"time": "14:56", "sgt_yi": None, "hgt_yi": "nan"},
        ])})
        self.assertIsNone(news.fetch_northbound(client))
