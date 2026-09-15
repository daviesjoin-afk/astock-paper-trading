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


class PointInTimeBase(unittest.TestCase):
    """所有因子调用都 patch 掉 manifest，避免依赖本机 data_cache。"""

    def setUp(self):
        patcher = mock.patch.object(F.dfc, "get_kline_manifest", return_value={})
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
        """复权口径必须显式 provenance：证明不了就不声称 PIT-safe。"""
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
            for code, expected in (("raw", True), ("qfq_before_asof", True),
                                   ("qfq_after_asof", False), ("unknown", False)):
                out = F.compute_price_factors({code: klines[TARGET]}, asof=ASOF)
                self.assertEqual(expected, bool(out.loc[code, "adjustment_pit_safe"]),
                                 "adjustment_pit_safe mismatch for %s" % code)
        # 全票都不可证明复权口径时，聚合 flag 必须为 False（而不是含糊的 True）
        with mock.patch.object(F.dfc, "get_kline_manifest",
                               return_value={"qfq_after_asof": manifest["qfq_after_asof"]}):
            out = F.compute_price_factors({"qfq_after_asof": klines[TARGET]}, asof=ASOF)
        self.assertFalse(out.attrs["pit"]["price_pit_safe"])
        self.assertEqual(1, out.attrs["pit"]["codes_without_proven_adjustment"])


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
        rows = [
            {"code": "600001", "delist_date": "2024-06-01"},   # 已退市
            {"code": "600002", "delist_date": "2024-06-14"},   # asof 当日 → 已退市（asof < delist 才有效）
            {"code": "600003", "delist_date": "2024-06-17"},   # 仍在市
            {"code": "600004"},                                # 无日期 → 未证明
        ]
        result = PIT.universe_asof_members(rows, ASOF)
        kept = [row["code"] for row in result["members"]]
        self.assertEqual(["600003", "600004"], kept)
        self.assertEqual(2, result["report"]["counts"][PIT.MEMBERSHIP_DELISTED])
        self.assertEqual("membership_unknown", PIT.universe_membership(rows[3], ASOF)["status"])

    def test_p9c_unproven_membership_is_reported_not_faked(self):
        rows = [{"code": "600001"}, {"code": "600002"}]
        default = PIT.universe_asof_members(rows, ASOF)
        self.assertEqual(2, default["report"]["unproven"])
        self.assertEqual(2, len(default["members"]))
        strict = PIT.universe_asof_members(rows, ASOF, drop_unproven=True)
        self.assertEqual([], strict["members"])

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
    def test_p14_historical_rebuild_filters_the_universe_by_asof(self):
        """回放重建必须先按 asof 过滤历史成分，而不是直接吃今天的成分。"""
        import paper_trading as PT

        universe = [{"code": "600001", "name": "老股", "list_date": "2020-01-01"},
                    {"code": "600002", "name": "未来上市", "list_date": "2030-01-01"}]
        seen = {}

        def gate_spy(rows, cutoff):
            seen["codes"] = sorted(str(r.get("code")) for r in rows)
            seen["cutoff"] = cutoff
            return {"passed": False, "reason": "stub"}

        with mock.patch.object(PT.U, "load_universe", return_value=universe), \
                mock.patch.object(PT, "_selection_factor_history_gate", side_effect=gate_spy):
            out = PT._rebuild_selection_factor_cache("2024-06-14")

        self.assertEqual(dt.date(2024, 6, 14), seen["cutoff"])
        # 未来上市的股票在进入覆盖门禁之前就被剔除
        self.assertEqual(["600001"], seen["codes"])
        self.assertEqual("blocked", out["status"])
        self.assertIsNotNone(out["universe_membership"])
        self.assertEqual(1, out["universe_membership"]["counts"][PIT.MEMBERSHIP_NOT_LISTED_YET])

    def test_p14b_live_rebuild_never_filters(self):
        """``asof_date=None``（live）不套用历史成员资格，且不报 unproven。"""
        import paper_trading as PT

        with mock.patch.object(PT.U, "load_universe",
                               return_value=[{"code": "600001"}]), \
                mock.patch.object(PT, "_selection_factor_history_gate",
                                  side_effect=AssertionError("live 不应调用覆盖门禁")):
            out = PT._rebuild_selection_factor_cache(None)
        self.assertIsNone(out.get("universe_membership"))


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
