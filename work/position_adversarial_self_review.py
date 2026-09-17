# -*- coding: utf-8 -*-
"""§39 对抗性自审：把规格点名的每一种退化都当作攻击面来验。

用法: python work/position_adversarial_self_review.py
只读；不触碰任何生产表。
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import selection_tradability as ST          # noqa: E402
import tradability_archive as TA            # noqa: E402
import tradability_position_evidence as PE  # noqa: E402
import tradability_position_shadow as PS    # noqa: E402
import tradability_shadow as TS             # noqa: E402

DDL = """
CREATE TABLE paper_orders (id INTEGER PRIMARY KEY, account_id TEXT, side TEXT,
  code TEXT, name TEXT, status TEXT, created_at TEXT, executed_at TEXT,
  execution_status TEXT, execution_verified INTEGER, execution_evidence_source TEXT);
CREATE TABLE paper_fills (id INTEGER PRIMARY KEY, order_id INTEGER, account_id TEXT,
  side TEXT, code TEXT, qty INTEGER, price REAL, amount REAL, fees REAL,
  fill_date TEXT, quote_at TEXT, assumption TEXT);
CREATE TABLE paper_position_lots (id INTEGER PRIMARY KEY, cycle_id INTEGER,
  account_id TEXT, code TEXT, name TEXT, industry TEXT, qty INTEGER,
  remaining_qty INTEGER, cost REAL, acquired_at TEXT, available_date TEXT,
  asset_type TEXT, source_order_id INTEGER, cost_fee_included INTEGER,
  is_t_base INTEGER);
