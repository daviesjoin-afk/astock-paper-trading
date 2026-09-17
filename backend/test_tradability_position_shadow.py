# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow Validation 契约测试。

这些测试只为一件事：**仓位层的 T+1 观察必须来自真实证据与既有权威**。

因此它们刻意大量断言"不可比"与"拒绝"：

* 真实成交 session（``paper_fills.fill_date``）说话，订单意图 / 创建时刻不算；
* 多 lot 不得被压成一个 ``entry_session``，份额必须逐 lot 拆开；
* 未验证的成交不得成为 acquisition 事实；
* PIT：晚于 ``validation_as_of`` 记录的 lot 不得进入该快照；
* 证据不足一律 fail closed（``position_unknown`` / ``position_unprovable``），
  **绝不**"不知道 ⇒ 可卖"；
* ETF T+0 不被 T+1 overlay 阻断；
* 节假日历由权威给（这里用 2026 国庆：09-30 的下一个交易日是 10-08）；
* BUY 完全不受影响；
* ON/OFF 不得改变任何生产输出。

夹具全部是内存 SQLite + 注入的假市场证据，无网络、不触碰执行路径。

变异脚本 ``work/tradability_position_mutation_check.py`` 的 M-T1-1 … M-T1-10
逐条还原这里的缺陷，本文件必须把它们抓住。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selection_tradability as ST  # noqa: E402
import tradability_archive as TA  # noqa: E402
import tradability_position_evidence as PE  # noqa: E402
import tradability_position_shadow as PS  # noqa: E402
import tradability_shadow as TS  # noqa: E402

ACCOUNT = "acct_a"
NORMAL = "600001"
ETF = "510300"
NORMAL_NAME = "平安银行"
ETF_NAME = "沪深300ETF"

#: 决策 session 与它们的权威下一交易日（由 backend.universe 的法定交易日历给出）。
D1 = "2026-09-16"          # 周三
D1_NEXT = "2026-09-17"     # 周四
D2 = "2026-09-17"
FRIDAY = "2026-09-18"
MONDAY = "2026-09-21"      # 周五的下一个交易日
PRE_HOLIDAY = "2026-09-30"  # 周三
HOLIDAY = "2026-10-01"     # 国庆，非交易日
POST_HOLIDAY = "2026-10-08"  # 长假后第一个交易日

AS_OF = "2026-10-09T16:00:00+08:00"

DDL = """
CREATE TABLE paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
    signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
    amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
    risk_payload TEXT NOT NULL DEFAULT '', realized_pnl REAL,
    created_at TEXT NOT NULL, executed_at TEXT,
    order_type TEXT NOT NULL DEFAULT 'market',
    origin TEXT NOT NULL DEFAULT 'strategy', expires_at TEXT, cancelled_at TEXT,
    strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
    retry_of_order_id INTEGER,
    execution_status TEXT, execution_verified INTEGER, execution_evidence_source TEXT
);
CREATE TABLE paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
    account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL, fees REAL NOT NULL,
    fill_date TEXT NOT NULL, quote_at TEXT, assumption TEXT NOT NULL DEFAULT ''
);
CREATE TABLE paper_positions (
    account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
    qty INTEGER NOT NULL, cost REAL NOT NULL, entry_date TEXT NOT NULL,
    available_date TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'stock_t1',
    peak_price REAL, take_stage INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, code)
);
CREATE TABLE paper_position_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
    account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
    qty INTEGER NOT NULL, remaining_qty INTEGER NOT NULL, cost REAL NOT NULL,
    acquired_at TEXT NOT NULL, available_date TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'stock_t1', source_order_id INTEGER,
    cost_fee_included INTEGER NOT NULL DEFAULT 0,
    is_t_base INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE paper_accounts (id TEXT PRIMARY KEY, cash REAL NOT NULL DEFAULT 0);
"""


