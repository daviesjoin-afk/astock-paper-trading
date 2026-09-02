# -*- coding: utf-8 -*-
"""自进化调参系统：AI 自主学习和优化调参策略。

核心机制：
1. 追踪每次调参的结果（成功/失败/收益）
2. 分析历史数据，学习哪些调参模式有效
3. 自动调整调参策略的参数（步长、阈值、共识规则）
4. 根据市场环境变化自适应进化策略

进化维度：
- 步长进化：根据调参成功率自动调整单次最大调整幅度
- 共识阈值进化：根据分歧模式调整共识要求
- 证据阈值进化：根据hold/propose比例调整决策阈值
- 市场感知进化：学习不同市场环境下的最优策略

设计原则：
- 保守进化：每次只微调，避免剧烈变化
- 可追溯：所有进化决策都有完整记录
- 可回滚：发现异常时能快速恢复
- 双AI验证：进化决策本身也需要双AI共识
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import time
from typing import Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
from adaptive_common import _now, _json, _loads  # C3: 收敛重复工具函数

# ─── 进化参数默认值 ───
EVOLUTION_VERSION = "self-evolution-v1"

# 进化参数的边界限制（防止过度进化）
BOUNDS = {
    "max_weight_delta": {"min": 0.01, "max": 0.06, "step": 0.003},      # 单个权重最大调整幅度
    "max_delta_threshold": {"min": 0.001, "max": 0.015, "step": 0.0005}, # 入场阈值最大调整
    "confidence_threshold": {"min": 50, "max": 90, "step": 3},           # 决策置信度阈值
    "consensus_weight_ratio": {"min": 0.40, "max": 0.80, "step": 0.03},  # 共识权重幅度比
    "consensus_direction_threshold": {"min": 0.002, "max": 0.015, "step": 0.001}, # 方向一致性阈值
    "hold_bias": {"min": 0.0, "max": 0.5, "step": 0.02},                # hold倾向（越保守越难propose）
}

# 最小样本量：少于此数量不触发进化
MIN_SAMPLES_FOR_EVOLUTION = 5
# 进化冷却期（秒）：两次进化之间最少间隔
EVOLUTION_COOLDOWN_SECONDS = 3600  # 1小时
# 回滚窗口：最近N次调参中如果成功率骤降，触发回滚
ROLLBACK_WINDOW = 10
ROLLBACK_THRESHOLD = 0.3  # 成功率低于30%触发回滚


def _num(value, default=None):
    try:
        v = float(value)
        return v if abs(v) < 1e15 else default
    except (TypeError, ValueError):
        return default


def ensure_schema(conn):
    """创建自进化相关的数据库表。"""
    conn.executescript("""
        -- 自进化参数快照表
        CREATE TABLE IF NOT EXISTS evolution_params(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            params TEXT NOT NULL,          -- JSON: 当前进化参数
            source TEXT NOT NULL,          -- 'init' | 'evolve' | 'rollback' | 'manual'
            reason TEXT,                   -- 进化/回滚原因
            parent_id INTEGER,             -- 上一版参数的ID
            performance_snapshot TEXT,     -- JSON: 触发进化时的性能快照
            created_at TEXT NOT NULL,
            FOREIGN KEY (parent_id) REFERENCES evolution_params(id)
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_params_recent
            ON evolution_params(id DESC);

        -- 调参结果追踪表
        CREATE TABLE IF NOT EXISTS evolution_tracking(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,       -- 关联 dual_ai_tuning_runs.id
            trigger TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,          -- 'consensus' | 'no_consensus' | 'both_hold' | 'failed'
            market_regime TEXT,
            -- 执行结果
            applied BOOLEAN DEFAULT 0,     -- 是否实际执行了调参
            applied_count INTEGER DEFAULT 0, -- 执行的提案数
            -- 事后评估（延迟填充）
            evaluated BOOLEAN DEFAULT 0,
            eval_score REAL,               -- 事后评分 [-1, 1]，正=有效，负=有害
            eval_detail TEXT,              -- JSON: 评估详情
            eval_at TEXT,
            -- 性能指标
            mimo_latency_ms INTEGER,
            deepseek_latency_ms INTEGER,
            total_latency_ms INTEGER,
            mimo_confidence REAL,
            deepseek_confidence REAL,
            consensus_confidence REAL,     -- 共识置信度（取较小值）
            -- 进化上下文
            evolution_params_id INTEGER,   -- 使用的进化参数版本
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES dual_ai_tuning_runs(id),
            FOREIGN KEY (evolution_params_id) REFERENCES evolution_params(id)
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_tracking_recent
            ON evolution_tracking(id DESC);
        CREATE INDEX IF NOT EXISTS idx_evolution_tracking_status
            ON evolution_tracking(status, created_at DESC);

        -- 进化事件日志
        CREATE TABLE IF NOT EXISTS evolution_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,      -- 'evolve' | 'rollback' | 'manual_adjust' | 'anomaly_detected'
            params_id INTEGER,             -- 关联的参数版本
            detail TEXT NOT NULL,          -- JSON: 事件详情
            metrics TEXT,                  -- JSON: 触发时的指标
            created_at TEXT NOT NULL,
            FOREIGN KEY (params_id) REFERENCES evolution_params(id)
        );
    """)


def get_current_params(conn) -> dict:
    """获取当前生效的进化参数。"""
    row = conn.execute(
        "SELECT id, params, version, source, created_at FROM evolution_params ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return {
            "id": row[0],
            "params": _loads(row[1], _default_params()),
            "version": row[2],
            "source": row[3],
            "created_at": row[4],
        }
    return {"id": None, "params": _default_params(), "version": EVOLUTION_VERSION, "source": "default", "created_at": None}


def _default_params() -> dict:
    """默认进化参数。"""
    return {
        "max_weight_delta": 0.03,        # 单个权重最大调整3%
        "max_delta_threshold": 0.005,    # 入场阈值最大调整0.005
        "confidence_threshold": 70,      # 置信度>=70才propose
        "consensus_weight_ratio": 0.60,  # 共识权重幅度比
        "consensus_direction_threshold": 0.005, # 方向一致性阈值
        "hold_bias": 0.1,               # 轻微hold倾向
        "max_proposals_per_run": 3,      # 单次最多提案数
        "require_dual_confidence": True, # 要求双AI都有一定置信度
        "min_dual_confidence": 40,       # 双AI最低置信度
    }


def init_params(conn, params: Optional[dict] = None, source: str = "init") -> dict:
    """初始化进化参数（仅在没有参数时执行）。"""
    existing = get_current_params(conn)
    if existing["id"] is not None and source == "init":
        return existing  # 已有参数，不覆盖

    p = _default_params()
    if params:
        p.update(params)

    # 验证边界
    p = _clamp_params(p)

    now = _now()
    cursor = conn.execute(
        "INSERT INTO evolution_params(version, params, source, reason, created_at) VALUES(?,?,?,?,?)",
        (EVOLUTION_VERSION, _json(p), source, "初始化", now)
    )
    conn.commit()
    return {"id": cursor.lastrowid, "params": p, "version": EVOLUTION_VERSION, "source": source, "created_at": now}


def _clamp_params(params: dict) -> dict:
    """将参数限制在合法边界内。"""
    clamped = dict(params)
    for key, bounds in BOUNDS.items():
        if key in clamped:
            clamped[key] = max(bounds["min"], min(bounds["max"], clamped[key]))
    return clamped


def track_run(conn, run_id: int, trigger: str, mode: str, status: str,
              market_regime: str = None, applied: bool = False, applied_count: int = 0,
              mimo_latency_ms: int = None, deepseek_latency_ms: int = None,
              total_latency_ms: int = None, mimo_confidence: float = None,
              deepseek_confidence: float = None) -> int:
    """追踪一次调参结果。"""
    params_info = get_current_params(conn)
    consensus_confidence = None
    if mimo_confidence is not None and deepseek_confidence is not None:
        consensus_confidence = min(mimo_confidence, deepseek_confidence)

    cursor = conn.execute(
        """INSERT INTO evolution_tracking(
            run_id, trigger, mode, status, market_regime, applied, applied_count,
            mimo_latency_ms, deepseek_latency_ms, total_latency_ms,
            mimo_confidence, deepseek_confidence, consensus_confidence,
            evolution_params_id, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, trigger, mode, status, market_regime, int(applied), applied_count,
         mimo_latency_ms, deepseek_latency_ms, total_latency_ms,
         mimo_confidence, deepseek_confidence, consensus_confidence,
         params_info["id"], _now())
    )
    conn.commit()
    return cursor.lastrowid


