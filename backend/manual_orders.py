# -*- coding: utf-8 -*-
"""Manual order vertical, extracted from paper_trading.py (Phase 2).

Owns the full manual-trade path: risk state -> order plan -> preview ->
execute/commit -> submit (two-phase confirm) -> cancel -> pending-order
sweep.  Function bodies are byte-for-byte moves from paper_trading.py;
each function resolves its shared-engine dependencies lazily at call time
(`from paper_trading import ...`) so importing either module first is safe.
paper_trading.py keeps thin *args/**kwargs facades under the original
names, so `import paper_trading as P; P.submit_manual_order(...)` and the
api_paper.py call sites are unchanged.
"""
from __future__ import annotations



def _manual_risk_state(conn, account, nav, asof_day):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        _account_reference_capital,
        _date,
        _num,
        _risk_profile,
    )
    profile = _risk_profile(account)
    day = _date(asof_day).isoformat()
    start_nav = _num(account.get("daily_start_nav"), nav)
    navs = [r[0] for r in conn.execute(
        "SELECT nav FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account["id"],)
    ).fetchall()]
    peak = max(navs + [nav, _account_reference_capital(account) or nav])
    daily_loss = 1 - nav / start_nav if start_nav else 0.0
    drawdown = 1 - nav / peak if peak else 0.0
    reasons = []
    cooldown = account.get("cooldown_until")
    if cooldown and str(cooldown) >= day:
        reasons.append(f"冷静期至 {cooldown}")
    if daily_loss >= profile["daily_loss"]:
        reasons.append(f"单日亏损 {daily_loss*100:.2f}% 已触发熔断")
    if drawdown >= profile["drawdown"]:
        reasons.append(f"滚动回撤 {drawdown*100:.2f}% 已触发熔断")
    return {
        "blocked": bool(reasons), "reasons": reasons,
        "daily_loss_pct": round(daily_loss * 100, 2),
        "drawdown_pct": round(drawdown * 100, 2),
        "cooldown_until": cooldown,
    }


