# -*- coding: utf-8 -*-
"""Risk Authority 的写入原语：卖出委托与风险决策。

这些 helper 只做 SQL 写入，不依赖 ``paper_trading`` / ``paper_risk_service``：
调用方负责准备决策快照、``strategy_stamp`` 与 ``now``；本模块统一校验并组装
decision provenance，不从 payload 猜测决策权限。
抽出来是为了让 ``paper_risk_service.run`` 的调用点不再各写一份重复 SQL。
"""
from __future__ import annotations

import json
from typing import Any

import paper_order_ledger as POL
import paper_schema_migrations as PSM


def _json(value: Any, *, sort_keys=True) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=sort_keys)


def insert_sell_order(conn, position, *, qty, price, status, reason, detail,
                      strategy_stamp, cycle_id, now):
    """Risk Authority 的卖出委托写入，唯一入口（返回新订单 id）。

    ``strategy_stamp`` 是 ``(strategy_id, strategy_version, strategy_checksum)``；
    ``now`` 是调用方给出的写入时间戳（不读时钟）。
    """
    return POL.insert_order(conn, fields={
        "account_id": position["account_id"], "side": "sell", "code": position["code"],
        "name": position.get("name"), "qty": qty, "planned_price": price or None,
        "status": status, "reason": reason, "risk_payload": _json(detail),
        "created_at": now, "strategy_id": strategy_stamp[0],
        "strategy_version": strategy_stamp[1], "strategy_checksum": strategy_stamp[2],
        "cycle_id": cycle_id,
    })


def require_decision_authority(*, order_id, authority):
    """校验权限声明；调用方在快照和策略查询前即可拒绝非法输入。"""
    if order_id is not None and authority is None:
        raise ValueError("order-linked risk decision requires an explicit authority")
    if authority is not None:
        PSM.risk_decision_provenance(authority, None)


def insert_risk_decision(conn, account_id, code, side, decision, reason, payload, *,
                         strategy_stamp, cycle_id, now, order_id=None,
                         authority=None, decision_kind=None, sort_payload_keys=True):
    """风险决策的唯一写入口，统一写入来源信息且不提交事务。

    ``order_id`` 非空时，调用方必须已经校验过该 order 归属。
    ``authority`` 由业务调用方声明；词汇和结构仍由 schema owner 定义。
    ``cycle_id`` 保留原调用契约，不用于猜测或补写历史归属。
    ``sort_payload_keys`` 保留两条旧路径各自的 JSON 字段顺序。
    """
    require_decision_authority(order_id=order_id, authority=authority)
    if authority is not None:
        payload = dict(payload)
        payload["decision_provenance"] = PSM.risk_decision_provenance(
            authority, decision_kind or decision)
    strategy_id, strategy_version, strategy_checksum = strategy_stamp
    conn.execute(
        """INSERT INTO paper_risk_decisions(
               account_id,code,side,decision,reason,payload,created_at,
               strategy_id,strategy_version,strategy_checksum,order_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, code, side, decision, reason,
         _json(payload, sort_keys=sort_payload_keys), now,
         strategy_id, strategy_version, strategy_checksum,
         int(order_id) if order_id is not None else None),
    )
