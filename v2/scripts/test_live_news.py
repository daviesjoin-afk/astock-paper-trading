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

    def call(self, name, *args, **kwargs):
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
    """Upstream contract (astock_public/_upstream/SKILL.md, hsgt_realtime):
    ``hgt`` is 沪股通 (Shanghai) and ``sgt`` is 深股通 (Shenzhen)."""

    def test_the_shanghai_leg_comes_from_hgt_and_the_shenzhen_leg_from_sgt(self):
        # Deliberately asymmetric values: swapping the two legs is visible here,
        # and the whole point of the fix is that they must not be swapped.
        client = FakeClient({"hsgt_realtime": Frame([
            {"time": "09:10", "hgt_yi": 0.00, "sgt_yi": 0.00},
            {"time": "13:00", "hgt_yi": 12.5, "sgt_yi": -3.2},
            {"time": "14:56", "hgt_yi": 20.0, "sgt_yi": -5.0},
        ])})
        block = news.fetch_northbound(client)
        self.assertEqual(block["shanghai_net_yi"], 20.0, "沪股通 is the hgt column")
        self.assertEqual(block["shenzhen_net_yi"], -5.0, "深股通 is the sgt column")
        self.assertEqual(block["shanghai_delta_yi"], 20.0)
        self.assertEqual(block["shenzhen_delta_yi"], -5.0)
        self.assertEqual(block["as_of"], "14:56")
        self.assertEqual(block["source_fields"]["shanghai"], "hgt_yi")
        self.assertEqual(block["source_fields"]["shenzhen"], "sgt_yi")

    def test_a_sign_flip_between_the_legs_follows_the_source_columns(self):
        # One leg buys, the other sells. A swapped mapping would report the
        # opposite sign for each market, which is the defect being fixed.
        client = FakeClient({"hsgt_realtime": Frame([
            {"time": "09:10", "hgt_yi": 0.0, "sgt_yi": 0.0},
            {"time": "10:30", "hgt_yi": 379.8, "sgt_yi": -9.3},
        ])})
        block = news.fetch_northbound(client)
        self.assertGreater(block["shanghai_net_yi"], 0)      # 沪股通 net buy
        self.assertLess(block["shenzhen_net_yi"], 0)         # 深股通 net sell

    def test_one_leg_going_nan_keeps_the_other_legs_last_valid_value(self):
        # Observed in production: 深股通 (sgt_yi) is NaN from 14:58 while
        # 沪股通 (hgt_yi) keeps printing. Each leg keeps its own last value and
        # its own as-of, so the block never claims a shared moment for both.
        client = FakeClient({"hsgt_realtime": Frame([
            {"time": "09:10", "hgt_yi": 0.0, "sgt_yi": 364.81},
            {"time": "09:11", "hgt_yi": 0.03, "sgt_yi": 365.5},
            {"time": "15:00", "hgt_yi": -9.28, "sgt_yi": "nan"},
        ])})
        block = news.fetch_northbound(client)
        self.assertEqual(block["shenzhen_net_yi"], 365.5, "last valid, not NaN")
        self.assertEqual(block["shanghai_net_yi"], -9.28)
        self.assertEqual(block["as_of"], "15:00")
        self.assertEqual(block["as_of_by_leg"],
                         {"shanghai": "15:00", "shenzhen": "09:11"},
                         "each leg reports the moment its own value came from")

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


