# -*- coding: utf-8 -*-
"""选股标签契约的 PIT / 隔离 / 状态机测试（P1–P20）。

每条测试对应契约里的一个**可证伪**陈述，而不是“跑通即通过”。
变异脚本 ``mut_selection_labels.py`` 的 M1–M12 逐条还原这些缺陷，
本文件必须把它们抓住。
"""
from __future__ import annotations

import inspect
import math
import sqlite3
import unittest

import selection_labels as SL


# ───────────────────────────── shared fixtures ─────────────────────────────

#: 普通一周：T 前后都是交易日，含一个周末（06-15/06-16）。
SESSIONS = [
    "2024-06-11", "2024-06-12", "2024-06-13", "2024-06-14",
    "2024-06-17", "2024-06-18", "2024-06-19", "2024-06-20",
]

#: 国庆长假：09-30 与 10-08 之间没有任何交易日。
GOLDEN_WEEK = ["2024-09-27", "2024-09-30", "2024-10-08", "2024-10-09", "2024-10-10"]

T_AFTER_CLOSE = "2024-06-14T16:00:00+08:00"
T_INTRADAY = "2024-06-14T10:00:00+08:00"
ASOF = "2024-06-30"


def _flat(prices_map, sessions=SESSIONS):
    return {day: prices_map.get(day) for day in sessions}


def _label(**overrides):
    kwargs = {
        "code": "600001",
        "decision_at": T_AFTER_CLOSE,
        "horizon": 2,
        "sessions": SESSIONS,
        "prices": {day: 10.0 for day in SESSIONS},
        "asof": ASOF,
    }
    kwargs.update(overrides)
    return SL.selection_label(**kwargs)


# ───────────────────────────────── P1–P9 ─────────────────────────────────


class PriceLeakageTest(unittest.TestCase):
    """标签证据严格发生在决策之后，且未来 outcome 不得回流进特征。"""

    def test_p1_future_price_changes_label_but_never_t_features(self):
        """P1：T 后极端价格改变 label，但 T 时刻特征逐字节不变。"""
        before = {day: 10.0 for day in SESSIONS[:4]}      # <= 2024-06-14
        after_a = {"2024-06-17": 10.0, "2024-06-18": 10.0, "2024-06-19": 10.5}
        after_b = {"2024-06-17": 10.0, "2024-06-18": 10.0, "2024-06-19": 100.0}
        prices_a = {**before, **after_a}
        prices_b = {**before, **after_b}

        def feature_at_t(prices):
            """一个 T 时刻特征消费者能看到的全部证据 —— 只用 <= 决策日的数据。"""
            window = SL.evidence_windows(
                decision_at=T_AFTER_CLOSE, horizon=2, sessions=SESSIONS
            )
            usable = sorted(day for day in prices if day <= window["feature_window_end"])
            return {"last_close": prices[usable[-1]], "n": len(usable),
                    "window_end": window["feature_window_end"]}

        label_a = _label(prices=prices_a)
        label_b = _label(prices=prices_b)

        self.assertEqual(feature_at_t(prices_a), feature_at_t(prices_b))
        self.assertNotEqual(label_a.raw_forward_return, label_b.raw_forward_return)
        self.assertAlmostEqual(0.05, label_a.raw_forward_return, places=12)
        self.assertAlmostEqual(9.0, label_b.raw_forward_return, places=12)
        self.assertEqual(SL.CLASS_POSITIVE, label_a.label_class)

    def test_p2_intraday_decision_cannot_use_decision_day_close_as_entry(self):
        """P2：盘中决策时，当日收盘价尚未发生，不得当作 entry 证据。"""
        base = {day: 10.0 for day in SESSIONS}
        wildly_different = [
            {**base, "2024-06-14": 1.0},
            {**base, "2024-06-14": 500.0},
            {**base, "2024-06-14": 0.5},
        ]
        labels = [
            _label(decision_at=T_INTRADAY, horizon=2, prices=prices)
            for prices in wildly_different
        ]
        for label in labels:
            self.assertEqual(SL.STATUS_VERIFIED, label.label_status)
            self.assertEqual("2024-06-17", label.entry_date)
            self.assertNotEqual("2024-06-14", label.entry_date)
        self.assertEqual(
            {label.raw_forward_return for label in labels}, {0.0}
        )

    def test_p2b_intraday_feature_window_excludes_the_decision_day(self):
        """P2b：盘中决策的特征窗口在**前一个交易日**就结束。"""
        window = SL.evidence_windows(
            decision_at=T_INTRADAY, horizon=2, sessions=SESSIONS
        )
        self.assertEqual("2024-06-13", window["feature_window_end"])
        self.assertEqual("2024-06-17", window["label_window_start"])
        after_close = SL.evidence_windows(
            decision_at=T_AFTER_CLOSE, horizon=2, sessions=SESSIONS
        )
        self.assertEqual("2024-06-14", after_close["feature_window_end"])

    def test_p3_after_close_decision_cannot_backfill_same_day_execution(self):
        """P3：收盘后决策不能回填当日成交 —— entry 必在决策日之后。"""
        prices = {day: 10.0 for day in SESSIONS}
        prices["2024-06-14"] = 42.0
        prices["2024-06-17"] = 11.0
        prices["2024-06-19"] = 12.1
        label = _label(decision_at=T_AFTER_CLOSE, horizon=2, prices=prices)
        self.assertEqual("2024-06-17", label.entry_date)
        self.assertAlmostEqual(11.0, label.entry_price, places=12)
        # 用 06-14 收盘(42.0) 回填会得到 12.1/42.0-1 = -0.71，是明确不同的错误答案。
        self.assertAlmostEqual(0.1, label.raw_forward_return, places=12)
        self.assertEqual(SL.CLASS_POSITIVE, label.label_class)

    def test_p4_horizon_counts_trading_days_not_calendar_days(self):
        """P4：horizon 以交易日计，跨国庆长假不得按自然日平移。"""
        prices = {"2024-09-30": 20.0, "2024-10-09": 22.0}
        label = SL.selection_label(
            code="600519",
            decision_at="2024-09-27T16:00:00+08:00",
            horizon=2,
            sessions=GOLDEN_WEEK,
            prices=prices,
            asof="2024-10-31",
        )
        self.assertEqual(SL.STATUS_VERIFIED, label.label_status)
        self.assertEqual("2024-09-30", label.entry_date)
        # 自然日平移会落到 2024-10-02（长假，无证据）→ unavailable。
        self.assertEqual("2024-10-09", label.exit_date)
        self.assertAlmostEqual(0.1, label.raw_forward_return, places=12)

    def test_p4b_sessions_between_skips_statutory_holidays(self):
        """P4b：交易日序列由仓库唯一日历（universe.is_trade_day）生成。"""
        sessions = SL.sessions_between("2024-09-27", "2024-10-10")
        self.assertIn("2024-09-30", sessions)
        self.assertIn("2024-10-08", sessions)
        for holiday in ("2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07"):
            self.assertNotIn(holiday, sessions)

    def test_p5_unfinished_horizon_is_pending_never_negative(self):
        """P5：horizon 未走完是 pending，绝不落成负例。"""
        prices = {day: 10.0 for day in SESSIONS}
        prices["2024-06-19"] = 1.0          # 未来会暴跌 —— 但还没发生
        label = _label(prices=prices, asof="2024-06-18")
        self.assertEqual(SL.STATUS_PENDING, label.label_status)
        self.assertEqual(SL.REASON_HORIZON_NOT_MATURED, label.label_reason)
        self.assertIsNone(label.label_score)
        self.assertIsNone(label.label_class)
        self.assertIsNone(label.label)
        self.assertIsNone(label.raw_forward_return)
        self.assertNotEqual(SL.CLASS_NEGATIVE, label.label_class)

    def test_p6_missing_future_price_is_unavailable_never_zero_return(self):
        """P6：未来价格缺失是 unavailable，绝不当成 0 收益。"""
        prices = {day: 10.0 for day in SESSIONS}
        del prices["2024-06-19"]           # exit 日行情缺失
        label = _label(prices=prices)
        self.assertEqual(SL.STATUS_UNAVAILABLE, label.label_status)
        self.assertEqual(SL.REASON_EXIT_PRICE_MISSING, label.label_reason)
        self.assertIsNone(label.raw_forward_return)
        self.assertIsNone(label.label_score)
        self.assertIsNone(label.label_class)
        self.assertIsNone(label.exit_price)

    def test_p7_invalid_inputs_are_invalid_never_guessed(self):
        """P7：非法决策时间/非法 horizon/空 code → invalid，绝不猜。"""
        bad_time = _label(decision_at="not-a-timestamp")
        self.assertEqual(SL.STATUS_INVALID, bad_time.label_status)
        self.assertEqual(SL.REASON_INVALID_DECISION_TIMESTAMP, bad_time.label_reason)
        self.assertIsNone(bad_time.label_score)

        for horizon in (0, -1, None, 2.5, True):
            bad_horizon = _label(horizon=horizon)
            self.assertEqual(SL.STATUS_INVALID, bad_horizon.label_status, horizon)
            self.assertEqual(SL.REASON_INVALID_HORIZON, bad_horizon.label_reason)

        for code in ("", "   ", None):
            bad_code = _label(code=code)
            self.assertEqual(SL.STATUS_INVALID, bad_code.label_status, code)
            self.assertEqual(SL.REASON_INVALID_CODE, bad_code.label_reason)

        bad_phase = _label(decision_phase="during_lunch")
        self.assertEqual(SL.STATUS_INVALID, bad_phase.label_status)
        self.assertEqual(SL.REASON_INVALID_PHASE, bad_phase.label_reason)

        bad_asof = _label(asof="???")
        self.assertEqual(SL.STATUS_INVALID, bad_asof.label_status)
        self.assertEqual(SL.REASON_INVALID_EVALUATION_ASOF, bad_asof.label_reason)

    def test_p8_exact_entry_exit_boundary_produces_the_right_return(self):
        """P8：entry/exit 边界精确 —— 价格与两种口径收益逐个核对。"""
        prices = {"2024-09-30": 20.0, "2024-10-09": 22.0}
        benchmark = {"2024-09-30": 100.0, "2024-10-09": 105.0}
        label = SL.selection_label(
            code="600519",
            decision_at="2024-09-27T16:00:00+08:00",
            horizon=2,
            sessions=GOLDEN_WEEK,
            prices=prices,
            asof="2024-10-31",
            basis=SL.BASIS_EXCESS,
            benchmark_prices=benchmark,
        )
        self.assertEqual(SL.STATUS_VERIFIED, label.label_status)
        self.assertEqual("2024-09-30", label.entry_date)
        self.assertEqual("2024-10-09", label.exit_date)
        self.assertAlmostEqual(20.0, label.entry_price, places=12)
        self.assertAlmostEqual(22.0, label.exit_price, places=12)
        self.assertAlmostEqual(0.1, label.raw_forward_return, places=12)
        self.assertAlmostEqual(0.05, label.benchmark_return, places=12)
        self.assertAlmostEqual(0.05, label.excess_return, places=12)
        self.assertAlmostEqual(0.05, label.label_score, places=12)
        self.assertEqual("selection-label-v1/excess-return", label.label_version)

    def test_p9_current_price_cannot_alter_an_old_historical_label(self):
        """P9：今天的价格/快照不能改变已经算出的历史标签。"""
        prices = {day: 10.0 for day in SESSIONS}
        prices.update({"2024-06-17": 10.0, "2024-06-19": 10.5})
        baseline = _label(prices=prices)
        self.assertEqual(SL.STATUS_VERIFIED, baseline.label_status)

        # 当前快照：exit 之后价格暴涨 900%（“今天的价格”）
        afterwards = dict(prices)
        afterwards["2024-06-20"] = 1000.0
        afterwards["2024-07-31"] = 5000.0
        reprocessed = _label(prices=afterwards)
        self.assertEqual(baseline.sample_key, reprocessed.sample_key)
        self.assertEqual(baseline.as_dict(), reprocessed.as_dict())

        # 只保留到 exit 为止的证据，也与“今天能看到一切”一致。
        truncated = {day: value for day, value in prices.items() if day <= "2024-06-19"}
        self.assertEqual(baseline.as_dict(), _label(prices=truncated).as_dict())