def evaluate_run(conn, tracking_id: int, eval_score: float, eval_detail: dict = None):
    """事后评估一次调参的效果。

    Args:
        tracking_id: evolution_tracking.id
        eval_score: 评分 [-1, 1]，正=有效，负=有害
        eval_detail: 评估详情
    """
    conn.execute(
        "UPDATE evolution_tracking SET evaluated=1, eval_score=?, eval_detail=?, eval_at=? WHERE id=?",
        (max(-1.0, min(1.0, eval_score)), _json(eval_detail or {}), _now(), tracking_id)
    )
    conn.commit()


def get_performance_metrics(conn, window: int = 20) -> dict:
    """计算最近N次调参的性能指标。"""
    rows = conn.execute(
        """SELECT status, applied, eval_score, consensus_confidence,
                  mimo_latency_ms, deepseek_latency_ms, market_regime, created_at
           FROM evolution_tracking ORDER BY id DESC LIMIT ?""",
        (window,)
    ).fetchall()

    if not rows:
        return {"sample_count": 0, "has_data": False}

    total = len(rows)
    consensus_count = sum(1 for r in rows if r[0] == "consensus")
    hold_count = sum(1 for r in rows if r[0] in ("both_hold", "consensus"))
    propose_count = sum(1 for r in rows if r[0] == "consensus")
    applied_count = sum(1 for r in rows if r[1])
    failed_count = sum(1 for r in rows if r[0] == "failed")

    # 评估统计
    evaluated = [(r[2], r[3]) for r in rows if r[2] is not None]
    avg_eval = sum(e[0] for e in evaluated) / len(evaluated) if evaluated else None
    positive_evals = sum(1 for e in evaluated if e[0] > 0)
    negative_evals = sum(1 for e in evaluated if e[0] < 0)

    # 延迟统计
    latencies = [r[5] for r in rows if r[5] is not None]
    avg_latency = sum(latencies) / len(latencies) if latencies else None

    # 市场环境分布
    regimes = {}
    for r in rows:
        regime = r[6] or "unknown"
        regimes[regime] = regimes.get(regime, 0) + 1

    return {
        "sample_count": total,
        "has_data": True,
        "consensus_rate": consensus_count / total,
        "propose_rate": propose_count / total,
        "hold_rate": (hold_count - propose_count) / total if hold_count > propose_count else 0,
        "applied_rate": applied_count / total,
        "failure_rate": failed_count / total,
        "avg_eval_score": avg_eval,
        "positive_eval_rate": positive_evals / len(evaluated) if evaluated else None,
        "negative_eval_rate": negative_evals / len(evaluated) if evaluated else None,
        "avg_latency_ms": avg_latency,
        "market_regime_distribution": regimes,
        "evaluated_count": len(evaluated),
    }


