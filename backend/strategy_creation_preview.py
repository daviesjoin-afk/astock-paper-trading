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
import paper_allocation as PA
import strategy_dsl_schema as DSL
import strategy_parameter_schema as SPS
import strategy_risk_fingerprint as SRF
import strategy_risk_profiles as SRP

__all__ = ["strategy_creation_preview"]

# 用户值相对推荐档的方向性阈值（超出即"高风险 override"）。
_HIGH_RISK_KEYS = (
    ("max_positions", "席位", "more"),       # 席位更多 = 更激进
    ("max_weight_pct", "单票权重%", "more"),
    ("max_exposure_pct", "总敞口%", "more"),
    ("hold_max", "最长持有天数", "more"),
    ("hold_min", "最短持有天数", "fewer"),    # 持有更短 = 更激进
)

# 既有体系的风格别名 → 指纹关键词（strong/quality/main_force 等不被
# compile_strategy_risk_fingerprint 识别，需先归一化）。
_STYLE_ALIASES = {
    "strong": "breakout 突破",
    "pullback": "trend pullback 趋势 回踩",
    "sector": "sector rotation 板块 轮动",
    "quality": "earnings report event 财报 公告",
    "main_force": "flow momentum 资金 主力",
}

# 自然中文标签 → 空格分词的指纹关键词（单 token 中文无法匹配）。
_LABEL_NORMALIZATION = {
    "板块轮动": "sector rotation 板块 轮动",
    "趋势回踩": "trend pullback 趋势 回踩 均线",
    "短线日内做t": "breakout realtime 盘中 突破 止损",
    "超强主力股": "flow momentum 资金 主力",
    "三日策略": "earnings report event 财报",
}