class PositionTestCase(unittest.TestCase):
    """共享夹具：一套真实形状的账本 + 一个只读市场证据 provider。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        TA.ensure_schema(self.conn)
        self.repo = TA.TradabilityArchiveRepository(self.conn)
        self.snapshots = {}

    def tearDown(self):
        self.conn.close()

    # ── 夹具构造 ──

    def add_lot(self, *, code=NORMAL, account=ACCOUNT, qty, session, fill_id,
                order_id, name=NORMAL_NAME, asset_type=None, verified=True,
                recorded_at=None, order_created_at=None, remaining=None,
                cycle_id=1, lot_id=None, status="filled", side="buy"):
        """写入一笔委托 + 一条成交 + 一个 lot（形状与真实账本一致）。

        ``session`` 是**真实成交 session**（进 ``paper_fills.fill_date``）；
        ``order_created_at`` 默认为它**前一天**，用来暴露"用订单意图日期冒充
        成交日期"这类缺陷。
        """
        if asset_type is None:
            asset_type = "etf_t0" if code == ETF else "stock_t1"
        exec_status = "verified" if verified else "unknown"
        exec_flag = 1 if verified else 0
        self.conn.execute(
            "INSERT INTO paper_orders(id,account_id,side,code,name,qty,status,"
            "risk_payload,created_at,executed_at,execution_status,"
            "execution_verified,execution_evidence_source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, account, side, code, name, int(qty), status, "",
             order_created_at or f"{session} 09:30:00", f"{session} 10:00:00",
             exec_status, exec_flag,
             "paper_orders+paper_fills" if verified else "no_evidence_available"),
        )
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, account, side, code, int(qty), 10.0, int(qty) * 10.0, 0.0,
             session, None, "close"),
        )
        self.conn.execute(
            "INSERT INTO paper_position_lots(id,cycle_id,account_id,code,name,industry,"
            "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
            "source_order_id,cost_fee_included,is_t_base) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lot_id, cycle_id, account, code, name, None, int(qty),
             int(qty) if remaining is None else int(remaining), 10.0,
             recorded_at or f"{session} 10:00:00", D2, asset_type,
             order_id, 1, 1),
        )

    def add_archive_fact(self, code=NORMAL, session=D2, **flags):
        payload = {
            "code": code, "session_date": session, "source": "fixture.json",
            "observed_at": f"{session}T15:05:00", "effective_at": f"{session}T15:05:00",
            "is_listed": True, "is_st": False, "is_suspended": False,
            "has_market_quote": True, "has_trade_volume": True,
        }
        payload.update(flags)
        self.repo.save(TA.normalize_record(payload))

    # ── 只读视图 ──

    def market_evidence(self, code, session):
        """该 session 的市场证据（PIT：收盘后可用）。"""
        name = ETF_NAME if code == ETF else NORMAL_NAME
        return ST.MarketEvidence(
            session=session,
            available_at=ST.session_close_at(session),
            price=10.6, reference_price=10.5, volume=1_000_000.0,
            halted=False, name=name, risk_flag=None,
        )

    def adapter(self, *, provider=True):
        return PE.PositionEvidenceAdapter(
            self.conn,
            evidence_provider=(self.market_evidence if provider else None),
        )

    def context(self, code=NORMAL, *, session=D2, account=ACCOUNT,
                requested=None, as_of=None, provider=True):
        return self.adapter(provider=provider).context_for(
            code, account_id=account, decision_session=session,
            validation_as_of=as_of, requested_sell_quantity=requested,
        )

    # ── 可观察的存储状态 ──

    def position_lot_state(self):
        """现有 lot 账本的 ``(remaining_qty, available_date)`` 集合。"""
        return sorted(
            (int(row["remaining_qty"]), str(row["available_date"]))
            for row in self.conn.execute(
                "SELECT remaining_qty, available_date FROM paper_position_lots"
            )
        )

    # ── 生产状态快照（ON/OFF 等值断言用）──

    PRODUCTION_TABLES = ("paper_orders", "paper_fills", "paper_positions",
                         "paper_position_lots", "paper_accounts")

    def snapshot_production(self):
        out = {}
        for table in self.PRODUCTION_TABLES:
            rows = self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[table] = [tuple(row) for row in rows]
        return out

    def assert_production_untouched(self, before):
        self.assertEqual(before, self.snapshot_production(),
                         "仓位层观察不得改动任何生产表")


# ───────────────────────── T1-1 同日不可卖 ─────────────────────────


class NormalStockSameDaySellIsT1Blocked(PositionTestCase):
    """T1-1：普通 A 股当日买入当日卖出必须被 T+1 拦下，且能被观察到。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)

    def test_same_day_sell_is_blocked_by_the_t1_authority(self):
        context = self.context(session=D1, requested=1000)
        self.assertEqual(PE.PositionEvidenceStatus.PROVEN, context.evidence_status)
        self.assertEqual(0, context.sellable_quantity)
        self.assertEqual(1000, context.t1_locked_quantity)
        self.assertEqual(PE.SellabilityStatus.T1_BLOCKED, context.sellability_status)

    def test_the_lot_reports_the_authoritative_reason(self):
        context = self.context(session=D1, requested=1000)
        lot = context.lots[0]
        self.assertEqual(PE.LotSellability.BLOCKED, lot.sellability)
        self.assertEqual(ST.REASON_T1_NOT_SELLABLE, lot.sellability_reason)
        self.assertEqual(PE.T1EvalSource.PRODUCTION_T1_BLOCKED, lot.t1_eval_source)


# ───────────────────────── T1-2 次日可卖 ─────────────────────────


