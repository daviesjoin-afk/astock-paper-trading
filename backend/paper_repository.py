# -*- coding: utf-8 -*-
"""Paper ledger 的最小仓储接口。

仓储（repository）是隔离数据库读写的薄接口。本阶段只统一通用行读取和
审计写入；具体业务 SQL 仍由上层编排，便于后续按账户、订单和成交逐步迁移。

**执行验证闸门**：本模块所有"成交绩效"投影都必须引用
:data:`execution_verification.VERIFIED_PREDICATE`（唯一一份），不得自己拼
``execution_verified=1``。谓词是 fail closed 的：缺列、NULL、两列不一致一律排除；
"探测不到列就把闸门关掉"是 fail open，等于把这份投影悄悄降级成未验证口径。
"""
from __future__ import annotations

import json

import strategy_registry as SR

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import execution_verification as EV
except ImportError:  # pragma: no cover - package-style import
    from . import execution_verification as EV


def rows(conn, sql, params=()):
    """执行查询并把 sqlite Row 转成普通字典，保持旧返回形状。"""
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def audit(conn, account_id, event, detail, created_at, *, strategy_stamp=None):
    """写入一条结构化审计事件。

    ``strategy_stamp=None`` keeps legacy/live resolution.  An explicit tuple -
    including ``(None, None, None)`` - is passed through unchanged so a
    committed order's durable provenance can be inherited by its audit row.
    """
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(paper_audit)").fetchall()
    }
    stamp_columns = {"strategy_id", "strategy_version", "strategy_checksum"}
    if not stamp_columns.issubset(columns):
        # Compatibility for focused unit-test fixtures created with the old
        # four-column audit table. The migrated application schema always
        # takes the stamped branch below.
        conn.execute(
            "INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
            (account_id, event, detail, created_at),
        )
        return
    if strategy_stamp is None:
        strategy_id, strategy_version, strategy_checksum = SR.stamp_for_account(
            conn, account_id,
        )
    else:
        strategy_id, strategy_version, strategy_checksum = strategy_stamp
    conn.execute(
        """INSERT INTO paper_audit(
               account_id,event,detail,created_at,
               strategy_id,strategy_version,strategy_checksum)
           VALUES(?,?,?,?,?,?,?)""",
        (account_id, event, detail, created_at,
         strategy_id, strategy_version, strategy_checksum),
    )


def account_metric_inputs(conn, account_ids, today):
    """批量读取 dashboard 账户卡片所需的窄账本投影。

    这里只负责 SQL 读取和按账户分组，不计算 NAV 或收益率；这样上层
    dashboard 可以复用同一份卖出、成交计数和 NAV 历史，避免逐账户重复扫表。
    """
    ids = [str(account_id) for account_id in account_ids if account_id]
    empty = {
        "latest_nav": {}, "navs": {}, "previous_nav": {}, "sells": {},
        "buy_count": {}, "rejected": {},
    }
    if not ids:
        return empty
    placeholders = ",".join("?" for _ in ids)
    order_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
    realized_field = "realized_pnl" if "realized_pnl" in order_columns else "NULL AS realized_pnl"
    executed_field = "executed_at" if "executed_at" in order_columns else "NULL AS executed_at"
    sell_fields = (
        f"id,account_id,code,qty,filled_price,amount,fees,status,{realized_field},"
        f"created_at,{executed_field}"
    )
    # 卖出行只收**已验证**成交。谓词来自 execution_verification 的唯一实现；
    # 这里不再按"列在不在"开关闸门 —— 那会把缺列的库静默降级成未验证口径。
    sell_rows = rows(
        conn,
        f"SELECT {sell_fields} FROM paper_orders "
        f"WHERE account_id IN ({placeholders}) AND side='sell' AND status='filled' "
        f"AND {EV.VERIFIED_PREDICATE}",
        tuple(ids),
    )
    sells = {account_id: [] for account_id in ids}
    for row in sell_rows:
        sells.setdefault(str(row.get("account_id") or ""), []).append(row)
    buy_rows = rows(
        conn,
        f"SELECT account_id,COUNT(*) AS count FROM paper_fills "
        f"WHERE account_id IN ({placeholders}) AND side='buy' GROUP BY account_id",
        tuple(ids),
    )
    buy_count = {str(row["account_id"]): int(row["count"] or 0) for row in buy_rows}
    rejected_rows = rows(
        conn,
        f"SELECT account_id,COUNT(*) AS count FROM paper_orders "
        f"WHERE account_id IN ({placeholders}) AND status!='filled' GROUP BY account_id",
        tuple(ids),
    )
    rejected = {str(row["account_id"]): int(row["count"] or 0) for row in rejected_rows}
    nav_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_nav)").fetchall()}
    quote_status_field = ",quote_status" if "quote_status" in nav_columns else ""
    nav_rows = rows(
        conn,
        f"SELECT account_id,nav_date,nav,benchmark{quote_status_field},created_at "
        f"FROM paper_nav WHERE account_id IN ({placeholders}) ORDER BY account_id,nav_date",
        tuple(ids),
    )
    navs = {account_id: [] for account_id in ids}
    latest_nav = {}
    previous_nav = {}
    today_key = str(today)[:10]
    for row in nav_rows:
        account_id = str(row.get("account_id") or "")
        navs.setdefault(account_id, []).append(row.get("nav"))
        latest_nav[account_id] = row
        if str(row.get("nav_date") or "") < today_key:
            previous_nav[account_id] = row
    return {
        "latest_nav": latest_nav, "navs": navs, "previous_nav": previous_nav,
        "sells": sells, "buy_count": buy_count, "rejected": rejected,
    }