# ──────────────────────────── P10–P14 ────────────────────────────


class LabelIsolationTest(unittest.TestCase):
    """历史标签不因当前 universe / 身份错配而改写。"""

    def test_p10_current_universe_cannot_delete_historical_labels(self):
        """P10：今天退市 / 不在当前 universe 都不能让历史标签消失。"""
        delisted = {day: 10.0 for day in SESSIONS}
        delisted.update({"2024-06-17": 10.0, "2024-06-19": 10.5})
        # "今天已退市" = exit 之后再也没有任何价格证据
        still_listed = dict(delisted)
        still_listed.update({"2024-06-20": 10.5, "2024-07-01": 11.0})

        gone = _label(prices=delisted)
        present = _label(prices=still_listed)
        self.assertEqual(SL.STATUS_VERIFIED, gone.label_status)
        self.assertEqual(gone.as_dict(), present.as_dict())

        # 结构性保证：标签函数没有 current-universe / membership 输入。
        parameters = set(inspect.signature(SL.selection_label).parameters)
        for forbidden in ("universe", "universe_membership", "members", "is_listed", "delist_date"):
            self.assertNotIn(forbidden, parameters)

    def test_p11_selected_stock_is_not_automatically_positive(self):
        """P11：被选中的股票不会因此变成正例。"""
        losing = _label(prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 8.0})
        self.assertEqual(SL.CLASS_NEGATIVE, losing.label_class)
        assembled = SL.assemble_learning_rows(
            [SL.feature_record(code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
                               features={"momentum": 1.0}, selected=True, score=99.0)],
            [SL.label_record(losing)],
        )
        self.assertEqual(1, len(assembled["rows"]))
        self.assertEqual(SL.CLASS_NEGATIVE, assembled["rows"][0]["label_class"])
        self.assertTrue(assembled["rows"][0]["selected"])
        self.assertLess(assembled["rows"][0]["label_score"], 0)

    def test_p12_unselected_stock_is_not_automatically_negative(self):
        """P12：没被选中的股票不会因此变成负例。"""
        winner = _label(prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 13.0})
        self.assertEqual(SL.CLASS_POSITIVE, winner.label_class)
        assembled = SL.assemble_learning_rows(
            [SL.feature_record(code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
                               features={"momentum": -1.0}, selected=False, score=-99.0)],
            [SL.label_record(winner)],
        )
        self.assertEqual(1, len(assembled["rows"]))
        self.assertEqual(SL.CLASS_POSITIVE, assembled["rows"][0]["label_class"])
        self.assertFalse(assembled["rows"][0]["selected"])

    def test_p13_sample_identity_includes_time_horizon_and_version(self):
        """P13：样本身份 = (code, decision_at, horizon, label_version)。"""
        base = SL.sample_identity("600001", T_AFTER_CLOSE, 2, SL.label_version())
        self.assertEqual(base, SL.sample_identity("600001", T_AFTER_CLOSE, 2, SL.label_version()))
        self.assertNotEqual(base, SL.sample_identity("600001", T_INTRADAY, 2, SL.label_version()))
        self.assertNotEqual(base, SL.sample_identity("600001", "2024-06-17T16:00:00+08:00", 2,
                                                     SL.label_version()))
        self.assertNotEqual(base, SL.sample_identity("600001", T_AFTER_CLOSE, 1, SL.label_version()))
        self.assertNotEqual(base, SL.sample_identity("600001", T_AFTER_CLOSE, 2,
                                                     SL.label_version(SL.BASIS_EXCESS)))
        self.assertNotEqual(base, SL.sample_identity("600002", T_AFTER_CLOSE, 2, SL.label_version()))

    def test_p14_neighbouring_decisions_do_not_share_one_label_result(self):
        """P14：相邻交易日的决策各自用自己的 entry/exit，结果不得互相复制。"""
        prices = {day: 10.0 for day in SESSIONS}
        prices["2024-06-18"] = 11.0
        prices["2024-06-19"] = 12.0
        prices["2024-06-20"] = 15.0

        first = _label(decision_at=T_AFTER_CLOSE, horizon=1, prices=prices)
        second = _label(decision_at="2024-06-17T16:00:00+08:00", horizon=1, prices=prices)

        self.assertEqual("2024-06-17", first.entry_date)
        self.assertEqual("2024-06-18", first.exit_date)
        self.assertEqual("2024-06-18", second.entry_date)
        self.assertEqual("2024-06-19", second.exit_date)
        self.assertAlmostEqual(0.1, first.raw_forward_return, places=12)
        self.assertAlmostEqual(12.0 / 11.0 - 1.0, second.raw_forward_return, places=12)
        self.assertNotEqual(first.sample_key, second.sample_key)

        assembled = SL.assemble_learning_rows(
            [
                SL.feature_record(code="600001", decision_at=T_AFTER_CLOSE, horizon=1,
                                  features={"f": 1.0}),
                SL.feature_record(code="600001", decision_at="2024-06-17T16:00:00+08:00",
                                  horizon=1, features={"f": 2.0}),
            ],
            [SL.label_record(first), SL.label_record(second)],
        )
        self.assertEqual(2, len(assembled["rows"]))
        self.assertEqual(2, len({row["sample_key"] for row in assembled["rows"]}))
        self.assertEqual(
            {round(0.1, 10), round(12.0 / 11.0 - 1.0, 10)},
            {round(row["raw_forward_return"], 10) for row in assembled["rows"]},
        )