class NextSellableSessionPassesT1(PositionTestCase):
    """T1-2：到权威的下一个可卖 session，T+1 这一维不再拦。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)

    def test_next_session_is_sellable(self):
        context = self.context(session=D1_NEXT, requested=1000)
        self.assertEqual(PE.SellabilityStatus.T1_SELLABLE, context.sellability_status)
        self.assertEqual(1000, context.sellable_quantity)
        self.assertEqual(0, context.t1_locked_quantity)

    def test_t1_pass_is_not_a_claim_of_executability(self):
        """T+1 通过 ≠ 最终可执行：那由市场层面说了算，本层不越权。"""
        context = self.context(session=D1_NEXT, requested=1000)
        self.assertEqual(PE.SellabilityStatus.T1_SELLABLE, context.sellability_status)
        # 市场层面另判：本层从不产出"放行"。
        self.assertFalse(hasattr(context, "executable"))
        self.assertFalse(hasattr(context, "allowed"))


# ───────────────────────── T1-3 周末 / 休市 ─────────────────────────


class AuthorityCalendarIsRespected(PositionTestCase):
    """T1-3：必须是"下一个交易日"，不是"日历 +1 天"。"""

    def test_friday_entry_unlocks_on_monday(self):
        self.add_lot(qty=100, session=FRIDAY, fill_id=1, order_id=1)
        context = self.context(session=MONDAY, requested=100)
        self.assertTrue(context.comparable)
        self.assertEqual(PE.SellabilityStatus.T1_SELLABLE, context.sellability_status)
        self.assertEqual(100, context.sellable_quantity)

    def test_the_friday_itself_and_the_weekend_are_both_blocked(self):
        self.add_lot(qty=100, session=FRIDAY, fill_id=1, order_id=1)
        self.assertEqual(
            PE.SellabilityStatus.T1_BLOCKED,
            self.context(session=FRIDAY, requested=100).sellability_status,
        )
        saturday = "2026-09-19"
        self.assertEqual(
            PE.SellabilityStatus.T1_BLOCKED,
            self.context(session=saturday, requested=100).sellability_status,
            "周末不是可卖 session（calendar+1 会错判为可卖）",
        )

    def test_statutory_holiday_is_not_a_sellable_session(self):
        """2026 国庆：09-30 之后是 10-08，而不是日历 +1 天的 10-01。"""
        self.add_lot(qty=100, session=PRE_HOLIDAY, fill_id=1, order_id=1)
        self.assertEqual(
            PE.SellabilityStatus.T1_BLOCKED,
            self.context(session=PRE_HOLIDAY, requested=100).sellability_status,
        )
        self.assertEqual(
            PE.SellabilityStatus.T1_BLOCKED,
            self.context(session=HOLIDAY, requested=100).sellability_status,
            "10-01 是法定假日，不是可卖 session（calendar+1 会错判为可卖）",
        )
        self.assertEqual(
            PE.SellabilityStatus.T1_SELLABLE,
            self.context(session=POST_HOLIDAY, requested=100).sellability_status,
        )


# ───────────────────────── T1-4 T+0 ETF ─────────────────────────


class T0EtfIsNotBlockedByT1(PositionTestCase):
    """T1-4：``etf_t0`` 当日即可卖，T+1 overlay 不得阻断它。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, code=ETF, session=D2, fill_id=1, order_id=1)

    def test_same_day_sell_is_not_blocked(self):
        context = self.context(code=ETF, session=D2, requested=1000)
        self.assertEqual(PE.PositionEvidenceStatus.PROVEN, context.evidence_status)
        self.assertEqual(1000, context.sellable_quantity)
        self.assertEqual(0, context.t1_locked_quantity)
        self.assertEqual(PE.SellabilityStatus.T1_SELLABLE, context.sellability_status)

    def test_asset_type_comes_from_the_authority_not_the_lot_row(self):
        context = self.context(code=ETF, session=D2, requested=1000)
        self.assertEqual("etf_t0", context.lots[0].asset_type_authority)


# ───────────────────────── T1-5 证据缺失 ─────────────────────────


class MissingPositionEvidenceFailsClosed(PositionTestCase):
    """T1-5：没有可证明的持仓证据时**不可比**，绝不当作可卖。"""

    def test_no_lot_rows_at_all_is_position_unknown(self):
        context = self.context(code="600999", session=D2, requested=100)
        self.assertEqual(PE.PositionEvidenceStatus.UNKNOWN, context.evidence_status)
        self.assertFalse(context.comparable)
        self.assertEqual(PE.SellabilityStatus.POSITION_UNKNOWN,
                         context.sellability_status)
        self.assertEqual(0, context.sellable_quantity)

    def test_lot_without_source_order_is_unprovable(self):
        self.add_lot(qty=500, session=D2, fill_id=1, order_id=77)
        self.conn.execute("DELETE FROM paper_orders WHERE id=77")
        context = self.context(session=D2, requested=500)
        self.assertEqual(PE.AcquisitionStatus.NO_ORDER,
                         context.lots[0].acquisition_status)
        self.assertEqual(0, context.sellable_quantity)
        self.assertFalse(context.comparable)
        self.assertEqual(PE.SellabilityStatus.POSITION_UNPROVABLE,
                         context.sellability_status)

    def test_missing_evidence_never_becomes_sellable(self):
        """承重断言：三种"不知道"都不得变成"可卖"。"""
        for code in ("600999",):
            context = self.context(code=code, session=D2, requested=100)
            self.assertEqual(0, context.sellable_quantity)
        self.add_lot(qty=500, session=D2, fill_id=1, order_id=90)
        self.conn.execute("DELETE FROM paper_orders WHERE id=90")
        self.assertEqual(0, self.context(session=D2, requested=500).sellable_quantity)


# ───────────────────────── T1-6 真实成交 session ─────────────────────────


class ActualFillSessionOverridesIntendedSession(PositionTestCase):
    """T1-6（承重）：``order intended entry = D`` 而 ``actual fill = D+1`` 时，
    T+1 基准必须是 **D+1**；用 D 会提前一天解锁。
    """

    def setUp(self):
        super().setUp()
        # 委托在 09-16 创建（意图日），但成交发生在 09-17。
        self.add_lot(qty=1000, session=D2, fill_id=1, order_id=1,
                     order_created_at=f"{D1} 09:30:00")

    def test_acquisition_session_is_the_fill_date(self):
        context = self.context(session=D2, requested=1000)
        lot = context.lots[0]
        self.assertEqual(D2, lot.acquisition_session)
        self.assertNotEqual(D1, lot.acquisition_session,
                            "订单创建日不得冒充成交 session")

    def test_the_intended_session_cannot_borrow_the_lot(self):
        """意图日那天并没有这笔仓位 —— 绝不能被当成"可卖"。"""
        context = self.context(session=D1, requested=1000)
        self.assertEqual(0, context.sellable_quantity)
        self.assertEqual(1000, context.unknown_quantity)
        self.assertEqual(PE.SellabilityStatus.POSITION_UNPROVABLE,
                         context.sellability_status)
        self.assertFalse(context.comparable)
        self.assertIn("lot_not_visible_at_decision", context.diagnostics)

    def test_it_unlocks_one_session_after_the_actual_fill(self):
        context = self.context(session="2026-09-18", requested=1000)
        self.assertEqual(PE.SellabilityStatus.T1_SELLABLE, context.sellability_status)


