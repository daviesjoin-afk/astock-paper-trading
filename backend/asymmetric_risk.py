# -*- coding: utf-8 -*-
"""非对称风险进化（PR：asymmetric risk evolution）。

风险参数的调整必须**不对称**：

- **收紧**（降 exposure / 降 weight / 减席位）永远是安全方向 → 低证据门槛
  即可立即生效；
- **放大**（升 exposure / 升 weight / 加席位）必须满足三重门槛，缺一不可：
  1. 更严格的证据量（≥ ``EXPANSION_MIN_SAMPLES``，远高于收紧门槛）；
  2. 单轮幅度硬上限（``MAX_SINGLE_ROUND_STEP``），任何超额一律拒绝；
  3. **观察期**：提案后必须经过 ``OBSERVATION_WINDOW_DAYS`` 天观察窗口才
     允许生效——窗口未满只能"登记提案"，不能改运行参数。

禁止单轮大幅扩大 strategy exposure、risk-per-trade（单票权重）或 position
cap 的不变式由 :func:`validate_risk_updates` 统一执行。证据缺失时按
fail-closed 处理：只允许收紧。
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "ASYMMETRIC_RISK_VERSION",
    "RISK_DIRECTION_KEYS",
    "TIGHTEN_MIN_SAMPLES",
    "EXPANSION_MIN_SAMPLES",
    "OBSERVATION_WINDOW_DAYS",
    "MAX_SINGLE_ROUND_STEP",
    "classify_risk_change",
    "validate_risk_updates",
]

ASYMMETRIC_RISK_VERSION = "asymmetric-risk-evolution-v1"

# 有方向语义的风险参数（strategy_overrides 的子集）。
RISK_DIRECTION_KEYS = ("max_exposure_pct", "max_weight_pct", "max_positions")

# 收紧：安全方向，低证据门槛。
TIGHTEN_MIN_SAMPLES = 5
# 放大：必须显著更多证据。
EXPANSION_MIN_SAMPLES = 20
# 放大必须经过的观察期（自然日）。
OBSERVATION_WINDOW_DAYS = 10

# 单轮放大幅度硬上限（超出即拒绝，不是夹回边界）。
MAX_SINGLE_ROUND_STEP = {
    "max_exposure_pct": 3.0,   # 最多 +3 个百分点
    "max_weight_pct": 3.0,     # 最多 +3 个百分点
    "max_positions": 1,        # 最多 +1 席
}

DIRECTION_LABELS = {
    "tighten": "风险收紧",
    "expand": "风险放大",
    "none": "无方向变化",
}


def classify_risk_change(key: str, old: Any, new: Any) -> str:
    """判定一次参数变化的方向：tighten / expand / none。"""
    name = str(key or "")
    try:
        old_value = float(old)
        new_value = float(new)
    except (TypeError, ValueError):
        return "none"
    if abs(new_value - old_value) < 1e-9:
        return "none"
    if name == "max_positions":
        return "expand" if new_value > old_value else "tighten"
    # 百分比类：数值增大 = 敞口放大。
    return "expand" if new_value > old_value else "tighten"


def validate_risk_updates(
    current_overrides: Mapping[str, Mapping[str, Any]],
    proposed_overrides: Mapping[str, Mapping[str, Any]],
    *,
    evidence_count: int | None,
    observation_days: int | None = None,
) -> dict[str, Any]:
    """对一组 strategy_overrides 更新执行非对称风险校验。

    - **收紧**：安全方向。人工/运营路径（未提供证据上下文）可直接收紧；
    - **放大**：需要 ``evidence_count >= EXPANSION_MIN_SAMPLES`` 且
      ``observation_days >= OBSERVATION_WINDOW_DAYS``（观察期必须从提案登记
      起算满），且单轮幅度不得超过 ``MAX_SINGLE_ROUND_STEP``；
    - **证据缺失**：放大一律拒绝（fail-closed）；收紧视为运营人工操作放行。

    返回 ``{allowed, violations, expansions, tightenings}``。
    """
    violations: list[str] = []
    expansions: list[dict[str, Any]] = []
    tightenings: list[dict[str, Any]] = []
    for strategy_id, proposed in dict(proposed_overrides or {}).items():
        current = dict((current_overrides or {}).get(strategy_id) or {})
        for key, new_raw in dict(proposed or {}).items():
            if key not in RISK_DIRECTION_KEYS:
                continue
            old_value = current.get(key)
            direction = classify_risk_change(key, old_value, new_raw)
            if direction == "none":
                continue
            label = f"{strategy_id}.{key}"
            if direction == "tighten":
                if evidence_count is not None and evidence_count < TIGHTEN_MIN_SAMPLES:
                    violations.append(
                        f"{label} 风险收紧证据不足（{evidence_count}/{TIGHTEN_MIN_SAMPLES}）"
                    )
                    continue
                tightenings.append({
                    "strategy_id": strategy_id, "key": key,
                    "old": old_value, "new": new_raw,
                })
                continue
            # ---- 放大：三重门槛 ----
            try:
                old_value_f = float(old_value)
                new_value_f = float(new_raw)
            except (TypeError, ValueError):
                violations.append(f"{label} 的值不是数字")
                continue
            step = MAX_SINGLE_ROUND_STEP.get(key)
            if step is not None and (new_value_f - old_value_f) > float(step) + 1e-9:
                violations.append(
                    f"{label} 单轮放大超过硬上限 +{step}"
                    f"（{old_value_f} → {new_value_f}），禁止单轮大幅扩大风险"
                )
                continue
            if evidence_count is None:
                violations.append(
                    f"{label} 风险放大必须提供证据样本数"
                    f"（≥{EXPANSION_MIN_SAMPLES}）并经过观察期"
                )
                continue
            if evidence_count < EXPANSION_MIN_SAMPLES:
                violations.append(
                    f"{label} 风险放大证据不足"
                    f"（{evidence_count}/{EXPANSION_MIN_SAMPLES}）；"
                    f"放大需要比收紧（{TIGHTEN_MIN_SAMPLES}）更严格的证据"
                )
                continue
            observed = 0 if observation_days is None else int(observation_days)
            if observed < OBSERVATION_WINDOW_DAYS:
                violations.append(
                    f"{label} 风险放大的观察期未满"
                    f"（{observed}/{OBSERVATION_WINDOW_DAYS} 天）；"
                    "请先登记提案并等待观察窗口结束"
                )
                continue
            expansions.append({
                "strategy_id": strategy_id, "key": key,
                "old": old_value, "new": new_raw,
                "evidence_count": evidence_count,
                "observation_days": observed,
            })
    return {
        "allowed": not violations,
        "violations": violations,
        "expansions": expansions,
        "tightenings": tightenings,
        "version": ASYMMETRIC_RISK_VERSION,
    }
