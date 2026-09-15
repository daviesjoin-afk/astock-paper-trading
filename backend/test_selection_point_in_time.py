# -*- coding: utf-8 -*-
"""P1–P12：选股 / 回放的 strict point-in-time 边界。

本文件只验证一件事：**历史时点 T 的选股结果，只允许来自 T 当时真实可获得的信息。**

模式边界（与 ``backend/point_in_time.py`` 一致）：

* ``asof is None``   → live compatibility mode，现有实时路径行为必须逐值不变（P10）。
* ``asof is not None`` → strict PIT historical mode，未知可用性 = 不可用（P1–P9、P11、P12）。

P12 是端到端泄漏哨兵：它不走"每个 helper 各自看起来正确"，而是驱动**真实生产因子
链路**（``compute_price_factors`` → ``compute_fundamental_factors`` →
``compute_sentiment_factors`` → ``strategies.build_factor_table``），断言未来信息
的存在与否**不改变** T 时点的选股打分，并用 live 模式做正向对照证明哨兵数据本身有杀伤力。
"""
import datetime as dt
import math
import os
import sys
import unittest
from unittest import mock

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import factors as F  # noqa: E402
import point_in_time as PIT  # noqa: E402
import strategies as S  # noqa: E402
import universe as U  # noqa: E402

# ── 固定常量 ────────────────────────────────────────────────────────────────

ASOF = "2024-06-14"
ASOF_DAY = dt.date(2024, 6, 14)
ASOF_INTRADAY = "2024-06-14T10:00:00+08:00"
HISTORY_START = dt.date(2024, 1, 2)

TARGET = "600519"
CONTROLS = ["600001", "600002", "600003", "600004", "600005", "600006",
            "600007", "600008", "600009", "600010", "600011", "600012"]

#: 故意极端到"如果泄漏就必然看得出来"的未来/当前数据。
LEAK_SENTINEL = {
    "future_close_multiple": 10.0,      # 未来收益 +900%
    "future_roe": 999.0,                # 未来 ROE 999%
    "current_pe": 1.0,                  # 当前 PE=1（极度"便宜"）
    "current_pb": 0.01,
    "future_hot_rank": 1,               # 未来热榜第 1
    "future_industry": "最热门行业",
    "future_observed_at": "2024-07-01T09:30:00+08:00",
    "future_published_at": "2024-08-30",
    "future_report_period": "2024-06-30",
}

#: P10 live 兼容锚点：改造前实测的 live 输出（见 PR body 的 live/compat 说明）。
LIVE_CODE = "600000"
LIVE_LAST = dt.date(2026, 6, 12)
LIVE_DAYS = 120
LIVE_GOLDEN = {
    "price": 16.0,
    "last_date": "2026-06-12",
    "mom5": 0.022556390977443552,
    "mom20": 0.09677419354838701,
    "mom60": 0.3599999999999999,
    "vol20": 0.002121366883662308,
    "vol_surge": 1.195729537366548,
    "flow_proxy": 1.195729537366548,
    "rsi14": 100.0,
    "macd_dif": 0.49284551305477464,
    "ma5": 15.858823529411765,
    "ma20": 15.329411764705885,
    "ma60": 13.917647058823528,
}


# ── 构造工具 ────────────────────────────────────────────────────────────────

def weekdays(start, end):
    day, out = start, []
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def daily_frame(rows):
    """``[(date, close, amount)]`` → naive-midnight 索引 DataFrame（生产解析器口径）。"""
    index = pd.DatetimeIndex([dt.datetime.combine(day, dt.time(0, 0)) for day, _c, _a in rows])
    return pd.DataFrame(
        {
            "open": [close - 0.05 for _d, close, _a in rows],
            "high": [close + 0.10 for _d, close, _a in rows],
            "low": [close - 0.10 for _d, close, _a in rows],
            "close": [close for _d, close, _a in rows],
            "volume": [1_000_000.0 for _ in rows],
            "amount": [amount for _d, _c, amount in rows],
        },
        index=index,
    )


def series(start, end, base, step=0.01, amount=1.0e8):
    return [(day, base + i * step, amount + i * 1.0e4)
            for i, day in enumerate(weekdays(start, end))]


def live_kline():
    """P10 的 live 锚点数据集：与捕获 golden 时逐位一致。"""
    index = pd.DatetimeIndex(
        [LIVE_LAST - dt.timedelta(days=LIVE_DAYS - 1 - i) for i in range(LIVE_DAYS)])
    index = pd.DatetimeIndex([day for day in index if day.weekday() < 5])
    n = len(index)
    close = np.linspace(10.0, 16.0, n)
    return {
        LIVE_CODE: pd.DataFrame(
            {
                "open": close - 0.05,
                "high": close + 0.15,
                "low": close - 0.2,
                "close": close,
                "volume": np.full(n, 1_000_000.0),
                "amount": np.linspace(1.0e8, 2.0e8, n),
            },
            index=index,
        )
    }


def finance_record(**extra):
    record = {
        "report_date": "2024-03-31",
        "report_published_at": "2024-04-28",
        "roe": 5.0, "rev_yoy": 4.0, "profit_yoy": 6.0,
        "net_profit": 1.0e8, "eps": 0.5, "bps": 5.0,
    }
    record.update(extra)
    return record


def snapshot_row(code, **extra):
    row = {
        "code": code, "name": "样本%s" % code, "industry": "银行",
        "pe": 8.0, "pb": 0.9, "pct": 1.0,
        "mktcap": 1.0e10, "float_cap": 8.0e9,
        "main_net": 1.0e6, "super_net": 2.0e5, "turnover": 2.0,
    }
    row.update(extra)
    return row


class PitSafeManifest(dict):
    """任何 code 都有**可证明** PIT-safe 复权口径的 manifest（不复权源）。

    strict 模式下"复权口径不可证明"会让该 code 的价格因子整行被剔除，
    那会掩盖其它 PIT 断言。所以默认给测试数据一套可证明的来源；
    需要"不可证明"场景的用例向它塞一个显式条目即可覆盖默认值。
    """

    def get(self, key, default=None):
        if key in self:
            return dict.__getitem__(self, key)
        return {"source": "eastmoney", "adjustment": "none"}


def pit_safe_manifest():
    return PitSafeManifest()


class PointInTimeBase(unittest.TestCase):
    """所有因子调用都 patch 掉 manifest，避免依赖本机 data_cache。"""

    def setUp(self):
        patcher = mock.patch.object(F.dfc, "get_kline_manifest",
                                    return_value=pit_safe_manifest())
        patcher.start()
        self.addCleanup(patcher.stop)
        hot = mock.patch.object(F.dfc, "fetch_hot_rank", return_value=[])
        hot.start()
        self.addCleanup(hot.stop)

    # ── 组合真实生产链路 ──
    def factor_table(self, klines, snapshot, finance, asof, sentiment=None):
        price_f = F.compute_price_factors(klines, asof=asof)
        fund_f = F.compute_fundamental_factors(snapshot, finance, asof=asof)
        if sentiment is None:
            sentiment = F.compute_sentiment_factors(set(klines), asof=asof)
        return S.build_factor_table(price_f, fund_f, sentiment), price_f, fund_f

    # ── 哨兵数据集 ──
    def sentinel_inputs(self, *, with_future):
        """历史选股输入。``with_future=True`` 时额外塞入极端未来数据。

        未来数据一律对 TARGET 有利，且刻意做成"只要泄漏就必然改变打分"。
        """
        future_days = weekdays(dt.date(2024, 6, 17), dt.date(2024, 6, 28))
        klines = {}
        for code in [TARGET] + CONTROLS:
            base = 20.0 if code == TARGET else 10.0 + int(code[-2:]) * 0.1
            rows = series(HISTORY_START, ASOF_DAY, base)
            if with_future and code == TARGET:
                last = rows[-1][1]
                rows = rows + [
                    (day, last * LEAK_SENTINEL["future_close_multiple"], 9.0e9)
                    for day in future_days
                ]
            klines[code] = daily_frame(rows)

        snapshot = [snapshot_row(code) for code in CONTROLS]
        target_snapshot = snapshot_row(TARGET)
        if with_future:
            # "当前 PE / 当前行业 / 未来观测时点"三种泄漏一起塞进去
            target_snapshot.update({
                "pe": LEAK_SENTINEL["current_pe"],
                "pb": LEAK_SENTINEL["current_pb"],
                "industry": LEAK_SENTINEL["future_industry"],
                "observed_at": LEAK_SENTINEL["future_observed_at"],
                "main_net": 9.0e9, "super_net": 9.0e9, "turnover": 99.0,
            })
        snapshot.append(target_snapshot)

        data = {code: finance_record() for code in CONTROLS}
        target_finance = finance_record()
        if with_future:
            # 未来年报：极端值 + 未来披露时间。必须整条被判 future 而不可见。
            target_finance.update({
                "annual_net_profit": LEAK_SENTINEL["future_roe"] * 1.0e6,
                "annual_report_date": LEAK_SENTINEL["future_report_period"],
                "annual_report_published_at": LEAK_SENTINEL["future_published_at"],
            })
        data[TARGET] = target_finance
        return klines, snapshot, {"data": data}

    def sentinel_hot_rank(self):
        return [{"code": TARGET, "rank": LEAK_SENTINEL["future_hot_rank"], "rank_chg": 200}]


