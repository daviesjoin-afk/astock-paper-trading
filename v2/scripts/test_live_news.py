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
