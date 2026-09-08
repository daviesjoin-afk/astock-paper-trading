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

import datetime as dt
import sqlite3
from typing import Any, Mapping

__all__ = [
    "ASYMMETRIC_RISK_VERSION",
    "RISK_DIRECTION_KEYS",
    "TIGHTEN_MIN_SAMPLES",
    "EXPANSION_MIN_SAMPLES",
    "OBSERVATION_WINDOW_DAYS",
    "MAX_SINGLE_ROUND_STEP",
    "classify_risk_change",
    "ensure_proposals_registered",
    "observation_days_for",
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


def ensure_proposals_table(conn) -> None:
    """创建风险放大提案登记表（幂等）。

    观察期从**落库的提案登记时间**起算，绝不信任调用方自报的天数——
    否则"提案后立刻自报 10 天"就能绕过整个观察期。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS risk_expansion_proposals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            key TEXT NOT NULL,
            old_value REAL,
            new_value REAL NOT NULL,
            evidence_count INTEGER,
            proposed_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_risk_proposals_lookup
            ON risk_expansion_proposals(strategy_id, key, status, proposed_at DESC);
        """
    )


def _proposal_key_match(conn, strategy_id: str, key: str, new_value: float):
    try:
        ensure_proposals_table(conn)
        row = conn.execute(
            """SELECT id, proposed_at FROM risk_expansion_proposals
                WHERE strategy_id=? AND key=? AND status='pending'
                  AND ABS(new_value-?)<1e-9
                ORDER BY proposed_at DESC LIMIT 1""",
            (str(strategy_id), str(key), float(new_value)),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def ensure_proposals_registered(
    conn,
    current_overrides: Mapping[str, Mapping[str, Any]],
    proposed_overrides: Mapping[str, Mapping[str, Any]],
    *,
    evidence_count: int | None,
) -> list[dict[str, Any]]:
    """把证据达标、但观察期未满的放大意图登记为提案（启动观察时钟）。

    同 strategy+key+目标值已有 pending 提案时不重复登记。返回登记的提案。
    """
    if conn is None or evidence_count is None or evidence_count < EXPANSION_MIN_SAMPLES:
        return []
    ensure_proposals_table(conn)
    now = dt.datetime.now().isoformat(timespec="seconds")
    registered: list[dict[str, Any]] = []
    for strategy_id, proposed in dict(proposed_overrides or {}).items():
        current = dict((current_overrides or {}).get(strategy_id) or {})
        for key, new_raw in dict(proposed or {}).items():
            if key not in RISK_DIRECTION_KEYS:
                continue
            direction = classify_risk_change(key, current.get(key), new_raw)
            if direction != "expand":
                continue
            try:
                new_value = float(new_raw)
                old_value = float(current.get(key))
            except (TypeError, ValueError):
                continue
            step = MAX_SINGLE_ROUND_STEP.get(key)
            if step is not None and (new_value - old_value) > float(step) + 1e-9:
                continue  # 超上限的放大永不登记
            if _proposal_key_match(conn, strategy_id, key, new_value) is not None:
                continue
            conn.execute(
                """INSERT INTO risk_expansion_proposals(
                       strategy_id,key,old_value,new_value,evidence_count,proposed_at)
                   VALUES(?,?,?,?,?,?)""",
                (str(strategy_id), str(key), old_value, new_value,
                 int(evidence_count), now),
            )
            registered.append({
                "strategy_id": str(strategy_id), "key": str(key),
                "new_value": new_value, "proposed_at": now,
            })
    if registered:
        conn.commit()
    return registered


def observation_days_for(conn, strategy_id: str, key: str, new_value: Any) -> int | None:
    """从持久化提案的登记时间推算已观察天数；无提案返回 None。"""
    if conn is None:
        return None
    try:
        new_value_f = float(new_value)
    except (TypeError, ValueError):
        return None
    proposal = _proposal_key_match(conn, strategy_id, key, new_value_f)
    if proposal is None:
        return None
    try:
        proposed_at = dt.datetime.fromisoformat(str(proposal["proposed_at"])[:19])
    except ValueError:
        return None
    return max(0, (dt.datetime.now() - proposed_at).days)


def validate_risk_updates(
    current_overrides: Mapping[str, Mapping[str, Any]],
    proposed_overrides: Mapping[str, Mapping[str, Any]],
    *,
    evidence_count: int | None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """对一组 strategy_overrides 更新执行非对称风险校验。

    - **收紧**：安全方向。人工/运营路径（未提供证据上下文）可直接收紧；
    - **放大**：需要 ``evidence_count >= EXPANSION_MIN_SAMPLES`` 且观察期
      满 10 天——观察期从**持久化的提案登记时间**推算（``conn`` 必须提供，
      调用方自报天数不被信任），且单轮幅度不得超过 ``MAX_SINGLE_ROUND_STEP``；
    - **证据缺失**：放大一律拒绝（fail-closed）；收紧视为运营人工操作放行。

    返回 ``{allowed, violations, expansions, tightenings, registered}``。
    """
    violations: list[str] = []
    expansions: list[dict[str, Any]] = []
    tightenings: list[dict[str, Any]] = []
    window_pending: list[dict[str, Any]] = []
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
            observed = observation_days_for(conn, strategy_id, key, new_value_f) \
                if conn is not None else None
            if observed is None:
                window_pending.append({
                    "strategy_id": strategy_id, "key": key, "new": new_raw,
                })
                violations.append(
                    f"{label} 风险放大尚未登记观察提案：本次已自动登记，"
                    f"观察期 {OBSERVATION_WINDOW_DAYS} 天从现在起算，期满后重试"
                )
                continue
            if observed < OBSERVATION_WINDOW_DAYS:
                violations.append(
                    f"{label} 风险放大的观察期未满"
                    f"（{observed}/{OBSERVATION_WINDOW_DAYS} 天）；"
                    "请等待观察窗口结束后重试"
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
        "window_pending": window_pending,
        "version": ASYMMETRIC_RISK_VERSION,
    }
