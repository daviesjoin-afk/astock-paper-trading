# -*- coding: utf-8 -*-
"""R27-B2C-4B —— portfolio/accounting owner fact contract 的永久回归。

存在理由是这条不变量：

    **portfolio/accounting owner 只能发布它自己**能证明**的记账事实
    （cash / realized_pnl / position_cost_summary），全部以 cycle + account + asof 定界；
    它**不能**把调用方提供的裸市场价格升级成自己的 verified 事实。**

分四组：

    PFACT-01 ~ 03  契约形状：无 public raw 构造器、kind/status 是闭集、
                   cycle/account/asof 由 owner context 派生（零 fallback）
    PFACT-04 ~ 12  事实内容：现金来自 bounded 重建（不是当前账户余额）、
                   已实现盈亏只认已验证 SELL、不存在/未挂载账户绝不变成 verified zero、
                   持仓成本来自 durable lots（不是 paper_positions）、
                   数量未证明 → unknown/None、未来成交不进事实、archived 或不可证明
                   context 全部 fail closed、非有限账本值不得成为 verified fact、
                   **已验证的零**不得被误判成 unknown
    PFACT-13       固定且确定性的发布顺序（cash → realized_pnl → position_cost_summary）
    PFACT-14 ~ 15  权威边界：投影**没有** NAV / market_value / unrealized / daily_return /
                   quote_status 表面；factory **不接受** valuations / current quote /
                   latest 之类的 fallback 入口

全部离线：临时 SQLite 账本 + owner 自己的 public read，不连真实库、不读墙钟。
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_portfolio_read_model as P  # noqa: E402
import paper_trading as PT  # noqa: E402

ACCOUNT = next(iter(PT.ACCOUNT_SPECS))
CODE = "600519"
OTHER_CODE = "600000"
DAY = dt.date(2026, 9, 20)
NEXT = DAY + dt.timedelta(days=1)

#: 本轮投影**禁止**出现的表面 —— 它们是跨 owner 组合事实（组合账本 + R24 估值），
#: 不是 portfolio-only 事实。
FORBIDDEN_SURFACE = (
    "nav", "latest_nav", "prior_nav", "daily_pnl", "daily_return",
    "market_value", "unrealized_pnl", "benchmark", "quote_status",
    "quote", "valuation", "price",
)


def _executable_source(func) -> str:
    """模块级函数的**会执行**代码，docstring 已剥掉。

    边界断言必须只看代码：本模块的 docstring 刻意写清"为什么不发布 NAV"，
    字符级子串搜索会把说明文字本身当成越界证据。
    """
    tree = ast.parse(inspect.getsource(func))
    node = tree.body[0]
    body = list(node.body)
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


class PortfolioFactContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patchers = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._patchers:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle = self._cycle("r27b2c4b-c1", "running")
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle, ACCOUNT)
        )
        self.conn.commit()
        self._attach(ACCOUNT)
        self.initial_cash = float(self.conn.execute(
            "SELECT initial_cash FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0])

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self.tmp.cleanup()

    # ---------- fixtures ----------

    def _cycle(self, key, status, capital=100000.0, created=DAY):
        stamp = f"{created.isoformat()} 09:00:00"
        return int(self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (key, status, capital, "shared_pool", stamp, stamp,
             stamp if status == "running" else None),
        ).lastrowid)

    def _attach(self, account_id=ACCOUNT, *, effective=DAY, cycle=None):
        """写入 account 属于该 cycle 的**有界挂载证据**（parameter version）。"""
        day = effective.isoformat()
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,"
            "reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cycle or self.cycle, account_id, "1", "default", "{}", "r27b2c4b",
             day, f"{day} 09:00:00"),
        )
        self.conn.commit()

    def _order_and_fill(self, *, cycle_id, side, qty, price, fill_date, code=CODE,
                        verified=True, realized_pnl=None, fees=5.0):
        amount = qty * price
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        order_id = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified,realized_pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, side, code, "测试股", qty, price, price, amount, fees, "filled",
             "r27b2c4b-test", "{}", f"{fill_date} 09:30:00", f"{fill_date} 09:30:01",
             "market", "seed", *stamp, cycle_id,
             "verified" if verified else "unknown", 1 if verified else 0, realized_pnl),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, ACCOUNT, side, code, qty, price, amount, fees, fill_date,
             f"{fill_date} 09:30:00", "r27b2c4b-test"),
        )
        return order_id

    def _lot(self, cycle_id, qty, cost, *, acquired_at=None, remaining_qty=None,
             source_order_id=None, account_id=ACCOUNT, code=CODE):
        acquired_at = acquired_at or f"{DAY.isoformat()} 10:00:00"
        remaining_qty = qty if remaining_qty is None else remaining_qty
        return int(self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, account_id, code, "测试股", "测试", qty, remaining_qty, cost,
             acquired_at, NEXT.isoformat(), "stock_t1", source_order_id, 1, 1),
        ).lastrowid)

    def _facts(self, *, asof=DAY, account=ACCOUNT, cycle=None):
        context = P.PortfolioReadContext(cycle or self.cycle, asof)
        return P.accounting_fact_projections(self.conn, context, account_id=account)

    def _issue(self, **overrides):
        """直接走 owner 的私有签发口 —— 用来证明非法形状**在构造期**就被拒绝。"""
        fields = {
            "version": P.PORTFOLIO_FACT_CONTRACT_VERSION,
            "fact_kind": P.PORTFOLIO_FACT_CASH,
            "cycle_id": self.cycle,
            "account_id": ACCOUNT,
            "asof_day": DAY.isoformat(),
            "status": P.STATUS_VERIFIED,
            "value": 1.0,
        }
        fields.update(overrides)
        return P._issue_portfolio_fact_projection(**fields)

    # ---------- PFACT-01 ~ 03：契约形状 ----------

    def test_PFACT_01_public_projection_constructor_is_rejected(self):
        """PFACT-01：``PortfolioFactProjection(...)`` 公共直接构造被拒绝。

        否则任何调用方都能自述种类 / 身份 / 业务日 / 核验结论 / 值，然后声称"这是一条
        portfolio owner 事实" —— "唯一发布入口"就只是一句声明。
        """
        with self.assertRaises(TypeError):
            P.PortfolioFactProjection(
                version=P.PORTFOLIO_FACT_CONTRACT_VERSION,
                fact_kind=P.PORTFOLIO_FACT_CASH,
                cycle_id=self.cycle, account_id=ACCOUNT, asof_day=DAY.isoformat(),
                status=P.STATUS_VERIFIED, value=1.0,
            )
        # 非空性：真的投影只能由 owner 的 public read 产出（否则上面只是"构造器恒抛"）。
        facts = self._facts()
        self.assertEqual(3, len(facts))
        for fact in facts:
            self.assertIs(type(fact), P.PortfolioFactProjection)
        # 私有签发口也不是"随便传就通过"：unknown 带值必须被拒绝。
        with self.assertRaises(P.PortfolioFactContractError) as caught:
            self._issue(status=P.STATUS_UNKNOWN, value=123.45)
        self.assertEqual("unknown_fact_with_value", caught.exception.reason)

    def test_PFACT_02_fact_kind_and_status_are_closed_sets(self):
        """PFACT-02：fact kind / status 是 owner 的闭集，非法值构造期 fail closed。"""
        self.assertEqual(
            ("cash", "realized_pnl", "position_cost_summary"), P.PORTFOLIO_FACT_KINDS,
        )
        self.assertEqual(("verified", "unknown"), P.PORTFOLIO_FACT_STATUSES)
        for fact in self._facts():
            self.assertIn(fact.fact_kind, P.PORTFOLIO_FACT_KINDS)
            self.assertIn(fact.status, P.PORTFOLIO_FACT_STATUSES)

        for overrides, reason in (
            ({"fact_kind": "nav"}, "unknown_fact_kind"),
            ({"fact_kind": "market_value"}, "unknown_fact_kind"),
            ({"status": "source_unusable"}, "unknown_fact_status"),
            ({"version": "portfolio-fact-v2"}, "version_mismatch"),
            ({"cycle_id": 0}, "alien_cycle_id"),
            ({"cycle_id": True}, "alien_cycle_id"),
            ({"account_id": ""}, "alien_account_id"),
            ({"asof_day": "2026-09-20 00:00:00"}, "alien_asof_day"),
            ({"asof_day": "not-a-day"}, "alien_asof_day"),
            ({"value": float("inf")}, "non_finite_fact_value"),
            ({"value": float("nan")}, "non_finite_fact_value"),
            ({"value": True}, "field_not_a_finite_number"),
            ({"value": "1.0"}, "field_not_a_finite_number"),
            (
                {"fact_kind": P.PORTFOLIO_FACT_POSITION_COST_SUMMARY, "value": None},
                "field_not_a_position_cost_summary",
            ),
            (
                {"fact_kind": P.PORTFOLIO_FACT_POSITION_COST_SUMMARY, "value": 1.0},
                "field_not_a_position_cost_summary",
            ),
        ):
            with self.subTest(overrides={k: repr(v) for k, v in overrides.items()}):
                with self.assertRaises(P.PortfolioFactContractError) as caught:
                    self._issue(**overrides)
                self.assertEqual(reason, caught.exception.reason)
        # 非空性对照：合法形状必须能签发（否则上面可能只是因为签发口恒抛）。
        self.assertEqual(P.STATUS_VERIFIED, self._issue().status)

    def test_PFACT_03_cycle_account_asof_are_owner_context_derived(self):
        """PFACT-03：cycle / account / asof 全部由 owner context 派生，零 fallback。

        ``accounting_fact_projections`` 的签名里**没有** ``asof`` / ``cycle`` /
        ``valuations`` / ``latest`` / ``today`` 之类的自述或回落入口；``account_id``
        是必须显式给出的 keyword-only 参数；不是 ``PortfolioReadContext`` 的输入被拒绝。
        """
        signature = inspect.signature(P.accounting_fact_projections)
        self.assertEqual(["conn", "context", "account_id"], list(signature.parameters))
        account_parameter = signature.parameters["account_id"]
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, account_parameter.kind)
        self.assertIs(inspect.Parameter.empty, account_parameter.default)
        for forbidden in ("asof", "asof_day", "cycle", "cycle_id", "valuations",
                          "valuation", "quote", "latest", "today", "active"):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, signature.parameters)

        # account_id 必须显式且非空 —— 不接受 active account fallback。
        for empty in ("", "   ", None):
            with self.subTest(account=repr(empty)):
                with self.assertRaises(ValueError):
                    P.accounting_fact_projections(
                        self.conn, P.PortfolioReadContext(self.cycle, DAY),
                        account_id=empty,
                    )
        # cycle / asof 不得由调用方自述：非 context 输入被拒绝。
        with self.assertRaises(TypeError):
            P.accounting_fact_projections(
                self.conn, {"cycle_id": self.cycle, "asof_day": DAY.isoformat()},
                account_id=ACCOUNT,
            )
        # context 自己要求显式 cycle + asof（不接受 None）。
        for kwargs in ({"cycle_id": None, "asof_day": DAY},
                       {"cycle_id": self.cycle, "asof_day": None}):
            with self.subTest(context=kwargs):
                with self.assertRaises(ValueError):
                    P.PortfolioReadContext(**kwargs)

        for fact in self._facts():
            self.assertEqual(self.cycle, fact.cycle_id)
            self.assertEqual(ACCOUNT, fact.account_id)
            self.assertEqual(DAY.isoformat(), fact.asof_day)
        # 另一个业务日 → 另一条身份（asof 真的来自 context，不是常量）。
        self.assertEqual(NEXT.isoformat(), self._facts(asof=NEXT)[0].asof_day)
        self.assertEqual(P.PORTFOLIO_FACT_KINDS[0], self._facts(asof=NEXT)[0].fact_kind)

    # ---------- PFACT-04 ~ 12：事实内容 ----------

    def test_PFACT_04_cash_is_bounded_reconstruction_not_current_account_state(self):
        """PFACT-04：现金来自 bounded 重建，``paper_accounts.cash`` 当前值不能覆盖它。

        ``paper_accounts.cash`` 是**当前可变状态**，不是历史 as-of authority。改动它
        不得改变一条历史记账事实的值。
        """
        self.conn.execute(
            "UPDATE paper_accounts SET cash=? WHERE id=?", (999999.0, ACCOUNT)
        )
        self.conn.commit()
        cash = self._facts()[0]
        self.assertEqual(P.PORTFOLIO_FACT_CASH, cash.fact_kind)
        self.assertEqual(P.STATUS_VERIFIED, cash.status)
        self.assertEqual(self.initial_cash, cash.value)
        self.assertNotEqual(999999.0, cash.value)

        # 当前状态再改一次：历史事实必须一字不变。
        self.conn.execute("UPDATE paper_accounts SET cash=? WHERE id=?", (1.0, ACCOUNT))
        self.conn.commit()
        self.assertEqual(self.initial_cash, self._facts()[0].value)
        # 反过来也要成立：一笔**已验证的 BUY** 会改变 bounded 重建（说明这不是常量）。
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        self.conn.commit()
        self.assertEqual(self.initial_cash - 1005.0, self._facts()[0].value)

    def test_PFACT_05_realized_pnl_consumes_only_bounded_verified_sell_evidence(self):
        """PFACT-05：已实现盈亏只统计 as-of 之前**已验证** SELL 的 owner 事实。

        出现一笔未验证 SELL 时必须整体 fail closed（``unknown`` / ``None``），
        而不是"只把已验证的那几笔加起来" —— 后者会把一条被污染的账本发布成确定数字。
        """
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        self._order_and_fill(cycle_id=self.cycle, side="sell", qty=30, price=12.0,
                             fill_date=DAY.isoformat(), realized_pnl=59.5)
        self._order_and_fill(cycle_id=self.cycle, side="sell", qty=20, price=12.0,
                             fill_date=DAY.isoformat(), realized_pnl=39.5)
        self.conn.commit()
        realized = self._facts()[1]
        self.assertEqual(P.PORTFOLIO_FACT_REALIZED_PNL, realized.fact_kind)
        self.assertEqual(P.STATUS_VERIFIED, realized.status)
        self.assertEqual(99.0, realized.value)

        # as-of 之后的 SELL 不进历史事实。
        self._order_and_fill(cycle_id=self.cycle, side="sell", qty=10, price=12.0,
                             fill_date=NEXT.isoformat(), realized_pnl=1000.0)
        self.conn.commit()
        self.assertEqual(99.0, self._facts()[1].value)

        # 未验证 SELL → 整条事实 fail closed。
        self._order_and_fill(cycle_id=self.cycle, side="sell", qty=50, price=12.0,
                             fill_date=DAY.isoformat(), verified=False,
                             realized_pnl=5000.0)
        self.conn.commit()
        blocked = self._facts()[1]
        self.assertEqual(P.STATUS_UNKNOWN, blocked.status)
        self.assertIsNone(blocked.value)

    def test_PFACT_06_nonexistent_or_unattached_account_is_never_verified_zero(self):
        """PFACT-06：不存在 / 未挂载的 account **绝不**变成 "verified 0"。

        单独调用 ``realized_pnl`` 时，"账户存在但没有卖出"与"账户压根不存在"都可能得到
        ``0.0, verified``。因此 typed fact 入口必须先证明 account 属于该 cycle 且在 asof
        前已挂载；证明不了时三条事实全部 ``unknown`` 且 ``value=None``。
        """
        # 1) 非空性对照：**已证明挂载**的账户在同一天必须给出 verified 值
        #    （否则下面的 unknown 可能只是因为签发口恒报 unknown）。
        control = self._facts()
        for fact in control:
            with self.subTest(kind=fact.fact_kind, scope="attached-control"):
                self.assertEqual(P.STATUS_VERIFIED, fact.status)
                self.assertIsNotNone(fact.value)

        # 2) 账户根本不存在。
        for fact in self._facts(account="r27b2c4b-ghost"):
            with self.subTest(kind=fact.fact_kind, scope="nonexistent"):
                self.assertEqual(P.STATUS_UNKNOWN, fact.status)
                self.assertIsNone(fact.value)

        # 3) 账户存在、cycle 绑定也匹配，但 asof 前没有任何挂载/活动证据
        #    （挂载证据的 effective_date 在 asof 之后）。
        other = self._cycle("r27b2c4b-c2", "running", created=NEXT)
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (other, ACCOUNT)
        )
        self.conn.commit()
        self._attach(ACCOUNT, effective=NEXT, cycle=other)
        for fact in self._facts(cycle=other):
            with self.subTest(kind=fact.fact_kind, scope="unattached"):
                self.assertEqual(P.STATUS_UNKNOWN, fact.status)
                self.assertIsNone(fact.value)

    def test_PFACT_07_position_cost_summary_uses_bounded_lots_not_the_projection(self):
        """PFACT-07：持仓成本摘要来自 durable lots，**不读** ``paper_positions``。

        R22 已明确 ``paper_positions`` 是 compatibility-only 投影；它没有 cycle 归属，
        也不携带 as-of 证据，因此不能作为 typed owner fact 的事实来源。
        """
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        # 兼容投影里放一个矛盾的值：它**不得**影响 typed fact。
        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, "测试股", "测试", 999, 77.0, DAY.isoformat(),
             NEXT.isoformat(), "stock_t1", 0.0, 0),
        )
        self.conn.commit()
        summary = self._facts()[2]
        self.assertEqual(P.PORTFOLIO_FACT_POSITION_COST_SUMMARY, summary.fact_kind)
        self.assertEqual(P.STATUS_VERIFIED, summary.status)
        self.assertIs(type(summary.value), P.PositionCostSummary)
        self.assertEqual(1, summary.value.position_count)
        self.assertEqual(1000.0, summary.value.cost_value)
        self.assertNotEqual(999 * 77.0, summary.value.cost_value)

    def test_PFACT_08_unproven_quantity_never_becomes_a_verified_summary(self):
        """PFACT-08：``quantity_status == unknown`` → summary 也必须 unknown / ``value=None``。

        既不发"能算多少算多少"，也不发 verified zero。
        """
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        self._order_and_fill(cycle_id=self.cycle, side="sell", qty=50, price=12.0,
                             fill_date=DAY.isoformat(), verified=False)
        self.conn.commit()
        _lots, quantity_status = P.bounded_lots_with_status(
            self.conn, P.PortfolioReadContext(self.cycle, DAY), account_id=ACCOUNT,
        )
        # 非空性：owner 读路径**确实**把数量判成未知（否则本用例是空转）。
        self.assertEqual(P.STATUS_UNKNOWN, quantity_status)
        summary = self._facts()[2]
        self.assertEqual(P.STATUS_UNKNOWN, summary.status)
        self.assertIsNone(summary.value)

    def test_PFACT_09_future_fills_and_lots_never_enter_the_fact(self):
        """PFACT-09：asof 之后的成交 / 持仓绝不进入当日事实。"""
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        future = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=50,
                                      price=11.0, fill_date=NEXT.isoformat(),
                                      code=OTHER_CODE)
        self._lot(self.cycle, 50, 11.0, acquired_at=f"{NEXT.isoformat()} 10:00:00",
                  source_order_id=future, code=OTHER_CODE)
        self.conn.commit()

        facts = self._facts()
        self.assertEqual(self.initial_cash - 1005.0, facts[0].value)
        summary = facts[2]
        self.assertEqual(P.STATUS_VERIFIED, summary.status)
        self.assertEqual(1, summary.value.position_count)
        self.assertEqual(1000.0, summary.value.cost_value)
        # 当 asof 移到未来日时，未来那一笔才出现（证明它只是被 as-of 界挡住）。
        later = self._facts(asof=NEXT)[2]
        self.assertEqual(2, later.value.position_count)
        self.assertEqual(1000.0 + 550.0, later.value.cost_value)

    def test_PFACT_10_archived_or_unprovable_context_fails_closed(self):
        """PFACT-10：archived cycle 或不可证明的 context → 三条事实全部 fail closed。"""
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "UPDATE paper_cycles SET status='archived' WHERE id=?", (self.cycle,)
        )
        self.conn.commit()
        for fact in self._facts():
            with self.subTest(kind=fact.fact_kind, scope="archived"):
                self.assertEqual(P.STATUS_UNKNOWN, fact.status)
                self.assertIsNone(fact.value)

        self.conn.execute(
            "UPDATE paper_cycles SET status='running' WHERE id=?", (self.cycle,)
        )
        self.conn.commit()
        # 不存在的 cycle：归属不可证明。
        for fact in self._facts(cycle=self.cycle + 999):
            with self.subTest(kind=fact.fact_kind, scope="missing-cycle"):
                self.assertEqual(P.STATUS_UNKNOWN, fact.status)
                self.assertIsNone(fact.value)

    def test_PFACT_11_non_finite_ledger_values_never_become_verified_facts(self):
        """PFACT-11：非有限账本值不得成为 verified fact（现金 / 已实现盈亏 / 成本）。"""
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        self.conn.execute(
            "UPDATE paper_fills SET amount=? WHERE order_id=?", (float("inf"), buy)
        )
        self.conn.commit()
        cash = self._facts()[0]
        self.assertEqual(P.STATUS_UNKNOWN, cash.status)
        self.assertIsNone(cash.value)

        sell = self._order_and_fill(cycle_id=self.cycle, side="sell", qty=30,
                                    price=12.0, fill_date=DAY.isoformat(),
                                    realized_pnl=10.0)
        self.conn.execute(
            "UPDATE paper_orders SET realized_pnl=? WHERE id=?", (float("inf"), sell)
        )
        self.conn.commit()
        realized = self._facts()[1]
        self.assertEqual(P.STATUS_UNKNOWN, realized.status)
        self.assertIsNone(realized.value)

        # durable lot 本身带非有限数量 → 数量不可证明 → summary unknown/None。
        self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.cycle, ACCOUNT, CODE, "测试股", "测试", float("inf"), 100, 10.0,
             f"{DAY.isoformat()} 10:00:00", NEXT.isoformat(), "stock_t1", None, 1, 1),
        )
        self.conn.commit()
        summary = self._facts()[2]
        self.assertEqual(P.STATUS_UNKNOWN, summary.status)
        self.assertIsNone(summary.value)

    def test_PFACT_12_a_proven_zero_is_not_mistaken_for_unknown(self):
        """PFACT-12：已验证的 0 是**合法事实**，不得与 ``unknown`` 混淆。

        ``verified 0.0`` 是一个肯定性结论（"确实没有现金 / 确实没有已实现盈亏 / 确实没有
        开仓成本"），``unknown`` 必须带 ``None``。两者压平会让"不知道"看起来像一个确定的
        零，也会让真实的零看起来像"读不出来"。
        """
        self.conn.execute(
            "UPDATE paper_accounts SET initial_cash=? WHERE id=?", (0.0, ACCOUNT)
        )
        self.conn.commit()
        cash, realized, summary = self._facts()
        self.assertEqual(P.STATUS_VERIFIED, cash.status)
        self.assertEqual(0.0, cash.value)
        self.assertIsInstance(cash.value, float)
        self.assertEqual(P.STATUS_VERIFIED, realized.status)
        self.assertEqual(0.0, realized.value)
        self.assertEqual(P.STATUS_VERIFIED, summary.status)
        self.assertIs(type(summary.value), P.PositionCostSummary)
        self.assertEqual(0, summary.value.position_count)
        self.assertEqual(0.0, summary.value.cost_value)
        for fact in (cash, realized, summary):
            with self.subTest(kind=fact.fact_kind):
                self.assertNotEqual(P.STATUS_UNKNOWN, fact.status)
                self.assertIsNotNone(fact.value)

    # ---------- PFACT-13 ~ 15：确定性与权威边界 ----------

    def test_PFACT_13_fact_order_is_fixed_and_deterministic(self):
        """PFACT-13：发布顺序固定（cash → realized_pnl → position_cost_summary）且确定性。"""
        buy = self._order_and_fill(cycle_id=self.cycle, side="buy", qty=100,
                                   price=10.0, fill_date=DAY.isoformat())
        self._lot(self.cycle, 100, 10.0, source_order_id=buy)
        self._order_and_fill(cycle_id=self.cycle, side="sell", qty=30, price=12.0,
                             fill_date=DAY.isoformat(), realized_pnl=59.5)
        self.conn.commit()
        kinds = [fact.fact_kind for fact in self._facts()]
        self.assertEqual(list(P.PORTFOLIO_FACT_KINDS), kinds)
        self.assertEqual(["cash", "realized_pnl", "position_cost_summary"], kinds)
        # 重复读逐字相同（不依赖 dict / set 迭代顺序）。
        self.assertEqual(
            [fact.as_dict() for fact in self._facts()],
            [fact.as_dict() for fact in self._facts()],
        )

    def test_PFACT_14_projection_has_no_nav_or_market_value_surface(self):
        """PFACT-14：投影**没有** NAV / market_value / unrealized / daily_return 表面。

        这些是跨 owner 组合事实（组合账本 + R24 估值）。portfolio owner 不能把 caller 提供
        的裸价格升级成自己的 verified 事实，因此连字段都不存在。
        """
        self.assertEqual(
            {"version", "fact_kind", "cycle_id", "account_id", "asof_day", "status", "value"},
            {field.name for field in dataclasses.fields(P.PortfolioFactProjection)},
        )
        self.assertEqual(
            {"position_count", "cost_value"},
            {field.name for field in dataclasses.fields(P.PositionCostSummary)},
        )
        for name in P.PORTFOLIO_FACT_KINDS:
            for word in FORBIDDEN_SURFACE:
                with self.subTest(kind=name, forbidden=word):
                    self.assertNotIn(word, name)
        for fact in self._facts():
            for word in FORBIDDEN_SURFACE:
                with self.subTest(kind=fact.fact_kind, forbidden=word):
                    self.assertFalse(hasattr(fact, word))
                    self.assertNotIn(word, fact.as_dict())
        self.assertEqual(
            {"version", "fact_kind", "cycle_id", "account_id", "asof_day", "status", "value"},
            {key for fact in self._facts() for key in fact.as_dict()},
        )

    def test_PFACT_15_factory_takes_no_valuations_or_current_quote(self):
        """PFACT-15：typed fact factory 不接受 valuations / current quote / latest fallback。

        这条必须看**会执行的代码**而不是源码文本：本模块的 docstring 刻意写清"为什么不
        发布 NAV"，字符级搜索会把说明文字本身当成越界证据。
        """
        signature = inspect.signature(P.accounting_fact_projections)
        self.assertEqual(["conn", "context", "account_id"], list(signature.parameters))
        for forbidden in ("valuations", "valuation", "quotes", "quote", "prices",
                          "price", "reading", "snapshot", "market_data", "latest", "nav"):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, signature.parameters)

        for func in (P.accounting_fact_projections, P._position_cost_summary,
                     P._numeric_fact_value, P._portfolio_fact):
            with self.subTest(function=func.__name__):
                body = _executable_source(func)
                for token in ("portfolio_for_context", "valuations", "paper_nav",
                              "paper_positions", "MarketDataReading", "latest"):
                    self.assertNotIn(token, body)
        for func in (P.accounting_fact_projections, P._position_cost_summary):
            with self.subTest(wall_clock=func.__name__):
                body = _executable_source(func)
                for token in ("date.today", "datetime.now", "time.time"):
                    self.assertNotIn(token, body)

        # 非空性：剥 docstring 的手法是有效的 —— 带 docstring 的原文**包含**这些词。
        self.assertIn("paper_positions", inspect.getsource(P._position_cost_summary))
        self.assertNotIn("paper_positions", _executable_source(P._position_cost_summary))


if __name__ == "__main__":
    unittest.main()
