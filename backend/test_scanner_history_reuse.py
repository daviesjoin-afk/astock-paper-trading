# -*- coding: utf-8 -*-
"""扫描接口不应为“改一个过滤条件”重做整段历史（E1 回归）。

缺陷：``main.scanner()`` 每次请求都遍历全部股票调用 ``load_shared_kline()``
（读 CSV、解析日期、去重排序）再计算整张价格因子表。用户只是换一个过滤条件，
不变的历史部分也要重算一遍。

修复契约：
* 历史因子按 **完整交易日 + 历史清单版本 + 因子算法版本** 复用；
* 实时行情仍独立检查覆盖率与新鲜度（不能被缓存顶掉）；
* 过滤条件仍然作用在同一份因子表上。
"""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import factors as F
import main as M


def _universe():
    return [
        {"code": code, "name": code, "industry": "bank"}
        for code in ("600000", "600001", "000001", "000002")
    ]


def _kline(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2025-01-01", periods=140, freq="B")
    close = 10 + rng.standard_normal(140).cumsum()
    return pd.DataFrame(
        {
            "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1.0e6, "amount": 1.0e8,
        },
        index=index,
    )


def _quote_rows():
    """真实候选 4 只 + 填充行，凑过“覆盖 >= 4000 只”的正式扫描门槛。"""
    rows = []
    for index, item in enumerate(_universe()):
        rows.append({
            "code": item["code"], "name": item["name"], "industry": "bank",
            "price": 10.0, "pct": 1.0, "main_pct": 1.0, "amount": 1.0e8,
        })
    for index in range(4000 - len(rows)):
        rows.append({
            "code": f"9000{index:03d}", "name": "filler", "industry": "other",
            "price": 5.0, "pct": 0.0, "main_pct": 0.0, "amount": 1.0e6,
        })
    return rows


class ScannerHistoryReuseTests(unittest.TestCase):
    def setUp(self):
        M._SCANNER_HISTORY_CACHE.update({"signature": None, "price": None, "codes": ()})
        self.loads = []
        self.factor_calls = 0
        real_compute = F.compute_price_factors

        def counting_compute(klines, *args, **kwargs):
            self.factor_calls += 1
            return real_compute(klines, *args, **kwargs)

        patches = [
            mock.patch.object(M.U, "data_ready", return_value=True),
            mock.patch.object(M.U, "load_universe", side_effect=lambda: _universe()),
            mock.patch.object(M.dfc, "load_shared_kline", side_effect=self._load),
            mock.patch.object(M.dfc, "fetch_realtime_for_codes",
                              side_effect=lambda codes, **kw: {"rows": _quote_rows()}),
            mock.patch.object(M.dfc, "fetch_finance_latest",
                              return_value={"data": {}, "report_dates": []}),
            mock.patch.object(M.dfc, "fetch_hot_rank", return_value=[]),
            mock.patch.object(M.F, "compute_price_factors", side_effect=counting_compute),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(M._SCANNER_HISTORY_CACHE.update,
                        {"signature": None, "price": None, "codes": ()})

    def _load(self, code):
        self.loads.append(code)
        return _kline(int(code[-1]) + 1)

    def test_filter_only_change_reuses_the_history(self):
        first = M.scanner()
        self.assertTrue(first.get("picks"), f"首次扫描应有结果：{first}")
        first_loads = len(self.loads)
        self.assertEqual(self.factor_calls, 1)
        self.assertGreater(first_loads, 0)

        second = M.scanner(pe_max=20.0)
        self.assertEqual(self.factor_calls, 1, "只是换了过滤条件，不得重算价格因子")
        self.assertEqual(len(self.loads), first_loads, "不得重新读取历史 CSV")
        self.assertEqual(second.get("picks"), [], "过滤条件仍要作用在同一份因子表上")

    def test_signature_change_invalidates_the_cache(self):
        M.scanner()
        self.assertEqual(self.factor_calls, 1)
        with mock.patch.object(M, "_scanner_history_signature", return_value=("changed",)):
            M.scanner()
        self.assertEqual(self.factor_calls, 2, "清单/算法/交易日变化后必须重算")

    def test_live_quote_coverage_is_checked_on_every_request(self):
        calls = []
        M.scanner()
        with mock.patch.object(
            M.dfc, "fetch_realtime_for_codes",
            side_effect=lambda codes, **kw: calls.append(1) or {"rows": _quote_rows()[:10]},
        ):
            degraded = M.scanner()
        self.assertEqual(len(calls), 1, "覆盖率检查不能被历史缓存顶掉")
        self.assertEqual(degraded.get("status"), "degraded")
        self.assertEqual(self.factor_calls, 1, "历史因子仍然复用")

    def test_history_is_invalidated_when_the_cutoff_moves(self):
        M.scanner()
        self.assertEqual(self.factor_calls, 1)
        moved = M.U.latest_complete_trade_date(dt.date.today()) - dt.timedelta(days=1)
        with mock.patch.object(M.U, "latest_complete_trade_date", return_value=moved):
            M.scanner()
        self.assertEqual(self.factor_calls, 2)


if __name__ == "__main__":
    unittest.main()