"""

NORMAL = "600001"
ETF = "510300"
ETF_NAME = "沪深300ETF"
AS_OF = "2026-10-09T16:00:00+08:00"

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def add_lot(conn, oid, code, session, qty, *, order_created=None, verified=True,
            recorded=None, name=None, remaining=None, asset_type=None):
    name = name or ("沪深300ETF" if code == ETF else "平安银行")
    asset_type = asset_type or ("etf_t0" if code == ETF else "stock_t1")
    conn.execute(
        "INSERT INTO paper_orders(id,account_id,side,code,name,status,created_at,"
        "executed_at,execution_status,execution_verified,execution_evidence_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "A", "buy", code, name, "filled",
         "%s 09:30:00" % (order_created or session), "%s 10:00:00" % session,
         "verified" if verified else "unknown", 1 if verified else 0, "ledger"),
    )
    conn.execute(
        "INSERT INTO paper_fills(id,order_id,account_id,side,code,qty,price,amount,"
        "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, oid, "A", "buy", code, qty, 10.0, qty * 10.0, 0.0, session, None, "close"),
    )
    conn.execute(
        "INSERT INTO paper_position_lots(id,cycle_id,account_id,code,name,industry,"
        "qty,remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
        "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, 1, "A", code, name, None, qty, qty if remaining is None else remaining,
         10.0, "%s 10:00:00" % (recorded or session), "2026-10-01", asset_type, oid, 1, 1),
    )


def adapter(conn):
    def provider(code, session):
        name = ETF_NAME if code == ETF else "平安银行"
        return ST.MarketEvidence(
            session=session, available_at=ST.session_close_at(session),
            price=4.2 if code == ETF else 10.6,
            reference_price=4.1 if code == ETF else 10.5,
            volume=1_000_000.0, halted=False, name=name, risk_flag=None,
        )
    return PE.PositionEvidenceAdapter(conn, evidence_provider=provider)


def main():
    # 1) 真实成交 session vs 订单意图日期
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-17", 1000, order_created="2026-09-16")
    a = adapter(conn)
    ctx = a.context_for(NORMAL, account_id="A", decision_session="2026-09-17")
    check("真实 fill 覆盖意图日期",
          ctx.lots[0].acquisition_session == "2026-09-17" and ctx.t1_locked_quantity == 1000)

    # 2) mixed lots 不得塌缩
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    add_lot(conn, 2, NORMAL, "2026-09-17", 500)
    a = adapter(conn)
    ctx = a.context_for(NORMAL, account_id="A", decision_session="2026-09-17")
    check("mixed lots 不塌缩",
          ctx.t1_locked_quantity == 500 and ctx.sellable_quantity == 1000
          and len(ctx.acquisition_sessions) == 2)
    over = a.context_for(NORMAL, account_id="A", decision_session="2026-09-17",
                         requested_sell_quantity=1200)
    within = a.context_for(NORMAL, account_id="A", decision_session="2026-09-17",
                           requested_sell_quantity=900)
    check("partial sell 超可卖额被拦", over.sellability_status == PE.SellabilityStatus.T1_BLOCKED)
    check("partial sell 在可卖额内通过",
          within.sellability_status == PE.SellabilityStatus.T1_SELLABLE)

    # 3) 未来证据不得入更早的快照
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-17", 1000, recorded="2026-09-21 13:00:00")
    a = adapter(conn)
    ctx = a.context_for(NORMAL, account_id="A", decision_session="2026-09-21",
                        validation_as_of="2026-09-21T12:00:00+08:00")
    check("未来证据不入快照",
          ctx.sellable_quantity == 0 and ctx.unknown_quantity == 1000
          and not ctx.comparable)

    # 4) ETF T+0 不得被 T+1 overlay 阻断
    conn = fresh()
    add_lot(conn, 1, ETF, "2026-09-17", 1000)
    a = adapter(conn)
    ctx = a.context_for(ETF, account_id="A", decision_session="2026-09-17",
                        requested_sell_quantity=1000)
    check("ETF T+0 不被 T+1 阻断",
          ctx.sellable_quantity == 1000
          and ctx.sellability_status == PE.SellabilityStatus.T1_SELLABLE
          and ctx.lots[0].asset_type_authority == "etf_t0")

    # 5) 周末与法定假日
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-18", 100)
    a = adapter(conn)
    check("周五→周一（周六不可卖）",
          a.context_for(NORMAL, account_id="A",
                        decision_session="2026-09-19").sellability_status
          == PE.SellabilityStatus.T1_BLOCKED
          and a.context_for(NORMAL, account_id="A",
                            decision_session="2026-09-21").sellability_status
          == PE.SellabilityStatus.T1_SELLABLE)
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-30", 100)
    a = adapter(conn)
    check("国庆假期：10-01 不可卖、10-08 可卖",
          a.context_for(NORMAL, account_id="A",
                        decision_session="2026-10-01").sellability_status
          == PE.SellabilityStatus.T1_BLOCKED
          and a.context_for(NORMAL, account_id="A",
                            decision_session="2026-10-08").sellability_status
          == PE.SellabilityStatus.T1_SELLABLE)

    # 6) 零 authority：lot 账本不得被任何观察改写
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    before = [tuple(r) for r in conn.execute(
        "SELECT id,remaining_qty,available_date FROM paper_position_lots")]
    a = adapter(conn)
    for quantity in (None, 0, 500, 1000, 99999):
        a.context_for(NORMAL, account_id="A", decision_session="2026-09-17",
                      requested_sell_quantity=quantity)
    after = [tuple(r) for r in conn.execute(
        "SELECT id,remaining_qty,available_date FROM paper_position_lots")]
    check("零 authority：lot 账本逐字节不变", before == after)

    # 7) 比较分母：不可比不进 agreement / disagreement
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    TA.ensure_schema(conn)
    repo = TA.TradabilityArchiveRepository(conn)
    repo.save(TA.normalize_record({
        "code": NORMAL, "session_date": "2026-09-17", "source": "fix",
        "observed_at": "2026-09-17T15:05:00", "effective_at": "2026-09-17T15:05:00",
        "is_listed": True, "is_st": False, "is_suspended": False,
        "has_market_quote": True, "has_trade_volume": True,
    }))
    a = adapter(conn)
    observer = PS.PositionShadowObserver(TS.ShadowComparator(repo), a, account_id="A")
    comparable = observer.observe(
        code=NORMAL, session="2026-09-17", side=ST.SIDE_SELL,
        production_verdict=ST.exit_tradability(a._evidence_provider(NORMAL, "2026-09-17"),
                                               code=NORMAL, exit_session="2026-09-17"),
        requested_sell_quantity=1000, validation_as_of=AS_OF)
    unprovable = observer.observe(
        code="600999", session="2026-09-17", side=ST.SIDE_SELL,
        production_verdict=ST.exit_tradability(a._evidence_provider(NORMAL, "2026-09-17"),
                                               code="600999", exit_session="2026-09-17"),
        requested_sell_quantity=100, validation_as_of=AS_OF)
    summary = observer.summarize([comparable, unprovable]).to_dict()
    check("不可比不进分母",
          summary["sell_comparisons"] == 2 and summary["position_comparable"] == 1
          and summary["position_not_comparable"] == 1)
    check("分母为 0 → None",
          PS.PositionShadowSummary().to_dict()["position_comparison_rate"] is None)

    # 8) BUY 逐字段与市场层面一致
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    TA.ensure_schema(conn)
    a = adapter(conn)
    buy = observer_buy = PS.PositionShadowObserver(None, a, account_id="A")
    market = TS.ShadowComparator(TA.TradabilityArchiveRepository(conn)).compare(
        ST.entry_tradability(a._evidence_provider(NORMAL, "2026-09-17"),
                             code=NORMAL, entry_session="2026-09-17"),
        code=NORMAL, session="2026-09-17", side=ST.SIDE_BUY,
        decision_at=ST.session_close_at("2026-09-17"), validation_as_of=AS_OF)
    observed = buy.observe(
        code=NORMAL, session="2026-09-17", side=ST.SIDE_BUY,
        production_verdict=ST.entry_tradability(a._evidence_provider(NORMAL, "2026-09-17"),
                                                code=NORMAL, entry_session="2026-09-17"),
        validation_as_of=AS_OF, market_comparison=market)
    check("BUY 与市场层面逐字段一致",
          observed.position_status == market.status
          and observed.market_status == market.status
          and observed.held_quantity == 0
          and observed.requested_sell_quantity is None)

    failed = [name for name, ok in _checks if not ok]
    print("\n%d/%d checks passed" % (len(_checks) - len(failed), len(_checks)))
    if failed:
        print("failed: %s" % (failed,))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
