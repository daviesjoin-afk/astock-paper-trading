# -*- coding: utf-8 -*-
"""Bounded evolution of paper-account candidate ranking models."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import statistics

import strategies as S
from adaptive_common import _loads, _json, _clamp  # C3: 收敛重复工具函数

ACCOUNT_NAMES = {
    "tq_breakout": "短线日内做T",
    "trend_pullback": "趋势波段优选",
    "sector_rotation": "板块轮动先锋",
    "reported_profit_breakout": "三日策略",
    "main_force_top10": "超强主力股",
}
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
    closed = paper.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND side='sell' AND status='filled'", (account_id,)
    ).fetchone()[0]
    fills = paper.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status='filled'", (account_id,)
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
    baseline = _loads(item.get("baseline_params"), {})
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
            account_params["adaptive_selection"] = _merge_selection_overlay(
                account_params, candidate, current_weights, current_model,
            )
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
            paper.execute("INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
                          (item["account_id"], "adaptive_selection_applied",
                           f"candidate={candidate_id}; version={version}; effective={effective}", now_fn()))
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
            paper.execute("UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
                          (_json(previous), version, now_fn(), account_id))
            paper.execute(
                """INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (account.get("cycle_id"), account_id, version, account.get("style") or "adaptive-selection",
                 _json(previous), reason, effective, now_fn()),
            )
            paper.execute("INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
                          (account_id, "adaptive_selection_rolled_back",
                           f"candidate={candidate_id}; reason={reason}", now_fn()))
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
        "policy": "三套模拟账户可进化因子权重、入场阈值和白名单选股条件；板块热度不足时可提出个股强势路径，趋势模型可提出从抄底切换为趋势延续；结构变更先影子验证并人工确认，公共选股页面不受影响。",
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
