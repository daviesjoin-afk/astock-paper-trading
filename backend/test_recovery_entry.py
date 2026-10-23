# -*- coding: utf-8 -*-
"""普跌后恢复入场（第二批）：证据判定 + 状态机恢复观察 + 三场景重放。

背景（2026-10-23 诊断 §3.3 / §6 第二批）
-----------------------------------------
`trend_pullback` 禁止"午后首次触发"，而上午已确认的候选一旦跌破触发价 0.5%，
状态机把记录**删掉**：午后恢复再来时被当成全新首次触发，于是仍被拒绝。结果是
"恐慌后确认恢复、择机买入"这条用户诉求在代码里根本没有路径。

修复不是删掉时段限制（那等于允许午后无条件追涨），而是：

1. 状态机把失效记录转成 ``recovery_watch`` 并**保留上午证据**（几点触发、触发价、
   是否已确认、失效时间与原因）；
2. 恢复资格由 ``recovery_entry_decision`` 按决策时点可证的事实判定：当日沪深300
   参照、相对强弱、个股负面事件、主力资金、跌后低点收复；
3. 证据连续成立 ``recovery_confirm_scans`` 次后，状态机才重新给出入场资格，
   并以新的机会编号（``retrigger_seq``）继续走完整门禁。

本文件钉死：

    RE-01  无上午证据 / 未声明策略 → 一律不放行（全新候选仍是午后禁区）。
    RE-02  指数参照必须属于当日，否则"市场已恢复"无从证明。
    RE-03  指数未恢复 / 个股独立走弱 / 负面事件 / 资金流出 / 未收复跌后低点
           各自被归类拒绝，且分类可区分。
    RE-04  全部成立才 allowed，且证据链完整（每个条件都有可读依据）。
    RE-05  跌破触发价后记录转为 recovery_watch，上午证据不丢。
    RE-06  恢复证据连续成立两次才确认；证据断开即清零（假反弹不积累）。
    RE-07  快速通道只观察，不累计恢复确认次数。
    RE-08  午后：有恢复观察的候选可以走完全流程；全新候选仍被"午后不接受首次触发"拒绝。
    RE-09  三场景重放（普跌后恢复 / 继续下跌 / 假反弹）的成交率与过期原因。

纯 fixture：临时状态文件、内存数据，不联网、不写真实账本。
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import entry_timing as ET  # noqa: E402
import recovery_entry_decision as RECEV  # noqa: E402
import strategy_policies as SPOL  # noqa: E402

ACCOUNT = "trend_pullback"
CODE = "600495"
DAY = dt.date(2026, 10, 23)
POLICY = SPOL.recovery_entry_policy(ACCOUNT)

MORNING = {
    "morning_evidence": {
        "first_trigger_at": "2026-10-23T10:00:00",
        "trigger_price": 10.00,
        "trigger_pct": 1.2,
        "confirmed_at": "2026-10-23T10:06:00",
        "confirmation_path": "regular",
        "mode": "normal",
    },
    "low_since_breakdown": 9.40,
    "breakdown_at": "2026-10-23T10:40:00",
    "breakdown_reason": "跌破触发价 0.5%，结构失效",
    "recovery_scans": 0,
    "retrigger_seq": 0,
}
MARKET_OK = {
    "live_index_pct": -0.1, "live_index_time": "20261023133012",
    "index_reference": {"status": "same_session"},
}
QUOTE_OK = {"price": 9.52, "pct": -0.8, "main_pct": 2.4, "quote_at": "2026-10-23 13:30:00"}


def _assess(**overrides):
    kwargs = {
        "morning": MORNING, "quote": QUOTE_OK, "market": MARKET_OK,
        "negative_news": False, "policy": POLICY,
    }
    kwargs.update(overrides)
    return RECEV.assess(**kwargs)


class EvidenceDecisionTests(unittest.TestCase):
    """RE-01 ~ RE-04 —— 证据判定只负责拒绝，不负责找理由放行。"""

    def test_re01_requires_morning_evidence(self):
        verdict = _assess(morning=None)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(RECEV.CLASSIFICATION_NO_EVIDENCE, verdict["classification"])

    def test_re01_undeclared_strategy_is_disabled(self):
        verdict = _assess(policy=SPOL.recovery_entry_policy("tq_breakout"))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(RECEV.CLASSIFICATION_DISABLED, verdict["classification"])

    def test_re02_requires_a_same_session_index_reference(self):
        for status in ("stale", "unavailable", None):
            with self.subTest(status=status):
                market = {"live_index_pct": -0.1,
                          "index_reference": {"status": status}}
                verdict = _assess(market=market)
                self.assertFalse(verdict["allowed"])
                self.assertEqual(RECEV.CLASSIFICATION_MARKET_UNPROVEN,
                                 verdict["classification"])
                self.assertIn("当日沪深300指数", verdict["missing"])

    def test_re03_market_not_recovered(self):
        verdict = _assess(market={**MARKET_OK, "live_index_pct": -1.4})
        self.assertEqual(RECEV.CLASSIFICATION_MARKET_NOT_RECOVERED,
                         verdict["classification"])

    def test_re03_stock_specific_weakness_is_not_a_market_drawdown(self):
        verdict = _assess(quote={**QUOTE_OK, "pct": -4.0})   # 相对 -3.9%
        self.assertEqual(RECEV.CLASSIFICATION_STOCK_WEAKNESS, verdict["classification"])
        self.assertFalse(verdict["allowed"])

    def test_re03_negative_event_blocks_even_when_everything_else_passes(self):
        verdict = _assess(negative_news=True)
        self.assertEqual(RECEV.CLASSIFICATION_NEGATIVE_EVENT, verdict["classification"])
        self.assertFalse(verdict["allowed"])

    def test_re03_persistent_outflow_blocks(self):
        verdict = _assess(quote={**QUOTE_OK, "main_pct": -1.6})
        self.assertEqual(RECEV.CLASSIFICATION_OUTFLOW, verdict["classification"])
        self.assertFalse(verdict["allowed"])

    def test_re03_missing_flow_evidence_fails_closed(self):
        verdict = _assess(quote={**QUOTE_OK, "main_pct": None, "main_net_pct": None})
        self.assertEqual(RECEV.CLASSIFICATION_UNPROVEN_FLOW, verdict["classification"])
        self.assertFalse(verdict["allowed"])
        self.assertIn("主力净流入占比", verdict["missing"])

    def test_re03_unreclaimed_low_is_a_false_rebound(self):
        # reclaim_pct=0.008 → 9.40 * 1.008 = 9.4752，现价 9.46 不够
        verdict = _assess(quote={**QUOTE_OK, "price": 9.46})
        self.assertEqual(RECEV.CLASSIFICATION_FALSE_REBOUND, verdict["classification"])
        self.assertFalse(verdict["allowed"])

    def test_re04_all_conditions_met_yields_an_auditable_allow(self):
        verdict = _assess()
        self.assertTrue(verdict["allowed"])
        self.assertEqual(RECEV.CLASSIFICATION_MARKET_RECOVERY, verdict["classification"])
        joined = " ".join(verdict["evidence"])
        for fragment in ("上午", "指数已回到", "相对沪深300", "主力净流入", "收复跌后低点"):
            self.assertIn(fragment, joined, verdict["evidence"])
        self.assertEqual([], verdict["missing"])
        # 判定输入随结果保存，复盘不需要重跑行情
        self.assertEqual(9.40, verdict["inputs"]["low_since_breakdown"])
        self.assertEqual("same_session", verdict["inputs"]["index_status"])


class _FakeDate(dt.date):
    """把状态机的"今天是哪天"钉到 DAY，避免测试随真实日期漂移。"""

    @classmethod
    def today(cls):
        return cls(DAY.year, DAY.month, DAY.day)


class _FakeDateTimeModule:
    datetime = dt.datetime
    date = _FakeDate
    time = dt.time
    timedelta = dt.timedelta


class _StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patches = (
            mock.patch.object(ET, "STATE_PATH",
                              str(pathlib.Path(self.tmp.name) / "state.json")),
            mock.patch.object(ET, "_state", None),
            mock.patch.object(ET, "dt", _FakeDateTimeModule()),
        )
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tmp.cleanup()

    def at(self, hour, minute):
        return dt.datetime.combine(DAY, dt.time(hour, minute))

    def confirm_morning(self, price=10.0):
        ET.evaluate(ACCOUNT, CODE, price, 1.2, now=self.at(10, 0))
        allowed, info = ET.evaluate(ACCOUNT, CODE, price, 1.2, now=self.at(10, 6))
        self.assertTrue(allowed, info)
        return info


class RecoveryWatchStateTests(_StateCase):
    """RE-05 ~ RE-07 —— 失效不删证据，恢复要连续确认。"""

    def test_re05_breakdown_keeps_the_morning_evidence(self):
        self.confirm_morning()
        allowed, info = ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(11, 0))
        self.assertFalse(allowed)
        self.assertEqual("recovery_watch", info["state"])
        evidence = ET.recovery_watch_evidence(ACCOUNT, CODE)
        self.assertIsNotNone(evidence, "跌破触发价后上午证据被删掉了")
        self.assertEqual(10.0, evidence["morning_evidence"]["trigger_price"])
        self.assertEqual("2026-10-23T10:06:00", evidence["morning_evidence"]["confirmed_at"])
        self.assertEqual(9.94, evidence["low_since_breakdown"])

    def test_re06_recovery_needs_two_consecutive_confirmations(self):
        self.confirm_morning()
        ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(11, 0))
        allowed, info = ET.evaluate(ACCOUNT, CODE, 9.52, -0.8, now=self.at(13, 30),
                                    evidence={"recovery_ok": True})
        self.assertFalse(allowed)
        self.assertEqual("跌后恢复确认中 1/2", info["reason"])
        allowed, info = ET.evaluate(ACCOUNT, CODE, 9.55, -0.7, now=self.at(13, 33),
                                    evidence={"recovery_ok": True})
        self.assertTrue(allowed, info)
        self.assertEqual("confirmed", info["state"])
        self.assertTrue(info.get("recovery"))
        self.assertEqual("selloff_recovery", info["record"]["confirmation_path"])
        self.assertEqual(1, info["record"]["retrigger_seq"],
                         "恢复入场必须拿到新的机会编号")

    def test_re06_broken_evidence_resets_the_counter(self):
        self.confirm_morning()
        ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(11, 0))
        ET.evaluate(ACCOUNT, CODE, 9.52, -0.8, now=self.at(13, 30),
                    evidence={"recovery_ok": True})
        allowed, info = ET.evaluate(ACCOUNT, CODE, 9.41, -2.4, now=self.at(13, 33),
                                    evidence={"recovery_ok": False,
                                              "recovery_reason": "假反弹：未收复"})
        self.assertFalse(allowed)
        self.assertEqual(0, info["record"]["recovery_scans"])
        allowed, _info = ET.evaluate(ACCOUNT, CODE, 9.55, -0.7, now=self.at(13, 36),
                                     evidence={"recovery_ok": True})
        self.assertFalse(allowed, "证据断开后必须重新累计，不能一次反弹就凑满两次")

    def test_re07_fast_channel_does_not_accumulate_recovery_scans(self):
        self.confirm_morning()
        ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(11, 0))
        for minute in (31, 32, 33):
            allowed, _info = ET.evaluate(
                ACCOUNT, CODE, 9.55, -0.7, now=self.at(13, minute),
                fast=True, evidence={"recovery_ok": True})
            self.assertFalse(allowed)
        self.assertEqual(0, ET.recovery_watch_evidence(ACCOUNT, CODE)["recovery_scans"],
                         "30 秒通道替恢复确认攒次数")

    def test_re08_afternoon_still_blocks_candidates_without_morning_evidence(self):
        allowed, info = ET.evaluate(ACCOUNT, CODE, 9.52, -0.8, now=self.at(13, 30),
                                    evidence={"recovery_ok": True})
        self.assertFalse(allowed)
        self.assertEqual("observing", info["state"])
        self.assertIn("午后不接受首次触发", info["reason"])

    def test_re08_other_profiles_do_not_get_the_recovery_path(self):
        self.assertFalse(ET.PROFILES["tq_breakout"].get("recovery_entry"),
                         "日内做T本来就允许午后首次触发，不需要恢复通道")
        self.assertFalse(ET.PROFILES["reported_profit_breakout"].get("recovery_entry"),
                         "三日策略的 10:30 首发窗口是策略特征，不因恢复通道放宽")
        self.assertTrue(ET.PROFILES["trend_pullback"].get("recovery_entry"))

    def test_re05_evidence_accessor_is_read_only(self):
        self.confirm_morning()
        self.assertIsNone(ET.recovery_watch_evidence(ACCOUNT, CODE),
                          "已确认但未失效的候选不该被当成恢复观察")
        self.assertIsNone(ET.recovery_watch_evidence(ACCOUNT, "000001"))

    def test_re05_profiles_without_the_recovery_path_keep_the_legacy_reset(self):
        """未启用恢复通道的策略必须保持"跌破即删记录、可当日重触发"。"""
        other = "tq_breakout"
        ET.evaluate(other, CODE, 10.0, 1.2, now=self.at(10, 0))
        allowed, _info = ET.evaluate(other, CODE, 10.0, 1.2, now=self.at(10, 3))
        self.assertTrue(allowed, "日内做T 两次确认应当成立")
        allowed, info = ET.evaluate(other, CODE, 9.90, -0.5, now=self.at(10, 30))
        self.assertFalse(allowed)
        self.assertEqual("expired", info["state"])
        self.assertIsNone(ET.recovery_watch_evidence(other, CODE),
                          "未启用恢复通道的策略不该留下恢复观察记录")
        allowed, info = ET.evaluate(other, CODE, 10.05, 1.0, now=self.at(10, 33))
        self.assertIn(info["state"], {"triggered", "confirmed"},
                      "删记录后应当可以按全新触发重新观察")


class ThreeScenarioReplayTests(_StateCase):
    """RE-09 —— 三个场景在决策层重放，比较成交率与过期原因。

    这里只重放**决策层**：入场状态机 + 恢复证据。它回答"该不该给出入场资格"，
    不产生成交价与收益。组合级净收益/回撤/资金使用率需要黄金回放环境，
    见模块 docstring 说明的边界。
    """

    def outcome(self, ticks):
        """按 (时刻, 价格, pct, market, quote) 序列推进，返回首个放行时点。"""
        attempts = 0
        for hour, minute, price, pct, market, quote, negative in ticks:
            attempts += 1
            verdict = RECEV.assess(
                morning=ET.recovery_watch_evidence(ACCOUNT, CODE),
                quote={**quote, "price": price, "pct": pct},
                market=market, negative_news=negative, policy=POLICY,
            )
            allowed, info = ET.evaluate(
                ACCOUNT, CODE, price, pct, now=self.at(hour, minute),
                evidence={"recovery_ok": verdict["allowed"],
                          "recovery_reason": verdict["reason"]},
            )
            if allowed:
                return {"entry": True, "attempts": attempts,
                        "reason": info.get("reason"), "verdict": verdict}
        return {"entry": False, "attempts": attempts,
                "reason": info.get("reason"), "verdict": verdict}

    def morning(self):
        self.confirm_morning()

    def test_s1_market_selloff_then_recovery_earns_an_entry(self):
        self.morning()
        ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(10, 40))
        result = self.outcome([
            (13, 30, 9.50, -1.6, {**MARKET_OK, "live_index_pct": -1.2}, QUOTE_OK, False),
            (13, 33, 9.60, -0.8, MARKET_OK, QUOTE_OK, False),
            (13, 36, 9.62, -0.7, MARKET_OK, QUOTE_OK, False),
        ])
        self.assertTrue(result["entry"], result)
        self.assertEqual(3, result["attempts"])
        self.assertEqual(RECEV.CLASSIFICATION_MARKET_RECOVERY,
                         result["verdict"]["classification"])

    def test_s2_continued_decline_never_earns_an_entry(self):
        self.morning()
        ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(10, 40))
        result = self.outcome([
            (13, 30, 9.10, -3.4, {**MARKET_OK, "live_index_pct": -2.6}, QUOTE_OK, False),
            (13, 33, 8.90, -4.1, {**MARKET_OK, "live_index_pct": -2.9}, QUOTE_OK, False),
            (13, 36, 8.80, -4.6, {**MARKET_OK, "live_index_pct": -3.0}, QUOTE_OK, False),
        ])
        self.assertFalse(result["entry"])
        self.assertEqual(0, result["attempts"] - 3, "继续下跌时不该有任何放行")
        self.assertIn(result["verdict"]["classification"], {
            RECEV.CLASSIFICATION_MARKET_NOT_RECOVERED,
            RECEV.CLASSIFICATION_STOCK_WEAKNESS,
            RECEV.CLASSIFICATION_FALSE_REBOUND,
        })

    def test_s3_false_rebound_does_not_earn_an_entry(self):
        self.morning()
        ET.evaluate(ACCOUNT, CODE, 9.94, -0.6, now=self.at(10, 40))
        result = self.outcome([
            # 指数恢复、个股反弹但没收复跌后低点 → 第一次不成立
            (13, 30, 9.44, -1.9, MARKET_OK, QUOTE_OK, False),
            # 只差一点，仍未达 reclaim → 第二次也不成立（计数被清零）
            (13, 33, 9.47, -1.7, MARKET_OK, QUOTE_OK, False),
        ])
        self.assertFalse(result["entry"])
        self.assertEqual(RECEV.CLASSIFICATION_FALSE_REBOUND,
                         result["verdict"]["classification"])


class DiscriminatorSweepTests(_StateCase):
    """RE-10 —— 同一套参数在不同路径上的判别力（确定性网格，非随机）。

    "恢复后多买了几只"不能作为验收：真正要证明的是**同一组阈值**能把
    "可解释的市场回撤"和"个股独立走弱/假反弹"分开。这里固定触发与跌后低点，
    只改两个变量（相对强弱、是否收复），看放行结果是否只跟着该跟的变量走。
    """

    LOW = 9.50

    def _path_entry(self, *, relative_pct, reclaim_ratio, index_pct=-0.1,
                    negative=False, main_pct=2.0):
        ET.reset()
        self.confirm_morning()
        ET.evaluate(ACCOUNT, CODE, self.LOW, -4.0, now=self.at(10, 40))
        price = round(self.LOW * (1 + reclaim_ratio), 2)
        quote = {"price": price, "pct": index_pct + relative_pct,
                 "main_pct": main_pct, "quote_at": "2026-10-23 13:30:00"}
        market = {**MARKET_OK, "live_index_pct": index_pct}
        entries = 0
        for offset in (0, 3):
            verdict = RECEV.assess(
                morning=ET.recovery_watch_evidence(ACCOUNT, CODE),
                quote=quote, market=market, negative_news=negative, policy=POLICY,
            )
            allowed, _info = ET.evaluate(
                ACCOUNT, CODE, price, quote["pct"],
                now=self.at(13, 30 + offset),
                evidence={"recovery_ok": verdict["allowed"],
                          "recovery_reason": verdict["reason"]},
            )
            if allowed:
                entries += 1
        return entries

    def test_re10_relative_strength_is_the_discriminator(self):
        allowed_case = [self._path_entry(relative_pct=rel, reclaim_ratio=0.012)
                        for rel in (-0.5, -1.0, -1.5)]
        blocked_case = [self._path_entry(relative_pct=rel, reclaim_ratio=0.012)
                        for rel in (-2.5, -3.0, -4.0)]
        self.assertEqual([1, 1, 1], allowed_case,
                         "相对强弱在 -2% 以内、且已收复低点的恢复应当放行一次")
        self.assertEqual([0, 0, 0], blocked_case,
                         "个股独立走弱不得被市场恢复解释")

    def test_re10_reclaim_is_the_false_rebound_discriminator(self):
        entries = [self._path_entry(relative_pct=-1.0, reclaim_ratio=ratio)
                   for ratio in (-0.004, 0.0, 0.004)]
        self.assertEqual([0, 0, 0], entries,
                         "没收复跌后低点的反弹不能放行")
        self.assertEqual(1, self._path_entry(relative_pct=-1.0, reclaim_ratio=0.012))

    def test_re10_negative_event_and_outflow_override_a_good_path(self):
        self.assertEqual(
            0, self._path_entry(relative_pct=-1.0, reclaim_ratio=0.012, negative=True))
        self.assertEqual(
            0, self._path_entry(relative_pct=-1.0, reclaim_ratio=0.012, main_pct=-1.5))


if __name__ == "__main__":
    unittest.main()