def should_evolve(conn) -> tuple[bool, str]:
    """判断是否应该触发进化。

    Returns:
        (should_evolve: bool, reason: str)
    """
    metrics = get_performance_metrics(conn, 20)

    if not metrics["has_data"]:
        return False, "无历史数据"

    if metrics["sample_count"] < MIN_SAMPLES_FOR_EVOLUTION:
        return False, f"样本不足 ({metrics['sample_count']}/{MIN_SAMPLES_FOR_EVOLUTION})"

    # 检查冷却期
    last_evolution = conn.execute(
        "SELECT created_at FROM evolution_log WHERE event_type='evolve' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_evolution:
        try:
            last_time = dt.datetime.fromisoformat(last_evolution[0])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=TZ)
            elapsed = (dt.datetime.now(TZ) - last_time).total_seconds()
            if elapsed < EVOLUTION_COOLDOWN_SECONDS:
                return False, f"冷却期中 ({int(elapsed)}/{EVOLUTION_COOLDOWN_SECONDS}s)"
        except (ValueError, TypeError):
            pass

    # 检查是否需要回滚
    if metrics["failure_rate"] > ROLLBACK_THRESHOLD and metrics["sample_count"] >= ROLLBACK_WINDOW:
        return True, f"失败率过高 ({metrics['failure_rate']:.1%} > {ROLLBACK_THRESHOLD:.0%})，需要回滚或进化"

    # 检查评估数据
    if metrics["evaluated_count"] >= MIN_SAMPLES_FOR_EVOLUTION:
        avg_score = metrics["avg_eval_score"]
        if avg_score is not None and avg_score < -0.1:
            return True, f"平均评估分数过低 ({avg_score:.3f})，需要进化策略"
        if avg_score is not None and avg_score > 0.3:
            return True, f"平均评估分数良好 ({avg_score:.3f})，可以适度放宽策略"

    # 检查共识率
    if metrics["consensus_rate"] < 0.3 and metrics["sample_count"] >= 8:
        return True, f"共识率过低 ({metrics['consensus_rate']:.1%})，需要调整共识阈值"
    if metrics["consensus_rate"] > 0.8 and metrics["propose_rate"] > 0.6:
        return True, f"共识率过高 ({metrics['consensus_rate']:.1%})，可能阈值太松"

    return False, "当前状态稳定，无需进化"


