# -*- coding: utf-8 -*-
"""价格-only 全市场回退必须**如实**带出日内高低/今开/量额。

现网现场（OCI oci-main，2026-10-30）：东财 clist 整段不可达 → 走腾讯价格-only
回退。旧实现组装快照行时把 ``high`` / ``low`` / ``open_price`` **写死成
None**（腾讯报文里其实有：``[5]``=今开、``[33]``=最高、``[34]``=最低），
``volume`` / ``amount`` / ``turnover`` 也一并写死。

后果：

* ``main_force_intent`` 的"日内高低价"恒缺 → "收盘靠近日内低位"那 14 分永远
  拿不到 → 深跌票卡在下跌确认门槛之下；
* 因子表 ``amount`` / ``turnover`` 整列缺失 → 每轮 ``factor_columns_missing``；
* ``paper_live_tradability`` 的 ``interior`` 分支拿不到 low/high → 成交性观测
  回落到东财 push2（该机房不可达）→ 委托挂起。

这些用例钉住三条边界：

1. **有值即真值**：报文里有就如实透传（绝不写死 None）；
2. **无值即 None**：自洽校验不通过就整体保持 unknown —— 不写 0、不猜、
   不用别的字段顶替；
3. **量额口径不倒退**：``volume`` / ``amount`` / ``turnover`` 一并透传，
   交叉校验不通过仍然不写。
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

import data_fetcher as dfc
import marketdata_providers as MP

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 30, 10, 30, 0, tzinfo=TZ)


def _tencent_line(*, price="9.83", prev_close="9.62", pct="2.18",
                   open_price="9.56", high="9.90", low="9.50",
                   volume_lots="461758", amount_wan="44897", turnover="0.14",
                   quote_at="20261030103000", code="600000", name="浦发银行"):
    parts = [""] * 88
    parts[0] = "1"
    parts[1] = name
    parts[2] = code
    parts[3] = price
    parts[4] = prev_close
    parts[5] = open_price
    parts[6] = volume_lots
    parts[30] = quote_at
    parts[32] = pct
    parts[33] = high
    parts[34] = low
    parts[37] = amount_wan
    parts[38] = turnover
    return f'v_sh{code}="' + "~".join(parts) + '";'


def _sina_line(*, price="9.830", prev_close="9.620", open_price="9.560",
               high="9.900", low="9.500", volume_shares="45797793",
               amount_yuan="445251713.000", code="600000", name="浦发银行"):
    values = [""] * 34
    values[0] = name
    values[1] = open_price
    values[2] = prev_close
    values[3] = price
    values[4] = high
    values[5] = low
    values[8] = volume_shares
    values[9] = amount_yuan
    values[30] = "2026-10-30"
    values[31] = "10:30:00"
    return f'var hq_str_sh{code}="' + ",".join(values) + '";'


class TencentOhlcExtractionTests(unittest.TestCase):
    def _row(self, **kwargs):
        rows = MP.parse_tencent_realtime_text(_tencent_line(**kwargs))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_intraday_high_low_are_extracted(self):
        row = self._row()
        self.assertAlmostEqual(row["high"], 9.90)
        self.assertAlmostEqual(row["low"], 9.50)
        self.assertAlmostEqual(row["open_price"], 9.56)

    def test_inconsistent_range_is_rejected_not_written(self):
        """high < low 或现价落在区间外 —— 说明字段错位，整体保持 unknown。"""
        for kwargs in ({"high": "9.40", "low": "9.50"}, {"low": "9.95"}):
            with self.subTest(kwargs=kwargs):
                row = self._row(**kwargs)
                self.assertNotIn("high", row)
                self.assertNotIn("low", row)

    def test_absent_range_stays_absent(self):
        row = self._row(high="", low="")
        self.assertNotIn("high", row)
        self.assertNotIn("low", row)
        # 价格仍必须可用：不能因为补字段把主路径弄坏。
        self.assertAlmostEqual(row["price"], 9.83)

    def test_open_outside_the_range_is_dropped_but_range_survives(self):
        row = self._row(open_price="9.99")
        self.assertNotIn("open_price", row)
        self.assertAlmostEqual(row["high"], 9.90)


class SinaOhlcExtractionTests(unittest.TestCase):
    def _row(self, **kwargs):
        rows = MP.parse_sina_realtime_text(_sina_line(**kwargs))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_intraday_high_low_are_extracted(self):
        row = self._row()
        self.assertAlmostEqual(row["high"], 9.90)
        self.assertAlmostEqual(row["low"], 9.50)
        self.assertAlmostEqual(row["open_price"], 9.56)

    def test_inconsistent_range_is_rejected(self):
        row = self._row(high="9.40", low="9.50")
        self.assertNotIn("high", row)
        self.assertNotIn("low", row)


class TencentSnapshotRowAssemblyTests(unittest.TestCase):
    """快照组装必须透传真实值，而不是写死 None。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="astock-ohlc-")
        self.addCleanup(self._cleanup)
        for name, value in (
            ("MARKET_SNAPSHOT_FULL_CACHE_PATH",
             os.path.join(self._dir, "market_snapshot_full.json")),
            ("_full_market_min_rows", lambda: 2),
        ):
            patcher = mock.patch.object(dfc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dfc, "fetch_sina_realtime_for_codes", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            try:
                os.unlink(os.path.join(self._dir, name))
            except OSError:
                pass
        os.rmdir(self._dir)

    def _snapshot(self, rows):
        codes = [row["code"] for row in rows]
        with mock.patch.object(dfc, "fetch_tencent_realtime_for_codes", return_value=rows):
            return dfc.fetch_tencent_market_snapshot(codes, max_age=240, now=NOW)

    def _row(self, code, **kwargs):
        parsed = MP.parse_tencent_realtime_text(_tencent_line(code=code, **kwargs))[0]
        parsed["quote_at"] = "20261030103000"
        return parsed

    def test_snapshot_rows_carry_high_low_and_amount(self):
        snapshot = self._snapshot([self._row("600000"), self._row("600001")])
        self.assertEqual(len(snapshot), 2)
        first = snapshot[0]
        self.assertAlmostEqual(first["high"], 9.90)
        self.assertAlmostEqual(first["low"], 9.50)
        self.assertAlmostEqual(first["open_price"], 9.56)
        # 量额一并透传：腾讯报文 idx6/37/38 已解析并做过量纲交叉校验。
        self.assertAlmostEqual(first["volume"], 461758.0)
        self.assertAlmostEqual(first["amount"], 448970000.0)
        self.assertAlmostEqual(first["turnover"], 0.14)
        # 昨收继续透传。
        self.assertAlmostEqual(first["prev_close"], 9.62)

    def test_missing_range_stays_none_in_the_snapshot(self):
        row = self._row("600000", high="", low="")
        snapshot = self._snapshot([row, self._row("600001")])
        first = snapshot[0]
        self.assertIsNone(first["high"])
        self.assertIsNone(first["low"])
        # 没有值就是 None —— 绝不填 0。
        self.assertNotEqual(first["high"], 0)
        self.assertNotEqual(first["low"], 0)

    def test_snapshot_rows_declare_the_missing_flow_evidence(self):
        """腾讯报文没有资金流字段：如实标记"没有资金证据"，不伪造。"""
        snapshot = self._snapshot([self._row("600000"), self._row("600001")])
        first = snapshot[0]
        self.assertIsNone(first["super_net"])
        self.assertIsNone(first["main_net"])
        self.assertIsNone(first["main_pct"])
        self.assertEqual(first["flow_source_status"], MP.FLOW_SOURCE_SYMBOL_MISSING)


class SnapshotOhlcConsistencyTests(unittest.TestCase):
    """跨行收口：进入快照的日内区间必须自洽。"""

    def test_inconsistent_range_is_dropped(self):
        row = {"low": 9.5, "high": 9.9, "open_price": 9.6, "price": 9.83}
        self.assertEqual(dfc._snapshot_ohlc(row, 9.83), (9.6, 9.9, 9.5))

    def test_price_outside_the_range_is_dropped(self):
        row = {"low": 9.5, "high": 9.9, "open_price": 9.6, "price": 10.5}
        self.assertEqual(dfc._snapshot_ohlc(row, 10.5), (None, None, None))

    def test_high_below_low_is_dropped(self):
        row = {"low": 9.9, "high": 9.5, "open_price": 9.6, "price": 9.83}
        self.assertEqual(dfc._snapshot_ohlc(row, 9.83), (None, None, None))

    def test_missing_fields_stay_none(self):
        self.assertEqual(dfc._snapshot_ohlc({"price": 9.83}, 9.83),
                         (None, None, None))


if __name__ == "__main__":
    unittest.main()
