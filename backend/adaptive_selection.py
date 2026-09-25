# -*- coding: utf-8 -*-
"""Bounded evolution of paper-account candidate ranking models."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import statistics
from typing import Any

import strategies as S
import paper_repository as PRP
import execution_verification as EV
import adaptive_selection_compat as UNIT_COMPAT
from strategy_registry import labels as strategy_labels
from adaptive_common import _loads, _json  # C3: 收敛重复工具函数
from adaptive_common import TZ as OWNER_TZ

ACCOUNT_NAMES = strategy_labels()
ACCOUNT_MODELS = {
    "tq_breakout": "one_to_two",
    "trend_pullback": "bottom_reversal",
    "sector_rotation": "sentiment_pioneer",
}
ACCOUNT_ALLOWED_MODELS = {
    "tq_breakout": {"one_to_two"},
    "trend_pullback": {"bottom_reversal", "trend_continuation"},
    "sector_rotation": {"sentiment_pioneer"},
}
BASE_WEIGHTS = {
    "one_to_two": {"mom_short": 0.45, "flow": 0.25, "volsurge": 0.20, "sentiment": 0.10},
    "bottom_reversal": {"value": 0.28, "quality": 0.18, "volsurge": 0.22, "flow": 0.15, "mom_short": 0.10, "rsi": 0.07},
    "trend_continuation": {"mom_short": 0.28, "mom": 0.22, "flow": 0.20, "volsurge": 0.15, "quality": 0.15},
    "sentiment_pioneer": {"sentiment": 0.40, "flow": 0.25, "mom_short": 0.20, "volsurge": 0.15},
}
REGIME_MULTIPLIERS = {
    "momentum": {"mom_short": 1.15, "mom": 1.15, "flow": 1.10, "volsurge": 1.05, "sentiment": 1.00, "value": 0.90, "quality": 1.00, "rsi": 0.90},
    "rotation": {"sentiment": 1.20, "flow": 1.15, "volsurge": 1.05, "mom_short": 1.00, "mom": 0.95, "value": 0.95, "quality": 1.00, "rsi": 1.00},
    "risk_off": {"quality": 1.25, "value": 1.20, "rsi": 1.15, "flow": 1.05, "mom_short": 0.75, "mom": 0.75, "volsurge": 0.80, "sentiment": 0.85},
    "high_volatility": {"quality": 1.25, "value": 1.15, "rsi": 1.10, "flow": 1.00, "mom_short": 0.85, "mom": 0.85, "volsurge": 0.75, "sentiment": 0.80},
    "balanced": {},
    "unclassified": {},
}


def _num(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _next_weekday(value):
    day = dt.date.fromisoformat(str(value)[:10]) + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day.isoformat()


def _paper(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adaptive_selection_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            account_id TEXT NOT NULL,
            regime TEXT NOT NULL,
            model_id TEXT NOT NULL,
            baseline_params TEXT NOT NULL,
            candidate_params TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL,
            tier TEXT NOT NULL,
            reason TEXT NOT NULL,
            previous_account_params TEXT,
            effective_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            applied_at TEXT,
            UNIQUE(run_date,account_id,regime)
        );
        CREATE TABLE IF NOT EXISTS adaptive_selection_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            account_id TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_selection_outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            operation TEXT NOT NULL DEFAULT 'apply',
            version TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            applied_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_selection_outbox_pending
            ON adaptive_selection_outbox(status, updated_at);
        """
    )


def _factor_only_patch(candidate, baseline, model_id):
    """Validate the only shape permitted by an automatic AI apply.

    The adaptive database and paper database are separate SQLite files, so the
    outbox below makes application replayable.  This validator is the second,
    deterministic boundary: automatic callers may alter existing weights only,
    never thresholds, conditions, model families, paths, risk, or execution.
    """
    if not isinstance(candidate, dict) or set(candidate) != {"weights"}:
        return False
    base = (baseline or {}).get("weights")
    requested = candidate.get("weights")
    allowed = BASE_WEIGHTS.get(model_id)
    if not isinstance(base, dict) or not isinstance(requested, dict) or not allowed:
        return False
    if set(base) != set(allowed) or set(requested) != set(base):
        return False
    try:
        base_norm = _normalize(base)
        requested_norm = _normalize(requested)
    except Exception:
        return False
    return all(abs(float(requested_norm[key]) - float(base_norm[key])) <= 0.030001
               for key in base_norm)


def _merge_selection_overlay(account_params, candidate, current_weights, model_id):
    """Merge a factor-only patch into the live overlay without erasing it.

    A realtime/AI patch intentionally contains only ``weights``.  Replacing
    ``adaptive_selection`` with that sparse object used to silently drop the
    active model family, conditions and entry paths.  Validate against the
    *currently effective* weights and preserve every unrelated field.
    """
    candidate = dict(candidate or {})
    if set(candidate) != {"weights"}:
        return candidate
    baseline = {"weights": dict(current_weights or {})}
    if not _factor_only_patch(candidate, baseline, model_id):
        raise ValueError("选股权重补丁必须只包含现有因子且单因子变化不超过3个百分点")
    overlay = dict((account_params or {}).get("adaptive_selection") or {})
    overlay["weights"] = dict(candidate["weights"])
    return overlay


def _normalize(weights):
    raw = {key: max(1e-9, _num(value)) for key, value in weights.items()}
    result = {}
    free = set(raw)
    remaining = 1.0
    while free:
        total = sum(raw[key] for key in free) or float(len(free))
        trial = {key: remaining * raw[key] / total for key in free}
        lows = [key for key, value in trial.items() if value < 0.03 - 1e-12]
        highs = [key for key, value in trial.items() if value > 0.65 + 1e-12]
        if not lows and not highs:
            result.update(trial)
            break
        if highs:
            for key in highs:
                result[key] = 0.65
                remaining -= 0.65
                free.remove(key)
            continue
        for key in lows:
            result[key] = 0.03
            remaining -= 0.03
            free.remove(key)
    return {key: round(result[key], 6) for key in weights}


def _current(account):
    account_id = account["id"]
    base_model_id = ACCOUNT_MODELS[account_id]
    params = _loads(account.get("params"), {})
    overlay = params.get("adaptive_selection") or {}
    meta = params.get("adaptive_selection_meta") or {}
    model_id = base_model_id
    candidate_model = str(overlay.get("model_family") or "")
    if meta.get("status") == "active" and candidate_model in ACCOUNT_ALLOWED_MODELS.get(account_id, {base_model_id}):
        model_id = candidate_model
    weights = dict(BASE_WEIGHTS[model_id])
    entry_delta = _num(params.get("entry_score_delta"), _num(params.get("min_t_score_delta")))
    if meta.get("status") == "active":
        candidate_weights = overlay.get("weights") or {}
        if set(candidate_weights) == set(weights):
            weights = _normalize(candidate_weights)
        entry_delta = max(-0.02, min(0.03, _num(overlay.get("entry_score_delta"), entry_delta)))
    return model_id, weights, round(entry_delta, 4), params, meta


def _conditions(model_id, override=None):
    base = S.paper_condition_defaults(model_id)
    if not isinstance(override, dict):
        return base
    enabled = dict(base.get("enabled") or {})
    enabled.update({key: bool(value) for key, value in (override.get("enabled") or {}).items() if key in enabled})
    base["enabled"] = enabled
    for key in base:
        if key == "enabled" or key not in override:
            continue
        value = _num(override.get(key), None)
        if value is not None:
            base[key] = value
    return base


def _structure_target(account_id, current_model, tier):
    """返回可审计的结构变异；结构变异只在标准/成熟证据阶段申请。"""
    paths = {"normal": True}
    target_model = current_model
    mutation = "none"
    reason = ""
    if account_id == "trend_pullback" and tier in {"standard", "mature"}:
        target_model = "trend_continuation"
        mutation = "model_family_switch"
        paths = {"trend_continuation": True, "bottom_fishing": False}
        reason = "趋势波段连续亏损或回撤时，候选从底部反转切换为均线/动量延续；不再把超跌作为买入理由"
    elif account_id == "sector_rotation":
        # 板块热度是排序项，个股强势路径始终保留；它不放宽行情真实性、Q级或单票风控。
        mutation = "add_individual_strong_path"
        paths = {"sector_heat": True, "individual_strong": True}
        reason = "板块热度不足但个股量价、资金、动量同时极强时，允许进入独立强势候选路径"
    elif account_id == "tq_breakout":
        mutation = "add_board_acceleration_path"
        paths = {"normal": True, "board_acceleration": True}
        reason = "保留普通日内T，同时增加竞价/板块/量价共振的强势突破路径"
    return target_model, paths, mutation, reason