def evolve(conn, reason: str = "auto") -> dict:
    """执行一次进化：基于历史数据调整参数。

    进化策略：
    1. 分析最近的性能指标
    2. 确定需要调整的参数
    3. 计算调整方向和幅度
    4. 验证边界约束
    5. 保存新参数

    Returns:
        dict 包含进化结果
    """
    current = get_current_params(conn)
    old_params = dict(current["params"])
    metrics = get_performance_metrics(conn, 20)
    new_params = dict(old_params)
    adjustments = []

    # ─── 策略1：根据失败率调整 ───
    if metrics.get("failure_rate", 0) > 0.4:
        # 高失败率 → 更保守
        new_params["hold_bias"] = min(BOUNDS["hold_bias"]["max"],
                                      old_params.get("hold_bias", 0.1) + BOUNDS["hold_bias"]["step"] * 2)
        adjustments.append(f"失败率高({metrics['failure_rate']:.0%})→增大hold倾向")

    # ─── 策略2：根据共识率调整阈值 ───
    consensus_rate = metrics.get("consensus_rate", 0.5)
    if consensus_rate < 0.3:
        # 共识率太低 → 放宽共识要求
        new_params["consensus_weight_ratio"] = max(
            BOUNDS["consensus_weight_ratio"]["min"],
            old_params.get("consensus_weight_ratio", 0.6) - BOUNDS["consensus_weight_ratio"]["step"]
        )
        adjustments.append(f"共识率低({consensus_rate:.0%})→放宽共识幅度比")
    elif consensus_rate > 0.8:
        # 共识率太高 → 可能太松，收紧
        new_params["consensus_weight_ratio"] = min(
            BOUNDS["consensus_weight_ratio"]["max"],
            old_params.get("consensus_weight_ratio", 0.6) + BOUNDS["consensus_weight_ratio"]["step"]
        )
        adjustments.append(f"共识率高({consensus_rate:.0%})→收紧共识幅度比")

    # ─── 策略3：根据评估分数调整步长 ───
    avg_eval = metrics.get("avg_eval_score")
    if avg_eval is not None:
        if avg_eval < -0.2:
            # 评估差 → 缩小步长
            new_params["max_weight_delta"] = max(
                BOUNDS["max_weight_delta"]["min"],
                old_params.get("max_weight_delta", 0.03) - BOUNDS["max_weight_delta"]["step"]
            )
            adjustments.append(f"评估差({avg_eval:.3f})→缩小权重步长")
        elif avg_eval > 0.3:
            # 评估好 → 可以适度放大步长
            new_params["max_weight_delta"] = min(
                BOUNDS["max_weight_delta"]["max"],
                old_params.get("max_weight_delta", 0.03) + BOUNDS["max_weight_delta"]["step"] * 0.5
            )
            adjustments.append(f"评估好({avg_eval:.3f})→适度放大权重步长")

    # ─── 策略4：根据置信度分布调整阈值 ───
    # 如果大多数调参的置信度都很高，可以提高阈值
    # 如果大多数置信度都低，降低阈值
    # （这里用propose率作为代理指标）

    # ─── 策略5：需要回滚的情况 ───
    if metrics.get("failure_rate", 0) > ROLLBACK_THRESHOLD:
        # 回滚到上一个稳定的参数版本
        rollback_target = _find_rollback_target(conn)
        if rollback_target:
            new_params = rollback_target["params"]
            adjustments.append(f"失败率过高→回滚到参数版本#{rollback_target['id']}")

    # 边界约束
    new_params = _clamp_params(new_params)

    # 检查是否有实际变化
    changed_keys = [k for k in new_params if new_params.get(k) != old_params.get(k)]
    if not changed_keys:
        return {
            "evolved": False,
            "reason": "无需调整",
            "metrics": metrics,
        }

    # 保存新参数
    now = _now()
    cursor = conn.execute(
        "INSERT INTO evolution_params(version, params, source, reason, parent_id, performance_snapshot, created_at) VALUES(?,?,?,?,?,?,?)",
        (EVOLUTION_VERSION, _json(new_params), "evolve",
         f"{reason}: " + "; ".join(adjustments), current["id"],
         _json(metrics), now)
    )
    new_id = cursor.lastrowid

    # 记录进化事件
    conn.execute(
        "INSERT INTO evolution_log(event_type, params_id, detail, metrics, created_at) VALUES(?,?,?,?,?)",
        ("evolve", new_id, _json({
            "adjustments": adjustments,
            "changed_keys": changed_keys,
            "old_params": {k: old_params.get(k) for k in changed_keys},
            "new_params": {k: new_params.get(k) for k in changed_keys},
        }), _json(metrics), now)
    )
    conn.commit()

    return {
        "evolved": True,
        "new_params_id": new_id,
        "adjustments": adjustments,
        "changed_keys": changed_keys,
        "old_params": {k: old_params.get(k) for k in changed_keys},
        "new_params": {k: new_params.get(k) for k in changed_keys},
        "metrics": metrics,
    }


