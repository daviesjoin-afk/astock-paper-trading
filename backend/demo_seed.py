# -*- coding: utf-8 -*-
"""Deterministic demo ledger and synthetic market data.

``ASTOCK_DEMO=1`` boots the application into demo mode: a fully synthetic
universe (10 virtual tickers on allowed 600xxx prefixes), a matching snapshot
file, and a seeded paper ledger that shows a complete auditable chain:

  signal -> risk decision -> order -> fill -> position -> NAV -> audit

The demo ledger includes: a normal buy fill, a same-day T+1 sell rejection, a
stale-quote rejection, a hard-stop sell, a limit-up buy, position reviews and
several days of NAV per strategy.  Every price, name and decision is synthetic
and intentionally does not reference any real company or account.

Data files live under ``data_cache/`` exactly like production; dashboard,
overview, health, activity and risk-audit pages are pure cache/ledger reads,
so no code change is required in the read models.  Re-running is idempotent
(a ``demo_seeded`` audit marker is checked before seeding again).
"""
from __future__ import annotations

import json
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_fetcher as dfc  # noqa: E402
import universe as U  # noqa: E402
import paper_trading as PT  # noqa: E402

# 10 virtual tickers. Prefix 600xxx passes paper_trading_rules.security_scope
# so every ledger path behaves like a real listed board; names are invented
# and never match an actual company.
DEMO_TICKERS = [
    # code, name, industry, price, pct, prev_close
    ("600901", "晨星锂电", "电池", 18.50, +0.85, 18.34),
    ("600902", "蓝湾数据", "数据要素", 45.20, -1.20, 45.75),
    ("600903", "海岳风能", "风电", 9.60, +2.30, 9.38),
    ("600904", "逐日新材", "新材料", 22.10, +4.90, 21.07),
    ("600905", "汇联医疗", "医疗器械", 56.40, -6.10, 60.06),
    ("600906", "极光智能", "算力服务", 27.50, +9.98, 25.01),
    ("600907", "云图传媒", "传媒", 12.30, -0.40, 12.35),
    ("600908", "恒锐智造", "工业母机", 33.80, +1.60, 33.27),
    ("600909", "深蓝光学", "消费电子", 8.95, -2.10, 9.14),
    ("600910", "青禾农业", "种植业", 6.70, +0.30, 6.68),
]

DEMO_CAPITAL = 1_000_000.0


def _iso_days_ago(days):
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _ts(days_ago, hhmm="10:30:00"):
    return f"{_iso_days_ago(days_ago)} {hhmm}"


def _write_universe():
    stocks = []
    for code, name, industry, price, pct, prev_close in DEMO_TICKERS:
        stocks.append({
            "code": code, "name": name, "board": "主板",
            "risk_flag": 0, "snapshot_tradable": True, "listing_status": "listed",
            "price": price, "pct": pct, "prev_close": prev_close,
            "open_price": round(prev_close * (1 + min(pct, 9.98) / 100.0), 2),
            "high": round(price * 1.03, 2), "low": round(price * 0.97, 2),
            "volume": 800000 + int(code) * 137, "amount": round(price * (800000 + int(code) * 137), 2),
            "turnover": round(1.0 + (int(code) % 900) / 100.0, 2),
            "pe": round(8.0 + (int(code) % 400) / 10.0, 2), "pb": round(1.0 + (int(code) % 300) / 100.0, 2),
            "mktcap": round(price * 900000000, 2), "float_cap": round(price * 360000000, 2),
            "industry": industry, "main_net": (int(code) % 7) * 10 ** 6 - 3 * 10 ** 6,
            "quote_at": _ts(0, "15:15:00"),
        })
    payload = {
        "built_at": _ts(0, "08:00:00"), "scope": "demo_synthetic",
        "requested_limit": len(stocks), "demo": True, "stocks": stocks,
    }
    os.makedirs(dfc.CACHE_DIR, exist_ok=True)
    with open(U.UNIVERSE_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    # /api/health reads market_snapshot_full.json directly (rows/saved_at);
    # the dashboard quote loader needs >= FULL_MARKET_MIN_ROWS rows, so it
    # skips this file and falls back to universe static marks — fine for a
    # read-only demo ledger.
    snapshot = {
        "saved_at": _ts(0, "15:15:00"), "complete": True,
        "expected_rows": len(stocks), "quote_at_min": _ts(0, "15:15:00"),
        "rows": [
            {k: v for k, v in s.items() if k != "quote_at"} | {"quote_at": _ts(0, "15:15:00")}
            for s in stocks
        ],
    }
    with open(dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)


def _now():
    return PT._now()


def _decision(conn, account_id, code, side, decision, reason, name):
    strategy_stamp = PT._strategy_stamp(conn, account_id)
    PT._rows(conn, """
        INSERT INTO paper_risk_decisions(account_id,code,side,decision,reason,payload,created_at,
                                         strategy_id,strategy_version,strategy_checksum)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (
        account_id, code, side, decision, reason,
        json.dumps({"name": name, "decision_note": reason}, ensure_ascii=False), _now(),
        *strategy_stamp,
    ))


def _order(conn, account_id, code, name, side, qty, status, reason, created_at, planned=None, filled=None):
    price = filled if filled is not None else (planned if planned is not None else 0.0)
    strategy_stamp = PT._strategy_stamp(conn, account_id)
    cur = conn.execute(
        "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,strategy_id,strategy_version,strategy_checksum) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, side, code, name, qty,
         planned if planned is not None else price, filled, round(price * qty, 2),
         round(price * qty * 0.0003, 2), status, reason, "{}", created_at,
         created_at if status == "filled" else None, *strategy_stamp),
    )
    return cur.lastrowid


def _fill(conn, order_id, account_id, code, side, qty, price, created_at):
    PT._rows(conn, """
        INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
        order_id, account_id, side, code, qty, price, round(price * qty, 2),
        round(price * qty * 0.0003, 2), created_at[:10], created_at, "snapshot_price_rule",
    ))


