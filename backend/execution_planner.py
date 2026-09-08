# -*- coding: utf-8 -*-
"""中央执行计划器（PR-06）：统一自动策略与手动委托的 plan / revalidate / commit。

背景
----
此前“下单前能不能买、买多少、怎么落库”分散在 ``paper_trading._buy_order`` 与
``manual_orders._manual_order_plan`` 两条链路里：同一条“共享池最后一席预留给主力”
的规则被抄了两份，并且都以“比较账户 ID”的形式硬编码
在执行路径上；手动委托是否走策略专属入场复核也靠比较账户 ID 判断。
结果是新增一个策略就要改执行代码，两条链路还容易各自漂移。

本模块把**执行决策**集中到一处：

- :class:`ExecutionPolicy` / :func:`policy_for`：把身份差异收敛成一张声明式策略表，
  执行代码只读策略字段，不再按账户 ID 分支；
- :func:`seat_reserve_gate` / :func:`capacity_gate`：席位与容量门禁，自动与手动共用；
- :func:`quote_gate` / :func:`security_gate` / :func:`market_gate` /
  :func:`account_risk_gate` / :func:`cash_gate`：继续复用既有行情新鲜度、证券范围、
  市场灯、账户风险与共享资金检查，此处只做统一编排与口径收敛；
- :func:`plan_entry` / :func:`revalidate_order_plan`：下单前复核（revalidate）复用
  同一套计划逻辑，避免“提交时一套口径、触发时另一套口径”；
- :func:`commit_fill`：统一成交落库原语（预留 → 扣款 → 记 lot → 写 fill → 风险日志）。

本模块不在导入期依赖 ``paper_trading``（惰性取用，避免循环导入），且不改任何
既有门禁的判定口径——只改“谁来编排”。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "EXECUTION_PLANNER_VERSION",
    "ExecutionPolicy",
    "account_risk_gate",
    "capacity_gate",
    "cash_gate",
    "commit_fill",
    "market_gate",
    "plan_entry",
    "policy_for",
    "quote_gate",
    "revalidate_order_plan",
    "seat_reserve_gate",
    "security_gate",
]

EXECUTION_PLANNER_VERSION = "execution-planner-v1"

# 席位预留与手动入场复核的“所有者”仍然来自 paper_trading 的单一常量定义，
# 但只在构造策略表时读取一次；执行代码里不再出现身份比较。
_CHASE_LANES = frozenset({"none", "momentum", "sector_hot"})
_SEAT_RESERVE_DEADLINE = "14:30"


@dataclass(frozen=True)
class ExecutionPolicy:
    """一个账户在执行层的声明式差异。

    所有字段都是“能力/口径”，不是分支条件。执行代码通过
    ``policy_for(account_id)`` 取用，新增账户只需在策略表加一行。
    """

    account_id: str
    # 风险/提示文案里使用的策略名（避免执行代码出现账户 ID 字面量）。
    entry_label: str = ""
    # 追高通道：none=不追高，momentum=短线接力，sector_hot=板块热点加速。
    chase_lane: str = "none"
    chase_rejection: str = "该策略不追高"
    # 盘中“突破加速确认”门限（当日涨幅超过该值需双源/Q1/资金量能同步）；
    # None 表示该策略不使用该门禁。
    acceleration_pct: float | None = None
    # 是否附加一致预期 EPS 上下文（仅供人工复核与后续建模，不参与评分）。
    eps_consensus_context: bool = False
    # 首仓纪律：首笔买入不超过共享净值的该比例；None 表示不限制。
    first_tranche_nav_pct: float | None = None
    # 市场红灯时该策略的暂停理由（复用市场灯判定，仅文案按策略区分）。
    red_light_reason: str = "市场红灯，策略暂停新开仓"
    # 手动委托是否必须走策略专属入场复核（而不是绕过策略模型）。
    manual_entry_review: bool = False
    # 该账户请求买入时，共享池最后一席要预留给哪个账户（None=不预留）。
    seat_reserve_owner: str | None = None
    # 该账户是否持有被预留的席位（预留 owner 自身不受预留限制）。
    holds_reserved_seat: bool = False
    seat_reserve_deadline: str = _SEAT_RESERVE_DEADLINE

    def reserves_seat_for(self, pool_open_positions, pool_limit: int) -> bool:
        """是否进入“最后一席预留”检查窗口（与 owner 无关的容量前提）。"""
        owner = self.seat_reserve_owner
        return bool(owner) and not self.holds_reserved_seat and pool_limit > 1 and len(pool_open_positions) >= pool_limit - 1


def _pt():
    """惰性取用 paper_trading（避免导入期循环依赖）。"""
    import paper_trading as PT
    return PT


def _default_policies() -> dict[str, ExecutionPolicy]:
    """按 paper_trading 的账户常量构造默认策略表。

    身份到能力的映射只在这里出现一次：执行路径里不再有
    “比较账户 ID”之类的判断。
    """
    PT = _pt()
    main_force = str(getattr(PT, "MAIN_FORCE_STRATEGY_ID", "") or "")
    quality_breakout = str(getattr(PT, "NEW_STRATEGY_ID", "") or "")
    policies: dict[str, ExecutionPolicy] = {}

    def put(account_id: str, **kwargs) -> None:
        if not account_id:
            return
        policies[account_id] = ExecutionPolicy(account_id=account_id, **kwargs)

    # 主力策略拥有被预留的共享池席位；其他策略在池子只剩最后一席时为其让位。
    put(
        main_force,
        entry_label="主力策略",
        chase_lane="none",
        chase_rejection="主力策略不使用追高通道",
        red_light_reason="市场红灯，超强主力股暂停新开仓",
        first_tranche_nav_pct=0.12,
        holds_reserved_seat=True,
    )
    # 财报突破质量的手动委托必须走独立入场复核，不能在运营界面绕过策略模型。
    put(
        quality_breakout,
        entry_label="三日策略",
        chase_lane="none",
        chase_rejection="三日策略不使用追高通道",
        red_light_reason="市场红灯，三日策略暂停新开仓",
        acceleration_pct=3.5,
        eps_consensus_context=True,
        manual_entry_review=True,
        seat_reserve_owner=main_force,
    )
    put(
        "tq_breakout",
        entry_label="首板接力",
        chase_lane="momentum",
        chase_rejection="短线接力策略不追高",
        red_light_reason="市场红灯，首板接力暂停新开仓",
        seat_reserve_owner=main_force,
    )
    put(
        "trend_pullback",
        entry_label="趋势回踩",
        chase_lane="none",
        chase_rejection="趋势回踩策略不追高",
        red_light_reason="市场红灯，趋势回踩策略暂停新开仓",
        seat_reserve_owner=main_force,
    )
    put(
        "sector_rotation",
        entry_label="板块轮动",
        chase_lane="sector_hot",
        chase_rejection="板块轮动策略不追高",
        red_light_reason="市场红灯，板块轮动策略暂停新开仓",
        seat_reserve_owner=main_force,
    )
    return policies


_POLICY_CACHE: dict[str, ExecutionPolicy] | None = None
_POLICY_DEFAULT = ExecutionPolicy(account_id="")


def policy_for(account_id: str) -> ExecutionPolicy:
    """取账户的执行策略；未知账户返回一个保守默认（不预留、不追高）。"""
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        _POLICY_CACHE = _default_policies()
    return _POLICY_CACHE.get(str(account_id or ""), _POLICY_DEFAULT)


def seat_reserve_gate(
    conn,
    requester_id: str,
    pool_open_positions,
    pool_limit: int,
    asof_day=None,
) -> dict[str, Any]:
    """共享池最后一席预留门禁（自动与手动共用同一口径）。

    预留只在下列条件同时成立时生效：①本账户不是席位 owner 且 owner 当前空仓；
    ②池内占用已达 ``pool_limit - 1``；③owner 当日确有在途候选（排队/等待复核的
    非终态信号）；④未到放行时限（默认当日 14:30）。查询异常时维持原预留行为
    （fail-closed），与主站 2026-09-03 的死锁修复口径一致。
    """
    PT = _pt()
    policy = policy_for(requester_id)
    owner = policy.seat_reserve_owner or ""
    detail = {
        "reserved": False,
        "owner": owner or None,
        "interest": 0,
        "deadline": policy.seat_reserve_deadline,
        "planner": EXECUTION_PLANNER_VERSION,
    }
    if not owner or policy.holds_reserved_seat:
        return detail
    if any(str(key[0]) == owner for key in pool_open_positions):
        return detail
    if pool_limit <= 1 or len(pool_open_positions) < pool_limit - 1:
        return detail
    interest = 0
    try:
        interest = int(conn.execute(
            "SELECT COUNT(*) FROM paper_signals "
            "WHERE account_id=? AND intended_date=? AND status IN (?,?,?)",
            (owner, str(asof_day)[:10], *PT.ENTRY_RETRY_SIGNAL_STATUSES),
        ).fetchone()[0] or 0)
    except Exception:
        interest = 1
    now = PT._now()
    day = str(asof_day)[:10]
    deadline_at = f"{day} {policy.seat_reserve_deadline}:00" if day == now[:10] else None
    detail.update({
        "interest": interest,
        "deadline_at": deadline_at,
        "reserved": interest > 0 and (deadline_at is None or now < deadline_at),
    })
    return detail


def capacity_gate(
    *,
    code: str,
    account_id: str,
    open_codes,
    committed_open_codes,
    pool_open_positions,
    position_limit: int,
    pool_limit: int,
    asof_day=None,
    conn=None,
    allocation_source=None,
    allocation_version=None,
) -> dict[str, Any]:
    """策略席位 + 共享池席位门禁（含席位预留），返回 gate 明细与拒绝原因。"""
    reserve = seat_reserve_gate(
        conn, account_id, pool_open_positions, pool_limit, asof_day,
    ) if conn is not None else {
        "reserved": False, "owner": None, "interest": 0,
        "deadline": _SEAT_RESERVE_DEADLINE, "planner": EXECUTION_PLANNER_VERSION,
    }
    gate = {
        "current": len(open_codes),
        "committed": len(committed_open_codes),
        "limit": max(1, int(position_limit)),
        "pool_current": len(pool_open_positions),
        "pool_limit": pool_limit,
        "dynamic": True,
        "source": allocation_source,
        "allocation_version": allocation_version,
        "is_existing_position": code in open_codes,
        "seat_reserved": bool(reserve.get("reserved")),
        "seat_reserve_owner": reserve.get("owner"),
        "seat_reserve_interest": reserve.get("interest"),
        "planner": EXECUTION_PLANNER_VERSION,
        "scope": "按策略账户计数；同一股票可由其他策略独立持有和交易",
    }
    reasons: list[str] = []
    if code not in committed_open_codes and len(committed_open_codes) >= gate["limit"]:
        reasons.append(
            f"策略持仓及待成交席位已达动态上限 {len(committed_open_codes)}/{gate['limit']}"
        )
    if (account_id, code) not in pool_open_positions and len(pool_open_positions) >= pool_limit:
        reasons.append(
            f"总持仓及待成交席位已达共享硬上限 {len(pool_open_positions)}/{pool_limit}"
        )
    elif reserve.get("reserved"):
        owner_name = reserve.get("owner")
        reasons.append(
            "共享池仅剩最后 1 席：为主力策略独立席位预留，"
            "待主力建仓或池内席位释放后恢复其他策略买入"
            if owner_name else "共享池仅剩最后 1 席：为预留席位账户保留"
        )
    return {"gate": gate, "reasons": reasons, "reserve": reserve}


def quote_gate(quote, asof_day, purpose: str = "entry") -> dict[str, Any]:
    """行情新鲜度门禁（复用 ``_execution_quote_status``，口径不变）。"""
    PT = _pt()
    status = PT._execution_quote_status(quote, asof_day, purpose=purpose)
    return {
        "status": status,
        "fresh": bool(status.get("fresh")),
        "reason": status.get("reason"),
        "planner": EXECUTION_PLANNER_VERSION,
    }


def security_gate(code, name=None, risk_flag=None) -> dict[str, Any]:
    """证券范围门禁（复用 ``_security_scope``，口径不变）。"""
    PT = _pt()
    scope = PT._security_scope(code, name, risk_flag)
    return {
        "scope": scope,
        "allowed": bool(scope.get("allowed")),
        "reason": scope.get("reason"),
        "planner": EXECUTION_PLANNER_VERSION,
    }


def market_gate(market, account_id: str) -> dict[str, Any]:
    """市场灯门禁（红灯/未知禁止新开仓；文案按策略声明，判定口径不变）。"""
    state = market if isinstance(market, Mapping) else {}
    light = state.get("light")
    blocked = light in ("red", "unknown")
    return {
        "market": dict(state),
        "blocked": blocked,
        "reason": policy_for(account_id).red_light_reason if blocked else None,
        "planner": EXECUTION_PLANNER_VERSION,
    }


def account_risk_gate(risk_state) -> list[str]:
    """账户风控状态（熔断/冷静期）→ 拒绝原因列表。"""
    state = risk_state if isinstance(risk_state, Mapping) else {}
    if not state.get("blocked"):
        return []
    return [str(item) for item in (state.get("reasons") or []) if item]


def cash_gate(
    conn,
    side: str,
    amount: float,
    fees: float,
    *,
    exclude_reservation_key=None,
    shared_cash: float | None = None,
) -> dict[str, Any]:
    """共享资金池可用性门禁（含在途买单预占）。"""
    PT = _pt()
    if side != "buy":
        return {"allowed": True, "reason": None, "pending_cash": 0.0, "shared_cash": shared_cash}
    _, pending_cash = PT._pending_buy_reservations(
        conn, exclude_order_key=exclude_reservation_key,
    )
    available = PT._shared_cash(conn) if shared_cash is None else float(shared_cash)
    short = amount + fees > available - pending_cash + 1e-6
    reason = None
    if short:
        reason = (
            f"共享资金池可用现金不足（已有待成交买单预占 ¥{pending_cash:,.2f}）"
            if pending_cash > 0 else "共享资金池可用现金不足"
        )
    return {
        "allowed": not short,
        "reason": reason,
        "pending_cash": pending_cash,
        "shared_cash": available,
        "planner": EXECUTION_PLANNER_VERSION,
    }


def plan_entry(
    conn,
    *,
    account: Mapping[str, Any],
    code: str,
    side: str,
    quote: Mapping[str, Any],
    asof_day,
    market=None,
    open_codes=None,
    committed_open_codes=None,
    pool_open_positions=None,
    position_limit: int | None = None,
    pool_limit: int | None = None,
    allocation_source=None,
    allocation_version=None,
    risk_state=None,
    amount: float = 0.0,
    fees: float = 0.0,
    exclude_reservation_key=None,
    shared_cash: float | None = None,
    require_market_gate: bool = True,
) -> dict[str, Any]:
    """下单前的统一准入编排：自动与手动共用同一套门禁与文案。

    只负责“能不能下”，不负责“下多少”（数量仍由 ``paper_sizing``/``_price_aware_qty``
    在执行时决定）。返回 ``{"allowed", "reasons", "gates", "policy"}``。
    """
    account_id = str((account or {}).get("id") or "")
    policy = policy_for(account_id)
    reasons: list[str] = []
    gates: dict[str, Any] = {}

    scope = security_gate(code, quote.get("name"), quote.get("risk_flag"))
    gates["security_scope"] = scope["scope"]
    if not scope["allowed"]:
        reasons.append(scope["reason"])

    if require_market_gate and market is not None:
        market_check = market_gate(market, account_id)
        gates["market"] = market_check["market"]
        if market_check["blocked"]:
            reasons.append(market_check["reason"])

    if risk_state is not None:
        gates["account"] = risk_state
        reasons.extend(account_risk_gate(risk_state))

    if side == "buy" and pool_open_positions is not None and pool_limit is not None:
        capacity = capacity_gate(
            code=code, account_id=account_id,
            open_codes=open_codes or set(),
            committed_open_codes=committed_open_codes or set(),
            pool_open_positions=pool_open_positions,
            position_limit=position_limit if position_limit is not None else 1,
            pool_limit=pool_limit, asof_day=asof_day, conn=conn,
            allocation_source=allocation_source,
            allocation_version=allocation_version,
        )
        gates["position_count_gate"] = capacity["gate"]
        gates["seat_reserve"] = capacity["reserve"]
        reasons.extend(capacity["reasons"])

    freshness = quote_gate(quote, asof_day, purpose="entry" if side == "buy" else "exit")
    gates["execution_quote"] = freshness["status"]
    if not freshness["fresh"]:
        reasons.append(
            f"成交行情未通过校验：{freshness['reason'] or '未知行情状态'}"
        )

    cash = cash_gate(
        conn, side, amount, fees,
        exclude_reservation_key=exclude_reservation_key, shared_cash=shared_cash,
    )
    gates["cash"] = cash
    if not cash["allowed"]:
        reasons.append(cash["reason"])

    return {
        "allowed": not reasons,
        "reasons": list(dict.fromkeys([str(item) for item in reasons if item])),
        "gates": gates,
        "policy": {
            "account_id": account_id,
            "chase_lane": policy.chase_lane,
            "manual_entry_review": policy.manual_entry_review,
            "seat_reserve_owner": policy.seat_reserve_owner,
            "planner": EXECUTION_PLANNER_VERSION,
        },
        # 手动/自动都据此决定是否需要策略专属入场复核，不再比较账户 ID。
        "requires_manual_entry_review": bool(policy.manual_entry_review),
    }


def revalidate_order_plan(conn, order, *, plan_builder, asof_day, quote=None, **kwargs):
    """待成交委托的复核（revalidate）：与提交时用同一套计划逻辑。

    ``plan_builder`` 由调用方注入（手动委托即 ``_manual_order_plan``），本函数只负责
    把持久化的委托行翻译成计划请求，避免“提交一套口径、触发另一套口径”。
    """
    row = dict(order or {})
    return plan_builder(
        conn,
        row.get("account_id"),
        row.get("code"),
        (row.get("side") or "").lower(),
        row.get("qty") or 0,
        (row.get("order_type") or "limit").lower(),
        row.get("planned_price") if (row.get("order_type") or "limit").lower() == "limit" else None,
        asof_day,
        quote=quote,
        exclude_reservation_key=str(row.get("id")) if row.get("id") is not None else None,
        **kwargs,
    )


def commit_fill(
    conn,
    *,
    account: Mapping[str, Any],
    plan: Mapping[str, Any],
    order_id: int,
    asof_day,
    side: str | None = None,
    reserved: bool = False,
    action: str = "manual_filled",
    risk_log_reason: str | None = None,
    audit_action: str | None = None,
    audit_message: str | None = None,
    reason: str = "手动模拟委托经模型复核后成交",
    detail: Mapping[str, Any] | None = None,
    assumption: str = "本地行情快照按 0.10% 滑点模拟；不代表真实可成交价格",
    is_t_base: bool = True,
):
    """统一成交落库原语：自动与手动共用“扣款 → 记 lot → 写 fill → 风险日志”。

    - ``reserved=True``：资金已在委托阶段预占（手动/策略待成交），成交时消费预占；
    - ``reserved=False``：本函数内部完成预占再扣款（策略辅助买入）。
    """
    PT = _pt()
    side = side or str(plan.get("side") or "")
    qty = int(plan.get("qty") or 0)
    amount = PT._num(plan.get("amount"))
    fees = PT._num(plan.get("fees"))
    fill_price = PT._num(plan.get("fill_price"))
    code = str(plan.get("code"))
    account_id = account["id"]
    realized_pnl = None

    PT._assert_active_lease(conn, "execution planner commit")
    if side == "buy":
        if not reserved:
            ok, reserve_reason = PT._reserve_shared_capital(
                conn, order_id, account_id, code, amount, fees,
            )
            if not ok:
                raise RuntimeError(reserve_reason or "共享资金池预占失败")
        PT._assert_active_lease(conn, "execution planner cash debit")
        PT._debit_shared_cash(conn, amount + fees, preferred_account_id=account_id)
        PT._finish_capital_reservation(conn, order_id, "consumed")
        PT._record_lot(
            conn, account, plan, qty, fill_price, asof_day, order_id,
            is_t_base=is_t_base, fees=fees,
        )
    else:
        PT._assert_active_lease(conn, "execution planner lot consumption")
        consumed, cost_amount = PT._consume_available_lots(conn, account_id, code, qty, asof_day)
        if consumed != qty:
            raise RuntimeError("可卖份额在成交前发生变化，委托已停止")
        realized_pnl = amount - cost_amount - fees
        PT._credit_shared_cash(conn, amount - fees, account_id)

    PT._assert_active_lease(conn, "execution planner finalization")
    conn.execute(
        """UPDATE paper_orders SET filled_price=?,amount=?,fees=?,status='filled',
           reason=?,risk_payload=?,realized_pnl=?,executed_at=? WHERE id=?""",
        (fill_price, amount, fees, reason, PT._json(detail if detail is not None else plan),
         realized_pnl, PT._now(), order_id),
    )
    conn.execute(
        """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, account_id, side, code, qty, fill_price, amount, fees,
         PT._date(asof_day).isoformat(), plan.get("quote_at"), assumption),
    )
    PT._risk_log(
        conn, account_id, code, side, action,
        risk_log_reason or reason, detail if detail is not None else plan,
    )
    PT._audit(
        conn, account_id, audit_action or action,
        audit_message or f"{side} {code} {qty}股 @ {fill_price:.2f}",
    )
    PT._sync_positions(conn, account_id, asof_day)
    return realized_pnl
