# -*- coding: utf-8 -*-
"""Central execution planner regression tests (PR-06).

测试全部离线：``execution_planner`` 通过 ``_pt()`` 惰性取用 ``paper_trading``，
这里直接注入替身命名空间，既不依赖真实数据库，也不会被生产 ``data_cache``
污染（与 ``test_ai_controls`` 的隔离思路一致）。
"""
from __future__ import annotations

import datetime as dt
import inspect
import os
import re
import types
import unittest
from unittest import mock

import execution_planner as EP

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
NOW = "2026-09-08 10:00:00"
LATE = "2026-09-08 15:00:00"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return (self._value,)


class _FakeConn:
    """Only records the signal-interest query this planner makes."""

    def __init__(self, interest=0, raises=False):
        self.interest = interest
        self.raises = raises
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, tuple(params)))
        if self.raises:
            raise RuntimeError("db unavailable")
        return _FakeResult(self.interest)


def _pt_stub(now=NOW, statuses=("pending", "waitlist", "retry")):
    """paper_trading 替身：只提供 planner 真正用到的成员。"""
    return types.SimpleNamespace(
        _now=lambda: now,
        ENTRY_RETRY_SIGNAL_STATUSES=statuses,
        MAIN_FORCE_STRATEGY_ID="main_force_top10",
        NEW_STRATEGY_ID="reported_profit_breakout",
        _num=lambda value, default=0.0: default if value in (None, "") else float(value),
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


class CommitFillTests(_StubbedPlannerTest):
    def test_buy_commit_reserves_debits_and_writes_the_fill(self):
        calls = []
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: calls.append(("lease", label)),
            _reserve_shared_capital=lambda conn, order_id, account_id, code, amount, fees: (
                calls.append(("reserve", code, amount)), (True, None))[1],
            _debit_shared_cash=lambda conn, value, preferred_account_id=None: calls.append(
                ("debit", round(value, 2))),
            _finish_capital_reservation=lambda conn, order_id, status: calls.append(
                ("reservation", status)),
            _record_lot=lambda conn, account, plan, qty, price, day, order_id, is_t_base=True, fees=0.0: calls.append(
                ("lot", qty)),
            _consume_available_lots=lambda conn, account_id, code, qty, day: (qty, 0.0),
            _credit_shared_cash=lambda conn, value, account_id=None: calls.append(("credit", value)),
            _json=lambda value: value,
            _now=lambda: NOW,
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *args, **kwargs: calls.append(("risk_log", args[5])),
            _audit=lambda *args, **kwargs: calls.append(("audit", args[2], args[3])),
            _sync_positions=lambda conn, account_id, day: calls.append(("sync",)),
        )
        conn = _FakeConn()
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
            )
        kinds = [item[0] for item in calls]
        self.assertIn("reserve", kinds)
        self.assertIn("debit", kinds)
        # 预留必须先于扣款，成交后才消费预占。
        self.assertLess(kinds.index("reserve"), kinds.index("debit"))
        self.assertLess(kinds.index("debit"), kinds.index("lot"))
        self.assertIn(("reservation", "consumed"), calls)
        self.assertEqual(2152.15, [item for item in calls if item[0] == "debit"][0][1])
        self.assertIn(("audit", "strategy_buy"), [(c[0], c[1]) for c in calls if c[0] == "audit"])

    def test_manual_commit_does_not_reserve_again(self):
        calls = []
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: None,
            _reserve_shared_capital=lambda *args, **kwargs: calls.append(("reserve",)) or (True, None),
            _debit_shared_cash=lambda conn, value, preferred_account_id=None: calls.append(("debit",)),
            _finish_capital_reservation=lambda conn, order_id, status: calls.append(("reservation", status)),
            _record_lot=lambda *args, **kwargs: calls.append(("lot",)),
            _consume_available_lots=lambda conn, account_id, code, qty, day: (qty, 0.0),
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
                _FakeConn(), account={"id": "tq_breakout"}, plan=plan, order_id=9,
                asof_day=dt.date(2026, 9, 8), reserved=True,
                risk_log_reason="手动模拟委托通过模型门禁并成交",
                reason="手动模拟委托经模型复核后成交",
            )
        self.assertNotIn("reserve", [item[0] for item in calls])
        self.assertIn(("risk_log", "手动模拟委托通过模型门禁并成交"), calls)

    def test_sell_commit_requires_the_full_available_quantity(self):
        stub = types.SimpleNamespace(
            _assert_active_lease=lambda conn, label: None,
            _reserve_shared_capital=lambda *args, **kwargs: (True, None),
            _debit_shared_cash=lambda *args, **kwargs: None,
            _finish_capital_reservation=lambda *args, **kwargs: None,
            _record_lot=lambda *args, **kwargs: None,
            _consume_available_lots=lambda conn, account_id, code, qty, day: (qty - 100, 0.0),
            _credit_shared_cash=lambda *args, **kwargs: None,
            _json=lambda value: value,
            _now=lambda: NOW,
            _date=lambda day: day,
            _num=lambda value, default=0.0: default if value in (None, "") else float(value),
            _risk_log=lambda *args, **kwargs: None,
            _audit=lambda *args, **kwargs: None,
            _sync_positions=lambda *args, **kwargs: None,
        )
        plan = {"side": "sell", "code": "002241", "qty": 200, "amount": 4000.0,
                "fees": 4.0, "fill_price": 20.0, "quote_at": None, "risk": {}}
        with mock.patch.object(EP, "_pt", lambda: stub):
            with self.assertRaises(RuntimeError):
                EP.commit_fill(_FakeConn(), account={"id": "tq_breakout"}, plan=plan,
                               order_id=11, asof_day=dt.date(2026, 9, 8), reserved=True)


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
