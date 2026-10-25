# -*- coding: utf-8 -*-
"""配对做 T 的只读评价：底仓 slice 盈亏 ≠ 配对价差。

**为什么必须单独算**：卖腿的 ``paper_orders.realized_pnl`` 是"被 FIFO 消耗的
底仓 lot 的生命周期盈亏"（``amount - cost_amount - fees``），按定义**不含**
"卖高抛、低价买回"的价差；价差在系统里只以两条互不相连的记账存在
（``paper_performance`` 的昨收基准卖出贡献 + 回补份额的今日成本）。用卖出
相对底仓成本的利润冒充做 T 收益，会把"底仓上涨"读成"做 T 赚钱"。

**本模块只读**：不写库、不下单、不改成本、不重算历史 ``realized_pnl``，也不
替代执行层的成交与核验。它把同一组 ``t_sell`` / ``t_rebuy`` 观测拆成三块并
分别标注证据状态：

1. **底仓 slice 盈亏**：卖腿 ``amount - fees - risk_payload.cost_amount``；
2. **配对价差**：已回补股数上的 ``(卖价 - 回补均价) * 股数 - 分摊卖出费用 - 回补费用``；
3. **未回补机会成本**：当日收盘（或最后一条观测价，需标注来源）相对卖价的差额。

已知偏差（必须在报告里显式出现，不得静默修正）：

- 冻结旁路会写一条 ``t_rebuy`` 观测但**成交 0 股**；把"存在 t_rebuy 观测"当成
  已回补会把 0 股回补算成闭合，本模块按**实际成交买单**判定回补。
- 回补可能只买回一部分（``qty = min(sizing_qty, sold_qty)``），未回补余量不落库。
- 费用口径偏乐观（``MIN_COMMISSION = 0.0``、滑点 0.1%），小额 pair 的真实成本更高；
  这里只如实使用已记录费用，不假装更精确。
- 跨 cycle 的隔日回补不会配对（``consume_available_lots`` fail-closed）。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sqlite3
from pathlib import Path

import data_fetcher as dfc
import execution_verification as EV

VERSION = "intraday-t-pair-v1"
ACCOUNT_ID = "tq_breakout"
MARKET_TZ = dt.timezone(dt.timedelta(hours=8))
STATUS_OK = "verified"

BOUNDARY = (
    "仅复核当前周期已核验成交的配对做 T：按卖单实际成交股数配对回补买单，"
    "底仓 slice 盈亏与配对价差分开报告，未回补部分另行计价。"
    "回补失败不写观测、冻结旁路会写 0 股 t_rebuy 观测，因此\"存在 t_rebuy 观测\""
    "不等于已回补。这里不证明做 T 有效，也不据此调权重或放宽风控。"
)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _time(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=MARKET_TZ) if result.tzinfo is None else result


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _filled_qty(order):
    """实际成交股数：优先 filled_qty，退回 qty。

    成交与否只认 ``execution_verification`` 的唯一谓词（``is_verified_row``），
    本模块不另写一份"verified"判断。
    """
    if str(order.get("status") or "") != "filled":
        return None
    if not EV.is_verified_row(order):
        return None
    qty = _number(order.get("filled_qty"))
    if qty is None:
        qty = _number(order.get("qty"))
    if qty is None or qty <= 0:
        return None
    return qty


def _order_fees(order):
    return _number(order.get("fees")) or 0.0


def _avg_price(order, qty):
    price = _number(order.get("filled_price"))
    amount = _number(order.get("amount"))
    if price is not None and price > 0:
        return price
    if amount is not None and qty and qty > 0:
        return amount / qty
    return None


def _sell_slice_pnl(sell, qty):
    """底仓 slice 盈亏：只用卖腿自己的成本证据，缺失就明确不可用。"""
    amount = _number(sell.get("amount"))
    price = _avg_price(sell, qty)
    if amount is None and price is not None:
        amount = price * qty
    cost_amount = _number(_object(sell.get("risk_payload")).get("cost_amount"))
    if amount is None or cost_amount is None:
        return None
    return amount - _order_fees(sell) - cost_amount


def _pair_spread(sell, qty, sell_price, rebuys):
    """已回补股数上的价差：卖出费用按回补股数占比分摊，卖出金额按比例折算。"""
    paired_qty = 0.0
    cost = 0.0
    fees = 0.0
    for order in rebuys:
        rebuy_qty = _filled_qty(order)
        price = _avg_price(order, rebuy_qty or 0.0)
        if rebuy_qty is None or price is None:
            continue
        paired_qty += rebuy_qty
        cost += price * rebuy_qty
        fees += _order_fees(order)
    if paired_qty <= 0 or sell_price is None:
        return None
    locked = min(paired_qty, qty)
    rebuy_avg = cost / paired_qty
    sell_fee_share = _order_fees(sell) * (locked / qty) if qty else 0.0
    spread = (sell_price - rebuy_avg) * locked - sell_fee_share - fees
    return {"paired_qty": locked, "rebuy_avg_price": rebuy_avg,
            "rebuy_fees": fees, "spread_pnl": spread}


def summarize(sell_orders, observations, rebuy_orders, closes=None):
    """纯计算：给定订单/观测/收盘价行，返回三块口径的配对评价。

    所有入参都是普通 dict 列表；调用方负责按 cycle 与 as-of 过滤。任何缺失证据
    都落到 ``unavailable``，不用 0 或早期数据填补。
    """
    closes = closes or {}
    orders_by_id = {int(row["id"]): dict(row) for row in sell_orders
                    if _number(row.get("id")) is not None}
    rebuys_by_obs = {}
    for row in rebuy_orders or ():
        order = dict(row)
        pointer = _object(order.get("risk_payload")).get("paired_sell_observation")
        try:
            pointer = int(pointer)
        except (TypeError, ValueError):
            continue
        rebuys_by_obs.setdefault(pointer, []).append(order)
    deferred_observations = 0
    pairs = []
    seen = set()
    for raw in observations or ():
        observation = dict(raw)
        if str(observation.get("action") or "") != "t_sell":
            if (str(observation.get("action") or "") == "t_rebuy"
                    and _number(_object(observation.get("payload")).get("qty")) in (0, None)):
                deferred_observations += 1
            continue
        obs_id = int(observation["id"])
        if obs_id in seen:
            continue
        seen.add(obs_id)
        payload = _object(observation.get("payload"))
        sell_id = payload.get("order_id")
        try:
            sell = orders_by_id.get(int(sell_id))
        except (TypeError, ValueError):
            sell = None
        qty = _filled_qty(sell or {})
        if sell is None or qty is None:
            pairs.append({
                "sell_observation_id": obs_id, "sell_order_id": (sell or {}).get("id"),
                "code": observation.get("code"), "sold_qty": None, "sell_price": None,
                "rebought_qty": 0.0, "unrebought_qty": 0.0, "base_slice_pnl": None,
                "spread_pnl": None, "rebuy_avg_price": None,
                "unrebought_opportunity_cost": None, "close_price_source": None,
                "status": "unverified_sell",
            })
            continue
        sell_price = _avg_price(sell, qty)
        rebuys = [order for order in rebuys_by_obs.get(obs_id, ())
                  if _filled_qty(order) is not None]
        spread = _pair_spread(sell, qty, sell_price, rebuys)
        rebought_qty = spread["paired_qty"] if spread else 0.0
        unrebought_qty = max(0.0, qty - rebought_qty)
        code = str(observation.get("code") or sell.get("code") or "")
        close = closes.get(code)
        close_price = _number((close or {}).get("price"))
        opportunity = None
        if unrebought_qty > 0 and sell_price is not None and close_price is not None:
            opportunity = (close_price - sell_price) * unrebought_qty
        pairs.append({
            "sell_observation_id": obs_id,
            "sell_order_id": sell.get("id"),
            "code": code,
            "sold_qty": qty,
            "sell_price": sell_price,
            "rebought_qty": rebought_qty,
            "unrebought_qty": unrebought_qty,
            "base_slice_pnl": _sell_slice_pnl(sell, qty),
            "spread_pnl": spread["spread_pnl"] if spread else None,
            "rebuy_avg_price": spread["rebuy_avg_price"] if spread else None,
            "unrebought_opportunity_cost": opportunity,
            "close_price_source": (close or {}).get("source") if close else None,
            "status": (
                "closed" if unrebought_qty <= 0 else
                "partial" if rebought_qty > 0 else "open"),
        })
    closed = [row for row in pairs if row["status"] == "closed"]
    partial = [row for row in pairs if row["status"] == "partial"]
    open_rows = [row for row in pairs if row["status"] == "open"]
    return {
        "version": VERSION,
        "status": "case_review_only",
        "boundary": BOUNDARY,
        "pairs": len(pairs),
        "closed_pairs": len(closed),
        "partial_pairs": len(partial),
        "open_pairs": len(open_rows),
        "deferred_rebuy_observations": deferred_observations,
        "base_slice_pnl": _sum(row["base_slice_pnl"] for row in pairs),
        "paired_spread_pnl": _sum(row["spread_pnl"] for row in closed + partial),
        "unrebought_opportunity_cost": _sum(
            row["unrebought_opportunity_cost"] for row in pairs),
        "unrebought_qty": round(sum(row["unrebought_qty"] for row in pairs), 2),
        "rows": pairs,
        "caveats": [
            "底仓 slice 盈亏沿用卖腿自身的 cost_amount 证据，不是做 T 收益。",
            "配对价差只覆盖实际回补股数；未回补部分单列机会成本。",
            "机会成本仅在能取到收盘价（或最后观测价）时给出，缺失即 None。",
            "冻结旁路的 0 股 t_rebuy 观测不计入已回补。",
        ],
    }


def _sum(values):
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers), 2)


def _kline_close(code, trade_date):
    """只读日线缓存里该股当天的收盘价；没有当天就用之前最近一天，取不到返回 None。"""
    path = Path(dfc.kline_cache_path(str(code)))
    if not path.is_file():
        return None
    try:
        rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig", errors="replace").splitlines()))
    except OSError:
        return None
    best_date, best_close = None, None
    for row in rows:
        day = str(row.get("date") or row.get("trade_date") or "")[:10]
        if not day or day > str(trade_date):
            continue
        close = _number(row.get("close"))
        if close is None or close <= 0:
            continue
        if best_date is None or day > best_date:
            best_date, best_close = day, close
    return best_close


def _resolve_closes(observations):
    """给每个出现过收盘价的标的补齐 closes；来源写清楚，缺失就留空。"""
    closes = {}
    latest = {}
    for row in observations or ():
        code = str(row.get("code") or "")
        if not code:
            continue
        value = _number(row.get("price"))
        if value is not None and value > 0:
            latest[code] = value
    for code in latest:
        trade_date = None
        for row in observations or ():
            if str(row.get("code") or "") == code:
                observed = _time(row.get("observed_at"))
                if observed is not None:
                    trade_date = max(trade_date, observed.date()) if trade_date else observed.date()
        close = _kline_close(code, trade_date) if trade_date else None
        if close is not None:
            closes[code] = {"price": close, "source": "kline_close",
                            "trade_date": trade_date.isoformat()}
        else:
            closes[code] = {"price": latest[code], "source": "last_intraday_observation"}
    return closes


def build_report(paper_db_path, *, asof=None, closes=None):
    """只读账本读取；缺表或查询失败时返回 ``unavailable``，绝不建表或补数据。"""
    now = _time(asof) if asof is not None else dt.datetime.now(MARKET_TZ)
    unavailable = {"version": VERSION, "status": "unavailable", "boundary": BOUNDARY}
    if now is None:
        return {**unavailable, "reason": "invalid_asof"}
    cutoff = now.astimezone(MARKET_TZ).replace(tzinfo=None).isoformat(sep=" ")
    conn = None
    try:
        conn = sqlite3.connect(Path(paper_db_path).resolve().as_uri() + "?mode=ro",
                               uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        account = conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (ACCOUNT_ID,)).fetchone()
        if account is None or account["cycle_id"] is None:
            return {**unavailable, "reason": "current_account_cycle_missing"}
        cycle_id = account["cycle_id"]
        orders = [dict(row) for row in conn.execute(
            "SELECT id,account_id,code,side,qty,filled_qty,filled_price,amount,fees,"
            "status,execution_status,execution_verified,executed_at,risk_payload"
            " FROM paper_orders"
            " WHERE account_id=? AND cycle_id=? AND status='filled'"
            " AND " + EV.VERIFIED_PREDICATE +
            " AND datetime(executed_at)<=datetime(?)"
            " ORDER BY datetime(executed_at),id", (ACCOUNT_ID, cycle_id, cutoff))]
        observations = [dict(row) for row in conn.execute(
            """SELECT id,code,observed_at,price,action,payload
                 FROM paper_intraday_observations
                WHERE account_id=? AND cycle_id=? AND action IN ('t_sell','t_rebuy')
                  AND datetime(observed_at)<=datetime(?)
                ORDER BY id""", (ACCOUNT_ID, cycle_id, cutoff))]
        if closes is None:
            # 未回补机会成本原本永远算不出来：唯一的生产调用方从不传 closes。
            # 这里用只读日线缓存补收盘价，取不到才退回落最后一条观测价，并标明来源。
            closes = _resolve_closes(observations)
        result = summarize([row for row in orders if row["side"] == "sell"],
                           observations,
                           [row for row in orders if row["side"] == "buy"],
                           closes=closes)
        result.update({"cycle_id": cycle_id, "asof": now.isoformat()})
        return result
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {**unavailable, "reason": "ledger_or_schema_unavailable",
                "error_type": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()


def evidence_summary(report):
    """给 AI 研究入口的有界载荷：不带原始订单与账户明细。"""
    return {key: value for key, value in report.items() if key != "rows"}


__all__ = ["VERSION", "ACCOUNT_ID", "BOUNDARY", "summarize", "build_report",
           "evidence_summary"]
