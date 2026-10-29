# -*- coding: utf-8 -*-
"""手动委托：日线复用的黄金回放与失败矩阵。

审计要求（plans/source-shared-layer-slimming-audit-20261028.md 第 1 项）：
复用改造必须证明**买卖两条路径的真实决策结果不变**，并且覆盖
缺失数据、消费者修改返回值、下一次规划重新取证等情形。

对照方式：把复用的实现本身换掉，而不是复刻一份改造前的代码。
- ``before``：把 ``manual_orders`` 用的缓存换成"永不缓存"的等价实现，
  每次需要日线都重新读取 —— 这正是复用改造之前的语义；
- ``after``：真实实现（同一规划内按 (证券, 截止日, 是否含截止日) 复用）。

断言的是**决策事实相同**（模型、数量、理由、是否放行、决策快照）以及
**读取次数下降**，而不是行数或调用栈。
"""
import datetime as dt
import unittest
from dataclasses import replace
from unittest import mock

import pandas as pd

import manual_orders as MO
import paper_trading as PT
from test_deferred_fill_cycle_binding import _ApprovedAllocationFixture
from test_deferred_fill_cycle_binding import _ProvenRiskHarness


def _kline(day, closes=(10.0, 11.0, 12.0)):
    index = pd.to_datetime([day - dt.timedelta(days=3), day - dt.timedelta(days=2),
                            day - dt.timedelta(days=1)])
    return pd.DataFrame(
        {
            "open": list(closes),
            "close": list(closes),
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "volume": [100.0] * len(closes),
            "amount": [1000.0] * len(closes),
        },
        index=index,
    )


def _stable_snapshot(snapshot):
    """去掉与本次改造无关的易变字段（生成时刻）。"""
    if not isinstance(snapshot, dict):
        return snapshot
    return {key: value for key, value in snapshot.items()
            if key not in {"decision_at", "generated_at", "created_at"}}


class _NoReuseCache:
    """复用改造之前的语义：每次都重新读取，永不缓存。"""

    def __init__(self, loader):
        self._loader = loader
        self.reads = 0
        self.hits = 0

    def get(self, security, cutoff, *, inclusive=True):
        self.reads += 1
        return self._loader(security, cutoff, inclusive=inclusive)


class _PlanHarness(_ProvenRiskHarness, _ApprovedAllocationFixture):
    """在手单规划上做黄金回放的公共夹具。

    买入路径必须先拿到"精确组合分配证据"才能走到模型层，否则计划会停在
    ``deferred_capacity``，根本到不了被测的日线消费者。这里复用已被验证过的
    真实分配夹具（``_approved_allocation_plan`` / ``_same_allocation_snapshot``），
    避免在本文件里手写一份会漂移的替代品。
    """

    def _quote(self, price=11.0):
        return {
            "code": self.code,
            "name": "测试股",
            "price": price,
            "pct": 1.0,
            "turnover": 2.0,
            "quote_at": f"{self.day.isoformat()} 10:00:00",
            "quote_source": "live",
        }

    def _plan(self, side, *, frame, quote=None, loader=None, reuse=True):
        """跑一次真实规划，返回 (plan, 读取次数, 读取记录)。"""
        quote = quote or self._quote()
        calls = []

        def loader_fn(security, cutoff, inclusive=True):
            calls.append((str(security), str(cutoff), bool(inclusive)))
            if loader is not None:
                return loader(security, cutoff, inclusive=inclusive)
            return None if frame is None else frame.copy(deep=True)

        market = {"light": "green", "breadth": 0.5, "sentiment": "neutral",
                  "overseas": {"light": "green"}}
        cache_owner = MO.RoundKlineCache if reuse else _NoReuseCache
        with mock.patch.object(MO, "_completed_kline", side_effect=loader_fn) as load, \
                mock.patch.object(MO, "RoundKlineCache", cache_owner), \
                mock.patch.object(PT, "_build_portfolio_entry_plan",
                                  side_effect=self._approved_allocation_plan), \
                mock.patch.object(PT.PRS, "capture_portfolio_runtime_snapshot",
                                  side_effect=self._same_allocation_snapshot), \
                mock.patch.object(PT, "_entry_freeze_enabled", lambda: False), \
                mock.patch.object(PT, "_market_state", return_value=market):
            # 依赖在打桩之后解析，保证 vertical 拿到的是被替换过的能力。
            deps = MO._dependencies()
            with mock.patch.object(MO, "_dependencies", return_value=deps):
                plan = MO._manual_order_plan(
                    self.conn, self.account_id, self.code, side, qty=100,
                    asof_day=self.day, quote=quote, all_quotes={self.code: quote},
                    live_universe=[dict(quote)],
                )
        return plan, load.call_count, calls