def _position(conn, cycle_id, account_id, code, name, industry, qty, cost, entry_day, available_day):
    PT._rows(conn, """
        INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,available_date,asset_type,peak_price,take_stage)
        VALUES (?,?,?,?,?,?,?,?,'stock_t1',?,0)""",
        (account_id, code, name, industry, qty, cost, entry_day, available_day, round(cost * 1.03, 2)))
    PT._rows(conn, """
        INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,cost_fee_included,is_t_base)
        VALUES (?,?,?,?,?,?,?,?,?,?,'stock_t1',NULL,0,1)""",
        (cycle_id, account_id, code, name, industry, qty, qty, cost, f"{entry_day} 10:00:00", available_day))


def _drop_position(conn, account_id, code):
    PT._rows(conn, "DELETE FROM paper_positions WHERE account_id=? AND code=?", (account_id, code))
    PT._rows(conn, "UPDATE paper_position_lots SET remaining_qty=0 WHERE account_id=? AND code=?",
             (account_id, code))


def _review(conn, cycle_id, account_id, code, review_date, score, grade, action, market_value, position_pct,
            reasons, detail, replacement_code=None, replacement_score=None):
    PT._rows(conn, """
        INSERT INTO paper_position_reviews
        (cycle_id,account_id,code,review_date,score,grade,action,market_value,position_pct,
         replacement_code,replacement_score,reasons,detail,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(cycle_id,account_id,code,review_date) DO NOTHING""",
        (cycle_id, account_id, code, review_date, score, grade, action, market_value, position_pct,
         replacement_code, replacement_score, json.dumps(reasons, ensure_ascii=False),
         json.dumps(detail, ensure_ascii=False), f"{review_date} 15:05:00"))


def _nav_days(conn, accounts, share, day_counts):
    # day_counts[account_index][day_index] = percent move vs share base
    nav_dates = [_iso_days_ago(d) for d in range(4, -1, -1)]
    for day_index, nav_date in enumerate(nav_dates):
        for acct_index, account_id in enumerate(accounts):
            nav = share * (1 + day_counts[acct_index][day_index] / 100.0)
            PT._rows(conn, """
                INSERT OR IGNORE INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at,quote_status)
                VALUES (?,?,?,?,?,?,?,'verified')""",
                (account_id, nav_date, share, nav - share, round(nav, 2),
                 round(share * 0.99 * (1 + (day_index * 0.12) / 100.0), 2), f"{nav_date} 15:10:00"))