# ──────────────────────────── P15–P16 ────────────────────────────


class EvidenceStateTest(unittest.TestCase):
    """基准口径的 PIT 边界，以及停牌/缺失/非法的显式区分。"""

    def test_p15_excess_basis_is_pit_bound_and_fails_closed(self):
        """P15：超额口径只用 PIT 基准证据；缺基准 → unavailable，不退回 raw。"""
        prices = {"2024-09-30": 20.0, "2024-10-09": 22.0}
        benchmark = {"2024-09-30": 100.0, "2024-10-09": 105.0, "2024-10-10": 900.0}

        label = SL.selection_label(
            code="600519", decision_at="2024-09-27T16:00:00+08:00", horizon=2,
            sessions=GOLDEN_WEEK, prices=prices, asof="2024-10-31",
            basis=SL.BASIS_EXCESS, benchmark_prices=benchmark,
        )
        self.assertAlmostEqual(0.05, label.excess_return, places=12)
        # exit 之后的基准价格（10-10 的 900）绝不参与。
        later = dict(benchmark)
        later["2024-10-10"] = -5.0
        self.assertEqual(
            label.excess_return,
            SL.selection_label(
                code="600519", decision_at="2024-09-27T16:00:00+08:00", horizon=2,
                sessions=GOLDEN_WEEK, prices=prices, asof="2024-10-31",
                basis=SL.BASIS_EXCESS, benchmark_prices=later,
            ).excess_return,
        )

        missing_benchmark = SL.selection_label(
            code="600519", decision_at="2024-09-27T16:00:00+08:00", horizon=2,
            sessions=GOLDEN_WEEK, prices=prices, asof="2024-10-31",
            basis=SL.BASIS_EXCESS, benchmark_prices=None,
        )
        self.assertEqual(SL.STATUS_UNAVAILABLE, missing_benchmark.label_status)
        self.assertEqual(SL.REASON_BENCHMARK_EVIDENCE_MISSING, missing_benchmark.label_reason)
        self.assertIsNone(missing_benchmark.label_score)
        self.assertIsNone(missing_benchmark.excess_return)

    def test_p16_halt_and_missing_sessions_are_explicit_never_filled_zero(self):
        """P16：停牌 / 缺失 / 非法价格是三种不同状态，且都不是 0 收益。"""
        start = {day: 10.0 for day in SESSIONS}
        start.update({"2024-06-17": 10.0, "2024-06-19": 12.0})

        halted_entry = _label(prices={**start, "2024-06-17": {"close": None, "halted": True}})
        self.assertEqual(SL.STATUS_UNAVAILABLE, halted_entry.label_status)
        self.assertEqual(SL.REASON_ENTRY_HALTED, halted_entry.label_reason)
        self.assertIsNone(halted_entry.raw_forward_return)

        halted_exit = _label(prices={**start, "2024-06-19": {"close": None, "halted": True}})
        self.assertEqual(SL.STATUS_UNAVAILABLE, halted_exit.label_status)
        self.assertEqual(SL.REASON_EXIT_HALTED, halted_exit.label_reason)
        self.assertIsNone(halted_exit.raw_forward_return)

        missing_exit = _label(prices={key: value for key, value in start.items()
                                      if key != "2024-06-19"})
        self.assertEqual(SL.STATUS_UNAVAILABLE, missing_exit.label_status)
        self.assertEqual(SL.REASON_EXIT_PRICE_MISSING, missing_exit.label_reason)

        for bad in (0, -1.0, "NaN", float("nan"), float("inf"), "abc"):
            invalid = _label(prices={**start, "2024-06-19": bad})
            self.assertEqual(SL.STATUS_UNAVAILABLE, invalid.label_status, bad)
            self.assertEqual(SL.REASON_EXIT_PRICE_INVALID, invalid.label_reason, bad)
            self.assertIsNone(invalid.raw_forward_return)

        # 三种失败原因必须互不相同 —— 不许全部塌成同一个 NaN。
        self.assertEqual(
            4,
            len({halted_entry.label_reason, halted_exit.label_reason,
                 missing_exit.label_reason, SL.REASON_EXIT_PRICE_INVALID}),
        )
        # 明确的市场交易日但停牌 ≠ 行情缺失：状态不同、原因不同。
        self.assertNotEqual(halted_exit.label_reason, missing_exit.label_reason)

    def test_p16b_price_point_states_are_never_zero(self):
        """P16b：价格归一化本身从不产出 0 作为“缺失”的替身。"""
        for raw in (None, "", "  ", "null", "-", "none", "NAT"):
            price, state = SL.price_point(raw)
            self.assertIsNone(price, raw)
            self.assertEqual(SL.PRICE_MISSING, state, raw)
        for raw in (0, "nan", float("nan"), float("inf"), "abc", True):
            price, state = SL.price_point(raw)
            self.assertIsNone(price, raw)
            self.assertEqual(SL.PRICE_INVALID, state, raw)
        for raw in ({"close": None, "halted": True}, {"tradable": False}, {"suspended": True}):
            price, state = SL.price_point(raw)
            self.assertIsNone(price, raw)
            self.assertEqual(SL.PRICE_HALTED, state, raw)
        self.assertEqual((10.0, SL.PRICE_OK), SL.price_point(10.0))
        self.assertEqual((10.0, SL.PRICE_OK), SL.price_point({"close": 10.0, "tradable": True}))


# ──────────────────────────── P17–P20 ────────────────────────────