# ─────────────────────────────────────────────────────────────────────────────
# P1–P2 价格 / K 线
# ─────────────────────────────────────────────────────────────────────────────

class PricePointInTimeTests(PointInTimeBase):
    def test_p1_future_bars_never_enter_any_price_factor(self):
        """asof=T 之后（含 T+1）的 K 线不得影响任何价格因子。"""
        honest = {TARGET: daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))}
        future_days = weekdays(dt.date(2024, 6, 17), dt.date(2024, 6, 21))
        polluted = dict(honest)
        rows = series(HISTORY_START, ASOF_DAY, 20.0)
        polluted[TARGET] = daily_frame(
            rows + [(day, rows[-1][1] * 10.0, 9.0e9) for day in future_days])

        clean = F.compute_price_factors(honest, asof=ASOF)
        dirty = F.compute_price_factors(polluted, asof=ASOF)

        self.assertEqual("2024-06-14", clean.loc[TARGET, "last_date"])
        pd.testing.assert_frame_equal(clean, dirty)
        self.assertEqual(0, clean.attrs["pit"]["bars_dropped_future"])
        self.assertEqual(5, dirty.attrs["pit"]["bars_dropped_future"])

    def test_p2_intraday_asof_cannot_see_the_unfinished_daily_bar(self):
        """asof=T 10:00 不得使用 T 当日那根完整收盘日线。"""
        klines = {TARGET: daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))}

        intraday = F.compute_price_factors(klines, asof=ASOF_INTRADAY)
        date_only = F.compute_price_factors(klines, asof=ASOF)

        self.assertEqual("2024-06-13", intraday.loc[TARGET, "last_date"])
        self.assertNotEqual("2024-06-14", intraday.loc[TARGET, "last_date"])
        # date-only cutoff 表示"该交易日结束"，因此当日 bar 可见
        self.assertEqual("2024-06-14", date_only.loc[TARGET, "last_date"])

    def test_p2b_intraday_boundary_is_market_close(self):
        """收盘 15:00 是分界：14:59 不可见，15:00 可见。"""
        klines = {TARGET: daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))}
        before = F.compute_price_factors(klines, asof="2024-06-14T14:59:59+08:00")
        at_close = F.compute_price_factors(klines, asof="2024-06-14T15:00:00+08:00")
        self.assertEqual("2024-06-13", before.loc[TARGET, "last_date"])
        self.assertEqual("2024-06-14", at_close.loc[TARGET, "last_date"])

    def test_p2c_unreadable_index_fails_closed_in_history(self):
        """索引不可解释时该票直接不参与，而不是"猜一个日期"。"""
        frame = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        frame.index = pd.DatetimeIndex([None] * len(frame))
        out = F.compute_price_factors({TARGET: frame}, asof=ASOF)
        self.assertTrue(out.empty)
        self.assertEqual(len(frame), out.attrs["pit"]["bars_unreadable_index"])

    def test_p2d_unparseable_asof_fails_closed(self):
        """asof 解析不了就不是合法的回放依据：整轮不产出任何价格因子。"""
        klines = {TARGET: daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))}
        out = F.compute_price_factors(klines, asof="not-a-date")
        self.assertTrue(out.empty)

    def test_p2e_adjustment_provenance_is_explicit(self):
        """复权口径必须显式 provenance：证明不了就**不产出价格因子**。"""
        klines = {TARGET: daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))}
        manifest = {
            "raw": {"source": "sina", "adjustment": "none"},
            "qfq_before_asof": {"source": "tencent", "adjustment": "qfq",
                                "updated_at": "2024-01-10 09:00:00"},
            "qfq_after_asof": {"source": "tencent", "adjustment": "qfq",
                               "updated_at": "2026-09-15 09:00:00"},
            "unknown": {"source": "unknown", "adjustment": "unknown"},
        }
        with mock.patch.object(F.dfc, "get_kline_manifest", return_value=manifest):
            for code, safe in (("raw", True), ("qfq_before_asof", True),
                               ("qfq_after_asof", False), ("unknown", False)):
                out = F.compute_price_factors({code: klines[TARGET]}, asof=ASOF)
                if safe:
                    self.assertEqual(True, bool(out.loc[code, "adjustment_pit_safe"]), code)
                    self.assertEqual(True, bool(out.loc[code, "price_pit_safe"]), code)
                else:
                    # 不可证明 → 该 code 的价格 alpha **整行不可用**（不是标记后照用）
                    self.assertNotIn(code, out.index)
                    self.assertEqual(1, out.attrs["pit"]["codes_excluded_unproven_adjustment"])
                    self.assertFalse(out.attrs["pit"]["price_pit_safe"])
        # 全票都不可证明复权口径时，结果必须为空表（fail closed）
        with mock.patch.object(F.dfc, "get_kline_manifest",
                               return_value={"qfq_after_asof": manifest["qfq_after_asof"]}):
            out = F.compute_price_factors({"qfq_after_asof": klines[TARGET]}, asof=ASOF)
        self.assertTrue(out.empty)
        self.assertFalse(out.attrs["pit"]["price_pit_safe"])
        self.assertEqual(1, out.attrs["pit"]["codes_without_proven_adjustment"])

    def test_p20_a_malformed_index_code_is_excluded_per_code(self):
        """索引里混入无法解析的日期 → 该 code 整条排除，其它 code 照常计算。"""
        good = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        bad = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        labels = list(bad.index)
        labels[5] = "not-a-date"
        bad.index = pd.Index(labels)

        out = F.compute_price_factors({"bad": bad, "good": good}, asof=ASOF)

        self.assertNotIn("bad", out.index)
        self.assertIn("good", out.index)
        self.assertEqual("2024-06-14", out.loc["good", "last_date"])
        self.assertEqual(1, out.attrs["pit"]["codes_unreadable_index"])
        self.assertGreaterEqual(out.attrs["pit"]["bars_unreadable_index"], 1)

    def test_p20b_a_wholly_unparseable_index_is_excluded_without_raising(self):
        good = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        bad = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        bad.index = pd.Index(["nope"] * len(bad))

        out = F.compute_price_factors({"bad": bad, "good": good}, asof=ASOF)

        self.assertNotIn("bad", out.index)
        self.assertIn("good", out.index)
        self.assertEqual(1, out.attrs["pit"]["codes_unreadable_index"])
        self.assertEqual(len(bad), out.attrs["pit"]["bars_unreadable_index"])

    def test_p20c_a_raising_index_parser_is_contained_per_code(self):
        """即使索引解析本身抛异常，也必须在 per-code 边界内被吸收，不终止整轮。"""
        good = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        bad = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        bad.index = pd.Index(["RAISE"] * len(bad))
        seen = {"raised": 0}
        real = F._bar_availability

        def flaky(index):
            if len(index) and str(index[0]) == "RAISE":
                seen["raised"] += 1
                raise ValueError("simulated unreadable index")
            return real(index)

        with mock.patch.object(F, "_bar_availability", side_effect=flaky):
            out = F.compute_price_factors({"bad": bad, "good": good}, asof=ASOF)

        self.assertEqual(1, seen["raised"])
        self.assertNotIn("bad", out.index)
        self.assertIn("good", out.index)
        self.assertEqual(1, out.attrs["pit"]["codes_unreadable_index"])
        self.assertEqual(len(bad), out.attrs["pit"]["bars_unreadable_index"])

    def test_p20d_live_mode_does_not_apply_the_index_gate(self):
        """live 兼容：坏索引不触发新的 PIT 日期轴门禁（strict 才校验）。"""
        bad = daily_frame(series(HISTORY_START, ASOF_DAY, 20.0))
        bad.index = pd.Index(["nope"] * len(bad))

        out = F.compute_price_factors({"bad": bad})

        self.assertEqual("live", out.attrs["pit"]["mode"])
        self.assertEqual(0, out.attrs["pit"]["codes_unreadable_index"])
        self.assertEqual(0, out.attrs["pit"]["bars_unreadable_index"])

    def test_p15_unsafe_adjustment_price_evidence_cannot_reach_alpha(self):
        """未经 PIT 证明的 qfq 序列：即使人为造出 +900% 动量，也不得影响选股。

        断言口径与"这条 unsafe price evidence 完全不存在"时一致：
        截面里其它票的分数与排名逐值不变，且该 code 不在结果里。
        """
        codes = CONTROLS[:6]
        base = {code: daily_frame(series(HISTORY_START, ASOF_DAY, 10.0 + i * 0.1))
                for i, code in enumerate(codes)}

        # 伪造一条"按今天公司行为重算过"的历史序列，尾段暴涨制造极端动量
        unsafe_rows = series(HISTORY_START, ASOF_DAY, 10.0)
        unsafe_rows[-1] = (unsafe_rows[-1][0], unsafe_rows[-1][1] * 10.0, 9.0e9)
        unsafe = daily_frame(unsafe_rows)

        unsafe_manifest = PitSafeManifest()
        unsafe_manifest["qfq_after_asof"] = {
            "source": "tencent", "adjustment": "qfq",
            "updated_at": "2026-09-15 09:00:00",
        }

        snapshot = [snapshot_row(code, observed_at=ASOF) for code in codes]
        snapshot.append(snapshot_row("qfq_after_asof", observed_at=ASOF))
        finance = {"data": {row["code"]: finance_record() for row in snapshot}}

        def run(klines):
            price_f = F.compute_price_factors(klines, asof=ASOF)
            fund_f = F.compute_fundamental_factors(snapshot, finance, asof=ASOF)
            return S.build_factor_table(price_f, fund_f, {}), price_f

        with mock.patch.object(F.dfc, "get_kline_manifest", return_value=unsafe_manifest):
            with_unsafe, price_with = run({**base, "qfq_after_asof": unsafe})
            without_unsafe, price_without = run(dict(base))

        # unsafe 证据完全不存在
        self.assertNotIn("qfq_after_asof", price_with.index)
        self.assertEqual(1, price_with.attrs["pit"]["codes_excluded_unproven_adjustment"])
        self.assertFalse(price_with.attrs["pit"]["price_pit_safe"])
        # 截面分数 / 排名逐值一致（含 z-score，不会被"极端动量"带偏）
        pd.testing.assert_frame_equal(with_unsafe, without_unsafe, check_exact=True)
        pd.testing.assert_frame_equal(price_with, price_without, check_exact=True)

    def test_p15b_live_mode_still_keeps_the_same_series(self):
        """反向对照：live 模式下该序列照常参与（证明剔除只发生在 strict history）。"""
        rows = series(HISTORY_START, ASOF_DAY, 10.0)
        rows[-1] = (rows[-1][0], rows[-1][1] * 10.0, 9.0e9)
        manifest = {"code": {"source": "tencent", "adjustment": "qfq",
                             "updated_at": "2026-09-15 09:00:00"}}
        with mock.patch.object(F.dfc, "get_kline_manifest", return_value=manifest):
            live = F.compute_price_factors({"code": daily_frame(rows)})
        self.assertIn("code", live.index)
        self.assertFalse(bool(live.loc["code", "adjustment_pit_safe"]))
        self.assertFalse(bool(live.loc["code", "price_pit_safe"]))


