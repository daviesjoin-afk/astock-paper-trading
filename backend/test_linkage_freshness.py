# -*- coding: utf-8 -*-
"""板块联动的“今日”标记必须由适用时间规则与业务日证明（B6 回归）。

缺陷：``sector_linkage`` 直接使用 ``read_snapshot()`` 返回的行计算
``co_move_today`` / ``co_fund_today`` / ``resonance``，却不看新鲜度、报价日期和
降级状态。该读取入口刻意保留最后一份已知事实供展示，所以“返回行”不等于
“今天有效”—— 10 月 23 日拿着 10 月 22 日 15:00 的快照，仍会输出
``resonance=true`` 并被缓存五分钟。

历史相关性永远可以展示；当日共振不可判定时必须如实标为 ``None``，不能用
``False`` 冒充“今日不联动”。
"""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import linkage as L
import market_data_contract as MDC

CHINA_TZ = L.CHINA_TZ
NOW_AFTER_CLOSE = dt.datetime(2026, 10, 23, 15, 30, tzinfo=CHINA_TZ)
NOW_INTRADAY = dt.datetime(2026, 10, 23, 10, 30, tzinfo=CHINA_TZ)

UNIVERSE = (
    [{"code": f"60000{i}", "industry": "bank"} for i in range(2)]
    + [{"code": f"00000{i}", "industry": "steel"} for i in range(2)]
)