def recent_live_orders(conn, account_names, limit):
    """读取活动委托及执行事实；只抽取 risk_payload 中少量展示字段。"""
    order_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_orders)")}
    fill_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_fills)")}
    fields = (
        "id,account_id,signal_id,side,code,name,qty,planned_price,filled_price,"
        "amount,fees,status,reason,realized_pnl,created_at,executed_at,"
        "order_type,origin,expires_at,cancelled_at"
    )
    if "execution_status" in order_columns:
        fields += ",execution_status,execution_verified,execution_evidence_source"
    if {"order_id", "qty"}.issubset(fill_columns):
        fields += ",COALESCE((SELECT SUM(f.qty) FROM paper_fills f WHERE f.order_id=paper_orders.id),0) AS filled_qty"
        for name in ("execution_asof", "pricing_basis", "slippage_amount"):
            if name in fill_columns:
                fields += f",(SELECT MAX(f.{name}) FROM paper_fills f WHERE f.order_id=paper_orders.id) AS {name}"
        if "execution_event_key" in fill_columns:
            fields += ",0 AS _fill_projection_available"
    else:
        fields += ",0 AS filled_qty"
    if "risk_payload" in order_columns:
        # CASE prevents one malformed legacy payload from breaking the entire
        # activity read. The full multi-megabyte payload never leaves SQLite.
        fields += (",CASE WHEN json_valid(risk_payload) THEN json_extract(risk_payload,'$.execution') END"
                   " AS execution_json")
    if "cycle_id" in order_columns:
        fields += ",cycle_id"
    orders = rows(
        conn,
        f"SELECT {fields} FROM paper_orders ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),),
    )
    order_ids = [int(order["id"]) for order in orders if order.get("id") is not None]
    fills_by_order: dict[int, list[dict]] = {}
    event_fields = {
        "id", "order_id", "qty", "price", "amount", "fees", "fill_date", "quote_at",
        "execution_event_key", "execution_asof", "pricing_basis", "slippage_amount",
        "execution_evidence",
    }
    selected_event_fields = [name for name in event_fields if name in fill_columns]
    if order_ids and {"order_id", "qty", "price", "amount", "fees"}.issubset(fill_columns):
        placeholders = ",".join("?" for _ in order_ids)
        fill_rows = rows(
            conn,
            f"SELECT {','.join(selected_event_fields)} FROM paper_fills "
            f"WHERE order_id IN ({placeholders}) ORDER BY id DESC",
            tuple(order_ids),
        )
        for item in fill_rows:
            try:
                order_id = int(item.get("order_id"))
            except (TypeError, ValueError):
                continue
            event = dict(item)
            evidence = event.pop("execution_evidence", None)
            try:
                event["market_evidence"] = (json.loads(evidence) or {}).get("market_evidence") if evidence else None
            except (TypeError, ValueError, AttributeError):
                event["market_evidence"] = None
            fills_by_order.setdefault(order_id, []).append(event)
    for order in orders:
        account_id = order.get("account_id")
        order["account_name"] = account_names.get(account_id, account_id)
        order["archived_cycle"] = None
        desired = max(0, int(order.get("qty") or 0))
        filled = max(0, int(order.get("filled_qty") or 0))
        order["desired_qty"] = desired
        order["filled_qty"] = filled
        order["remaining_qty"] = max(0, desired - filled)
        order["fill_events"] = fills_by_order.get(int(order["id"]), [])
        for event in order["fill_events"]:
            event["account_id"] = account_id
            event["account_name"] = order["account_name"]
            event["side"] = order.get("side")
            event["code"] = order.get("code")
            event["name"] = order.get("name")
        execution = order.pop("execution_json", None)
        if isinstance(execution, str):
            try:
                execution = json.loads(execution)
            except (TypeError, ValueError):
                execution = None
        execution = execution if isinstance(execution, dict) else {}
        evidence = execution.get("market_evidence")
        if not isinstance(evidence, dict) and order["fill_events"]:
            evidence = order["fill_events"][0].get("market_evidence")
        order["execution_asof"] = execution.get("as_of") or order.get("execution_asof")
        order["market_data_state"] = execution.get("market_state")
        order["market_data_validation"] = (evidence or {}).get("quote_validation")
        order["market_data_asof"] = (evidence or {}).get("quote_at")
        order["market_evidence"] = evidence
        order["execution_reason_codes"] = execution.get("reason_codes") or []
        order["blocking_reason"] = order.get("reason") if order.get("remaining_qty") else None
        order["slippage_amount"] = sum(float(item.get("slippage_amount") or 0) for item in order["fill_events"])
        order["allowed_actions"] = []
        if order.get("origin") == "manual" and not order.get("archived_cycle"):
            try:
                import execution_lifecycle as EL
                if EL.can_transition(EL.canonical_state(order.get("status")), EL.STATE_CANCELLED):
                    order["allowed_actions"] = ["cancel"]
            except (ImportError, ValueError):
                pass
    return orders
