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
    """A filled buy order row as `paper_orders` would return it."""
    row = {
        "id": 7, "account_id": "tq_breakout", "signal_id": None, "side": "buy",
        "code": "002241", "name": None, "qty": 100, "planned_price": 21.5,
        "filled_price": 21.5, "amount": 2150.0, "fees": 2.15, "status": "filled",
        "reason": "\u7a81\u7834\u4e70\u5165", "risk_payload": "{}", "realized_pnl": None,
        "created_at": NOW, "executed_at": NOW, "order_type": "limit",
        "origin": "strategy", "expires_at": None, "cancelled_at": None,
        "strategy_id": None, "strategy_version": None, "strategy_checksum": None,
        "retry_of_order_id": None, "cycle_id": ORDER_CYCLE,
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


class ExecutionAuthorityContractTests(unittest.TestCase):
    """R26 exports one execution authority and removes the old direct-fill API."""

    def test_execution_authority_is_the_only_exported_fill_owner(self):
        self.assertTrue(callable(EP.execute_order))
        self.assertIn("execute_order", EP.__all__)
        self.assertNotIn("commit_fill", EP.__all__)
        self.assertFalse(hasattr(EP, "commit_fill"))


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


if __name__ == "__main__":
    unittest.main()
