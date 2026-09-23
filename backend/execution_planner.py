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

import sqlite3
import datetime as dt
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from zoneinfo import ZoneInfo

from collections.abc import Mapping
from typing import Any

import paper_position_risk_state as PPRS
import market_data_contract as MDC
import paper_trading_rules as PTR

__all__ = [
    "EXECUTION_PLANNER_VERSION",
    "ExecutionPolicy",
    "ExecutionContext",
    "ExecutionDecision",
    "ExecutionReason",
    "FillEvent",
    "PersistedOrderIntent",
    "account_risk_gate",
    "capacity_gate",
    "cash_gate",
    "commit_fill",
    "estimate_execution_fees",
    "estimate_execution_terms",
    "estimated_fill_price",
    "evaluate_simulated_execution",
    "market_gate",
    "plan_entry",
    "policy_for",
    "quote_gate",
    "revalidate_order_plan",
    "seat_reserve_gate",
    "security_gate",
]

EXECUTION_PLANNER_VERSION = "execution-planner-v1"
SIMULATION_EXECUTION_RULESET = "a-share-simulation-v1"
SIMULATED_SLIPPAGE_RATE = PTR.SLIPPAGE
MAX_VOLUME_PARTICIPATION = 0.01
_CHINA_TZ = ZoneInfo("Asia/Shanghai")


class ExecutionReason(str, Enum):
    """Stable reason vocabulary emitted by the simulation execution authority."""

    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    MARKET_STALE = "MARKET_STALE"
    MARKET_UNVERIFIED = "MARKET_UNVERIFIED"
    MARKET_DISAGREEMENT = "MARKET_DISAGREEMENT"
    OUT_OF_SESSION = "OUT_OF_SESSION"
    TRADABILITY_UNKNOWN = "TRADABILITY_UNKNOWN"
    SUSPENDED = "SUSPENDED"
    PRICE_LIMIT_LOCKED = "PRICE_LIMIT_LOCKED"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    T1_NOT_SELLABLE = "T1_NOT_SELLABLE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    LIMIT_PRICE_NOT_REACHED = "LIMIT_PRICE_NOT_REACHED"
    CANCELLED = "CANCELLED"
    ORDER_NOT_WORKING = "ORDER_NOT_WORKING"


def estimated_fill_price(reference_price, side):
    """Apply the deterministic simulation slippage policy to a reference price."""
    reference = _positive_number(reference_price)
    normalized_side = str(side or "").lower()
    if reference is None or normalized_side not in {"buy", "sell"}:
        return None
    direction = 1.0 if normalized_side == "buy" else -1.0
    return reference * (1.0 + direction * SIMULATED_SLIPPAGE_RATE)


def estimate_execution_fees(amount, side):
    """Estimate canonical A-share commission and sell stamp duty for a gross amount."""
    normalized_side = str(side or "").lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError(f"unsupported execution side: {side!r}")
    gross = max(0.0, float(amount or 0.0))
    return PTR.commission(gross) + (
        gross * PTR.STAMP_SELL if normalized_side == "sell" else 0.0
    )


def estimate_execution_terms(reference_price, quantity, side, *, limit_price=None):
    """Return a planning estimate; commit_fill still recomputes the actual event."""
    normalized_side = str(side or "").lower()
    price = estimated_fill_price(reference_price, normalized_side)
    if price is None:
        raise ValueError("execution estimate requires a positive reference price and side")
    limit = _positive_number(limit_price)
    if limit is not None:
        price = min(price, limit) if normalized_side == "buy" else max(price, limit)
    amount = max(0, int(quantity or 0)) * price
    return price, amount, estimate_execution_fees(amount, normalized_side)