class ManualOrderKlineReplayTests(_PlanHarness):
    """改造前/后黄金回放：决策事实必须逐项相同。"""

    def _assert_same_decision(self, before, after):
        self.assertEqual(before["risk"]["model"], after["risk"]["model"])
        self.assertEqual(before["qty"], after["qty"])
        self.assertEqual(before["recommended_qty"], after["recommended_qty"])
        self.assertEqual(before["reasons"], after["reasons"])
        self.assertEqual(before["allowed"], after["allowed"])
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before.get("available_qty"), after.get("available_qty"))
        self.assertEqual(
            _stable_snapshot(before["risk"].get("decision_snapshot")),
            _stable_snapshot(after["risk"].get("decision_snapshot")),
        )

    def test_buy_path_decision_is_identical_before_and_after_reuse(self):
        frame = _kline(self.day)
        before, before_reads, _ = self._plan("buy", frame=frame, reuse=False)
        after, after_reads, _ = self._plan("buy", frame=frame, reuse=True)
        self._assert_same_decision(before, after)
        self.assertGreater(before_reads, 0)
        self.assertLess(after_reads, before_reads, "复用后同一规划内的读取次数必须下降")

    def test_sell_path_decision_is_identical_before_and_after_reuse(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        frame = _kline(self.day)
        before, before_reads, _ = self._plan("sell", frame=frame, reuse=False)
        after, after_reads, _ = self._plan("sell", frame=frame, reuse=True)
        self._assert_same_decision(before, after)
        self.assertLess(after_reads, before_reads)

    def test_hold_days_and_model_see_the_same_bars(self):
        """持仓天数、模型、快照必须是同一份日线的事实。"""
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        frame = _kline(self.day)
        seen = []

        def loader(security, cutoff, inclusive=True):
            return frame.copy(deep=True)

        def decision(position, *, kline, **kwargs):
            seen.append(("model", int(kline["close"].iloc[-1]),
                         position.get("hold_days")))
            return {"action": "观察", "hold_days": position.get("hold_days")}

        def snapshot(payload, **kwargs):
            seen.append(("snapshot", float(kwargs["kline"]["close"].iloc[-1]), None))
            return payload

        deps = MO._dependencies()
        quote = self._quote()
        with mock.patch.object(MO, "_completed_kline", side_effect=loader) as load, \
                mock.patch.object(MO.DE, "sell_decision", side_effect=decision), \
                mock.patch.object(MO, "_dependencies", return_value=replace(
                    deps, _with_decision_snapshot=snapshot)):
            MO._manual_order_plan(
                self.conn, self.account_id, self.code, "sell", qty=100,
                asof_day=self.day, quote=quote, all_quotes={self.code: quote},
            )
        self.assertEqual(load.call_count, 1, "一次规划只应读取一次日线")
        self.assertEqual([row[0] for row in seen], ["model", "snapshot"])
        self.assertEqual(seen[0][1], seen[1][1], "模型与快照必须看到同一根 K 线")

    def test_missing_kline_produces_identical_decision_either_way(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        before, _, _ = self._plan("sell", frame=None, reuse=False)
        after, _, _ = self._plan("sell", frame=None, reuse=True)
        self._assert_same_decision(before, after)

    def test_next_plan_reads_again(self):
        frame = _kline(self.day)
        _, first_reads, _ = self._plan("sell", frame=frame)
        _, second_reads, _ = self._plan("sell", frame=frame)
        self.assertGreaterEqual(first_reads, 1)
        self.assertGreaterEqual(second_reads, 1)


class ManualOrderKlineFailureMatrixTests(_PlanHarness):
    """失败矩阵：缺失、空、重试、消费者修改、跨截止日。"""

    def test_empty_frame_is_not_cached_and_can_be_retried(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        empty = pd.DataFrame()
        calls = []

        def loader(security, cutoff, inclusive=True):
            calls.append(str(cutoff))
            return empty

        quote = self._quote()
        with mock.patch.object(MO, "_completed_kline", side_effect=loader) as load:
            MO._manual_order_plan(
                self.conn, self.account_id, self.code, "sell", qty=100,
                asof_day=self.day, quote=quote, all_quotes={self.code: quote},
            )
        # 空结果必须被重读（而不是缓存成"确定没有日线"）。
        self.assertGreater(load.call_count, 1)

    def test_consumer_mutation_does_not_leak_to_other_consumers(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        frame = _kline(self.day)
        seen = []

        def decision(position, *, kline, **kwargs):
            seen.append(float(kline["close"].iloc[-1]))
            kline.iloc[-1, kline.columns.get_loc("close")] = 999.0
            return {"action": "观察"}

        def snapshot(payload, **kwargs):
            seen.append(float(kwargs["kline"]["close"].iloc[-1]))
            return payload

        deps = MO._dependencies()
        quote = self._quote()
        with mock.patch.object(MO, "_completed_kline", return_value=frame), \
                mock.patch.object(MO.DE, "sell_decision", side_effect=decision), \
                mock.patch.object(MO, "_dependencies", return_value=replace(
                    deps, _with_decision_snapshot=snapshot)):
            MO._manual_order_plan(
                self.conn, self.account_id, self.code, "sell", qty=100,
                asof_day=self.day, quote=quote, all_quotes={self.code: quote},
            )
        self.assertEqual(seen, [12.0, 12.0])
        self.assertEqual(float(frame["close"].iloc[-1]), 12.0)

    def test_different_cutoffs_are_not_mixed(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        calls = []

        def loader(security, cutoff, inclusive=True):
            calls.append((str(cutoff), bool(inclusive)))
            return _kline(self.day)

        quote = self._quote()
        with mock.patch.object(MO, "_completed_kline", side_effect=loader):
            MO._manual_order_plan(
                self.conn, self.account_id, self.code, "sell", qty=100,
                asof_day=self.day, quote=quote, all_quotes={self.code: quote},
            )
        # 同一规划里只允许一种 (截止日, 含截止日) 组合被反复使用；
        # 若出现多种组合，说明缓存键没有覆盖真实区分度。
        self.assertTrue(calls)
        self.assertEqual(len(set(calls)), 1)

    def test_inclusive_and_exclusive_cutoffs_are_separate_entries(self):
        self._insert_lot(self.account_id, self.code, qty=200, cost=10.0)
        calls = []

        def loader(security, cutoff, inclusive=True):
            calls.append((str(cutoff), bool(inclusive)))
            return _kline(self.day)

        from paper_kline_snapshot import RoundKlineCache

        cache = RoundKlineCache(loader)
        cache.get("600001", "2026-09-10", inclusive=True)
        cache.get("600001", "2026-09-10", inclusive=False)
        cache.get("600001", "2026-09-10", inclusive=False)
        self.assertEqual(
            calls,
            [("2026-09-10", True), ("2026-09-10", False)],
            "是否包含截止日必须区分缓存键",
        )


if __name__ == "__main__":
    unittest.main()