# ─────────────────────────────────────────────────────────────────────────────
# P3–P4 财务披露
# ─────────────────────────────────────────────────────────────────────────────

class FinancialPointInTimeTests(PointInTimeBase):
    def _roe(self, record, asof):
        out = F.compute_fundamental_factors(
            [snapshot_row(TARGET, observed_at=asof)], {"data": {TARGET: record}}, asof=asof)
        return out.loc[TARGET, "roe"], out.loc[TARGET, "profit_source"]

    def test_p3_publication_date_decides_visibility_not_report_period(self):
        """period < asof 但 published_at > asof → 财务值不可见。"""
        record = finance_record(report_date="2024-03-31", report_published_at="2024-04-28")
        hidden, source = self._roe(record, "2024-04-20")
        self.assertTrue(pd.isna(hidden))
        self.assertEqual("future", source)

        shown, source2 = self._roe(record, "2024-04-29")
        self.assertEqual(5.0, shown)
        self.assertEqual("reported", source2)

    def test_p3b_report_period_is_never_substituted_for_publication(self):
        """缺披露时间戳时，绝不退化成 report_date <= asof → visible。"""
        out = F.compute_fundamental_factors(
            [snapshot_row(TARGET, observed_at=ASOF)],
            {"data": {TARGET: {"report_date": "2024-03-31", "roe": 5.0}}},
            asof=ASOF,
        )
        self.assertTrue(pd.isna(out.loc[TARGET, "roe"]))
        self.assertEqual("shadow", out.loc[TARGET, "profit_source"])

    def test_p4_missing_publication_is_invisible_in_history(self):
        record = finance_record()
        record.pop("report_published_at")
        visible_live, _ = self._roe(record, None)
        hidden_history, _ = self._roe(record, ASOF)
        # live 兼容：值保留（shadow）；strict history：必须缺失
        self.assertEqual(5.0, visible_live)
        self.assertTrue(pd.isna(hidden_history))


# ─────────────────────────────────────────────────────────────────────────────
# P5–P8 snapshot 动态字段 / 行业分类
# ─────────────────────────────────────────────────────────────────────────────

class SnapshotPointInTimeTests(PointInTimeBase):
    FINANCE = {"data": {TARGET: finance_record()}}

    def _row(self, **extra):
        out = F.compute_fundamental_factors([snapshot_row(TARGET, **extra)],
                                            self.FINANCE, asof=ASOF)
        return out.loc[TARGET]

    def test_p5_untimestamped_snapshot_fields_are_unavailable_in_history(self):
        """历史 asof + 无 observed_at 的当前 PE → PE 必须不可用。"""
        row = self._row()
        self.assertTrue(pd.isna(row["pe"]))
        self.assertTrue(pd.isna(row["pb"]))
        self.assertTrue(pd.isna(row["mktcap"]))
        self.assertTrue(pd.isna(row["float_cap"]))
        self.assertTrue(pd.isna(row["main_net"]))
        self.assertTrue(pd.isna(row["super_net"]))
        self.assertTrue(pd.isna(row["turnover"]))
        self.assertTrue(pd.isna(row["pct_today"]))
        self.assertEqual("availability_unknown", row["snapshot_pit_reason"])

    def test_p6_observed_before_asof_is_usable(self):
        row = self._row(observed_at="2024-06-14T09:31:00+08:00")
        self.assertEqual(8.0, row["pe"])
        self.assertEqual(0.9, row["pb"])
        self.assertEqual(1.0e10, row["mktcap"])
        self.assertEqual(2.0, row["turnover"])
        self.assertEqual("visible", row["snapshot_pit_reason"])

    def test_p6b_quote_at_is_accepted_as_availability_evidence(self):
        row = self._row(quote_at="2024-06-14T09:31:00+08:00")
        self.assertEqual(8.0, row["pe"])

    def test_p7_snapshot_observed_after_asof_is_entirely_invisible(self):
        row = self._row(observed_at="2024-07-01T09:30:00+08:00")
        for column in ("pe", "pb", "mktcap", "float_cap", "main_net",
                       "super_net", "turnover", "pct_today"):
            self.assertTrue(pd.isna(row[column]), column)
        self.assertEqual("future", row["snapshot_pit_reason"])

    def test_p8_current_industry_cannot_rewrite_history(self):
        """当前行业值没有 historical effective metadata → 历史模式不得使用。"""
        row = self._row()
        self.assertIsNone(row["industry"])
        self.assertEqual("availability_unknown", row["classification_pit_reason"])

    def test_p8b_effective_window_must_cover_asof(self):
        future_start = self._row(industry="新能源",
                                 industry_effective_from="2026-01-01")
        self.assertIsNone(future_start["industry"])
        self.assertEqual("future", future_start["classification_pit_reason"])

        covered = self._row(industry="银行", industry_effective_from="2020-01-01",
                            industry_effective_to="2025-01-01")
        self.assertEqual("银行", covered["industry"])
        self.assertEqual("effective_window", covered["classification_basis"])

        expired = self._row(industry="银行", industry_effective_from="2020-01-01",
                            industry_effective_to="2024-06-14")
        self.assertIsNone(expired["industry"])

    def test_p8c_industry_observed_before_asof_is_usable(self):
        row = self._row(industry="银行", observed_at="2024-05-01T15:00:00+08:00")
        self.assertEqual("银行", row["industry"])
        self.assertEqual("observed_at", row["classification_basis"])

    def test_p8d_live_mode_keeps_the_current_industry(self):
        """live compatibility：无时间戳也照常使用当前行业/PE。"""
        out = F.compute_fundamental_factors([snapshot_row(TARGET)], self.FINANCE, asof=None)
        self.assertEqual("银行", out.loc[TARGET, "industry"])
        self.assertEqual(8.0, out.loc[TARGET, "pe"])
        self.assertEqual(1.0e10, out.loc[TARGET, "mktcap"])
        self.assertEqual("live", out.attrs["pit"]["mode"])