# archetype → 持有天数基线（min, max）：hold_min/hold_max 覆盖比较的基准。
_HOLDING_BASELINES = {
    "breakout": (1, 5),
    "trend": (5, 20),
    "mean_reversion": (5, 20),
    "rotation": (3, 7),
    "event_driven": (5, 12),
    "flow_momentum": (1, 3),
    "composite": (3, 10),
}

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
    *,
    dsl_ast: Mapping[str, Any] | None = None,
    pool_capital: float | None = None,
) -> dict[str, Any]:
    """根据草稿配置生成创建预览（指纹 / 推荐画像 / 执行方式 / 边界 / 覆盖）。

    PR-34：携带 ``dsl_ast`` 时走**与生产一致的 StrategyRuntime 编译管线**
    （DSL normalize → Risk Fingerprint → Risk Profile → Execution Profile →
    Parameter Schema），并给出初始 lifecycle（用户策略一律 pilot 起步）与
    按资金池折算的预计资金。DSL 无法解析时 fail-closed 返回保守预览并附
    ``dsl_error``。
    """
    draft = dict(draft or {})
    if dsl_ast is not None:
        return _dsl_preview(draft, risk_profiles, dsl_ast, pool_capital)
    style = str(draft.get("style") or "").strip()
    normalized_style = _LABEL_NORMALIZATION.get(style.lower(), style)
    alias = _STYLE_ALIASES.get(normalized_style.lower())
    if alias:
        normalized_style = f"{normalized_style} {alias}".strip()
    if normalized_style:
        draft["style"] = normalized_style
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
    hold_min_baseline, hold_max_baseline = _HOLDING_BASELINES.get(
        archetype, _HOLDING_BASELINES["composite"])
    recommended_limits = {
        "max_positions": int(recommended.get("max_positions", 3)
                             if recommended else 3),
        "max_weight_pct": round(float(recommended.get("max_weight", 0.32)) * 100, 1)
        if recommended else 32.0,
        "max_exposure_pct": round(float(recommended.get("max_exposure", 0.92)) * 100, 1)
        if recommended else 90.0,
        "hold_min": hold_min_baseline,
        "hold_max": hold_max_baseline,
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


def _dsl_preview(
    draft: Mapping[str, Any],
    risk_profiles: Mapping[str, Mapping[str, Any]],
    dsl_ast: Mapping[str, Any],
    pool_capital: float | None,
) -> dict[str, Any]:
    """PR-34：基于 DSL 的编辑期实时预览（与生产同一条编译管线）。"""
    try:
        normalized = DSL.normalize(dsl_ast)
        dsl_error = None
    except (ValueError, TypeError) as exc:
        normalized = None
        dsl_error = str(exc)
    if normalized is None:
        # fail-closed：解析失败按保守 composite 展示，绝不猜测用户意图。
        base = strategy_creation_preview(draft, risk_profiles)
        base["dsl_valid"] = False
        base["dsl_error"] = dsl_error
        base["engine"] = "strategy-creation-preview-v2"
        return base

    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), Mapping) else None
    fingerprint = SRF.compile_strategy_risk_fingerprint(normalized, metadata)
    archetype = fingerprint.archetype
    execution = EPF.execution_profile_for(archetype)
    profile = SRP.compile_strategy_risk_profile(fingerprint)
    soft = dict(profile.soft_limits)
    recommended_profile_key = _ARCHETYPE_PROFILE.get(archetype, "composite")
    recommended = dict(risk_profiles.get(recommended_profile_key) or {})
    schema = SPS.StrategyParameterSchema.from_dsl(normalized)

    # 用户自建策略上线即试点：小额资金验证后再人工晋升（PR-26 语义）。
    initial_stage = "pilot"
    capital_scale = PA.DEFAULT_STAGE_CAPITAL_SCALE.get(initial_stage, 0.0)
    estimated_capital = None
    if pool_capital is not None:
        try:
            estimated_capital = round(float(pool_capital) * float(capital_scale), 2)
        except (TypeError, ValueError):
            estimated_capital = None

    high_risk_overrides: list[dict[str, Any]] = []
    for item in schema.parameters:
        if item.risk_direction not in ("higher_is_riskier", "lower_is_riskier"):
            continue
        high_risk_overrides.append({
            "key": item.parameter_id,
            "label": f"声明式参数 {item.parameter_id}",
            "user_value": item.value,
            "declared_direction": item.risk_direction,
            "bounds": [item.min, item.max],
            "max_step": item.max_step,
            "min_evidence": item.min_evidence,
            "note": "风险方向参数调整受非对称风险门约束（收紧快行/放大四重门槛）",
        })

    return {
        "engine": "strategy-creation-preview-v2",
        "dsl_valid": True,
        "dsl_error": None,
        "structure_checksum": schema.structure_checksum,
        "risk_fingerprint": fingerprint.to_dict(),
        "risk_profile": {
            "template": profile.template,
            "recommended_profile": recommended_profile_key,
            "recommended_profile_label": (recommended or {}).get(
                "name", recommended_profile_key),
            "hard_rules": dict(profile.hard_rules),
            "soft_limits": {
                "max_positions": soft.get("max_positions"),
                "max_weight_pct": round(float(soft.get("max_weight", 0.0)) * 100, 1),
                "max_exposure_pct": round(float(soft.get("max_exposure", 0.0)) * 100, 1),
                "max_industry_pct": round(float(soft.get("max_industry", 0.0)) * 100, 1),
                "risk_per_trade": soft.get("risk_per_trade"),
            },
            "evolvable_params": dict(profile.evolvable_params),
            "user_locked_params": dict(profile.user_locked_params),
        },
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
        "lifecycle": {
            "initial_stage": initial_stage,
            "stage_label": "试点（pilot）",
            "capital_scale": capital_scale,
            "pool_capital": pool_capital,
            "estimated_capital": estimated_capital,
            "note": "用户策略上线即试点，仅按资金系数的小比例部署；验证后人工晋升 standard",
        },
        "evolution": {
            "tunable": list(schema.editable),
            "locked": list(schema.immutable),
            "min_samples": max(
                (item.min_evidence for item in schema.parameters), default=0),
            "note": "只有可调参数允许自进化；锁定参数任何路径都不能改",
        },
        "parameters": {
            "editable": list(schema.editable),
            "immutable": list(schema.immutable),
            "items": [item.to_dict() for item in schema.parameters],
        },
        "high_risk_overrides": high_risk_overrides,
        "declared_limits": {},
        "fail_closed": False,
    }