# ───────────────────────── T1-7 PIT：未来证据 ─────────────────────────


class FutureEvidenceCannotEnterAnEarlierSnapshot(PositionTestCase):
    """T1-7：晚于 ``validation_as_of`` / ``decision_at`` 的证据不得进入该快照。

    两个门禁必须**分别**被隔离验证：只测其中一个，另一个漏水也看不出来。
    每个用例自建 lot，避免一个可证明的 lot 把另一个不可证明的 lot 的结论盖住。
    """

    def test_an_as_of_before_the_record_excludes_the_lot(self):
        """成交 09-17，但账本直到 09-21 盘中才记录 → 09-21 盘中快照不该看见它。"""
        self.add_lot(qty=1000, session=D2, fill_id=1, order_id=1,
                     recorded_at="2026-09-21 13:00:00")
        context = self.context(session=MONDAY, requested=1000,
                               as_of="2026-09-21T12:00:00+08:00")
        self.assertEqual(0, context.sellable_quantity)
        self.assertEqual(1000, context.unknown_quantity,
                         "证据还没进入这个知识时点 → 只能是 unknown，不是可卖")
        self.assertIn("lot_recorded_after_validation_as_of", context.diagnostics)
        self.assertFalse(context.comparable)

    def test_the_lot_is_hidden_not_dropped(self):
        """隐藏原因不得让 lot **消失**（消失会把不可证明粉饰成可比）。"""
        self.add_lot(qty=1000, session=D2, fill_id=1, order_id=1,
                     recorded_at="2026-09-21 13:00:00")
        context = self.context(session=MONDAY, requested=1000,
                               as_of="2026-09-21T12:00:00+08:00")
        self.assertEqual(1000, context.held_quantity)
        self.assertEqual(1, len(context.lots))

    def test_it_is_provable_once_the_as_of_passes_the_record(self):
        self.add_lot(qty=1000, session=D2, fill_id=1, order_id=1,
                     recorded_at="2026-09-21 13:00:00")
        context = self.context(session=MONDAY, requested=1000,
                               as_of="2026-09-21T16:00:00+08:00")
        self.assertEqual(1000, context.sellable_quantity)
        self.assertEqual(PE.PositionEvidenceStatus.PROVEN, context.evidence_status)

    def test_a_lot_recorded_after_the_decision_cannot_be_used(self):
        """记录时点晚于决策时点 → 决策当时不可能知道 → fail closed。"""
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1,
                     recorded_at="2026-09-25 10:00:00")
        context = self.context(session=MONDAY, requested=1000)
        self.assertIn("lot_not_visible_at_decision", context.diagnostics)
        self.assertEqual(PE.LotSellability.UNKNOWN, context.lots[0].sellability)
        self.assertEqual(1000, context.unknown_quantity)
        self.assertEqual(0, context.sellable_quantity)

    def test_a_fill_effective_after_the_decision_cannot_prove_holding(self):
        """成交生效时点晚于决策时点 → 不能拿来证明决策当时已持有。"""
        self.add_lot(qty=400, session="2026-09-24", fill_id=1, order_id=1,
                     recorded_at="2026-09-24 10:00:00")
        context = self.context(session=MONDAY, requested=400)
        self.assertIn("lot_not_visible_at_decision", context.diagnostics)
        self.assertEqual(PE.LotSellability.UNKNOWN, context.lots[0].sellability)
        self.assertEqual(0, context.sellable_quantity)


# ───────────────────────── T1-8 BUY 不受影响 ─────────────────────────