def _manual_order_plan(
    conn, account_id, code, side, qty=0, order_type="market",
    limit_price=None, asof_day=None, quote=None, exclude_reservation_key=None,
    all_quotes=None, live_universe=None, market_context=None,
):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        ACCOUNT_SPECS,
        DE,
        ENTRY_FREEZE_ENV,
        ENTRY_FROZEN_WAITLIST_STATUS,
        LOT_SIZE,
        RSET,
        SHARED_POOL_MAX_EXPOSURE,
        SLIPPAGE,
        _spec_for,
        STAMP_SELL,
        _asset_type,
        _commission,
        _completed_kline,
        _date,
        _dynamic_position_limits,
        _entry_freeze_enabled,
        _entry_frozen_reason,
        _existing_position_addition_gate,
        _hold_days,
        _limit_pct,
        _market_state,
        _num,
        _pending_position_slots,
        _position_rows,
        _price_aware_qty,
        _quotes,
        _risk_profile,
        _shared_account_exposure,
        _shared_cash,
        _shared_risk_state,
        _strategy_entry_assessment,
        _strategy_pool_budget,
        _universe_snapshot_time,
        _with_decision_snapshot,
        dfc,
    )
    import execution_planner as EP
    day = _date(asof_day)
    code = str(code or "").strip()
    side = str(side or "").lower()
    order_type = str(order_type or "market").lower()
    requested_qty = int(_num(qty))
    reasons = []
    entry_frozen = side == "buy" and _entry_freeze_enabled()
    if entry_frozen:
        reasons.append(_entry_frozen_reason("手动委托"))
    if side not in {"buy", "sell"}:
        reasons.append("方向必须为买入或卖出")
    if order_type not in {"market", "limit"}:
        reasons.append("委托类型必须为市价或限价")
    if len(code) != 6 or not code.isdigit():
        reasons.append("请输入六位证券代码")
    account_row = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
    account = dict(account_row) if account_row else None
    if not account:
        reasons.append("未找到策略账户")
    elif account["status"] != "running":
        reasons.append("策略账户未运行，请先启动或恢复当前周期")

    if quote is None:
        # Callers that already prefetched market evidence pass ``quote``.
        # When a plan is evaluated inside a write transaction, use only the
        # persisted local snapshot; network I/O here can otherwise hold the
        # ledger lock while a provider retries.
        local_quote = dict(
            ({} if conn.in_transaction else _quotes([code], asof_date=day)).get(code)
            or {}
        )
    else:
        local_quote = dict(quote)
    snapshot_at = local_quote.get("quote_at") or _universe_snapshot_time()
    local_quote["quote_at"] = snapshot_at
    local_quote["quote_source"] = local_quote.get("quote_source") or "local_cache"
    price = _num(local_quote.get("price"), 0)
    if price <= 0:
        reasons.append("缺少有效行情快照")
    if not snapshot_at:
        reasons.append("缺少行情快照时间")
    elif str(snapshot_at)[:10] != day.isoformat():
        reasons.append(f"行情快照停留在 {str(snapshot_at)[:10]}，禁止模拟成交")
    if order_type == "limit" and _num(limit_price) <= 0:
        reasons.append("限价委托必须填写有效价格")

    plan = {
        "account_id": account_id, "account_name": account.get("name") if account else None,
        "code": code, "name": local_quote.get("name") or code,
        "industry": local_quote.get("industry") or "未知", "side": side,
        "order_type": order_type, "limit_price": _num(limit_price) if limit_price is not None else None,
        "quote": local_quote, "quote_price": price, "quote_at": snapshot_at,
        "requested_qty": requested_qty, "recommended_qty": 0, "qty": requested_qty,
        "allowed": False, "triggered": order_type == "market",
        "risk": {"entry_freeze": {
            "enabled": True, "env": ENTRY_FREEZE_ENV,
            "status": ENTRY_FROZEN_WAITLIST_STATUS,
        }} if entry_frozen else {},
        "reasons": reasons, "entry_frozen": entry_frozen,
    }
    if not account or price <= 0 or side not in {"buy", "sell"}:
        if entry_frozen:
            plan["status"] = ENTRY_FROZEN_WAITLIST_STATUS
            plan["waitlisted"] = True
        return plan

    strategy_positions = _position_rows(conn, account_id, day)
    position = next((p for p in strategy_positions if p["code"] == code), None)
    positions = _position_rows(conn, asof_day=day)
    open_codes = {
        item["code"] for item in positions
        if item.get("account_id") == account_id and int(_num(item.get("qty"))) >= LOT_SIZE
    }
    pending_slots = _pending_position_slots(
        conn, positions, exclude_order_key=exclude_reservation_key,
    )
    committed_open_codes = open_codes | {
        pending_code for pending_account, pending_code in pending_slots
        if pending_account == account_id
    }
    count_budget = _dynamic_position_limits(conn)
    position_limit = max(
        1,
        # PR-39：内置账户保持原口径；用户策略账户走 _spec_for 派生，
        # 不再静默退回硬编码的 5。
        int(count_budget["limits"].get(
            account_id, (ACCOUNT_SPECS.get(account_id) or _spec_for(account_id, conn=conn)).get("max_positions", 5)
        )),
    )
    pool_open_positions = {
        (str(item.get("account_id")), str(item.get("code"))) for item in positions
        if int(_num(item.get("qty"))) >= LOT_SIZE
    } | pending_slots
    all_codes = sorted({p["code"] for p in positions} | {code})
    # 仓位总额也必须使用同一轮实时快照；仅读历史 universe 会把已持仓
    # 按成本或前收估值，造成共享池净值与风控单日亏损被夸大。
    if all_quotes is None:
        all_quotes = {} if conn.in_transaction else _quotes(all_codes, asof_date=day)
    else:
        all_quotes = dict(all_quotes)
    _, position_value, nav, industries, code_values = _shared_account_exposure(conn, all_quotes, day)
    shared_cash = _shared_cash(conn)
    strategy_budget = _strategy_pool_budget(
        conn, account, nav, positions, all_quotes,
        exclude_reservation_key=exclude_reservation_key,
    )
    risk_state = _shared_risk_state(conn, account, nav, day)
    plan["risk"]["account"] = risk_state
    plan["risk"]["strategy_budget"] = strategy_budget
    plan["nav"] = round(nav, 2)
    plan["cash"] = round(shared_cash, 2)
    plan["position"] = position
    asset_type = _asset_type(code, local_quote.get("name"))
    plan["asset_type"] = asset_type

    if side == "buy":
        security_gate = EP.security_gate(code, local_quote.get("name"), local_quote.get("risk_flag"))
        plan["risk"]["security_scope"] = security_gate["scope"]
        if not security_gate["allowed"]:
            reasons.append(security_gate["reason"])
        # 席位与共享池容量门禁（含主力最后一席预留）由中央执行计划器统一判定：
        # 手动委托与自动策略共用同一口径，执行路径不再按账户身份分支。
        capacity = EP.capacity_gate(
            code=code, account_id=account_id, open_codes=open_codes,
            committed_open_codes=committed_open_codes,
            pool_open_positions=pool_open_positions,
            position_limit=position_limit, pool_limit=count_budget["pool_limit"],
            asof_day=day, conn=conn,
            allocation_source=count_budget.get("source"),
            allocation_version=count_budget.get("allocation_version"),
        )
        plan["risk"]["position_count_gate"] = capacity["gate"]
        plan["risk"]["seat_reserve"] = capacity["reserve"]
        reasons.extend(capacity["reasons"])
        if code in open_codes:
            addition_allowed, addition_reason = _existing_position_addition_gate(
                conn, account, code, day,
            )
            plan["risk"]["existing_addition_gate"] = {
                "allowed": addition_allowed, "reason": addition_reason,
            }
            if not addition_allowed:
                reasons.append(addition_reason)
        # 候选、回补和加仓必须共享本轮实时市场快照，不能在同一轮又退回
        # 上一交易日的收盘门控。
        if live_universe is None:
            # A transaction-held plan must not fetch a full-market snapshot.
            # Missing live evidence is intentionally fail-closed by the
            # market gate below.
            if conn.in_transaction:
                live_universe = []
            else:
                try:
                    live_universe = dfc.fetch_market_snapshot_full(max_age=240) or []
                except Exception:
                    live_universe = []
        market = dict(market_context or _market_state(
            day, live_universe=live_universe,
            allow_network=not conn.in_transaction,
        ))
        plan["risk"]["market"] = market
        strategy_budget = _strategy_pool_budget(
            conn, account, nav, positions, all_quotes, market=market,
            exclude_reservation_key=exclude_reservation_key,
        )
        plan["risk"]["strategy_budget"] = strategy_budget
        # 市场灯门禁沿用既有判定（红灯/未知禁止新开仓），仅由 planner 统一编排。
        if EP.market_gate(market, account_id)["blocked"]:
            reasons.append("市场门控为红灯或未知，禁止新开仓")
        reasons.extend(EP.account_risk_gate(risk_state))
        if account.get("mode") == "intraday_t" and asset_type != "stock_t1":
            reasons.append("短线日内做T账户只接受普通股票")
        if local_quote.get("risk_flag") or "ST" in str(local_quote.get("name") or "").upper():
            reasons.append("ST/退市风险标的禁止开仓")
        kline = _completed_kline(code, day, inclusive=False)
        decision = DE.buy_decision(
            code, name=local_quote.get("name"), kline=kline, snap=local_quote,
            sector_flow=[], overseas_gate=market.get("overseas") or {"light": "unknown"},
            news_hits=[],
        )
        plan["risk"]["model"] = decision
        if decision.get("tier") not in ("T1", "T2"):
            reasons.append(f"买入模型为 {decision.get('tier')}，未通过开仓门禁")
        # 是否必须走策略专属入场复核由执行策略声明，而不是比较账户 ID：
        # 手动委托同样不能绕过该策略的模型门禁。
        if EP.policy_for(account_id).manual_entry_review:
            manual_pick = dict(local_quote)
            manual_pick.update({"code": code, "name": local_quote.get("name") or code})
            manual_entry = _strategy_entry_assessment(
                account, manual_pick, local_quote, kline, decision, market=market,
            )
            plan["risk"]["entry_model"] = manual_entry
            if not manual_entry.get("passed"):
                reasons.extend(manual_entry.get("reasons") or ["未通过三日策略专属入场复核"])
        profile = _risk_profile(account)
        fill_reference = _num(limit_price) if order_type == "limit" else price
        fill_reference = max(fill_reference, 0.01)
        code_value = code_values.get(code, 0.0)
        industry_value = industries.get(plan["industry"], 0.0)
        safe_qty, sizing = _price_aware_qty(
            nav, shared_cash, position_value, industry_value, code_value,
            # PR-39：用户策略账户不在 ACCOUNT_SPECS 里，直接下标会 KeyError；
            # 统一走 paper_trading._spec_for（内置 → 表，用户 → RuntimeContext）。
            fill_reference * (1 + SLIPPAGE), _spec_for(account_id, conn=conn)["hard_stop"], profile,
            exposure_cap=RSET.get(conn, "shared_pool_exposure_cap", SHARED_POOL_MAX_EXPOSURE),
            max_exposure_cap=RSET.get(conn, "shared_pool_exposure_cap", SHARED_POOL_MAX_EXPOSURE),
            strategy_position_value=strategy_budget["current_amount"],
            strategy_cap_amount=strategy_budget["absolute_cap_amount"],
            pool_cap_amount=strategy_budget["pool_cap_amount"],
            pending_strategy_amount=strategy_budget.get("pending_reserve_amount", 0.0),
            pending_pool_amount=strategy_budget.get("pending_pool_reserve_amount", 0.0),
            single_position_max_amount=RSET.get(conn, "single_position_max_amount", 0.0),
        )
        plan["risk"]["sizing"] = sizing
        plan["recommended_qty"] = safe_qty
        if requested_qty <= 0:
            requested_qty = safe_qty
        if requested_qty > safe_qty:
            reasons.append(f"委托数量超过模型上限 {safe_qty} 股")
        plan["qty"] = requested_qty
    else:
        decision = DE.sell_decision(
            {
                "code": code, "name": local_quote.get("name"),
                "cost": position.get("cost") if position else None,
                "peak_price": position.get("peak_price") if position else None,
                "hold_days": _hold_days(position, day) if position else 0,
            },
            kline=_completed_kline(code, day, inclusive=False), snap=local_quote,
            overseas_gate={"light": "unknown"}, news_hits=[],
        )
        plan["risk"]["model"] = decision
        available_qty = int((position or {}).get("available_qty") or 0)
        plan["available_qty"] = available_qty
        if not position:
            reasons.append("该策略账户没有此标的持仓")
        if requested_qty <= 0:
            requested_qty = available_qty
        if requested_qty > available_qty:
            reasons.append(f"可卖份额仅 {available_qty} 股；当日买入份额仍受 T+1 锁定")
        plan["recommended_qty"] = available_qty
        plan["qty"] = requested_qty

    if plan["qty"] < LOT_SIZE or plan["qty"] % LOT_SIZE:
        reasons.append("委托数量必须为 100 股的正整数倍")
    if order_type == "limit":
        limit_value = _num(limit_price)
        plan["triggered"] = price <= limit_value if side == "buy" else price >= limit_value

    # 手动委托只是人工发起，不得绕过自动交易使用的行情真实性门禁。限价单在
    # 尚未触发时可以保留（触发瞬间仍会复核）；一旦需要模拟成交，买入必须双源
    # 通过，卖出至少要有当日新鲜主行情，且跌停时绝不虚构成交。
    execution_gate = EP.quote_gate(
        local_quote, day, purpose="entry" if side == "buy" else "exit",
    )["status"]
    plan["risk"]["execution_quote"] = execution_gate
    requires_fill_gate = order_type == "market" or plan["triggered"]
    if requires_fill_gate and not execution_gate.get("fresh"):
        reasons.append(f"成交行情未通过校验：{execution_gate.get('reason') or '未知行情状态'}")
        plan["risk"]["fill_deferred"] = {
            "kind": "quote",
            "reason": execution_gate.get("reason") or "实时行情校验未通过",
        }
    if side == "sell" and requires_fill_gate:
        limit_pct = _limit_pct(code, local_quote.get("name"), local_quote.get("risk_flag"))
        limit_down = price <= 0 or _num(local_quote.get("pct")) <= -limit_pct + 0.05
        plan["risk"]["limit_down_gate"] = {
            "blocked": limit_down,
            "limit_pct": limit_pct,
            "pct": _num(local_quote.get("pct")),
        }
        if limit_down:
            reasons.append(f"当前触及 {limit_pct:.1f}% 跌停保护，不能虚构卖出成交")
            plan["risk"]["fill_deferred"] = {
                "kind": "limit_down",
                "reason": f"当前触及 {limit_pct:.1f}% 跌停保护",
            }
    fill_price = (
        min(price * (1 + SLIPPAGE), _num(limit_price))
        if side == "buy" and order_type == "limit" and plan["triggered"]
        else max(price * (1 - SLIPPAGE), _num(limit_price))
        if side == "sell" and order_type == "limit" and plan["triggered"]
        else price * (1 + SLIPPAGE if side == "buy" else 1 - SLIPPAGE)
    )
    amount = max(plan["qty"], 0) * max(fill_price, 0)
    fees = _commission(amount) + (amount * STAMP_SELL if side == "sell" else 0.0)
    if side == "buy":
        # 共享资金池可用性（含在途预占）同样交给 planner，保持与自动路径同口径。
        cash_check = EP.cash_gate(
            conn, side, amount, fees,
            exclude_reservation_key=exclude_reservation_key, shared_cash=shared_cash,
        )
        if not cash_check["allowed"]:
            reasons.append(cash_check["reason"])
    plan.update({
        "fill_price": round(fill_price, 4), "amount": round(amount, 2),
        "fees": round(fees, 2), "reasons": list(dict.fromkeys(reasons)),
    })
    plan["allowed"] = not plan["reasons"]
    if entry_frozen:
        plan["status"] = ENTRY_FROZEN_WAITLIST_STATUS
        plan["waitlisted"] = True
    else:
        plan["status"] = (
            "risk_rejected" if not plan["allowed"]
            else "ready_to_fill" if plan["triggered"]
            else "pending_limit"
        )
    plan["risk"] = _with_decision_snapshot(
        plan.get("risk") or {}, account_id=account_id, code=code, side=side,
        decision=(
            ENTRY_FROZEN_WAITLIST_STATUS
            if entry_frozen
            else "approved_manual" if plan["allowed"] else "rejected_manual"
        ),
        reason="；".join(plan.get("reasons") or []) or None,
        asof_date=day, quote=local_quote,
        kline=_completed_kline(code, day, inclusive=False),
        final_score=((plan.get("risk") or {}).get("model") or {}).get("avg_score")
        if isinstance((plan.get("risk") or {}).get("model"), dict) else None,
    )
    return plan