class LearningAssemblyTest(unittest.TestCase):
    """只有 verified outcome 能进 verified learning set；特征与标签严格隔离。"""

    def _records(self):
        prices_up = {**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0}
        labels = {
            "verified": _label(code="600001", prices=prices_up),
            "pending": _label(code="600002", prices={d: 10.0 for d in SESSIONS},
                              asof="2024-06-18"),
            "unavailable": _label(code="600003",
                                  prices={d: 10.0 for d in SESSIONS if d != "2024-06-19"}),
            "invalid": _label(code="600004", decision_at="nope"),
        }
        features = [
            SL.feature_record(
                code=label.code, decision_at=label.decision_at, horizon=label.horizon,
                features={"momentum": 0.1}, selected=(name == "verified"),
            )
            for name, label in labels.items()
        ]
        return labels, features

    def test_p17_unfinished_samples_never_enter_the_verified_set(self):
        """P17：pending / unavailable / invalid 一律不进 verified 学习集。"""
        labels, features = self._records()
        result = SL.assemble_learning_rows(features, [SL.label_record(x) for x in labels.values()])
        report = result["report"]
        self.assertEqual(1, len(result["rows"]))
        self.assertEqual(SL.STATUS_VERIFIED, result["rows"][0]["label_status"])
        self.assertEqual(3, report["excluded"]["unverified_label"])
        self.assertEqual(1, report["label_status_counts"][SL.STATUS_PENDING])
        self.assertEqual(1, report["label_status_counts"][SL.STATUS_UNAVAILABLE])
        self.assertEqual(1, report["label_status_counts"][SL.STATUS_INVALID])
        self.assertEqual(4, report["label_rows"])
        for row in result["rows"]:
            self.assertEqual(SL.STATUS_VERIFIED, row["label_status"])
            self.assertIsNotNone(row["label_score"])

    def test_p18_end_to_end_dataset_reports_every_exclusion(self):
        """P18：排除必须逐个计数上报，绝不静默消失。"""
        labels, features = self._records()
        label_records = [SL.label_record(x) for x in labels.values()]

        # 一条特征完全没有对应标签 → 计入 missing_label，而不是被丢掉。
        orphan = SL.feature_record(code="000002", decision_at="2024-06-14T16:00:00+08:00",
                                   horizon=2, features={"momentum": 0.2})
        # 反过来：一条标签没有对应特征 → 计入 missing_feature。
        extra_label = SL.label_record(
            _label(code="000003", decision_at="2024-06-13T16:00:00+08:00", horizon=2,
                   prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0})
        )

        result = SL.assemble_learning_rows(
            features + [orphan], label_records + [extra_label], require_verified=True
        )
        report = result["report"]
        self.assertEqual(5, report["feature_rows"])
        self.assertEqual(5, report["label_rows"])
        self.assertEqual(1, report["assembled_rows"])
        self.assertEqual(1, report["excluded"]["missing_label"])
        self.assertEqual(1, report["excluded"]["missing_feature"])
        self.assertEqual(3, report["excluded"]["unverified_label"])
        self.assertEqual(1, len(result["rows"]))

        # 只按 code join 会把 4 个不同状态/决策塌成一条 —— 明确禁止。
        self.assertEqual(1, len({row["sample_key"] for row in result["rows"]}))
        self.assertEqual(4, len({record["code"] for record in label_records}))
        self.assertEqual(
            {SL.STATUS_VERIFIED, SL.STATUS_PENDING, SL.STATUS_UNAVAILABLE, SL.STATUS_INVALID},
            set(report["label_status_counts"]) - {
                status for status, count in report["label_status_counts"].items() if count == 0
            },
        )

    def test_p19_label_fields_cannot_leak_into_the_feature_record(self):
        """P19：未来 outcome 塞进特征表会被结构性拒绝。"""
        good = _label(prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0})
        for field_name in ("label_score", "label_class", "raw_forward_return",
                           "excess_return", "exit_price", "label_status"):
            with self.assertRaises(ValueError):
                SL.assert_feature_record({
                    "code": "600001", "decision_at": T_AFTER_CLOSE, "horizon": 2,
                    "label_version": SL.label_version(), "features": {}, field_name: 1.0,
                })
        with self.assertRaises(ValueError):
            SL.assert_label_record({**SL.label_record(good), "features": {"momentum": 1.0}})
        with self.assertRaises(ValueError):
            SL.assert_feature_record({
                "code": "600001", "decision_at": T_AFTER_CLOSE, "horizon": 2,
                "label_version": SL.label_version(), "features": {}, "surprise": 1.0,
            })

    def test_p20_changing_pre_decision_prices_leaves_the_label_boundary_intact(self):
        """P20（P1 的反向控制）：改 T 前价格不改变 labels 的计算边界。"""
        stable_tail = {"2024-06-17": 10.0, "2024-06-19": 12.0}
        cheap_history = {day: 1.0 for day in SESSIONS[:4]}
        rich_history = {day: 1000.0 for day in SESSIONS[:4]}

        cheap = _label(prices={**cheap_history, **stable_tail})
        rich = _label(prices={**rich_history, **stable_tail})

        self.assertEqual(SL.STATUS_VERIFIED, cheap.label_status)
        self.assertEqual("2024-06-17", cheap.entry_date)
        self.assertEqual("2024-06-19", cheap.exit_date)
        self.assertAlmostEqual(0.2, cheap.raw_forward_return, places=12)
        self.assertAlmostEqual(0.2, rich.raw_forward_return, places=12)
        self.assertEqual(cheap.entry_date, rich.entry_date)
        self.assertEqual(cheap.exit_date, rich.exit_date)
        self.assertEqual(cheap.entry_price, rich.entry_price)
        self.assertEqual(cheap.exit_price, rich.exit_price)

    def test_p20b_join_on_version_is_enforced(self):
        """P20b：join 身份含 label_version，跨版本不会错配。"""
        raw_feature = SL.feature_record(
            code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
            version=SL.label_version(SL.BASIS_RAW), features={"momentum": 0.1},
        )
        # 特征声明 v1/raw-return，标签是 v1/excess-return → 不得匹配。
        excess_label = SL.label_record(
            SL.selection_label(
                code="600001", decision_at=T_AFTER_CLOSE, horizon=2, sessions=SESSIONS,
                prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0}, asof=ASOF,
                basis=SL.BASIS_EXCESS,
                benchmark_prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0},
            )
        )
        result = SL.assemble_learning_rows([raw_feature], [excess_label])
        self.assertEqual(0, len(result["rows"]))
        self.assertEqual(1, result["report"]["excluded"]["missing_label"])


# ─────────────── P17/P18 extension: dataset & report consumers ───────────────


class LearningDatasetBridgeTest(unittest.TestCase):
    """selection outcome 进入 learning dataset 的桥：只有 verified 被放行。"""

    PRICES_UP = {**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0}

    def _verified_record(self):
        return SL.label_record(_label(prices=self.PRICES_UP))

    def test_verified_outcome_maps_to_pit_verified(self):
        import learning_dataset as LD

        evidence = LD.selection_label_evidence(self._verified_record())
        self.assertTrue(evidence["verified"])
        self.assertEqual(LD.PIT_VERIFIED, evidence["pit_status"])
        self.assertEqual("2024-06-19", evidence["label_end_date"])
        self.assertEqual("2024-06-19T15:00:00+08:00", evidence["label_available_at"])
        self.assertIsNone(evidence["exclusion_reason"])
        # 只有 verified 才可能落进 strict 数据集。
        self.assertIn(evidence["pit_status"], LD.STRICT_PIT_STATUSES)

    def test_non_verified_outcomes_are_never_promoted(self):
        import learning_dataset as LD

        cases = {
            "2024-06-18": (SL.STATUS_PENDING, "immature_label"),
            "2024-06-30": (SL.STATUS_UNAVAILABLE, "missing_label"),
        }
        pending = SL.label_record(_label(prices={d: 10.0 for d in SESSIONS}, asof="2024-06-18"))
        unavailable = SL.label_record(
            _label(prices={d: 10.0 for d in SESSIONS if d != "2024-06-19"})
        )
        invalid = SL.label_record(_label(decision_at="nope"))

        for record, expected in (
            (pending, cases["2024-06-18"]),
            (unavailable, cases["2024-06-30"]),
            (invalid, (SL.STATUS_INVALID, "invalid_label_time")),
        ):
            evidence = LD.selection_label_evidence(record)
            self.assertEqual(expected[0], evidence["label_status"])
            self.assertFalse(evidence["verified"], record["label_status"])
            self.assertEqual(LD.PIT_UNPROVEN, evidence["pit_status"])
            self.assertEqual(expected[1], evidence["exclusion_reason"])
            self.assertNotIn(evidence["pit_status"], LD.STRICT_PIT_STATUSES)
            self.assertIsNone(evidence["label_available_at"])
            self.assertIsNone(evidence["label_score"])

    def test_verified_claim_without_evidence_is_demoted(self):
        import learning_dataset as LD

        good = self._verified_record()
        for broken in (
            {**good, "label_score": None},
            {**good, "label_score": float("nan")},
            {**good, "exit_date": None},
            {**good, "label_status": "something_new"},
        ):
            evidence = LD.selection_label_evidence(broken)
            self.assertFalse(evidence["verified"], broken)
            self.assertEqual(LD.PIT_UNPROVEN, evidence["pit_status"])
            self.assertIsNotNone(evidence["exclusion_reason"])

    def test_every_status_is_counted_never_silently_dropped(self):
        import learning_dataset as LD

        records = [
            self._verified_record(),
            SL.label_record(_label(code="600002", prices={d: 10.0 for d in SESSIONS},
                                   asof="2024-06-18")),
            SL.label_record(_label(code="600003",
                                   prices={d: 10.0 for d in SESSIONS if d != "2024-06-19"})),
            SL.label_record(_label(code="600004", decision_at="nope")),
        ]
        result = LD.selection_outcome_rows(records)
        self.assertEqual(4, result["report"]["input_rows"])
        self.assertEqual(1, result["report"]["accepted_rows"])
        self.assertEqual(1, result["report"]["status_counts"][SL.STATUS_VERIFIED])
        self.assertEqual(1, result["report"]["status_counts"][SL.STATUS_PENDING])
        self.assertEqual(1, result["report"]["status_counts"][SL.STATUS_UNAVAILABLE])
        self.assertEqual(1, result["report"]["status_counts"][SL.STATUS_INVALID])
        self.assertEqual(1, result["report"]["exclusion_reasons"]["immature_label"])
        self.assertEqual(1, result["report"]["exclusion_reasons"]["missing_label"])
        self.assertEqual(1, result["report"]["exclusion_reasons"]["invalid_label_time"])
        self.assertTrue(all(row["verified"] for row in result["rows"]))


