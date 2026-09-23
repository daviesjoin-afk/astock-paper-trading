# -*- coding: utf-8 -*-
"""中央执行计划器：统一自动策略与手动委托的准入、复核和模拟执行。

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
- :func:`execute_order`：唯一模拟成交 authority（决策 → 预留 → 账务 → 成交事件 → 风险日志）。

本模块不在导入期依赖 ``paper_trading``（惰性取用，避免循环导入），且不改任何
既有门禁的判定口径——只改“谁来编排”。
"""
from __future__ import annotations

import sqlite3
import datetime as dt
import hashlib
import json
import math

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import paper_position_risk_state as PPRS
import paper_trading_rules as PTR
import execution_lifecycle as EL

__all__ = [
    "EXECUTION_PLANNER_VERSION",
    "ExecutionPolicy",
    "ExecutionContext",
    "ExecutionDecision",
    "build_execution_context",
    "decide_simulated_execution",
    "account_risk_gate",
    "capacity_gate",
    "cash_gate",
    "execute_order",
    "market_gate",
    "plan_entry",
    "policy_for",
    "quote_gate",
    "revalidate_order_plan",
    "seat_reserve_gate",
    "security_gate",
]

EXECUTION_PLANNER_VERSION = "execution-planner-v1"
EXECUTION_RULESET_VERSION = PTR.SIMULATION_RULESET_VERSION
LIQUIDITY_PARTICIPATION_RATE = 0.01
LOT_SIZE = 100


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """单次模拟执行所需的冻结输入；quote 以规范 JSON 保存，避免引用外部可变字典。"""

    account_id: str
    cycle_id: int | None
    code: str
    side: str
    desired_qty: int
    as_of: str
    market_evidence_json: str
    order_type: str = "market"
    limit_price: float | None = None
    sellable_qty: int | None = None
    same_day_filled_qty: int = 0

    @property
    def market_evidence(self) -> dict[str, Any]:
        value = json.loads(self.market_evidence_json or "{}")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """后端执行规则的不可变结果，包含数量、价格、原因和市场事实。"""

    executable_now: bool
    status: str
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    desired_qty: int
    max_executable_qty: int
    filled_qty: int
    remaining_qty: int
    reference_price: float | None
    fill_price: float | None
    amount: float
    fees: float
    commission: float
    stamp_duty: float
    slippage_amount: float
    pricing_basis: str | None
    as_of: str
    session: str
    market_state: str
    liquidity: Mapping[str, Any]
    ruleset_version: str = EXECUTION_RULESET_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable_now": self.executable_now,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "desired_qty": self.desired_qty,
            "max_executable_qty": self.max_executable_qty,
            "filled_qty": self.filled_qty,
            "remaining_qty": self.remaining_qty,
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "amount": self.amount,
            "fees": self.fees,
            "commission": self.commission,
            "stamp_duty": self.stamp_duty,
            "slippage_amount": self.slippage_amount,
            "pricing_basis": self.pricing_basis,
            "as_of": self.as_of,
            "session": self.session,
            "market_state": self.market_state,
            "liquidity": dict(self.liquidity),
            "ruleset_version": self.ruleset_version,
        }


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_execution_context(
    *, account_id: str, cycle_id: int | None, code: str, side: str,
    desired_qty: int, as_of: Any, market_evidence: Mapping[str, Any],
    order_type: str = "market", limit_price: float | None = None,
    sellable_qty: int | None = None, same_day_filled_qty: int = 0,
) -> ExecutionContext:
    """把调用方行情快照固化为不可变执行输入，不读取 provider 或当前行情。"""
    raw = json.dumps(dict(market_evidence or {}), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    if isinstance(as_of, dt.datetime):
        as_of_text = as_of.isoformat(timespec="seconds")
    else:
        as_of_text = str(as_of or "").strip()
    return ExecutionContext(
        account_id=str(account_id), cycle_id=int(cycle_id) if cycle_id is not None else None,
        code=str(code), side=str(side or "").lower(), desired_qty=int(desired_qty or 0),
        as_of=as_of_text, market_evidence_json=raw,
        order_type=str(order_type or "market").lower(),
        limit_price=_finite_number(limit_price),
        sellable_qty=int(sellable_qty) if sellable_qty is not None else None,
        same_day_filled_qty=max(0, int(same_day_filled_qty or 0)),
    )


def _blocked_decision(context: ExecutionContext, codes: list[str], reasons: list[str], *,
                      session: str = "unknown", market_state: str = "unknown",
                      liquidity: Mapping[str, Any] | None = None,
                      reference_price: float | None = None) -> ExecutionDecision:
    return ExecutionDecision(
        False, "blocked", tuple(codes), tuple(reasons), context.desired_qty, 0, 0,
        max(0, context.desired_qty), reference_price, None, 0.0, 0.0, 0.0, 0.0,
        0.0, None, context.as_of, session, market_state, dict(liquidity or {}),
    )


def decide_simulated_execution(context: ExecutionContext) -> ExecutionDecision:
    """按单一后端规则决定当前能否成交以及可成交数量；不做任何数据库或网络写入。"""
    quote = context.market_evidence
    reasons: list[tuple[str, str]] = []
    try:
        as_of = dt.datetime.fromisoformat(context.as_of.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _blocked_decision(context, ["missing_execution_as_of"], ["缺少有效执行时点"])
    if as_of.tzinfo is not None:
        as_of = as_of.astimezone().replace(tzinfo=None)
    quote_text = str(quote.get("quote_at") or "")
    try:
        quote_at = dt.datetime.fromisoformat(quote_text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _blocked_decision(context, ["missing_market_as_of"], ["行情缺少可信源时间戳"])
    if quote_at.tzinfo is not None:
        quote_at = quote_at.astimezone().replace(tzinfo=None)
    if quote_at.date() != as_of.date() or quote_at > as_of:
        return _blocked_decision(context, ["market_as_of_mismatch"], ["行情时点与执行时点不一致或行情来自未来"])
    session = "morning" if dt.time(9, 30) <= as_of.time() <= dt.time(11, 30) else (
        "afternoon" if dt.time(13, 0) <= as_of.time() <= dt.time(15, 0) else "closed_or_break"
    )
    if not PTR.is_trade_weekday(as_of.date()):
        reasons.append(("non_trading_day", "执行日不是 A 股交易日"))
    elif session == "closed_or_break":
        reasons.append(("outside_continuous_session", "当前处于非连续交易时段"))
    quote_status = quote_gate(quote, as_of.date(), purpose="entry" if context.side == "buy" else "exit")
    if not quote_status.get("fresh"):
        reasons.append(("market_untrusted", str(quote_status.get("reason") or "行情真实性或新鲜度未通过")))
    if str(quote.get("code") or context.code) != context.code:
        reasons.append(("market_symbol_mismatch", "行情标的与委托标的不一致"))
    if quote.get("suspended") is True or quote.get("halted") is True or quote.get("tradable") is False \
            or quote.get("snapshot_tradable") is False:
        reasons.append(("not_tradable", "证券停牌或行情标记为不可交易"))
    listing = str(quote.get("listing_status") or "").strip().lower()
    if listing and listing not in {"listed", "active", "trading", "正常"}:
        reasons.append(("not_tradable", "证券上市状态不允许交易"))
    reference = _finite_number(quote.get("price"))
    if reference is None or reference <= 0:
        reasons.append(("invalid_market_price", "缺少有效行情价格"))
    if context.side not in {"buy", "sell"}:
        reasons.append(("unsupported_side", "委托方向无效"))
    if context.desired_qty <= 0:
        reasons.append(("invalid_quantity", "委托数量必须为正数"))
    if context.side == "buy" and context.desired_qty % LOT_SIZE:
        reasons.append(("invalid_lot_size", "买入数量必须是 100 股的整数倍"))
    if context.side == "sell":
        if context.sellable_qty is None:
            reasons.append(("sellable_quantity_unknown", "下单时点可卖份额无法证明"))
        elif context.sellable_qty <= 0:
            reasons.append(("t_plus_one_locked", "没有已解锁的可卖份额（受 T+1 限制）"))
    if context.order_type == "limit" and context.limit_price is not None and reference is not None:
        triggered = reference <= context.limit_price if context.side == "buy" else reference >= context.limit_price
        if not triggered:
            reasons.append(("limit_not_triggered", "限价尚未触发"))

    explicit_up = _finite_number(quote.get("limit_up_price"))
    explicit_down = _finite_number(quote.get("limit_down_price"))
    no_daily_limit = quote.get("no_price_limit") is True
    band = PTR.price_limit_band(
        context.code, quote.get("name"), quote.get("risk_flag"), quote.get("prev_close"),
    )
    if explicit_up is not None and explicit_down is not None:
        band = {
            "limit_up_price": explicit_up, "limit_down_price": explicit_down,
            "limit_pct": (band or {}).get("limit_pct"), "source": "market_evidence",
        }
    if not no_daily_limit and band is None:
        reasons.append(("price_limit_reference_unknown", "缺少前收盘价或涨跌停价，不能确认成交价格边界"))
    if reference is not None and band:
        tick = 0.01
        if reference > band["limit_up_price"] + tick / 2 or reference < band["limit_down_price"] - tick / 2:
            reasons.append(("price_outside_daily_limit", "行情价格超出当日涨跌停范围"))

    limit_pct = float((band or {}).get("limit_pct") or PTR.limit_pct(
        context.code, quote.get("name"), quote.get("risk_flag"),
    ))
    pct = _finite_number(quote.get("pct"))
    locked_key = "limit_up_locked" if context.side == "buy" else "limit_down_locked"
    depth_key = "ask_available_qty" if context.side == "buy" else "bid_available_qty"
    depth_qty = _finite_number(quote.get(depth_key))
    at_directional_limit = (not no_daily_limit) and pct is not None and (
        pct >= limit_pct - 0.05 if context.side == "buy" else pct <= -limit_pct + 0.05
    )
    locked_value = quote.get(locked_key)
    locked = locked_value is True or (locked_value is None and at_directional_limit and not (depth_qty and depth_qty > 0))
    if locked:
        reasons.append(("locked_price_limit", "涨跌停封板且缺少对手盘可成交量"))

    amount = _finite_number(quote.get("amount"))
    if reference is None or amount is None or amount <= 0:
        reasons.append(("liquidity_unknown", "缺少可信成交额，无法估算可成交流动性"))
        liquidity = {"source": "market_snapshot.amount", "state": "unknown"}
    else:
        turnover_shares = amount / reference
        market_capacity = max(0, int(turnover_shares * LIQUIDITY_PARTICIPATION_RATE))
        if depth_qty is not None and depth_qty >= 0:
            market_capacity = min(market_capacity, int(depth_qty))
            liquidity_source = depth_key
        else:
            liquidity_source = "market_snapshot.amount_participation"
        market_capacity = max(0, market_capacity - context.same_day_filled_qty)
        liquidity = {
            "source": liquidity_source,
            "state": "known",
            "observed_amount": amount,
            "turnover_shares_estimate": int(turnover_shares),
            "participation_rate": LIQUIDITY_PARTICIPATION_RATE,
            "already_consumed_qty": context.same_day_filled_qty,
            "max_executable_qty": market_capacity,
        }
    if reasons:
        return _blocked_decision(
            context, [code for code, _ in reasons], [reason for _, reason in reasons],
            session=session, market_state=str(quote_status.get("status") or "unknown"),
            liquidity=liquidity if "liquidity" in locals() else {}, reference_price=reference,
        )
    max_qty = min(context.desired_qty, market_capacity)
    if context.side == "sell":
        max_qty = min(max_qty, max(0, context.sellable_qty or 0))
    odd_lot_close = (
        context.side == "sell" and context.sellable_qty is not None
        and context.desired_qty == context.sellable_qty
        and context.desired_qty > 0 and context.desired_qty % LOT_SIZE != 0
        and max_qty == context.desired_qty
    )
    if context.side == "buy" or (max_qty % LOT_SIZE and not odd_lot_close):
        max_qty = (max_qty // LOT_SIZE) * LOT_SIZE
    if max_qty <= 0:
        return _blocked_decision(
            context, ["no_executable_liquidity"], ["当前可成交流动性不足一个交易单位"],
            session=session, market_state=str(quote_status.get("status") or "unknown"),
            liquidity=liquidity, reference_price=reference,
        )
    price_band_limit = None
    if band and not no_daily_limit:
        price_band_limit = band["limit_up_price"] if context.side == "buy" else band["limit_down_price"]
    if context.order_type == "limit" and context.limit_price is not None:
        if price_band_limit is None:
            price_band_limit = context.limit_price
        elif context.side == "buy":
            price_band_limit = min(price_band_limit, context.limit_price)
        else:
            price_band_limit = max(price_band_limit, context.limit_price)
    terms = PTR.simulated_execution_terms(
        reference, context.side, max_qty,
        limit_price=price_band_limit,
    )
    state = "filled" if max_qty == context.desired_qty else "partially_filled"
    return ExecutionDecision(
        True, state, (), (), context.desired_qty, max_qty, max_qty,
        context.desired_qty - max_qty, reference, terms["fill_price"], terms["amount"],
        terms["fees"], terms["commission"], terms["stamp_duty"], terms["slippage_amount"],
        terms["pricing_basis"], context.as_of, session,
        str(quote_status.get("status") or "unknown"), dict(liquidity),
    )

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


def _ev():
    """惰性取用执行验证闸门（PR-150 消费层 wiring）。"""
    import execution_verification as EV
    return EV


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


def _assert_order_identity(conn, *, order_id, account_id, code, side):
    """下单方传入的 plan/account 必须与**落库的那张订单**讲同一件事（§12）。

    ``execute_order`` 的请求参数来自不同来源：``order_id`` 由调用方给出，
    ``account`` 与 ``plan`` 可能是另一轮扫描里缓存的快照。只要其中任何一个与订单
    行不符，成交就会把 A 的事实记到 B 的账上 —— 扣错账户的现金、消耗错标的的底仓，
    而订单行本身看起来很正常。这类错误不会被下游任何一致性检查发现，因为账本是
    自洽的，只是属于另一笔委托。

    因此身份冲突一律 fail closed，绝不「以调用方为准」继续写。复用订单行作为
    权威：``plan`` 是请求，订单行是已持久化的请求。
    """
    if side not in ("buy", "sell"):
        raise RuntimeError(f"order identity mismatch: unsupported side {side!r}")
    try:
        row = conn.execute(
            "SELECT account_id, code, side, strategy_id, strategy_version, strategy_checksum"
            " FROM paper_orders WHERE id=?",
            (int(order_id),),
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError) as exc:  # pragma: no cover
        raise RuntimeError(f"order identity unreadable for order_id={order_id}") from exc
    if row is None:
        raise RuntimeError(f"order identity mismatch: order_id={order_id} not found")
    if hasattr(row, "keys"):
        stored_account, stored_code, stored_side = (
            row["account_id"], row["code"], row["side"],
        )
        order_strategy_stamp = (
            row["strategy_id"], row["strategy_version"], row["strategy_checksum"],
        )
    else:
        stored_account, stored_code, stored_side = row[0], row[1], row[2]
        order_strategy_stamp = (row[3], row[4], row[5]) if len(row) >= 6 else (None, None, None)
    mismatches = []
    if str(stored_account) != str(account_id):
        mismatches.append(f"account_id stored={stored_account!r} caller={account_id!r}")
    if str(stored_code) != str(code):
        mismatches.append(f"code stored={stored_code!r} caller={code!r}")
    if str(stored_side) != str(side):
        mismatches.append(f"side stored={stored_side!r} caller={side!r}")
    if mismatches:
        raise RuntimeError(
            f"order identity mismatch for order_id={order_id}: " + "; ".join(mismatches)
        )
    if any(value is not None for value in order_strategy_stamp) and any(
        value is None for value in order_strategy_stamp
    ):
        raise RuntimeError(
            f"partial strategy stamp on order_id={order_id}: {order_strategy_stamp!r}"
        )
    return order_strategy_stamp


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return dict(row)
    return {}


def _timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _order_fill_totals(conn, order_id: int) -> tuple[int, float, float]:
    row = conn.execute(
        "SELECT COALESCE(SUM(qty),0),COALESCE(SUM(amount),0),COALESCE(SUM(fees),0) "
        "FROM paper_fills WHERE order_id=?", (int(order_id),),
    ).fetchone()
    return int(row[0] or 0), float(row[1] or 0), float(row[2] or 0)


def _same_day_filled_qty(conn, code: str, day: dt.date, as_of: dt.datetime) -> int:
    rows = conn.execute(
        "SELECT qty,quote_at FROM paper_fills WHERE code=? AND fill_date=?",
        (code, day.isoformat()),
    ).fetchall()
    consumed = 0
    for row in rows:
        quote_at = _timestamp(row[1])
        # Legacy rows without an event time cannot be safely excluded from the
        # cumulative-volume cap, so count them conservatively as already used.
        if quote_at is None or quote_at <= as_of:
            consumed += max(0, int(row[0] or 0))
    return consumed


def _event_key(order_id: int, context: ExecutionContext) -> str:
    identity = {
        "order_id": int(order_id), "as_of": context.as_of,
        "ruleset": EXECUTION_RULESET_VERSION,
        "market_evidence": context.market_evidence_json,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def execute_order(
    conn,
    *,
    account: Mapping[str, Any],
    plan: Mapping[str, Any],
    order_id: int,
    asof_day,
    execution_quote: Mapping[str, Any] | None = None,
    execution_as_of: Any = None,
    order_type: str | None = None,
    limit_price: float | None = None,
    side: str | None = None,
    action: str = "manual_filled",
    risk_log_reason: str | None = None,
    audit_action: str | None = None,
    audit_message: str | None = None,
    reason: str = "手动模拟委托经模型复核后成交",
    detail: Mapping[str, Any] | None = None,
    assumption: str = "本地行情快照按 0.10% 滑点模拟；不代表真实可成交价格",
    is_t_base: bool = True,
    sell_next_take_stage: int | None = None,
):
    """执行唯一决策和账务路径：判定 → 幂等 fill → cash/lot/order/projection。"""
    PT = _pt()
    side = side or str(plan.get("side") or "")
    code = str(plan.get("code") or "")
    account_id = account["id"]
    PT._assert_active_lease(conn, "execution planner decision")

    # ── §12 顺序（不可调换）：lease → provenance → identity → execution-cycle
    #    invariant → 才允许任何 reservation / cash / lot / fill 写 ────────────
    # 成交阶段**绝不**解析「当前 active cycle」。订单的周期是它创建时写下的事实，
    # 一个 cycle 8 建的 pending SELL 在 cycle 9 激活后成交时，必须仍然只碰 cycle 8
    # 的 lot；legacy NULL-cycle 订单的归属**不可证明**，只能 fail closed（既不建
    # 带确定周期的 lot，也不去消费某个周期的底仓）。
    provenance = PT._order_cycle_provenance_for_order(conn, order_id)
    if not provenance.is_proven:
        raise PT.OrderCycleProvenanceUnknown(
            order_id, provenance.status,
            f"{side} 成交被拒绝：订单周期归属不可证明",
        )
    order_strategy_stamp = _assert_order_identity(
        conn, order_id=order_id, account_id=account_id, code=code, side=side,
    )
    order_row = _row_dict(conn.execute(
        "SELECT * FROM paper_orders WHERE id=?", (int(order_id),),
    ).fetchone())
    if not order_row:
        raise RuntimeError(f"order identity mismatch: order_id={order_id} not found")
    order_cycle_id = int(provenance.cycle_id)
    quote = dict(execution_quote or plan.get("execution_quote") or {})
    if not quote and isinstance(detail, Mapping):
        quote = dict(detail.get("quote") or {})
    event_asof = execution_as_of or quote.get("quote_at")
    parsed_asof = _timestamp(event_asof)
    if parsed_asof is not None and parsed_asof.date() < PT._date():
        # Historical replay is tied to the order's frozen cycle/provenance. It
        # must not consult today's mutable active cycle or current account binding.
        current_cycle_guard = False
    else:
        current_cycle_guard = True
    if current_cycle_guard:
        PT._assert_order_execution_cycle(
            conn, order_id, account_id=account_id, provenance=provenance,
            allow_out_of_cycle_account=(side == "sell"),
        )
    stored_status = str(order_row.get("status") or "").lower()
    if EL.canonical_state(stored_status) in EL.TERMINAL_STATES:
        return {
            "order_id": int(order_id), "status": stored_status,
            "desired_qty": int(order_row.get("qty") or 0),
            "filled_qty": _order_fill_totals(conn, order_id)[0],
            "remaining_qty": max(0, int(order_row.get("qty") or 0) - _order_fill_totals(conn, order_id)[0]),
            "event_filled_qty": 0, "realized_pnl": order_row.get("realized_pnl"),
            "execution": {}, "idempotent": True,
        }
    desired_qty = int(order_row.get("qty") or 0)
    if int(plan.get("qty") or 0) != desired_qty:
        raise RuntimeError(
            f"execution quantity mismatch for order_id={order_id}: "
            f"stored={desired_qty} plan={plan.get('qty')}"
        )
    filled_before, _amount_before, _fees_before = _order_fill_totals(conn, order_id)
    remaining_before = max(0, desired_qty - filled_before)
    if remaining_before <= 0:
        return {
            "order_id": int(order_id), "status": "filled", "desired_qty": desired_qty,
            "filled_qty": filled_before, "remaining_qty": 0, "event_filled_qty": 0,
            "execution": {}, "idempotent": True,
        }
    sellable_qty = None
    if side == "sell" and parsed_asof is not None:
        available_row = conn.execute(
            """SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots
               WHERE cycle_id=? AND account_id=? AND code=? AND remaining_qty>0 AND available_date<=?""",
            (order_cycle_id, account_id, code, parsed_asof.date().isoformat()),
        ).fetchone()
        sellable_qty = int(available_row[0] or 0)
    same_day_used = (
        _same_day_filled_qty(conn, code, parsed_asof.date(), parsed_asof)
        if parsed_asof is not None else 0
    )
    context = build_execution_context(
        account_id=account_id, cycle_id=order_cycle_id, code=code, side=side,
        desired_qty=remaining_before, as_of=event_asof, market_evidence=quote,
        order_type=order_type or order_row.get("order_type") or "market",
        limit_price=limit_price if limit_price is not None else order_row.get("planned_price"),
        sellable_qty=sellable_qty, same_day_filled_qty=same_day_used,
    )
    event_key = _event_key(order_id, context)
    existing_event = conn.execute(
        "SELECT id,qty,price,amount,fees FROM paper_fills WHERE execution_event_key=?",
        (event_key,),
    ).fetchone()
    if existing_event is not None:
        filled_total, _, _ = _order_fill_totals(conn, order_id)
        return {
            "order_id": int(order_id), "status": stored_status,
            "desired_qty": desired_qty, "filled_qty": filled_total,
            "remaining_qty": max(0, desired_qty - filled_total),
            "event_filled_qty": int(existing_event[1]), "fill_price": float(existing_event[2]),
            "amount": float(existing_event[3]), "fees": float(existing_event[4]),
            "execution": {}, "idempotent": True,
        }
    current_payload = PT._loads(order_row.get("risk_payload"), {}) or {}
    if current_payload.get("last_execution_event_key") == event_key:
        execution = current_payload.get("execution") or {}
        return {
            "order_id": int(order_id), "status": stored_status,
            "desired_qty": desired_qty, "filled_qty": _order_fill_totals(conn, order_id)[0],
            "remaining_qty": max(0, desired_qty - _order_fill_totals(conn, order_id)[0]),
            "event_filled_qty": 0, "execution": execution, "idempotent": True,
        }
    decision = decide_simulated_execution(context)
    decision_payload = decision.as_dict()
    if not decision.executable_now:
        filled_total, total_amount, total_fees = _order_fill_totals(conn, order_id)
        status = "partially_filled" if filled_total else "pending_execution"
        EL.assert_simulated_transition(stored_status, status)
        current_payload.update({
            "execution": {**decision_payload, "market_evidence": quote},
            "last_execution_event_key": event_key,
        })
        why = "；".join(decision.reasons) or "当前行情条件不满足模拟成交"
        conn.execute(
            """UPDATE paper_orders SET status=?,reason=?,risk_payload=?,filled_price=?,amount=?,fees=?
               WHERE id=?""",
            (status, why, PT._json(current_payload),
             round(total_amount / filled_total, 4) if filled_total else None,
             total_amount if filled_total else None, total_fees if filled_total else None,
             order_id),
        )
        _ev().stamp_order(conn, order_id)
        PT._risk_log(conn, account_id, code, side, "execution_blocked", why, decision_payload,
                     strategy_stamp=order_strategy_stamp)
        PT._audit(conn, account_id, "execution_blocked", f"{side} {code} {desired_qty}股：{why}",
                  strategy_stamp=order_strategy_stamp)
        return {
            "order_id": int(order_id), "status": status, "desired_qty": desired_qty,
            "filled_qty": filled_total, "remaining_qty": max(0, desired_qty - filled_total),
            "event_filled_qty": 0, "execution": decision_payload, "idempotent": False,
        }

    qty = int(decision.filled_qty)
    next_status = "filled" if qty == remaining_before else "partially_filled"
    EL.assert_simulated_transition(stored_status, next_status)
    fill_price = float(decision.fill_price or 0)
    amount = float(decision.amount)
    fees = float(decision.fees)
    realized_pnl = None
    cost_amount = None
    source_lot_id = None
    event_remaining_qty = max(0, remaining_before - qty)
    PT._assert_active_lease(conn, "execution planner ledger mutation")
    if side == "buy":
        # Resize any earlier reservation to the actual partial fill before consuming it.
        ok, reserve_reason = PT._reserve_shared_capital(
            conn, order_id, account_id, code, amount, fees,
            expected_cycle_id=order_cycle_id,
        )
        if not ok:
            raise RuntimeError(reserve_reason or "共享资金池预占失败")
        PT._debit_shared_cash(conn, amount + fees, preferred_account_id=account_id)
        if qty == remaining_before:
            PT._finish_capital_reservation(conn, order_id, "consumed")
        else:
            # 部分成交后，资金预占代表的是剩余委托。保留同一订单的可调整预占，
            # 让后续新行情事件能继续撮合；若当前余额不足，释放预占但保留剩余委托。
            reserve_terms = PTR.simulated_execution_terms(
                decision.reference_price, "buy", event_remaining_qty,
            )
            reserved_next, reserve_next_reason = PT._reserve_shared_capital(
                conn, order_id, account_id, code,
                reserve_terms["amount"], reserve_terms["fees"],
                expected_cycle_id=order_cycle_id,
            )
            if not reserved_next:
                PT._finish_capital_reservation(conn, order_id, "released")
                current_payload["remaining_reservation"] = {
                    "status": "unreserved", "reason": reserve_next_reason,
                }
            else:
                current_payload["remaining_reservation"] = {
                    "status": "reserved", "amount": reserve_terms["amount"],
                    "fees": reserve_terms["fees"],
                }
        source_lot_id = PT._record_lot(
            conn, account, plan, qty, fill_price, parsed_asof.date(), order_id,
            is_t_base=is_t_base, fees=fees, cycle_id=order_cycle_id,
        )
    else:
        consumed, cost_amount = PT._consume_available_lots(
            conn, account_id, code, qty, parsed_asof.date(), cycle_id=order_cycle_id,
        )
        if consumed != qty:
            raise RuntimeError("可卖份额在成交前发生变化，委托已停止")
        realized_pnl = amount - cost_amount - fees
        PT._credit_shared_cash(conn, amount - fees, account_id)
        PPRS.finalize_sell(
            conn, cycle_id=order_cycle_id, account_id=account_id, code=code,
            next_take_stage=sell_next_take_stage,
        )

    fill_detail = dict(detail or plan)
    if side == "sell" and cost_amount is not None:
        fill_detail.setdefault("cost_amount", round(cost_amount, 2))
        fill_detail.setdefault("realized_pnl", round(realized_pnl, 2))
    fill_detail["execution"] = {**decision_payload, "market_evidence": quote}
    fill_detail["last_execution_event_key"] = event_key

    PT._assert_active_lease(conn, "execution planner finalization")
    fill_cursor = conn.execute(
        """INSERT INTO paper_fills(
               order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption,
               execution_event_key,execution_asof,pricing_basis,slippage_amount,execution_evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, account_id, side, code, qty, fill_price, amount, fees,
         parsed_asof.date().isoformat(), context.market_evidence.get("quote_at"),
         f"{assumption}; ruleset={decision.ruleset_version}", event_key,
         decision.as_of, decision.pricing_basis, decision.slippage_amount,
         PT._json({"decision": decision_payload, "market_evidence": quote})),
    )
    if side == "buy" and source_lot_id is not None:
        conn.execute(
            "UPDATE paper_position_lots SET source_fill_id=? WHERE id=?",
            (int(fill_cursor.lastrowid), int(source_lot_id)),
        )
    filled_total, total_amount, total_fees = _order_fill_totals(conn, order_id)
    remaining_qty = max(0, desired_qty - filled_total)
    status = "filled" if remaining_qty == 0 else "partially_filled"
    current_payload.update(fill_detail)
    existing_realized = PT._num(order_row.get("realized_pnl"), 0.0)
    realized_total = existing_realized + (realized_pnl or 0.0) if side == "sell" else None
    conn.execute(
        """UPDATE paper_orders SET filled_price=?,amount=?,fees=?,status=?,reason=?,risk_payload=?,
           realized_pnl=?,executed_at=? WHERE id=?""",
        (round(total_amount / filled_total, 4), total_amount, total_fees, status,
         reason if remaining_qty == 0 else f"部分成交 {filled_total}/{desired_qty} 股，剩余待执行",
         PT._json(current_payload), realized_total, decision.as_of, order_id),
    )
    # 执行验证基于落库后的累计成交事实；部分成交由生命周期/证据契约标成 partial。
    # 执行验证闸门（PR-150 wiring）：**必须在 fill 流水写入之后**盖章，否则
    # evidence_from_order 看不到这条流水，会把一次真实成交记成"没有证据"。
    # 结论本身委托 execution_verification（它再委托 execution_evidence），
    # 这里不重新判断任何成交规则。
    EV = _ev()
    EV.stamp_order(conn, order_id)
    PT._risk_log(
        conn, account_id, code, side, action,
        risk_log_reason or reason, fill_detail,
        strategy_stamp=order_strategy_stamp,
    )
    PT._audit(
        conn, account_id, audit_action or action,
        audit_message or f"{side} {code} {qty}股 @ {fill_price:.2f}",
        strategy_stamp=order_strategy_stamp,
    )
    PT._sync_positions(conn, account_id, parsed_asof.date())
    return {
        "order_id": int(order_id), "status": status, "desired_qty": desired_qty,
        "filled_qty": filled_total, "remaining_qty": remaining_qty,
        "event_filled_qty": qty, "fill_price": fill_price, "amount": amount,
        "fees": fees, "realized_pnl": realized_pnl, "execution": decision_payload,
        "idempotent": False,
    }
