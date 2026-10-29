"""公共日线截断语义：越界数据绝不进入决策输入。

回归 2026-10-28 审计发现：``data_fetcher.completed_kline`` 在索引日期比较
异常时直接返回原始 frame，于是截止日之后的行可能被当成完整收盘数据使用。
新的语义是 fail-closed：无法解析日期的行被丢弃，截断绝不失败。
"""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

import data_fetcher as dfc


def _frame(index, closes=None):
    if closes is None:
        closes = [10.0 + i for i in range(len(index))]
    return pd.DataFrame(
        {
            "open": [10.0] * len(closes),
            "close": list(closes),
            "high": [10.5] * len(closes),
            "low": [9.5] * len(closes),
            "volume": [100.0] * len(closes),
            "amount": [1000.0] * len(closes),
        },
        index=index,
    )


class CompletedKlineTruncationTests(unittest.TestCase):
    def test_rows_after_cutoff_are_removed(self):
        frame = _frame(pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]))
        with mock.patch.object(dfc, "load_cached_kline", return_value=frame):
            inclusive = dfc.completed_kline("000001", "2026-01-06")
            exclusive = dfc.completed_kline("000001", "2026-01-06", inclusive=False)
        self.assertEqual(
            [str(value.date()) for value in inclusive.index],
            ["2026-01-05", "2026-01-06"],
        )
        self.assertEqual(
            [str(value.date()) for value in exclusive.index],
            ["2026-01-05"],
        )

    def test_unparseable_index_dates_are_dropped_not_returned_whole(self):
        # A mixed-timezone string index makes ``index.date`` unusable; the old
        # implementation caught that and returned the untruncated frame.
        index = pd.Index(["2026-01-05T00:00:00+08:00", "2026-01-06T00:00:00+00:00"])
        frame = _frame(index)
        with mock.patch.object(dfc, "load_cached_kline", return_value=frame):
            result = dfc.completed_kline("000001", "2026-01-05")
        self.assertEqual(list(result.index), ["2026-01-05T00:00:00+08:00"])

    def test_unreadable_dates_never_leak_future_bars(self):
        index = pd.Index(["not-a-date", "2026-01-06"])
        frame = _frame(index)
        with mock.patch.object(dfc, "load_cached_kline", return_value=frame):
            result = dfc.completed_kline("000001", "2026-01-05")
        self.assertEqual(list(result.index), [])

    def test_garbage_index_returns_no_rows_instead_of_everything(self):
        frame = _frame(pd.Index(["x", "y"]))
        with mock.patch.object(dfc, "load_cached_kline", return_value=frame):
            result = dfc.completed_kline("000001", "2026-01-05")
        self.assertTrue(result.empty)

    def test_empty_and_missing_sources_stay_empty(self):
        with mock.patch.object(dfc, "load_cached_kline", return_value=None):
            self.assertIsNone(dfc.completed_kline("000001", "2026-01-05"))
        with mock.patch.object(dfc, "load_cached_kline", return_value=pd.DataFrame()):
            self.assertTrue(dfc.completed_kline("000001", "2026-01-05").empty)

    def test_source_metadata_survives_truncation(self):
        frame = _frame(pd.to_datetime(["2026-01-05", "2026-01-06"]))
        frame.attrs.update({"source": "tencent", "adjustment": "qfq"})
        with mock.patch.object(dfc, "load_cached_kline", return_value=frame):
            result = dfc.completed_kline("000001", "2026-01-05")
        self.assertEqual(result.attrs.get("source"), "tencent")
        self.assertEqual(result.attrs.get("adjustment"), "qfq")

    def test_timezone_aware_index_keeps_local_bar_date(self):
        index = pd.to_datetime(
            ["2026-01-05T00:00:00+08:00", "2026-01-06T00:00:00+08:00"]
        )
        frame = _frame(index)
        with mock.patch.object(dfc, "load_cached_kline", return_value=frame):
            result = dfc.completed_kline("000001", "2026-01-05")
        self.assertEqual(len(result), 1)
        self.assertEqual(str(result.index[0])[:10], "2026-01-05")


if __name__ == "__main__":
    unittest.main()
