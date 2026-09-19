# -*- coding: utf-8 -*-
"""``paper_risk_decision`` 确定性退出决策引擎的 golden matrix（R15）。

R15 correctness defect
----------------------
``_sell_plan(asof_day=D)`` 曾把 as-of 日期漏传给峰值口径，峰值 helper 于是回退到
``dt.date.today()``。回放历史日期时，同日新仓不再被识别为同日新仓，于是把**买入前**
的日内 high 吸进 peak，仅凭机器当前日期就凭空造出回撤并真实触发 ``trailing_stop``
（exit ``none`` → ``trailing_stop``，``sell_ratio`` 0.0 → 1.0）。

本文件的 golden matrix 把修复后的契约钉死：

    RD-01  ``bought_today`` 语义矩阵（整仓同日 / 隔夜 / 部分加仓 / 零仓）
    RD-02  as-of 必须显式给出：``None`` / 空串一律 fail fast，没有 wall-clock 回退
    RD-03  同日新仓的 peak 不吸收买入前的日内 high
    RD-04  隔夜仓的 peak 照常吸收日内 high（语义不得改变）
    RD-05  部分加仓的老仓仍用完整口径
    RD-06  as-of 规整：date / datetime / ISO 串 / 带空白串 等价
    RD-07  ``evaluate_sell`` 必须显式拿到 ``limit_pct``
    RD-08  无有效报价 → ``no_quote``，不卖
    RD-09  硬止损 + 崩盘形态 → 全清
    RD-10  硬止损首触且非崩盘 → 首段减仓 + 当日去重标记
    RD-11  当日已确认跌破 → 全清
    RD-12  移动止损：峰值回撤口径
    RD-13  严重度仲裁：硬止损压过最长持有，不被后者覆盖
    RD-14  档位未知跳过阶梯止盈；档位已知时跳空越档单轮连续消费
    RD-15  真实 ``_sell_plan`` 委托纯 engine，且不随机器当前日期漂移

全部纯 fixture：不打开数据库、不访问网络 / K 线（``_completed_kline`` 打桩）、
不 sleep、不动真实系统时钟（机器"今天"用假 ``PT.dt`` 显式模拟）。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_trading as PT  # noqa: E402
import paper_risk_decision as PRD  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
ASOF = dt.date(2026, 1, 5)
#: 与 asof 脱节的"机器今天"：用于证明结果不依赖 wall clock。
MACHINE_OTHER = dt.date(2026, 9, 20)

COST = 10.00
PRE_ENTRY_HIGH = 12.00
ASOF_ISO = ASOF.isoformat()

#: 通用确定性阈值（仅测试用）。take_profit 只留一档，避免阶梯干扰基础断言。
SPEC = {
    "hard_stop": -0.05,
    "trail_after": 0.04,
    "trail_stop": 0.05,
    "hold_max": 15,
    "take_profit": [[0.08, 0.50]],
    "strategy_version": "test-risk-v1",
}


def _fake_dt(machine_day):
    """一个"机器今天是 machine_day"的 ``datetime`` 替代模块。"""
    class _Date(dt.date):
        @classmethod
        def today(cls):
            return cls(machine_day.year, machine_day.month, machine_day.day)

    class _Datetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            base = dt.datetime(machine_day.year, machine_day.month, machine_day.day, 10, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    class _DT:
        date = _Date
        datetime = _Datetime
        timedelta = dt.timedelta

    return _DT


def _position(*, qty=100, today_qty=100, entry_date=ASOF_ISO, cost=COST,
              peak_price=COST, take_stage=None, code=CODE):
    return {"account_id": ACCOUNT, "code": code, "qty": qty, "today_acquired_qty": today_qty,
            "entry_date": entry_date, "cost": cost, "peak_price": peak_price,
            "take_stage": take_stage}


def _sell_plan(position, quote, *, spec=None, machine_day=MACHINE_OTHER, asof_day=ASOF,
               news=(), hard_stop_touched_today=False):
    """驱动真实生产 ``_sell_plan``：K 线打桩、机器日期显式模拟。"""
    with mock.patch.object(PT, "dt", _fake_dt(machine_day)), \
            mock.patch.object(PT, "_completed_kline", return_value=None):
        return PT._sell_plan(position, quote, asof_day, list(news),
                             hard_stop_touched_today=hard_stop_touched_today,
                             spec_override=dict(spec or SPEC))


class BoughtTodayTests(unittest.TestCase):
    """RD-01 —— 整仓都是当日买入才适用"买入后峰值"口径。"""

    def test_rd01_bought_today_semantics(self):
        same_day = _position(qty=100, today_qty=100, entry_date=ASOF.isoformat())
        overnight = _position(qty=100, today_qty=0, entry_date="2026-01-04")
        partial_add = _position(qty=200, today_qty=100, entry_date=ASOF.isoformat())
        empty = _position(qty=0, today_qty=0, entry_date=ASOF.isoformat())
        self.assertTrue(PRD.bought_today(same_day, asof_day=ASOF))
        self.assertFalse(PRD.bought_today(overnight, asof_day=ASOF))
        self.assertFalse(PRD.bought_today(partial_add, asof_day=ASOF))
        self.assertFalse(PRD.bought_today(empty, asof_day=ASOF))
        # as-of 与 entry_date 不一致：即便当日买过，也不是"交易日当天"的判定范围
        self.assertFalse(PRD.bought_today(same_day, asof_day=dt.date(2026, 1, 6)))


class ExplicitAsofTests(unittest.TestCase):
    """RD-02 —— 决策日期只能由调用方显式给出。"""

    def test_rd02_missing_asof_fails_fast(self):
        position = _position()
        quote = {"price": 10.50, "high": PRE_ENTRY_HIGH, "pct": 5.0}
        with self.assertRaises(ValueError):
            PRD.bought_today(position, asof_day=None)
        with self.assertRaises(ValueError):
            PRD.bought_today(position, asof_day="   ")
        with self.assertRaises(ValueError):
            PRD.position_peak(position, quote, 10.50, asof_day=None)
        with self.assertRaises(ValueError):
            PRD.evaluate_sell(position, quote, asof_day=None, spec=dict(SPEC), hold_days=0,
                              limit_pct=10.0)
        # 允许的只有显式日期；绝不接受"省略即用机器今天"
        with self.assertRaises(TypeError):
            PRD.bought_today(position)


class PositionPeakTests(unittest.TestCase):
    """RD-03 / RD-04 / RD-05 —— 峰值口径的三条语义分支。"""

    def test_rd03_same_day_new_position_ignores_pre_entry_high(self):
        position = _position(peak_price=COST)
        quote = {"high": PRE_ENTRY_HIGH}
        self.assertEqual(
            PRD.position_peak(position, quote, COST, asof_day=ASOF), COST,
            "同日新仓把买入前的日内 high 计入了峰值 —— R15 缺陷回归",
        )

    def test_rd04_overnight_position_absorbs_intraday_high(self):
        position = _position(today_qty=0, entry_date="2026-01-04",
                             peak_price=COST, take_stage=0)
        quote = {"high": PRE_ENTRY_HIGH}
        self.assertEqual(PRD.position_peak(position, quote, COST, asof_day=ASOF), PRE_ENTRY_HIGH)

    def test_rd05_partial_add_on_keeps_full_high_scope(self):
        position = _position(qty=200, today_qty=100, entry_date=ASOF.isoformat(),
                             peak_price=COST, take_stage=0)
        quote = {"high": PRE_ENTRY_HIGH}
        self.assertEqual(PRD.position_peak(position, quote, COST, asof_day=ASOF), PRE_ENTRY_HIGH)


class AsofNormalizationTests(unittest.TestCase):
    """RD-06 —— date / datetime / ISO 串 一律规整成同一个 as-of。"""

    def test_rd06_asof_accepts_equivalent_forms(self):
        position = _position()
        quote = {"high": PRE_ENTRY_HIGH}
        forms = [
            ASOF,
            dt.datetime(2026, 1, 5, 14, 30, 0),
            "2026-01-05",
            " 2026-01-05 ",
            "2026-01-05T14:30:00",
        ]
        results = [PRD.bought_today(position, asof_day=form) for form in forms]
        peaks = [PRD.position_peak(position, quote, COST, asof_day=form) for form in forms]
        self.assertEqual(results, [True] * len(forms))
        self.assertEqual(peaks, [COST] * len(forms))


class EvaluateSellContractTests(unittest.TestCase):
    """RD-07 / RD-08 —— 调用方必须交出执行参数；无报价不决策。"""

    def test_rd07_limit_pct_must_be_resolved_by_caller(self):
        position = _position()
        quote = {"price": 9.40, "high": 10.20, "pct": -3.0}
        with self.assertRaises(ValueError):
            PRD.evaluate_sell(position, quote, asof_day=ASOF, spec=dict(SPEC), hold_days=0)

    def test_rd08_missing_quote_is_a_no_op(self):
        position = _position()
        decision = PRD.evaluate_sell(position, {"high": PRE_ENTRY_HIGH}, asof_day=ASOF,
                                     spec=dict(SPEC), hold_days=0, limit_pct=10.0)
        self.assertEqual(decision["status"], "no_quote")
        self.assertEqual(decision["sell_ratio"], 0.0)
        self.assertEqual(decision["exit_class"], "none")
        self.assertIsNone(decision["exit_reason_code"])
        self.assertIsNone(decision["main_force_intent"])


class HardStopTests(unittest.TestCase):
    """RD-09 / RD-10 / RD-11 —— 硬止损的三种收尾形态。"""

    def _quote(self, *, price, pct):
        return {"price": price, "high": price, "pct": pct}

    def test_rd09_crash_tape_clears_the_whole_position(self):
        decision = PRD.evaluate_sell(_position(cost=COST), self._quote(price=9.40, pct=-8.5),
                                     asof_day=ASOF, spec=dict(SPEC), hold_days=0,
                                     limit_pct=10.0)
        self.assertEqual(decision["exit_class"], "hard_stop")
        self.assertEqual(decision["exit_reason_code"], "hard_stop")
        self.assertEqual(decision["sell_ratio"], 1.0)
        self.assertIn("崩盘形态", decision["reason"])
        self.assertIsNone(decision["exit_marker"])

    def test_rd10_first_touch_trims_before_clearing(self):
        decision = PRD.evaluate_sell(_position(cost=COST), self._quote(price=9.40, pct=-3.0),
                                     asof_day=ASOF, spec=dict(SPEC), hold_days=0,
                                     limit_pct=10.0, hard_stop_first_trim_ratio=0.25)
        self.assertEqual(decision["exit_class"], "hard_stop")
        self.assertEqual(decision["exit_marker"], "hard_stop_first_trim")
        self.assertAlmostEqual(decision["sell_ratio"], 0.25)
        self.assertIn("首段减仓", decision["reason"])

    def test_rd11_confirmed_breakdown_clears_the_whole_position(self):
        decision = PRD.evaluate_sell(_position(cost=COST), self._quote(price=9.40, pct=-3.0),
                                     asof_day=ASOF, spec=dict(SPEC), hold_days=0,
                                     limit_pct=10.0, hard_stop_touched_today=True)
        self.assertEqual(decision["exit_class"], "hard_stop")
        self.assertEqual(decision["sell_ratio"], 1.0)
        self.assertIn("跌破确认", decision["reason"])


class TrailingStopTests(unittest.TestCase):
    """RD-12 —— 移动止损按峰值回撤判定。"""

    def test_rd12_trailing_stop_fires_on_peak_drawdown(self):
        # take_profit 清空，隔离移动止损这一条判定
        spec = dict(SPEC, trail_after=0.04, trail_stop=0.05, take_profit=[])
        # 隔夜仓（峰值口径吸收日内 high）：峰值 12.50，现价 12.00 → 回撤 4.0% < 5.0%
        position = _position(today_qty=0, entry_date="2026-01-04",
                             cost=10.00, peak_price=10.00, take_stage=0)
        quote = {"price": 12.00, "high": 12.50, "pct": 3.0}
        quiet = PRD.evaluate_sell(position, quote, asof_day=ASOF, spec=spec, hold_days=0,
                                  limit_pct=10.0)
        self.assertEqual(quiet["exit_class"], "none")
        self.assertEqual(quiet["sell_ratio"], 0.0)
        # 现价 11.80 → 回撤 5.6% ≥ 5.0% → 全清
        quote_deep = {"price": 11.80, "high": 12.50, "pct": 1.5}
        fired = PRD.evaluate_sell(position, quote_deep, asof_day=ASOF, spec=spec, hold_days=0,
                                  limit_pct=10.0)
        self.assertEqual(fired["exit_class"], "trailing_stop")
        self.assertEqual(fired["exit_reason_code"], "trailing_stop")
        self.assertEqual(fired["sell_ratio"], 1.0)
        self.assertAlmostEqual(fired["drawdown"], 0.056, places=3)


class ExitSeverityTests(unittest.TestCase):
    """RD-13 —— 更严重的退出类别胜出，不被后续判定覆盖。"""

    def test_rd13_hard_stop_outranks_max_hold(self):
        spec = dict(SPEC, hold_max=3)
        quote = {"price": 9.40, "high": 9.60, "pct": -8.5}
        decision = PRD.evaluate_sell(_position(cost=COST), quote, asof_day=ASOF, spec=spec,
                                     hold_days=3, limit_pct=10.0)
        self.assertEqual(decision["exit_class"], "hard_stop")
        self.assertEqual(decision["exit_reason_code"], "hard_stop")
        # 两个原因都留痕，但归因取更严重者
        self.assertIn("硬止损", decision["reason"])
        self.assertIn("最长持有", decision["reason"])


class StagedTakeProfitTests(unittest.TestCase):
    """RD-14 —— 档位未知不得卖出；档位已知时跳空越档单轮消费完。"""

    #: trail_after 抬到不可达，隔离阶梯止盈这一条判定。
    SPEC = dict(SPEC, trail_after=9.99, trail_stop=9.99,
                take_profit=[[0.05, 0.30], [0.10, 0.40], [0.20, 0.50]])

    def _quote(self):
        price = round(COST * 1.21, 2)
        return {"price": price, "high": price, "pct": 21.0}

    def test_rd14_unknown_stage_skips_and_known_stage_consumes_levels(self):
        quote = self._quote()
        unknown = _position(peak_price=quote["price"], take_stage=None)
        skipped = PRD.evaluate_sell(unknown, quote, asof_day=ASOF, spec=dict(self.SPEC),
                                    hold_days=0, limit_pct=10.0)
        self.assertEqual(skipped["sell_ratio"], 0.0)
        self.assertEqual(skipped["exit_class"], "none")
        self.assertEqual(skipped["next_stage"], 0)

        known = _position(peak_price=quote["price"], take_stage=0)
        consumed = PRD.evaluate_sell(known, quote, asof_day=ASOF, spec=dict(self.SPEC),
                                     hold_days=0, limit_pct=10.0)
        self.assertEqual(consumed["next_stage"], 3)
        self.assertEqual(consumed["exit_class"], "tactical_take_profit")
        self.assertEqual(consumed["exit_reason_code"], "take_profit")
        self.assertEqual(consumed["sell_ratio"], 1.0)


class ProductionSellPlanTests(unittest.TestCase):
    """RD-15 —— 生产 adapter 委托纯 engine，且结果不随机器日期漂移。"""

    def test_rd15_sell_plan_is_machine_date_independent(self):
        # R15 场景：as-of 当天整仓买入，quote.high 是**买入前**的日内最高价。
        position = _position(peak_price=COST, take_stage=None)
        quote = {"price": 10.50, "high": PRE_ENTRY_HIGH, "pct": 5.0}
        leaked_spec = dict(SPEC, hard_stop=-0.50, trail_after=0.01, trail_stop=0.05,
                           take_profit=[], hold_max=100000)

        same_day = _sell_plan(position, quote, spec=leaked_spec, machine_day=ASOF)
        other_day = _sell_plan(position, quote, spec=leaked_spec, machine_day=MACHINE_OTHER)

        # 峰值不吸收买入前的 high → 无回撤 → 不产生假移动止损
        self.assertEqual(same_day[0], 0.0)
        self.assertEqual(same_day[3]["exit_class"], "none")
        self.assertEqual(same_day[3]["drawdown_pct"], 0.0)
        # 换一个"机器今天"，逐字段一致：结果只由显式 asof 决定
        self.assertEqual(same_day[0], other_day[0])
        self.assertEqual(same_day[1], other_day[1])
        self.assertEqual(same_day[2], other_day[2])
        self.assertEqual(same_day[3]["exit_class"], other_day[3]["exit_class"])
        self.assertEqual(same_day[3]["drawdown_pct"], other_day[3]["drawdown_pct"])
        self.assertEqual(same_day[3]["ret_pct"], other_day[3]["ret_pct"])
        self.assertEqual(same_day[3]["protective_exit"], False)
        # adapter 补齐的 orchestration 诊断仍然在位（本 PR 不改行为）
        self.assertIn("volatility_shadow", same_day[3])
        self.assertIn("shadow_news_notice", same_day[3])
        # 显式 as-of 直接驱动峰值口径，与纯 engine 的答案一致
        self.assertEqual(
            PRD.position_peak(position, quote, quote["price"], asof_day=ASOF),
            quote["price"],
        )


if __name__ == "__main__":
    unittest.main()