# ─────────────────────────────────────────────────────────────────────────────
# P9 历史 universe
# ─────────────────────────────────────────────────────────────────────────────

class UniversePointInTimeTests(unittest.TestCase):
    def test_p9_listing_date_gate(self):
        rows = [{"code": "600001", "list_date": "2025-01-01"},
                {"code": "600002", "list_date": "2024-06-14"},
                {"code": "600003", "list_date": "2024-06-17"},
                {"code": "600004", "list_date": "2020-01-01"}]
        result = PIT.universe_asof_members(rows, ASOF)
        kept = {row["code"] for row in result["members"]}
        self.assertEqual({"600002", "600004"}, kept)
        self.assertEqual(2, result["report"]["counts"][PIT.MEMBERSHIP_NOT_LISTED_YET])

    def test_p9b_delisting_boundary(self):
        """退市边界：``asof < delist_date`` 才在市；每行都带**合法 list_date**。

        只有 ``delist_date`` 的行证明不了"asof 时已上市"（见 P9f），所以真正的
        退市边界必须用"上市日 + 退市日"成对的数据来测。
        """
        rows = [
            {"code": "600001", "list_date": "2020-01-01", "delist_date": "2024-06-01"},   # 已退市
            {"code": "600002", "list_date": "2020-01-01", "delist_date": "2024-06-14"},   # asof 当日 → 已退市（asof < delist 才有效）
            {"code": "600003", "list_date": "2020-01-01", "delist_date": "2024-06-17"},   # 仍在市
            {"code": "600004"},                                                            # 无日期 → 未证明
        ]
        result = PIT.universe_asof_members(rows, ASOF)
        kept = [row["code"] for row in result["members"]]
        self.assertEqual(["600003", "600004"], kept)
        self.assertEqual(2, result["report"]["counts"][PIT.MEMBERSHIP_DELISTED])
        boundary = PIT.universe_membership(rows[2], ASOF)
        self.assertEqual("member", boundary["status"])
        self.assertTrue(boundary["proven"])
        self.assertEqual("2020-01-01", boundary["list_date"])
        self.assertEqual("2024-06-17", boundary["delist_date"])
        self.assertEqual("membership_unknown", PIT.universe_membership(rows[3], ASOF)["status"])
        self.assertFalse(PIT.universe_membership(rows[3], ASOF)["proven"])

    def test_p9f_future_delist_date_alone_never_proves_membership(self):
        """只有 future ``delist_date``（缺 ``list_date``）绝不能被判成 member。

        ``delist_date = 2026-01-01`` 只证明"到该日之后不能再持有"，**不能反证**
        asof 时已经上市——真实 ``list_date`` 完全可能是 ``2025-01-01``。
        """
        future_only = {"code": "600009", "delist_date": "2026-01-01"}
        verdict = PIT.universe_membership(future_only, ASOF)
        self.assertEqual("membership_unknown", verdict["status"])
        self.assertFalse(verdict["proven"])
        self.assertIsNone(verdict["list_date"])
        self.assertEqual("2026-01-01", verdict["delist_date"])

        # 反向对照 1：缺 list_date 且无 delist_date → 同样未知（不猜）
        bare = {"code": "600010"}
        bare_verdict = PIT.universe_membership(bare, ASOF)
        self.assertEqual("membership_unknown", bare_verdict["status"])
        self.assertFalse(bare_verdict["proven"])

        # 反向对照 2：非法 list_date + future delist_date → 仍未知（不得用 delist 补证）
        broken = {"code": "600011", "list_date": "not-a-date", "delist_date": "2026-01-01"}
        broken_verdict = PIT.universe_membership(broken, ASOF)
        self.assertEqual("membership_unknown", broken_verdict["status"])
        self.assertFalse(broken_verdict["proven"])

        # 反向对照 3：**已退市**的排除不依赖 list_date（asof >= delist 是安全的排除）
        gone_verdict = PIT.universe_membership({"code": "600012", "delist_date": "2024-06-01"}, ASOF)
        self.assertEqual("delisted", gone_verdict["status"])
        self.assertFalse(gone_verdict["member"])

        # 生产 strict（drop_unproven=True）必须真的把 future-delist-only 剔除
        rows = [future_only, {"code": "600013", "list_date": "2020-01-01"}]
        strict = PIT.universe_asof_members(rows, ASOF, drop_unproven=True)
        self.assertEqual(["600013"], [row["code"] for row in strict["members"]])
        self.assertEqual(1, strict["report"]["unproven"])

    def test_p9c_unproven_membership_is_reported_not_faked(self):
        """低层 API 的兼容默认：保留未证明成分，但**必须**如实报 unproven。

        production strict history 不允许用这个默认值 —— 它显式传
        ``drop_unproven=True``（见 P14 / P16）。
        """
        rows = [{"code": "600001"}, {"code": "600002"}]
        default = PIT.universe_asof_members(rows, ASOF)
        self.assertEqual(2, default["report"]["unproven"])
        self.assertEqual(2, len(default["members"]))
        strict = PIT.universe_asof_members(rows, ASOF, drop_unproven=True)
        self.assertEqual([], strict["members"])
        self.assertEqual(2, strict["report"]["unproven"])
        self.assertEqual(0, strict["report"]["kept"])

    def test_p9d_live_mode_never_filters_the_universe(self):
        rows = [{"code": "600001", "list_date": "2025-01-01"}]
        result = PIT.universe_asof_members(rows, None)
        self.assertEqual(1, len(result["members"]))
        self.assertEqual("live", result["report"]["mode"])

    def test_p9e_universe_module_exposes_the_contract(self):
        """契约挂在选股真正读取的 universe 模块上，而不是只活在测试里。"""
        self.assertTrue(hasattr(U, "asof_members"))
        rows = [{"code": "600001", "list_date": "2025-01-01"},
                {"code": "600002", "list_date": "2020-01-01"}]
        out = U.asof_members(rows, ASOF)
        self.assertEqual(["600002"], [row["code"] for row in out["members"]])


# ─────────────────────────────────────────────────────────────────────────────
# P10 live 兼容性
# ─────────────────────────────────────────────────────────────────────────────

class LiveCompatibilityTests(PointInTimeBase):
    def test_p10_live_price_factors_are_value_identical(self):
        out = F.compute_price_factors(live_kline())
        row = out.loc[LIVE_CODE]
        for key, expected in LIVE_GOLDEN.items():
            if isinstance(expected, str):
                self.assertEqual(expected, row[key], key)
            else:
                self.assertAlmostEqual(expected, float(row[key]), places=12, msg=key)
        self.assertFalse(bool(row["adjustment_warning"]))
        self.assertEqual(1.0, float(row["price_evidence_quality"]))
        self.assertEqual("live", out.attrs["pit"]["mode"])

    def test_p10b_live_fundamental_factors_are_value_identical(self):
        snapshot = [snapshot_row(LIVE_CODE, name="浦发银行")]
        finance = {"data": {LIVE_CODE: finance_record(report_date="2026-03-31",
                                                     report_published_at="2026-04-28")}}
        out = F.compute_fundamental_factors(snapshot, finance)
        row = out.loc[LIVE_CODE]
        self.assertEqual("银行", row["industry"])
        self.assertEqual(8.0, row["pe"])
        self.assertEqual(0.9, row["pb"])
        self.assertEqual(5.0, row["roe"])
        self.assertEqual(4.0, row["rev_yoy"])
        self.assertEqual(6.0, row["profit_yoy"])
        self.assertEqual(1.0e8, row["net_profit"])
        self.assertEqual("reported", row["profit_source"])
        self.assertEqual(1.0e10, row["mktcap"])
        self.assertEqual(2.0, row["turnover"])
        self.assertEqual(1.0, row["pct_today"])
        self.assertEqual("live", out.attrs["pit"]["mode"])

    def test_p10c_live_sentiment_is_value_identical(self):
        hot = [{"code": LIVE_CODE, "rank": 5, "rank_chg": 20}]
        with mock.patch.object(F.dfc, "fetch_hot_rank", return_value=hot) as fetcher:
            out = F.compute_sentiment_factors({LIVE_CODE})
        self.assertEqual(1, fetcher.call_count)
        self.assertEqual({"hot_rank": 5, "rank_chg": 20, "sentiment": 0.616},
                         out[LIVE_CODE])

    def test_p10d_live_snapshot_fields_survive_a_todays_cutoff(self):
        """生产实盘会把"最近完整交易日"当作 asof 传给因子层：实时行必须仍然可用。"""
        snapshot = [snapshot_row(LIVE_CODE,
                                 observed_at="2026-09-15T14:30:00+08:00",
                                 industry_effective_from="2020-01-01")]
        finance = {"data": {LIVE_CODE: finance_record(report_date="2026-06-30",
                                                     report_published_at="2026-08-20")}}
        out = F.compute_fundamental_factors(snapshot, finance, asof="2026-09-15")
        row = out.loc[LIVE_CODE]
        self.assertEqual(8.0, row["pe"])
        self.assertEqual("银行", row["industry"])
        self.assertEqual(5.0, row["roe"])

    def test_p10e_no_silent_fallback_across_modes(self):
        """同一份数据：live 有值、strict history 缺失——两套契约必须清晰分层。"""
        snapshot = [snapshot_row(LIVE_CODE)]
        finance = {"data": {LIVE_CODE: finance_record(report_date="2026-03-31",
                                                     report_published_at="2026-04-28")}}
        live = F.compute_fundamental_factors(snapshot, finance)
        historical = F.compute_fundamental_factors(snapshot, finance, asof="2024-06-14")
        self.assertEqual(8.0, live.loc[LIVE_CODE, "pe"])
        self.assertTrue(pd.isna(historical.loc[LIVE_CODE, "pe"]))
        self.assertEqual("银行", live.loc[LIVE_CODE, "industry"])
        self.assertIsNone(historical.loc[LIVE_CODE, "industry"])