def _condition_target(model_id, regime):
    """Generate an auditable rule-set mutation for the current market regime."""
    target = _conditions(model_id)
    if regime == "momentum":
        if model_id == "one_to_two":
            target.update({"pct_high": 6.0, "chase_guard_pct": 8.0, "chase_penalty": 0.52})
        elif model_id == "bottom_reversal":
            target.update({"flow_min": 0.35, "mom20_min": -0.10})
        else:
            target.update({"sentiment_min": -0.75, "sentiment_penalty": 0.12})
    elif regime in {"risk_off", "high_volatility"}:
        if model_id == "one_to_two":
            target.update({"pct_high": 3.5, "chase_guard_pct": 6.0, "chase_penalty": 0.65, "weak_guard_pct": -0.5})
        elif model_id == "bottom_reversal":
            target.update({"vol_surge_min": 1.2, "flow_min": 0.8, "mom20_min": -0.02})
        else:
            target.update({"enabled": {"sentiment_guard": True}, "sentiment_min": 0.0, "sentiment_penalty": 0.35})
    return target


def _blend_conditions(model_id, current, target, tier):
    blend = {"waiting": 0.0, "fast_shadow": 0.10, "micro": 0.25, "standard": 0.65, "mature": 1.0}.get(tier, 0.0)
    # PR-1.1 §17 防复发：新候选 = 当前 overlay 值与最新默认值的插值。若当前
    # overlay 还带着 legacy 单位（2.0），插值会产出 blend(2.0, 0.02) ≈ 1.60 这种
    # 既不是旧值也不是 canonical 的垃圾值，而 activation 边界认不出它（它不是
    # 已证实的 sentinel）。因此在生成候选前先把 current 在内存里归一化。
    current = UNIT_COMPAT.normalize_legacy_conditions(current, model_family=model_id)[0]
    result = _conditions(model_id, current)
    result["enabled"] = dict(current.get("enabled") or {})
    for key in target:
        if key == "enabled":
            if tier in {"standard", "mature"}:
                result["enabled"].update(target["enabled"])
            continue
        result[key] = round(_num(current.get(key), target[key]) + (_num(target[key]) - _num(current.get(key), target[key])) * blend, 6)
    return result