class BuyIsUnaffectedByThePositionLayer(PositionTestCase):
    """T1-8：BUY 的观察与市场层面逐字段相同，且不带任何仓位字段。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)
        self.add_archive_fact()
        self.comparator = TS.ShadowComparator(self.repo)
        self.observer = PS.PositionShadowObserver(
            self.comparator, self.adapter(), account_id=ACCOUNT)

    def _observe(self, side):
        verdict = (
            ST.entry_tradability(self.market_evidence(NORMAL, D2),
                                 code=NORMAL, entry_session=D2)
            if side == ST.SIDE_BUY else
            ST.exit_tradability(self.market_evidence(NORMAL, D2),
                                code=NORMAL, exit_session=D2)
        )
        return self.observer.observe(
            code=NORMAL, session=D2, side=side, production_verdict=verdict,
            validation_as_of=AS_OF, requested_sell_quantity=1000,
        )

    def test_buy_status_equals_the_market_level_status(self):
        buy = self._observe(ST.SIDE_BUY)
        self.assertEqual(buy.market_status, buy.position_status)

    def test_buy_carries_no_position_fields(self):
        buy = self._observe(ST.SIDE_BUY)
        self.assertIsNone(buy.position_evidence_status)
        self.assertIsNone(buy.position_sellability_status)
        self.assertIsNone(buy.requested_sell_quantity)
        self.assertEqual((0, 0, 0, 0), (buy.held_quantity, buy.sellable_quantity,
                                        buy.t1_locked_quantity, buy.unknown_quantity))
        self.assertEqual("buy_unaffected_by_position_layer", buy.position_diagnostic)

    def test_sell_does_carry_position_fields_so_the_contrast_is_real(self):
        sell = self._observe(ST.SIDE_SELL)
        self.assertIsNotNone(sell.position_evidence_status)
        self.assertEqual(1000, sell.held_quantity)

    def test_buy_only_run_is_field_for_field_identical_to_off(self):
        """ON/OFF 等值：只跑 BUY 时，仓位层的产物与市场层面完全一致。"""
        buy = self._observe(ST.SIDE_BUY)
        market = self.comparator.compare(
            ST.entry_tradability(self.market_evidence(NORMAL, D2),
                                 code=NORMAL, entry_session=D2),
            code=NORMAL, session=D2, side=ST.SIDE_BUY,
            decision_at=ST.session_close_at(D2), validation_as_of=AS_OF,
        )
        self.assertEqual(market.status, buy.position_status)
        self.assertEqual(market.production_allowed, buy.market_production_allowed)


# ───────────────────────── T1-9 未验证成交 ─────────────────────────


class UnverifiedFillCannotEstablishAnAcquisitionLot(PositionTestCase):
    """T1-9：``execution_verified=0`` 的成交不得被升级成可靠的 T+1 lot。"""

    def test_unverified_buy_is_not_a_proven_acquisition(self):
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, verified=False)
        context = self.context(session=D2, requested=1000)
        self.assertEqual(PE.AcquisitionStatus.UNVERIFIED,
                         context.lots[0].acquisition_status)
        self.assertEqual(0, context.sellable_quantity)
        self.assertEqual(1000, context.unknown_quantity)
        self.assertFalse(context.comparable)

    def test_partial_execution_status_is_not_verified_either(self):
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, verified=False)
        self.conn.execute(
            "UPDATE paper_orders SET execution_status='partial', execution_verified=1"
        )
        context = self.context(session=D2, requested=1000)
        self.assertEqual(PE.AcquisitionStatus.UNVERIFIED,
                         context.lots[0].acquisition_status)
        self.assertEqual(0, context.sellable_quantity)


# ───────────────────────── T1-10 多 lot / partial sell ─────────────────────────


class PartialAndMixedLotsRespectSellableQuantity(PositionTestCase):
    """T1-10 + T1-11：D-1 买 1000、D 买 500、D 卖 1200 的真实语义。

    真实语义是"昨日 1000 股可卖、今日 500 股不可卖"，而不是整仓全可卖 /
    全不可卖。
    """

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, lot_id=101)
        self.add_lot(qty=500, session=D2, fill_id=2, order_id=2, lot_id=102)

    def test_mixed_sessions_do_not_collapse_into_one_entry_session(self):
        context = self.context(session=D2, requested=None)
        self.assertEqual((D1, D2), context.acquisition_sessions)
        sellability = {lot.acquisition_session: lot.sellability
                       for lot in context.lots}
        self.assertEqual(PE.LotSellability.SELLABLE, sellability[D1])
        self.assertEqual(PE.LotSellability.BLOCKED, sellability[D2])

    def test_quantity_split_is_exact(self):
        context = self.context(session=D2, requested=None)
        self.assertEqual(1500, context.held_quantity)
        self.assertEqual(1000, context.sellable_quantity)
        self.assertEqual(500, context.t1_locked_quantity)
        self.assertEqual(0, context.unknown_quantity)

    def test_a_partial_sell_inside_the_sellable_quantity_passes(self):
        """请求 900 <= 1000 可卖 → T+1 这一维不阻断。"""
        context = self.context(session=D2, requested=900)
        self.assertEqual(PE.SellabilityStatus.T1_SELLABLE, context.sellability_status)
        self.assertTrue(context.comparable)

    def test_a_sell_beyond_the_sellable_quantity_is_blocked(self):
        """请求 1200 > 1000 可卖 → 阻断（不是"因为有一手昨日 lot 就全仓可卖"）。"""
        context = self.context(session=D2, requested=1200)
        self.assertEqual(PE.SellabilityStatus.T1_BLOCKED, context.sellability_status)
        self.assertTrue(context.comparable)

    def test_exactly_the_sellable_quantity_passes(self):
        self.assertEqual(
            PE.SellabilityStatus.T1_SELLABLE,
            self.context(session=D2, requested=1000).sellability_status,
        )

    def test_quantity_invariant_always_holds(self):
        for requested in (None, 0, 500, 1000, 1500, 99999):
            context = self.context(session=D2, requested=requested)
            self.assertEqual(
                context.held_quantity,
                context.sellable_quantity + context.t1_locked_quantity
                + context.unknown_quantity,
                f"份额不变量被破坏（requested={requested}）",
            )


# ───────────────────────── T1-12 市场拦截仍然生效 ─────────────────────────


class MarketBlockRemainsAMarketBlock(PositionTestCase):
    """T1-12：T+1 通过不等于市场层面放行；两个维度必须分开报告。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)
        self.add_archive_fact()
        self.comparator = TS.ShadowComparator(self.repo)

    def test_position_pass_does_not_overwrite_a_market_block(self):
        # 市场层面：跌停 → 卖不出去。仓位层面：T+1 已解锁。
        evidence = ST.MarketEvidence(
            session=D1_NEXT, available_at=ST.session_close_at(D1_NEXT),
            price=9.0, reference_price=10.0, volume=1_000_000.0,
            halted=False, name=NORMAL_NAME, risk_flag=None,
        )
        verdict = ST.exit_tradability(evidence, code=NORMAL, exit_session=D1_NEXT)
        self.assertEqual(ST.REASON_LIMIT_DOWN_SELL_BLOCKED, verdict.reason)

        observer = PS.PositionShadowObserver(self.comparator, self.adapter(),
                                             account_id=ACCOUNT)
        observed = observer.observe(
            code=NORMAL, session=D1_NEXT, side=ST.SIDE_SELL,
            production_verdict=verdict, validation_as_of=AS_OF,
            requested_sell_quantity=1000,
        )
        self.assertEqual(PS.PositionShadowStatus.COMPARABLE_T1_PASS,
                         observed.position_status)
        # 市场层面的 reason 原样保留，没有被仓位层改写。
        self.assertEqual(ST.REASON_LIMIT_DOWN_SELL_BLOCKED,
                         observed.production_reason)
        self.assertIsNotNone(observed.market_status)


