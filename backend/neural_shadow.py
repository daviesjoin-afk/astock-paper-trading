# -*- coding: utf-8 -*-
"""Read-only admission gate for neural-network research."""
from __future__ import annotations

import learning_dataset

NEURAL_SHADOW_VERSION = "neural-shadow-v3-dataset-gated"
NEURAL_CONTROL_VERSION = "neural-control-v2"
# ─── 加速模式参数 ───
# 原始值: MIN_PROFILE_DAYS=60, MIN_LABEL_DATES=40, MIN_LABEL_ROWS=20000
# 加速后: 降低门槛，让更多数据量级可以开始影子训练
MIN_PROFILE_DAYS = 25          # 原60 → 25，约5个交易周即可开始
MIN_LABEL_DATES = 15           # 原40 → 15，约3个交易周
MIN_LABEL_ROWS = 5_000         # 原20000 → 5000，降低数据量门槛
REQUIRED_HORIZONS = (1, 3, 5)
# 数据集契约门禁的读取上限：保证概览读路径不被大表拖慢；一旦触顶即视为
# "无法证明整库完整性"，按 fail-closed 处理（不会因为截断而误判为 ready）。
DATASET_GATE_MAX_EVIDENCE_ROWS = 40_000

# 加速模式配置键
ACCELERATED_MODE_KEY = "neural_accelerated_mode"


def approval_state(conn):
    """Return the persisted neural gate without changing trading state.

    The model is deliberately fail-closed: a missing config row, an
    untrained model, or a non-admitted readiness report can only produce a
    shadow result.  ``approved`` is an explicit human action and is never
    inferred from sample counts.
    """
    try:
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT key,value FROM adaptive_config WHERE key IN "
                "('neural_network_approved','neural_shadow_enabled','neural_accelerated_mode')"
            ).fetchall()
        }
    except Exception:
        rows = {}
    def as_bool(value):
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "approved": as_bool(rows.get("neural_network_approved")),
        "shadow_enabled": as_bool(rows.get("neural_shadow_enabled", "1")),
        "accelerated_mode": as_bool(rows.get("neural_accelerated_mode", "1")),
        "version": NEURAL_CONTROL_VERSION,
    }


def control_status(conn):
    """Combine readiness and approval into a UI/audit-safe status object."""
    ready = readiness(conn)
    approval = approval_state(conn)
    admitted = bool(ready.get("admitted"))
    approved = bool(approval.get("approved"))
    accelerated = bool(approval.get("accelerated_mode"))
    if not approval["shadow_enabled"]:
        status = "disabled"
    elif approved and admitted:
        status = "approved_bounded_shadow"
    elif approved and not admitted:
        status = "approval_waiting_data"
    else:
        status = "shadow_only"
    return {
        "version": NEURAL_CONTROL_VERSION,
        "architecture": "MLP候选评分接口（Transformer仅研究）",
        "status": status,
        "mode": "shadow_only",
        "trading_impact": "none",
        "approved": approved,
        "shadow_enabled": bool(approval["shadow_enabled"]),
        "accelerated_mode": accelerated,
        "readiness": ready,
        "hard_gates_unchanged": True,
        "max_rank_adjustment": 0.05 if status == "approved_bounded_shadow" else 0.0,
        "not_used": ["直接下单", "绕过行情双源", "绕过板块权限", "放宽仓位/T+1/风控"],
    }


def _count(conn, sql, args=()):
    try:
        return int(conn.execute(sql, args).fetchone()[0] or 0)
    except Exception:
        return 0


def _dataset_gate(conn):
    """PR-8：把学习数据集契约接进 readiness，失败即 fail closed。

    行数达标不等于科研就绪：这里额外要求 PIT 合同成立、标签已成熟、可按时间
    切分、三个分区都非空、purge 后无时序重叠、且 fingerprint 成功产出。
    任一条不满足：``admitted = False``，但 ``mode`` 仍是 ``shadow_only``、
    ``trading_impact`` 仍是 ``none`` —— 绝不影响任何交易路径。
    """
    try:
        return learning_dataset.contract_status(
            conn, max_evidence_rows=DATASET_GATE_MAX_EVIDENCE_ROWS
        )
    except Exception:
        # 契约层自身异常也必须表现为"未就绪"，而不是"默认就绪"。
        return {
            "contract_version": learning_dataset.CONTRACT_VERSION,
            "dataset_fingerprint": None,
            "pit_eligible_rows": 0,
            "split_rows": {name: 0 for name in learning_dataset.PARTITIONS},
            "dataset_blockers": ["dataset_contract_error"],
            "exclusion_reasons": {},
        }