class TimeValidityTests(unittest.TestCase):
    """A record with no readable stamp, or one stamped after the fetch, cannot
    describe the round being decided."""

    def snapshot(self):
        return {"quotes": [{"symbol": "600000.SH", "name": "浦发银行"}]}

    def test_an_unparseable_time_is_labelled_and_kept_out_of_items(self):
        client = FakeClient({"cls_telegraph": [
            {"title": "浦发银行获增持", "content": "", "time": "刚刚"},
        ]})
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(block["items"], [])
        self.assertEqual(block["counts"]["dropped_unknown_time"], 1)
        self.assertEqual(block["rejected"][0]["reason"], "unknown_time")

    def test_a_missing_time_field_is_not_a_fact_either(self):
        client = FakeClient({"cls_telegraph": [
            {"title": "浦发银行获增持", "content": ""},
        ]})
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(block["items"], [])
        self.assertEqual(block["counts"]["dropped_unknown_time"], 1)

    def test_a_timestamp_after_the_fetch_moment_is_labelled_future(self):
        client = FakeClient({"cls_telegraph": [
            {"title": "浦发银行获增持", "content": "",
             "time": (NOW + timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")},
        ]})
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(block["items"], [])
        self.assertEqual(block["counts"]["dropped_future_time"], 1)
        self.assertEqual(block["rejected"][0]["reason"], "future_time")

    def test_a_few_seconds_of_clock_skew_do_not_discard_a_real_headline(self):
        # Minute-resolution stamps plus a local clock that is slightly behind the
        # publisher must not empty the block; only real future stamps are rejected.
        client = FakeClient({"cls_telegraph": [
            {"title": "浦发银行获增持", "content": "",
             "time": (NOW + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")},
        ]})
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(len(block["items"]), 1)

    def test_per_symbol_rows_are_held_to_the_same_time_rule(self):
        client = FakeClient({
            "eastmoney_stock_news": [
                {"title": "浦发银行公告", "content": "", "time": ""},
            ],
        })
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        self.assertEqual(block["items"], [])
        self.assertEqual(block["counts"]["dropped_unknown_time"], 1)


class PerSymbolVerificationTests(unittest.TestCase):
    """The per-stock endpoint is a keyword search and returns weakly related
    rows; the query alone is not evidence the text is about that stock."""

    def snapshot(self):
        return {"quotes": [{"symbol": "600000.SH", "name": "浦发银行"}]}

    def test_a_row_that_never_names_the_stock_is_not_bound_to_it(self):
        client = FakeClient({"eastmoney_stock_news": [
            {"title": "银行板块整体走强", "content": "同业多家公司披露季报", "time": stamp(4)},
        ]})
        rows = news.fetch_symbol_news(client, "600000.SH", name="浦发银行")
        self.assertEqual(rows[0]["symbols"], [])
        self.assertEqual(rows[0]["verification"], "unverified_no_symbol_mention")

    def test_a_row_that_names_the_stock_in_its_title_is_bound(self):
        client = FakeClient({"eastmoney_stock_news": [
            {"title": "浦发银行三季度净利增长", "content": "", "time": stamp(4)},
        ]})
        rows = news.fetch_symbol_news(client, "600000.SH", name="浦发银行")
        self.assertEqual(rows[0]["symbols"], ["600000.SH"])

    def test_a_row_that_names_the_stock_only_in_its_summary_is_bound(self):
        client = FakeClient({"eastmoney_stock_news": [
            {"title": "沪市银行股午后异动", "content": "浦发银行领涨", "time": stamp(4)},
        ]})
        rows = news.fetch_symbol_news(client, "600000.SH", name="浦发银行")
        self.assertEqual(rows[0]["symbols"], ["600000.SH"])

    def test_a_row_naming_only_the_code_is_bound(self):
        client = FakeClient({"eastmoney_stock_news": [
            {"title": "600000 临时停牌", "content": "", "time": stamp(4)},
        ]})
        rows = news.fetch_symbol_news(client, "600000.SH", name="浦发银行")
        self.assertEqual(rows[0]["symbols"], ["600000.SH"])

    def test_an_unverified_row_never_reaches_the_items_or_the_symbols(self):
        client = FakeClient({
            "cls_telegraph": [],
            "eastmoney_stock_news": [
                {"title": "银行板块整体走强", "content": "同业多家公司披露季报",
                 "time": stamp(4)},
                {"title": "浦发银行获增持", "content": "", "time": stamp(4)},
            ],
        })
        block = news.digest_news(client, self.snapshot(), watch={"600000.SH"}, now=NOW)
        titles = [item["title"] for item in block["items"]]
        self.assertEqual(titles, ["浦发银行获增持"])
        self.assertTrue(all(item["symbols"] for item in block["items"]))
        self.assertEqual(block["counts"]["dropped_unverified"], 1)


class WatchScopeTests(unittest.TestCase):
    """``watch`` is the matching scope; only holdings get a per-symbol request."""

    def snapshot(self):
        return {"quotes": [
            {"symbol": "600000.SH", "name": "浦发银行"},
            {"symbol": "002242.SZ", "name": "九阳股份"},
        ]}

    def test_a_candidate_is_matched_against_the_wires_without_a_per_symbol_call(self):
        client = FakeClient({"cls_telegraph": [
            {"title": "九阳股份获机构调研", "content": "", "time": stamp(6)},
        ]})
        block = news.digest_news(client, self.snapshot(),
                                 watch={"600000.SH", "002242.SZ"},
                                 per_symbol_watch={"600000.SH"}, now=NOW)
        self.assertEqual([item["symbols"] for item in block["items"]],
                         [["002242.SZ"]], "a ranked candidate still gets covered")
        self.assertEqual(client.calls.count("eastmoney_stock_news"), 1,
                         "only the holding costs a per-symbol request")

    def test_holdings_keep_their_per_symbol_lookup(self):
        client = FakeClient({"eastmoney_stock_news": [
            {"title": "浦发银行获增持", "content": "", "time": stamp(6)},
        ]})
        block = news.digest_news(client, self.snapshot(),
                                 watch={"600000.SH"}, now=NOW)
        self.assertEqual([item["symbols"] for item in block["items"]],
                         [["600000.SH"]])