# ───────────────────────── T1-13 确定性 / 幂等 ─────────────────────────


class PositionObservationIsDeterministic(PositionTestCase):
    """T1-13：同一输入的观察必须逐字段、逐指纹相同。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, lot_id=101)
        self.add_lot(qty=500, session=D2, fill_id=2, order_id=2, lot_id=102)

    def test_repeated_contexts_are_identical(self):
        first = self.context(session=D2, requested=1200)
        second = self.context(session=D2, requested=1200)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)

    def test_fingerprint_is_content_addressed(self):
        base = self.context(session=D2, requested=1200)
        other_quantity = self.context(session=D2, requested=900)
        self.assertNotEqual(base.evidence_fingerprint, other_quantity.evidence_fingerprint,
                            "请求卖出量是影响结论的内容，必须进指纹")
        other_session = self.context(session=D1, requested=1200)
        self.assertNotEqual(
            base.evidence_fingerprint, other_session.evidence_fingerprint,
            "同一份 lot 证据在不同 decision_session 下的结论不同，指纹必须区分",
        )

    def test_evidence_changes_change_the_fingerprint_but_not_the_shadow_identity(self):
        """同身份 + 内容变化 → 指纹必须变（绝不 last-write-wins）。"""
        base = self.context(session=D2, requested=1200)
        self.add_lot(qty=300, session=D1, fill_id=3, order_id=3, lot_id=103)
        changed = self.context(session=D2, requested=1200)
        self.assertNotEqual(base.evidence_fingerprint, changed.evidence_fingerprint)


# ───────────────────────── T1-14 ON/OFF 零生产影响 ─────────────────────────


class PositionAwareOnOffHasZeroProductionEffect(PositionTestCase):
    """T1-14 + §26：仓位层是附加观察维度，不得改写任何既有结果。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, lot_id=101)
        self.add_lot(qty=500, session=D2, fill_id=2, order_id=2, lot_id=102)
        self.add_archive_fact()
        self.conn.execute("INSERT INTO paper_positions(account_id,code,name,"
                          "qty,cost,entry_date,available_date,asset_type) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                          (ACCOUNT, NORMAL, NORMAL_NAME, 1500, 10.0, D1, D2, "stock_t1"))
        self.conn.execute("INSERT INTO paper_accounts(id,cash) VALUES(?,?)",
                          (ACCOUNT, 100000.0))
        self.comparator = TS.ShadowComparator(self.repo)

    def _items(self):
        items = []
        for side in ST.SIDES:
            if side == ST.SIDE_BUY:
                verdict = ST.entry_tradability(
                    self.market_evidence(NORMAL, D2), code=NORMAL, entry_session=D2)
            else:
                verdict = ST.exit_tradability(
                    self.market_evidence(NORMAL, D2), code=NORMAL, exit_session=D2)
            items.append({
                "code": NORMAL, "session": D2, "side": side,
                "production_verdict": verdict,
                "decision_at": ST.session_close_at(D2),
                "validation_as_of": AS_OF,
            })
        return items

    def test_market_level_results_are_byte_for_byte_identical(self):
        items = self._items()
        off = [c.to_dict() for c in self.comparator.compare_many(items)]

        # 市场层面再算一遍（这就是真实 CLI 的 ON 路径）+ 叠加仓位层。
        again = self.comparator.compare_many(items)
        market_by_key = {(c.code, c.session, c.decision_at, c.side,
                          c.validation_as_of): c for c in again}
        observer = PS.PositionShadowObserver(None, self.adapter(),
                                             account_id=ACCOUNT)
        for item in items:
            observer.observe(
                code=item["code"], session=item["session"], side=item["side"],
                production_verdict=item["production_verdict"],
                decision_at=item["decision_at"],
                validation_as_of=item["validation_as_of"],
                requested_sell_quantity=1200,
                market_comparison=market_by_key.get(
                    (item["code"], item["session"], item["decision_at"],
                     item["side"], item["validation_as_of"])),
            )
        on = [c.to_dict() for c in again]
        self.assertEqual(off, on, "仓位层不得改写既有市场层面比对结果")

    def test_no_position_lot_is_consumed_or_rescheduled(self):
        """承重：仓位层绝不能改写 ``remaining_qty`` / ``available_date``。"""
        before = self.position_lot_state()
        observer = PS.PositionShadowObserver(self.comparator, self.adapter(),
                                             account_id=ACCOUNT)
        observer.observe_many(self._items())
        self.assertEqual(before, self.position_lot_state())

    def test_no_production_table_is_touched(self):
        before = self.snapshot_production()
        items = self._items()
        self.comparator.compare_many(items)
        observer = PS.PositionShadowObserver(self.comparator, self.adapter(),
                                             account_id=ACCOUNT)
        observer.observe_many(items)
        self.assert_production_untouched(before)

    def test_the_only_new_output_is_the_diagnostic_report(self):
        before = self.snapshot_production()
        items = self._items()
        comparisons = self.comparator.compare_many(items)
        observer = PS.PositionShadowObserver(self.comparator, self.adapter(),
                                             account_id=ACCOUNT)
        observed = observer.observe_many(items)
        summary = observer.summarize(observed)
        # 新增的只有观察产物本身。
        self.assertEqual(len(items), summary.requested)
        self.assert_production_untouched(before)
        # 既有的市场层面产物没有被这些调用改动。
        self.assertEqual(
            [c.to_dict() for c in self.comparator.compare_many(items)],
            [c.to_dict() for c in comparisons],
        )


# ───────────────────────── taxonomy / 分母口径 ─────────────────────────


class PositionTaxonomyKeepsNotComparableOutOfDenominators(PositionTestCase):
    """§13 / §14 / §32：不可比不进分母；比率必须带分母。"""

    def setUp(self):
        super().setUp()
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, lot_id=101)
        self.add_archive_fact()
        self.comparator = TS.ShadowComparator(self.repo)
        self.observer = PS.PositionShadowObserver(
            self.comparator, self.adapter(), account_id=ACCOUNT)

    def _observe(self, code, session, requested):
        verdict = ST.exit_tradability(
            self.market_evidence(code, session), code=code, exit_session=session)
        return self.observer.observe(
            code=code, session=session, side=ST.SIDE_SELL,
            production_verdict=verdict, validation_as_of=AS_OF,
            requested_sell_quantity=requested,
        )

    def _unprovable_position(self, *, code="600002", qty=700, lot_id=202,
                             order_id=91):
        """建一笔**持仓存在但建仓不可证明**的仓位（订单行缺失）。

        必须用**独立的 code**，否则它会和 ``setUp`` 里那个可证明的 lot 混成
        ``position_partial``，测不到"整仓不可证明"这一态。
        """
        self.add_lot(code=code, qty=qty, session=D2, fill_id=order_id,
                     order_id=order_id, lot_id=lot_id)
        self.conn.execute("DELETE FROM paper_orders WHERE id=?", (order_id,))
        return self.context(code=code, session=D2, requested=qty)

    def test_required_statuses_exist(self):
        required = {
            "position_comparable_t1_pass", "position_comparable_t1_blocked",
            "position_not_comparable",
        }
        self.assertEqual(required, set(PS.POSITION_SHADOW_STATUSES))

    def test_a_held_but_unprovable_position_is_not_comparable(self):
        """承重：**有持仓**不等于**可比** —— 建仓不可证明就不能进分母。"""
        context = self._unprovable_position()
        self.assertEqual(700, context.held_quantity)
        self.assertFalse(context.comparable)
        self.assertEqual(PE.PositionEvidenceStatus.UNPROVABLE, context.evidence_status)
        observed = self._observe("600002", D2, 700)
        self.assertFalse(observed.comparable)
        self.assertEqual(PS.PositionShadowStatus.NOT_COMPARABLE, observed.position_status)

    def test_a_held_but_unprovable_position_stays_out_of_the_denominator(self):
        summary = self.observer.summarize(
            [self._observe(NORMAL, D2, 1000), self._observe("600002", D2, 700)]
        ).to_dict()
        # 第二条观察里，唯一开放 lot 的建仓不可证明 → 不可比。
        self.assertEqual(2, summary["sell_comparisons"])
        self.assertEqual(1, summary["position_comparable"])
        self.assertEqual(1, summary["position_not_comparable"])

    def test_partial_evidence_is_not_comparable_and_keeps_the_split_visible(self):
        """部分可证明 → ``position_partial``，仍不可比，且份额分解必须完整。"""
        self.add_lot(qty=700, session=D2, fill_id=91, order_id=91, lot_id=202)
        self.conn.execute("DELETE FROM paper_orders WHERE id=91")
        context = self.context(session=D1_NEXT, requested=None)
        self.assertEqual(PE.PositionEvidenceStatus.PARTIAL, context.evidence_status)
        self.assertFalse(context.comparable)
        self.assertEqual(1700, context.held_quantity)
        self.assertEqual(1000, context.sellable_quantity)
        self.assertEqual(700, context.unknown_quantity)
        self.assertEqual(0, context.t1_locked_quantity,
                         "不可证明的份额不得被算成可卖，也不得被算成锁定")
        self.assertEqual(
            context.held_quantity,
            context.sellable_quantity + context.t1_locked_quantity
            + context.unknown_quantity,
        )

    def test_not_comparable_observations_are_excluded_from_the_denominator(self):
        comparable = self._observe(NORMAL, D2, 1000)
        unprovable = self._observe("600999", D2, 100)
        summary = self.observer.summarize([comparable, unprovable]).to_dict()
        self.assertEqual(2, summary["sell_comparisons"])
        self.assertEqual(1, summary["position_comparable"])
        self.assertEqual(1, summary["position_not_comparable"])
        self.assertEqual(0.5, summary["position_comparison_rate"],
                         "比率的分母是全部卖出观察，分子只数可比的")
        self.assertFalse(unprovable.comparable)

    def test_rate_is_none_when_there_is_no_sell_denominator(self):
        """分母为 0 → ``None``，**绝不**默认 0 / 1。"""
        summary = PS.PositionShadowSummary().to_dict()
        self.assertEqual(0, summary["sell_comparisons"])
        self.assertIsNone(summary["position_comparison_rate"])

    def test_evidence_gaps_are_reported_per_bucket_not_as_one_percentage(self):
        summary = self.observer.summarize(
            [self._observe("600999", D2, 100), self._observe(NORMAL, D2, 1000)]
        ).to_dict()
        self.assertEqual(1, summary["position_evidence_unknown"])
        self.assertEqual(1, summary["position_evidence_proven"])
        self.assertEqual(2, summary["sell_comparisons"])

    def test_buy_does_not_enter_the_sell_denominator(self):
        buy_verdict = ST.entry_tradability(
            self.market_evidence(NORMAL, D2), code=NORMAL, entry_session=D2)
        buy = self.observer.observe(
            code=NORMAL, session=D2, side=ST.SIDE_BUY,
            production_verdict=buy_verdict, validation_as_of=AS_OF,
        )
        summary = self.observer.summarize([buy]).to_dict()
        self.assertEqual(1, summary["buy_comparisons"])
        self.assertEqual(0, summary["sell_comparisons"])
        self.assertIsNone(summary["position_comparison_rate"])

    def test_quantity_totals_are_reported_with_their_denominator(self):
        summary = self.observer.summarize(
            [self._observe(NORMAL, D2, 1200)]
        ).to_dict()
        self.assertEqual(1200, summary["requested_sell_quantity"])
        self.assertEqual(1000, summary["proven_sellable_quantity"])
        self.assertEqual(0, summary["t1_locked_quantity"])


