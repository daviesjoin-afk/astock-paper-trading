# -*- coding: utf-8 -*-
"""策略创建预览（PR：strategy creation risk and execution preview）。

创建策略前，把系统**推断**出的风险画像完整展示给用户：

- **Risk Fingerprint**：从草稿配置（风格关键词 / 席位 / 持有天数 / 数据源
  / 止损等）编译的九维风险指纹；证据不足的字段保持 conservative；
- **推荐 Risk Profile**：按 archetype 推荐（breakout/trend/sector/
  core_quality/main_force / 保守 composite）；
- **Execution Profile**：推荐画像对应的执行方式（订单类型/让价/TTL/批量/
  核验）；
- **预计最大仓位/席位**：推荐档的仓位参数，以及用户值是否越出安全边界；
- **可自进化参数**：该画像的 tunable/locked/min_samples；
- **高风险 override**：用户值比推荐档更激进（更大敞口/权重/席位、放松
  hold_bias 等）时逐项列出——明确展示，绝不静默接受。

fail-closed：输入无法解析时返回保守 composite 指纹与空 override 清单。
"""
from __future__ import annotations

from typing import Any, Mapping

import evolution_profiles as EP
import execution_profiles as EPF
import strategy_risk_fingerprint as SRF

__all__ = ["strategy_creation_preview"]

# 用户值相对推荐档的方向性阈值（超出即"高风险 override"）。
_HIGH_RISK_KEYS = (
    ("max_positions", "席位", "more"),       # 席位更多 = 更激进
    ("max_weight_pct", "单票权重%", "more"),
    ("max_exposure_pct", "总敞口%", "more"),
    ("hold_max", "最长持有天数", "more"),
    ("hold_min", "最短持有天数", "fewer"),    # 持有更短 = 更激进
)

# archetype → 推荐的风险画像档（RISK_PROFILES 键）。
_ARCHETYPE_PROFILE = {
    "breakout": "breakout",
    "trend": "trend",
    "mean_reversion": "trend",
    "rotation": "sector",
    "event_driven": "core_quality",
    "flow_momentum": "main_force",
    "composite": "composite",
}


def strategy_creation_preview(
    draft: Mapping[str, Any] | None,
    risk_profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """根据草稿配置生成创建预览（指纹 / 推荐画像 / 执行方式 / 边界 / 覆盖）。"""
    draft = dict(draft or {})
    fingerprint = SRF.compile_strategy_risk_fingerprint(None, draft)
    archetype = fingerprint.archetype
    execution = EPF.execution_profile_for(archetype)
    recommended_profile_key = _ARCHETYPE_PROFILE.get(archetype, "composite")
    recommended = dict(risk_profiles.get(recommended_profile_key) or {})
    # evolution 画像
    evolution = EP.evolution_profile_for(
        _ARCHETYPE_STRATEGY.get(archetype, "unknown_strategy"))
    # 用户申报的席位/权重边界
    declared = {
        "max_positions": draft.get("max_positions"),
        "max_weight_pct": draft.get("max_weight_pct"),
        "max_exposure_pct": draft.get("max_exposure_pct"),
        "hold_max": draft.get("hold_max"),
        "hold_min": draft.get("hold_min"),
    }
    recommended_limits = {
        "max_positions": int(recommended.get("max_positions", 3)
                             if recommended else 3),
        "max_weight_pct": round(float(recommended.get("max_weight", 0.32)) * 100, 1)
        if recommended else 32.0,
        "max_exposure_pct": round(float(recommended.get("max_exposure", 0.92)) * 100, 1)
        if recommended else 90.0,
    }
    high_risk_overrides: list[dict[str, Any]] = []
    accepted: dict[str, Any] = {}
    for key, label, direction in _HIGH_RISK_KEYS:
        user_value = declared.get(key)
        if user_value is None:
            continue
        try:
            user_number = float(user_value)
        except (TypeError, ValueError):
            continue
        recommended_value = recommended_limits.get(key)
        aggressive = (
            user_number > float(recommended_value)
            if direction == "more"
            else (user_number < float(recommended_value)
                  if recommended_value is not None else False)
        )
        entry = {
            "key": key, "label": label,
            "user_value": user_value,
            "recommended_value": recommended_value,
            "high_risk": bool(aggressive),
        }
        if aggressive:
            high_risk_overrides.append(entry)
        accepted[key] = user_number
    return {
        "engine": "strategy-creation-preview-v1",
        "risk_fingerprint": fingerprint.to_dict(),
        "recommended": {
            "risk_profile": recommended_profile_key,
            "risk_profile_label": (recommended or {}).get("name",
                                                          recommended_profile_key),
            "execution_profile": {
                "family": execution.get("family"),
                "label": execution.get("label"),
                "order_type": execution.get("order_type"),
                "limit_offset_pct": execution.get("limit_offset_pct"),
                "ttl_minutes": execution.get("ttl_minutes"),
                "batch": bool(execution.get("batch")),
                "verification_required": bool(execution.get("verification_required")),
                "strict_ttl": bool(execution.get("strict_ttl")),
            },
            "limits": recommended_limits,
        },
        "evolution": {
            "tunable": list(evolution["tunable"]),
            "locked": list(evolution["locked"]),
            "min_samples": evolution["min_samples"],
            "note": "只有可调参数允许自进化；锁定参数任何路径都不能改",
        },
        "declared_limits": accepted,
        "high_risk_overrides": high_risk_overrides,
        "fail_closed": archetype == "composite"
        and fingerprint.entry_urgency == "conservative"
        and fingerprint.holding_horizon == "conservative",
    }


_ARCHETYPE_STRATEGY = {
    "breakout": "tq_breakout",
    "trend": "trend_pullback",
    "rotation": "sector_rotation",
    "event_driven": "reported_profit_breakout",
    "flow_momentum": "main_force_top10",
    "mean_reversion": "trend_pullback",
    "composite": "unknown_strategy",
}
