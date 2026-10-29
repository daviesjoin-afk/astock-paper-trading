# -*- coding: utf-8 -*-
"""市场灯必须同时看趋势与广度，而不是被一根滞后日线永久压成黄灯。

生产 2026-10-28 14:00：沪深300 +0.55%、上涨家数占比 71.9%、海外绿灯，
但因为 10-27 的收盘价仍在 MA20 下方，旧规则把整轮风控打成"谨慎仓位"。
新规则保留"趋势 + 广度"双确认：广度推力必须同时满足指数强度与市场宽度，
任何单一指标都不能单独放宽。
"""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

import paper_trading as PT

TZ = dt.timezone(dt.timedelta(hours=8))
DAY = dt.date(2026, 10, 28)


def _bench(last_close, five_day_base, periods=25):
    dates = pd.date_range(end=DAY, periods=periods, freq="B")
    closes = [10.0] * (periods - 6) + [five_day_base] + [10.0] * 4 + [last_close]
    return pd.DataFrame({"close": closes}, index=[str(d.date()) for d in dates])


def _rows(up, down):
    rows = []
    for index in range(up):
        rows.append({"code": f"6{index:05d}", "pct": 1.0,
                     "quote_at": f"{DAY.isoformat()}T10:00:00+08:00"})
    for index in range(down):
        rows.append({"code": f"0{index:05d}", "pct": -1.0,
                     "quote_at": f"{DAY.isoformat()}T10:00:00+08:00"})
    return rows


class MarketLightRegimeTests(unittest.TestCase):
    def _state(self, *, up, down, last_close, five_day_base, live_pct,
               overseas="green"):
        with mock.patch.object(PT.dfc, "load_cached_kline",
                               return_value=_bench(last_close, five_day_base)), \
                mock.patch.object(PT.F, "overseas_risk_gate",
                                  return_value={"light": overseas, "advice": ""}), \
                mock.patch.object(PT.AD, "northbound_realtime", return_value=None):
            return PT._market_state(
                DAY, live_universe=_rows(up, down), allow_network=True,
                live_index={"code": "sh000300", "pct": live_pct,
                            "time": "20261028140000"},
            )

    def test_strong_breadth_and_index_override_a_lagging_ma20(self):
        # 2026-10-28 现场：MA20 下方、5 日 -2.47%、但指数 +0.55%、广度 71.9%。
        state = self._state(up=3854, down=1507, last_close=9.9,
                            five_day_base=10.15, live_pct=0.55)
        self.assertFalse(state["benchmark_above_ma20"])
        self.assertEqual(state["light"], "green")
        self.assertIn("广度推力", state["reason"])

    def test_above_ma20_with_thin_breadth_stays_yellow(self):
        state = self._state(up=1000, down=1500, last_close=10.4,
                            five_day_base=10.0, live_pct=0.2)
        self.assertTrue(state["benchmark_above_ma20"])
        self.assertEqual(state["light"], "yellow")
        self.assertIn("宽度不足", state["reason"])

    def test_above_ma20_with_normal_breadth_is_green(self):
        state = self._state(up=3000, down=1500, last_close=10.4,
                            five_day_base=10.0, live_pct=0.2)
        self.assertEqual(state["light"], "green")

    def test_thrust_requires_five_day_trend_not_just_one_green_day(self):
        state = self._state(up=3854, down=1507, last_close=10.0,
                            five_day_base=10.8, live_pct=1.0)
        self.assertEqual(state["light"], "yellow")

    def test_weak_index_still_tightens(self):
        state = self._state(up=1500, down=3000, last_close=10.4,
                            five_day_base=10.0, live_pct=-0.8)
        self.assertEqual(state["light"], "yellow")
        state = self._state(up=1500, down=3000, last_close=10.4,
                            five_day_base=10.0, live_pct=-2.1)
        self.assertEqual(state["light"], "red")

    def test_unknown_overseas_risk_keeps_the_old_conservative_gate(self):
        state = self._state(up=3854, down=1507, last_close=9.9,
                            five_day_base=10.15, live_pct=0.55,
                            overseas="unknown")
        self.assertEqual(state["light"], "yellow")

    def test_panic_breadth_with_decline_is_red(self):
        state = self._state(up=600, down=4400, last_close=10.4,
                            five_day_base=10.0, live_pct=-1.2)
        self.assertEqual(state["light"], "red")


if __name__ == "__main__":
    unittest.main()
