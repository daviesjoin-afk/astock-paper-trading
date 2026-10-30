# -*- coding: utf-8 -*-
"""涨跌停带兜底：东财整段不可达时，成交性观测**仍然成立**。

现网事实（2026-10-30，oci-main）：东财 ``push2*`` 的**所有**行情入口整段
TCP 443 FAIL，于是 ``stock/get`` 的 ``f51``/``f52``（涨跌停价）取不到 →
成交性观测整段缺失 → 执行计划判 ``TRADABILITY_UNKNOWN`` → 真实委托永久
挂起。

腾讯 / 新浪与 ``datacenter-web.eastmoney.com``（停牌报表）是**可达**的，它们
的报文里带着昨收、涨跌幅、最高、最低。本模块据此在**观测优先、推导兜底**
的口径下重建涨跌停带，并把每个结论的证据等级显式标注出来。

三条硬约束（这里逐条钉死）：

1. **观测优先**：能拿到真实日内区间 / 真实涨跌停价时，绝不推导；
2. **推导必须显式标注**：推导值一律带 ``derived_from_prev_close``，绝不与
   观测混同；
3. **拿不到就不放宽**：昨收不可得 → 观测**不成立**（``evidence_present=False``），
   绝不猜一个涨跌停价出来。

另外，本文件还钉死了"锁死"与"观测不可得"这两件事在**证据**上的区别：

* 锁死 → ``is_price_limit_locked=True`` + ``price_limit_direction="down"``
  （一条**已证事实**，卖出被"跌停锁死"拦下）；
* 观测不可得 → 整条观测缺席（"我不知道"）。

把真跌停写成后者，审计就再也分不清"数据源坏了"和"股票真的锁死"。
"""

import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

import paper_live_tradability as L
import tradability_archive as TA
import tradability_ingestion as TI


#: 现网实测的六只票（腾讯 / 新浪报文，2026-10-30 12:4x，oci-main）。
#: ``price`` 现价、``prev`` 昨收、``high`` 最高、``low`` 最低。
REAL_TICKERS = {
    # 东贝集团：真跌停（low == price == 推导跌停价 6.03）
    "601956": {"name": "东贝集团", "price": 6.03, "prev": 6.70, "high": 6.31, "low": 6.03,
               "pct": -10.0},
    # 浙江仙通：日内区间内部
    "603239": {"name": "浙江仙通", "price": 13.79, "prev": 14.69, "high": 14.78, "low": 13.30,
               "pct": -6.13},
    # 龙旗科技：日内区间内部
    "603341": {"name": "龙旗科技", "price": 37.12, "prev": 38.29, "high": 38.12, "low": 35.96,
               "pct": -3.06},
    # 拓邦股份：日内区间内部
    "002139": {"name": "拓邦股份", "price": 9.69, "prev": 9.67, "high": 9.84, "low": 9.45,
               "pct": 0.21},
    # 江阴银行：日内区间内部
    "002807": {"name": "江阴银行", "price": 4.33, "prev": 4.29, "high": 4.35, "low": 4.25,
               "pct": 0.93},
}