# ─────────────────────────────────────────────────────────────────────────────
# P11 实时情绪
# ─────────────────────────────────────────────────────────────────────────────

class SentimentPointInTimeTests(PointInTimeBase):
    def test_p11_historical_replay_never_touches_the_live_hot_rank_api(self):
        with mock.patch.object(F.dfc, "fetch_hot_rank",
                               return_value=self._hot()) as fetcher:
            out = F.compute_sentiment_factors({TARGET, CONTROLS[0]}, asof=ASOF)
        self.assertEqual({}, out)
        self.assertEqual(0, fetcher.call_count)

    def test_p11b_live_mode_still_uses_the_realtime_rank(self):
        with mock.patch.object(F.dfc, "fetch_hot_rank",
                               return_value=self._hot()) as fetcher:
            out = F.compute_sentiment_factors({TARGET})
        self.assertEqual(1, fetcher.call_count)
        self.assertIn(TARGET, out)

    @staticmethod
    def _hot():
        return [{"code": TARGET, "rank": 1, "rank_chg": 200},
                {"code": CONTROLS[0], "rank": 40, "rank_chg": 5}]


# ─────────────────────────────────────────────────────────────────────────────
# P12 / 十九 端到端泄漏哨兵
# ─────────────────────────────────────────────────────────────────────────────

class LeakSentinelTests(PointInTimeBase):
    SCORE_COLUMNS = ("value", "quality", "mom_short", "mom", "volsurge", "rsi", "flow")

    #: 承载"未来信息是否泄漏"的列。刻意**排除** ``report_date`` /
    #: ``annual_report_date`` / ``*_published_at``：这些是诊断用的报告期回显，
    #: 即使值被隐藏也会保留报告期本身（这是 ``financial_visibility`` 的既有契约），
    #: 因此它们不参与"是否泄漏"的判定。
    COMPARABLE_COLUMNS = (
        "value", "quality", "mom_short", "mom", "volsurge", "rsi", "flow",
        "price", "pct", "turnover", "main_net", "main_pct",
        "pe", "pb", "roe", "rev_yoy", "profit_yoy", "net_profit", "annual_net_profit",
        "industry", "name", "mktcap", "float_cap", "profit_source",
        "mom5_raw", "mom20_raw", "mom60_raw", "vol_surge_raw", "rsi14_raw",
        "value_evidence_quality", "quality_evidence_quality", "flow_evidence_quality",
        "mom_evidence_quality", "mom_short_evidence_quality",
        "volsurge_evidence_quality", "rsi_evidence_quality", "flow_source",
    )

    @staticmethod
    def normalized(value):
        if isinstance(value, np.generic):
            value = value.item()
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return value

    def compare_tables(self, left, right):
        """逐码逐列比较"会不会因为未来数据而改变"的那些列。"""
        columns = [c for c in self.COMPARABLE_COLUMNS
                   if c in left.columns and c in right.columns]
        self.assertGreaterEqual(len(columns), 20, "可比较列太少，哨兵可能是空洞的")
        self.assertEqual(sorted(map(str, left.index)), sorted(map(str, right.index)))
        for code in left.index:
            expected = {c: self.normalized(left.loc[code, c]) for c in columns}
            actual = {c: self.normalized(right.loc[code, c]) for c in columns}
            self.assertEqual(expected, actual, "未来数据改变了 %s 的因子/打分" % code)

    def test_p12_future_data_cannot_change_the_historical_selection_score(self):
        clean = self._run(with_future=False)
        polluted = self._run(with_future=True)

        # 未来行存在与否不能改变 T 时点结果（整张截面上所有承载 alpha 的列逐值一致）
        self.compare_tables(clean[0], polluted[0])

        for column in self.SCORE_COLUMNS:
            self.assertEqual(
                self.normalized(clean[0].loc[TARGET, column]),
                self.normalized(polluted[0].loc[TARGET, column]),
                "未来数据改变了 TARGET 的 %s" % column)

        # 排名也必须不变
        self.assertEqual(clean[0]["mom"].rank(ascending=False).loc[TARGET],
                         polluted[0]["mom"].rank(ascending=False).loc[TARGET])

        # 未来数据确实被识别并丢弃（否则上面的相等可能是"数据根本没读到"）
        self.assertGreater(polluted[1].attrs["pit"]["bars_dropped_future"], 0)
        self.assertEqual("future", polluted[2].loc[TARGET, "snapshot_pit_reason"])

    def test_p12b_live_mode_shows_that_the_sentinel_data_really_bites(self):
        """正向对照：同样数据在 live 模式下**确实**改变打分。

        这条断言是哨兵自身的体检——若未来数据在 live 模式下也毫无影响，
        上面那条"严格模式相等"就可能是空洞的。
        """
        clean = self._run(with_future=False, asof=None)
        polluted = self._run(with_future=True, asof=None)
        changed = any(
            clean[0].loc[TARGET, column] != polluted[0].loc[TARGET, column]
            for column in self.SCORE_COLUMNS)
        self.assertTrue(changed, "哨兵未来数据在 live 模式下未产生任何影响，测不出泄漏")
        self.assertGreater(polluted[1].loc[TARGET, "price"], clean[1].loc[TARGET, "price"])

    def test_p12c_extreme_future_values_never_reach_the_alpha_components(self):
        polluted = self._run(with_future=True)
        table, price_f, fund_f = polluted
        # 未来 +900% 的收盘价不得进入 T 时点的价格因子
        self.assertEqual("2024-06-14", price_f.loc[TARGET, "last_date"])
        # 未来 ROE 999% 不得进入质量因子
        self.assertTrue(pd.isna(fund_f.loc[TARGET, "annual_net_profit"]))
        self.assertEqual("future", fund_f.loc[TARGET, "classification_pit_reason"])
        # 当前 PE=1（极"便宜"）不得把 value 分抬高
        self.assertTrue(pd.isna(fund_f.loc[TARGET, "pe"]))
        self.assertTrue(pd.isna(fund_f.loc[TARGET, "pb"]))
        self.assertIsNone(fund_f.loc[TARGET, "industry"])
        self.assertNotEqual(LEAK_SENTINEL["future_industry"], fund_f.loc[TARGET, "industry"])
        # value 分只能来自"可见"的 pe/pb；两者都不可见时它必须缺失，而不是变成 0
        # （用 0 填会把"不可用"伪装成"估值中性"，等于让未来数据以另一种形式进入打分）
        self.assertTrue(pd.isna(table.loc[TARGET, "value"]))
        self.assertEqual(0.0, float(table.loc[TARGET, "value_evidence_quality"]))

    def test_p12f_a_timestamped_snapshot_does_enter_the_score(self):
        """反向对照：给 T 时点合法的观测时点，PE 就必须**真的**进入打分。

        否则上面那些"未来数据没影响"可能只是因为 PE 通道被永久关闭，
        而不是因为它是按 decision_asof 正确判定的。
        """
        klines, snapshot, finance = self.sentinel_inputs(with_future=False)
        for row in snapshot:
            if row["code"] == TARGET:
                row["observed_at"] = "2024-06-14T09:31:00+08:00"
                row["industry_effective_from"] = "2020-01-01"
        table, price_f, fund_f = self.factor_table(klines, snapshot, finance, ASOF)

        self.assertEqual(8.0, fund_f.loc[TARGET, "pe"])
        self.assertEqual("银行", fund_f.loc[TARGET, "industry"])
        self.assertFalse(pd.isna(fund_f.loc[TARGET, "pe"]))
        self.assertFalse(pd.isna(table.loc[TARGET, "value"]))
        self.assertGreater(float(table.loc[TARGET, "value_evidence_quality"]), 0.0)

    def test_p12d_live_hot_rank_cannot_enter_a_historical_run(self):
        klines, snapshot, finance = self.sentinel_inputs(with_future=True)
        with mock.patch.object(F.dfc, "fetch_hot_rank",
                               return_value=self.sentinel_hot_rank()) as fetcher:
            sentiment = F.compute_sentiment_factors(set(klines), asof=ASOF)
        self.assertEqual({}, sentiment)
        self.assertEqual(0, fetcher.call_count)

    def test_p12e_provenance_answers_per_data_class(self):
        _, price_f, fund_f = self._run(with_future=False)
        provenance = F.pit_provenance(price_f, fund_f, {}, asof=ASOF)
        self.assertEqual("strict", provenance["mode"])
        self.assertEqual(ASOF + "T23:59:59+08:00", provenance["decision_asof"])
        for flag in ("price_pit_safe", "financial_pit_safe", "snapshot_pit_safe",
                     "classification_pit_safe", "sentiment_pit_safe"):
            self.assertIn(flag, provenance)
        # 无 observed_at / 无生效区间的 snapshot 与行业 → 必须明确 False，而不是被模糊化
        self.assertFalse(provenance["snapshot_pit_safe"])
        self.assertFalse(provenance["classification_pit_safe"])
        self.assertTrue(provenance["sentiment_pit_safe"])

    def _run(self, *, with_future, asof=ASOF):
        klines, snapshot, finance = self.sentinel_inputs(with_future=with_future)
        return self.factor_table(klines, snapshot, finance, asof)


