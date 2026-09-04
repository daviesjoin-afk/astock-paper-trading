# -*- coding: utf-8 -*-
"""模拟盘持仓读模型的纯聚合逻辑。

本模块不连接数据库，也不读取行情。调用方负责准备 lots、旧聚合持仓和
成交现金流，再注入项目现有的数值转换函数，以保持历史数据兼容。
"""
from __future__ import annotations


def aggregate_positions(lots, legacy_rows, cash_flows, day, *, num):
    """将可用持仓 lot 聚合成兼容旧接口的持仓字典列表。"""
    grouped = {}
    for lot in lots:
        key = (lot["account_id"], lot["code"])
        item = grouped.setdefault(key, {
            "account_id": lot["account_id"], "code": lot["code"], "name": lot.get("name"),
            "industry": lot.get("industry") or "未知", "qty": 0, "cost_amount": 0.0,
            "entry_date": lot["acquired_at"][:10], "available_qty": 0, "locked_qty": 0,
            "asset_type": lot.get("asset_type") or "stock_t1", "available_date": lot["available_date"],
        })
        qty = int(lot["remaining_qty"])
        item["qty"] += qty
        item["cost_amount"] += qty * num(lot["cost"])
        if str(lot.get("acquired_at") or "")[:10] == day:
            item["today_acquired_qty"] = int(item.get("today_acquired_qty") or 0) + qty
            item["today_acquired_cost"] = num(item.get("today_acquired_cost")) + qty * num(lot["cost"])
        item["entry_date"] = min(item["entry_date"], lot["acquired_at"][:10])
        item["available_date"] = min(item["available_date"], lot["available_date"])
        if lot["available_date"] <= day:
            item["available_qty"] += qty
        else:
            item["locked_qty"] += qty

    legacy = {(p["account_id"], p["code"]): p for p in legacy_rows}
    out = []
    for key, item in grouped.items():
        item["cost"] = item.pop("cost_amount") / max(item["qty"], 1)
        item["settlement_cost"] = item["cost"]
        flow = cash_flows.get(key) or {}
        net_invested = num(flow.get("buy_cash")) - num(flow.get("sell_cash"))
        item["display_cost"] = net_invested / max(item["qty"], 1)
        old = legacy.get(key, {})
        item["peak_price"] = num(old.get("peak_price"), item["cost"])
        item["take_stage"] = int(num(old.get("take_stage"), 0))
        out.append(item)
    return out