def ensure_demo_data(force=False):
    """Idempotently seed the synthetic universe + ledger. Returns True when seeded."""
    # Make the read models treat the small synthetic snapshot as complete so
    # dashboard/risk paths use synthetic marks instead of network fallbacks.
    os.environ["ASTOCK_FULL_MARKET_MIN_ROWS"] = "1"
    PT.init_db()
    with PT._db() as conn:
        marker = conn.execute(
            "SELECT 1 FROM paper_audit WHERE event='demo_seeded' LIMIT 1"
        ).fetchone()
        if marker and not force:
            return False
        conn.execute(
            "DELETE FROM paper_audit WHERE event='demo_seeded'"
        ) if marker else None

    _write_universe()

    with PT._db() as conn:
        # Rebuild a running cycle so the ledger is deterministic on every
        # (re)seed: archive whatever draft/running/paused cycle exists first.
        current = PT._rows(
            conn,
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1",
        )
        if current:
            PT._archive_current_cycle(conn, "demo seed 重建确定性演示周期")
        cycle = PT._create_cycle(conn, DEMO_CAPITAL, status="running", reason="确定性演示账本播种")
        cycle_id = cycle["id"]
        accounts = [row["id"] for row in PT._rows(conn, "SELECT id FROM paper_accounts ORDER BY id")]
        share = DEMO_CAPITAL / max(len(accounts), 1)
        # deterministic assignment: first two accounts carry the story
        acc_hold = accounts[0]     # 正常买入 + T+1 卖拒
        acc_flow = accounts[1]     # 行情过期拒 + 止损
        acc_live = accounts[2]     # 涨停成交

        # --- 1) D-1 normal buy fills (positions remain open) ---
        for account_id, code, name, qty, price, day in (
            (acc_hold, "600901", "晨星锂电", 2000, 18.50, 1),
            (acc_flow, "600902", "蓝湾数据", 300, 45.20, 1),
        ):
            oid = _order(conn, account_id, code, name, "buy", qty, "filled", "策略入场通过全部门禁",
                         _ts(day, "10:02:00"), planned=price, filled=price)
            _fill(conn, oid, account_id, code, "buy", qty, price, _ts(day, "10:02:00"))
            _position(conn, cycle_id, account_id, code, name, "合成", qty, price, _iso_days_ago(day),
                      _iso_days_ago(day - 1))
            _decision(conn, account_id, code, "buy", "entry_approved",
                      f"{name} 通过行情/资金/T+1/风控门禁，限价 {price} 元成交", name)

        # --- 2) same-day T+1 sell rejection on a today-bought name ---
        buy_today_oid = _order(conn, acc_hold, "600904", "逐日新材", "buy", 400, "filled",
                               "盘中买入成交（当日 T+1 锁定）", _ts(0, "09:45:00"),
                               planned=22.10, filled=22.10)
        _fill(conn, buy_today_oid, acc_hold, "600904", "buy", 400, 22.10, _ts(0, "09:45:00"))
        _position(conn, cycle_id, acc_hold, "600904", "逐日新材", "新材料", 400, 22.10,
                  _iso_days_ago(0), _iso_days_ago(1))
        _decision(conn, acc_hold, "600904", "buy", "entry_approved", "逐日新材 今日买入成交", "逐日新材")
        _order(conn, acc_hold, "600904", "逐日新材", "sell", 400, "risk_rejected",
                        "T+1：今日买入份额最早可卖日期为明日，拒绝卖出", _ts(0, "10:05:00"),
                        planned=22.40, filled=None)
        _decision(conn, acc_hold, "600904", "sell", "t1_rejected",
                  "逐日新材 T+1 门禁拒绝：当日买入不可当日卖出", "逐日新材")

        # --- 3) stale-quote rejection (market snapshot older than policy) ---
        _order(conn, acc_flow, "600903", "海岳风能", "buy", 5000, "risk_rejected",
               "行情过期：最新快照超出门禁时长，拒绝以陈旧价入场", _ts(0, "10:40:00"),
               planned=9.62, filled=None)
        _decision(conn, acc_flow, "600903", "buy", "stale_quote_rejected",
                  "海岳风能 行情快照已过期，拒绝基于陈旧价格的委托", "海岳风能")

        # --- 4) hard-stop loss: bought D-2, stopped out D-0 ---
        stop_buy_oid = _order(conn, acc_flow, "600905", "汇联医疗", "buy", 800, "filled",
                              "趋势回调建仓", _ts(2, "10:30:00"), planned=60.00, filled=60.00)
        _fill(conn, stop_buy_oid, acc_flow, "600905", "buy", 800, 60.00, _ts(2, "10:30:00"))
        _position(conn, cycle_id, acc_flow, "600905", "汇联医疗", "医疗器械", 800, 60.00,
                  _iso_days_ago(2), _iso_days_ago(1))
        _decision(conn, acc_flow, "600905", "buy", "entry_approved", "汇联医疗 60.00 元建仓", "汇联医疗")
        _review(conn, cycle_id, acc_flow, "600905", _iso_days_ago(0), 2.0, "D", "sell", 45120.0, 4.5,
                ["价格跌破硬止损线"], {"review_phase": "position_quality", "hard_stop": -0.06,
                                        "model_score": 1.0, "trend_score": 0.0}, )
        stop_oid = _order(conn, acc_flow, "600905", "汇联医疗", "sell", 800, "filled",
                          "硬止损 -6%：现价 56.40 低于风险价 56.40，触发卖出", _ts(0, "09:37:00"),
                          planned=56.40, filled=56.40)
        _fill(conn, stop_oid, acc_flow, "600905", "sell", 800, 56.40, _ts(0, "09:37:00"))
        _drop_position(conn, acc_flow, "600905")
        _decision(conn, acc_flow, "600905", "sell", "hard_stop_sell",
                  "汇联医疗 触及硬止损，56.40 元清仓离场（已实现亏损 -2880 元）", "汇联医疗")

        # --- 5) limit-up buy on the +9.98% ticker ---
        up_oid = _order(conn, acc_live, "600906", "极光智能", "buy", 1200, "filled",
                        "强势放量突破：涨停价附近承接成交", _ts(0, "13:05:00"),
                        planned=27.50, filled=27.50)
        _fill(conn, up_oid, acc_live, "600906", "buy", 1200, 27.50, _ts(0, "13:05:00"))
        _position(conn, cycle_id, acc_live, "600906", "极光智能", "算力服务", 1200, 27.50,
                  _iso_days_ago(0), _iso_days_ago(1))
        _decision(conn, acc_live, "600906", "buy", "entry_approved",
                  "极光智能 +9.98% 涨停候选通过门禁，27.50 元成交", "极光智能")

        # --- open-position quality review for portfolio cards ---
        _review(conn, cycle_id, acc_hold, "600901", _iso_days_ago(0), 7.5, "A", "hold", 37000.0, 3.7,
                ["动量与资金面共振，继续持有"], {"review_phase": "position_quality", "model_score": 8.0,
                                                "trend_score": 7.0, "flow_score": 7.5})

        # --- 5-day NAV curve for every active account ---
        # per-day % moves for accounts[0..4] across D-4..D0
        day_counts = [
            [+0.1, +0.0, -0.2, +0.3, +0.1],   # account 0
            [+0.2, +0.1, +0.4, +0.6, -0.4],   # account 1 (stop loss day)
            [+0.0, +0.1, +0.2, +0.3, +0.8],   # account 2 (limit-up)
            [+0.1, +0.1, +0.1, +0.1, +0.1],
            [+0.1, +0.2, +0.0, -0.1, +0.2],
        ]
        _nav_days(conn, accounts, share, day_counts)

        # sync account cash with the handwritten fills so shared-pool reads
        # reconcile (seed fills never went through the engine's cash ledger)
        for account_id in accounts:
            flow = conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN side='sell' THEN amount-fees
                                          WHEN side='buy' THEN -(amount+fees)
                                          ELSE 0 END),0)
                   FROM paper_fills WHERE account_id=?""",
                (account_id,),
            ).fetchone()[0]
            PT._rows(conn, "UPDATE paper_accounts SET cash=?,updated_at=? WHERE id=?",
                     (round(share + float(flow), 2), _now(), account_id))

        PT._audit(conn, None, "demo_seeded",
                  "确定性演示账本已播种：10 只合成股票，事件链含正常成交/T+1 拒绝/行情过期拒绝/止损/涨停")
        PT._audit(conn, None, "capital_configured", f"演示资金池 {DEMO_CAPITAL:.0f} 元，五策略等分 {share:.0f} 元")
    return True


if __name__ == "__main__":
    seeded = ensure_demo_data(force=os.environ.get("ASTOCK_DEMO_FORCE") == "1")
    print("demo seeded:" if seeded else "demo already seeded:", seeded)
