# -*- coding: utf-8 -*-
"""OrderIntent：策略与模拟盘执行器之间的统一订单意图契约。

契约（PR-05）：
- 策略层只允许描述“意图”——买什么、方向、信念强度、紧急程度、数据截止、
  有效期、止损参考与理由；
- 策略层**不允许**直接确定最终下单数量（qty/shares/amount 等字段属于执行器，
  由 :mod:`paper_sizing` 依据现金、风险预算、敞口、行业与整手约束统一计算）；
- 现有五套模拟盘策略（tq_breakout / trend_pullback / sector_rotation /
  reported_profit_breakout / main_force_top10）通过 :func:`order_intent_from_signal`
  兼容适配，适配输出再经 :func:`intent_to_legacy_fields` 还原后与旧行为等价。

本模块是纯函数、无副作用、不依赖执行器，方便离线回归与审计复核。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "ORDER_INTENT_FIELDS",
    "OrderIntent",
    "OrderIntentContractError",
    "intent_to_legacy_fields",
    "order_intent_from_payload",
    "order_intent_from_signal",
    "reject_qty_claims",
]

# 契约字段：策略能表达的“意图”边界。
ORDER_INTENT_FIELDS = (
    "symbol", "side", "strength", "urgency",
    "data_asof", "expires_at", "stop_reference", "reason",
)

# 决定最终数量的字段只属于执行器（paper_sizing）；策略负载中出现即违约。
_QTY_LIKE_KEYS = frozenset({
    "qty", "quantity", "shares", "order_qty", "target_qty",
    "amount", "order_amount", "target_amount", "sizing",
})

_SIDES = frozenset({"buy", "sell"})
_URGENCIES = frozenset({"immediate", "same_session", "next_session", "conservative"})

# 现有五套模拟盘策略的意图画像：紧急程度、止损参考、当日有效期与强度基准。
# 画像只描述“如何把旧 pick 翻译成意图”，不改变任何执行路径（PR-05 等价承诺）。
_STRATEGY_INTENT_PROFILES = {
    "tq_breakout": {"urgency": "immediate", "stop_reference": "price", "expires": "same_day", "strength_ref": 1.0},
    "trend_pullback": {"urgency": "next_session", "stop_reference": "technical", "expires": "next_session", "strength_ref": 1.0},
    "sector_rotation": {"urgency": "same_session", "stop_reference": "technical", "expires": "same_day", "strength_ref": 1.0},
    "reported_profit_breakout": {"urgency": "next_session", "stop_reference": "price", "expires": "next_session", "strength_ref": 1.0},
    "main_force_top10": {"urgency": "same_session", "stop_reference": "price", "expires": "same_day", "strength_ref": 1.0},
}
_DEFAULT_PROFILE = {"urgency": "conservative", "stop_reference": "none", "expires": "next_session", "strength_ref": 1.0}

_SAME_DAY_EXPIRY_TIME = "15:05"
_NEXT_SESSION_EXPIRY_TIME = "09:35"


class OrderIntentContractError(ValueError):
    """策略负载违反 OrderIntent 契约（例如携带数量字段）时抛出。"""


def reject_qty_claims(payload: Any) -> None:
    """递归检查负载；出现任何数量/金额字段立即判定违约。

    数量由执行器按账户现金、风险预算与敞口约束统一计算，策略层给出
    数值即越权，必须显式失败而不是被静默采纳或丢弃。
    """
    if isinstance(payload, Mapping):
        for key, item in payload.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _QTY_LIKE_KEYS:
                raise OrderIntentContractError(
                    f"OrderIntent 契约禁止策略层携带数量/金额字段: {key!r}"
                )
            reject_qty_claims(item)
    elif isinstance(payload, (list, tuple, frozenset, set)):
        for item in payload:
            reject_qty_claims(item)


def _as_date(value: Any) -> dt.date | None:
    text = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if number != number:  # NaN
        return 0.5
    return max(0.0, min(1.0, number))


@dataclass(frozen=True)
class OrderIntent:
    """一条不可变的策略买入/卖出意图。数量永远不在此结构中。"""

    symbol: str
    side: str
    strength: float
    urgency: str
    data_asof: str
    expires_at: str
    stop_reference: str
    reason: str
    strategy_id: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.symbol or "").strip():
            raise OrderIntentContractError("OrderIntent.symbol 不能为空")
        if self.side not in _SIDES:
            raise OrderIntentContractError(f"OrderIntent.side 非法: {self.side!r}")
        try:
            strength = float(self.strength)
        except (TypeError, ValueError):
            raise OrderIntentContractError(f"OrderIntent.strength 非法: {self.strength!r}") from None
        if not 0.0 <= strength <= 1.0 or strength != strength:
            raise OrderIntentContractError(f"OrderIntent.strength 越界: {self.strength!r}")
        if strength != float(self.strength):
            object.__setattr__(self, "strength", strength)
        if self.urgency not in _URGENCIES:
            raise OrderIntentContractError(f"OrderIntent.urgency 非法: {self.urgency!r}")
        asof = _as_date(self.data_asof)
        if asof is None:
            raise OrderIntentContractError(f"OrderIntent.data_asof 非法: {self.data_asof!r}")
        expires = _as_date(self.expires_at)
        if expires is None:
            raise OrderIntentContractError(f"OrderIntent.expires_at 非法: {self.expires_at!r}")
        if expires < asof:
            raise OrderIntentContractError("OrderIntent.expires_at 早于 data_asof")
        reject_qty_claims(self.context)

    def to_payload(self) -> dict[str, Any]:
        """JSON 安全的字典形式（入库/传输用），字段与契约一一对应。"""
        payload = asdict(self)
        payload["context"] = dict(self.context)
        payload["contract_version"] = "order-intent-v1"
        return payload


def order_intent_from_payload(payload: Mapping[str, Any]) -> OrderIntent:
    """从入库的字典形式还原 OrderIntent（校验与构造共用同一套规则）。"""
    known = {name: payload[name] for name in ORDER_INTENT_FIELDS if name in payload}
    known.setdefault("strategy_id", str(payload.get("strategy_id") or ""))
    context = payload.get("context")
    known["context"] = dict(context) if isinstance(context, Mapping) else {}
    return OrderIntent(**known)


def intent_to_legacy_fields(intent: OrderIntent) -> dict[str, Any]:
    """把意图还原为执行器当前读取的旧字段视图（等价性承诺的实现）。

    旧执行路径读取 pick 的 ``code``/``name``/``price``/``score``/``industry``/
    ``entry_model``/``reason`` 等键；适配器把无法映射进契约字段的旧键原样保存在
    ``context`` 中，这里原样吐回，并在顶层补回 ``code``，从而保证
    “旧 signal -> 意图 -> 旧字段”对执行器可见行为等价。
    """
    legacy = dict(intent.context)
    legacy["code"] = intent.symbol
    legacy["strategy_id"] = intent.strategy_id
    legacy["side"] = intent.side
    legacy["urgency"] = intent.urgency
    return legacy


def order_intent_from_signal(
    strategy_id: str,
    signal: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> OrderIntent:
    """把五套策略现有 pick/signal 负载适配为 OrderIntent（兼容 adapter）。

    只读映射、确定性输出：同一输入永远得到同一意图；适配过程不改变任何
    执行路径（旧行为等价）。负载中若出现数量/金额字段则抛出契约异常。
    """
    if not isinstance(signal, Mapping):
        raise OrderIntentContractError("signal 必须是映射")
    reject_qty_claims(signal)

    profile = _STRATEGY_INTENT_PROFILES.get(str(strategy_id or ""), _DEFAULT_PROFILE)
    now = now or dt.datetime.now()
    symbol = str(signal.get("code") or signal.get("symbol") or "").strip()
    if not symbol:
        raise OrderIntentContractError("signal 缺少 code/symbol，无法构造 OrderIntent")

    side = str(signal.get("side") or "buy").strip().lower()
    if side not in _SIDES:
        raise OrderIntentContractError(f"signal.side 非法: {side!r}")

    strength_ref = max(1e-9, float(profile.get("strength_ref") or 1.0))
    strength = _clamp01(_clamp01(signal.get("score")) / strength_ref)

    urgency = str(signal.get("urgency") or profile["urgency"])
    if urgency not in _URGENCIES:
        entry_model = str(signal.get("entry_model") or "")
        urgency = "immediate" if ("实时" in entry_model or "realtime" in entry_model.lower()) else (
            "next_session" if ("收盘" in entry_model or "daily" in entry_model.lower()) else profile["urgency"]
        )

    asof = (
        _as_date(signal.get("asof_date"))
        or _as_date(signal.get("date"))
        or _as_date(signal.get("quote_at"))
        or _as_date(signal.get("trade_date"))
        or now.date()
    )

    if profile["expires"] == "same_day":
        expires_at = f"{asof.isoformat()}T{_SAME_DAY_EXPIRY_TIME}:00"
    else:
        next_session = asof + dt.timedelta(days=1)
        expires_at = f"{next_session.isoformat()}T{_NEXT_SESSION_EXPIRY_TIME}:00"

    stop_reference = str(signal.get("stop_reference") or profile["stop_reference"])
    if stop_reference not in {"price", "atr", "technical", "none"}:
        has_stop = bool(signal.get("stop_loss") or signal.get("hard_stop"))
        has_atr = bool(signal.get("atr") or signal.get("atr14"))
        has_ma = bool(signal.get("ma20") or signal.get("ma60"))
        stop_reference = "price" if has_stop else "atr" if has_atr else "technical" if has_ma else "none"

    reason = str(
        signal.get("reason")
        or signal.get("entry_model")
        or signal.get("desc")
        or f"{strategy_id} 候选"
    )[:300]

    context = {
        key: value for key, value in signal.items()
        if key not in ORDER_INTENT_FIELDS and key not in {"code", "symbol", "side", "urgency", "asof_date", "date"}
    }
    reject_qty_claims(context)

    return OrderIntent(
        symbol=symbol,
        side=side,
        strength=strength,
        urgency=urgency,
        data_asof=asof.isoformat(),
        expires_at=expires_at,
        stop_reference=stop_reference,
        reason=reason,
        strategy_id=str(strategy_id or ""),
        context=context,
    )