def readiness(conn):
    """Return a conservative training gate; never changes trading behavior."""
    profile_days = _count(conn, "SELECT COUNT(DISTINCT profile_date) FROM adaptive_alpha_samples")
    feature_rows = _count(conn, "SELECT COUNT(*) FROM adaptive_alpha_samples")
    label_rows = _count(conn, "SELECT COUNT(*) FROM adaptive_alpha_returns")
    label_dates = _count(conn, "SELECT COUNT(DISTINCT start_date) FROM adaptive_alpha_returns")
    horizons = [int(row[0]) for row in conn.execute(
        "SELECT DISTINCT horizon FROM adaptive_alpha_returns ORDER BY horizon"
    ).fetchall()]
    available_horizons = [value for value in REQUIRED_HORIZONS if value in horizons]
    blockers = []
    if profile_days < MIN_PROFILE_DAYS:
        blockers.append(f"独立盘面日 {profile_days}/{MIN_PROFILE_DAYS}")
    if label_dates < MIN_LABEL_DATES:
        blockers.append(f"完成标签日 {label_dates}/{MIN_LABEL_DATES}")
    if label_rows < MIN_LABEL_ROWS:
        blockers.append(f"完成标签 {label_rows}/{MIN_LABEL_ROWS}")
    if len(available_horizons) != len(REQUIRED_HORIZONS):
        blockers.append("1/3/5日标签不完整")
    dataset_contract = _dataset_gate(conn)
    dataset_blockers = list(dataset_contract.get("dataset_blockers") or [])
    # 行数达标只是必要条件；数据集契约不通过同样不能算科研就绪。
    blockers.extend(f"数据集契约未通过：{code}" for code in dataset_blockers)
    admitted = not blockers
    # 计算各维度的完成进度百分比
    progress = {
        "profile_days_pct": round(min(profile_days / max(MIN_PROFILE_DAYS, 1), 1.0) * 100, 1),
        "label_dates_pct": round(min(label_dates / max(MIN_LABEL_DATES, 1), 1.0) * 100, 1),
        "label_rows_pct": round(min(label_rows / max(MIN_LABEL_ROWS, 1), 1.0) * 100, 1),
        "horizons_pct": round(len(available_horizons) / max(len(REQUIRED_HORIZONS), 1) * 100, 1),
        "overall_pct": round(
            (min(profile_days / max(MIN_PROFILE_DAYS, 1), 1.0)
             + min(label_dates / max(MIN_LABEL_DATES, 1), 1.0)
             + min(label_rows / max(MIN_LABEL_ROWS, 1), 1.0)
             + len(available_horizons) / max(len(REQUIRED_HORIZONS), 1)) / 4 * 100, 1
        ),
    }
    return {
        "version": NEURAL_SHADOW_VERSION,
        "mode": "shadow_only",
        "trading_impact": "none",
        "architecture": "小型多层感知器（MLP）候选评分",
        "accelerated": True,
        "acceleration_note": "v2加速模式：profile_days 60→25, label_dates 40→15, label_rows 20000→5000",
        "not_used": ["Transformer", "自动下单", "自动放宽风控"],
        "status": "ready_for_offline_shadow_training" if admitted else "waiting_data",
        "admitted": admitted,
        "feature_rows": feature_rows,
        "label_rows": label_rows,
        "profile_days": profile_days,
        "label_dates": label_dates,
        "available_horizons": available_horizons,
        "progress": progress,
        "requirements": {
            "min_profile_days": MIN_PROFILE_DAYS,
            "min_label_dates": MIN_LABEL_DATES,
            "min_label_rows": MIN_LABEL_ROWS,
            "required_horizons": list(REQUIRED_HORIZONS),
        },
        "blockers": blockers,
        # ── PR-8 追加字段（只增不改：上面既有字段原样保留，UI/API 兼容）──
        "dataset_contract": dataset_contract.get("contract_version"),
        "dataset_cutoff": dataset_contract.get("cutoff"),
        "dataset_fingerprint": dataset_contract.get("dataset_fingerprint"),
        "pit_eligible_rows": int(dataset_contract.get("pit_eligible_rows") or 0),
        "split_rows": dict(dataset_contract.get("split_rows") or {}),
        "dataset_exclusions": dict(dataset_contract.get("exclusion_reasons") or {}),
        "horizon_semantics": dataset_contract.get("horizon_semantics"),
        "dataset_blockers": dataset_blockers,
        "execution_authority": "none",
        "protocol": [
            "只使用点时可见特征与已完成的1/3/5日标签；可用性无法证明的行一律排除",
            "缺失/未知一律不填空：不做 0/均值/中性 插补，只在契约层标记或排除",
            "按 label_start_date 时间切分训练、验证、样本外测试，不随机打乱未来",
            "切分后按 label_end_date 清除跨界标签：train→validation、validation→test",
            "horizon 语义为“已捕获 profile 日步数”，不等于经认证的交易所交易日",
            "先与当前硬规则和遗传算法并行影子比较",
            "只有样本外成本后表现达标且人工确认，才允许作为建议分",
            "数据集就绪不授予任何执行权限：不能下单、不能放宽风控、不能自我晋升",
        ],
    }


if __name__ == "__main__":
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE adaptive_alpha_samples(profile_date TEXT, code TEXT);
    CREATE TABLE adaptive_alpha_returns(start_date TEXT, horizon INTEGER);
    CREATE TABLE adaptive_config(key TEXT PRIMARY KEY, value TEXT);
    """)
    result = readiness(conn)
    assert result["status"] == "waiting_data"
    assert result["trading_impact"] == "none"
    assert result["accelerated"] == True
    assert result["requirements"]["min_profile_days"] == 25
    # PR-8：数据集契约门禁必须以 fail-closed 形式出现，且不夺取执行权。
    assert result["dataset_blockers"], result["dataset_blockers"]
    assert result["execution_authority"] == "none"
    assert result["horizon_semantics"] == learning_dataset.HORIZON_SEMANTICS_OBSERVED_PROFILE_STEPS
    status = control_status(conn)
    assert status["status"] == "shadow_only"
    assert status["max_rank_adjustment"] == 0.0
    assert status["accelerated_mode"] == True
    print(f"neural_shadow self-check passed ({NEURAL_SHADOW_VERSION})")
