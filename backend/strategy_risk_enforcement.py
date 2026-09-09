# -*- coding: utf-8 -*-
"""PR-30：把编译策略风险画像（PR-03/04）真正接入生产风控参数。

职责
----
PR-70 已经把 ``StrategyRuntimeContext``（fingerprint + risk_profile +
execution_profile）作为策略版本的唯一运行时契约，但生产 sizing / 止损 /
持仓上限 / 加仓仍在消费 ``RISK_PROFILES`` / ``ACCOUNT_SPECS`` 硬编码层。
本模块把两条链路**融合**：

- **帽类参数取更紧**：max_weight / max_exposure / max_industry /
  single_risk(=risk_per_trade) / max_positions 一律取
  ``min(生产现值, 模板值)``——画像只能收紧、永不放宽现网政策；
- **纪律类参数取更紧**：hard_stop 取更深（对负值取 max，即亏损更小）、
  trail_after / trail_stop 取更小（更早/更紧移动止损）、
  hold_max(时间止损) 取更小、max_pyramiding(加仓上限) 取更小；
- **fail-closed**：注册表缺失 / 版本缺失 / 任何异常 → 落到 Composite
  最保守模板（低置信度同口径），绝不因画像解析失败而放宽或跳过；
- **System hard rules 不可触碰**：T+1、证券范围、stale quote、全局 pool
  exposure、系统 drawdown 仍由 paper_trading_rules 全局拥有，本模块只在
  策略软风险层做收紧，输出的审计 payload 不含任何系统规则键。

合并结果统一写入 ``risk["compiled_risk_profile"]`` 审计字段（模板名、
archetype、每个键的 before/after），保证"画像为什么收紧了这笔单"可回放。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping

__all__ = [
    "STRATEGY_RISK_ENFORCEMENT_VERSION",
    "CAP_MERGE_KEYS",
    "DISCIPLINE_MERGE_KEYS",
    "composite_compiled_profile",
    "compiled_profile_for",
    "effective_spec",
    "tighten_caps",
    "tighten_spec",
]

STRATEGY_RISK_ENFORCEMENT_VERSION = "strategy-risk-enforcement-v1"

# 帽类：取 min（画像只能收紧）。
CAP_MERGE_KEYS = ("max_weight", "max_exposure", "max_industry", "single_risk")
# 纪律类：止损更深 / 更早更紧 / 时间止损更短 / 加仓次数更少。
DISCIPLINE_MERGE_KEYS = ("hard_stop", "trail_after", "trail_stop", "max_pyramiding")

# 生产键 → 编译画像键的映射（single_risk 在画像层叫 risk_per_trade）。
_PROFILE_KEY_MAP = {"single_risk": "risk_per_trade"}


def _num(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def composite_compiled_profile() -> dict[str, Any]:
    """最保守兜底：Composite 模板（低置信度 / 解析失败同口径）。"""
    from strategy_risk_profiles import compile_strategy_risk_profile

    profile = compile_strategy_risk_profile("Composite")
    return _flatten(profile, template="Composite", archetype="composite")


def _flatten(profile, *, template: str, archetype: str) -> dict[str, Any]:
    """把 StrategyRiskProfile 展平成执行域参数字典。"""
    soft = profile.soft_limits or {}
    evo = profile.evolvable_params or {}
    return {
        "template": template,
        "archetype": archetype,
        "max_positions": _num(soft.get("max_positions")),
        "max_weight": _num(soft.get("max_weight")),
        "max_exposure": _num(soft.get("max_exposure")),
        "max_industry": _num(soft.get("max_industry")),
        "risk_per_trade": _num(soft.get("risk_per_trade")),
        "hard_stop": _num(evo.get("hard_stop")),
        "trail_after": _num(evo.get("trail_after")),
        "trail_stop": _num(evo.get("trail_stop")),
        "holding_days": _num(evo.get("holding_days")),
        "max_pyramiding": _num(evo.get("max_pyramiding")),
        "version": STRATEGY_RISK_ENFORCEMENT_VERSION,
    }


def compiled_profile_for(conn: sqlite3.Connection, account_id: Any) -> dict[str, Any]:
    """解析某账户/策略的编译风险画像（fail-closed → Composite）。

    ``account_id`` 同时是策略注册表中的 strategy id（PR-70 约定）。注册表
    未收录的策略同样回落 Composite——新策略在拿到可信指纹之前只允许最
    保守的风险表达。
    """
    try:
        import strategy_runtime as SRT

        context = SRT.get_context(conn, str(account_id))
        flattened = _flatten(
            context.risk_profile,
            template=context.risk_profile.template,
            archetype=context.risk_fingerprint.archetype,
        )
        try:
            flattened["execution_ttl_minutes"] = context.execution_profile.get("ttl_minutes")
        except Exception:
            flattened["execution_ttl_minutes"] = None
        return flattened
    except Exception:
        return composite_compiled_profile()


def _tighter_stop(base: Any, compiled: Any) -> float | None:
    """止损深度：负值域内取 max（亏损上限更小 = 更紧）。"""
    base_v, compiled_v = _num(base), _num(compiled)
    if compiled_v is None:
        return base_v
    if base_v is None:
        return compiled_v
    return max(base_v, compiled_v)


def tighten_caps(profile: Mapping[str, Any], compiled: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """帽类 + 纪律类收紧 ``_risk_profile`` 产出，返回 (merged, audit)。"""
    merged = dict(profile)
    audit: dict[str, Any] = {
        "template": compiled.get("template"),
        "archetype": compiled.get("archetype"),
        "version": STRATEGY_RISK_ENFORCEMENT_VERSION,
        "tightened": {},
    }
    for key in CAP_MERGE_KEYS:
        compiled_key = _PROFILE_KEY_MAP.get(key, key)
        compiled_v = _num(compiled.get(compiled_key))
        base_v = _num(merged.get(key))
        if compiled_v is None:
            continue
        if base_v is None or compiled_v < base_v:
            merged[key] = compiled_v
            audit["tightened"][key] = {"before": base_v, "after": compiled_v}
    for key in DISCIPLINE_MERGE_KEYS:
        compiled_v = _num(compiled.get(key))
        base_v = merged.get(key)
        if compiled_v is None:
            continue
        if key == "hard_stop":
            new_v = _tighter_stop(base_v, compiled_v)
        elif key in ("trail_after", "trail_stop", "max_pyramiding"):
            base_num = _num(base_v)
            if compiled_v is None:
                continue
            new_v = compiled_v if base_num is None else min(base_num, compiled_v)
            if key == "max_pyramiding":
                new_v = int(new_v)
        else:  # pragma: no cover - 枚举已穷尽
            continue
        if base_v is None or new_v != _num(base_v):
            merged[key] = new_v
            audit["tightened"][key] = {"before": base_v, "after": new_v}
    return merged, audit


def tighten_spec(spec: Mapping[str, Any], compiled: Mapping[str, Any]) -> dict[str, Any]:
    """把 ACCOUNT_SPECS 层的执行参数按编译画像收紧（纯函数）。"""
    merged = dict(spec or {})
    hard_stop = _tighter_stop(merged.get("hard_stop"), compiled.get("hard_stop"))
    if hard_stop is not None:
        merged["hard_stop"] = hard_stop
    for key in ("trail_after", "trail_stop"):
        compiled_v = _num(compiled.get(key))
        if compiled_v is not None:
            base_v = _num(merged.get(key))
            merged[key] = compiled_v if base_v is None else min(base_v, compiled_v)
    holding_days = _num(compiled.get("holding_days"))
    if holding_days is not None and holding_days > 0:
        hold_max = _num(merged.get("hold_max"))
        merged["hold_max"] = int(holding_days) if hold_max is None else int(min(hold_max, holding_days))
    max_positions = _num(compiled.get("max_positions"))
    if max_positions is not None:
        base_v = _num(merged.get("max_positions"))
        merged["max_positions"] = int(max_positions if base_v is None else min(base_v, max_positions))
    max_pyramiding = _num(compiled.get("max_pyramiding"))
    if max_pyramiding is not None:
        base_v = _num(merged.get("max_pyramiding"))
        merged["max_pyramiding"] = int(max_pyramiding if base_v is None else min(base_v, max_pyramiding))
    return merged


def effective_spec(conn: sqlite3.Connection, account_id: Any, base_spec: Mapping[str, Any]) -> dict[str, Any]:
    """ACCOUNT_SPECS × 编译画像 → 生效执行参数（生产站点直接调用）。"""
    return tighten_spec(base_spec, compiled_profile_for(conn, account_id))