class PositionLayerRefusesToGuess(PositionTestCase):
    """适配器的 fail-closed 边界：没有权威判定就一律 ``t1_unknown``。"""

    def test_no_evidence_provider_means_no_sellable_claim(self):
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)
        context = self.context(session=D1_NEXT, requested=1000, provider=False)
        self.assertEqual(0, context.sellable_quantity)
        self.assertEqual(1000, context.unknown_quantity)
        self.assertEqual("evidence_provider_absent",
                         context.lots[0].sellability_reason)
        self.assertFalse(context.comparable)

    def test_invalid_decision_session_is_position_invalid(self):
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)
        context = self.context(session="not-a-session", requested=1000)
        self.assertEqual(PE.PositionEvidenceStatus.INVALID, context.evidence_status)
        self.assertFalse(context.comparable)

    def test_ambiguous_fill_sessions_are_not_usable(self):
        """同一 lot 的两条流水落在不同 session → 无法用单一 acquisition 描述。"""
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (1, ACCOUNT, "buy", NORMAL, 100, 10.0, 1000.0, 0.0, D2, None, "close"),
        )
        context = self.context(session=D2, requested=1100)
        self.assertEqual(PE.AcquisitionStatus.AMBIGUOUS_SESSION,
                         context.lots[0].acquisition_status)
        self.assertEqual(0, context.sellable_quantity)

    def test_consumed_lots_are_reported_not_silently_dropped(self):
        """已被消耗的 lot 只进诊断桶 —— 本层不重放生产 FIFO。"""
        self.add_lot(qty=1000, session=D1, fill_id=1, order_id=1, lot_id=101,
                     remaining=0)
        self.add_lot(qty=500, session=D1, fill_id=2, order_id=2, lot_id=102)
        context = self.context(session=D1_NEXT, requested=None)
        self.assertEqual(1000, context.consumed_quantity)
        self.assertIn("consumed_lots_excluded_fifo_not_replayed",
                      context.diagnostics)
        self.assertEqual(500, context.held_quantity)

    def test_quantity_basis_is_declared(self):
        self.add_lot(qty=100, session=D1, fill_id=1, order_id=1)
        context = self.context(session=D1_NEXT, requested=100)
        self.assertEqual(PE.QUANTITY_BASIS_OPEN_LOTS, context.quantity_basis)

    def test_pre_t1_gate_falls_back_to_the_t1_authority_not_to_allow(self):
        """生产 verdict 停在 T+1 之前时，问权威的 T+1 入口 —— 而不是放行。"""
        detail = ("该分支必须只通过 ST.earliest_sellable_session 与"
                  " ST.REASON_T1_NOT_SELLABLE 说话")
        source = (PE.__file__ and open(PE.__file__, encoding="utf-8").read()) or ""
        self.assertIn("ST.earliest_sellable_session", source, detail)
        self.assertIn("ST.REASON_T1_NOT_SELLABLE", source, detail)


if __name__ == "__main__":
    unittest.main()