def _find_rollback_target(conn) -> Optional[dict]:
    """找到合适的回滚目标参数。"""
    # 找最近一次成功率较高的参数版本
    rows = conn.execute(
        """SELECT ep.id, ep.params
           FROM evolution_params ep
           JOIN evolution_log el ON el.params_id = ep.id
           WHERE el.event_type = 'evolve'
           ORDER BY ep.id DESC LIMIT 10"""
    ).fetchall()

    for row in rows:
        # 检查使用该参数的调参成功率
        success_count = conn.execute(
            """SELECT COUNT(*) FROM evolution_tracking
               WHERE evolution_params_id = ? AND status IN ('consensus', 'both_hold')""",
            (row[0],)
        ).fetchone()[0]
        total_count = conn.execute(
            "SELECT COUNT(*) FROM evolution_tracking WHERE evolution_params_id = ?",
            (row[0],)
        ).fetchone()[0]

        if total_count >= 3 and success_count / total_count > 0.5:
            return {"id": row[0], "params": _loads(row[1], _default_params())}

    return None


def manual_adjust(conn, adjustments: dict, reason: str = "manual") -> dict:
    """手动调整进化参数。

    Args:
        adjustments: 要调整的参数键值对
        reason: 调整原因
    """
    current = get_current_params(conn)
    old_params = dict(current["params"])
    new_params = dict(old_params)
    new_params.update(adjustments)
    new_params = _clamp_params(new_params)

    changed_keys = [k for k in new_params if new_params.get(k) != old_params.get(k)]
    if not changed_keys:
        return {"adjusted": False, "reason": "无变化"}

    now = _now()
    cursor = conn.execute(
        "INSERT INTO evolution_params(version, params, source, reason, parent_id, created_at) VALUES(?,?,?,?,?,?)",
        (EVOLUTION_VERSION, _json(new_params), "manual", reason, current["id"], now)
    )
    new_id = cursor.lastrowid

    conn.execute(
        "INSERT INTO evolution_log(event_type, params_id, detail, created_at) VALUES(?,?,?,?)",
        ("manual_adjust", new_id, _json({
            "adjustments": adjustments,
            "changed_keys": changed_keys,
            "old_params": {k: old_params.get(k) for k in changed_keys},
            "new_params": {k: new_params.get(k) for k in changed_keys},
            "reason": reason,
        }), now)
    )
    conn.commit()

    return {
        "adjusted": True,
        "new_params_id": new_id,
        "changed_keys": changed_keys,
        "old_params": {k: old_params.get(k) for k in changed_keys},
        "new_params": {k: new_params.get(k) for k in changed_keys},
    }


