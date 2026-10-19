# -*- coding: utf-8 -*-
"""同轮报价复用语义：轮内超集报价供后续阶段吃掉，跨轮/缺价/超龄不复用。"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_trading as PT  # noqa: E402


class _FakeRow(dict):
    """轻量 dict 即可满足 _round_quote_* 的 isinstance(row, dict) 判定。"""


class RoundQuoteCacheTests(unittest.TestCase):
    def setUp(self):
        self.today = dt.date.today()
        PT._end_round_quote_cache()
        self.addCleanup(PT._end_round_quote_cache)

    def _row(self, price=10.0, pct=1.0, quote_at=None):
        return {
            "code": "600000",
            "price": price,
            "pct": pct,
            "quote_at": quote_at or dt.datetime.now().isoformat(timespec="seconds"),
            "quote_source": "live",
        }

    def test_offer_then_take_reuses_superset_rows(self):
        PT._begin_round_quote_cache(self.today)
        superset = {
            "600000": self._row(),
            "000001": self._row(price=5.0, pct=2.0),
            "600519": self._row(price=1.0, pct=-1.0),
        }
        accepted = PT._round_quote_offer(self.today, superset)
        self.assertEqual(accepted, 3)
        taken = PT._round_quote_take(["600000", "000001", "600519", "300001"], self.today)
        self.assertEqual(sorted(taken), ["000001", "600000", "600519"])
        self.assertEqual(taken["600000"]["price"], 10.0)

    def test_missing_or_unusable_rows_are_not_reused(self):
        PT._begin_round_quote_cache(self.today)
        superset = {
            "600000": self._row(),
            "000001": self._row(price=0),           # 缺价
            "600519": self._row(pct=None),          # 缺 pct
        }
        accepted = PT._round_quote_offer(self.today, superset)
        self.assertEqual(accepted, 1)
        taken = PT._round_quote_take(["600000", "000001", "600519"], self.today)
        self.assertEqual(list(taken), ["600000"])

    def test_stale_row_is_not_reused(self):
        PT._begin_round_quote_cache(self.today)
        stale = dt.datetime.now() - dt.timedelta(hours=2)
        superset = {"600000": self._row(quote_at=stale.isoformat(timespec="seconds"))}
        accepted = PT._round_quote_offer(self.today, superset)
        self.assertEqual(accepted, 0)
        self.assertEqual(PT._round_quote_take(["600000"], self.today), {})

    def test_asof_day_mismatch_does_not_reuse(self):
        PT._begin_round_quote_cache(self.today)
        PT._round_quote_offer(self.today, {"600000": self._row()})
        other = self.today - dt.timedelta(days=1)
        self.assertEqual(PT._round_quote_take(["600000"], other), {})
        self.assertEqual(PT._round_quote_offer(other, {"600000": self._row()}), 0)

    def test_cache_isolation_per_round(self):
        PT._begin_round_quote_cache(self.today)
        PT._round_quote_offer(self.today, {"600000": self._row()})
        PT._end_round_quote_cache()
        self.assertEqual(PT._round_quote_take(["600000"], self.today), {})

    def test_quotes_with_round_cache_short_circuits_full_hit(self):
        PT._begin_round_quote_cache(self.today)
        PT._round_quote_offer(self.today, {
            "600000": self._row(),
            "000001": self._row(price=5.0, pct=2.0),
        })
        with mock.patch.object(PT, "_quotes") as quotes:
            merged = PT._quotes_with_round_cache(["600000", "000001"], asof_date=self.today)
        quotes.assert_not_called()
        self.assertEqual(sorted(merged), ["000001", "600000"])
        self.assertEqual(merged["600000"]["price"], 10.0)

    def test_quotes_with_round_cache_fetches_only_missing(self):
        PT._begin_round_quote_cache(self.today)
        PT._round_quote_offer(self.today, {"600000": self._row()})
        with mock.patch.object(PT, "_quotes",
                               return_value={"000001": self._row(price=5.0, pct=2.0)}) as quotes:
            merged = PT._quotes_with_round_cache(["600000", "000001", "600519"],
                                                 asof_date=self.today)
        quotes.assert_called_once()
        self.assertEqual(sorted(quotes.call_args.args[0]), ["000001", "600519"])
        self.assertEqual(sorted(merged), ["000001", "600000", "600519"])
        self.assertEqual(merged["600000"]["price"], 10.0)
        self.assertEqual(merged["000001"]["price"], 5.0)
        self.assertEqual(merged["600519"], {})

    def test_no_round_cache_delegates_to_quotes_unchanged(self):
        with mock.patch.object(PT, "_quotes",
                               return_value={"600000": self._row()}) as quotes:
            merged = PT._quotes_with_round_cache(["600000"], asof_date=self.today)
        quotes.assert_called_once()
        self.assertEqual(merged, {"600000": self._row()})

    def test_round_cache_shape_matches_quotes_for_codes(self):
        """复用路径与直取路径的返回形状逐键一致。"""
        codes = ["600000", "000001", "300001"]
        superset = {
            "600000": self._row(),
            "000001": self._row(price=5.0, pct=2.0),
            "300001": self._row(price=1.0, pct=-1.0),
        }
        PT._begin_round_quote_cache(self.today)
        PT._round_quote_offer(self.today, superset)
        reused = PT._quotes_with_round_cache(codes, asof_date=self.today)
        with mock.patch.object(PT, "_quotes", return_value=dict(superset)) as quotes:
            direct = PT._quotes_with_round_cache(codes, asof_date=self.today)
        quotes.assert_not_called()
        self.assertEqual(sorted(reused), sorted(direct))
        self.assertEqual(reused, direct)


if __name__ == "__main__":
    unittest.main()