class AlphaReportLabelSemanticsTest(unittest.TestCase):
    """选股 alpha 报告必须复用唯一契约，而不是自己再写一套标签。"""

    KLINE = {
        "2024-06-13": (10.0, 10.0),
        "2024-06-14": (10.0, 42.0),   # 决策日：收盘价 42（收盘后才形成信号）
        "2024-06-17": (10.8, 11.0),
        "2024-06-18": (11.0, 11.5),
        "2024-06-19": (12.0, 12.1),
    }

    def test_entry_is_the_session_after_the_decision_not_the_decision_day(self):
        import selection_alpha_report as AR

        sessions = SL.normalize_sessions(self.KLINE)
        result = AR.label_for("600001", "2024-06-14", 2, sessions,
                              asof="2024-06-30", kline=self.KLINE)
        self.assertEqual(SL.STATUS_VERIFIED, result.label_status)
        self.assertEqual("2024-06-17", result.entry_date)
        self.assertEqual("2024-06-19", result.exit_date)
        self.assertAlmostEqual(11.0, result.entry_price, places=12)
        self.assertAlmostEqual(12.1 / 11.0 - 1.0, result.raw_forward_return, places=12)
        self.assertEqual(SL.label_version(SL.BASIS_RAW), result.label_version)

        legacy = AR.legacy_forward_returns(self.KLINE, "2024-06-14", 2)
        # 旧口径拿决策日收盘 42 当 entry → 一个现实中不可执行的成交价，
        # 得出的收益完全不同（这里是 -0.71 vs +0.10）。
        self.assertNotAlmostEqual(legacy, result.raw_forward_return)
        self.assertLess(legacy, 0)

    def test_unfinished_samples_are_counted_but_not_scored(self):
        import selection_alpha_report as AR

        sessions = SL.normalize_sessions(self.KLINE)
        lines, summary = AR.evaluate(
            [("s1", "2024-06-14", "600001")], 2, "多空候选", sessions, asof="2024-06-17"
        )
        self.assertEqual(0, summary["n"])
        self.assertIsNone(summary["mean_return"])
        self.assertIsNone(summary["mean_excess"])
        self.assertEqual(1, summary["label_status_counts"][SL.STATUS_PENDING])
        self.assertEqual(0, summary["label_status_counts"][SL.STATUS_VERIFIED])
        self.assertEqual(SL.label_version(SL.BASIS_RAW), summary["label_version"])
        # 未走完的样本绝不被当成负例：报告里没有任何“胜率”可以因它而变差。
        self.assertIn("pending 1", "\n".join(lines))