class DerivedLimitFallbackTests(unittest.TestCase):
    """东财 ``stock/get`` 不可达时的涨跌停带兜底。"""

    def setUp(self):
        self.now = dt.datetime.now(L.TZ)
        self.day = self.now.date().isoformat()
        self.report = {"success": True, "result": {"pages": 1, "count": 0, "data": []}}

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def quote(self, code, *, name, price, prev_close=None, pct=None, high=None, low=None,
              volume=100, quote_at=None, validation="cross_source_checked"):
        """一条"腾讯 / 新浪可达、东财不可达"的行情信封。"""
        envelope = {
            "code": code, "name": name, "price": price, "pct": pct, "volume": volume,
            "quote_at": (quote_at or self.now).isoformat(), "quote_source": "live",
            "quote_validation": validation,
        }
        if prev_close is not None:
            envelope["prev_close"] = prev_close
        if high is not None:
            envelope["high"] = high
        if low is not None:
            envelope["low"] = low
        return envelope

    def prefetch(self, quotes, *, profile=None, reading=None):
        """跑一轮 prefetch。

        ``profile=None`` 表示**东财整段不可达**：``_get`` 抛传输层异常，
        与现网 ``push2*`` 全部 TCP 443 FAIL 的行为一致。
        """
        def get(key, url, params, **kwargs):
            if key[0] == "suspend":
                return self.report, self.now.isoformat()
            if profile is None:
                raise L.requests.ConnectionError("push2 unreachable")
            return profile, self.now.isoformat()

        unavailable = L.MDC.unavailable_reading(L.MDC.LIVE_MARKET_POLICY, L.MDC.REASON_MISSING)
        with patch.object(L, "_get", side_effect=get), patch.object(
                L.MDSvc, "read_snapshot", return_value=(reading or unavailable)):
            return L.prefetch(quotes, asof_day=self.day)

    def database(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        TI.ensure_ingestion_schema(conn)
        conn.commit()
        return conn

    # ── (i) 推导路径成立：观测成立、可卖 ──────────────────────────────────

    def test_push2_unreachable_with_prev_close_proves_a_sellable_observation(self):
        """东财不可达 + 昨收可得 + 现价在推导区间内部 → 观测成立、**可卖**。"""
        item = self.prefetch({"603239": self.quote(
            "603239", name="浙江仙通", price=13.79, prev_close=14.69, pct=-6.13)})["603239"]
        self.assertIsNotNone(item, "推导路径必须让观测成立")
        self.assertEqual(item["source"], L.DERIVED_LIMIT_SOURCE)
        self.assertEqual(item["source_evidence"]["limit_basis"], "derived")
        self.assertFalse(item["fields"]["is_price_limit_locked"])
        self.assertTrue(item["fields"]["has_market_quote"])
        self.assertIs(item["fields"]["is_suspended"], False)
        # 推导值必须**显式标注**，绝不含糊成观测。
        derived = item["source_evidence"]["derived_from_prev_close"]
        self.assertEqual(derived["basis"], "derived_from_prev_close")
        self.assertEqual(derived["prev_close"], 14.69)
        self.assertEqual(derived["limit_amplitude"], "0.10")
        self.assertEqual(derived["lower"], 13.22)
        self.assertEqual(derived["upper"], 16.16)
        self.assertNotIn("derived_from_prev_close", item["source_observations"],
                         "推导标记只在 source_evidence 里，不得污染观测时点表")

    def test_derived_observation_instant_is_the_quote_timestamp_not_a_fake_profile_time(self):
        """推导值的时点必须是**该行情自己的时间戳**，不得伪造成东财档案时点。

        东财档案这条路径整段不可达，所以**不存在**一个"档案时点"可以被冒充。
        推导证据里的时点必须就是腾讯 / 新浪那条行情自己的时间戳。
        """
        quote_at = self.now - dt.timedelta(seconds=30)
        item = self.prefetch({"603341": self.quote(
            "603341", name="龙旗科技", price=37.12, prev_close=38.29, pct=-3.06,
            quote_at=quote_at)})["603341"]
        # 推导值的"观测时点"= 该行情自己的时间戳。
        self.assertEqual(item["source_observations"]["profile"], quote_at.isoformat())
        self.assertEqual(item["source_observations"]["quote"], quote_at.isoformat())
        self.assertEqual(item["source_evidence"]["derived_from_prev_close"]["quote_at"],
                         quote_at.isoformat())
        # 整条观测的时点是**各来源观测的最大值**（这里停牌报表更新得更晚），
        # 但它**不得早于**任何一条来源的观测时点。
        self.assertGreaterEqual(item["observed_at"], quote_at.isoformat())

    def test_derived_observation_survives_the_archive_and_allows_selling(self):
        """推导观测写进台账后，执行计划必须判**可卖**（而不是 UNKNOWN）。"""
        conn = self.database()
        quote = self.quote("603239", name="浙江仙通", price=13.79, prev_close=14.69, pct=-6.13)
        observation = self.prefetch({"603239": quote})["603239"]
        envelope = {**quote, "tradability_observation": observation,
                    "execution_asof": (self.now + dt.timedelta(seconds=1)).isoformat()}
        L.persist(conn, envelope, code="603239", asof_day=self.day)
        decision = TA.tradability_at("603239", self.day, decision_time=envelope["execution_asof"],
                                     repository=TA.TradabilityArchiveRepository(conn))
        self.assertTrue(decision.evidence_present, "推导观测也是**观测**，证据必须成立")
        self.assertTrue(decision.can_sell)
        self.assertTrue(decision.can_buy)

    # ── (ii) 现价恰好等于推导边界 → 确实锁死，不得判可卖 ──────────────────

    def test_price_exactly_at_the_derived_lower_bound_is_locked_down(self):
        """现价**恰好**等于推导跌停价 → 锁死，**不得**判成可卖。

        这是现网 601956 的形态（low == price == 推导跌停价 6.03）。
        """
        item = self.prefetch({"601956": self.quote(
            "601956", name="东贝集团", price=6.03, prev_close=6.70, pct=-10.0)})["601956"]
        self.assertIsNotNone(item, "真跌停必须是**已证事实**，不是'观测不可得'")
        self.assertTrue(item["fields"]["is_price_limit_locked"])
        self.assertEqual(item["fields"]["price_limit_direction"], "down")
        derived = item["source_evidence"]["derived_from_prev_close"]
        self.assertEqual(derived["lower"], 6.03)
        self.assertTrue(derived["locked"])

    def test_a_genuine_limit_down_is_evidence_not_missing_evidence(self):
        """"确实锁死"与"观测不可得"必须在**证据**上可区分。

        * 锁死 → ``is_price_limit_locked=True`` + 方向 ``down``；
        * 观测不可得 → 整条观测缺席。

        两者在执行计划上都表现为"不成交"，但语义完全不同。
        """
        conn = self.database()
        quote = self.quote("601956", name="东贝集团", price=6.03, prev_close=6.70, pct=-10.0)
        observation = self.prefetch({"601956": quote})["601956"]
        envelope = {**quote, "tradability_observation": observation,
                    "execution_asof": (self.now + dt.timedelta(seconds=1)).isoformat()}
        L.persist(conn, envelope, code="601956", asof_day=self.day)
        decision = TA.tradability_at("601956", self.day, decision_time=envelope["execution_asof"],
                                     repository=TA.TradabilityArchiveRepository(conn))
        self.assertTrue(decision.evidence_present, "锁死也是**已证事实**，证据必须成立")
        self.assertFalse(decision.can_sell, "跌停锁死 → 卖单被拦下")
        self.assertTrue(decision.can_buy, "跌停只拦卖，不拦买")
        self.assertEqual(decision.sell_block_reason, TA.TradabilityReason.SELL_LIMIT_LOCKED)

    def test_price_exactly_at_the_derived_upper_bound_is_locked_up(self):
        """现价**恰好**等于推导涨停价 → 锁死（方向 ``up``），买��被拦、卖可放。"""
        item = self.prefetch({"603239": self.quote(
            "603239", name="浙江仙通", price=16.16, prev_close=14.69, pct=10.01)})["603239"]
        self.assertIsNotNone(item)
        self.assertTrue(item["fields"]["is_price_limit_locked"])
        self.assertEqual(item["fields"]["price_limit_direction"], "up")

    # ── (iii) 昨收缺失 → 观测不成立（**不放宽**） ──────────────────────────

    def test_missing_prev_close_never_invents_a_limit_band(self):
        """昨收不可得 → 观测**不成立**（``evidence_present=False``），绝不猜。"""
        self.assertEqual(self.prefetch({"603239": self.quote(
            "603239", name="浙江仙通", price=13.79, pct=-6.13)}), {})

    def test_missing_prev_close_leaves_the_decision_unknown_not_sellable(self):
        """观测不成立时，执行计划必须判 UNKNOWN（**不是**"可卖"）。"""
        conn = self.database()
        quote = self.quote("603239", name="浙江仙通", price=13.79, pct=-6.13)
        self.assertEqual(self.prefetch({"603239": quote}), {})
        # 整条观测缺席 → 台账里没有这一行 → 判定层报 UNKNOWN_STATE。
        decision = TA.tradability_at("603239", self.day, decision_time=self.now.isoformat(),
                                     repository=TA.TradabilityArchiveRepository(conn))
        self.assertFalse(decision.evidence_present)
        self.assertFalse(decision.can_sell)

    def test_price_outside_the_derived_band_is_not_forced_into_one(self):
        """现价**越出**推导区间 → 昨收与现价互相矛盾 → 推导不成立（不放宽）。"""
        self.assertEqual(self.prefetch({"603239": self.quote(
            "603239", name="浙江仙通", price=20.0, prev_close=14.69, pct=36.1)}), {})

    def test_prev_close_inconsistent_with_the_reported_pct_is_refused(self):
        """昨收与涨跌幅互相矛盾 → 该份数据不可信 → 拒绝推导。"""
        self.assertEqual(self.prefetch({"603239": self.quote(
            "603239", name="浙江仙通", price=13.79, prev_close=14.69, pct=-30.0)}), {})

    # ── (iv) ST → 5% 幅度 ────────────────────────────────────────────────

    def test_st_name_uses_the_five_percent_amplitude(self):
        """ST 票在哪个板块都是 ±5%，且 ST 判定来自**名称**。"""
        item = self.prefetch({"600001": self.quote(
            "600001", name="ST东贝", price=9.50, prev_close=10.00, pct=-5.0)})["600001"]
        derived = item["source_evidence"]["derived_from_prev_close"]
        self.assertEqual(derived["limit_amplitude"], "0.05")
        self.assertEqual(derived["lower"], 9.50)
        self.assertEqual(derived["upper"], 10.50)
        self.assertTrue(item["fields"]["is_st"])

    def test_delisting_marker_also_uses_the_five_percent_amplitude(self):
        """退市整理标记同样按 ±5%。"""
        item = self.prefetch({"600001": self.quote(
            "600001", name="东贝退", price=9.50, prev_close=10.00, pct=-5.0)})["600001"]
        self.assertEqual(item["source_evidence"]["derived_from_prev_close"]["limit_amplitude"],
                         "0.05")

    def test_st_limit_down_is_locked_at_the_five_percent_boundary(self):
        """ST 票跌停在 5% 边界上 → 锁死（方向 down）。"""
        item = self.prefetch({"600001": self.quote(
            "600001", name="*ST东贝", price=9.50, prev_close=10.00, pct=-5.0)})["600001"]
        self.assertTrue(item["fields"]["is_price_limit_locked"])
        self.assertEqual(item["fields"]["price_limit_direction"], "down")

    # ── (e) 创业板 / 科创板 → 20% 幅度 ──────────────────────────────────

    def test_chinext_code_uses_the_twenty_percent_amplitude(self):
        """创业板 ``300xxx`` → ±20%。"""
        item = self.prefetch({"300750": self.quote(
            "300750", name="某创业板", price=180.00, prev_close=200.00, pct=-10.0)})["300750"]
        derived = item["source_evidence"]["derived_from_prev_close"]
        self.assertEqual(derived["limit_amplitude"], "0.20")
        self.assertEqual(derived["lower"], 160.00)
        self.assertEqual(derived["upper"], 240.00)
        self.assertFalse(item["fields"]["is_price_limit_locked"])

    def test_star_market_code_uses_the_twenty_percent_amplitude(self):
        """科创板 ``688xxx`` → ±20%。"""
        item = self.prefetch({"688981": self.quote(
            "688981", name="某科创板", price=48.00, prev_close=50.00, pct=-4.0)})["688981"]
        self.assertEqual(item["source_evidence"]["derived_from_prev_close"]["limit_amplitude"],
                         "0.20")

    def test_main_board_code_uses_the_ten_percent_amplitude(self):
        """主板 ``60xxxx`` / ``00xxxx`` → ±10%。"""
        for code in ("601956", "002139"):
            item = self.prefetch({code: self.quote(
                code, name=REAL_TICKERS[code]["name"], price=REAL_TICKERS[code]["price"],
                prev_close=REAL_TICKERS[code]["prev"], pct=REAL_TICKERS[code]["pct"])})[code]
            self.assertEqual(item["source_evidence"]["derived_from_prev_close"]["limit_amplitude"],
                             "0.10", code)

    def test_limit_prices_are_rounded_half_up_to_the_cent(self):
        """涨跌停价一律四舍五入到分（A 股最小变动单位）。

        ``6.03 × 0.9 = 5.427`` → 5.43；用二进制浮点会踩到 5.426999999999999。
        """
        item = self.prefetch({"601956": self.quote(
            "601956", name="东贝集团", price=5.60, prev_close=6.03, pct=-7.13)})["601956"]
        derived = item["source_evidence"]["derived_from_prev_close"]
        self.assertEqual(derived["lower"], 5.43)
        self.assertEqual(derived["upper"], 6.63)

    # ── (f) 既有东财路径行为不变 ──────────────────────────────────────────

    def test_the_observed_intraday_range_wins_over_the_derivation(self):
        """**观测优先于推导**：行情信封带真实日内区间时，绝不推导。

        这是 Lead 明确的优先级：真实 high/low 观测的证据等级高于
        "昨收 × 法定幅度"的推导。
        """
        item = self.prefetch({"603341": self.quote(
            "603341", name="龙旗科技", price=37.12, prev_close=38.29, pct=-3.06,
            high=38.12, low=35.96)})["603341"]
        self.assertEqual(item["source_evidence"]["limit_basis"], "range")
        self.assertEqual(item["source"], L.RANGE_SOURCE)
        self.assertIsNone(item["source_evidence"]["derived_from_prev_close"],
                          "观测成立时不得再推导")
        self.assertEqual(item["source_evidence"]["observed_intraday_range"],
                         {"low": 35.96, "high": 38.12})
        self.assertFalse(item["fields"]["is_price_limit_locked"])

    def test_the_observed_range_does_not_survive_a_price_at_the_extremum(self):
        """现价**恰好等于**日内极值 → 日内区间证明不了它是不是涨跌停。

        → 继续往下走推导路径（推导能给出真实涨跌停价并判定锁死方向）。
        """
        item = self.prefetch({"601956": self.quote(
            "601956", name="东贝集团", price=6.03, prev_close=6.70, pct=-10.0,
            high=6.31, low=6.03)})["601956"]
        self.assertEqual(item["source_evidence"]["limit_basis"], "derived")
        self.assertTrue(item["fields"]["is_price_limit_locked"])
        self.assertEqual(item["fields"]["price_limit_direction"], "down")

    def test_the_eastmoney_profile_path_is_unchanged(self):
        """东财可达时，行为与改造前**完全一致**：真实涨跌停价 + PROFILE_SOURCE。"""
        now = self.now
        profile = {"rc": 0, "data": {"f57": "600001", "f58": "测试股", "f189": 20000101,
                                     "f51": 12.0, "f52": 8.0, "f43": 10.0, "f86": now.timestamp()}}
        quote = self.quote("600001", name="测试股", price=10.0, pct=1.0, volume=100)
        item = self.prefetch({"600001": quote}, profile=profile)["600001"]
        self.assertEqual(item["source"], L.PROFILE_SOURCE)
        self.assertEqual(item["source_evidence"]["limit_basis"], "profile")
        self.assertIsNone(item["source_evidence"]["derived_from_prev_close"])
        self.assertEqual(item["fields"]["listing_date"], "2000-01-01")
        self.assertFalse(item["fields"]["is_price_limit_locked"])
        self.assertEqual(item["source_evidence"]["observed_range"], {"low": 8.0, "high": 12.0})

    def test_the_eastmoney_profile_path_still_refuses_a_limit_price(self):
        """东财档案把现价报在涨跌停价上 → 观测**不成立**（既有语义不变）。"""
        now = self.now
        profile = {"rc": 0, "data": {"f57": "600001", "f58": "测试股", "f189": 20000101,
                                     "f51": 12.0, "f52": 8.0, "f43": 12.0, "f86": now.timestamp()}}
        quote = self.quote("600001", name="测试股", price=12.0, pct=20.0, volume=100)
        self.assertEqual(self.prefetch({"600001": quote}, profile=profile), {})

    # ── 现网六只票的验收口径 ──────────────────────────────────────────────

    def test_real_ticker_verdicts_match_the_production_expectations(self):
        """现网六只票的判定必须与 Lead 在 oci-main 上实测的预期一致。

        * ``601956`` 真跌停 → 锁死（继续挂起是**正确**行为）；
        * 其余四只 → 观测成立、**可卖**（下一轮自动成交，无需人工干预）；
        * ``002807`` → 可卖（它没有挂起单，但判定必须是可卖）。
        """
        expectations = {
            "601956": False,  # 真跌停：锁死
            "603239": True,   # 日内区间内部：可卖
            "603341": True,   # 日内区间内部：可卖
            "002139": True,   # 日内区间内部：可卖
            "002807": True,   # 日内区间内部：可卖
        }
        for code, sellable in expectations.items():
            data = REAL_TICKERS[code]
            item = self.prefetch({code: self.quote(
                code, name=data["name"], price=data["price"], prev_close=data["prev"],
                pct=data["pct"], high=data["high"], low=data["low"])})[code]
            self.assertIsNotNone(item, f"{code} 必须有观测")
            if sellable:
                self.assertFalse(item["fields"]["is_price_limit_locked"], code)
            else:
                self.assertTrue(item["fields"]["is_price_limit_locked"], code)
                self.assertEqual(item["fields"]["price_limit_direction"], "down", code)

    def test_the_real_limit_down_ticker_is_locked_down_not_unavailable(self):
        """601956 的证据必须读作"确实锁死"，而不是"因为拿不到观测"。"""
        data = REAL_TICKERS["601956"]
        item = self.prefetch({"601956": self.quote(
            "601956", name=data["name"], price=data["price"], prev_close=data["prev"],
            pct=data["pct"], high=data["high"], low=data["low"])})["601956"]
        self.assertIsNotNone(item)
        self.assertTrue(item["fields"]["is_price_limit_locked"])
        self.assertEqual(item["fields"]["price_limit_direction"], "down")
        # 推导值 = 6.70 × 0.9 = 6.03，与"现价 == 跌停价"精确相等。
        derived = item["source_evidence"]["derived_from_prev_close"]
        self.assertEqual(derived["lower"], 6.03)
        self.assertEqual(derived["upper"], 7.37)
        self.assertTrue(derived["locked"])
        self.assertEqual(derived["direction"], "down")

    def test_the_real_sellable_tickers_prove_selling(self):
        """其余四只（含无挂起单的 002807）必须判**可卖**。"""
        for code in ("603239", "603341", "002139", "002807"):
            data = REAL_TICKERS[code]
            conn = self.database()
            quote = self.quote(code, name=data["name"], price=data["price"],
                               prev_close=data["prev"], pct=data["pct"],
                               high=data["high"], low=data["low"])
            observation = self.prefetch({code: quote})[code]
            envelope = {**quote, "tradability_observation": observation,
                        "execution_asof": (self.now + dt.timedelta(seconds=1)).isoformat()}
            L.persist(conn, envelope, code=code, asof_day=self.day)
            decision = TA.tradability_at(code, self.day, decision_time=envelope["execution_asof"],
                                         repository=TA.TradabilityArchiveRepository(conn))
            self.assertTrue(decision.evidence_present, code)
            self.assertTrue(decision.can_sell, code)


if __name__ == "__main__":
    unittest.main()