def preview_manual_order(
    account_id, code, side, qty=0, order_type="market", limit_price=None, asof_date=None,
):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        _db,
        init_db,
    )
    init_db()
    with _db() as conn:
        return _manual_order_plan(
            conn, account_id, code, side, qty, order_type, limit_price, asof_date,
        )


def _execute_manual_plan(conn, account, plan, order_id, asof_day):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        _assert_active_lease,
        _entry_freeze_enabled,
        _entry_frozen_reason,
    )
    import execution_planner as EP
    _assert_active_lease(conn, "manual fill")
    if plan.get("side") == "buy" and _entry_freeze_enabled():
        # Callers normally gate this earlier; keep the fill primitive itself
        # fail-closed so a future path cannot debit cash or write a lot while
        # the operator freeze is active.
        raise RuntimeError(_entry_frozen_reason("成交执行"))
    # 成交落库统一走中央执行计划器：预留已在提交阶段完成，这里只消费预占。
    # 自动策略买入（_commit_strategy_buy）复用同一原语，只是由 planner 内部预占。
    return EP.commit_fill(
        conn,
        account=account,
        plan=plan,
        order_id=order_id,
        asof_day=asof_day,
        reserved=True,
        action="manual_filled",
        risk_log_reason="手动模拟委托通过模型门禁并成交",
        audit_action="manual_order_filled",
        reason="手动模拟委托经模型复核后成交",
        detail=plan.get("risk"),
        assumption="本地行情快照按 0.10% 滑点模拟；不代表真实可成交价格",
    )


