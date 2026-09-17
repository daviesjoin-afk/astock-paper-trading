# -*- coding: utf-8 -*-
"""§39 对抗性自审：把规格点名的每一种退化都当作攻击面来验。

用法: python work/position_adversarial_self_review.py
只读；不触碰任何生产表。

v2 追加的攻击面（规格 §1–§7 的 blocker）：
历史数量重放、cycle/account 作用域、精确 decision_at、非法 validation_as_of、
身份完整性、请求卖出量严格合法。
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
CREATE TABLE paper_cycles (id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT,
  started_at TEXT, ended_at TEXT, created_at TEXT);
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
CYCLE = 1
CYCLE_OLD = 0
ACCOUNT = "A"
ACCOUNT_B = "B"

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh(*, with_cycle=True):
    """新建一个内存账本。

    ``with_cycle=True``（默认）会写入一条覆盖夹具日期的周期行 —— 真实账本里
    lot 所属的周期必然存在，且 ``started_at`` 可证明。**没有**周期行时归属
    正确地为 unprovable（fail closed），因此需要"周期缺失"语义的用例应显式
    传 ``with_cycle=False``。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    if with_cycle:
        conn.execute(
            "INSERT INTO paper_cycles(id,cycle_key,status,started_at,ended_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (CYCLE, "cycle-test", "running", "2026-01-01", None, "2026-01-01 00:00:00"),
        )
    return conn


def add_lot(conn, oid, code, session, qty, *, order_created=None, verified=True,
            recorded=None, name=None, remaining=None, asset_type=None,
            account=ACCOUNT, cycle_id=CYCLE, available_date=None,
            order_account=None, order_code=None, fill_account=None,
            fill_code=None, executed_at=None):
    name = name or (ETF_NAME if code == ETF else "平安银行")
    asset_type = asset_type or ("etf_t0" if code == ETF else "stock_t1")
    conn.execute(
        "INSERT INTO paper_orders(id,account_id,side,code,name,status,created_at,"
        "executed_at,execution_status,execution_verified,execution_evidence_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (oid, order_account or account, "buy", order_code or code, name, "filled",
         "%s 09:30:00" % (order_created or session),
         executed_at or ("%s 10:00:00" % session),
         "verified" if verified else "unknown", 1 if verified else 0, "ledger"),
    )
    conn.execute(
        "INSERT INTO paper_fills(id,order_id,account_id,side,code,qty,price,amount,"
        "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, oid, fill_account or account, "buy", fill_code or code, qty, 10.0,
         qty * 10.0, 0.0, session, None, "close"),
    )
    conn.execute(
        "INSERT INTO paper_position_lots(id,cycle_id,account_id,code,name,industry,"
        "qty,remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
        "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, cycle_id, account, code, name, None, qty,
         qty if remaining is None else remaining,
         10.0, "%s 10:00:00" % (recorded or session),
         available_date or "2026-10-01", asset_type, oid, 1, 1),
    )


def add_sell_fill(conn, fid, oid, code, session, qty, *, account=ACCOUNT,
                  verified=True, order_account=None, order_code=None,
                  fill_account=None, fill_code=None, cycle_id=CYCLE,
                  executed_at=None, order_cycle_id=None):
    conn.execute(
        "INSERT INTO paper_orders(id,account_id,side,code,name,status,created_at,"
        "executed_at,execution_status,execution_verified,execution_evidence_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (oid, order_account or account, "sell", order_code or code, "平安银行",
         "filled", "%s 09:30:00" % session,
         executed_at or ("%s 10:00:00" % session),
         "verified" if verified else "unknown", 1 if verified else 0, "ledger"),
    )
    conn.execute(
        "INSERT INTO paper_fills(id,order_id,account_id,side,code,qty,price,amount,"
        "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, oid, fill_account or account, "sell", fill_code or code, qty, 10.0,
         qty * 10.0, 0.0, session, None, "close"),
    )


def set_remaining(conn, lot_id, remaining):
    conn.execute("UPDATE paper_position_lots SET remaining_qty=? WHERE id=?",
                 (remaining, lot_id))


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


def ctx(a, code=NORMAL, *, session, account=ACCOUNT, cycle_id=CYCLE,
        requested=None, as_of=None, decision_at=None):
    return a.context_for(code, cycle_id=cycle_id, account_id=account,
                         decision_session=session, decision_at=decision_at,
                         validation_as_of=as_of,
                         requested_sell_quantity=requested)