def get_evolution_history(conn, limit: int = 20) -> list:
    """获取进化历史记录。"""
    rows = conn.execute(
        """SELECT id, version, params, source, reason, parent_id, performance_snapshot, created_at
           FROM evolution_params ORDER BY id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    return [{
        "id": r[0], "version": r[1], "params": _loads(r[2], {}),
        "source": r[3], "reason": r[4], "parent_id": r[5],
        "performance_snapshot": _loads(r[6], {}),
        "created_at": r[7],
    } for r in rows]


def get_evolution_log(conn, limit: int = 50) -> list:
    """获取进化事件日志。"""
    rows = conn.execute(
        """SELECT id, event_type, params_id, detail, metrics, created_at
           FROM evolution_log ORDER BY id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    return [{
        "id": r[0], "event_type": r[1], "params_id": r[2],
        "detail": _loads(r[3], {}), "metrics": _loads(r[4], {}),
        "created_at": r[5],
    } for r in rows]


def evolution_status(conn) -> dict:
    """返回自进化系统的完整状态。"""
    current = get_current_params(conn)
    metrics = get_performance_metrics(conn, 20)
    should, should_reason = should_evolve(conn)
    recent_log = get_evolution_log(conn, 10)

    return {
        "version": EVOLUTION_VERSION,
        "current_params": current,
        "performance_metrics": metrics,
        "should_evolve": should,
        "should_evolve_reason": should_reason,
        "bounds": BOUNDS,
        "recent_events": recent_log,
        "config": {
            "min_samples": MIN_SAMPLES_FOR_EVOLUTION,
            "cooldown_seconds": EVOLUTION_COOLDOWN_SECONDS,
            "rollback_window": ROLLBACK_WINDOW,
            "rollback_threshold": ROLLBACK_THRESHOLD,
        },
    }


def auto_evolve_if_needed(conn) -> Optional[dict]:
    """自动检查并执行进化（如果需要）。"""
    should, reason = should_evolve(conn)
    if not should:
        return None
    return evolve(conn, reason=reason)