def _commit_strategy_buy(
    conn, account, plan, asof_day, *, reason, detail, action,
    is_t_base=True, assumption="实时行情 + 滑点模拟；不代表真实可成交价格",
):
    """Commit a strategy buy through one reservation/debit/fill transaction.

    Rebuy and scale-in used to debit the shared cash balance directly.  This
    primitive makes them obey the same reservation invariant as normal and
    manual buys, while a savepoint prevents a malformed lot/fill write from
    leaving a cash debit behind.
    """
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        STRATEGY_EXECUTION_RETRY_STATUS,
        _assert_active_lease,
        _finish_capital_reservation,
        _json,
        _lease_lost,
        _now,
        _num,
        _risk_log,
        _strategy_stamp,
    )
    import execution_planner as EP
    _assert_active_lease(conn, "strategy auxiliary buy")
    account_id = account["id"]
    code = str(plan["code"])
    qty = int(plan["qty"])
    fill_price = _num(plan["fill_price"])
    strategy_stamp = _strategy_stamp(conn, account_id)
    cursor = conn.execute(
        """INSERT INTO paper_orders(
           account_id,side,code,name,qty,planned_price,status,reason,
           risk_payload,created_at,strategy_id,strategy_version,strategy_checksum)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, "buy", code, plan.get("name"), qty,
         _num(plan.get("planned_price"), fill_price), "pending_execution",
         reason, _json(detail), _now(), *strategy_stamp),
    )
    order_id = int(cursor.lastrowid)
    savepoint = f"strategy_buy_{order_id}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        # 与手动成交共用同一落库原语（planner 内部完成预占 → 扣款 → 记 lot → 写 fill）。
        EP.commit_fill(
            conn,
            account=account,
            plan={**plan, "quote_at": plan.get("quote_at")},
            order_id=order_id,
            asof_day=asof_day,
            reserved=False,
            action=action,
            audit_message=f"{code} {qty}股 @ {fill_price:.2f}",
            reason=reason,
            detail=detail,
            assumption=assumption,
            is_t_base=is_t_base,
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if _lease_lost(exc):
            raise
        _finish_capital_reservation(conn, order_id, "released")
        failure = f"{action}未成交：{exc}"
        conn.execute(
            "UPDATE paper_orders SET status=?,reason=?,filled_price=NULL,amount=NULL,fees=NULL,executed_at=NULL WHERE id=?",
            (STRATEGY_EXECUTION_RETRY_STATUS, failure, order_id),
        )
        _risk_log(conn, account_id, code, "buy", STRATEGY_EXECUTION_RETRY_STATUS, failure, detail)
        return None, failure
    _assert_active_lease(conn, "strategy auxiliary audit")
    # risk/audit 事件由 planner 的 commit_fill 统一写入，此处不再重复记录，
    # 避免同一笔成交在 paper_risk_decisions / paper_audit 中出现两次。
    return {"order_id": order_id, "side": "buy", "code": code, "qty": qty}, None


def submit_manual_order(
    account_id, code, side, qty=0, order_type="market", limit_price=None, asof_date=None,
):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        ENTRY_FROZEN_WAITLIST_STATUS,
        MANUAL_EXECUTION_RETRY_STATUS,
        _audit,
        _commission,
        _date,
        _db,
        _entry_frozen_reason,
        _finish_capital_reservation,
        _json,
        _market_state,
        _now,
        _num,
        _quotes,
        _record_nav,
        _reserve_shared_capital,
        _risk_log,
        _rows,
        _strategy_stamp,
        dfc,
        init_db,
    )
    init_db()
    day = _date(asof_date)
    with _db() as snapshot_conn:
        existing_codes = [
            row["code"] for row in _rows(
                snapshot_conn,
                "SELECT DISTINCT code FROM paper_position_lots WHERE remaining_qty>0",
            )
        ]
    quote_map = _quotes(sorted(set(existing_codes) | {str(code)}), asof_date=day)
    live_universe = None
    if str(side).lower() == "buy":
        try:
            live_universe = dfc.fetch_market_snapshot_full(max_age=240) or []
        except Exception:
            live_universe = []
    market_context = _market_state(day, live_universe=live_universe, allow_network=True) if str(side).lower() == "buy" else None
    with _db(immediate=True) as conn:
        plan = _manual_order_plan(
            conn, account_id, code, side, qty, order_type, limit_price, day,
            quote=quote_map.get(str(code)) or {}, all_quotes=quote_map,
            live_universe=live_universe, market_context=market_context,
        )
        account_row = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        account = dict(account_row) if account_row else None
        status = plan.get("status") or "risk_rejected"
        reason = "；".join(plan.get("reasons") or [])
        if status == "pending_limit":
            reason = "限价尚未触发；委托当日有效，触发时重新执行风控"
        elif status == ENTRY_FROZEN_WAITLIST_STATUS:
            reason = reason or _entry_frozen_reason("手动委托")
        strategy_stamp = _strategy_stamp(conn, account_id)
        cursor = conn.execute(
            """INSERT INTO paper_orders(
               account_id,side,code,name,qty,planned_price,status,reason,risk_payload,
               order_type,origin,expires_at,created_at,
               strategy_id,strategy_version,strategy_checksum)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_id, side, code, plan.get("name"), int(plan.get("qty") or 0),
                _num(limit_price) if order_type == "limit" else _num(plan.get("quote_price")),
                status, reason, _json(plan.get("risk") or {}), order_type, "manual",
                day.isoformat() if status in {"pending_limit", ENTRY_FROZEN_WAITLIST_STATUS}
                and order_type == "limit" else None, _now(),
                *strategy_stamp,
            ),
        )
        order_id = cursor.lastrowid
        if status in {"ready_to_fill", "pending_limit"} and account \
                and plan.get("allowed") and side == "buy":
            # Limit orders reserve at their limit price (plus commission),
            # not at the last snapshot price.  A pending order therefore
            # cannot silently consume capacity earmarked for another model.
            reserve_price = (
                _num(limit_price) if status == "pending_limit"
                else _num(plan.get("fill_price"))
            )
            reserve_amount = max(0, int(plan.get("qty") or 0)) * max(reserve_price, 0.0)
            reserve_fees = _commission(reserve_amount)
            reserved, reserve_reason = _reserve_shared_capital(
                conn, order_id, account_id, code, reserve_amount, reserve_fees,
            )
            if not reserved:
                status = "risk_rejected"
                reason = reserve_reason or "共享资金池预占失败"
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=? WHERE id=?",
                    (status, reason, order_id),
                )
                _risk_log(conn, account_id, code, side, "manual_rejected", reason, plan)
        if status == "ready_to_fill" and account:
            savepoint = f"manual_fill_{int(order_id)}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                _execute_manual_plan(conn, account, plan, order_id, day)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                status = "filled"
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                _finish_capital_reservation(conn, order_id, "released")
                status = MANUAL_EXECUTION_RETRY_STATUS
                reason = f"手动模拟委托执行失败，可重试：{type(exc).__name__}: {exc}"
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=?,risk_payload=?,filled_price=NULL,amount=NULL,fees=NULL,executed_at=NULL WHERE id=?",
                    (status, reason, _json({**(plan.get("risk") or {}), "execution_error": str(exc), "retryable": True}), order_id),
                )
                _risk_log(conn, account_id, code, side, status, reason, {"order_id": order_id, "error": str(exc)})
        elif status == "pending_limit" and account and plan.get("allowed"):
            # Reservation is held until trigger, cancellation or expiry.
            pass
        elif status == ENTRY_FROZEN_WAITLIST_STATUS:
            _risk_log(
                conn, account_id, code, "buy", ENTRY_FROZEN_WAITLIST_STATUS,
                reason, plan,
            )
            _audit(conn, account_id, ENTRY_FROZEN_WAITLIST_STATUS, f"manual: {code} order={order_id}")
        elif status == "risk_rejected":
            _risk_log(conn, account_id, code, side, "manual_rejected", reason, plan)
        _record_nav(conn, day, quotes=quote_map)
        return {"order_id": order_id, "status": status, "plan": plan}