def _evidence(adaptive, paper, account_id, regime=None):
    nav_days = paper.execute("SELECT COUNT(*) FROM paper_nav WHERE account_id=?", (account_id,)).fetchone()[0]
    # 证据量口径：只有被证据证明成交的行才算执行样本（与 adaptive_risk 同口径）。
    closed = paper.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND side='sell'"
        " AND status='filled' AND " + EV.VERIFIED_PREDICATE, (account_id,)
    ).fetchone()[0]
    fills = paper.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status='filled'"
        " AND " + EV.VERIFIED_PREDICATE, (account_id,)
    ).fetchone()[0]
    rewards = adaptive.execute(
        "SELECT regime,raw_reward,excess_return_pct,created_at FROM adaptive_rewards WHERE account_id=? ORDER BY id DESC",
        (account_id,),
    ).fetchall()
    regimes = sorted({row["regime"] for row in rewards if row["regime"] != "unclassified"})
    reward_values = [_num(row["raw_reward"]) for row in rewards]
    recent_values = reward_values[:6]
    regime_values = [_num(row["raw_reward"]) for row in rewards if regime and row["regime"] == regime]
    recent_mean = statistics.mean(recent_values) if recent_values else None
    regime_mean = statistics.mean(regime_values) if regime_values else None
    # Recent observations describe the current market better, but a single
    # noisy close must not dominate the decision.  Blend recency with the
    # current-regime sample whenever it exists.
    effective_mean = None
    if recent_mean is not None and regime_mean is not None:
        effective_mean = recent_mean * 0.65 + regime_mean * 0.35
    elif recent_mean is not None:
        effective_mean = recent_mean
    elif regime_mean is not None:
        effective_mean = regime_mean
    excess = [_num(row["excess_return_pct"]) for row in rewards]
    return {
        "nav_days": int(nav_days), "closed_trades": int(closed), "filled_orders": int(fills),
        "trade_events": max(int(closed), int(fills) // 2), "reward_samples": len(rewards),
        "regime_count": len(regimes), "regimes": regimes,
        "mean_reward": round(effective_mean, 4) if effective_mean is not None else None,
        "historical_mean_reward": round(statistics.mean(reward_values), 4) if reward_values else None,
        "recent_mean_reward": round(recent_mean, 4) if recent_mean is not None else None,
        "current_regime_mean_reward": round(regime_mean, 4) if regime_mean is not None else None,
        "mean_excess_pct": round(statistics.mean(excess), 4) if excess else None,
    }


def _requirements(config, prefix):
    defaults = {
        "shadow": (1, 1, 2, 1), "fast": (3, 2, 4, 1), "standard": (5, 4, 6, 1), "mature": (10, 8, 12, 2),
    }[prefix]
    return {
        "nav_days": int(config.get(f"selection_{prefix}_nav_days", defaults[0])),
        "trade_events": int(config.get(f"selection_{prefix}_trade_events", defaults[1])),
        "reward_samples": int(config.get(f"selection_{prefix}_reward_samples", defaults[2])),
        "regime_count": int(config.get(f"selection_{prefix}_regimes", defaults[3])),
    }


def _tier(evidence, profile, config):
    tiers = {}
    achieved = "waiting"
    for prefix, name in (("shadow", "fast_shadow"), ("fast", "micro"), ("standard", "standard"), ("mature", "mature")):
        req = _requirements(config, prefix)
        checks = {key: {"current": int(evidence.get(key) or 0), "required": value,
                        "passed": int(evidence.get(key) or 0) >= value} for key, value in req.items()}
        checks["data_quality"] = {"current": profile.get("quality"), "required": "valid_close",
                                  "passed": profile.get("quality") == "valid_close"}
        passed = all(item["passed"] for item in checks.values())
        tiers[prefix] = {"passed": passed, "checks": checks, "requirements": req}
        if passed:
            achieved = name
    return achieved, tiers


def _proposal(account_id, model_id, current_weights, current_delta, current_conditions, regime, mean_reward, tier, current_structure=None):
    target_model, entry_paths, mutation, structure_reason = _structure_target(account_id, model_id, tier)
    proposal_model = target_model if target_model in BASE_WEIGHTS else model_id
    multipliers = REGIME_MULTIPLIERS.get(regime, {})
    target = _normalize({key: value * multipliers.get(key, 1.0) for key, value in BASE_WEIGHTS[proposal_model].items()})
    blend = {"waiting": 0.20, "fast_shadow": 0.35, "micro": 0.65, "standard": 0.85, "mature": 1.0}.get(tier, 0.20)
    if set(current_weights) == set(target):
        weights = _normalize({key: current_weights[key] + (target[key] - current_weights[key]) * blend for key in current_weights})
    else:
        # 模型族切换先生成完整基线，执行仍由结构门禁控制。
        weights = target
    target_delta = 0.0
    if mean_reward is not None and mean_reward < -0.10:
        target_delta = 0.020
    elif mean_reward is not None and mean_reward > 0.10:
        target_delta = -0.015
    step = {"waiting": 0.004, "fast_shadow": 0.008, "micro": 0.018, "standard": 0.030, "mature": 0.045}.get(tier, 0.004)
    delta = current_delta + max(-step, min(step, target_delta - current_delta))
    delta = round(max(-0.03, min(0.05, delta)), 4)
    target_conditions = _condition_target(proposal_model, regime)
    conditions = _blend_conditions(proposal_model, current_conditions, target_conditions, tier)
    result = {"model_family": proposal_model, "weights": weights, "entry_score_delta": delta, "conditions": conditions,
              "entry_paths": entry_paths, "mutation_type": mutation,
              "structure_reason": structure_reason,
              "objectives": ["收益", "最大回撤", "交易成本覆盖", "行情质量通过率"]}
    if account_id == "sector_rotation":
        result["objectives"] += ["个股强势路径成功率", "板块热度不足时的独立强势胜率", "追高失败率"]
    if account_id == "tq_breakout":
        result["objectives"] += ["封板触达率", "次日高开率", "炸板率"]
    if account_id == "trend_pullback":
        result["objectives"] += ["趋势延续率", "抄底误判率", "持仓回撤"]
    return result


def _changed(weights, delta, conditions, candidate):
    if abs(delta - candidate["entry_score_delta"]) > 1e-8:
        return True
    if any(abs(weights[key] - candidate["weights"][key]) > 1e-6 for key in weights):
        return True
    return conditions != (candidate.get("conditions") or {}) or bool(candidate.get("mutation_type") not in {None, "none"})


def _upsert(conn, payload, now):
    existing = conn.execute(
        "SELECT id,status FROM adaptive_selection_candidates WHERE run_date=? AND account_id=? AND regime=?",
        (payload["run_date"], payload["account_id"], payload["regime"]),
    ).fetchone()
    if existing and existing["status"] in {"applied", "rolled_back"}:
        return existing["id"]
    if existing:
        pending = conn.execute(
            "SELECT 1 FROM adaptive_selection_outbox WHERE candidate_id=? AND status IN ('pending','error') LIMIT 1",
            (int(existing["id"]),),
        ).fetchone()
        if pending:
            # Do not overwrite the candidate while a cross-database apply is
            # waiting for replay; the outbox must describe the exact row that
            # will be applied.
            return existing["id"]
    conn.execute(
        """INSERT INTO adaptive_selection_candidates(
           run_date,account_id,regime,model_id,baseline_params,candidate_params,evidence,status,tier,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_date,account_id,regime) DO UPDATE SET
             model_id=excluded.model_id,baseline_params=excluded.baseline_params,candidate_params=excluded.candidate_params,
             evidence=excluded.evidence,status=excluded.status,tier=excluded.tier,reason=excluded.reason,updated_at=excluded.updated_at""",
        (payload["run_date"], payload["account_id"], payload["regime"], payload["model_id"],
         _json(payload["baseline_params"]), _json(payload["candidate_params"]), _json(payload["evidence"]),
         payload["status"], payload["tier"], payload["reason"], now, now),
    )
    return conn.execute(
        "SELECT id FROM adaptive_selection_candidates WHERE run_date=? AND account_id=? AND regime=?",
        (payload["run_date"], payload["account_id"], payload["regime"]),
    ).fetchone()["id"]


_AUTO_APPROVERS = {"", "bounded-auto", "auto", "deepseek-bounded-realtime"}


def _queue_outbox(conn, item, candidate, previous, version, effective, approved_by, now,
                  operation="apply", outbox_candidate_id=None, reason=None):
    payload = {
        "candidate": candidate,
        "previous_account_params": previous,
        "effective_date": effective,
        "approved_by": approved_by,
        "tier": item["tier"],
        "regime": item["regime"],
        "reason": reason,
    }
    conn.execute(
        """INSERT INTO adaptive_selection_outbox(
           candidate_id,account_id,operation,version,payload,status,attempts,created_at,updated_at)
           VALUES(?,?,?,?,?,'pending',0,?,?)
           ON CONFLICT(candidate_id) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (int(outbox_candidate_id if outbox_candidate_id is not None else item["id"]),
         item["account_id"], operation, version, _json(payload), now, now),
    )
    # Commit the durable intent before touching the separate paper database.
    # A crash after this point is replayable by replay_pending_outbox().
    conn.commit()


def _mark_outbox_error(conn, candidate_id, error, now):
    conn.execute(
        """UPDATE adaptive_selection_outbox
           SET status='error',attempts=attempts+1,last_error=?,updated_at=?
           WHERE candidate_id=?""",
        (str(error)[:500], now, int(candidate_id)),
    )
    conn.commit()


def _mark_outbox_applied(conn, candidate_id, now):
    conn.execute(
        """UPDATE adaptive_selection_outbox
           SET status='applied',last_error=NULL,updated_at=?,applied_at=?
           WHERE candidate_id=?""",
        (now, now, int(candidate_id)),
    )


def cancel_outbox(conn, candidate_id, now, reason="跨账本补偿取消"):
    """终止一次已经无法安全重放的跨库意图。

    外层补偿会把纸盘参数恢复到快照；此时原来的 pending/error 意图必须
    变成终态，否则下一轮 evaluate 会把已补偿的写入再次 replay。回滚意图
    使用负 candidate_id，因此同时终止正、负两个键，但绝不改写已 applied
    的历史记录。
    """
    candidate_id = int(candidate_id)
    ids = (candidate_id, -candidate_id) if candidate_id else (0,)
    conn.execute(
        """UPDATE adaptive_selection_outbox
              SET status='cancelled',last_error=?,updated_at=?
            WHERE candidate_id IN (?,?) AND status IN ('pending','error')""",
        (str(reason or "跨账本补偿取消")[:500], now, ids[0], ids[1]),
    )
    conn.commit()


def apply_candidate(conn, paper_db_path, candidate_id, now_fn, approved_by="bounded-auto", effective_date=None):
    row = conn.execute("SELECT * FROM adaptive_selection_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("选股候选不存在")
    item = dict(row)
    approved = str(approved_by or "").lower()
    if item["status"] == "applied":
        _mark_outbox_applied(conn, candidate_id, now_fn())
        conn.commit()
        return candidate_id
    if item["status"] not in {"eligible_auto_adjust", "eligible_manual_review", "eligible_structural_review"}:
        raise ValueError("选股候选尚未通过进化门槛")
    candidate_preview = _loads(item.get("candidate_params"), {})
    is_structural = candidate_preview.get("mutation_type") not in {None, "none"}
    if approved in _AUTO_APPROVERS:
        if is_structural:
            raise ValueError("结构变更必须人工确认后才能生效")

    # Read the current paper account once to capture the rollback point.  The
    # outbox is committed only after this read succeeds, so a missing account
    # never creates a phantom pending operation.
    paper = _paper(paper_db_path)
    try:
        account_row = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (item["account_id"],)).fetchone()
        if not account_row:
            raise ValueError("模拟策略账户不存在")
        account = dict(account_row)
        current_model, current_weights, _, account_params, _ = _current(account)
        previous = _json(account_params)
        if approved in _AUTO_APPROVERS and not _factor_only_patch(
                candidate_preview, {"weights": current_weights}, current_model):
            raise ValueError("自动应用只允许当前生效因子权重且单因子变化不超过3个百分点")
    finally:
        paper.close()

    candidate = candidate_preview
    effective = str(effective_date or now_fn())[:10]
    version = f"select-evo-{item['run_date'].replace('-', '')}-{candidate_id}"
    now = now_fn()
    _queue_outbox(conn, item, candidate, previous, version, effective, approved_by, now)

    paper = _paper(paper_db_path)
    try:
        paper.execute("BEGIN IMMEDIATE")
        account_row = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (item["account_id"],)).fetchone()
        if not account_row:
            raise ValueError("模拟策略账户不存在")
        account = dict(account_row)
        existing_version = paper.execute(
            "SELECT 1 FROM paper_parameter_versions WHERE account_id=? AND version=? LIMIT 1",
            (item["account_id"], version),
        ).fetchone()
        if not existing_version:
            current_model, current_weights, _, account_params, _ = _current(account)
            merged_overlay = _merge_selection_overlay(
                account_params, candidate, current_weights, current_model,
            )
            # Layer B（PR-1.1）：历史 candidate 可能携带 PR #107 之前的
            # percentage-point 值（individual_mom5_min = 2.0）。必须在**写 live
            # 之前**做一次兼容归一化——只修已证实的 legacy sentinel，且**不修改
            # candidate 行本身**（历史证据保持原样，见 §18）。
            merged_overlay, _unit_fixes = UNIT_COMPAT.normalize_legacy_selection_units(
                merged_overlay, effective_model=current_model,
            )
            account_params["adaptive_selection"] = merged_overlay
            account_params["adaptive_selection_meta"] = {
                "status": "active", "candidate_id": candidate_id, "version": version,
                "effective_date": effective, "approved_by": approved_by,
                "source_regime": item["regime"], "tier": item["tier"],
            }
            paper.execute("UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
                          (_json(account_params), version, now_fn(), item["account_id"]))
            paper.execute(
                """INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (account.get("cycle_id"), item["account_id"], version, account.get("style") or "adaptive-selection",
                 _json(account_params), f"模拟盘选股进化候选 {candidate_id}；{item['tier']}", effective, now_fn()),
            )
            PRP.audit(
                paper, item["account_id"], "adaptive_selection_applied",
                f"candidate={candidate_id}; version={version}; effective={effective}", now_fn(),
            )
            # 归一化发生时才写审计：同时记录 candidate 原始值（历史事实）与
            # 实际生效值，二者一起落在同一事务里。
            for _unit_fix in _unit_fixes:
                PRP.audit(
                    paper, item["account_id"], UNIT_COMPAT.NORMALIZED_EVENT,
                    _json({**_unit_fix, "candidate_id": candidate_id,
                           "applied_by": approved_by}),
                    now_fn(),
                )
        paper.commit()
    except Exception as exc:
        try:
            paper.rollback()
        finally:
            paper.close()
        _mark_outbox_error(conn, candidate_id, f"{type(exc).__name__}: {exc}", now_fn())
        raise
    finally:
        try:
            paper.close()
        except Exception:
            pass

    now = now_fn()
    conn.execute(
        """UPDATE adaptive_selection_candidates SET status='applied',previous_account_params=?,effective_date=?,
           applied_at=?,updated_at=? WHERE id=?""", (previous, effective, now, now, candidate_id),
    )
    conn.execute("INSERT INTO adaptive_selection_events(candidate_id,account_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                 (candidate_id, item["account_id"], "applied", _json({"tier": item["tier"], "effective_date": effective}), now))
    _mark_outbox_applied(conn, candidate_id, now)
    conn.commit()
    return candidate_id


def replay_pending_outbox(conn, paper_db_path, now_fn, limit=20):
    """Replay durable selection applies after a process crash.

    ``apply_candidate`` checks the paper version before writing, so replay is
    idempotent even when the paper commit succeeded immediately before a crash.
    """
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT candidate_id,operation,payload FROM adaptive_selection_outbox
           WHERE status IN ('pending','error') ORDER BY id LIMIT ?""", (max(1, min(int(limit), 100)),)
    ).fetchall()
    recovered = []
    for row in rows:
        candidate_id = int(row[0])
        payload = _loads(row[2], {}) or {}
        try:
            if str(row[1]) == "rollback":
                _finish_rollback(conn, paper_db_path, abs(candidate_id), payload, now_fn)
            else:
                apply_candidate(
                    conn, paper_db_path, candidate_id, now_fn,
                    approved_by=payload.get("approved_by", "bounded-auto"),
                    effective_date=payload.get("effective_date"),
                )
            recovered.append(candidate_id)
        except Exception as exc:
            _mark_outbox_error(conn, candidate_id, f"replay:{type(exc).__name__}: {exc}", now_fn())
    return recovered


def _finish_rollback(conn, paper_db_path, candidate_id, payload, now_fn):
    """Apply/reconcile a rollback outbox item idempotently."""
    item = conn.execute(
        "SELECT * FROM adaptive_selection_candidates WHERE id=?", (int(candidate_id),)
    ).fetchone()
    if not item:
        raise ValueError("回滚候选不存在")
    account_id = str(item["account_id"])
    version = str(payload.get("version") or f"select-rollback-{account_id}-{candidate_id}")
    previous = payload.get("previous_account_params") or {}
    reason = str(payload.get("reason") or "人工回滚")[:500]
    effective = str(payload.get("effective_date") or now_fn())[:10]
    paper = _paper(paper_db_path)
    try:
        paper.execute("BEGIN IMMEDIATE")
        account_row = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if not account_row:
            raise ValueError("模拟策略账户不存在")
        account = dict(account_row)
        existing_version = paper.execute(
            "SELECT 1 FROM paper_parameter_versions WHERE account_id=? AND version=? LIMIT 1",
            (account_id, version),
        ).fetchone()
        if not existing_version:
            # Layer B（PR-1.1）：回滚点本身也可能带着 legacy 单位值（它来自
            # 更早的 live params 快照）。这里同样是"写 live 之前"的兼容边界，
            # 只修已证实的 legacy sentinel，不改写 outbox payload 历史。
            restored, _unit_fixes = UNIT_COMPAT.normalize_legacy_account_params(previous)
            paper.execute("UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
                          (_json(restored), version, now_fn(), account_id))
            paper.execute(
                """INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (account.get("cycle_id"), account_id, version, account.get("style") or "adaptive-selection",
                 _json(restored), reason, effective, now_fn()),
            )
            PRP.audit(
                paper, account_id, "adaptive_selection_rolled_back",
                f"candidate={candidate_id}; reason={reason}", now_fn(),
            )
            for _unit_fix in _unit_fixes:
                PRP.audit(
                    paper, account_id, UNIT_COMPAT.NORMALIZED_EVENT,
                    _json({**_unit_fix, "candidate_id": candidate_id,
                           "source": "rollback"}),
                    now_fn(),
                )
        paper.commit()
    except Exception as exc:
        try:
            paper.rollback()
        finally:
            paper.close()
        _mark_outbox_error(conn, -int(candidate_id), f"{type(exc).__name__}: {exc}", now_fn())
        raise
    finally:
        try:
            paper.close()
        except Exception:
            pass
    now = now_fn()
    conn.execute("UPDATE adaptive_selection_candidates SET status='rolled_back',updated_at=? WHERE id=?", (now, candidate_id))
    conn.execute("INSERT INTO adaptive_selection_events(candidate_id,account_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                 (candidate_id, account_id, "rolled_back", _json({"reason": reason}), now))
    _mark_outbox_applied(conn, -int(candidate_id), now)
    conn.commit()


def rollback(conn, paper_db_path, account_id, now_fn, reason="人工回滚"):
    item = conn.execute(
        "SELECT * FROM adaptive_selection_candidates WHERE account_id=? AND status='applied' ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if not item:
        raise ValueError("该策略没有可回滚的选股进化版本")
    item = dict(item)
    previous = _loads(item["previous_account_params"], {})
    if not isinstance(previous, dict):
        raise ValueError("回滚版本缺少可恢复参数")
    # Capture the account existence before making the durable intent.  The
    # actual cross-database write is replayed through the same outbox protocol
    # as apply_candidate().
    paper = _paper(paper_db_path)
    try:
        account = paper.execute("SELECT id FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            raise ValueError("模拟策略账户不存在")
    finally:
        paper.close()
    version = f"select-rollback-{account_id}-{item['id']}"
    now = now_fn()
    _queue_outbox(
        conn, item, previous, previous, version, dt.date.today().isoformat(),
        "human-ui", now, operation="rollback", outbox_candidate_id=-int(item["id"]), reason=reason,
    )
    _finish_rollback(conn, paper_db_path, int(item["id"]), {
        "previous_account_params": previous,
        "version": version,
        "effective_date": dt.date.today().isoformat(),
        "reason": reason,
    }, now_fn)
    return item["id"]


def evaluate(conn, profile, config, paper_db_path, now_fn):
    ensure_schema(conn)
    config = config or {}
    if not os.path.exists(paper_db_path):
        return {"status": "paper_db_missing", "candidates": 0, "auto_applied": []}
    recovered_ids = replay_pending_outbox(conn, paper_db_path, now_fn)
    paper = _paper(paper_db_path)
    candidate_ids = []
    auto_ids = []
    try:
        accounts = [dict(row) for row in paper.execute(
            """SELECT * FROM paper_accounts
               WHERE id IN ('tq_breakout','trend_pullback','sector_rotation','reported_profit_breakout','main_force_top10')
               ORDER BY id"""
        )]
        for account in accounts:
            # 三日策略的利润披露、均线与证券范围是可解释的硬规则，不属于
            # PAPER_WEIGHTS 模型。把它伪装成可调权重模型会产生“已应用”但
            # 实盘候选根本不会读取的假版本。因此只接入同一份兑现/行情证据，
            # 生成影子复盘，任何结构或阈值变化仍必须人工另行评审。
            if account["id"] in {"reported_profit_breakout", "main_force_top10"}:
                evidence = _evidence(conn, paper, account["id"], profile.get("regime"))
                evidence["strategy_version"] = str(account.get("version") or "")
                evidence["profile_date"] = str(profile.get("profile_date") or "")[:10]
                tier, tier_checks = _tier(evidence, profile, config)
                evidence["evolution_tier"] = tier
                evidence["tier_checks"] = tier_checks
                evidence["hard_rule_locked"] = True
                payload = {
                    "run_date": profile["profile_date"], "account_id": account["id"],
                    "regime": profile["regime"], "model_id": account["id"],
                    "baseline_params": {"hard_rule_locked": True},
                    "candidate_params": {"mode": "shadow_review_only", "hard_rule_locked": True},
                    "evidence": evidence, "status": "shadow_candidate", "tier": tier,
                    "reason": ("三日策略已接入自进化证据与兑现复盘；利润披露、均线和证券范围硬条件保持锁定，仅生成影子建议"
                               if account["id"] == "reported_profit_breakout" else
                               "超强主力股已接入兑现复盘；每日10选3、资金确认与出货退出规则保持锁定，仅生成影子建议"),
                }
                candidate_ids.append(_upsert(conn, payload, now_fn()))
                continue
            model_id, weights, delta, params, _ = _current(account)
            current_conditions = _conditions(model_id, (params.get("adaptive_selection") or {}).get("conditions"))
            evidence = _evidence(conn, paper, account["id"], profile.get("regime"))
            evidence["strategy_version"] = str(account.get("version") or "")
            evidence["profile_date"] = str(profile.get("profile_date") or "")[:10]
            # 叠加新闻学习因子：候选池中有利好/利空新闻时调整进化方向
            try:
                import news_learning as nl
                news_overlay = nl.code_overlay("__market__", profile.get("profile_date"))
                evidence["news_factor"] = {
                    "status": news_overlay.get("status"),
                    "threshold_delta": news_overlay.get("threshold_delta", 0.0),
                    "events": news_overlay.get("events", 0),
                }
            except Exception:
                evidence["news_factor"] = {"status": "unavailable", "threshold_delta": 0.0}
            tier, tier_checks = _tier(evidence, profile, config)
            evidence["evolution_tier"] = tier
            evidence["tier_checks"] = tier_checks
            gate_key = {"fast_shadow": "shadow", "micro": "fast", "standard": "standard", "mature": "mature"}.get(tier, "shadow")
            evidence["gates"] = tier_checks[gate_key]["checks"]
            current_overlay = params.get("adaptive_selection") or {}
            current_structure = {
                "model_family": current_overlay.get("model_family") or model_id,
                "entry_paths": current_overlay.get("entry_paths") or {"normal": True},
            }
            proposal = _proposal(account["id"], model_id, weights, delta, current_conditions,
                                profile["regime"], evidence.get("mean_reward"), tier, current_structure)
            changed = _changed(weights, delta, current_conditions, proposal)
            is_structural = proposal.get("mutation_type") not in {None, "none"}
            if not changed:
                status, reason = "no_change", "当前盘面与兑现结果不要求改变模拟盘选股参数"
            elif is_structural:
                # 结构性变更（切换模型/新增入场路径）不得在微调阶段自动上线。
                # micro/fast 只记录影子候选，必须积累到 standard/mature 后人工确认。
                if tier in {"standard", "mature"}:
                    status, reason = "eligible_structural_review", f"{tier} 级证据通过；生成结构变更候选，等待人工确认：{proposal.get('structure_reason')}"
                else:
                    status, reason = "shadow_candidate", f"{tier} 阶段仅记录结构候选，需积累证据后人工确认"
            elif tier in {"micro", "standard", "mature"}:
                # Only a pure factor-weight patch can ever enter the bounded
                # automatic lane.  Conditions, score deltas and paths are
                # still recorded for human review, never silently applied.
                factor_only = _factor_only_patch(
                    {"weights": proposal.get("weights")},
                    {"weights": weights},
                    model_id,
                ) and proposal.get("entry_score_delta") == delta \
                    and proposal.get("conditions") == current_conditions
                if factor_only:
                    status, reason = "eligible_auto_adjust", f"{tier} 级证据通过；仅允许模拟盘已有因子权重小步调整（单因子不超过±3个百分点）"
                else:
                    status, reason = "eligible_manual_review", f"{tier} 级证据通过；候选包含条件/阈值变化，保留为人工确认，不自动生效"
            elif tier == "fast_shadow":
                status, reason = "shadow_candidate", "1日快速影子候选已生成；继续观察至3日门槛后再应用小步调整"
            elif evidence["nav_days"] >= 3 and evidence["reward_samples"] >= 3:
                status, reason = "shadow_candidate", "已形成选股影子候选，等待证据门槛"
            else:
                status, reason = "waiting_data", "净值、交易事件、成熟奖励或盘面样本不足"
            payload = {
                "run_date": profile["profile_date"], "account_id": account["id"], "regime": profile["regime"],
                "model_id": model_id, "baseline_params": {"weights": weights, "entry_score_delta": delta},
                "candidate_params": proposal, "evidence": evidence, "status": status, "tier": tier, "reason": reason,
            }
            candidate_id = _upsert(conn, payload, now_fn())
            candidate_ids.append(candidate_id)
            # A bounded candidate is still untrusted until the human
            # confirmation action.  Never self-apply when the caller omits a
            # policy value or passes a stale legacy config.
            if status == "eligible_auto_adjust" and config.get("selection_auto_apply_bounded", False) is True:
                apply_candidate(conn, paper_db_path, candidate_id, now_fn)
                auto_ids.append(candidate_id)
    finally:
        paper.close()
    return {"status": "completed", "candidates": len(candidate_ids), "auto_applied": auto_ids,
            "recovered_outbox": recovered_ids}


def overview(conn, config, paper_db_path):
    ensure_schema(conn)
    candidates = []
    for row in conn.execute("SELECT * FROM adaptive_selection_candidates ORDER BY run_date DESC,id DESC LIMIT 12"):
        item = dict(row)
        for key in ("baseline_params", "candidate_params", "evidence"):
            item[key] = _loads(item[key], {})
        item.pop("previous_account_params", None)
        item["account_name"] = ACCOUNT_NAMES.get(item["account_id"], item["account_id"])
        candidates.append(item)
    active = []
    if os.path.exists(paper_db_path):
        paper = _paper(paper_db_path)
        try:
            for row in paper.execute("SELECT id,name,version,params FROM paper_accounts ORDER BY id"):
                params = _loads(row["params"], {})
                meta = params.get("adaptive_selection_meta") or {}
                if meta.get("status") == "active":
                    # 账户总 version 会被随后生效的风控版本覆盖，不能拿它展示
                    # 选股版本。以选股元数据的独立 version 为主，并从版本账本
                    # 读取真实写入时间，避免出现“8月11日生成、8月5日生效”的伪倒序。
                    version = str(meta.get("version") or row["version"] or "")
                    audit = paper.execute(
                        """SELECT effective_date,created_at FROM paper_parameter_versions
                           WHERE account_id=? AND version=? ORDER BY id DESC LIMIT 1""",
                        (row["id"], version),
                    ).fetchone()
                    active.append({
                        "account_id": row["id"], "account_name": row["name"], "version": version,
                        "account_version": row["version"], "params": params.get("adaptive_selection") or {},
                        "meta": meta,
                        "effective_date": (audit["effective_date"] if audit else meta.get("effective_date")),
                        "created_at": (audit["created_at"] if audit else None),
                    })
        finally:
            paper.close()
    return {
        "mode": "模拟盘选股自动进化",
        "policy": "当前模拟账户（内置模板与用户自定义策略）可进化因子权重、入场阈值和白名单选股条件；板块热度不足时可提出个股强势路径，趋势模型可提出从抄底切换为趋势延续；结构变更先影子验证并人工确认，公共选股页面不受影响。",
        "auto_apply_bounded": bool(config.get("selection_auto_apply_bounded", False)),
        "requirements": _requirements(config, "shadow"),
        "tiers": {
            "fast_shadow": {"label": "1日快速影子", "max_step": "仅观察，不改账户", "requirements": _requirements(config, "shadow")},
            "micro": {"label": "3日小步调整", "max_step": "权重/阈值小步变更", "requirements": _requirements(config, "fast")},
            "standard": {"label": "5日标准验证", "max_step": "参数自动生效；结构变更进入人工确认", "requirements": _requirements(config, "standard")},
            "mature": {"label": "10日成熟进化", "max_step": "完整结构候选，人工确认后生效并可回滚", "requirements": _requirements(config, "mature")},
        },
        "candidates": candidates, "active_versions": active,
    }


# ---------------------------------------------------------------------------
# R27-B2C-6 —— selection candidate 的 typed owner fact contract
# ---------------------------------------------------------------------------
#
# ``adaptive_selection_candidates`` 是一张**可变**行表：同一个
# ``(run_date,account_id,regime)`` 会被 owner 反复 UPDATE，行还会一路走到 ``applied`` /
# ``rolled_back``。因此在这段契约之前，research 侧没有任何东西能回答三个事实层问题：
#
#     identity          这条候选事实究竟是哪一条（row 会被覆盖 → 只知道 ``id`` 不够）
#     availability      这条候选**在哪个瞬间**才成为可引用的事实
#     verification      owner 能不能证明这是**它自己签发**的事实
#
# 本段只回答这三个问题，并把两件极易混淆的事刻意分开：
#
#     candidate lifecycle status ≠ owner verification
#     run_date                   ≠ revision availability
#
# ``eligible_auto_adjust`` / ``applied`` / ``shadow_candidate`` 是**业务生命周期与资格**
# 词，供 reviewer 与 apply 门禁使用。本段**不**把它们（或它们的任何组合）映射成核验结论，
# 也**不**认为 ``applied`` 让一条候选更"可信"。

SELECTION_FACT_CONTRACT_VERSION = "adaptive-selection-fact-v1"

#: AI 影子提案的 tier 记号 —— 由 owner 独占签发，调用方不能传入。
AI_REALTIME_TIER = "ai_realtime"

#: 影子提案候选的生命周期状态 —— 同样由 owner 独占决定。
#:
#: 它刻意**不在** ``apply_candidate`` 允许的资格集合里（``eligible_auto_adjust`` /
#: ``eligible_manual_review`` / ``eligible_structural_review``），因此"AI 提案直接生效"
#: 在这条链路上结构性不可表达：AI 只能产出影子候选，apply 仍必须由既有的
#: 人工/门禁路径发起并重新校验。
SHADOW_PROPOSAL_STATUS = "shadow_proposal"

#: owner 自己能签发的 lifecycle status 闭集。**不是**核验词表。
SELECTION_LIFECYCLE_STATUSES = (
    "waiting_data", "shadow_candidate", "no_change",
    "eligible_auto_adjust", "eligible_manual_review", "eligible_structural_review",
    SHADOW_PROPOSAL_STATUS, "applied", "rolled_back",
)

#: owner 自己能签发的 tier 闭集。
SELECTION_TIERS = ("waiting", "fast_shadow", "micro", "standard", "mature", AI_REALTIME_TIER)

#: owner 签发的**极小** factual verification 闭集。两态，且与 lifecycle status 词表**零
#: 交集** —— 因此 ``status`` 的任何取值都不可能被读成核验结论。
#:
#: * ``selection_candidate_recorded``：owner 能证明这条记录是**它自己签发**的、必要归一
#:   列齐全且自洽的事实。这个值的含义**仅**是"这是一条可靠的 owner 事实"，
#:   **不是**"这条候选通过了晋级验证"，更不是"它值得 apply"。
#: * ``selection_candidate_unproven``：记录可读，但 owner **无法自证**它是自己签发的
#:   （用了 owner 不签发的 status / tier / account / model 词汇，或缺少必要归一列）。
#:   它仍然是一条事实，但 owner 不为它的来源背书 —— 于是它在 research 层只能是
#:   ``unverified``，永远不会因为"看起来像候选"而升级。
SELECTION_FACT_RECORDED = "selection_candidate_recorded"
SELECTION_FACT_OWNER_UNPROVEN = "selection_candidate_unproven"
SELECTION_FACT_VERIFICATION_STATUSES = (
    SELECTION_FACT_RECORDED, SELECTION_FACT_OWNER_UNPROVEN,
)

SELECTION_CANDIDATE_RECORD_KIND = "selection_candidate"

#: typed 读侧要求的**必需归一列**：少一个就不构成一条可引用的候选事实。
_SELECTION_REQUIRED_COLUMNS = (
    "id", "run_date", "account_id", "regime", "model_id",
    "baseline_params", "candidate_params", "evidence", "status", "tier", "reason",
    "created_at", "updated_at",
)


class SelectionFactContractError(ValueError):
    """typed selection fact 读侧的 **fail closed** 拒绝。

    JSON 坏掉、必需归一列缺失、``updated_at`` 不是可解析的**带时区**瞬间 —— 一律抛这个
    错误，而不是回落到 ``{}`` / ``run_date`` / ``created_at`` / ``now()``。把"读不出来"
    伪装成"没有候选"或"还在等数据"，正是数据损坏变成业务结论的路径。
    """


def _owner_instant(value: Any, *, what: str) -> dt.datetime:
    """owner 的 revision 瞬间 —— 必须显式、可解析、**带时区**。

    naive 时间戳一律拒绝：``updated_at`` 是可用性的唯一 authority，用本地时区去猜它就等于
    猜"这条事实什么时候可见"，而那正是 PIT 泄漏的入口。
    """
    text = str(value or "").strip()
    if not text:
        raise SelectionFactContractError(f"{what} is required for a typed selection fact")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise SelectionFactContractError(
            f"{what} is not a parsable owner instant: {text!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise SelectionFactContractError(
            f"{what} must be timezone-aware; got naive {text!r} — "
            "无法证明可用瞬间，禁止用本地时区猜"
        )
    return parsed


def _owner_availability_day(instant: dt.datetime, *, what: str) -> str:
    """owner 时区归一后的业务日。

    与 news adapter 同一条规则：**不接受**原始 offset 的日期。
    ``2026-09-20T16:30+00:00`` 在上海已经是 9/21 00:30，业务日必须是 9/21。
    """
    if OWNER_TZ is None:  # pragma: no cover - 只在 tzdata 缺失时
        raise SelectionFactContractError(
            f"{what}: owner timezone (Asia/Shanghai) unavailable — "
            "拒绝在无时区定义的前提下派生业务日"
        )
    return instant.astimezone(OWNER_TZ).date().isoformat()


def _business_day(value: Any, *, what: str) -> str:
    """显式、canonical 的 ``YYYY-MM-DD`` 业务日。**没有默认值，也不看今天。**"""
    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise SelectionFactContractError(
            f"{what} requires an explicit canonical YYYY-MM-DD business day; got {value!r}"
        ) from exc
    if parsed.isoformat() != text:
        raise SelectionFactContractError(f"{what} is not canonical: {value!r}")
    return text


def _strict_mapping(value: Any, *, what: str) -> dict:
    """严格 JSON parse + 顶层必须是 object。

    坏 JSON / 空值 → fail closed。**绝不** ``_loads(value, {})``：在 typed evidence path
    上把损坏内容静默变成空对象，就是让"读不出来"冒充"是一个空事实"。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SelectionFactContractError(f"{what} is empty — 缺内容不是空对象")
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise SelectionFactContractError(f"{what} is not parsable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SelectionFactContractError(
            f"{what} must parse to a JSON object; got {type(parsed).__name__}"
        )
    return parsed


def _canonical_json(value: Any, *, what: str) -> str:
    """确定性 canonical 文本。NaN / Inf → fail closed（不可审计的内容不进事实）。"""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SelectionFactContractError(f"{what} is not canonically serializable: {exc}") from exc


def _canonical_params(value: Any, *, what: str) -> str:
    return _canonical_json(_strict_mapping(value, what=what), what=what)


def _selection_fact_verification_status(
    *, account_id: str, model_id: str, lifecycle_status: str, tier: str,
) -> str:
    """owner 自洽性 → 极小 factual verification 闭集。

    判据**只**回答"这条记录是不是 owner 自己签发的事实"，由**词汇归属**构成：owner 的
    account / model / lifecycle / tier 词表只有 owner 自己的 emitter 会产出。四项里任何
    一项落在 owner 词表之外，说明这行不是本 owner 签发的（legacy 行、别处写入的行、或用了
    未登记词的新行），于是归 ``unproven``。

    **刻意不看** ``status`` 的业务含义：``eligible_auto_adjust`` 不比 ``waiting_data``
    更"核验通过"，``applied`` 也不等于"已验证"。lifecycle 只参与"这是不是 owner 的词汇"
    这一个问题，不参与"这条候选值不值得晋级"。

    已知限制（与 R27 其余 owner 一致，不声称已关闭）：词汇归属证明的是 **contract-issued**，
    不是 **physical database origin**。调用方仍可自造 SQLite fixture 写入恰好合法词汇的行。
    见 ``docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md`` 的 OPEN / REQUIRED 条目。
    """
    if not (account_id and model_id and lifecycle_status and tier):
        return SELECTION_FACT_OWNER_UNPROVEN
    if account_id not in ACCOUNT_MODELS:
        return SELECTION_FACT_OWNER_UNPROVEN
    if model_id not in (ACCOUNT_ALLOWED_MODELS.get(account_id) or set()):
        return SELECTION_FACT_OWNER_UNPROVEN
    if lifecycle_status not in SELECTION_LIFECYCLE_STATUSES:
        return SELECTION_FACT_OWNER_UNPROVEN
    if tier not in SELECTION_TIERS:
        return SELECTION_FACT_OWNER_UNPROVEN
    return SELECTION_FACT_RECORDED


@dataclasses.dataclass(frozen=True)
class AdaptiveSelectionFactProjection:
    """**selection owner 自己签发**的候选事实投影 —— 一次不可变快照。

    它携带的每一列都直接来自 owner 的 durable 归一列，没有任何一列由调用方提供：

    ``identity`` / ``revision_identity``
        行是**可变**的，所以 ``id`` 不是 revision identity。identity 由
        ``<candidate_id>@<revision_at>`` 构成 —— ``revision_at`` 是 owner 在每次改写时
        都会推进的 ``updated_at``。内容一变，identity 就变，因此同一个 ``source_id``
        不会在内容变化后指向两条不同的事实（那会让冲突检测失效）。

    ``availability_day``
        **从 ``revision_at`` 派生的 owner 时区业务日**，不是 ``run_date``。``run_date``
        是"这次评估关于哪一天"的标签；当前行内容在 ``updated_at`` 之前并不存在。

    ``fact_verification_status``
        owner 签发的极小闭集（见 :data:`SELECTION_FACT_VERIFICATION_STATUSES`）。
        它回答"这是不是一条 owner 自洽签发的候选事实"，
        **不**回答"这条候选是否通过晋级验证"。

    ``content_fingerprint``
        构造期确定性计算（sha256 over canonical JSON），因此即使调用方保留了内部容器的
        引用也无法改写它。指纹覆盖 record kind / identity / revision / availability /
        核验状态与全部事实内容。

    已知限制：dataclass 是可构造的，所以"手工造投影 → adapter"这条两步伪造路径在本层
    **只**被限制在"identity 只能由候选列派生、指纹只能是内容的函数"，而**不是**被证明
    关闭。physical database provenance 仍是 OPEN / REQUIRED。
    """

    version: str
    record_kind: str
    candidate_id: int
    account_id: str
    run_date: str
    regime: str
    model_id: str
    baseline_params_canonical: str
    candidate_params_canonical: str
    evidence_canonical: str
    lifecycle_status: str
    tier: str
    reason: str
    revision_at: str
    availability_day: str
    created_at: str
    fact_verification_status: str
    content_fingerprint: str = ""

    def __post_init__(self) -> None:
        record_kind = str(self.record_kind or "").strip()
        if record_kind != SELECTION_CANDIDATE_RECORD_KIND:
            raise SelectionFactContractError(
                f"unknown selection fact record_kind: {record_kind!r}; "
                f"allowed: {SELECTION_CANDIDATE_RECORD_KIND!r}"
            )
        object.__setattr__(self, "record_kind", record_kind)
        object.__setattr__(
            self, "version",
            str(self.version or "").strip() or SELECTION_FACT_CONTRACT_VERSION,
        )
        if isinstance(self.candidate_id, bool) or not isinstance(self.candidate_id, int):
            raise SelectionFactContractError(
                f"selection candidate_id must be an int, got {self.candidate_id!r}"
            )
        if self.candidate_id <= 0:
            raise SelectionFactContractError(
                f"selection candidate_id must be positive, got {self.candidate_id}"
            )
        for name in ("account_id", "regime", "model_id"):
            text = str(getattr(self, name) or "").strip()
            if not text:
                raise SelectionFactContractError(f"selection fact requires {name}")
            object.__setattr__(self, name, text)
        object.__setattr__(
            self, "run_date", _business_day(self.run_date, what="selection fact run_date"),
        )
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "lifecycle_status", str(self.lifecycle_status or "").strip(),
        )
        object.__setattr__(self, "tier", str(self.tier or "").strip())

        revision = _owner_instant(self.revision_at, what="selection fact revision_at")
        object.__setattr__(self, "revision_at", revision.isoformat(timespec="seconds"))
        day = _business_day(self.availability_day, what="selection fact availability_day")
        expected = _owner_availability_day(revision, what="selection fact revision_at")
        if day != expected:
            raise SelectionFactContractError(
                f"selection fact availability_day {day} disagrees with the owner-timezone "
                f"day derived from revision_at ({expected}) — 业务日只能由 owner 从 "
                "revision 瞬间派生，不能由调用方指定"
            )
        if not str(self.created_at or "").strip():
            raise SelectionFactContractError("selection fact requires created_at")

        for name in (
            "baseline_params_canonical", "candidate_params_canonical", "evidence_canonical",
        ):
            text = str(getattr(self, name) or "")
            _strict_mapping(text, what=f"selection fact {name}")
            canonical = _canonical_json(
                _strict_mapping(text, what=f"selection fact {name}"),
                what=f"selection fact {name}",
            )
            if canonical != text:
                raise SelectionFactContractError(
                    f"selection fact {name} is not canonical — 事实内容必须能确定性重算指纹"
                )

        status = str(self.fact_verification_status or "").strip()
        if status not in SELECTION_FACT_VERIFICATION_STATUSES:
            raise SelectionFactContractError(
                f"unknown selection fact verification status: {status!r}; "
                f"allowed: {SELECTION_FACT_VERIFICATION_STATUSES}"
            )
        object.__setattr__(self, "fact_verification_status", status)

        # 指纹在**构造期**算出：调用方即使保留了内部容器的引用也改不动它。
        object.__setattr__(self, "content_fingerprint", self._fingerprint())

    # ---------- owner-derived identity ----------

    @property
    def revision_identity(self) -> str:
        """owner 的 revision identity —— ``<candidate_id>@<revision_at>``。"""
        return f"{self.candidate_id}@{self.revision_at}"

    @property
    def identity(self) -> tuple[str, str, str]:
        """``(record_kind, revision_identity, availability_day)``。"""
        return (self.record_kind, self.revision_identity, self.availability_day)

    # ---------- factual payload (parsed copies) ----------

    @property
    def baseline_params(self) -> dict:
        return json.loads(self.baseline_params_canonical)

    @property
    def candidate_params(self) -> dict:
        return json.loads(self.candidate_params_canonical)

    @property
    def evidence(self) -> dict:
        return json.loads(self.evidence_canonical)

    # ---------- fingerprint ----------

    def _fingerprint(self) -> str:
        payload = {
            "version": self.version,
            "record_kind": self.record_kind,
            "candidate_id": self.candidate_id,
            "account_id": self.account_id,
            "run_date": self.run_date,
            "regime": self.regime,
            "model_id": self.model_id,
            "baseline_params": self.baseline_params_canonical,
            "candidate_params": self.candidate_params_canonical,
            "evidence": self.evidence_canonical,
            "lifecycle_status": self.lifecycle_status,
            "tier": self.tier,
            "reason": self.reason,
            "revision_at": self.revision_at,
            "availability_day": self.availability_day,
            "created_at": self.created_at,
            "fact_verification_status": self.fact_verification_status,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def projection(self) -> dict:
        """给 API / 前端的稳定投影：只 render，不重算任何核验语义。"""
        return {
            "version": self.version,
            "record_kind": self.record_kind,
            "identity": self.revision_identity,
            "record_id": self.candidate_id,
            "account_id": self.account_id,
            "run_date": self.run_date,
            "regime": self.regime,
            "model_id": self.model_id,
            "lifecycle_status": self.lifecycle_status,
            "tier": self.tier,
            "reason": self.reason,
            "revision_at": self.revision_at,
            "availability_day": self.availability_day,
            "created_at": self.created_at,
            "fact_verification_status": self.fact_verification_status,
            "content_fingerprint": self.content_fingerprint,
            "authority": "owner_fact",
            "lifecycle_status_is_verification": False,
        }


def _selection_projection_from_row(item: dict, *, revision: dt.datetime, available_day: str):
    account_id = str(item.get("account_id") or "").strip()
    model_id = str(item.get("model_id") or "").strip()
    lifecycle = str(item.get("status") or "").strip()
    tier = str(item.get("tier") or "").strip()
    return AdaptiveSelectionFactProjection(
        version=SELECTION_FACT_CONTRACT_VERSION,
        record_kind=SELECTION_CANDIDATE_RECORD_KIND,
        candidate_id=int(item["id"]),
        account_id=account_id,
        run_date=str(item.get("run_date") or ""),
        regime=str(item.get("regime") or ""),
        model_id=model_id,
        baseline_params_canonical=_canonical_params(
            item.get("baseline_params"), what="selection candidate baseline_params",
        ),
        candidate_params_canonical=_canonical_params(
            item.get("candidate_params"), what="selection candidate candidate_params",
        ),
        evidence_canonical=_canonical_params(
            item.get("evidence"), what="selection candidate evidence",
        ),
        lifecycle_status=lifecycle,
        tier=tier,
        reason=str(item.get("reason") or ""),
        revision_at=revision.isoformat(timespec="seconds"),
        availability_day=available_day,
        created_at=str(item.get("created_at") or ""),
        fact_verification_status=_selection_fact_verification_status(
            account_id=account_id, model_id=model_id,
            lifecycle_status=lifecycle, tier=tier,
        ),
    )


def selection_candidate_fact(conn, candidate_id, *, as_of):
    """把一条 selection 候选行读成 typed owner fact。``as_of`` **必须显式**。

    **fail-closed 的历史语义。** 行是**可变**的，旧 revision 一旦被覆盖就不复存在。因此当
    当前 revision 的 ``availability_day`` 晚于 ``as_of`` 时，本函数返回 ``None``
    （UNAVAILABLE / not returned），而**不是**把当前行倒填进更早的 ``as_of`` ——
    那正是 look-ahead。``updated_at`` 是唯一的可用性 authority；``run_date`` 不是。

    刻意**没有**这些 fallback：

    * ``as_of=None`` → latest；
    * 取墙钟 / ``today()`` / ``now()``；
    * 取"当前 active candidate"或按 ``run_date`` 找最近一行。

    ``as_of`` 缺失或不是 canonical 业务日即 :class:`SelectionFactContractError`。行不存在
    返回 ``None``；行存在但内容损坏（坏 JSON / 缺列 / ``updated_at`` naive）则**抛错**，
    绝不用 ``{}`` / ``created_at`` / ``run_date`` 兜底。
    """
    day = _business_day(as_of, what="selection candidate fact as_of")
    cursor = conn.execute(
        "SELECT * FROM adaptive_selection_candidates WHERE id=?", (int(candidate_id),),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    # 行形状是**调用方**的事实，不是本读侧可以假定的前提：生产连接设了
    # ``row_factory = sqlite3.Row``，迁移 / 运维入口却是裸 tuple。两种都必须能读。
    item = EV._row_as_dict(cursor, row)
    missing = [name for name in _SELECTION_REQUIRED_COLUMNS if name not in item]
    if missing:
        raise SelectionFactContractError(
            f"selection candidate {candidate_id} is missing required normalized columns: "
            f"{missing}"
        )
    # 可用性判定**先于**内容解析：一条在 as_of 那天还不存在的 revision 的正确结论是
    # "拿不到"，而不是"损坏"。两者都 fail closed，但语义不同。
    revision = _owner_instant(
        item.get("updated_at"), what="selection candidate updated_at",
    )
    available_day = _owner_availability_day(
        revision, what="selection candidate updated_at",
    )
    if available_day > day:
        return None
    return _selection_projection_from_row(item, revision=revision, available_day=available_day)


def record_shadow_proposal(conn, *, run_date, account_id, regime, model_id,
                           baseline_params, candidate_params, evidence, reason, now):
    """owner 的**唯一窄接口**：持久化一条 AI 影子候选提案。

    这是 R27-B2C-6 writer 收敛的落点：``deepseek_advisor.run_realtime_tuning`` 不再自己拼
    ``INSERT INTO adaptive_selection_candidates``，而是调用本函数。DeepSeek 因此是
    **producer / caller**，``adaptive_selection`` 才是这张 ledger 的 owner。

    本接口**只**做 owner 持久化，刻意不做以下任何一件事：

    * 不调用 LLM、不决定 proposal 内容（内容由 caller 提出，本函数只校验）；
    * 不自动 apply、不写 paper account、不碰 outbox；
    * 不扩大 selection 权限 —— ``status`` 与 ``tier`` 由 **owner 独占决定**
      (:data:`SHADOW_PROPOSAL_STATUS` / :data:`AI_REALTIME_TIER`)，caller 无法传入。
      因为 ``shadow_proposal`` 不在 ``apply_candidate`` 的资格集合里，
      "AI 提案直接生效"在这里结构性不可表达，human apply 边界逐字未变。

    校验（fail closed，不合格即抛错而不是记一条坏候选）：

    * ``run_date`` 必须是 canonical 业务日；
    * ``account_id`` / ``model_id`` 必须落在 owner 自己的词表内；
    * ``candidate_params`` 必须是**纯因子权重**补丁（键集恰为 ``{"weights"}``），且单因子
      变化不超过既有的 ±3 个百分点边界。阈值 / 条件 / 入场路径一律在这里被拒绝，而不是
      被记成一个以后可能被 apply 的候选。**apply 边界仍然独立重新校验一次**：本函数校验的
      是提案自洽性，权威边界依旧是 :func:`apply_candidate` 与人工确认。

    **追加而非改写**：``(run_date, account_id, regime)`` 已存在时返回既有行 id，绝不改写
    它的生命周期 —— 提案不得把一条已 ``applied`` / ``rolled_back`` 的候选改回影子态。
    """
    ensure_schema(conn)
    day = _business_day(run_date, what="shadow proposal run_date")
    account = str(account_id or "").strip()
    if account not in ACCOUNT_MODELS:
        raise ValueError(f"shadow proposal account_id is not owner-managed: {account!r}")
    resolved_model = str(model_id or "").strip()
    if resolved_model not in (ACCOUNT_ALLOWED_MODELS.get(account) or set()):
        raise ValueError(
            f"shadow proposal model_id {resolved_model!r} is not allowed for account {account!r}"
        )
    regime_text = str(regime or "").strip()
    if not regime_text:
        raise ValueError("shadow proposal requires a non-empty regime")
    baseline = _strict_mapping(baseline_params, what="shadow proposal baseline_params")
    candidate = _strict_mapping(candidate_params, what="shadow proposal candidate_params")
    if set(candidate) != {"weights"}:
        raise ValueError(
            "shadow proposal candidate_params must be a factor-weights-only patch; "
            f"got keys {sorted(candidate)}"
        )
    if not _factor_only_patch(
        {"weights": candidate["weights"]}, {"weights": baseline.get("weights")}, resolved_model,
    ):
        raise ValueError(
            "shadow proposal must reuse the existing factor set and stay within the "
            "±3pp per-factor bound"
        )
    evidence_payload = _strict_mapping(evidence, what="shadow proposal evidence")
    # writer 落库的瞬间必须能被 owner 自己的 typed read 重新解析 —— 否则写进去的就是一条
    # 其 owner 读不出来的事实。
    opened = _owner_instant(now, what="shadow proposal now")
    now_text = opened.isoformat(timespec="seconds")

    existing = conn.execute(
        "SELECT id FROM adaptive_selection_candidates WHERE run_date=? AND account_id=? AND regime=?",
        (day, account, regime_text),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    cursor = conn.execute(
        """INSERT INTO adaptive_selection_candidates(
           run_date,account_id,regime,model_id,baseline_params,candidate_params,evidence,status,tier,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_date,account_id,regime) DO NOTHING""",
        (day, account, regime_text, resolved_model, _json(baseline), _json(candidate),
         _json(evidence_payload), SHADOW_PROPOSAL_STATUS, AI_REALTIME_TIER,
         str(reason or "")[:500], now_text, now_text),
    )
    if not cursor.rowcount:
        row = conn.execute(
            "SELECT id FROM adaptive_selection_candidates WHERE run_date=? AND account_id=? AND regime=?",
            (day, account, regime_text),
        ).fetchone()
        return int(row["id"])
    return int(cursor.lastrowid)