# ─────────────────────────────────────────────────────────────────────────────
# P13–P14 实时盘中截面 与 回放接线
# ─────────────────────────────────────────────────────────────────────────────

class LiveSnapshotCutoffTests(PointInTimeBase):
    """实时选股有两套 cutoff，混用会打掉盘中扫描（P1 review finding）。"""

    TODAY = "2026-09-15"           # 盘中
    COMPLETE_CUTOFF = "2026-09-14"  # 最近一个完整交易日（盘中即昨天）
    NOW = "2026-09-15T10:06:00+08:00"
    QUOTE_AT = "2026-09-15T10:05:00+08:00"
    PRICE_CODE = "600000"

    def _inputs(self):
        days = weekdays(dt.date(2026, 4, 1), dt.date(2026, 9, 14))
        klines = {self.PRICE_CODE: daily_frame(
            [(day, 10.0 + i * 0.01, 1.0e8 + i) for i, day in enumerate(days)])}
        snapshot = [snapshot_row(self.PRICE_CODE, observed_at=self.QUOTE_AT)]
        finance = {"data": {self.PRICE_CODE: finance_record(
            report_date="2026-06-30", report_published_at="2026-08-20")}}
        return klines, snapshot, finance

    def _fund(self, *, declare_snapshot_cutoff):
        _klines, snapshot, finance = self._inputs()
        kwargs = {"asof": self.COMPLETE_CUTOFF}
        if declare_snapshot_cutoff:
            kwargs["snapshot_asof"] = self.NOW
        return F.compute_fundamental_factors(snapshot, finance, **kwargs)

    def test_p13_live_intraday_rows_survive_the_completed_daily_cutoff(self):
        """声明了实时截面决策时点后，当日实时行必须可用。"""
        out = self._fund(declare_snapshot_cutoff=True)
        row = out.loc[self.PRICE_CODE]
        self.assertEqual(8.0, row["pe"])
        self.assertEqual(0.9, row["pb"])
        self.assertEqual("银行", row["industry"])
        self.assertEqual(1.0, row["pct_today"])
        self.assertEqual(2.0, row["turnover"])
        self.assertEqual(1.0e10, row["mktcap"])
        self.assertEqual(1.0e6, row["main_net"])
        self.assertEqual("visible", row["snapshot_pit_reason"])
        self.assertEqual(0, out.attrs["pit"]["rows_with_hidden_snapshot"])
        self.assertEqual("strict", out.attrs["pit"]["snapshot_mode"])
        self.assertEqual(self.NOW, out.attrs["pit"]["snapshot_asof"])

    def test_p13b_without_the_declaration_it_stays_fail_closed(self):
        """不声明 → 沿用 asof，当日实时行被判 future（历史回放的默认行为）。"""
        out = self._fund(declare_snapshot_cutoff=False)
        row = out.loc[self.PRICE_CODE]
        self.assertTrue(pd.isna(row["pe"]))
        self.assertIsNone(row["industry"])
        self.assertTrue(pd.isna(row["pct_today"]))
        self.assertEqual("future", row["snapshot_pit_reason"])

    def test_p13c_live_scan_keeps_the_required_pct_column(self):
        """``build_factor_table`` 的 ``pct`` 取自 ``fund_f``：盘中不能整列丢失。"""
        klines, snapshot, finance = self._inputs()

        def table(declare):
            price_f = F.compute_price_factors(klines, asof=self.COMPLETE_CUTOFF)
            kwargs = {"asof": self.COMPLETE_CUTOFF}
            if declare:
                kwargs["snapshot_asof"] = self.NOW
            fund_f = F.compute_fundamental_factors(snapshot, finance, **kwargs)
            return S.build_factor_table(price_f, fund_f, {})

        declared = table(True)
        undeclared = table(False)
        # table["pct"] 只来自 fund_f（price_f 不产出该列）——盘中一旦整列丢失，
        # 生产扫描的必填列校验就会报警。
        self.assertEqual(1.0, declared.loc[self.PRICE_CODE, "pct"])
        # main_net 在 price 缺失时回落到 fund，同样会被清空
        self.assertEqual(1.0e6, declared.loc[self.PRICE_CODE, "main_net"])
        self.assertTrue(pd.isna(undeclared.loc[self.PRICE_CODE, "pct"]))
        self.assertNotIn("pct", declared.attrs.get("factor_warnings") or [])

    def test_p13d_live_decision_time_is_now_not_the_previous_trading_day(self):
        frozen = dt.datetime(2026, 9, 15, 2, 6, tzinfo=dt.timezone.utc)  # 10:06 +08:00
        moment = PIT.live_decision_time(frozen)
        self.assertEqual("2026-09-15T10:06:00+08:00", moment.isoformat(timespec="seconds"))

    def test_p13e_production_live_call_sites_declare_the_snapshot_cutoff(self):
        """三处生产 live 调用点都必须显式声明实时截面的决策时点。"""
        import inspect

        import main as M
        import paper_trading as PT

        for module, name in ((M, "_select_uncached"), (M, "scanner"),
                             (PT, "_candidate_rows")):
            source = inspect.getsource(getattr(module, name))
            self.assertIn("snapshot_asof=F.live_snapshot_asof()", source,
                          "%s.%s 未声明实时截面决策时点" % (module.__name__, name))