@dataclass(frozen=True, slots=True)
class PersistedOrderIntent:
    """Immutable persisted order request; distinct from strategy-side ``order_intent``."""

    order_id: int
    account_id: str
    cycle_id: int | None
    strategy_id: str | None
    strategy_version: int | None
    strategy_checksum: str | None
    signal_id: int | None
    symbol: str
    side: str
    desired_quantity: int
    intent_at: str | None
    reference_price: float | None
    order_type: str = "market"
    signal_provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "signal_provenance", _freeze_evidence(self.signal_provenance))


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Frozen market/account/position facts for one simulated execution event."""

    session_date: str
    execution_asof: str | None
    quote: Mapping[str, Any] = field(default_factory=dict)
    market_reading: Any = None
    tradability: Any = None
    session_phase: str | None = None
    available_liquidity: int | None = None
    sellable_quantity: int | None = None
    buying_power: float | None = None
    already_filled_quantity: int = 0
    current_order_status: str = "pending_execution"
    lot_size: int = 100
    participation_rate: float = MAX_VOLUME_PARTICIPATION
    ruleset_version: str = SIMULATION_EXECUTION_RULESET

    def __post_init__(self):
        object.__setattr__(self, "quote", _freeze_evidence(self.quote))
        if self.session_phase is None:
            object.__setattr__(self, "session_phase", _session_phase(self.execution_asof))


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """Auditable outcome for one execution attempt, including zero-fill reasons."""

    executable_now: bool
    fill_quantity: int
    remaining_quantity: int
    status: str
    reasons: tuple[str, ...]
    pricing_basis: str | None
    reference_price: float | None
    fill_price: float | None
    slippage_amount: float
    fees: float
    execution_asof: str | None
    market_evidence: Mapping[str, Any] = field(default_factory=dict)
    tradability_evidence: Mapping[str, Any] = field(default_factory=dict)
    ruleset_version: str = SIMULATION_EXECUTION_RULESET

    def __post_init__(self):
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "market_evidence", _freeze_evidence(self.market_evidence))
        object.__setattr__(self, "tradability_evidence", _freeze_evidence(self.tradability_evidence))


@dataclass(frozen=True, slots=True)
class FillEvent:
    """One deterministic partial or full simulated fill."""

    event_key: str
    order_id: int
    quantity: int
    price: float
    amount: float
    fees: float
    execution_asof: str
    quote_asof: str
    pricing_basis: str
    slippage_amount: float
    execution_evidence: Mapping[str, Any] = field(default_factory=dict)
    ruleset_version: str = SIMULATION_EXECUTION_RULESET

    def __post_init__(self):
        object.__setattr__(self, "execution_evidence", _freeze_evidence(self.execution_evidence))


def _freeze_evidence(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_evidence(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_evidence(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_evidence(item) for item in value)
    return value


def market_reading_for_execution(quote: Mapping[str, Any], *, asof_day, execution_asof):
    """Turn one prefetched quote envelope into an R24 reading; never performs I/O."""
    import signal_service as SIG

    day = MDC.canonical_day(asof_day)
    quote = dict(quote or {})
    evidence = SIG.signal_evidence(
        quote, asof_day=day or "", policy=MDC.EXECUTION_QUOTE_POLICY.name,
    )
    snapshot = MDC.MarketDataSnapshot(
        kind="symbol_quote",
        rows=(quote,) if quote else (),
        as_of=day,
        observed_at=quote.get("quote_at"),
        source=quote.get("quote_source"),
        complete=bool(quote),
        expected_rows=1,
        verification=evidence.verification,
        verification_method=evidence.verification_method,
        verification_detail=evidence.detail,
    )
    return MDC.classify(
        snapshot if quote else None,
        MDC.EXECUTION_QUOTE_POLICY,
        now=execution_asof,
        access_mode=MDC.ACCESS_READ,
        asof_day=day,
    )


def execution_context_from_facts(
    *,
    conn,
    order: Mapping[str, Any],
    quote: Mapping[str, Any],
    asof_day,
    reserved: bool = False,
) -> ExecutionContext:
    """Freeze supplied quote + PIT archive facts for the execution decision.

    ``quote`` must have been fetched before the writer transaction and carry an
    explicit ``execution_asof``. This function only reads the local ledger and
    R24/Tradability contracts.
    """
    import tradability_archive as TA

    row = dict(order or {})
    quote = dict(quote or {})
    day = MDC.canonical_day(asof_day) or ""
    execution_asof = quote.get("execution_asof")
    if execution_asof:
        reading = market_reading_for_execution(
            quote, asof_day=day, execution_asof=execution_asof,
        )
    else:
        reading = MDC.unavailable_reading(
            MDC.EXECUTION_QUOTE_POLICY, MDC.REASON_ASOF_UNPROVABLE,
            access_mode=MDC.ACCESS_READ,
        )
    tradability = TA.tradability_at(
        row.get("code"), day, decision_time=execution_asof,
        repository=TA.TradabilityArchiveRepository(conn),
    )
    cycle_id = row.get("cycle_id")
    sellable = None
    if row.get("side") == "sell" and cycle_id is not None and day:
        try:
            sellable = int(conn.execute(
                """SELECT COALESCE(SUM(remaining_qty),0)
                     FROM paper_position_lots
                    WHERE cycle_id=? AND account_id=? AND code=?
                      AND remaining_qty>0 AND available_date<=?""",
                (int(cycle_id), str(row.get("account_id") or ""),
                 str(row.get("code") or ""), day),
            ).fetchone()[0] or 0)
        except (sqlite3.Error, TypeError, ValueError, IndexError):
            sellable = None
    buying_power = None
    if row.get("side") == "buy":
        PT = _pt()
        if reserved:
            reservation = conn.execute(
                "SELECT amount,fees,status FROM paper_capital_reservations WHERE order_key=?",
                (str(row.get("id")),),
            ).fetchone()
            if reservation is None or str(reservation[2]) != "reserved":
                buying_power = 0.0
            else:
                buying_power = max(0.0, float(reservation[0] or 0) + float(reservation[1] or 0))
        else:
            _, pending = PT._pending_buy_reservations(
                conn, exclude_order_key=str(row.get("id")),
            )
            buying_power = max(0.0, float(PT._shared_cash(conn) or 0) - float(pending or 0))
    price = _positive_number(quote.get("price"))
    amount = _positive_number(quote.get("amount"))
    liquidity = max(0, int(amount / price)) if price and amount else 0
    return ExecutionContext(
        session_date=day,
        execution_asof=str(execution_asof or "") or None,
        quote=quote,
        market_reading=reading,
        tradability=tradability,
        available_liquidity=liquidity,
        sellable_quantity=sellable,
        buying_power=buying_power,
        already_filled_quantity=int(row.get("filled_qty") or 0),
        current_order_status=str(row.get("status") or ""),
    )


def evaluate_simulated_execution(
    intent: PersistedOrderIntent, context: ExecutionContext,
) -> ExecutionDecision:
    """Pure A-share execution rules. No database, provider, or wall-clock reads."""
    reasons: list[str] = []
    side = str(intent.side or "").lower()
    desired = int(intent.desired_quantity or 0)
    already_filled = max(0, int(context.already_filled_quantity or 0))
    remaining = max(0, desired - already_filled)
    quote = context.quote
    reading = context.market_reading
    tradability = context.tradability
    market_projection = reading.projection() if reading is not None else {}
    tradability_projection = (
        tradability.to_dict() if hasattr(tradability, "to_dict") else {}
    )

    if str(context.current_order_status or "").lower() in {
        "cancelled", "rejected", "expired", "filled", "superseded",
    }:
        reasons.append(
            ExecutionReason.CANCELLED.value
            if str(context.current_order_status).lower() == "cancelled"
            else ExecutionReason.ORDER_NOT_WORKING.value
        )
    if side not in {"buy", "sell"} or desired <= 0 or already_filled > desired:
        reasons.append(ExecutionReason.INVALID_QUANTITY.value)
    elif side == "buy" and desired % max(1, int(context.lot_size)):
        reasons.append(ExecutionReason.INVALID_QUANTITY.value)
    elif side == "sell" and desired % max(1, int(context.lot_size)) \
            and desired != int(context.sellable_quantity or 0):
        # Odd-lot sells are allowed only when they liquidate the whole currently
        # sellable remainder. Invalid intents are rejected, never rounded down.
        reasons.append(ExecutionReason.INVALID_QUANTITY.value)

    stamp = _parse_execution_instant(context.execution_asof)
    if stamp is None or stamp.date().isoformat() != str(context.session_date or ""):
        reasons.append(ExecutionReason.MARKET_UNAVAILABLE.value)
    elif context.session_phase not in {"continuous_morning", "continuous_afternoon"}:
        reasons.append(ExecutionReason.OUT_OF_SESSION.value)

    if reading is None or not reading.available:
        reasons.append(ExecutionReason.MARKET_UNAVAILABLE.value)
    else:
        snapshot = reading.snapshot
        if reading.freshness != MDC.FRESHNESS_FRESH:
            reasons.append(ExecutionReason.MARKET_STALE.value)
        elif snapshot is None or snapshot.verification == MDC.VERIFICATION_DISAGREEMENT:
            reasons.append(ExecutionReason.MARKET_DISAGREEMENT.value)
        elif not MDC.is_cross_source_verified(snapshot):
            reasons.append(ExecutionReason.MARKET_UNVERIFIED.value)
        if snapshot is not None and str(snapshot.as_of or "") != str(context.session_date or ""):
            reasons.append(ExecutionReason.MARKET_UNAVAILABLE.value)

    if tradability is None or not bool(getattr(tradability, "evidence_present", False)):
        reasons.append(ExecutionReason.TRADABILITY_UNKNOWN.value)
    else:
        side_allowed = bool(
            getattr(tradability, "can_buy", False) if side == "buy"
            else getattr(tradability, "can_sell", False)
        )
        if not side_allowed:
            block = getattr(
                tradability,
                "buy_block_reason" if side == "buy" else "sell_block_reason",
                None,
            )
            block_code = getattr(block, "value", str(block or "unknown_state"))
            if block_code == "suspended":
                reasons.append(ExecutionReason.SUSPENDED.value)
            elif block_code in {"buy_limit_locked", "sell_limit_locked"}:
                reasons.append(ExecutionReason.PRICE_LIMIT_LOCKED.value)
            else:
                reasons.append(ExecutionReason.TRADABILITY_UNKNOWN.value)

    if side == "sell":
        if context.sellable_quantity is None:
            reasons.append(ExecutionReason.TRADABILITY_UNKNOWN.value)
        elif context.sellable_quantity <= 0:
            reasons.append(ExecutionReason.T1_NOT_SELLABLE.value)

    reference = _positive_number(quote.get("price"))
    if reference is None:
        reasons.append(ExecutionReason.MARKET_UNAVAILABLE.value)
    # Remove duplicates without losing the fixed rule order.
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return ExecutionDecision(
            executable_now=False, fill_quantity=0, remaining_quantity=remaining,
            status=("cancelled" if ExecutionReason.CANCELLED.value in reasons else "pending_execution"),
            reasons=tuple(reasons), pricing_basis=None, reference_price=reference,
            fill_price=None, slippage_amount=0.0, fees=0.0,
            execution_asof=context.execution_asof, market_evidence=market_projection,
            tradability_evidence=tradability_projection,
            ruleset_version=context.ruleset_version,
        )

    if remaining <= 0:
        return ExecutionDecision(
            executable_now=False, fill_quantity=0, remaining_quantity=0,
            status="filled", reasons=(), pricing_basis="already_filled",
            reference_price=reference, fill_price=None, slippage_amount=0.0,
            fees=0.0, execution_asof=context.execution_asof,
            market_evidence=market_projection, tradability_evidence=tradability_projection,
            ruleset_version=context.ruleset_version,
        )

    raw_liquidity = max(0, int(context.available_liquidity or 0))
    if raw_liquidity <= 0:
        reasons.append(ExecutionReason.INSUFFICIENT_LIQUIDITY.value)
        liquidity_qty = 0
    else:
        liquidity_qty = int(raw_liquidity * max(0.0, min(1.0, context.participation_rate)))
    capacity = min(remaining, liquidity_qty)
    if side == "sell":
        capacity = min(capacity, max(0, int(context.sellable_quantity or 0)))
    odd_lot_exit = (
        side == "sell" and context.sellable_quantity == remaining
        and remaining < max(1, int(context.lot_size))
    )
    lot_size = max(1, int(context.lot_size))
    fill_quantity = capacity if odd_lot_exit else (capacity // lot_size) * lot_size
    if fill_quantity < remaining:
        if side == "sell" and (context.sellable_quantity or 0) < remaining:
            reasons.append(ExecutionReason.T1_NOT_SELLABLE.value)
        if liquidity_qty < remaining or fill_quantity < capacity:
            reasons.append(ExecutionReason.INSUFFICIENT_LIQUIDITY.value)
    if fill_quantity <= 0:
        return ExecutionDecision(
            executable_now=False, fill_quantity=0, remaining_quantity=remaining,
            status="pending_execution", reasons=tuple(dict.fromkeys(reasons)),
            pricing_basis="last_verified_quote", reference_price=reference,
            fill_price=None, slippage_amount=0.0, fees=0.0,
            execution_asof=context.execution_asof, market_evidence=market_projection,
            tradability_evidence=tradability_projection,
            ruleset_version=context.ruleset_version,
        )

    fill_price = round(estimated_fill_price(reference, side), 2)
    if fill_price <= 0:
        return ExecutionDecision(
            executable_now=False, fill_quantity=0, remaining_quantity=remaining,
            status="pending_execution", reasons=(ExecutionReason.MARKET_UNAVAILABLE.value,),
            pricing_basis=None, reference_price=reference, fill_price=None,
            slippage_amount=0.0, fees=0.0, execution_asof=context.execution_asof,
            market_evidence=market_projection, tradability_evidence=tradability_projection,
            ruleset_version=context.ruleset_version,
        )
    limit_price = _positive_number(intent.reference_price) if intent.order_type == "limit" else None
    if limit_price is not None and (
        (side == "buy" and fill_price > limit_price)
        or (side == "sell" and fill_price < limit_price)
    ):
        return ExecutionDecision(
            executable_now=False, fill_quantity=0, remaining_quantity=remaining,
            status="pending_execution", reasons=(ExecutionReason.LIMIT_PRICE_NOT_REACHED.value,),
            pricing_basis="limit_price", reference_price=reference, fill_price=None,
            slippage_amount=0.0, fees=0.0, execution_asof=context.execution_asof,
            market_evidence=market_projection, tradability_evidence=tradability_projection,
            ruleset_version=context.ruleset_version,
        )
    amount = round(fill_quantity * fill_price, 2)
    if side == "buy" and context.buying_power is not None:
        per_share_cost = fill_price * (1.0 + PTR.COMMISSION)
        affordable = max(0, int(context.buying_power / per_share_cost)) if per_share_cost else 0
        affordable = (affordable // lot_size) * lot_size
        if affordable < fill_quantity:
            reasons.append(ExecutionReason.INSUFFICIENT_CASH.value)
            fill_quantity = min(fill_quantity, affordable)
            if fill_quantity <= 0:
                return ExecutionDecision(
                    executable_now=False, fill_quantity=0, remaining_quantity=remaining,
                    status="pending_execution", reasons=tuple(dict.fromkeys(reasons)),
                    pricing_basis="verified_quote_plus_deterministic_slippage",
                    reference_price=reference, fill_price=None, slippage_amount=0.0,
                    fees=0.0, execution_asof=context.execution_asof,
                    market_evidence=market_projection,
                    tradability_evidence=tradability_projection,
                    ruleset_version=context.ruleset_version,
                )
            amount = round(fill_quantity * fill_price, 2)
    fees = round(estimate_execution_fees(amount, side), 2)
    return ExecutionDecision(
        executable_now=True, fill_quantity=fill_quantity,
        remaining_quantity=remaining - fill_quantity,
        status="filled" if fill_quantity == remaining else "partially_filled",
        reasons=tuple(dict.fromkeys(reasons)),
        pricing_basis="verified_quote_plus_deterministic_slippage",
        reference_price=reference, fill_price=fill_price,
        slippage_amount=round(fill_price - reference, 4), fees=fees,
        execution_asof=context.execution_asof, market_evidence=market_projection,
        tradability_evidence=tradability_projection,
        ruleset_version=context.ruleset_version,
    )


def _positive_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _parse_execution_instant(value):
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=_CHINA_TZ) if parsed.tzinfo is None else parsed.astimezone(_CHINA_TZ)


def _session_phase(value) -> str:
    """Freeze the coarse Shanghai session phase into each execution context."""
    stamp = _parse_execution_instant(value)
    if stamp is None:
        return "unknown"
    if stamp.weekday() >= 5:
        return "market_closed"
    current = stamp.time().replace(tzinfo=None)
    if current < dt.time(9, 30):
        return "pre_open"
    if current < dt.time(11, 30):
        return "continuous_morning"
    if current < dt.time(13, 0):
        return "lunch_break"
    if current < dt.time(14, 57):
        return "continuous_afternoon"
    if current < dt.time(15, 0):
        return "closing_auction"
    return "market_closed"

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
        row.get("remaining_qty") if row.get("remaining_qty") is not None
        else max(0, int(row.get("qty") or 0) - int(row.get("filled_qty") or 0)),
        (row.get("order_type") or "limit").lower(),
        row.get("planned_price") if (row.get("order_type") or "limit").lower() == "limit" else None,
        asof_day,
        quote=quote,
        exclude_reservation_key=str(row.get("id")) if row.get("id") is not None else None,
        **kwargs,
    )


def _assert_order_identity(conn, *, order_id, account_id, code, side):
    """下单方传入的 plan/account 必须与**落库的那张订单**讲同一件事（§12）。

    ``commit_fill`` 的三个参数来自不同来源：``order_id`` 由调用方给出，
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


