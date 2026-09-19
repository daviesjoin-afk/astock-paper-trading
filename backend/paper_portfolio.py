# -*- coding: utf-8 -*-
"""模拟盘持仓读模型的纯聚合逻辑。

本模块不连接数据库，也不读取行情。调用方负责准备 lots、旧聚合持仓和
成交现金流，再注入项目现有的数值转换函数，以保持历史数据兼容。
"""
from __future__ import annotations


def aggregate_positions(lots, risk_state_rows, cash_flows, day, *, num):
    """将可用持仓 lot 聚合成兼容旧接口的持仓字典列表。

    Authority（R14）::

        lots            -> qty / cost / entry_date（quantity authority）
        risk_state_rows -> peak_price / take_stage（runtime risk authority）
        paper_positions -> 不再参与聚合（zero execution authority）

    ``risk_state_rows`` 是**同周期** ``paper_position_risk_state`` 行。某持仓
    没有状态行时（升级前遗留持仓 / 无 episode 事实），语义是显式 fail-safe：

    * ``peak_price`` 锚定成本 —— 与全新 episode 的默认一致；``_position_peak``
    仍会吸收当日 high，因此移动止损按"今日观测峰值"照常工作，但绝不会因为一条
    不可证明的历史峰值而比基线卖得更多；
    * ``take_stage=None`` —— "已消费到哪一档"不可证明，``_sell_plan`` 对
    ``None`` 跳过阶梯止盈（绝不猜一个档位多卖）。

    hard stop / max hold 不依赖这两项，照常工作 —— 基础保护不被关闭。
    """
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

    risk_state = {(p["account_id"], p["code"]): p for p in risk_state_rows}
    out = []
    for key, item in grouped.items():
        item["cost"] = item.pop("cost_amount") / max(item["qty"], 1)
        item["settlement_cost"] = item["cost"]
        # 现金流投影只覆盖**已验证**的成交行。升级前的持仓验证列为 NULL，因此
        # 完全没有现金流行；把"缺失"当成 0 会让 net_invested 变成 0，摊薄成本
        # 就从一个"未知"变成一个"有定义的 0"，前端优先取它，于是每个升级前的
        # 持仓都显示摊薄成本 0。缺失必须是**未知** → 回落 lot 的结算成本。
        flow = cash_flows.get(key)
        if flow is None:
            item["display_cost"] = item["cost"]
            item["display_cost_source"] = "lot_settlement_cost"
        else:
            net_invested = num(flow.get("buy_cash")) - num(flow.get("sell_cash"))
            item["display_cost"] = net_invested / max(item["qty"], 1)
            item["display_cost_source"] = "verified_cash_flow"
        row = risk_state.get(key)
        if row is None:
            # 缺失 = 未知（unknown must not be upgraded into known）。
            item["peak_price"] = item["cost"]
            item["take_stage"] = None
            item["risk_state_source"] = "missing"
        else:
            item["peak_price"] = num(row.get("peak_price"), item["cost"])
            item["take_stage"] = int(num(row.get("take_stage"), 0))
            item["risk_state_source"] = "cycle_state"
        out.append(item)
    return out