class ReplayWiringTests(PointInTimeBase):
    #: 显式声明"持有完整历史成员史"的合法历史源。
    HISTORICAL_SOURCE = {
        "kind": "historical_archive",
        "historical_membership_complete": True,
        "historical_membership_asof": "2026-12-31",
        "historical_membership_source": "unit_test_archive",
    }

    def test_p14_historical_rebuild_filters_the_universe_by_asof(self):
        """回放重建必须先按 asof 过滤历史成分，且**未证明**的成分不得进入下游。"""
        import paper_trading as PT

        universe = [{"code": "600001", "name": "老股", "list_date": "2020-01-01"},
                    {"code": "600002", "name": "未来上市", "list_date": "2030-01-01"},
                    {"code": "600003", "name": "无日期（未证明）"}]
        seen = {}

        def gate_spy(rows, cutoff):
            seen["codes"] = sorted(str(r.get("code")) for r in rows)
            seen["cutoff"] = cutoff
            return {"passed": False, "reason": "stub"}

        with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                mock.patch.object(PT.U, "load_universe_source",
                                  return_value=self.HISTORICAL_SOURCE), \
                mock.patch.object(PT, "_selection_factor_history_gate", side_effect=gate_spy):
            out = PT._rebuild_selection_factor_cache("2024-06-14")

        self.assertEqual(dt.date(2024, 6, 14), seen["cutoff"])
        # 未来上市 + 未证明 都被剔除；下游（覆盖门禁 / 因子 / selector）只看到已证明的
        self.assertEqual(["600001"], seen["codes"])
        self.assertEqual("blocked", out["status"])
        self.assertIsNotNone(out["universe_membership"])
        self.assertEqual(1, out["universe_membership"]["counts"][PIT.MEMBERSHIP_NOT_LISTED_YET])
        self.assertEqual(1, out["universe_membership"]["unproven"])

    def test_p17_complete_rows_do_not_prove_a_complete_historical_universe(self):
        """每一行都有合法 list_date，但源只是**当前快照** → 仍然 fail closed。

        逐行日期只能证明"这条现存 row 在 asof 属于市场"，不能证明
        `current universe == historical universe at T`：已退市且今天不在快照里的
        证券根本不在输入里，任何逐行校验都发现不了它们缺失。
        """
        import paper_trading as PT

        universe = [{"code": "600001", "name": "甲", "list_date": "2015-01-05"},
                    {"code": "600002", "name": "乙", "list_date": "2016-03-01"}]
        seen = {"gate": False, "klines": [], "price": []}

        def gate_spy(rows, cutoff):
            seen["gate"] = True
            return {"passed": True}

        def kline_spy(code):
            seen["klines"].append(code)
            return None

        def price_spy(*args, **kwargs):
            seen["price"].append(kwargs.get("asof"))
            raise AssertionError("current snapshot 不得进入历史因子计算")

        current_snapshot_source = {
            "kind": "current_snapshot",
            "built_at": "2026-09-15 08:00:00",
            "scope": "all_a_shares",
        }
        with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                mock.patch.object(PT.U, "load_universe_source",
                                  return_value=current_snapshot_source), \
                mock.patch.object(PT, "_selection_factor_history_gate", side_effect=gate_spy), \
                mock.patch.object(PT.dfc, "load_shared_kline", side_effect=kline_spy), \
                mock.patch.object(PT.F, "compute_price_factors", side_effect=price_spy):
            out = PT._rebuild_selection_factor_cache("2024-06-14")

        self.assertEqual("blocked", out["status"])
        self.assertTrue(out["pit_unavailable"])
        self.assertNotIn("refresh_gate", out)
        report = out["universe_membership"]
        self.assertFalse(report["historical_membership_complete"])
        self.assertEqual(PIT.UNIVERSE_SOURCE_NOT_HISTORICAL, report["status"])
        # 逐行成员资格本身是通过的 —— 被挡住的是"源完整性"
        self.assertTrue(report["row_membership_passed"])
        self.assertEqual(2, report["kept"])
        # 没有进入任何候选/因子路径
        self.assertFalse(seen["gate"])
        self.assertEqual([], seen["klines"])
        self.assertEqual([], seen["price"])

    def test_p17b_adding_list_dates_alone_never_unlocks_historical_selection(self):
        """反向对照：单纯补 list_date 不得自动解锁；缺源声明时必须仍被拒。"""
        import paper_trading as PT

        universe = [{"code": "600001", "list_date": "2015-01-05"}]
        for source in (None, {}, {"kind": "current_snapshot"}, "universe.json"):
            with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                    mock.patch.object(PT.U, "load_universe_source", return_value=source):
                out = PT._rebuild_selection_factor_cache("2024-06-14")
            self.assertEqual("blocked", out["status"], source)
            self.assertTrue(out["pit_unavailable"], source)
            self.assertFalse(
                out["universe_membership"]["historical_membership_complete"], source)

    def test_p18_an_explicitly_complete_archive_source_passes_the_gate(self):
        """显式声明 historical-archive 完整的源可以进入后续路径。"""
        import paper_trading as PT

        universe = [{"code": "600001", "list_date": "2015-01-05"},
                    {"code": "600002", "list_date": "2030-01-01"}]
        seen = {}

        def gate_spy(rows, cutoff):
            seen["codes"] = sorted(str(r.get("code")) for r in rows)
            seen["cutoff"] = cutoff
            return {"passed": False, "reason": "coverage-stub"}

        with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                mock.patch.object(PT.U, "load_universe_source",
                                  return_value=self.HISTORICAL_SOURCE), \
                mock.patch.object(PT, "_selection_factor_history_gate", side_effect=gate_spy):
            out = PT._rebuild_selection_factor_cache("2024-06-14")

        # 通过了 PIT 门禁 → 进入覆盖门禁（下一阶段），而不是 pit_unavailable
        self.assertNotIn("pit_unavailable", out)
        self.assertEqual("blocked", out["status"])
        self.assertEqual("coverage-stub", out["refresh_gate"]["reason"])
        self.assertEqual(["600001"], seen["codes"])
        self.assertTrue(out["universe_membership"]["historical_membership_complete"])
        self.assertEqual(PIT.UNIVERSE_SOURCE_OK, out["universe_membership"]["status"])

    def test_p18b_source_gate_rejects_stale_or_incomplete_archives(self):
        universe = [{"code": "600001", "list_date": "2015-01-05"}]
        cases = [
            ({"kind": "historical_archive"}, PIT.UNIVERSE_SOURCE_ASOF_INVALID),
            ({"kind": "historical_archive", "historical_membership_asof": "2024-01-01",
              "historical_membership_complete": True}, PIT.UNIVERSE_SOURCE_ASOF_STALE),
            ({"kind": "historical_archive", "historical_membership_asof": "2026-12-31"},
             PIT.UNIVERSE_SOURCE_INCOMPLETE),
            ({"kind": "historical_archive", "historical_membership_asof": "not-a-date",
              "historical_membership_complete": True}, PIT.UNIVERSE_SOURCE_ASOF_INVALID),
            ("universe.json", PIT.UNIVERSE_SOURCE_UNKNOWN),
            (None, PIT.UNIVERSE_SOURCE_UNKNOWN),
        ]
        for source, expected in cases:
            out = PIT.historical_universe(universe, ASOF, source=source)
            self.assertFalse(out["passed"], source)
            self.assertEqual(expected, out["report"]["status"], source)
            self.assertEqual([], out["members"], source)

    def test_p18c_source_gate_ignores_a_generic_complete_flag(self):
        """只认显式命名的完整性标志：通用 ``complete`` 可能是分页语义。"""
        out = PIT.universe_source_provenance(
            {"kind": "historical_archive", "complete": True,
             "historical_membership_asof": "2026-12-31"}, ASOF)
        self.assertFalse(out["historical_membership_complete"])
        self.assertEqual(PIT.UNIVERSE_SOURCE_INCOMPLETE, out["status"])

    def test_p18d_live_mode_is_never_gated_by_the_source_contract(self):
        rows = [{"code": "600001"}]
        out = PIT.historical_universe(rows, None, source=None)
        self.assertTrue(out["passed"])
        self.assertEqual(rows, out["members"])
        self.assertEqual("live", out["report"]["mode"])

    def test_p14b_live_rebuild_never_filters(self):
        """``asof_date=None``（live）不套用历史成员资格，且不报 unproven。"""
        import paper_trading as PT

        with mock.patch.object(PT.U, "load_universe",
                               return_value=[{"code": "600001"}]), \
                mock.patch.object(PT, "_selection_factor_history_gate",
                                  side_effect=AssertionError("live 不应调用覆盖门禁")):
            out = PT._rebuild_selection_factor_cache(None)
        self.assertIsNone(out.get("universe_membership"))

    def test_p14c_production_rebuild_wires_the_pit_gate_and_source_reader(self):
        """生产重建必须真的走 PIT 门禁并读取 universe 源声明（不能硬编码"可信"）。"""
        import inspect

        import paper_trading as PT

        source = inspect.getsource(PT._rebuild_selection_factor_cache)
        # 断言**调用本身**（而不是松散子串——注释里也出现过这个词）
        self.assertIn("U.historical_universe(universe, cutoff, drop_unproven=True)", source)
        self.assertNotIn("U.asof_members(", source)
        self.assertIn("def load_universe_source", inspect.getsource(PT.U))

    def test_p17c_a_real_universe_file_never_claims_historical_completeness(self):
        """走**真实** ``load_universe_source``：当前快照格式的 universe.json 不得被当成历史归档。

        这条用例刻意不 patch 读取函数——否则"把来源硬编码成可信"的回归测不出来。
        """
        import json
        import tempfile

        import universe as U

        payload = {
            "built_at": "2026-09-15 08:00:00",
            "scope": "all_a_shares", "requested_limit": None,
            "stocks": [{"code": "600001", "list_date": "2015-01-05"}],
        }
        with tempfile.TemporaryDirectory(prefix="pit-universe-") as tmp:
            path = os.path.join(tmp, "universe.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            with mock.patch.object(U, "UNIVERSE_PATH", path):
                source = U.load_universe_source()
                out = U.historical_universe(payload["stocks"], ASOF)
                with tempfile.TemporaryDirectory(prefix="pit-missing-") as tmp2:
                    with mock.patch.object(U, "UNIVERSE_PATH",
                                           os.path.join(tmp2, "absent.json")):
                        missing = U.historical_universe(payload["stocks"], ASOF)

        self.assertNotIn("stocks", source)
        self.assertFalse(out["passed"])
        self.assertEqual(PIT.UNIVERSE_SOURCE_NOT_HISTORICAL, out["report"]["status"])
        self.assertFalse(out["report"]["historical_membership_complete"])
        # 读不到文件同样是"未证明"，而不是"默认可信"
        self.assertFalse(missing["passed"])
        self.assertEqual(PIT.UNIVERSE_SOURCE_UNKNOWN, missing["report"]["status"])

    def test_p16_historical_rebuild_fails_closed_without_membership_metadata(self):
        """只有今天的成分、且完全没有 list/delist metadata → 历史选股不得产出候选。

        绝不 fallback 到当前 universe，也绝不因为"没有证据证明未上市"就把这些股票
        送进 selector。
        """
        import paper_trading as PT

        universe = [{"code": "600001", "name": "只有今天的成分"},
                    {"code": "600002", "name": "同样没有日期"}]
        seen = {"gate": False, "klines": [], "price": []}

        def gate_spy(rows, cutoff):
            seen["gate"] = True
            return {"passed": True}

        def kline_spy(code):
            seen["klines"].append(code)
            return None

        def price_spy(*args, **kwargs):
            seen["price"].append(kwargs.get("asof"))
            raise AssertionError("不得为未证明的历史成分计算价格因子")

        with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                mock.patch.object(PT.U, "load_universe_source",
                                  return_value=self.HISTORICAL_SOURCE), \
                mock.patch.object(PT, "_selection_factor_history_gate", side_effect=gate_spy), \
                mock.patch.object(PT.dfc, "load_shared_kline", side_effect=kline_spy), \
                mock.patch.object(PT.F, "compute_price_factors", side_effect=price_spy):
            out = PT._rebuild_selection_factor_cache("2024-06-14")

        self.assertEqual("blocked", out["status"])
        self.assertTrue(out["pit_unavailable"])
        self.assertNotIn("refresh_gate", out)
        report = out["universe_membership"]
        self.assertEqual(0, report["kept"])
        self.assertEqual(2, report["unproven"])
        self.assertEqual(0, report["counts"][PIT.MEMBERSHIP_MEMBER])
        # 源本身是合法的历史归档，所以这条失败纯粹来自逐行成员资格未证明
        self.assertTrue(report["historical_membership_complete"])
        self.assertFalse(report["row_membership_passed"])
        # 没有进入任何候选/因子路径
        self.assertFalse(seen["gate"])
        self.assertEqual([], seen["klines"])
        self.assertEqual([], seen["price"])

    def test_p16b_future_delist_date_cannot_smuggle_a_code_into_the_universe(self):
        """只有 future ``delist_date``（缺 ``list_date``）的成分不得进入历史下游。

        源级门禁已通过（显式 ``historical_archive`` + ``historical_membership_complete``），
        所以这里只可能是**逐行**成员资格挡住它：``delist_date = 2026-01-01`` 不能反证
        2024 时已经上市。断言 B **从未**进入覆盖门禁 / K 线读取 / 因子阶段。
        """
        import paper_trading as PT

        universe = [{"code": "600001", "name": "老股", "list_date": "2020-01-01"},
                    {"code": "600002", "name": "只有未来退市日", "delist_date": "2026-01-01"}]
        state = {"coverage_gate_passed": False}
        seen = {"coverage": [], "klines": [], "price_asof": []}

        def coverage_spy(rows, cutoff):
            seen["coverage"].append(sorted(str(r.get("code")) for r in rows))
            return {"passed": state["coverage_gate_passed"], "reason": "coverage-stub"}

        def kline_spy(code):
            seen["klines"].append(str(code))
            return None

        def price_spy(*args, **kwargs):
            seen["price_asof"].append(kwargs.get("asof"))
            return pd.DataFrame()

        with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                mock.patch.object(PT.U, "load_universe_source",
                                  return_value=self.HISTORICAL_SOURCE), \
                mock.patch.object(PT, "_selection_factor_history_gate", side_effect=coverage_spy), \
                mock.patch.object(PT.dfc, "load_shared_kline", side_effect=kline_spy), \
                mock.patch.object(PT.F, "compute_price_factors", side_effect=price_spy):
            blocked_at_coverage = PT._rebuild_selection_factor_cache("2024-06-14")
            state["coverage_gate_passed"] = True
            reached_kline = PT._rebuild_selection_factor_cache("2024-06-14")

        # 覆盖门禁只看到已证明的 A
        self.assertEqual([["600001"], ["600001"]], seen["coverage"])
        # 覆盖门禁放开后，只有 A 被读取 K 线、只有 A 进入因子阶段
        self.assertEqual(["600001"], seen["klines"])
        self.assertEqual([dt.date(2024, 6, 14)], seen["price_asof"])
        self.assertEqual(1, reached_kline["eligible_universe_rows"])
        # B 从未出现在任何下游
        self.assertNotIn("600002", seen["klines"])
        self.assertNotIn("600002", [c for codes in seen["coverage"] for c in codes])

        for out in (blocked_at_coverage, reached_kline):
            self.assertEqual("blocked", out["status"])
            # 源是合法历史归档 → 失败只来自逐行成员资格，不是 pit_unavailable
            self.assertNotIn("pit_unavailable", out)
        report = blocked_at_coverage["universe_membership"]
        self.assertTrue(report["historical_membership_complete"])
        self.assertTrue(report["row_membership_passed"])
        self.assertEqual(1, report["kept"])
        self.assertEqual(1, report["counts"][PIT.MEMBERSHIP_MEMBER])
        self.assertEqual(1, report["counts"][PIT.MEMBERSHIP_UNKNOWN])
        self.assertEqual(1, report["unproven"])


# ─────────────────────────────────────────────────────────────────────────────
# 契约本身
# ─────────────────────────────────────────────────────────────────────────────

class PointInTimeContractTests(unittest.TestCase):
    def test_reason_vocabulary_is_fixed(self):
        self.assertEqual(
            ("visible", "future", "availability_unknown", "invalid_timestamp", "future_period"),
            PIT.REASON_CODES)
        for reason in PIT.REASON_CODES:
            self.assertIsInstance(reason, str)

    def test_china_tz_offset_matches_explicit_offset_timestamps(self):
        naive = PIT.parse_asof("2026-01-05 15:00")
        plus8 = PIT.parse_asof("2026-01-05T15:00:00+08:00")
        utc = PIT.parse_asof("2026-01-05T07:00:00+00:00")
        self.assertEqual(naive, plus8)
        self.assertEqual(naive, utc)
        self.assertEqual(dt.timedelta(hours=8), naive.utcoffset())
        # 判定结果也必须一致，而不是只有解析结果一致
        for stamp in (naive, plus8, utc):
            self.assertEqual("visible",
                             PIT.is_visible_at("2026-01-05T15:00:00+08:00", stamp)["reason"])
            self.assertEqual("visible",
                             PIT.is_visible_at("2026-01-05T15:00:00+08:00", stamp)["reason"])

    def test_date_only_availability_never_means_midnight(self):
        """date-only 可用性表示"该日结束"，不能假设当天 00:00 已经可用。"""
        self.assertEqual("2024-06-30T23:59:59+08:00",
                         PIT.parse_available_at("2024-06-30").isoformat(timespec="seconds"))
        self.assertEqual("future", PIT.is_visible_at("2024-06-30", "2024-06-30T00:00:00+08:00")["reason"])

    def test_numeric_epochs_are_not_guessed(self):
        self.assertIsNone(PIT.parse_available_at(1717200000))
        self.assertEqual("invalid_timestamp", PIT.is_visible_at(1717200000, ASOF)["reason"])

    def test_unknown_availability_is_unavailable_in_strict_mode_only(self):
        self.assertEqual("availability_unknown", PIT.is_visible_at(None, ASOF)["reason"])
        self.assertEqual("visible", PIT.is_visible_at(None, None)["reason"])

    def test_filter_visible_rows_drops_unknown_rows_in_history(self):
        rows = [{"code": "a", "observed_at": "2024-06-01"},
                {"code": "b", "observed_at": "2024-07-01"},
                {"code": "c"}]
        kept = PIT.filter_visible_rows(rows, ASOF, key="code")
        self.assertEqual(["a"], kept)
        self.assertEqual(["a", "b", "c"], PIT.filter_visible_rows(rows, None, key="code"))

    def test_pit_flags_default_to_fail_closed(self):
        flags = PIT.pit_flags()
        for key in ("price_pit_safe", "financial_pit_safe", "snapshot_pit_safe",
                    "classification_pit_safe", "sentiment_pit_safe"):
            self.assertFalse(flags[key])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