def _kline(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-06-01", periods=20, freq="B")
    return pd.DataFrame({"close": 10 + rng.standard_normal(20).cumsum()}, index=index)


class _Reading:
    """``MarketDataReading`` 的最小替身：只暴露 linkage 真正读取的字段。"""

    def __init__(self, rows, *, freshness, available=True, status=None, reason=None):
        self._rows = tuple(rows)
        self.freshness = freshness
        self.available = available
        self._status = status or freshness
        self._reason = reason

    def rows(self):
        return self._rows

    def projection(self):
        return {
            "status": self._status,
            "availability": "available" if self.available else "unavailable",
            "freshness": self.freshness,
            "verification": "single_source",
            "verification_method": "coverage_integrity",
            "as_of": "2026-10-23",
            "observed_at": "2026-10-23T15:00:00+08:00",
            "age_seconds": 1800.0,
            "reason": self._reason,
            "policy": "close_snapshot",
            "row_count": len(self._rows),
        }


def _row(code, industry, pct, main_pct, quote_at):
    return {
        "code": code, "industry": industry,
        "pct": pct, "amount": 1.0e9, "main_pct": main_pct,
        "price": 10.0, "quote_at": quote_at,
    }


def _rows(quote_at, *, both_up=True):
    sign = 1.0 if both_up else -1.0
    out = []
    for code in ("600000", "600001"):
        out.append(_row(code, "bank", 1.5 * sign, 5.0 * sign, quote_at))
    for code in ("000000", "000001"):
        out.append(_row(code, "steel", 1.2 * sign, 4.0 * sign, quote_at))
    return out


class LinkageTodayEvidenceTests(unittest.TestCase):
    def setUp(self):
        L._CACHE.update({"data": None, "ts": 0.0, "key": None})
        patches = [
            mock.patch.object(L.U, "load_universe", return_value=list(UNIVERSE)),
            mock.patch.object(L.dfc, "load_cached_kline",
                              side_effect=lambda code: _kline(int(code[-1]) + 1)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, reading, now):
        self.snapshot_calls = []
        service = mock.Mock()

        def _read_snapshot(**kwargs):
            self.snapshot_calls.append(kwargs)
            return reading

        service.read_snapshot = _read_snapshot
        with mock.patch.dict("sys.modules", {"market_data_service": service}), \
                mock.patch.object(L.dt, "datetime", wraps=dt.datetime) as frozen:
            frozen.now = mock.Mock(return_value=now)
            return L.sector_linkage(window=8, min_corr=0.5, min_members=2)

    def test_yesterday_only_snapshot_cannot_claim_todays_resonance(self):
        stale = _Reading(_rows("2026-10-22 15:00:00"), freshness=MDC.FRESHNESS_STALE,
                         status="stale", reason="stale")
        out = self._run(stale, NOW_AFTER_CLOSE)
        self.assertTrue(out["pairs"], "历史相关性仍然可以展示")
        self.assertFalse(out["today_evidence"]["usable"])
        self.assertIsNone(out["resonating_count"])
        self.assertIn("不可判定", out["summary"])
        for pair in out["pairs"]:
            self.assertIsNone(pair["resonance"], "证据不足时 resonance 必须是 None")
            self.assertIsNone(pair["co_move_today"])
            self.assertIsNone(pair["co_fund_today"])

    def test_fresh_reading_with_a_previous_business_day_is_still_rejected(self):
        # ``freshness`` 说新鲜，但报价日期不是今天 → 业务日校验必须否决。
        wrong_day = _Reading(_rows("2026-10-22 15:00:00"), freshness=MDC.FRESHNESS_FRESH,
                             status="fresh")
        out = self._run(wrong_day, NOW_AFTER_CLOSE)
        self.assertFalse(out["today_evidence"]["usable"])
        self.assertEqual(out["today_evidence"]["quote_date"], "2026-10-22")
        self.assertIsNone(out["pairs"][0]["resonance"])

    def test_todays_close_snapshot_marks_resonance(self):
        fresh = _Reading(_rows("2026-10-23 15:00:00"), freshness=MDC.FRESHNESS_FRESH,
                         status="fresh")
        out = self._run(fresh, NOW_AFTER_CLOSE)
        self.assertTrue(out["today_evidence"]["usable"])
        self.assertIs(self.snapshot_calls[0]["policy"], MDC.CLOSE_SNAPSHOT_POLICY,
                      "收盘后谈当日结果必须使用收盘口径")
        self.assertEqual(out["resonating_count"], len(out["pairs"]))
        self.assertTrue(all(pair["resonance"] for pair in out["pairs"]))
        self.assertIn("共振", out["summary"])
        self.assertNotIn("不可判定", out["summary"])

    def test_intraday_uses_the_live_policy(self):
        fresh = _Reading(_rows("2026-10-23 10:29:00"), freshness=MDC.FRESHNESS_FRESH,
                         status="fresh")
        out = self._run(fresh, NOW_INTRADAY)
        self.assertIs(self.snapshot_calls[0]["policy"], MDC.LIVE_MARKET_POLICY,
                      "盘中必须使用实时口径")
        self.assertEqual(self.snapshot_calls[0]["asof_day"], "2026-10-23")
        self.assertTrue(out["today_evidence"]["usable"])

    def test_opposite_direction_is_reported_as_false_not_unknown(self):
        fresh = _Reading(_rows("2026-10-23 15:00:00", both_up=False),
                         freshness=MDC.FRESHNESS_FRESH, status="fresh")
        out = self._run(fresh, NOW_AFTER_CLOSE)
        self.assertTrue(out["today_evidence"]["usable"])
        self.assertEqual(out["resonating_count"], 0)
        for pair in out["pairs"]:
            self.assertIs(pair["resonance"], False, "有证据的否定结论应当是 False")

    def test_snapshot_change_invalidates_the_cache(self):
        fresh = _Reading(_rows("2026-10-23 15:00:00"), freshness=MDC.FRESHNESS_FRESH,
                         status="fresh")
        first = self._run(fresh, NOW_AFTER_CLOSE)
        self.assertTrue(first["today_evidence"]["usable"])
        stale = _Reading(_rows("2026-10-22 15:00:00"), freshness=MDC.FRESHNESS_STALE,
                         status="stale", reason="stale")
        with mock.patch.object(L, "_evidence_clock_key", return_value=("changed",)):
            second = self._run(stale, NOW_AFTER_CLOSE)
        self.assertFalse(second["today_evidence"]["usable"],
                         "证据状态变化后不得继续返回上一轮的“共振”")


if __name__ == "__main__":
    unittest.main()
