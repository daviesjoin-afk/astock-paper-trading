# -*- coding: utf-8 -*-
"""策略级自进化画像（PR：per-strategy evolution profile）。

全局自进化参数（``self_evolution.BOUNDS`` / 默认参数）对所有策略一刀切。
不同策略的风险特征差异很大：日内做T允许更快的参数节奏，质量/主力策略
应该更保守、证据门槛更高。本模块把进化参数升级为 **strategy scoped**：

- **可调参数**（tunable）：允许进化/调整的键；
- **锁定参数**（locked）：任何路径都不许改（防误调）；
- **边界与步长**（bounds）：按策略覆盖全局 BOUNDS（min/max/step）；
- **最小证据量**（min_samples）：触发该策略进化所需的最低样本数
  （全局默认 5，策略画像普遍更严格）。

沿用既有 bounded evolution / audit / rollback 思路：所有调整仍然写
``evolution_params`` 版本链（带 ``strategy_id``）并记 ``evolution_log``；
回滚 = 重新插入上一版参数，不重放、不猜测。未知策略 fail-closed 回落
保守 default 画像。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "STRATEGY_EVOLUTION_VERSION",
    "STRATEGY_EVOLUTION_PROFILES",
    "DEFAULT_PROFILE",
    "apply_strategy_profile",
    "clamp_to_profile",
    "evolution_profile_for",
    "min_evidence_for",
    "validate_strategy_adjustment",
]

STRATEGY_EVOLUTION_VERSION = "strategy-evolution-profiles-v1"

# default：未知策略的保守回落画像。
DEFAULT_PROFILE: dict[str, Any] = {
    "label": "保守默认",
    "tunable": (
        "max_weight_delta", "max_delta_threshold", "hold_bias",
    ),
    "locked": ("confidence_threshold", "consensus_weight_ratio",
               "consensus_direction_threshold"),
    # 覆盖全局 BOUNDS 的更紧边界（step 同步收紧）。
    "bounds": {
        "max_weight_delta": {"min": 0.01, "max": 0.04, "step": 0.002},
        "max_delta_threshold": {"min": 0.001, "max": 0.010, "step": 0.0005},
        "hold_bias": {"min": 0.0, "max": 0.4, "step": 0.02},
    },
    "min_samples": 10,
}

STRATEGY_EVOLUTION_PROFILES: dict[str, dict[str, Any]] = {
    # 短线日内做T：节奏最快，但方向阈值锁死（做T依赖稳定门槛）。
    "tq_breakout": {
        "label": "短线日内做T",
        "tunable": ("max_weight_delta", "max_delta_threshold", "hold_bias",
                    "consensus_weight_ratio"),
        "locked": ("consensus_direction_threshold",),
        "bounds": {
            "max_weight_delta": {"min": 0.01, "max": 0.06, "step": 0.003},
            "max_delta_threshold": {"min": 0.001, "max": 0.015, "step": 0.0005},
            "hold_bias": {"min": 0.0, "max": 0.3, "step": 0.02},
            "consensus_weight_ratio": {"min": 0.45, "max": 0.75, "step": 0.02},
        },
        "min_samples": 10,
    },
    # 趋势波段：参数变化必须慢，hold 倾向只许更保守。
    "trend_pullback": {
        "label": "趋势波段优选",
        "tunable": ("max_weight_delta", "hold_bias"),
        "locked": ("max_delta_threshold", "confidence_threshold",
                   "consensus_direction_threshold"),
        "bounds": {
            "max_weight_delta": {"min": 0.01, "max": 0.04, "step": 0.002},
            "hold_bias": {"min": 0.05, "max": 0.5, "step": 0.02},
        },
        # 单调约束：hold 倾向只允许变得更保守（数值增大），不许放松。
        "monotonic": {"hold_bias": "increase"},
        "min_samples": 8,
    },
    # 板块轮动：热点节奏变化快，允许阈值微调；方向阈值锁定。
    "sector_rotation": {
        "label": "板块轮动先锋",
        "tunable": ("max_weight_delta", "max_delta_threshold", "hold_bias"),
        "locked": ("consensus_direction_threshold", "consensus_weight_ratio"),
        "bounds": {
            "max_weight_delta": {"min": 0.01, "max": 0.05, "step": 0.0025},
            "max_delta_threshold": {"min": 0.001, "max": 0.012, "step": 0.0005},
            "hold_bias": {"min": 0.0, "max": 0.4, "step": 0.02},
        },
        "min_samples": 8,
    },
    # 三日策略（质量）：证据密集型，锁定最多、证据门槛最高。
    "reported_profit_breakout": {
        "label": "三日策略",
        "tunable": ("max_weight_delta", "hold_bias"),
        "locked": ("max_delta_threshold", "confidence_threshold",
                   "consensus_weight_ratio", "consensus_direction_threshold"),
        "bounds": {
            "max_weight_delta": {"min": 0.01, "max": 0.03, "step": 0.0015},
            "hold_bias": {"min": 0.05, "max": 0.4, "step": 0.02},
        },
        "min_samples": 12,
    },
    # 超强主力股：资金行为参数敏感，置信度阈值锁死。
    "main_force_top10": {
        "label": "超强主力股",
        "tunable": ("max_weight_delta", "max_delta_threshold", "hold_bias"),
        "locked": ("confidence_threshold", "consensus_weight_ratio"),
        "bounds": {
            "max_weight_delta": {"min": 0.01, "max": 0.05, "step": 0.0025},
            "max_delta_threshold": {"min": 0.001, "max": 0.012, "step": 0.0005},
            "hold_bias": {"min": 0.0, "max": 0.35, "step": 0.02},
        },
        "min_samples": 12,
    },
}


def evolution_profile_for(strategy_id: Any) -> dict[str, Any]:
    """取策略进化画像；未知策略 fail-closed 回落保守 default。"""
    key = str(strategy_id or "").strip()
    profile = STRATEGY_EVOLUTION_PROFILES.get(key)
    fallback = False
    if profile is None:
        profile = DEFAULT_PROFILE
        fallback = True
    resolved = {
        "strategy_id": key or None,
        "label": profile["label"],
        "tunable": tuple(profile["tunable"]),
        "locked": tuple(profile["locked"]),
        "bounds": {name: dict(bounds) for name, bounds in profile["bounds"].items()},
        "monotonic": {name: direction for name, direction
                      in (profile.get("monotonic") or {}).items()},
        "min_samples": int(profile["min_samples"]),
        "fallback": fallback,
        "version": STRATEGY_EVOLUTION_VERSION,
    }
    return resolved


def min_evidence_for(strategy_id: Any) -> int:
    """触发该策略进化所需的最小证据量（样本数）。"""
    return int(evolution_profile_for(strategy_id)["min_samples"])


def clamp_to_profile(strategy_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """把参数钳制到策略画像边界内；锁定参数原样保留（由调用方拒绝）。"""
    profile = evolution_profile_for(strategy_id)
    clamped = dict(params)
    for key, bounds in profile["bounds"].items():
        if key not in clamped:
            continue
        try:
            value = float(clamped[key])
        except (TypeError, ValueError):
            continue
        clamped[key] = max(float(bounds["min"]), min(float(bounds["max"]), value))
    return clamped


def validate_strategy_adjustment(
    strategy_id: Any,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    evidence_count: int | None = None,
) -> dict[str, Any]:
    """校验一次策略级参数调整，返回 ``{allowed, violations, adjusted}``。

    规则（全部 fail-closed）：
    - 锁定参数不许动（哪怕值相同也只接受"未触碰"）；
    - 不可调/未知参数拒绝；
    - 步长超限拒绝（|新-旧| > step）；
    - 边界外拒绝（不是悄悄夹回边界——那是静默放行）；
    - ``evidence_count`` 低于画像 min_samples 拒绝。
    """
    profile = evolution_profile_for(strategy_id)
    violations: list[str] = []
    adjusted: dict[str, Any] = {}
    tunable = set(profile["tunable"])
    locked = set(profile["locked"])
    for key, value in dict(proposed or {}).items():
        if key in locked:
            violations.append(f"{key} 是锁定参数，不允许调整")
            continue
        if key not in tunable:
            violations.append(f"{key} 不在 {profile['label']} 的可调参数清单中")
            continue
        bounds = profile["bounds"].get(key)
        if bounds is None:
            violations.append(f"{key} 没有策略级边界定义")
            continue
        try:
            new_value = float(value)
            old_value = float(current.get(key, value))
        except (TypeError, ValueError):
            violations.append(f"{key} 的值不是数字")
            continue
        step = float(bounds.get("step", 0.0))
        if abs(new_value - old_value) > step + 1e-12:
            violations.append(
                f"{key} 超出单步步长 {step}（{old_value} → {new_value}）"
            )
            continue
        if not (float(bounds["min"]) - 1e-12 <= new_value <= float(bounds["max"]) + 1e-12):
            violations.append(
                f"{key} 超出策略边界 [{bounds['min']}, {bounds['max']}]"
            )
            continue
        direction = profile["monotonic"].get(key)
        if direction == "increase" and new_value < old_value - 1e-12:
            violations.append(
                f"{key} 只允许单向收紧（{old_value} → {new_value} 是放松）"
            )
            continue
        adjusted[key] = new_value
    if evidence_count is None:
        violations.append("未提供证据样本数（fail-closed）：策略级调整必须携带证据量")
    elif evidence_count < profile["min_samples"]:
        violations.append(
            f"证据不足（{evidence_count}/{profile['min_samples']}），"
            f"{profile['label']}画像要求更多样本"
        )
    return {
        "allowed": not violations,
        "violations": violations,
        "adjusted": adjusted,
        "profile": profile,
        "version": STRATEGY_EVOLUTION_VERSION,
    }


def apply_strategy_profile(strategy_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """校验 + 钳制的一步封装；校验失败抛出 ValueError（调用方记审计）。"""
    profile = evolution_profile_for(strategy_id)
    result = validate_strategy_adjustment(strategy_id, {}, params)
    if not result["allowed"]:
        raise ValueError("；".join(result["violations"]))
    clamped = clamp_to_profile(strategy_id, {**params})
    return {**{key: value for key, value in params.items() if key not in clamped},
            **clamped, "profile": profile["label"]}