def cancel_manual_order(order_id):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        ENTRY_RETRY_ORDER_STATUSES,
        _audit,
        _db,
        _finish_capital_reservation,
        _now,
        init_db,
    )
    init_db()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM paper_orders WHERE id=? AND origin='manual'", (int(order_id),)
        ).fetchone()
        if not row:
            raise ValueError("未找到手动模拟委托")
        if row["status"] not in set(ENTRY_RETRY_ORDER_STATUSES):
            raise ValueError("只有待触发的限价委托可以撤销")
        conn.execute(
            "UPDATE paper_orders SET status='cancelled',reason='用户撤销模拟委托',cancelled_at=? WHERE id=?",
            (_now(), int(order_id)),
        )
        _finish_capital_reservation(conn, order_id, "released")
        _audit(conn, row["account_id"], "manual_order_cancelled", f"order={order_id}")
    return {"order_id": int(order_id), "status": "cancelled"}


def process_pending_manual_orders(asof_date=None):
    # Phase 2 extraction: resolved at call time to avoid a circular import.
    from paper_trading import (
        ENTRY_FREEZE_ENV,
        ENTRY_FROZEN_WAITLIST_STATUS,
        ENTRY_RETRY_ORDER_STATUSES,
        MANUAL_EXECUTION_RETRY_STATUS,
        _assert_active_lease,
        _audit,
        _commission,
        _date,
        _db,
        _entry_freeze_enabled,
        _entry_frozen_reason,
        _finish_capital_reservation,
        _json,
        _lease_lost,
        _loads,
        _market_state,
        _num,
        _quotes,
        _record_nav,
        _reserve_shared_capital,
        _risk_log,
        _rows,
        dfc,
        init_db,
    )
    import execution_planner as EP
    init_db()
    day = _date(asof_date)
    output = []
    retry_placeholders = ",".join("?" for _ in ENTRY_RETRY_ORDER_STATUSES)
    retry_params = tuple(ENTRY_RETRY_ORDER_STATUSES)
    with _db() as snapshot_conn:
        pending = _rows(
            snapshot_conn,
            f"""SELECT * FROM paper_orders
               WHERE origin='manual' AND status IN ({retry_placeholders})
               ORDER BY id""",
            retry_params,
        )
        position_codes = [
            row["code"] for row in _rows(
                snapshot_conn,
                "SELECT DISTINCT code FROM paper_position_lots WHERE remaining_qty>0",
            )
        ]
    if not pending:
        return output
    # All provider calls happen before taking the write lock.  A quote failure
    # is isolated per source by _quotes and leaves the order retryable.
    quote_map = _quotes(sorted({row["code"] for row in pending} | set(position_codes)), asof_date=day)
    live_universe = None
    if any(row.get("side") == "buy" for row in pending):
        try:
            live_universe = dfc.fetch_market_snapshot_full(max_age=240) or []
        except Exception:
            live_universe = []
    market_context = (
        _market_state(day, live_universe=live_universe, allow_network=True)
        if any(row.get("side") == "buy" for row in pending) else None
    )
    with _db(immediate=True) as conn:
        pending = _rows(
            conn,
            f"""SELECT * FROM paper_orders
               WHERE origin='manual' AND status IN ({retry_placeholders})
               ORDER BY id""",
            retry_params,
        )
        for order in pending:
            _assert_active_lease(conn, "pending manual order")
            if order.get("side") == "buy" and _entry_freeze_enabled():
                reason = _entry_frozen_reason("待触发限价委托")
                payload = _loads(order.get("risk_payload"), {})
                payload["entry_freeze"] = {
                    "enabled": True, "env": ENTRY_FREEZE_ENV,
                    "status": ENTRY_FROZEN_WAITLIST_STATUS,
                    "source": "待触发限价委托", "asof_date": day.isoformat(),
                    "previous_status": order.get("status"),
                    "reason": reason,
                }
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=?,risk_payload=? WHERE id=?",
                    (ENTRY_FROZEN_WAITLIST_STATUS, reason, _json(payload), order["id"]),
                )
                _risk_log(
                    conn, order["account_id"], order["code"], "buy",
                    ENTRY_FROZEN_WAITLIST_STATUS, reason, payload,
                )
                _audit(conn, order["account_id"], ENTRY_FROZEN_WAITLIST_STATUS,
                       f"pending_limit: {order['code']} order={order['id']}")
                output.append({
                    "order_id": order["id"], "status": ENTRY_FROZEN_WAITLIST_STATUS,
                    "waitlisted": True, "reason": reason,
                })
                continue
            if order.get("expires_at") and order["expires_at"] < day.isoformat():
                conn.execute(
                    "UPDATE paper_orders SET status='expired',reason='限价委托已过有效期' WHERE id=?",
                    (order["id"],),
                )
                _finish_capital_reservation(conn, order["id"], "released")
                output.append({"order_id": order["id"], "status": "expired"})
                continue
            quote = dict(quote_map.get(order["code"]) or {})
            # 待成交复核（revalidate）与提交时共用同一计划逻辑，避免两套口径。
            try:
                plan = EP.revalidate_order_plan(
                    conn, order, plan_builder=_manual_order_plan, asof_day=day,
                    quote=quote, all_quotes=quote_map, live_universe=live_universe,
                    market_context=market_context,
                )
            except Exception as exc:
                reason = f"待成交订单复核失败，可重试：{type(exc).__name__}: {exc}"
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=?,risk_payload=? WHERE id=?",
                    (MANUAL_EXECUTION_RETRY_STATUS, reason, _json({"execution_error": str(exc), "retryable": True}), order["id"]),
                )
                _finish_capital_reservation(conn, order["id"], "released")
                _risk_log(conn, order["account_id"], order["code"], order["side"], MANUAL_EXECUTION_RETRY_STATUS, reason, {"order_id": order["id"], "error": str(exc)})
                output.append({"order_id": order["id"], "status": MANUAL_EXECUTION_RETRY_STATUS, "reason": reason})
                continue
            if not plan["allowed"]:
                reason = "；".join(plan["reasons"])
                deferred = dict((plan.get("risk") or {}).get("fill_deferred") or {})
                # 触发时只是行情缺失/跌停的限价单继续保留，等待下一轮真实行情
                # 或价格解锁；不能释放资金后改用旧报价成交，也不把它伪装成风控
                # 拒绝。策略、资金和 T+1 等其它门禁失败才会终止委托。
                if plan.get("triggered") and deferred:
                    conn.execute(
                        "UPDATE paper_orders SET reason=?,risk_payload=? WHERE id=?",
                        (reason, _json(plan["risk"]), order["id"]),
                    )
                    output.append({
                        "order_id": order["id"],
                        "status": "pending_execution_guard",
                        "reason": deferred.get("reason") or reason,
                    })
                    continue
                conn.execute(
                    "UPDATE paper_orders SET status='risk_rejected',reason=?,risk_payload=? WHERE id=?",
                    (reason, _json(plan["risk"]), order["id"]),
                )
                _finish_capital_reservation(conn, order["id"], "released")
                output.append({"order_id": order["id"], "status": "risk_rejected", "reason": reason})
                continue
            if not plan["triggered"]:
                reserve_price = _num(order.get("planned_price"), _num(plan.get("limit_price")))
                reserve_amount = max(0, int(plan.get("qty") or 0)) * max(reserve_price, 0.0)
                reserve_fees = _commission(reserve_amount)
                reserved, reserve_reason = _reserve_shared_capital(
                    conn, order["id"], order["account_id"], order["code"],
                    reserve_amount, reserve_fees,
                )
                if not reserved:
                    reason = reserve_reason or "共享资金池预占失败"
                    conn.execute(
                        "UPDATE paper_orders SET status='risk_rejected',reason=?,risk_payload=? WHERE id=?",
                        (reason, _json(plan["risk"]), order["id"]),
                    )
                    _finish_capital_reservation(conn, order["id"], "released")
                    output.append({"order_id": order["id"], "status": "risk_rejected", "reason": reason})
                    continue
                conn.execute(
                    "UPDATE paper_orders SET status='pending_limit',reason=?,risk_payload=? WHERE id=?",
                    ("限价尚未触发；委托当日有效，触发时重新执行风控", _json(plan["risk"]), order["id"]),
                )
                output.append({"order_id": order["id"], "status": "pending_limit"})
                continue
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (order["account_id"],)
            ).fetchone())
            reserve_price = _num(plan.get("fill_price"), _num(order.get("planned_price")))
            reserve_amount = max(0, int(plan.get("qty") or 0)) * max(reserve_price, 0.0)
            reserve_fees = _commission(reserve_amount)
            reserved, reserve_reason = _reserve_shared_capital(
                conn, order["id"], order["account_id"], order["code"],
                reserve_amount, reserve_fees,
            )
            if not reserved:
                reason = reserve_reason or "共享资金池预占失败"
                # A triggered order can become temporarily unfunded because
                # another reservation is still active.  Keep it retryable;
                # turning this into a terminal rejection loses the order even
                # though a later sell/cancel may release the cash.
                conn.execute(
                    "UPDATE paper_orders SET status='pending_limit',reason=?,risk_payload=? WHERE id=?",
                    (f"触发后等待资金重算：{reason}", _json({**(plan["risk"] or {}), "fill_deferred": {
                        "reason": reason, "retryable": True,
                    }}), order["id"]),
                )
                output.append({"order_id": order["id"], "status": "pending_execution_guard", "reason": reason})
                continue
            # Isolate each pending fill.  A single stale reservation, cash
            # mismatch, or ledger error must not roll back the whole batch and
            #—more importantly—must not abort the caller's subsequent risk
            # sells for this scan.
            savepoint = f"pending_exec_{int(order['id'])}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                _execute_manual_plan(conn, account, plan, order["id"], day)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                output.append({"order_id": order["id"], "status": "filled"})
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if _lease_lost(exc):
                    raise
                reason = f"待成交执行异常，下一轮重试：{type(exc).__name__}: {exc}"
                # Release the old reservation before retrying.  The next
                # cycle will resize it against the then-current cash pool.
                _finish_capital_reservation(conn, order["id"], "released")
                conn.execute(
                    "UPDATE paper_orders SET status='pending_limit',reason=?,risk_payload=? WHERE id=?",
                    (reason, _json({**(plan.get("risk") or {}),
                                    "execution_error": str(exc),
                                    "execution_retry": True}), order["id"]),
                )
                _risk_log(
                    conn, order["account_id"], order["code"], order["side"],
                    "pending_execution_retry", reason,
                    {"order_id": order["id"], "error": str(exc)},
                )
                output.append({
                    "order_id": order["id"], "status": "pending_execution_retry",
                    "reason": reason,
                })
        _record_nav(conn, day, quotes=quote_map)
    return output