class NonFiniteOutcomeTest(unittest.TestCase):
    """P21：非有限/缺失的未来收益永远不变成 0% 样本，也不污染截面 center。

    这是「缺数据 != 0%」在**真实消费者**上的落点：GA 的 alpha 数据集。
    SQLite 会把 NaN 存成 NULL（于是被 ``NOT NULL`` 拒绝），但 ±Inf **能落库**；
    一个 Inf 曾把同窗口的 ``AVG`` 拉成 ±Inf，把所有样本的 excess 一起污染。
    """

    SCHEMA = (
        "CREATE TABLE adaptive_alpha_samples(profile_date TEXT, code TEXT, regime REAL,"
        " price_momentum REAL, main_flow REAL, turnover REAL, volume_ratio REAL,"
        " small_size REAL, value REAL);"
        "CREATE TABLE adaptive_alpha_returns(start_date TEXT, end_date TEXT, horizon INTEGER,"
        " code TEXT, forward_return_pct REAL NOT NULL);"
    )

    def _conn(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(self.SCHEMA)
        for code, value in rows:
            conn.execute(
                "INSERT INTO adaptive_alpha_samples VALUES(?,?,?,?,?,?,?,?,?)",
                ("2024-06-14", code, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            )
            conn.execute(
                "INSERT INTO adaptive_alpha_returns VALUES(?,?,?,?,?)",
                ("2024-06-14", "2024-06-19", 2, code, value),
            )
        return conn

    def test_p21b_a_window_without_any_finite_outcome_yields_no_samples(self):
        import adaptive_engine as AE

        self.assertEqual([], AE._alpha_dataset(self._conn([("600001", float("inf"))])))

    def test_p21_non_finite_outcome_is_never_a_zero_return_sample(self):
        import adaptive_engine as AE

        dataset = AE._alpha_dataset(
            self._conn([("600001", 3.0), ("600002", float("inf")), ("600003", -1.0)])
        )
        # Inf 既不进数据集（600002 消失），也不参与 center：center 只取有限样本
        # 均值 (3.0 + -1.0) / 2 = 1.0 → excess 2.0 / -2.0。
        self.assertEqual(
            {"600001": 2.0, "600003": -2.0},
            {row["code"]: round(row["excess_return_pct"], 10) for row in dataset},
        )
        for row in dataset:
            self.assertTrue(math.isfinite(row["excess_return_pct"]), row)


# ─────────── review round 2: P22–P30 (exact-head blockers) ───────────


#: 一只上涨的票：entry 2024-06-17 收 10 → exit 2024-06-19 收 11（+10%）。
PRICES_UP = {**{d: 10.0 for d in SESSIONS}, "2024-06-19": 11.0}


class EvidenceAvailabilityTest(unittest.TestCase):
    """证据可用时点：收盘价在**收市时刻**才存在，asof 必须按完整 timestamp 比。"""

    def test_p22_intraday_asof_on_the_exit_day_cannot_consume_that_close(self):
        """P22：exit 日盘中评估看不到当日收盘价 —— pending，不是已验证收益。"""
        cases = (
            "2024-06-19T09:30:00+08:00",
            "2024-06-19T10:00:00+08:00",
            "2024-06-19T14:59:59+08:00",
        )
        for asof in cases:
            label = _label(prices=PRICES_UP, asof=asof)
            self.assertEqual(SL.STATUS_PENDING, label.label_status, asof)
            self.assertEqual(SL.REASON_HORIZON_NOT_MATURED, label.label_reason, asof)
            self.assertFalse(label.verified, asof)
            # 尚未发生的收盘价不得被消费成一个"已证明"的未来收益。
            self.assertIsNone(label.label_score, asof)
            self.assertIsNone(label.raw_forward_return, asof)
            self.assertIsNone(label.exit_price, asof)
            # 也绝不能因此被当成负例。
            self.assertNotEqual(SL.CLASS_NEGATIVE, label.label_class, asof)

        # 收市时刻与 date-only（= 该交易日的结束）之后才是完整证据。
        for asof in ("2024-06-19T15:00:00+08:00", "2024-06-19", ASOF):
            label = _label(prices=PRICES_UP, asof=asof)
            self.assertEqual(SL.STATUS_VERIFIED, label.label_status, asof)
            self.assertAlmostEqual(0.1, label.raw_forward_return, places=12)

        # 盘中标签与收市后标签是**同一个样本身份**，只是证据成熟度不同。
        self.assertEqual(
            _label(prices=PRICES_UP, asof="2024-06-19T10:00:00+08:00").sample_key,
            _label(prices=PRICES_UP, asof="2024-06-19").sample_key,
        )

    def test_p23_decision_at_is_compared_as_a_full_timestamp(self):
        """P23：decision_at 与 asof 必须按完整 timestamp 比，不能只比日期。"""
        later = _label(
            prices=PRICES_UP,
            decision_at="2024-06-14T16:00:00+08:00",
            asof="2024-06-14T10:00:00+08:00",
        )
        self.assertEqual(SL.STATUS_INVALID, later.label_status)
        self.assertEqual(SL.REASON_DECISION_AFTER_EVALUATION, later.label_reason)
        self.assertIsNone(later.label_score)
        self.assertIsNone(later.raw_forward_return)
        self.assertNotEqual(SL.CLASS_NEGATIVE, later.label_class)
        # 决策时点被如实保留，便于审计（而不是被静默改写成 asof）。
        self.assertEqual("2024-06-14", later.decision_trade_date)

        # 同一瞬间不算"之后"；date-only asof = 该自然日结束。
        same_moment = _label(
            prices=PRICES_UP,
            decision_at="2024-06-14T16:00:00+08:00",
            asof="2024-06-14T16:00:00+08:00",
        )
        self.assertEqual(SL.STATUS_PENDING, same_moment.label_status)
        date_only = _label(prices=PRICES_UP, asof="2024-06-14")
        self.assertEqual(SL.STATUS_PENDING, date_only.label_status)

    def test_p24_nonnumeric_horizon_is_invalid_not_an_exception(self):
        """P24：非数值 horizon 收敛成 invalid 标签，绝不让整批标注中断。"""
        for horizon in ("bad", "", "  ", "two", [], {}, float("nan")):
            label = _label(horizon=horizon)
            self.assertEqual(SL.STATUS_INVALID, label.label_status, horizon)
            self.assertEqual(SL.REASON_INVALID_HORIZON, label.label_reason, horizon)
            self.assertIsNone(label.label_score, horizon)
            self.assertFalse(label.verified, horizon)
        # 身份哨兵稳定（同一个坏值永远同一个 key），且绝不冒充一个真实 horizon。
        # 所有非法 horizon 共用哨兵身份是**有意**的：它们全都是 invalid，
        # 永远不会进入任何数据集，不需要靠身份区分彼此。
        self.assertEqual(
            _label(horizon="bad").sample_key, _label(horizon="bad").sample_key
        )
        self.assertNotEqual(_label(horizon="bad").sample_key, _label(horizon=2).sample_key)
        self.assertNotEqual(_label(horizon="bad").sample_key, _label(horizon=1).sample_key)


class VerifiedEvidenceTest(unittest.TestCase):
    """``label_status='verified'`` 是自称；证据不成立就必须 fail closed。"""

    def _feature(self, code="600001"):
        return SL.feature_record(
            code=code, decision_at=T_AFTER_CLOSE, horizon=2, features={"momentum": 0.1}
        )

    def test_p25_verified_claims_without_evidence_never_enter_the_dataset(self):
        """P25：只有状态字符串、没有证据的记录不得进 verified 学习集。"""
        import learning_dataset as LD

        good = SL.label_record(_label(prices=PRICES_UP))
        broken_cases = {
            "no_score": ({"label_score": None}, "invalid_verified_evidence"),
            "nan_score": ({"label_score": float("nan")}, "invalid_verified_evidence"),
            "no_exit_date": ({"exit_date": None}, "invalid_verified_evidence"),
            "no_entry_price": ({"entry_price": None}, "invalid_verified_evidence"),
            "zero_entry_price": ({"entry_price": 0.0}, "invalid_verified_evidence"),
            "inconsistent_raw": ({"raw_forward_return": 0.99}, "invalid_verified_evidence"),
            "inconsistent_score": ({"label_score": 0.99}, "invalid_verified_evidence"),
            "reversed_dates": ({"entry_date": "2024-06-20"}, "invalid_verified_evidence"),
            "nan_exit_price": ({"exit_price": float("nan")}, "invalid_verified_evidence"),
            # 三个收益字段必须互相自洽（excess == raw - benchmark）：只对上 score
            # 还不足以证明这条记录描述了一个真实窗口。
            "excess_arithmetic": ({"benchmark_return": 0.05, "excess_return": 0.99},
                                  "invalid_verified_evidence"),
            # 空版本同时让样本身份对不上 → 由身份校验先拦下（同样是拒绝）。
            "no_version": ({"label_version": ""}, "sample_key_mismatch"),
        }
        for name, (patch, counter) in broken_cases.items():
            record = {**good, **patch}
            self.assertFalse(SL.verified_evidence(record)["verified"], name)
            self.assertTrue(SL.verified_evidence(record)["claimed_verified"], name)
            # 两个消费者必须给出一致判定：桥不重复实现标签逻辑。
            self.assertEqual(
                SL.verified_evidence(record)["verified"],
                LD.selection_label_evidence(record)["verified"],
                name,
            )
            for require in (True, False):
                result = SL.assemble_learning_rows([self._feature()], [record],
                                                   require_verified=require)
                self.assertEqual([], result["rows"], (name, require))
                self.assertEqual(1, result["report"]["excluded"][counter], (name, require))

        # 身份不参与版本时，空版本仍然被**证据**校验拦下，不是靠身份错配兜住。
        result = SL.assemble_learning_rows(
            [self._feature()], [{**good, "label_version": ""}], join_on_version=False
        )
        self.assertEqual([], result["rows"])
        self.assertEqual(0, result["report"]["excluded"]["sample_key_mismatch"])
        self.assertEqual(1, result["report"]["excluded"]["invalid_verified_evidence"])

        # 真有证据的记录照常通过，且带的是校验过的数字。
        result = SL.assemble_learning_rows([self._feature()], [good])
        self.assertEqual(1, len(result["rows"]))
        row = result["rows"][0]
        self.assertEqual(SL.STATUS_VERIFIED, row["label_status"])
        self.assertIsNotNone(row["label_score"])
        self.assertEqual(good["exit_date"], row["exit_date"])
        self.assertEqual(0, result["report"]["excluded"]["invalid_verified_evidence"])

    def test_p26_conflicting_labels_are_refused_and_order_independent(self):
        """P26：同一身份的不同结论不得 last-write-wins，必须整体拒绝。"""
        up = SL.label_record(_label(prices=PRICES_UP))
        down = SL.label_record(
            _label(prices={**{d: 10.0 for d in SESSIONS}, "2024-06-19": 9.0})
        )
        self.assertEqual(up["sample_key"], down["sample_key"])
        self.assertNotEqual(up["raw_forward_return"], down["raw_forward_return"])

        feature = self._feature()
        for records in ([up, down], [down, up]):
            result = SL.assemble_learning_rows([feature], records)
            self.assertEqual([], result["rows"], records)
            self.assertEqual(2, result["report"]["excluded"]["conflicting_label"])
            self.assertEqual(0, result["report"]["excluded"]["duplicate_label"])

        # 第三条同身份记录也不会让被拒绝的样本复活。
        result = SL.assemble_learning_rows([feature], [up, down, up])
        self.assertEqual([], result["rows"])
        self.assertEqual(3, result["report"]["excluded"]["conflicting_label"])

        # 逐字节相同的重复：任何顺序都得到同一条，安全去重、不算冲突。
        for records in ([up, dict(up), up], [up, up, dict(up)]):
            result = SL.assemble_learning_rows([feature], records)
            self.assertEqual(1, len(result["rows"]))
            self.assertEqual(2, result["report"]["excluded"]["duplicate_label"])
            self.assertEqual(0, result["report"]["excluded"]["conflicting_label"])

    def test_p27_sample_identity_survives_the_label_record_bridge(self):
        """P27：``label_record`` 必须携带 sample identity，贴错身份的要被拒。"""
        import learning_dataset as LD

        good = _label(prices=PRICES_UP)
        record = SL.label_record(good)
        self.assertEqual(good.sample_key, record["sample_key"])
        self.assertEqual(
            good.sample_key,
            SL.sample_identity(record["code"], record["decision_at"],
                               record["horizon"], record["label_version"]),
        )
        # 桥接层拿到的是同一个身份，而不是 None / 空串。
        self.assertEqual(good.sample_key, LD.selection_label_evidence(record)["sample_key"])

        feature = self._feature()
        foreign = {
            **record,
            "sample_key": SL.sample_identity(
                "000002", good.decision_at, good.horizon, good.label_version
            ),
        }
        result = SL.assemble_learning_rows([feature], [foreign])
        self.assertEqual([], result["rows"])
        self.assertEqual(1, result["report"]["excluded"]["sample_key_mismatch"])
        # 正确身份的记录不受影响。
        ok = SL.assemble_learning_rows([feature], [record])
        self.assertEqual(1, len(ok["rows"]))
        self.assertEqual(record["sample_key"], ok["rows"][0]["sample_key"])


class FilledSignalDecisionDayTest(unittest.TestCase):
    """模拟盘成交样本的决策日必须是 ``signal_date``，不是执行日 ``intended_date``。"""

    KLINE = {
        "2024-06-13": (10.0, 10.0),
        "2024-06-14": (10.0, 10.0),
        "2024-06-17": (10.0, 10.0),   # 周一收盘后形成信号
        "2024-06-18": (10.5, 10.5),   # 周二成交
        "2024-06-19": (11.0, 11.0),
        "2024-06-20": (11.2, 11.2),
    }

    def test_p28_filled_signals_are_labelled_from_the_signal_date(self):
        """P28：用 signal_date 作决策日 → entry 正好是 intended_date 那个成交日。"""
        import os
        import sqlite3 as sq
        import tempfile

        import selection_alpha_report as AR

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "paper_trading.sqlite3")
            conn = sq.connect(db)
            conn.execute(
                "CREATE TABLE paper_signals(account_id TEXT, signal_date TEXT,"
                " intended_date TEXT, code TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_signals VALUES('acc','2024-06-17','2024-06-18','600001','filled')"
            )
            # 未成交的信号不参与，且不能被当成样本。
            conn.execute(
                "INSERT INTO paper_signals VALUES('acc','2024-06-17','2024-06-18','600002','pending')"
            )
            conn.commit()
            conn.close()

            original = AR.DATA_DIR
            AR.DATA_DIR = tmp
            try:
                picks = AR.picks_from_signals(3650)
            finally:
                AR.DATA_DIR = original

        self.assertEqual([("filled:acc", "2024-06-17", "600001")], picks)

        sessions = SL.normalize_sessions(self.KLINE)
        correct = AR.label_for("600001", picks[0][1], 2, sessions,
                               asof="2024-06-30", kline=self.KLINE)
        self.assertEqual(SL.STATUS_VERIFIED, correct.label_status)
        # 决策日 06-17 → entry = 06-18 = 真实成交日。
        self.assertEqual("2024-06-18", correct.entry_date)
        self.assertEqual("2024-06-20", correct.exit_date)

        # 用 intended_date 当决策日会把 entry 再往后推一个交易日 → 整条收益错位。
        shifted = AR.label_for("600001", "2024-06-18", 1, sessions,
                               asof="2024-06-30", kline=self.KLINE)
        self.assertEqual(SL.STATUS_VERIFIED, shifted.label_status)
        self.assertEqual("2024-06-19", shifted.entry_date)
        signal_day = AR.label_for("600001", "2024-06-17", 1, sessions,
                                  asof="2024-06-30", kline=self.KLINE)
        self.assertEqual("2024-06-18", signal_day.entry_date)
        self.assertNotAlmostEqual(signal_day.raw_forward_return, shifted.raw_forward_return)


class NonFiniteQuotaTest(unittest.TestCase):
    """非有限收益不得占用每个窗口的样本配额。"""

    SCHEMA = NonFiniteOutcomeTest.SCHEMA
    HORIZON = 2

    def _order_key(self, code):
        return (int(code) * 1103515245 + self.HORIZON * 12345) & 2147483647

    def test_p29_infinite_rows_do_not_consume_the_window_quota(self):
        """P29：有限性谓词必须在 SQL 里，坏行不能挤掉有效样本。"""
        import adaptive_engine as AE

        # 120 只票，按 SQL 的确定性排序键把最小的 105 只全部塞成 Inf ——
        # 配额会被坏行占满，排在后面的有限样本原本根本轮不到。
        # 生产默认配额（1200）远大于这个 fixture，所以这里把配额钉在下限 100，
        # 让 ``LIMIT`` 真正成为瓶颈；否则这个测试对"先 LIMIT 后过滤"是盲的。
        codes = sorted((str(600000 + i) for i in range(120)), key=self._order_key)
        poisoned, clean = codes[:105], codes[105:]
        self.assertGreater(len(poisoned), 100)
        self.assertTrue(set(poisoned).isdisjoint(clean))

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(self.SCHEMA)
        for code, value in [(c, float("inf")) for c in poisoned] + [(c, 3.0) for c in clean]:
            conn.execute(
                "INSERT INTO adaptive_alpha_samples VALUES(?,?,?,?,?,?,?,?,?)",
                ("2024-06-14", code, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            )
            conn.execute(
                "INSERT INTO adaptive_alpha_returns VALUES(?,?,?,?,?)",
                ("2024-06-14", "2024-06-19", self.HORIZON, code, value),
            )

        dataset = AE._alpha_dataset(conn, max_rows_per_window=100)
        self.assertEqual(set(clean), {row["code"] for row in dataset})
        for row in dataset:
            self.assertTrue(math.isfinite(row["excess_return_pct"]), row)
            # center 只由有限样本构成 → 3.0 的均值，excess 恒为 0。
            self.assertAlmostEqual(0.0, row["excess_return_pct"], places=12)


# ───── review round 3: P30–P32 (excess-return serialization contract) ─────


#: entry 2024-06-17 收 10 → exit 2024-06-19 收 11（raw +10%）。
#: 同窗口基准 100 → 105（+5%）→ excess +5%。raw != excess，正是本轮要证明的那类样本。
EXCESS_PRICES = {**{d: 10.0 for d in SESSIONS}, "2024-06-17": 10.0, "2024-06-19": 11.0}
EXCESS_BENCHMARK = {**{d: 100.0 for d in SESSIONS}, "2024-06-17": 100.0, "2024-06-19": 105.0}


def _excess_label(**overrides):
    kwargs = {
        "code": "600001",
        "decision_at": T_AFTER_CLOSE,
        "horizon": 2,
        "sessions": SESSIONS,
        "prices": EXCESS_PRICES,
        "asof": ASOF,
        "basis": SL.BASIS_EXCESS,
        "benchmark_prices": EXCESS_BENCHMARK,
    }
    kwargs.update(overrides)
    return SL.selection_label(**kwargs)


class ExcessReturnSerializationTest(unittest.TestCase):
    """excess-return 标签必须能走完 ``label_record`` → dataset 的完整链路。

    修复前 ``LABEL_RECORD_FIELDS`` 不含 ``basis``，``verified_evidence()`` 便用
    ``record.get("basis") or DEFAULT_BASIS`` 把每条超额标签读成 raw-return，
    再用 ``label_score == raw_forward_return`` 去否定它（raw +10% vs excess +5%）
    → 合法标签被降级成 ``inconsistent_outcome``。P8/P15 只证明标签**被算出来**，
    P20b 只证明 raw/excess **不匹配**；下面这条才是"能进数据集"的正面证明。
    """

    def test_p30_excess_return_survives_the_canonical_record_path(self):
        """P30：raw != excess 的真实超额标签，经 canonical 序列化后仍被放行。"""
        import learning_dataset as LD

        label = _excess_label()
        # 前提：这确实是一条 raw != excess 的样本，否则整条测试没有区分力。
        self.assertEqual(SL.STATUS_VERIFIED, label.label_status)
        self.assertAlmostEqual(0.10, label.raw_forward_return, places=12)
        self.assertAlmostEqual(0.05, label.benchmark_return, places=12)
        self.assertAlmostEqual(0.05, label.excess_return, places=12)
        self.assertAlmostEqual(0.05, label.label_score, places=12)
        self.assertNotAlmostEqual(label.raw_forward_return, label.label_score, places=6)
        self.assertEqual(SL.BASIS_EXCESS, label.basis)
        self.assertEqual("selection-label-v1/excess-return", label.label_version)

        record = SL.label_record(label)
        self.assertEqual("excess-return", record["basis"])
        self.assertEqual("selection-label-v1/excess-return", record["label_version"])

        verdict = SL.verified_evidence(record)
        self.assertTrue(verdict["verified"], verdict)
        self.assertEqual(SL.STATUS_VERIFIED, verdict["status"])
        self.assertEqual(SL.EVIDENCE_OK, verdict["reason"])
        self.assertAlmostEqual(label.excess_return, verdict["label_score"], places=12)
        self.assertEqual(SL.BASIS_EXCESS, verdict["basis"])

        evidence = LD.selection_label_evidence(record)
        self.assertTrue(evidence["verified"], evidence)
        self.assertEqual(LD.PIT_VERIFIED, evidence["pit_status"])
        self.assertAlmostEqual(label.excess_return, evidence["label_score"], places=12)

        feature = SL.feature_record(
            code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
            version=SL.label_version(SL.BASIS_EXCESS), features={"momentum": 0.1},
        )
        assembled = SL.assemble_learning_rows([feature], [record])
        self.assertEqual(1, assembled["report"]["assembled_rows"])
        self.assertEqual(1, len(assembled["rows"]))
        row = assembled["rows"][0]
        self.assertAlmostEqual(label.excess_return, row["label_score"], places=12)
        self.assertEqual("selection-label-v1/excess-return", row["label_version"])
        self.assertEqual(SL.BASIS_EXCESS, row["basis"])
        self.assertAlmostEqual(0.10, row["raw_forward_return"], places=12)
        self.assertAlmostEqual(0.05, row["benchmark_return"], places=12)
        self.assertEqual(0, assembled["report"]["excluded"]["invalid_verified_evidence"])

        # 反向控制：同一个标签配一条 raw-version 特征 → 不匹配，不能混进数据集。
        raw_feature = SL.feature_record(
            code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
            version=SL.label_version(SL.BASIS_RAW), features={"momentum": 0.1},
        )
        crossed = SL.assemble_learning_rows([raw_feature], [record])
        self.assertEqual([], crossed["rows"])
        self.assertEqual(1, crossed["report"]["excluded"]["missing_label"])

        # 批量桥同样只放行这一条。
        outcome = LD.selection_outcome_rows([record])
        self.assertEqual(1, outcome["report"]["accepted_rows"])
        self.assertTrue(all(item["verified"] for item in outcome["rows"]))

    def test_p30b_the_raw_return_path_is_unchanged(self):
        """P30b：raw-return 正常路径逐段不变（回归保护）。"""
        import learning_dataset as LD

        label = _label(prices=PRICES_UP)
        self.assertEqual(SL.BASIS_RAW, label.basis)
        self.assertEqual("selection-label-v1/raw-return", label.label_version)
        self.assertAlmostEqual(label.raw_forward_return, label.label_score, places=12)

        record = SL.label_record(label)
        self.assertEqual("raw-return", record["basis"])
        verdict = SL.verified_evidence(record)
        self.assertTrue(verdict["verified"], verdict)
        self.assertEqual(SL.BASIS_RAW, verdict["basis"])
        self.assertAlmostEqual(label.raw_forward_return, verdict["label_score"], places=12)
        self.assertTrue(LD.selection_label_evidence(record)["verified"])

        feature = SL.feature_record(
            code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
            version=SL.label_version(SL.BASIS_RAW), features={"momentum": 0.1},
        )
        assembled = SL.assemble_learning_rows([feature], [record])
        self.assertEqual(1, assembled["report"]["assembled_rows"])
        self.assertAlmostEqual(label.raw_forward_return,
                               assembled["rows"][0]["label_score"], places=12)
        self.assertEqual(SL.BASIS_RAW, assembled["rows"][0]["basis"])

    def test_p30c_an_internally_contradictory_excess_triple_is_refused(self):
        """P30c：三个收益字段必须互相自洽 —— ``excess == raw - benchmark``。

        只校验 ``score`` 与其中一个收益字段是自相矛盾的：把 ``label_score`` 和
        ``excess_return`` 一起改大，``score == excess`` 依然成立，但这条记录
        描述不出任何真实窗口。
        """
        import learning_dataset as LD

        good = SL.label_record(_excess_label())
        self.assertTrue(SL.verified_evidence(good)["verified"])
        # 前提：这条记录的三元组确实自洽，否则下面测不出"破坏自洽"这件事。
        self.assertAlmostEqual(good["excess_return"],
                               good["raw_forward_return"] - good["benchmark_return"],
                               places=12)

        feature = SL.feature_record(
            code="600001", decision_at=T_AFTER_CLOSE, horizon=2,
            version=SL.label_version(SL.BASIS_EXCESS), features={"momentum": 0.1},
        )

        forged = {**good, "excess_return": 0.99, "label_score": 0.99}
        self.assertAlmostEqual(forged["label_score"], forged["excess_return"], places=12)
        verdict = SL.verified_evidence(forged)
        self.assertFalse(verdict["verified"], verdict)
        self.assertEqual(SL.EVIDENCE_OUTCOME_INCONSISTENT, verdict["reason"])
        self.assertFalse(LD.selection_label_evidence(forged)["verified"])
        broken = SL.assemble_learning_rows([feature], [forged])
        self.assertEqual([], broken["rows"])
        self.assertEqual(1, broken["report"]["excluded"]["invalid_verified_evidence"])

        # 只把基准改坏，excess 与 score 保持原样 —— 同样必须被拒。
        self.assertFalse(SL.verified_evidence({**good, "benchmark_return": 0.99})["verified"])
        # 只把 raw 改坏（价格与 raw 仍然自洽，但 raw - benchmark != excess）。
        self.assertFalse(
            SL.verified_evidence(
                {**good, "raw_forward_return": 0.5,
                 "exit_price": good["entry_price"] * 1.5}
            )["verified"]
        )

        # 反向控制：换成另一个**自洽**的三元组仍然被放行。
        consistent = {**good, "benchmark_return": 0.02, "excess_return": 0.08,
                      "label_score": 0.08}
        self.assertTrue(SL.verified_evidence(consistent)["verified"])


class BasisVersionConsistencyTest(unittest.TestCase):
    """``label_version`` 是 authoritative；``basis`` 是冗余校验字段。

    冲突 / 缺失 / 不可解析一律 fail closed，且**不得自动修正**：
    "冲突证据 != 可猜测证据"。降级后仍不得进入任何数据集。
    """

    def _feature(self, code="600001", version=None):
        return SL.feature_record(
            code=code, decision_at=T_AFTER_CLOSE, horizon=2,
            version=version or SL.label_version(SL.BASIS_EXCESS),
            features={"momentum": 0.1},
        )

    def _assert_refused(self, record, expected_reason, feature=None):
        import learning_dataset as LD

        verdict = SL.verified_evidence(record)
        self.assertFalse(verdict["verified"], record)
        self.assertTrue(verdict["claimed_verified"], record)
        self.assertEqual(expected_reason, verdict["reason"], record)
        self.assertEqual(SL.STATUS_UNAVAILABLE, verdict["status"], record)
        self.assertIsNone(verdict["label_score"], record)
        self.assertIsNone(verdict["basis"], record)

        # 桥与 assembler 必须给出同一个判定（同一个 validator）。
        evidence = LD.selection_label_evidence(record)
        self.assertFalse(evidence["verified"], record)
        self.assertEqual(LD.PIT_UNPROVEN, evidence["pit_status"], record)
        self.assertIsNone(evidence["label_score"], record)

        feature = feature if feature is not None else self._feature()
        for require in (True, False):
            assembled = SL.assemble_learning_rows([feature], [record],
                                                 require_verified=require)
            self.assertEqual([], assembled["rows"], (record, require))
            self.assertEqual(1, assembled["report"]["excluded"]["invalid_verified_evidence"],
                             (record, require))
        # 拒绝的原因是 basis 契约，而不是顺带被身份错配兜住。
        outcome = LD.selection_outcome_rows([record], require_verified=True)
        self.assertEqual(0, outcome["report"]["accepted_rows"], record)

    def test_p31_version_basis_conflicts_are_refused_not_repaired(self):
        """P31：version/basis 冲突、缺失、不可解析 → fail closed，不自动修正。"""
        good = SL.label_record(_excess_label())
        self.assertTrue(SL.verified_evidence(good)["verified"])

        # case 1：version=excess-return，basis=raw-return。身份键未变，
        # 因此唯一能拦住它的就是 basis/version 一致性校验。
        case_1 = {**good, "basis": SL.BASIS_RAW}
        before = dict(case_1)
        self._assert_refused(case_1, SL.EVIDENCE_BASIS_VERSION_MISMATCH)
        self.assertEqual(before, case_1, "拒绝时不得就地修正记录")
        assembled = SL.assemble_learning_rows([self._feature()], [case_1])
        self.assertEqual(0, assembled["report"]["excluded"]["sample_key_mismatch"])

        # case 2：version=raw-return，basis=excess-return。version 变了，
        # 身份也要跟着重算 —— 否则会被 sample_key 校验先拦下，测不到 basis 契约。
        raw_version = SL.label_version(SL.BASIS_RAW)
        case_2 = {
            **good,
            "label_version": raw_version,
            "basis": SL.BASIS_EXCESS,
            "sample_key": SL.sample_identity(good["code"], good["decision_at"],
                                             good["horizon"], raw_version),
        }
        before = dict(case_2)
        self._assert_refused(
            case_2, SL.EVIDENCE_BASIS_VERSION_MISMATCH,
            feature=self._feature(version=raw_version),
        )
        self.assertEqual(before, case_2, "拒绝时不得就地修正记录")
        assembled = SL.assemble_learning_rows(
            [self._feature(version=raw_version)], [case_2]
        )
        self.assertEqual(0, assembled["report"]["excluded"]["sample_key_mismatch"])

        # 缺失 basis：新契约要求持久化，缺了就拒绝，绝不 ``or DEFAULT_BASIS``。
        missing_basis = {key: value for key, value in good.items() if key != "basis"}
        self._assert_refused(missing_basis, SL.EVIDENCE_BASIS_MISSING)
        # 空串与缺失同等对待。
        self._assert_refused({**good, "basis": ""}, SL.EVIDENCE_BASIS_MISSING)

        # 版本不可解析（未知 v2 / 未知口径）→ 无法判断该用哪条规则，同样拒绝。
        for bad_version in ("selection-label-v2/raw-return",
                            "selection-label-v1/nonsense", "raw-return"):
            record = {
                **good,
                "label_version": bad_version,
                "sample_key": SL.sample_identity(good["code"], good["decision_at"],
                                                 good["horizon"], bad_version),
            }
            self._assert_refused(
                record, SL.EVIDENCE_VERSION_UNSUPPORTED,
                feature=self._feature(version=bad_version),
            )

    def test_p32_basis_is_a_label_only_field(self):
        """P32：``basis`` 只能出现在标签侧；特征记录携带它会被结构性拒绝。"""
        self.assertIn("basis", SL.LABEL_RECORD_FIELDS)
        self.assertNotIn("basis", SL.FEATURE_RECORD_FIELDS)
        self.assertIn("basis", SL.LABEL_ONLY_FIELDS)
        with self.assertRaises(ValueError):
            SL.assert_feature_record({
                "code": "600001", "decision_at": T_AFTER_CLOSE, "horizon": 2,
                "label_version": SL.label_version(SL.BASIS_EXCESS),
                "features": {}, "basis": SL.BASIS_EXCESS,
            })
        # 版本→口径的解析只有一处，且对坏输入返回 None（不猜）。
        self.assertEqual(SL.BASIS_EXCESS,
                         SL.basis_from_label_version("selection-label-v1/excess-return"))
        self.assertEqual(SL.BASIS_RAW,
                         SL.basis_from_label_version("selection-label-v1/raw-return"))
        for bad in ("", None, "selection-label-v1", "selection-label-v1/",
                    "selection-label-v1/other", "selection-label-v2/raw-return"):
            self.assertIsNone(SL.basis_from_label_version(bad), bad)


if __name__ == "__main__":
    unittest.main()
