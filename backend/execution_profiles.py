# -*- coding: utf-8 -*-
r"""策略执行画像（PR-10）：按风险指纹/画像族自动选择执行方式。

设计
----
- 输入可以是：风险画像族字符串（`breakout`/`trend`/`sector`/...）、
  :class:`strategy_risk_fingerprint.StrategyRiskFingerprint`（用其
  archetype），或 `ACCOUNT_SPECS` 里每个账户声明的 `risk_profile`。
  :class:\`strategy_risk_fingerprint.StrategyRiskFingerprint\`（用其
  archetype），或 \`ACCOUNT_SPECS\` 里每个账户声明的 \`risk_profile\`。
  别名表把各口径收敛到七个画像族。
- 七个画像：breakout 快速（市价）、trend 中速（限价小让价）、
  mean_reversion 被动限价（限价不让价）、rotation batch（批量窗口）、
  event 核验（核验后执行）、flow strict TTL（严格时限）、composite
  保守（限价 + 宽 TTL）。
- **第一版只用现有 market/limit 两种订单类型**，不新增复杂订单类型；
  TTL / batch / verification 目前作为订单审计字段与延期依据，具体的
  批量撮合与人工核验流程由既有 retry/waitlist 机制承担。
- 未知画像族 fail-closed 回落到 composite（保守）。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "EXECUTION_PROFILE_VERSION",
    "EXECUTION_PROFILES",
    "PROFILE_FAMILY_ALIASES",
    "enforce_entry_limit",
    "execution_profile_for",
    "limit_price_for",
]

EXECUTION_PROFILE_VERSION = "execution-profiles-v1"

# 各画像族允许的订单类型只有两种（PR-10 第一版边界）。
_ALLOWED_ORDER_TYPES = frozenset({"market", "limit"})

EXECUTION_PROFILES: dict[str, dict[str, Any]] = {
    "breakout": {
        "label": "突破快速",
        "urgency": "fast",
        "order_type": "market",
        "limit_offset_pct": None,
        "ttl_minutes": None,
        "batch": False,
        "verification_required": False,
        "strict_ttl": False,
    },
    "trend": {
        "label": "趋势中速",
        "urgency": "medium",
        "order_type": "limit",
        "limit_offset_pct": 0.2,
        "ttl_minutes": 30,
        "batch": False,
        "verification_required": False,
        "strict_ttl": False,
    },
    "mean_reversion": {
        "label": "均值回归被动限价",
        "urgency": "passive",
        "order_type": "limit",
        "limit_offset_pct": 0.0,
        "ttl_minutes": 60,
        "batch": False,
        "verification_required": False,
        "strict_ttl": False,
    },
    "rotation": {
        "label": "轮动批量",
        "urgency": "batch",
        "order_type": "limit",
        "limit_offset_pct": 0.1,
        "ttl_minutes": 45,
        "batch": True,
        "verification_required": False,
        "strict_ttl": False,
    },
    "event_driven": {
        "label": "事件核验",
        "urgency": "verified",
        "order_type": "limit",
        "limit_offset_pct": 0.1,
        "ttl_minutes": None,
        "batch": False,
        "verification_required": True,
        "strict_ttl": False,
    },
    "flow_momentum": {
        "label": "资金流严格时限",
        "urgency": "strict_ttl",
        "order_type": "market",
        "limit_offset_pct": None,
        "ttl_minutes": 5,
        "batch": False,
        "verification_required": False,
        "strict_ttl": True,
    },
    "composite": {
        "label": "保守组合",
        "urgency": "conservative",
        "order_type": "limit",
        "limit_offset_pct": 0.1,
        "ttl_minutes": 45,
        "batch": False,
        "verification_required": False,
        "strict_ttl": False,
    },
}

# 各口径画像名 → 七个标准画像族（含 ACCOUNT_SPECS 的 risk_profile 取值与
# strategy_risk_fingerprint 的 archetype 取值）。
PROFILE_FAMILY_ALIASES = {
    "breakout": "breakout",
    "momentum": "breakout",
    "trend": "trend",
    "mean_reversion": "mean_reversion",
    "meanreversion": "mean_reversion",
    "reversion": "mean_reversion",
    "rotation": "rotation",
    "sector": "rotation",
    "event_driven": "event_driven",
    "event": "event_driven",
    "core_quality": "event_driven",
    "flow_momentum": "flow_momentum",
    "flow": "flow_momentum",
    "main_force": "flow_momentum",
    "composite": "composite",
}


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _fingerprint_archetype(candidate: Any) -> str:
    archetype = getattr(candidate, "archetype", None)
    return _normalize(archetype) if archetype is not None else ""


def execution_profile_for(profile: Any) -> dict[str, Any]:
    """按画像族 / 风险指纹 / 账户 risk_profile 取执行画像。

    未知输入 fail-closed 回落 composite（保守）。返回的 dict 自带
    ``family``（归一化画像族）与 ``version``（引擎版本）。
    """
    if isinstance(profile, Mapping):
        profile = profile.get("archetype") or profile.get("risk_profile") or profile
    archetype = _fingerprint_archetype(profile) or _normalize(profile)
    family = PROFILE_FAMILY_ALIASES.get(archetype, "composite")
    resolved = dict(EXECUTION_PROFILES[family])
    if family == "composite" and archetype and archetype not in PROFILE_FAMILY_ALIASES:
        # 未识别的画像名：保守回落并显式标记，避免静默误配。
        resolved["fallback_from"] = archetype
    resolved["family"] = family
    resolved["version"] = EXECUTION_PROFILE_VERSION
    return resolved


def limit_price_for(profile: Mapping[str, Any], reference_price, *, side: str = "buy") -> float | None:
    """按画像让价计算限价；市价画像返回 None（无限价概念）。"""
    try:
        price = float(reference_price or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0.0 or str(profile.get("order_type")) != "limit":
        return None
    offset = profile.get("limit_offset_pct")
    offset_pct = max(0.0, float(offset)) if offset is not None else 0.0
    factor = 1.0 + offset_pct / 100.0 if str(side).lower() == "buy" else 1.0 - offset_pct / 100.0
    return round(price * factor, 2)


def enforce_entry_limit(
    profile: Mapping[str, Any],
    fill_price,
    *,
    reference_price,
    side: str = "buy",
) -> dict[str, Any]:
    """入场限价门禁：限价锚定参考价（如信号收盘价），现价越界则延期。

    市价画像恒通过。限价画像：`limit = reference × (1 ± offset)`，
    若 `fill_price` 不优于限价（买入时更高），调用方应把订单延期
    （`execution_retry`），而不是按更差的价格成交。
    """
    try:
        price = float(fill_price or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    limit_price = limit_price_for(profile, reference_price, side=side)
    if limit_price is None:
        return {"allowed": True, "limit_price": None, "reason": None,
                "order_type": str(profile.get("order_type") or "market"),
                "version": EXECUTION_PROFILE_VERSION}
    allowed = 0.0 < price <= limit_price + 1e-9
    return {
        "allowed": allowed,
        "limit_price": limit_price,
        "reason": None if allowed else (
            f"{profile.get('label', profile.get('family', 'execution'))}画像限价未到："
            f"限价 {limit_price:.2f} 低于现价 {price:.2f}，等待下一执行窗口"
        ),
        "order_type": "limit",
        "version": EXECUTION_PROFILE_VERSION,
    }