def _row_mapping(cursor, row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    columns = [item[0] for item in cursor.description or ()]
    return dict(zip(columns, row, strict=True))


def _plain_evidence(value):
    if isinstance(value, Mapping):
        return {str(key): _plain_evidence(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_evidence(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _decision_evidence(decision: ExecutionDecision) -> dict[str, Any]:
    return {
        "executable_now": decision.executable_now,
        "fill_quantity": decision.fill_quantity,
        "remaining_quantity": decision.remaining_quantity,
        "status": decision.status,
        "reasons": list(decision.reasons),
        "pricing_basis": decision.pricing_basis,
        "reference_price": decision.reference_price,
        "fill_price": decision.fill_price,
        "slippage_amount": decision.slippage_amount,
        "fees": decision.fees,
        "execution_asof": decision.execution_asof,
        "market_evidence": _plain_evidence(decision.market_evidence),
        "tradability_evidence": _plain_evidence(decision.tradability_evidence),
        "ruleset_version": decision.ruleset_version,
    }


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
    sell_next_take_stage: int | None = None,
    execution_context: ExecutionContext | None = None,
):
    """唯一模拟执行与账务提交权威：评估 → 有证据成交 → 原子写入。

    ``plan`` 只携带调用方预先取到的行情事实；最终数量、价格、费用和状态都由
    Execution Simulation Authority 重算。provider 读取绝不能发生在本事务中。
    """
    PT = _pt()
    order_cursor = conn.execute("SELECT * FROM paper_orders WHERE id=?", (int(order_id),))
    order = _row_mapping(order_cursor, order_cursor.fetchone())
    if order is None:
        raise RuntimeError(f"execution order not found: {order_id}")
    # Provenance and identity are both read-only guards; resolve the order's
    # immutable cycle before checking caller-supplied identity, and before any
    # reservation, cash, lot, or fill mutation.
    if account is None:
        requested_account_id = ""
    elif hasattr(account, "keys"):
        requested_account_id = str(account["id"] if "id" in account.keys() else "")
    else:
        requested_account_id = str(account.get("id") or "")
    requested_code = str(plan.get("code") or "")
    side = str(side or plan.get("side") or order.get("side") or "").lower()
    code = str(order.get("code") or "")
    account_id = str(order.get("account_id") or "")
    qty = int(order.get("qty") or 0)
    realized_pnl = None
    cost_amount = None

    PT._assert_active_lease(conn, "execution planner commit")

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
        conn, order_id=order_id, account_id=requested_account_id,
        code=requested_code, side=side,
    )
    # §5 execution-cycle invariant：订单周期 == 账户当前周期 == active 周期。
    # 上层 scanner 已检查过一遍，这里仍然校验（§12 defense in depth）：调用方可能
    # 持有上一轮缓存的 account 快照，账本在两次读之间搬了家。
    order_cycle_id = PT._assert_order_execution_cycle(
        conn, order_id, account_id=account_id, provenance=provenance,
        allow_out_of_cycle_account=(side == "sell"),
    )

    quote = plan.get("execution_quote") or plan.get("quote") or {}
    context = execution_context or execution_context_from_facts(
        conn=conn, order=order, quote=quote, asof_day=asof_day, reserved=reserved,
    )
    intent = PersistedOrderIntent(
        order_id=int(order_id), account_id=account_id,
        cycle_id=order_cycle_id,
        strategy_id=order.get("strategy_id"),
        strategy_version=order.get("strategy_version"),
        strategy_checksum=order.get("strategy_checksum"),
        signal_id=order.get("signal_id"), symbol=code, side=side,
        desired_quantity=qty, intent_at=order.get("created_at"),
        reference_price=_positive_number(order.get("planned_price")),
        order_type=str(order.get("order_type") or "market").lower(),
        signal_provenance={
            "signal_id": order.get("signal_id"),
            "cycle_id": order_cycle_id,
            "strategy_id": order.get("strategy_id"),
            "strategy_version": order.get("strategy_version"),
            "strategy_checksum": order.get("strategy_checksum"),
        },
    )
    decision = evaluate_simulated_execution(intent, context)
    current_status = str(order.get("status") or "")
    current_version = int(order.get("execution_version") or 0)
    execution_evidence = _decision_evidence(decision)
    if not decision.executable_now:
        if current_status not in {"filled", "cancelled", "rejected", "risk_rejected", "manual_rejected", "expired", "superseded"}:
            next_status = decision.status
            if current_status == "partially_filled" and decision.status == "pending_execution":
                next_status = "partially_filled"
            if current_status == "pending_limit" and (
                ExecutionReason.LIMIT_PRICE_NOT_REACHED.value in decision.reasons
            ):
                next_status = "pending_limit"
            reason_codes = ",".join(decision.reasons)
            payload = _plain_evidence(detail if detail is not None else plan)
            payload = payload if isinstance(payload, dict) else {"detail": payload}
            payload["execution"] = execution_evidence
            conn.execute(
                """UPDATE paper_orders
                      SET status=?,reason=?,execution_asof=?,execution_reasons=?,
                          execution_evidence=?,ruleset_version=?,execution_version=execution_version+1,
                          remaining_qty=MAX(0,qty-COALESCE(filled_qty,0))
                    WHERE id=? AND execution_version=?""",
                (next_status, reason_codes or reason, decision.execution_asof,
                 PT._json(list(decision.reasons)), PT._json(execution_evidence),
                 decision.ruleset_version, order_id, current_version),
            )
            PT._risk_log(
                conn, account_id, code, side, "execution_blocked",
                reason_codes or reason, execution_evidence,
                strategy_stamp=order_strategy_stamp,
            )
        return None

    quote_at = str(context.quote.get("quote_at") or "")
    if not quote_at or not decision.execution_asof:
        raise RuntimeError("成交事件缺少明确的 quote_at / execution_asof")
    event_key = hashlib.sha256(
        f"{order_id}|{quote_at}|{decision.ruleset_version}".encode("utf-8")
    ).hexdigest()
    duplicate = conn.execute(
        "SELECT 1 FROM paper_fills WHERE order_id=? AND event_key=? LIMIT 1",
        (int(order_id), event_key),
    ).fetchone()
    if duplicate:
        return None

    fill_aggregate = conn.execute(
        """SELECT COALESCE(SUM(qty),0),COALESCE(SUM(amount),0),COALESCE(SUM(fees),0)
             FROM paper_fills WHERE order_id=?""",
        (int(order_id),),
    ).fetchone()
    prior_qty = int(fill_aggregate[0] or 0)
    prior_amount = float(fill_aggregate[1] or 0.0)
    prior_fees = float(fill_aggregate[2] or 0.0)
    stored_filled = int(order.get("filled_qty") or 0)
    if prior_qty != stored_filled:
        raise RuntimeError(
            f"order/fill quantity mismatch for order_id={order_id}: "
            f"order={stored_filled}, fills={prior_qty}"
        )

    qty = int(decision.fill_quantity)
    fill_price = float(decision.fill_price)
    amount = round(qty * fill_price, 2)
    fees = float(decision.fees)
    total_filled = prior_qty + qty
    remaining_qty = max(0, int(order.get("qty") or 0) - total_filled)
    total_amount = round(prior_amount + amount, 2)
    total_fees = round(prior_fees + fees, 2)
    average_price = round(total_amount / total_filled, 4) if total_filled else None
    next_status = "filled" if remaining_qty == 0 else "partially_filled"
    fill_event = FillEvent(
        event_key=event_key, order_id=int(order_id), quantity=qty,
        price=fill_price, amount=amount, fees=fees,
        execution_asof=str(decision.execution_asof), quote_asof=quote_at,
        pricing_basis=str(decision.pricing_basis or ""),
        slippage_amount=decision.slippage_amount,
        execution_evidence={
            "decision": execution_evidence,
            "market": _plain_evidence(decision.market_evidence),
            "tradability": _plain_evidence(decision.tradability_evidence),
        },
        ruleset_version=decision.ruleset_version,
    )
    fill_detail = _plain_evidence(detail if detail is not None else plan)
    fill_detail = fill_detail if isinstance(fill_detail, dict) else {"detail": fill_detail}
    fill_detail["execution"] = execution_evidence
    fill_detail["fill_event"] = {
        "event_key": fill_event.event_key,
        "quantity": fill_event.quantity,
        "price": fill_event.price,
        "amount": fill_event.amount,
        "fees": fill_event.fees,
        "execution_asof": fill_event.execution_asof,
        "quote_asof": fill_event.quote_asof,
        "ruleset_version": fill_event.ruleset_version,
    }

    PT._assert_active_lease(conn, "execution planner state transition")
    changed = conn.execute(
        """UPDATE paper_orders
              SET status=?,filled_qty=?,remaining_qty=?,filled_price=?,amount=?,fees=?,
                  reason=?,risk_payload=?,execution_asof=?,execution_reasons=?,
                  execution_evidence=?,pricing_basis=?,slippage=?,ruleset_version=?,
                  executed_at=?,execution_version=execution_version+1
            WHERE id=? AND execution_version=?
              AND COALESCE(filled_qty,0)=? AND COALESCE(remaining_qty,qty-COALESCE(filled_qty,0))>=?
              AND status IN ('pending_execution','partially_filled','ready_to_fill',
                             'pending_limit','pending_verification','execution_retry',
                             'manual_execution_retry')""",
        (next_status, total_filled, remaining_qty, average_price, total_amount,
         total_fees, reason, PT._json(fill_detail), decision.execution_asof,
         PT._json(list(decision.reasons)), PT._json(execution_evidence),
         decision.pricing_basis, decision.slippage_amount, decision.ruleset_version,
         decision.execution_asof, order_id, current_version, prior_qty, qty),
    )
    if getattr(changed, "rowcount", 1) != 1:
        raise RuntimeError(f"concurrent or terminal order transition: order_id={order_id}")

    fill_plan = {
        **dict(plan), "side": side, "code": code,
        "name": plan.get("name") or order.get("name"),
        "industry": plan.get("industry"), "qty": qty,
        "fill_price": fill_price, "amount": amount, "fees": fees,
        "quote_at": quote_at,
    }

    if side == "buy":
        if not reserved:
            # Strategy fills can arrive in multiple execution events. Give each
            # event its own short-lived reservation key so a consumed first fill
            # cannot prevent a later event from reserving the remaining cash.
            reservation_key = f"{order_id}:execution:{event_key}"
            # §20–§22：这张订单的周期归属已经在上面证明过，把它传给预占层，
            # 让「预占周期 == 订单周期」也在同一次写入里成立。
            ok, reserve_reason = PT._reserve_shared_capital(
                conn, reservation_key, account_id, code, amount, fees,
                expected_cycle_id=order_cycle_id,
            )
            if not ok:
                raise RuntimeError(reserve_reason or "共享资金池预占失败")
        PT._assert_active_lease(conn, "execution planner cash debit")
        PT._debit_shared_cash(conn, amount + fees, preferred_account_id=account_id)
        if reserved:
            PT._consume_capital_reservation(
                conn, order_id, amount, fees, final=(remaining_qty == 0),
            )
        else:
            PT._finish_capital_reservation(conn, reservation_key, "consumed")
        # §10：lot 的周期**显式**来自来源订单（`_record_lot` 内部同样强制这一点，
        # 这里显式传入，让「订单 cycle == lot cycle」在调用点也读得出来）。
        PT._record_lot(
            conn, account, fill_plan, qty, fill_price, asof_day, order_id,
            is_t_base=is_t_base, fees=fees, cycle_id=order_cycle_id,
            acquired_at=decision.execution_asof,
        )
    else:
        PT._assert_active_lease(conn, "execution planner lot consumption")
        # §9：FIFO 消耗必须绑定**订单自己**的周期，绝不重新解析 active cycle。
        consumed, cost_amount = PT._consume_available_lots(
            conn, account_id, code, qty, asof_day, cycle_id=order_cycle_id,
        )
        if consumed != qty:
            raise RuntimeError("可卖份额在成交前发生变化，委托已停止")
        realized_pnl = amount - cost_amount - fees
        PT._credit_shared_cash(conn, amount - fees, account_id)
        # R14 §17：SELL 的 episode 收尾与 lot 消耗同处一个事务，并直接依赖
        # paper_position_risk_state（不经 paper_trading 转发）。manual / deferred
        # SELL 同样能卖光最后一股，必须关闭 episode，否则权威表残留"看着还活着"
        # 的 peak/take_stage。finalizer 用订单自己的 durable cycle 去权威 lots 查
        # 同周期剩余量，不重解 active cycle；本路径无档位推进事实 ⇒
        # next_take_stage 缺省 None（部分卖出原样保留 stage，绝不重置）。
        PPRS.finalize_sell(
            conn, cycle_id=order_cycle_id, account_id=account_id, code=code,
            next_take_stage=sell_next_take_stage,
        )

    PT._assert_active_lease(conn, "execution planner finalization")
    if side == "sell" and cost_amount is not None:
        fill_detail["cost_amount"] = round(cost_amount, 2)
        fill_detail["realized_pnl"] = round(realized_pnl, 2)
        conn.execute(
            "UPDATE paper_orders SET realized_pnl=COALESCE(realized_pnl,0)+? WHERE id=?",
            (realized_pnl, order_id),
        )
    conn.execute(
        """INSERT INTO paper_fills(
               order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,
               assumption,event_key,execution_asof,pricing_basis,slippage,market_evidence,
               ruleset_version,execution_evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (order_id, account_id, side, code, qty, fill_price, amount, fees,
          PT._date(asof_day).isoformat(), quote_at, assumption, event_key,
          decision.execution_asof, decision.pricing_basis, decision.slippage_amount,
          PT._json(_plain_evidence(decision.market_evidence)), decision.ruleset_version,
          PT._json(_plain_evidence(fill_event.execution_evidence))),
    )
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
    PT._sync_positions(conn, account_id, asof_day)
    return realized_pnl
