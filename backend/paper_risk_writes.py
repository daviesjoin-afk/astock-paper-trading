# -*- coding: utf-8 -*-
"""Risk Authority 的写入原语：卖出委托与风险决策。

这些 helper 只做 SQL 写入，不依赖 ``paper_trading`` / ``paper_risk_service``：
调用方负责准备 ``strategy_stamp``、``now`` 与 decision provenance。
抽出来是为了让 ``paper_risk_service.run`` 的调用点不再各写一份重复 SQL。
"""
from __future__ import annotations

import json
from typing import Any


_INSERT_SELL_ORDER_SQL = (
    "INSERT INTO paper_orders("
    "account_id,side,code,name,qty,planned_price,status,reason,"
    "risk_payload,created_at,strategy_id,strategy_version,strategy_checksum,cycle_id"
    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def insert_sell_order(conn, position, *, qty, price, status, reason, detail,
                      strategy_stamp, cycle_id, now):
    """Risk Authority 的卖出委托写入，唯一入口。

    ``strategy_stamp`` 是 ``(strategy_id, strategy_version, strategy_checksum)``；
    ``now`` 是调用方给出的写入时间戳（不读时钟）。
    """
    return conn.execute(
        _INSERT_SELL_ORDER_SQL,
        (position["account_id"], "sell", position["code"], position.get("name"),
         qty, price or None, status, reason, _json(detail), now,
         *strategy_stamp, cycle_id),
    )


def insert_risk_decision(conn, account_id, code, side, decision, reason, payload, *,
                         strategy_stamp, cycle_id, now, order_id=None):
    """Risk Authority 的风险决策写入，带写入时刻的策略版本戳。

    ``order_id`` 非空时，调用方必须已经校验过该 order 归属。
    """
    strategy_id, strategy_version, strategy_checksum = strategy_stamp
    conn.execute(
        """INSERT INTO paper_risk_decisions(
               account_id,code,side,decision,reason,payload,created_at,
               strategy_id,strategy_version,strategy_checksum,order_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, code, side, decision, reason, _json(payload), now,
         strategy_id, strategy_version, strategy_checksum,
         int(order_id) if order_id is not None else None),
    )