def main():
    # 1) 真实成交 session vs 订单意图日期
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-17", 1000, order_created="2026-09-16")
    a = adapter(conn)
    c = ctx(a, session="2026-09-17")
    check("真实 fill 覆盖意图日期",
          c.lots[0].acquisition_session == "2026-09-17" and c.t1_locked_quantity == 1000)

    # 2) mixed lots 不得塌缩
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    add_lot(conn, 2, NORMAL, "2026-09-17", 500)
    a = adapter(conn)
    c = ctx(a, session="2026-09-17")
    check("mixed lots 不塌缩",
          c.t1_locked_quantity == 500 and c.sellable_quantity == 1000
          and len(c.acquisition_sessions) == 2)
    over = ctx(a, session="2026-09-17", requested=1200)
    within = ctx(a, session="2026-09-17", requested=900)
    check("partial sell 超可卖额被拦",
          over.sellability_status == PE.SellabilityStatus.T1_BLOCKED)
    check("partial sell 在可卖额内通过",
          within.sellability_status == PE.SellabilityStatus.T1_SELLABLE)

    # 3) 未来证据不得入更早的快照
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-17", 1000, recorded="2026-09-21 13:00:00")
    a = adapter(conn)
    c = ctx(a, session="2026-09-21", as_of="2026-09-21T12:00:00+08:00")
    check("未来证据不入快照",
          c.sellable_quantity == 0 and c.unknown_quantity == 1000
          and not c.comparable)

    # 4) ETF T+0 不得被 T+1 overlay 阻断
    conn = fresh()
    add_lot(conn, 1, ETF, "2026-09-17", 1000)
    a = adapter(conn)
    c = ctx(a, code=ETF, session="2026-09-17", requested=1000)
    check("ETF T+0 不被 T+1 阻断",
          c.sellable_quantity == 1000
          and c.sellability_status == PE.SellabilityStatus.T1_SELLABLE
          and c.lots[0].asset_type_authority == "etf_t0")

    # 5) 周末与法定假日
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-18", 100)
    a = adapter(conn)
    check("周五→周一（周六不可卖）",
          ctx(a, session="2026-09-19").sellability_status
          == PE.SellabilityStatus.T1_BLOCKED
          and ctx(a, session="2026-09-21").sellability_status
          == PE.SellabilityStatus.T1_SELLABLE)
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-30", 100)
    a = adapter(conn)
    check("国庆假期：10-01 不可卖、10-08 可卖",
          ctx(a, session="2026-10-01").sellability_status
          == PE.SellabilityStatus.T1_BLOCKED
          and ctx(a, session="2026-10-08").sellability_status
          == PE.SellabilityStatus.T1_SELLABLE)

    # 6) 零 authority：lot 账本不得被任何观察改写
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    before = [tuple(r) for r in conn.execute(
        "SELECT id,remaining_qty,available_date FROM paper_position_lots")]
    a = adapter(conn)
    for quantity in (None, 500, 1000, 99999):
        ctx(a, session="2026-09-17", requested=quantity)
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
    observer = PS.PositionShadowObserver(TS.ShadowComparator(repo), a,
                                        cycle_id=CYCLE, account_id=ACCOUNT)
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
    buy = PS.PositionShadowObserver(None, a, cycle_id=CYCLE, account_id=ACCOUNT)
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

    # ─────────── v2 新增攻击面（规格 §1–§7） ───────────

    # 9) HIST-Q：今天余额绝不能冒充决策时点数量
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 800)
    set_remaining(conn, 1, 200)
    a = adapter(conn)
    c = ctx(a, session="2026-09-16")
    check("历史数量：决策时点 1000 而非今天 200",
          c.held_quantity == 1000 and c.quantity_basis == PE.QUANTITY_BASIS_HISTORICAL_REPLAY,
          "held=%s basis=%s" % (c.held_quantity, c.quantity_basis))

    # 10) 完全消耗的历史 lot 不得从快照消失
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 1000)
    set_remaining(conn, 1, 0)
    a = adapter(conn)
    c = ctx(a, session="2026-09-16")
    check("完全消耗的 lot 仍在历史快照里",
          c.held_quantity == 1000 and len(c.lots) == 1)

    # 11) 不可重放 → fail closed，绝不退回当前余额
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    set_remaining(conn, 1, 200)
    a = adapter(conn)
    c = ctx(a, session="2026-09-16")
    check("不可重放 → position_unprovable（不退回 200）",
          c.evidence_status == PE.PositionEvidenceStatus.UNPROVABLE
          and c.held_quantity != 200 and c.sellable_quantity == 0,
          "held=%s" % c.held_quantity)

    # 12) cycle 隔离
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, cycle_id=CYCLE)
    add_lot(conn, 2, NORMAL, "2026-09-16", 9000, cycle_id=CYCLE_OLD)
    a = adapter(conn)
    c = ctx(a, session="2026-09-17", cycle_id=CYCLE)
    check("跨 cycle lot 不可见",
          c.held_quantity == 1000 and len(c.lots) == 1 and c.cycle_id == CYCLE)

    # 13) account 隔离（同 code 不同账户绝不池化）
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, account=ACCOUNT)
    add_lot(conn, 2, NORMAL, "2026-09-17", 500, account=ACCOUNT_B)
    a = adapter(conn)
    ca = ctx(a, session="2026-09-17", account=ACCOUNT)
    cb = ctx(a, session="2026-09-17", account=ACCOUNT_B)
    check("同 code 跨账户不池化",
          ca.held_quantity == 1000 and cb.held_quantity == 500
          and ca.held_quantity != 1500 and cb.held_quantity != 1500)

    # 14) 缺作用域必须被拒绝
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    a = adapter(conn)
    check("缺 account 作用域被拒绝",
          ctx(a, session="2026-09-16", account=None).evidence_status
          == PE.PositionEvidenceStatus.INVALID)
    check("缺 cycle 作用域被拒绝",
          ctx(a, session="2026-09-16", cycle_id=None).evidence_status
          == PE.PositionEvidenceStatus.INVALID)

    # 15) 精确 decision_at 必须被消费
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-17", 1000, recorded="2026-09-17 13:00:00")
    a = adapter(conn)
    early = ctx(a, session="2026-09-17", decision_at="2026-09-17T09:31:00+08:00")
    late = ctx(a, session="2026-09-17", decision_at="2026-09-17T15:00:00+08:00")
    check("同 session 内精确 decision_at 生效",
          early.unknown_quantity == 1000 and early.sellable_quantity == 0
          and late.held_quantity == 1000)
    check("身份保留精确 decision_at",
          early.decision_at == "2026-09-17T09:31:00+08:00"
          and late.decision_at == "2026-09-17T15:00:00+08:00")

    # 16) 显式非法 validation_as_of 绝不放宽成无上界
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, recorded="2030-01-01 10:00:00")
    a = adapter(conn)
    bad = ctx(a, session="2026-09-17", as_of="not-a-timestamp", requested=1000)
    check("非法 validation_as_of → position_invalid",
          bad.evidence_status == PE.PositionEvidenceStatus.INVALID
          and bad.sellable_quantity == 0)

    # 17) 身份完整性
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, fill_account=ACCOUNT_B)
    a = adapter(conn)
    check("跨账户成交不能证明本 lot",
          ctx(a, session="2026-09-17").lots[0].acquisition_status
          == PE.AcquisitionStatus.IDENTITY_MISMATCH)
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, order_code="000002")
    a = adapter(conn)
    check("跨股票委托不能证明本 lot",
          ctx(a, session="2026-09-17").lots[0].acquisition_status
          == PE.AcquisitionStatus.IDENTITY_MISMATCH)

    # 18) 请求卖出量严格合法
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000)
    a = adapter(conn)
    bad_qty = [ctx(a, session="2026-09-17", requested=v)
               for v in (0, -1, -1000, "abc", "0", "")]
    check("非正/非法请求量全部 fail closed",
          all(item.evidence_status == PE.PositionEvidenceStatus.INVALID
              for item in bad_qty)
          and all(item.sellability_status != PE.SellabilityStatus.T1_SELLABLE
                  for item in bad_qty))
    check("正数请求量仍正常工作",
          ctx(a, session="2026-09-17", requested=1000).sellability_status
          == PE.SellabilityStatus.T1_SELLABLE)

    # ── ROUND-2：同 session 成交时刻 / 跨周期 / 越卖 / 身份 / 知识时点 ──

    # 攻击面 A：同 session 卖出必须按 executed_at，而不是 session 收盘
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 400,
                  executed_at="2026-09-17 10:00:00")
    set_remaining(conn, 1, 600)
    c = ctx(adapter(conn), session="2026-09-17", decision_at="2026-09-17T14:00:00+08:00")
    check("14:00 回看 10:00 已成交的卖出 → 已扣除（600）",
          c.held_quantity == 600, "held=%s basis=%s" % (c.held_quantity, c.quantity_basis))

    # 攻击面 B：决策时点早于成交时刻 → 尚未扣除
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 400,
                  executed_at="2026-09-17 11:00:00")
    set_remaining(conn, 1, 600)
    c = ctx(adapter(conn), session="2026-09-17", decision_at="2026-09-17T10:00:00+08:00")
    check("10:00 回看 11:00 才成交的卖出 → 尚未扣除（1000）",
          c.held_quantity == 1000, "held=%s basis=%s" % (c.held_quantity, c.quantity_basis))

    # 攻击面 C：拿不到 executed_at 且决策时点在盘中 → 必须 fail closed，不许猜
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 400)
    conn.execute("UPDATE paper_orders SET executed_at=NULL WHERE id=9")
    set_remaining(conn, 1, 600)
    c = ctx(adapter(conn), session="2026-09-17", decision_at="2026-09-17T10:00:00+08:00")
    check("盘中无成交时刻 → 不可重建（不猜已发生/未发生）",
          c.quantity_basis == PE.QUANTITY_BASIS_UNPROVABLE and not c.comparable,
          "basis=%s comparable=%s" % (c.quantity_basis, c.comparable))

    # 攻击面 D：周期窗之外的卖出不得扣减本周期 lot
    conn = fresh()
    conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?", ("2026-09-17", CYCLE))
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    # 卖在 09-16，早于周期开始日 09-17 → 属于别的资金池。
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-16", 1000)
    c = ctx(adapter(conn), session="2026-09-17", decision_at=AS_OF)
    check("周期窗之外的卖出不扣减本周期 lot（held=1000）",
          c.held_quantity == 1000, "held=%s basis=%s" % (c.held_quantity, c.quantity_basis))

    # 攻击面 D2：窗内的同一笔卖出确实扣减（反向对照，证明差异来自窗）
    conn = fresh()
    conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?", ("2026-09-16", CYCLE))
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 1000)
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), session="2026-09-17", decision_at=AS_OF)
    check("周期窗之内的卖出确实扣减（held=0）",
          c.held_quantity == 0 and c.quantity_basis == PE.QUANTITY_BASIS_HISTORICAL_REPLAY,
          "held=%s basis=%s" % (c.held_quantity, c.quantity_basis))

    # 攻击面 E：越卖（卖出量 > 当时可卖 lots）→ 立刻 unprovable，绝不 continue
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 5000)
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), session="2026-09-17", decision_at=AS_OF)
    check("越卖 → unprovable（不把不可证明粉饰成可卖）",
          c.quantity_basis == PE.QUANTITY_BASIS_UNPROVABLE and not c.comparable,
          "basis=%s comparable=%s" % (c.quantity_basis, c.comparable))

    # 攻击面 F：观察身份必须区分账户与周期
    def _cmp(**over):
        base = dict(
            code=NORMAL, session="2026-09-17", side="sell", decision_at=AS_OF,
            validation_as_of=AS_OF, market_status="comparable",
            market_production_allowed=True, position_status="comparable_t1_pass",
            position_evidence_status="position_proven",
            position_sellability_status="t1_sellable", position_comparable=True,
            account_id=ACCOUNT, cycle_id=CYCLE, held_quantity=1000,
            sellable_quantity=1000, t1_locked_quantity=0, unknown_quantity=0,
            requested_sell_quantity=1000,
        )
        base.update(over)
        return PS.PositionShadowComparison(**base)

    check("身份区分两个账户",
          _cmp().identity() != _cmp(account_id=ACCOUNT_B).identity())
    check("身份区分两个周期",
          _cmp().identity() != _cmp(cycle_id=CYCLE_OLD).identity())
    check("指纹也随账户变化",
          _cmp().fingerprint() != _cmp(account_id=ACCOUNT_B).fingerprint())

    # ── ROUND-3：future-lot 时序 / 周期归属歧义 / 事件排序 ──

    # 攻击面 F：T+0 ETF —— 13:00 才买入的 lot 绝不能满足 10:00 的卖出
    conn = fresh()
    add_lot(conn, 1, ETF, "2026-09-17", 100, available_date="2026-09-17",
            asset_type="etf_t0", executed_at="2026-09-17 09:00:00")
    add_lot(conn, 2, ETF, "2026-09-17", 100, available_date="2026-09-17",
            asset_type="etf_t0", executed_at="2026-09-17 13:00:00")
    add_sell_fill(conn, 9, 9, ETF, "2026-09-17", 100,
                  executed_at="2026-09-17 10:00:00")
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), code=ETF, session="2026-09-17", decision_at=AS_OF)
    check("FUTURE-LOT-1：10:00 卖出只吃 09:00 的 lot，13:00 lot 完整保留",
          c.held_quantity == 100 and c.quantity_basis == PE.QUANTITY_BASIS_HISTORICAL_REPLAY,
          "held=%s basis=%s" % (c.held_quantity, c.quantity_basis))

    # 攻击面 G：当时根本不够卖 → 绝不用未来 lot 补齐后宣布 proven
    conn = fresh()
    add_lot(conn, 1, ETF, "2026-09-17", 50, available_date="2026-09-17",
            asset_type="etf_t0", executed_at="2026-09-17 09:00:00")
    add_lot(conn, 2, ETF, "2026-09-17", 100, available_date="2026-09-17",
            asset_type="etf_t0", executed_at="2026-09-17 13:00:00")
    add_sell_fill(conn, 9, 9, ETF, "2026-09-17", 100,
                  executed_at="2026-09-17 10:00:00")
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), code=ETF, session="2026-09-17", decision_at=AS_OF)
    check("FUTURE-LOT-2：缺口只能由未来 lot 补足 → unprovable + 点名根因",
          c.quantity_basis == PE.QUANTITY_BASIS_UNPROVABLE
          and "future_lot_consumption_required" in c.diagnostics,
          "basis=%s diags=%s" % (c.quantity_basis, c.diagnostics))

    # 攻击面 H：重叠开放周期 → 归属歧义必须 fail closed
    conn = fresh()
    conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?", ("2026-09-15", CYCLE))
    conn.execute(
        "INSERT INTO paper_cycles(id,cycle_key,status,started_at,ended_at,created_at)"
        " VALUES(?,?,?,?,?,?)", (CYCLE + 1, "c-b", "running", "2026-09-16", None,
                                 "2026-09-16 00:00:00"))
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 1000)
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), session="2026-09-17", decision_at=AS_OF)
    check("重叠周期 → 归属歧义 fail closed（不得直接采信请求周期）",
          c.quantity_basis == PE.QUANTITY_BASIS_UNPROVABLE
          and "sell_fill_cycle_ambiguous" in c.diagnostics,
          "basis=%s diags=%s" % (c.quantity_basis, c.diagnostics))

    # 攻击面 I：周期起点不可证明 → unprovable（不得默认 proven）
    conn = fresh()
    conn.execute(
        "UPDATE paper_cycles SET started_at=NULL, created_at=NULL WHERE id=?", (CYCLE,))
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 1000)
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), session="2026-09-17", decision_at=AS_OF)
    check("周期起点不可证明 → unprovable（不默认 cycle_ok=True）",
          c.quantity_basis == PE.QUANTITY_BASIS_UNPROVABLE
          and "sell_fill_cycle_unprovable" in c.diagnostics,
          "basis=%s diags=%s" % (c.quantity_basis, c.diagnostics))

    # 攻击面 J：同日两笔卖出的顺序必须按真实成交时刻（fill_id 顺序相反）
    conn = fresh()
    add_lot(conn, 1, NORMAL, "2026-09-16", 100, available_date="2026-09-17")
    # fill_id 顺序与真实时间相反：先写入 14:00，再写入 10:00。
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 30,
                  executed_at="2026-09-17 14:00:00")
    add_sell_fill(conn, 10, 10, NORMAL, "2026-09-17", 30,
                  executed_at="2026-09-17 10:00:00")
    set_remaining(conn, 1, 40)
    c = ctx(adapter(conn), session="2026-09-17", decision_at="2026-09-17T11:00:00+08:00")
    check("同日两笔卖出按真实时刻切分（11:00 回看 = 70，不是 100）",
          c.held_quantity == 70, "held=%s" % (c.held_quantity,))

    # 攻击面 K：paused 且起点未知的其它周期不得凭空否决一笔可证明的归属
    conn = fresh()
    conn.execute("UPDATE paper_cycles SET started_at=? WHERE id=?", ("2026-09-15", CYCLE))
    conn.execute(
        "INSERT INTO paper_cycles(id,cycle_key,status,started_at,ended_at,created_at)"
        " VALUES(?,?,?,?,?,?)", (CYCLE + 1, "c-paused", "paused", None, None,
                                 "2026-09-15 00:00:00"))
    add_lot(conn, 1, NORMAL, "2026-09-16", 1000, available_date="2026-09-17")
    add_sell_fill(conn, 9, 9, NORMAL, "2026-09-17", 1000)
    set_remaining(conn, 1, 0)
    c = ctx(adapter(conn), session="2026-09-17", decision_at=AS_OF)
    check("起点未知的 paused 周期不否决可证明归属（held=0 且 proven）",
          c.held_quantity == 0
          and c.quantity_basis == PE.QUANTITY_BASIS_HISTORICAL_REPLAY,
          "held=%s basis=%s diags=%s" % (c.held_quantity, c.quantity_basis,
                                         c.diagnostics))

    failed = [name for name, ok in _checks if not ok]
    print("\n%d/%d checks passed" % (len(_checks) - len(failed), len(_checks)))
    if failed:
        print("failed: %s" % (failed,))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
