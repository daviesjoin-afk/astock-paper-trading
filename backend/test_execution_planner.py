# -*- coding: utf-8 -*-
"""Central execution planner regression tests (PR-06).

测试全部离线：``execution_planner`` 通过 ``_pt()`` 惰性取用 ``paper_trading``，
这里直接注入替身命名空间，既不依赖真实数据库，也不会被生产 ``data_cache``
污染（与 ``test_ai_controls`` 的隔离思路一致）。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import types
import unittest
from unittest import mock

import execution_planner as EP

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
NOW = "2026-09-08 10:00:00"
LATE = "2026-09-08 15:00:00"
#: 替身订单与 lot 共用的 durable 周期（v18 write-time fact）。
ORDER_CYCLE = 8


def _order_row(**overrides):
    """A working order row as `paper_orders` would return before execution."""
    row = {
        "id": 7, "account_id": "tq_breakout", "signal_id": None, "side": "buy",
        "code": "002241", "name": None, "qty": 100, "planned_price": 21.5,
        "filled_price": None, "amount": None, "fees": None, "status": "pending_execution",
        "reason": "\u7a81\u7834\u4e70\u5165", "risk_payload": "{}", "realized_pnl": None,
        "created_at": NOW, "executed_at": None, "order_type": "market",
        "origin": "strategy", "expires_at": None, "cancelled_at": None,
        "strategy_id": None, "strategy_version": None, "strategy_checksum": None,
        "retry_of_order_id": None, "cycle_id": ORDER_CYCLE,
        "filled_qty": 0, "remaining_qty": 100, "execution_version": 0,
    }
    row.update(overrides)
    return row


def _provenance(order_cycle=ORDER_CYCLE, *, proven=True, status=None, exists=True):
    """``paper_trading._order_cycle_provenance_for_order`` 的替身结论。

    成交路径现在必须先证明订单的周期归属；替身必须能给出**四态**中的任意一种，
    否则「legacy NULL 必须 fail closed」这类契约在替身下无法被测试。
    """
    import paper_trading as PT

    if proven:
        return PT.OrderCycleProvenance(True, True, order_cycle, PT.ORDER_CYCLE_PROVEN)
    return PT.OrderCycleProvenance(
        exists, True, None, status or PT.ORDER_CYCLE_LEGACY_UNKNOWN,
    )


def _cycle_stub_kwargs(order_cycle=ORDER_CYCLE, *, proven=True, status=None,
                       exists=True):
    """注入到 paper_trading 替身里的周期归属成员。"""
    import paper_trading as PT

    return {
        "_order_cycle_provenance_for_order": (
            lambda conn, order_id: _provenance(
                order_cycle, proven=proven, status=status, exists=exists,
            )
        ),
        "OrderCycleProvenanceUnknown": PT.OrderCycleProvenanceUnknown,
        "ORDER_CYCLE_PROVEN": PT.ORDER_CYCLE_PROVEN,
        "ORDER_CYCLE_LEGACY_UNKNOWN": PT.ORDER_CYCLE_LEGACY_UNKNOWN,
        # execution-cycle invariant：``commit_fill`` 现在还会校验
        # order cycle == account cycle == active cycle。这里指向**真实**实现，
        # 让替身的 ``conn`` 通过它真正需要的三条读（provenance 已注入、
        # ``paper_accounts.cycle_id``、active cycle id）来决定放行与否 ——
        # 如果换成恒真的 lambda，本文件里所有成交用例都会在「周期校验被删掉」
        # 的变异下依然全绿。
        "_assert_order_execution_cycle": PT._assert_order_execution_cycle,
        "_active_cycle_id_readonly": PT._active_cycle_id_readonly,
        "_account_cycle_id_readonly": PT._account_cycle_id_readonly,
        "OrderExecutionCycleChanged": PT.OrderExecutionCycleChanged,
    }


def _fill_row(**overrides):
    """The matching `paper_fills` row: same qty as requested, so the verdict is verified."""
    row = {
        "id": 1, "order_id": 7, "account_id": "tq_breakout", "side": "buy",
        "code": "002241", "qty": 100, "price": 21.5, "amount": 2150.0,
        "fees": 2.15, "fill_date": "2026-09-08", "quote_at": NOW,
        "assumption": "local snapshot",
    }
    row.update(overrides)
    return row


def _valid_execution_context(*, side="buy", quantity=100, sellable=1000, quote_at=NOW,
                             status="pending_execution", already_filled=0):
    """Explicit verified market and tradability evidence for writer contract tests."""
    quote = {
        "code": "002241", "price": 21.5, "amount": 50_000_000.0,
        "quote_at": quote_at, "execution_asof": quote_at,
        "quote_source": "test_market", "quote_validation": "cross_source_checked",
    }
    reading = EP.market_reading_for_execution(
        quote, asof_day="2026-09-08", execution_asof=quote_at,
    )
    tradability = types.SimpleNamespace(
        evidence_present=True, can_buy=True, can_sell=True,
        buy_block_reason="ok", sell_block_reason="ok",
        to_dict=lambda: {"evidence_present": True, "can_buy": True, "can_sell": True},
    )
    return EP.ExecutionContext(
        session_date="2026-09-08", execution_asof=quote_at, quote=quote,
        market_reading=reading, tradability=tradability,
        available_liquidity=1_000_000, sellable_quantity=sellable,
        already_filled_quantity=already_filled, current_order_status=status,
    )


class _FakeResult:
    def __init__(self, value=None, row=None, rows=None):
        self._value = value
        self._row = row
        self._rows = rows

    def fetchone(self):
        if self._row is not None:
            return self._row
        return (self._value,)

    def fetchall(self):
        return list(self._rows or ())


class _FakeConn:
    """Records the queries this planner makes, including the verification read-back.

    ``commit_fill`` 现在还会：读订单的 durable 周期归属（读取被注入的
    ``_order_cycle_provenance_for_order``，不经此替身）、校验订单身份
    （``SELECT account_id, code, side FROM paper_orders WHERE id=?``）、校验
    execution-cycle invariant（``paper_accounts.cycle_id`` + active cycle id），
    并在写入成交后盖章（读回订单与其 fill 行）。替身因此要回答 ``paper_orders``
    行、``paper_fills`` 行、身份三元组、账户周期、active 周期、以及信号关注度计数。
    """

    def __init__(self, interest=0, raises=False, order_row=None, fill_rows=(),
                 identity=None, account_cycle=None, active_cycle=None):
        self.interest = interest
        self.raises = raises
        self.queries = []
        self.order_row = order_row
        self.fill_rows = list(fill_rows)
        #: ``(account_id, code, side[, strategy_id, strategy_version, strategy_checksum])``；
        #: 缺省从 ``order_row`` 推导，保持身份与 durable provenance 自洽。
        self.identity = identity
        #: 缺省与 ``ORDER_CYCLE`` 一致 —— 即「账本仍在订单所属周期」，让原本
        #: 合法的成交用例继续走通；需要构造漂移的用例显式传别的值。
        self.account_cycle = ORDER_CYCLE if account_cycle is None else account_cycle
        self.active_cycle = ORDER_CYCLE if active_cycle is None else active_cycle

    def _identity_row(self):
        if self.identity is not None:
            identity = tuple(self.identity)
            if len(identity) == 3:
                return identity + (None, None, None)
            return identity
        row = self.order_row
        if not row:
            return ("tq_breakout", "002241", "buy", None, None, None)
        get = row.get if isinstance(row, dict) else (lambda k, d=None: d)
        return (
            get("account_id"), get("code"), get("side"),
            get("strategy_id"), get("strategy_version"), get("strategy_checksum"),
        )

    def execute(self, sql, params=()):
        self.queries.append((sql, tuple(params)))
        if self.raises:
            raise RuntimeError("db unavailable")
        text = " ".join(str(sql).split()).lower()
        if text.startswith("select * from paper_fills"):
            return _FakeResult(rows=self.fill_rows)
        if text.startswith("select account_id, code, side"):
            return _FakeResult(row=self._identity_row())
        if text.startswith("select * from paper_orders where id"):
            return _FakeResult(row=self.order_row)
        if text.startswith("select 1 from paper_fills where order_id"):
            return _FakeResult(row=())
        if text.startswith("select coalesce(sum(qty),0),coalesce(sum(amount),0),coalesce(sum(fees),0)"):
            return _FakeResult(row=(0, 0.0, 0.0))
        # ── execution-cycle invariant 的两次只读查询（顺序无关，各自可辨认）──
        if text.startswith("select cycle_id from paper_accounts"):
            return _FakeResult(row=(self.account_cycle,))
        if text.startswith("select id from paper_cycles"):
            return _FakeResult(row=(self.active_cycle,))
        return _FakeResult(self.interest)


def _pt_stub(now=NOW, statuses=("pending", "waitlist", "retry")):
    """paper_trading 替身：只提供 planner 真正用到的成员。"""
    return types.SimpleNamespace(
        _now=lambda: now,
        ENTRY_RETRY_SIGNAL_STATUSES=statuses,
        MAIN_FORCE_STRATEGY_ID="main_force_top10",
        NEW_STRATEGY_ID="reported_profit_breakout",
        _num=lambda value, default=0.0: default if value in (None, "") else float(value),
        **_cycle_stub_kwargs(),
    )


class _StubbedPlannerTest(unittest.TestCase):
    """每个用例都用同一份替身，并重置策略表缓存，避免相互污染。"""

    def setUp(self):
        self._patcher = mock.patch.object(EP, "_pt", lambda: _pt_stub())
        self._patcher.start()
        EP._POLICY_CACHE = None

    def tearDown(self):
        self._patcher.stop()
        EP._POLICY_CACHE = None


class ExecutionPolicyTests(_StubbedPlannerTest):
    def test_policy_table_is_declarative_and_identity_free_in_execution(self):
        # 执行层差异在数据表里，执行代码里不再出现身份比较。
        main_force = EP.policy_for("main_force_top10")
        quality = EP.policy_for("reported_profit_breakout")
        self.assertTrue(main_force.holds_reserved_seat)
        self.assertFalse(quality.holds_reserved_seat)
        self.assertEqual("main_force_top10", quality.seat_reserve_owner)
        self.assertTrue(quality.manual_entry_review)
        self.assertFalse(main_force.manual_entry_review)

    def test_chase_lanes_are_declared_per_account(self):
        self.assertEqual("momentum", EP.policy_for("tq_breakout").chase_lane)
        self.assertEqual("sector_hot", EP.policy_for("sector_rotation").chase_lane)
        self.assertEqual("none", EP.policy_for("trend_pullback").chase_lane)
        self.assertEqual("none", EP.policy_for("main_force_top10").chase_lane)

    def test_unknown_account_falls_back_to_a_conservative_policy(self):
        policy = EP.policy_for("not_a_strategy")
        self.assertEqual("none", policy.chase_lane)
        self.assertFalse(policy.manual_entry_review)
        self.assertIsNone(policy.seat_reserve_owner)

    def test_execution_sources_contain_no_identity_branching(self):
        # 回归护栏：手动委托与 planner 里不允许再出现按账户 ID 的分支。
        pattern = re.compile(r"==\s*(MAIN_FORCE_STRATEGY_ID|NEW_STRATEGY_ID)")
        for name in ("manual_orders.py", "execution_planner.py"):
            with open(os.path.join(BACKEND_DIR, name), "r", encoding="utf-8") as handle:
                source = handle.read()
            self.assertIsNone(pattern.search(source), name)

    def test_paper_trading_execution_functions_are_identity_free(self):
        with open(os.path.join(BACKEND_DIR, "paper_trading.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        pattern = re.compile(r"==\s*(MAIN_FORCE_STRATEGY_ID|NEW_STRATEGY_ID)")
        for name in ("_buy_order", "_chase_entry_gate", "_strategy_market_policy"):
            match = re.search(rf"^def {name}\(", source, re.M)
            self.assertIsNotNone(match, name)
            start = match.start()
            nxt = re.search(r"^def ", source[start + 1:], re.M)
            body = source[start:start + 1 + (nxt.start() if nxt else len(source))]
            self.assertIsNone(pattern.search(body), name)


class SimulationExecutionContractTests(unittest.TestCase):
    DAY = "2026-09-08"

    def _facts(self, *, quote_at=None, execution_asof=None, amount=50_000_000.0,
               can_buy=True, can_sell=True, buy_reason="ok", sell_reason="ok",
               liquidity=50_000, sellable=50_000, status="pending_execution",
               verification="cross_source_checked", session_consumed=0):
        quote_at = quote_at or f"{self.DAY} 10:00:00"
        execution_asof = execution_asof or quote_at
        quote = {
            "code": "600901", "price": 20.0, "amount": amount,
            "quote_at": quote_at, "execution_asof": execution_asof,
            "quote_source": "live", "quote_validation": verification,
        }
        reading = EP.market_reading_for_execution(
            quote, asof_day=self.DAY, execution_asof=execution_asof,
        )
        tradability = types.SimpleNamespace(
            evidence_present=True, can_buy=can_buy, can_sell=can_sell,
            buy_block_reason=buy_reason, sell_block_reason=sell_reason,
            to_dict=lambda: {
                "evidence_present": True, "can_buy": can_buy, "can_sell": can_sell,
                "buy_block_reason": buy_reason, "sell_block_reason": sell_reason,
            },
        )
        context = EP.ExecutionContext(
            session_date=self.DAY, execution_asof=execution_asof, quote=quote,
            market_reading=reading, tradability=tradability,
            available_liquidity=liquidity, sellable_quantity=sellable,
            same_day_consumed_quantity=session_consumed,
            current_order_status=status,
        )
        return quote, context

    def _intent(self, *, side="buy", qty=1000, order_type="market", reference_price=20.0):
        return EP.PersistedOrderIntent(
            order_id=1, account_id="test", cycle_id=1,
            strategy_id="test", strategy_version=1, strategy_checksum="sha256:test",
            signal_id=9, symbol="600901", side=side, desired_quantity=qty,
            intent_at=f"{self.DAY} 09:35:00", reference_price=reference_price,
            order_type=order_type, signal_provenance={"frozen": True},
        )

    def test_verified_liquid_quote_produces_deterministic_full_fill_and_fees(self):
        _, context = self._facts(liquidity=200_000)
        first = EP.evaluate_simulated_execution(self._intent(), context)
        second = EP.evaluate_simulated_execution(self._intent(), context)
        self.assertTrue(first.executable_now)
        self.assertEqual("filled", first.status)
        self.assertEqual(1000, first.fill_quantity)
        self.assertEqual(0, first.remaining_quantity)
        self.assertEqual(20.02, first.fill_price)
        self.assertEqual(2.0, first.fees)
        self.assertEqual(first, second)

    def test_liquidity_caps_fill_and_preserves_remaining_quantity(self):
        _, context = self._facts(liquidity=30_000)
        result = EP.evaluate_simulated_execution(self._intent(), context)
        self.assertEqual("partially_filled", result.status)
        self.assertEqual(300, result.fill_quantity)
        self.assertEqual(700, result.remaining_quantity)
        self.assertIn(EP.ExecutionReason.INSUFFICIENT_LIQUIDITY.value, result.reasons)

    def test_odd_lot_liquidation_only_when_it_closes_the_sellable_remainder(self):
        """不足一手的**清仓**卖出必须放行；不足一手的部分卖出必须拒绝。

        奇数股余额（送转/配股后会留下不足 100 股的残额）如果按 ``lot_size`` 向下
        取整就永远卖不掉：``50 // 100 * 100 == 0``，残额被困在账户里。因此 planner
        对"卖出量 == 当前可卖余额、且小于一手"放行整笔；其余不足一手的卖出仍然拒绝，
        绝不静默取整成一个更小（甚至为 0）的单子。

        两条分支必须同时断言：只验证拒绝路径时，把清仓分支整个禁用也不会有测试失败
        （本 PR 合并前实测确认过），残额回退就成了无人看守的缺口。
        """
        # 清仓：可卖余额就是这 50 股，整笔放行（不按手数取整）。
        # 流动性给足，确保结论由 odd-lot 分支而非参与度上限决定。
        _, exit_context = self._facts(sellable=50, amount=500_000_000.0)
        exit_result = EP.evaluate_simulated_execution(
            self._intent(side="sell", qty=50), exit_context,
        )
        self.assertTrue(exit_result.executable_now)
        self.assertEqual("filled", exit_result.status)
        self.assertEqual(50, exit_result.fill_quantity,
                         "不足一手的清仓卖出被按手数取整（取整后为 0），残额永远卖不掉")

        # 部分卖出：同样 50 股，但可卖余额更大（200），必须拒绝而不是取整成 0。
        _, partial_context = self._facts(sellable=200, amount=500_000_000.0)
        partial_result = EP.evaluate_simulated_execution(
            self._intent(side="sell", qty=50), partial_context,
        )
        self.assertIn(EP.ExecutionReason.INVALID_QUANTITY.value, partial_result.reasons,
                      "不足一手的部分卖出被接受（应拒绝，绝不静默取整）")

    def test_same_day_consumption_is_subtracted_from_cumulative_participation(self):
        """同一个累计成交量不得被重复消费（R26）。

        行情给出的是**当日累计**成交额，10:05 的 snapshot 仍然包含 10:00 之前那部分
        成交量。若把已消耗量减掉，10:00 吃掉的 1% 参与额度就不能在 10:05 再吃一次。
        """
        _, context = self._facts(liquidity=10_000, session_consumed=3_000)
        result = EP.evaluate_simulated_execution(self._intent(), context)
        # 参与额度 = 10_000 × 1% = 100 股，已消耗 3_000 ⇒ 本事件可执行 0。
        self.assertFalse(result.executable_now)
        self.assertEqual(0, result.fill_quantity)
        self.assertEqual(1000, result.remaining_quantity)
        self.assertIn(EP.ExecutionReason.INSUFFICIENT_LIQUIDITY.value, result.reasons)
        self.assertEqual(
            0, result.liquidity_evidence["executable_capacity"],
            result.liquidity_evidence,
        )

    def test_growing_cumulative_volume_releases_further_capacity(self):
        """行情累计成交额增长后，剩余参与额度允许继续成交（R26）。"""
        # 10:00：累计 10_000 股 ⇒ 1% = 100 股额度，消耗 0 ⇒ 成交 100 股。
        _, first_context = self._facts(liquidity=10_000, session_consumed=0)
        first = EP.evaluate_simulated_execution(self._intent(qty=100), first_context)
        self.assertTrue(first.executable_now)
        self.assertEqual(100, first.fill_quantity)
        # 10:05：累计增长到 300_000 股（1% = 3_000 股），已消耗 100 ⇒ 仍可继续。
        _, second_context = self._facts(liquidity=300_000, session_consumed=100)
        second = EP.evaluate_simulated_execution(self._intent(qty=100), second_context)
        self.assertTrue(second.executable_now)
        self.assertEqual(100, second.fill_quantity)
        self.assertEqual(
            3_000, second.liquidity_evidence["participation_capacity"],
        )
        self.assertEqual(100, second.liquidity_evidence["session_consumed_quantity"])

    def test_session_consumption_blocks_when_participation_is_fully_used(self):
        """已消耗量吃掉全部参与额度时，本事件必须零成交（不能超买）。"""
        _, context = self._facts(liquidity=10_000, session_consumed=100)
        result = EP.evaluate_simulated_execution(self._intent(qty=100), context)
        self.assertFalse(result.executable_now)
        self.assertEqual(0, result.fill_quantity)
        self.assertEqual(
            "participation_exhausted", result.liquidity_evidence["capped_by"],
        )

    def test_stale_market_evidence_blocks_fill(self):
        _, context = self._facts(execution_asof=f"{self.DAY} 10:25:00")
        result = EP.evaluate_simulated_execution(self._intent(), context)
        self.assertFalse(result.executable_now)
        self.assertEqual(0, result.fill_quantity)
        self.assertIn(EP.ExecutionReason.MARKET_STALE.value, result.reasons)

    def test_historical_execution_rejects_a_quote_from_a_later_session(self):
        # 历史执行必须保持请求的交易日；不能把之后的行情日期当作执行日回退。
        later_quote = {
            "code": "600901", "price": 20.0, "amount": 50_000_000.0,
            "quote_at": "2026-09-09 10:00:00", "quote_source": "live",
            "quote_validation": "cross_source_checked",
        }
        reading = EP.market_reading_for_execution(
            later_quote, asof_day="2026-09-08",
            execution_asof="2026-09-09 10:00:00",
        )
        self.assertNotEqual("fresh", reading.status)
        self.assertIn(reading.reason, {"asof_mismatch", "asof_unprovable", "stale"})

    def test_single_source_evidence_blocks_fill(self):
        _, context = self._facts(verification="range_timestamp_checked")
        result = EP.evaluate_simulated_execution(self._intent(), context)
        self.assertIn(EP.ExecutionReason.MARKET_UNVERIFIED.value, result.reasons)

    def test_midday_break_and_closed_session_block_fill(self):
        _, lunch = self._facts(
            quote_at=f"{self.DAY} 12:00:00", execution_asof=f"{self.DAY} 12:00:00",
        )
        _, closed = self._facts(
            quote_at=f"{self.DAY} 15:00:00", execution_asof=f"{self.DAY} 15:00:00",
        )
        for context in (lunch, closed):
            result = EP.evaluate_simulated_execution(self._intent(), context)
            self.assertIn(EP.ExecutionReason.OUT_OF_SESSION.value, result.reasons)

    def test_t1_and_locked_limit_facts_block_the_relevant_side(self):
        _, t1 = self._facts(sellable=0)
        t1_result = EP.evaluate_simulated_execution(self._intent(side="sell"), t1)
        self.assertFalse(t1_result.executable_now)
        self.assertEqual(0, t1_result.fill_quantity)
        self.assertIn(EP.ExecutionReason.T1_NOT_SELLABLE.value, t1_result.reasons)
        _, locked = self._facts(can_buy=False, buy_reason="buy_limit_locked")
        locked_result = EP.evaluate_simulated_execution(self._intent(), locked)
        self.assertIn(EP.ExecutionReason.PRICE_LIMIT_LOCKED.value, locked_result.reasons)

    def test_invalid_lot_and_cancelled_order_cannot_fill(self):
        _, context = self._facts()
        invalid = EP.evaluate_simulated_execution(self._intent(qty=150), context)
        self.assertIn(EP.ExecutionReason.INVALID_QUANTITY.value, invalid.reasons)
        _, cancelled = self._facts(status="cancelled")
        result = EP.evaluate_simulated_execution(self._intent(), cancelled)
        self.assertFalse(result.executable_now)
        self.assertIn(EP.ExecutionReason.CANCELLED.value, result.reasons)

    def test_limit_order_respects_slippage_adjusted_price(self):
        _, context = self._facts()
        result = EP.evaluate_simulated_execution(
            self._intent(order_type="limit", reference_price=20.00), context,
        )
        self.assertIn(EP.ExecutionReason.LIMIT_PRICE_NOT_REACHED.value, result.reasons)


class SeatReserveGateTests(_StubbedPlannerTest):
    def _gate(self, requester, pool, pool_limit, *, interest=0, now=NOW, raises=False):
        conn = _FakeConn(interest=interest, raises=raises)
        with mock.patch.object(EP, "_pt", lambda: _pt_stub(now=now)):
            return EP.seat_reserve_gate(conn, requester, pool, pool_limit, dt.date(2026, 9, 8))

    def test_owner_itself_is_never_blocked(self):
        self.assertFalse(self._gate("main_force_top10", set(), 6)["reserved"])

    def test_owner_already_holding_releases_the_reservation(self):
        pool = {("main_force_top10", "601899")}
        self.assertFalse(self._gate("tq_breakout", pool, 6)["reserved"])

    def test_reserved_when_pool_is_one_seat_from_full_and_owner_has_interest(self):
        pool = {("tq_breakout", "002241"), ("sector_rotation", "300750")}
        detail = self._gate("reported_profit_breakout", pool, 3, interest=2)
        self.assertTrue(detail["reserved"])
        self.assertEqual(2, detail["interest"])
        self.assertEqual("main_force_top10", detail["owner"])

    def test_not_reserved_when_pool_has_room(self):
        pool = {("tq_breakout", "002241")}
        self.assertFalse(self._gate("reported_profit_breakout", pool, 6)["reserved"])

    def test_not_reserved_when_owner_has_no_pending_candidates(self):
        pool = {("tq_breakout", "002241"), ("sector_rotation", "300750")}
        self.assertFalse(self._gate("reported_profit_breakout", pool, 3, interest=0)["reserved"])

    def test_not_reserved_after_the_release_deadline(self):
        pool = {("tq_breakout", "002241"), ("sector_rotation", "300750")}
        detail = self._gate("reported_profit_breakout", pool, 3, interest=2, now=LATE)
        self.assertFalse(detail["reserved"])

    def test_query_failure_keeps_the_reservation_fail_closed(self):
        pool = {("tq_breakout", "002241"), ("sector_rotation", "300750")}
        detail = self._gate("reported_profit_breakout", pool, 3, raises=True)
        self.assertTrue(detail["reserved"])
        self.assertEqual(1, detail["interest"])


class SharedGateTests(_StubbedPlannerTest):
    def test_capacity_gate_reports_strategy_and_pool_limits(self):
        result = EP.capacity_gate(
            code="000002", account_id="tq_breakout",
            open_codes={"002241"}, committed_open_codes={"002241", "300750", "600519"},
            pool_open_positions={("tq_breakout", "002241"), ("sector_rotation", "300750")},
            position_limit=3, pool_limit=6, allocation_source="test",
        )
        self.assertIn("策略持仓及待成交席位已达动态上限 3/3", result["reasons"])
        self.assertEqual(3, result["gate"]["limit"])
        self.assertFalse(result["reasons"] and "共享硬上限" in result["reasons"][0])

    def test_capacity_gate_reports_shared_pool_hard_limit(self):
        pool = {(f"acct{i}", f"{60000 + i}") for i in range(6)}
        result = EP.capacity_gate(
            code="002241", account_id="tq_breakout",
            open_codes=set(), committed_open_codes=set(),
            pool_open_positions=pool, position_limit=3, pool_limit=6,
        )
        self.assertTrue(any("共享硬上限 6/6" in item for item in result["reasons"]))

    def test_capacity_gate_reports_the_reserved_last_seat(self):
        pool = {("tq_breakout", "002241"), ("sector_rotation", "300750"), ("trend_pullback", "600519")}
        with mock.patch.object(EP, "_pt", lambda: _pt_stub()):
            result = EP.capacity_gate(
                code="000651", account_id="reported_profit_breakout",
                open_codes=set(), committed_open_codes=set(),
                pool_open_positions=pool, position_limit=3, pool_limit=4,
                asof_day=dt.date(2026, 9, 8), conn=_FakeConn(interest=1),
            )
        self.assertTrue(result["gate"]["seat_reserved"])
        self.assertTrue(any("共享池仅剩最后 1 席" in item for item in result["reasons"]))

    def test_market_gate_blocks_red_and_unknown_with_the_declared_reason(self):
        for light in ("red", "unknown"):
            with self.subTest(light=light):
                gate = EP.market_gate({"light": light}, "reported_profit_breakout")
                self.assertTrue(gate["blocked"])
                self.assertEqual(
                    EP.policy_for("reported_profit_breakout").red_light_reason, gate["reason"])
        self.assertFalse(EP.market_gate({"light": "green"}, "tq_breakout")["blocked"])

    def test_account_risk_gate_only_reports_when_blocked(self):
        self.assertEqual([], EP.account_risk_gate({"blocked": False, "reasons": ["x"]}))
        self.assertEqual(["熔断"], EP.account_risk_gate({"blocked": True, "reasons": ["熔断"]}))

    def test_cash_gate_accounts_for_pending_reservations(self):
        stub = types.SimpleNamespace(**vars(_pt_stub()), _pending_buy_reservations=lambda conn, exclude_order_key=None: (None, 1000.0),
            _shared_cash=lambda conn: 5000.0,
        )
        with mock.patch.object(EP, "_pt", lambda: stub):
            short = EP.cash_gate(None, "buy", 4500.0, 10.0)
            ok = EP.cash_gate(None, "buy", 3000.0, 10.0)
            sell = EP.cash_gate(None, "sell", 99999.0, 10.0)
        self.assertFalse(short["allowed"])
        self.assertIn("待成交买单预占", short["reason"])
        self.assertTrue(ok["allowed"])
        self.assertTrue(sell["allowed"])

    def test_quote_and_security_gates_reuse_the_shared_implementation(self):
        stub = types.SimpleNamespace(**vars(_pt_stub()), _execution_quote_status=lambda quote, day, purpose="entry": {
                "fresh": False, "reason": "行情过旧"},
            _security_scope=lambda code, name=None, risk_flag=None: {
                "allowed": False, "reason": "不在证券范围"},
        )
        with mock.patch.object(EP, "_pt", lambda: stub):
            self.assertFalse(EP.quote_gate({}, dt.date(2026, 9, 8))["fresh"])
            self.assertFalse(EP.security_gate("600000")["allowed"])
            self.assertEqual("不在证券范围", EP.security_gate("600000")["reason"])


class CommitFillTests(_StubbedPlannerTest):
    def test_buy_commit_reserves_debits_and_writes_the_fill(self):
        calls = []
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: calls.append(("lease", label)),
            _reserve_shared_capital=lambda conn, order_id, account_id, code, amount, fees,
            expected_cycle_id=None: (
                calls.append(("reserve", code, amount, expected_cycle_id)), (True, None))[1],
            _debit_shared_cash=lambda conn, value, preferred_account_id=None: calls.append(
                ("debit", round(value, 2))),
            _finish_capital_reservation=lambda conn, order_id, status: calls.append(
                ("reservation", status)),
            _record_lot=lambda conn, account, plan, qty, price, day, order_id, is_t_base=True, fees=0.0,
            cycle_id=None, acquired_at=None: calls.append(("lot", qty, cycle_id)),
            _consume_available_lots=lambda conn, account_id, code, qty, day, cycle_id=None: (
                calls.append(("consume_cycle", cycle_id)), (qty, 0.0))[1],
            _credit_shared_cash=lambda conn, value, account_id=None: calls.append(("credit", value)),
            _json=lambda value: value,
            _now=lambda: NOW,
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *args, **kwargs: calls.append(("risk_log", args[5])),
            _audit=lambda *args, **kwargs: calls.append(("audit", args[2], args[3])),
            _sync_positions=lambda conn, account_id, day: calls.append(("sync",)),
            **_cycle_stub_kwargs(),
        )
        conn = _FakeConn(
            order_row=_order_row(),
            fill_rows=[_fill_row()],
        )
        plan = {
            "side": "buy", "code": "002241", "qty": 100, "amount": 2150.0,
            "fees": 2.15, "fill_price": 21.5, "quote_at": "2026-09-08T10:00:00",
            "risk": {"x": 1},
        }
        with mock.patch.object(EP, "_pt", lambda: stub):
            EP.commit_fill(
                conn, account={"id": "tq_breakout"}, plan=plan, order_id=7,
                asof_day=dt.date(2026, 9, 8), reserved=False, action="strategy_buy",
                reason="突破买入", detail={"x": 1},
                execution_context=_valid_execution_context(),
            )
        kinds = [item[0] for item in calls]
        self.assertIn("reserve", kinds)
        self.assertIn("debit", kinds)
        # 预留必须先于扣款，成交后才消费预占。
        self.assertLess(kinds.index("reserve"), kinds.index("debit"))
        self.assertLess(kinds.index("debit"), kinds.index("lot"))
        # §20–§22：预占必须显式带上订单的周期，否则「预占周期 == 订单周期」
        # 只能靠预占层自己解析 active cycle（那正是要消灭的隐式来源）。
        reserve_call = [item for item in calls if item[0] == "reserve"][0]
        self.assertEqual(reserve_call[3], ORDER_CYCLE,
                         "预占必须携带订单的 durable 周期")
        self.assertIn(("reservation", "consumed"), calls)
        self.assertEqual(2152.22, [item for item in calls if item[0] == "debit"][0][1])
        self.assertIn(("audit", "strategy_buy"), [(c[0], c[1]) for c in calls if c[0] == "audit"])

    def test_manual_commit_does_not_reserve_again(self):
        calls = []
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: None,
            _reserve_shared_capital=lambda *args, **kwargs: calls.append(("reserve",)) or (True, None),
            _debit_shared_cash=lambda conn, value, preferred_account_id=None: calls.append(("debit",)),
            _finish_capital_reservation=lambda conn, order_id, status: calls.append(("reservation", status)),
            _consume_capital_reservation=lambda conn, order_id, amount, fees, final=False: calls.append(
                ("consume_reservation", amount, fees, final)),
            _record_lot=lambda *args, **kwargs: calls.append(("lot",)),
            _consume_available_lots=lambda conn, account_id, code, qty, day, cycle_id=None: (
                None or (qty, 0.0)),
            **_cycle_stub_kwargs(),
            _credit_shared_cash=lambda *args, **kwargs: calls.append(("credit",)),
            _json=lambda value: value,
            _now=lambda: NOW,
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *args, **kwargs: calls.append(("risk_log", args[5])),
            _audit=lambda *args, **kwargs: calls.append(("audit", args[2])),
            _sync_positions=lambda conn, account_id, day: None,
        )
        plan = {
            "side": "buy", "code": "002241", "qty": 100, "amount": 1000.0,
            "fees": 1.0, "fill_price": 10.0, "quote_at": "2026-09-08T10:00:00", "risk": {},
        }
        with mock.patch.object(EP, "_pt", lambda: stub):
            EP.commit_fill(
                _FakeConn(order_row=_order_row(id=9), fill_rows=[_fill_row(order_id=9)]),
                account={"id": "tq_breakout"}, plan=plan, order_id=9,
                asof_day=dt.date(2026, 9, 8), reserved=True,
                risk_log_reason="手动模拟委托通过模型门禁并成交",
                reason="手动模拟委托经模型复核后成交",
                execution_context=_valid_execution_context(quote_at=NOW),
            )
        self.assertNotIn("reserve", [item[0] for item in calls])
        self.assertIn(("risk_log", "手动模拟委托通过模型门禁并成交"), calls)

    def test_commit_fill_logs_each_event_exactly_once(self):
        # 回归护栏：成交的 risk/audit 事件由 commit_fill 独占写入。
        calls = []
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: None,
            _reserve_shared_capital=lambda *args, **kwargs: (True, None),
            _debit_shared_cash=lambda conn, value, preferred_account_id=None: None,
            _finish_capital_reservation=lambda conn, order_id, status: None,
            _record_lot=lambda *args, **kwargs: None,
            _consume_available_lots=lambda conn, account_id, code, qty, day, cycle_id=None: (qty, 0.0),
            _credit_shared_cash=lambda *args, **kwargs: None,
            _json=lambda value: value,
            _now=lambda: NOW,
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *args, **kwargs: calls.append(("risk_log",)),
            _audit=lambda *args, **kwargs: calls.append(("audit",)),
            _sync_positions=lambda conn, account_id, day: None,
            **_cycle_stub_kwargs(),
        )
        plan = {
            "side": "buy", "code": "002241", "qty": 100, "amount": 1000.0,
            "fees": 1.0, "fill_price": 10.0, "quote_at": "2026-09-08T10:00:00",
        }
        with mock.patch.object(EP, "_pt", lambda: stub):
            EP.commit_fill(
                _FakeConn(order_row=_order_row(id=11), fill_rows=[_fill_row(order_id=11)]),
                account={"id": "tq_breakout"}, plan=plan, order_id=11,
                asof_day=dt.date(2026, 9, 8), reserved=False, action="strategy_buy",
                execution_context=_valid_execution_context(quote_at=NOW),
            )
        self.assertEqual(1, [item[0] for item in calls].count("risk_log"))
        self.assertEqual(1, [item[0] for item in calls].count("audit"))

    def test_auxiliary_buy_does_not_duplicate_commit_logging(self):
        # 成功路径的 risk/audit 由 planner 写入；调用方只保留失败分支的一次记录。
        with open(os.path.join(BACKEND_DIR, "manual_orders.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(r"^def _commit_strategy_buy\(", source, re.M)
        self.assertIsNotNone(match)
        start = match.start()
        nxt = re.search(r"^def ", source[start + 1:], re.M)
        body = source[start:start + 1 + (nxt.start() if nxt else len(source))]
        self.assertIn("EP.commit_fill(", body)
        self.assertEqual(1, body.count("_risk_log("))
        self.assertEqual(0, body.count("_audit("))

    def test_sell_commit_requires_the_full_available_quantity(self):
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: None,
            _reserve_shared_capital=lambda *args, **kwargs: (True, None),
            _debit_shared_cash=lambda *args, **kwargs: None,
            _finish_capital_reservation=lambda *args, **kwargs: None,
            _record_lot=lambda *args, **kwargs: None,
            _consume_available_lots=lambda conn, account_id, code, qty, day, cycle_id=None: (qty - 100, 0.0),
            _credit_shared_cash=lambda *args, **kwargs: None,
            _json=lambda value: value,
            _now=lambda: NOW,
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *args, **kwargs: None,
            _audit=lambda *args, **kwargs: None,
            _sync_positions=lambda *args, **kwargs: None,
            **_cycle_stub_kwargs(),
        )
        plan = {"side": "sell", "code": "002241", "qty": 200, "amount": 4000.0,
                "fees": 4.0, "fill_price": 20.0, "quote_at": None, "risk": {}}
        with mock.patch.object(EP, "_pt", lambda: stub):
            with self.assertRaises(RuntimeError):
                EP.commit_fill(
                    _FakeConn(order_row=_order_row(side="sell", order_type="market"), fill_rows=[]),
                    account={"id": "tq_breakout"}, plan=plan,
                    order_id=11, asof_day=dt.date(2026, 9, 8), reserved=True,
                    execution_context=_valid_execution_context(
                        side="sell", quantity=200, sellable=200,
                    ),
                )


class RevalidateTests(_StubbedPlannerTest):
    def test_revalidate_reuses_the_same_plan_builder_with_the_stored_request(self):
        captured = {}

        def builder(conn, account_id, code, side, qty, order_type, limit_price, day, **kwargs):
            captured.update({
                "account_id": account_id, "code": code, "side": side, "qty": qty,
                "order_type": order_type, "limit_price": limit_price, "day": day,
                "exclude_reservation_key": kwargs.get("exclude_reservation_key"),
                "quote": kwargs.get("quote"),
            })
            return {"allowed": True, "reasons": []}

        order = {
            "id": 42, "account_id": "tq_breakout", "code": "002241", "side": "BUY",
            "qty": 300, "order_type": "LIMIT", "planned_price": 21.0,
        }
        plan = EP.revalidate_order_plan(
            None, order, plan_builder=builder, asof_day=dt.date(2026, 9, 8), quote={"price": 21.0},
        )
        self.assertEqual({"allowed": True, "reasons": []}, plan)
        self.assertEqual("tq_breakout", captured["account_id"])
        self.assertEqual("buy", captured["side"])
        self.assertEqual("limit", captured["order_type"])
        self.assertEqual(21.0, captured["limit_price"])
        self.assertEqual("42", captured["exclude_reservation_key"])


class PlanEntryTests(_StubbedPlannerTest):
    def test_plan_entry_composes_the_shared_gates_and_reports_the_policy(self):
        stub = types.SimpleNamespace(**vars(_pt_stub()), _execution_quote_status=lambda quote, day, purpose="entry": {"fresh": True},
            _security_scope=lambda code, name=None, risk_flag=None: {"allowed": True},
            _pending_buy_reservations=lambda conn, exclude_order_key=None: (None, 0.0),
            _shared_cash=lambda conn: 100000.0,
        )
        with mock.patch.object(EP, "_pt", lambda: stub), \
                mock.patch.object(EP, "seat_reserve_gate", lambda *a, **k: {"reserved": False}):
            result = EP.plan_entry(
                None,
                account={"id": "reported_profit_breakout"},
                code="000651", side="buy",
                quote={"price": 42.3, "name": "格力电器"},
                asof_day=dt.date(2026, 9, 8),
                market={"light": "green"},
                open_codes=set(), committed_open_codes=set(),
                pool_open_positions=set(), position_limit=5, pool_limit=6,
                risk_state={"blocked": False}, amount=4230.0, fees=4.23,
            )
        self.assertTrue(result["allowed"])
        self.assertEqual([], result["reasons"])
        self.assertTrue(result["requires_manual_entry_review"])
        self.assertEqual("execution-planner-v1", result["policy"]["planner"])

    def test_plan_entry_collects_reasons_from_every_gate(self):
        stub = types.SimpleNamespace(**vars(_pt_stub()), _execution_quote_status=lambda quote, day, purpose="entry": {
                "fresh": False, "reason": "行情过旧"},
            _security_scope=lambda code, name=None, risk_flag=None: {
                "allowed": False, "reason": "不在证券范围"},
            _pending_buy_reservations=lambda conn, exclude_order_key=None: (None, 0.0),
            _shared_cash=lambda conn: 100.0,
        )
        with mock.patch.object(EP, "_pt", lambda: stub), \
                mock.patch.object(EP, "seat_reserve_gate", lambda *a, **k: {"reserved": False}):
            result = EP.plan_entry(
                None, account={"id": "tq_breakout"}, code="002241", side="buy",
                quote={"price": 21.5}, asof_day=dt.date(2026, 9, 8),
                market={"light": "red"},
                open_codes=set(), committed_open_codes=set(),
                pool_open_positions=set(), position_limit=1, pool_limit=1,
                risk_state={"blocked": True, "reasons": ["单日亏损已触发熔断"]},
                amount=99999.0, fees=10.0,
            )
        self.assertFalse(result["allowed"])
        reasons = result["reasons"]
        self.assertIn("不在证券范围", reasons)
        self.assertTrue(any("市场红灯" in item for item in reasons))
        self.assertIn("单日亏损已触发熔断", reasons)
        self.assertTrue(any("共享资金池可用现金不足" in item for item in reasons))
        self.assertTrue(any("成交行情未通过校验" in item for item in reasons))


class ExecutionEstimateTests(unittest.TestCase):
    def test_buy_and_sell_terms_use_one_deterministic_price_and_fee_policy(self):
        buy_price, buy_amount, buy_fees = EP.estimate_execution_terms(10.0, 1000, "buy")
        sell_price, sell_amount, sell_fees = EP.estimate_execution_terms(10.0, 1000, "sell")

        self.assertAlmostEqual(buy_price, 10.01)
        self.assertAlmostEqual(buy_amount, 10010.0)
        self.assertAlmostEqual(buy_fees, 1.001)
        self.assertAlmostEqual(sell_price, 9.99)
        self.assertAlmostEqual(sell_amount, 9990.0)
        self.assertAlmostEqual(sell_fees, 5.994)

    def test_limit_estimate_caps_planning_price_without_promising_a_fill(self):
        price, amount, fees = EP.estimate_execution_terms(
            10.0, 100, "buy", limit_price=10.0,
        )
        self.assertEqual((price, amount), (10.0, 1000.0))
        self.assertAlmostEqual(fees, 0.1)


if __name__ == "__main__":
    unittest.main()
