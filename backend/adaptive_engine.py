# -*- coding: utf-8 -*-
"""Auditable market-profile and strategy-evolution engine.

The engine learns from completed paper-ledger observations only.  It never
submits orders.  Strategy allocation stays advisory; risk overlays may only be
promoted after evidence gates pass, with automatic promotion restricted to
conservative tightening and every version remaining auditable and reversible.
"""
from __future__ import annotations

import datetime as dt
import csv
import hashlib
import gc
import json
import math
import os
import random
import sqlite3
import statistics
import threading
import time
from bisect import bisect_right as _bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import adaptive_risk as risk_evolution
import adaptive_selection as selection_evolution
import deepseek_advisor
import deepseek_research
import news_learning
import risk_center as paper_risk_center
import trade_attribution
import ai_analysis
import factor_quality_shadow
import neural_shadow
import evolution_adversarial as adversarial
import dual_ai_tuner
import self_evolution
import paper_ledger_reader as paper_reader
import adaptive_genetics as AG
from strategy_registry import labels as strategy_labels
from adaptive_common import _now, _json, _loads, _clamp  # C3: 收敛重复工具函数
try:
    import modlens_bridge
except ImportError:
    pass


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data_cache")
DB_PATH = os.path.join(CACHE_DIR, "adaptive_learning.sqlite3")
PAPER_DB_PATH = os.path.join(CACHE_DIR, "paper_trading.sqlite3")
SNAPSHOT_PATHS = (
    os.path.join(CACHE_DIR, "market_snapshot_full.json"),
    os.path.join(CACHE_DIR, "market_snapshot.json"),
)
TZ = ZoneInfo("Asia/Shanghai")

# 学习循环死亡可观测（A3）：心跳文件 + adaptive_runs 悬挂检测，让 SIGKILL 从"无痕"变"有痕"
LEARNING_HEARTBEAT = os.path.join(CACHE_DIR, ".learning_cycle.heartbeat")
# A crashed/manual run should not block the next scheduled cycle for hours.
# Two hours exceeds the normal close-learning runtime while allowing prompt
# recovery after OOM/restart; completed rows remain untouched.
_LEARNING_STALE_HOURS = 2


def _learning_heartbeat_write(started_at: str) -> None:
    try:
        with open(LEARNING_HEARTBEAT, "w", encoding="utf-8") as _f:
            _f.write(started_at)
            _f.flush()
    except Exception:
        pass


def _learning_heartbeat_clear() -> None:
    try:
        os.remove(LEARNING_HEARTBEAT)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _learning_detect_stale(conn) -> int:
    """把超过阈值仍 running 的残运行标记为 killed，返回被标记数量。"""
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(hours=_LEARNING_STALE_HOURS)).isoformat()
    try:
        cur = conn.execute(
            "UPDATE adaptive_runs SET status='killed', detail=? WHERE status='running' AND started_at < ?",
            (_json({"error": "process killed (OOM/restart) before completion; no finished_at"}), cutoff),
        )
        return cur.rowcount
    except Exception:
        return 0


def _learning_update_stage(run_id, stage, **detail) -> None:
    """Persist progress before a potentially expensive stage starts."""
    if not run_id:
        return
    payload = {"stage": str(stage), **detail}
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE adaptive_runs SET detail=? WHERE id=? AND status='running'",
                (_json(payload), int(run_id)),
            )
    except Exception:
        pass
ENGINE_VERSION = "adaptive-bandit-v2-recency"
EVIDENCE_ENGINE_VERSION = "closed-loop-evidence-v1"
CANARY_MAX_NAV_PCT = 10.0
CANARY_MAX_NEW_SLOTS = 2
HORIZON_WEIGHTS = {1: 0.20, 3: 0.35, 5: 0.45}
ALPHA_FEATURES = ("price_momentum", "main_flow", "turnover", "volume_ratio", "small_size", "value")
ALPHA_MIN_PROFILE_DAYS = 10
ALPHA_MIN_MATURE_ROWS = 5000
ALPHA_MAX_ROWS_PER_WINDOW = max(200, int(os.getenv("ALPHA_MAX_ROWS_PER_WINDOW", "1200")))
DISCLOSURE_MAX_CODES = 150
DISCLOSURE_CACHE_TTL_SECONDS = 6 * 60 * 60
DISCLOSURE_REFRESH_AFTER = dt.time(15, 15)
ACCOUNT_LABELS = strategy_labels()
REGIME_LABELS = {
    "momentum": "资金共振·动量扩张",
    "rotation": "板块轮动·结构分化",
    "risk_off": "风险收缩·资金退潮",
    "high_volatility": "高波动·拥挤博弈",
    "balanced": "均衡震荡·等待确认",
    "unclassified": "历史样本·画像缺失",
}
_RUN_LOCK = threading.Lock()
# The adaptive page is a read model.  Its expensive evidence aggregation must
# never fan out to source/network work for every browser refresh.
_OVERVIEW_CACHE_TTL_SECONDS = 45.0
_OVERVIEW_CACHE = {"data": None, "ts": 0.0}
_OVERVIEW_CACHE_LOCK = threading.Lock()
_DISCLOSURE_STATE_CACHE = {"day": None, "data": None, "ts": 0.0}
_DISCLOSURE_STATE_LOCK = threading.Lock()
_SHADOW_READ_CACHE: dict[str, dict] = {}
_SHADOW_READ_LOCK = threading.Lock()

# Self-evolution is an evidence-producing, shadow-only subsystem.  No value
# loaded from the adaptive config database can opt an unattended process into
# applying AI, GA, selection, or risk proposals.  Promotion remains available
# only through the explicit human apply endpoints below.
_AUTO_APPLY_KEYS = (
    "risk_auto_apply_conservative",
    "selection_auto_apply_bounded",
    "llm_realtime_auto_apply",
)
_SHADOW_POLICY = "shadow_only"


AI_SETTINGS_KEYS = {
    "llm_advisor_enabled",
    "llm_provider", "llm_realtime_tuning_enabled",
    "llm_realtime_auto_apply", "llm_realtime_require_cross_source",
    "llm_realtime_min_interval_minutes", "llm_realtime_min_valid_rows",
    "llm_realtime_mode",
    "dual_ai_enabled",
    "dual_ai_require_consensus",
}

AI_SETTINGS_META = {
    "llm_provider": {"label": "AI供应商", "description": "选择实际调用的模型供应商；当前默认 DeepSeek，Kimi/MIMO 先作为兼容预留，不会自动启用。", "effect": "决定后端使用哪套 API 凭据、地址和模型默认值。", "recommended": "deepseek", "risk": "供应商切换可能带来模型能力、费用和返回格式差异。"},
    "llm_advisor_enabled": {"label": "启用 DeepSeek 审阅", "description": "允许 AI 对数据质量、盈亏归因和异常证据进行解释。", "effect": "只生成审阅报告，不直接下单。", "recommended": False, "risk": "需要外部 API 调用，不能把 AI 结论当作行情真实性证明。"},
    "llm_realtime_tuning_enabled": {"label": "启用实时有界调参", "description": "允许 AI 基于模拟盘证据生成白名单范围内的参数候选。", "effect": "候选仍需确定性门禁、样本和跨源校验。", "recommended": True, "risk": "关闭后只保留确定性进化，不生成 AI 候选。"},
    "llm_realtime_auto_apply": {"label": "允许候选自动应用", "description": "是否允许通过全部门禁的 AI 候选在模拟盘内同日应用。", "effect": "关闭时仅保存 shadow_proposal，便于人工复核。", "recommended": False, "risk": "开启会减少人工确认，建议长期保持关闭或仅在充分验证后开启。"},
    "llm_realtime_require_cross_source": {"label": "必须通过双源校验", "description": "要求主行情与第二独立行情源对关键数据完成一致性校验。", "effect": "未 verified 时阻断 AI 调参。", "recommended": True, "risk": "第二源不可用时会降低调参频率，但可避免脏数据驱动调整。"},
    "llm_realtime_mode": {"label": "调参模式", "description": "shadow 只观察，intraday 允许盘中候选，close 只在收盘后运行。", "effect": "控制候选的生效时机，不改变风控硬上限。", "recommended": "intraday", "risk": "intraday 对数据时效要求更高；close 反应更慢但更稳。"},
    "llm_realtime_min_interval_minutes": {"label": "调参冷却时间（分钟）", "description": "两次 AI 调参之间的最短间隔。", "effect": "减少重复调用和短期噪声追随。", "recommended": 15, "risk": "过短增加成本和过拟合，过长可能错过环境变化。"},
    "llm_realtime_min_valid_rows": {"label": "最少有效行情行数", "description": "允许 AI 调参前，行情快照必须达到的有效记录数。", "effect": "作为数据质量门槛之一。", "recommended": 1000, "risk": "过低会让不完整快照进入调参链，过高会导致小样本时长期阻断。"},
}

def ai_settings_schema():
    import deepseek_advisor as _advisor
    return {"settings": ai_settings(), "parameters": AI_SETTINGS_META, "providers": _advisor.provider_catalog()}

def ai_settings():
    with _connect() as conn:
        cfg = _config(conn)
    return {key: cfg.get(key) for key in sorted(AI_SETTINGS_KEYS)}

def update_ai_settings(updates):
    if not isinstance(updates, dict):
        raise ValueError("AI调参设置必须是对象")
    unknown = sorted(set(updates) - AI_SETTINGS_KEYS)
    if unknown:
        raise ValueError("不允许修改的AI设置: " + ",".join(unknown))
    checked = {}
    bool_keys = {"llm_advisor_enabled", "llm_realtime_tuning_enabled", "llm_realtime_auto_apply", "llm_realtime_require_cross_source"}
    for key, value in updates.items():
        if key in bool_keys:
            if not isinstance(value, bool): raise ValueError(f"{key}必须是布尔值")
            if key == "llm_realtime_auto_apply" and value:
                raise ValueError("人工审批已启用，AI候选不允许自动应用")
            checked[key] = value
        elif key == "llm_provider":
            if value not in {"deepseek", "kimi", "mimo"}: raise ValueError("AI供应商只支持 deepseek、kimi、mimo")
            if value == "kimi": raise ValueError("Kimi接口已预留，当前版本暂不启用")  # 允许 deepseek 和 mimo
            checked[key] = value
        elif key == "llm_realtime_mode":
            if value not in {"shadow", "intraday", "close"}: raise ValueError("调参模式只支持 shadow、intraday、close")
            checked[key] = value
        elif key == "llm_realtime_min_interval_minutes":
            try: checked[key] = max(5, min(240, int(value)))
            except (TypeError, ValueError): raise ValueError("冷却时间必须是5-240的整数")
        elif key == "llm_realtime_min_valid_rows":
            try: checked[key] = max(100, min(10000, int(value)))
            except (TypeError, ValueError): raise ValueError("有效行情行数必须是100-10000的整数")
    with _connect() as conn:
        now = _now()
        for key, value in checked.items():
            conn.execute("INSERT INTO adaptive_config(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, _json(value), now))
        cfg = _config(conn)
    return {key: cfg.get(key) for key in sorted(AI_SETTINGS_KEYS)}


def _load_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def _num(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _background_shadow_read(key: str, ttl_seconds: float, loader, placeholder: dict) -> dict:
    """Never block a UI read on a research-only scan.

    Portfolio stress and factor-quality reports are advisory.  They are
    refreshed in a daemon worker and returned from the last coherent snapshot;
    no placeholder can influence the trading engine.
    """
    now = time.time()
    with _SHADOW_READ_LOCK:
        item = _SHADOW_READ_CACHE.get(key)
        if item and item.get("data") is not None and now - item.get("ts", 0.0) < ttl_seconds:
            return item["data"]
        if item and item.get("running"):
            return item.get("data") or {**placeholder, "status": "refreshing"}
        item = item or {"data": None, "ts": 0.0, "running": False}
        item["running"] = True
        _SHADOW_READ_CACHE[key] = item

    def worker() -> None:
        try:
            data = loader()
        except Exception as exc:
            data = {**placeholder, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        with _SHADOW_READ_LOCK:
            current = _SHADOW_READ_CACHE.setdefault(key, {})
            current.update(data=data, ts=time.time(), running=False)

    threading.Thread(target=worker, name=f"adaptive-{key}-refresh", daemon=True).start()
    return {**placeholder, "status": "refreshing"}


def _median(values, default=0.0):
    clean = [_num(value) for value in values]
    clean = [value for value in clean if value is not None]
    return statistics.median(clean) if clean else default


@contextmanager
def _connect():
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        _init_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_schema(conn):
    # 快速路径：核心表已存在时跳过 200 行 executescript 的重复解析。
    # executescript 全部为 CREATE IF NOT EXISTS / INSERT OR IGNORE（幂等），
    # 跳过不会改变任何数据，只是避免每个会话/请求都重新解析大段 DDL。
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_rewards'"
        ).fetchone()
        if exists:
            # 仍需保证 canary 种子与 6 个模块的表存在（新部署/升级时）
            conn.execute(
                """INSERT OR IGNORE INTO adaptive_canary_state(
                   id,mode,stage,max_nav_pct,max_new_slots,effective_at,frozen_reason,evidence,updated_at)
                   VALUES(1,'shadow','D1-D3',0,0,NULL,'等待证据链验收','{}',?)""",
                (_now(),),
            )
            risk_evolution.ensure_schema(conn)
            selection_evolution.ensure_schema(conn)
            deepseek_advisor.ensure_schema(conn)
            news_learning.ensure_schema(conn)
            trade_attribution.ensure_schema(conn)
            ai_analysis.ensure_schema(conn)
            _ensure_config_defaults(conn)
            return
    except Exception:
        pass  # 表不存在/库损坏 → 走完整建表路径
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adaptive_market_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_date TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL,
            source_at TEXT,
            regime TEXT NOT NULL,
            quality TEXT NOT NULL,
            valid_rows INTEGER NOT NULL DEFAULT 0,
            features TEXT NOT NULL,
            drivers TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_intraday_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_date TEXT NOT NULL,
            session TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_at TEXT,
            regime TEXT NOT NULL,
            quality TEXT NOT NULL,
            valid_rows INTEGER NOT NULL DEFAULT 0,
            features TEXT NOT NULL,
            drivers TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(profile_date,session)
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_intraday_profiles_date
            ON adaptive_intraday_profiles(profile_date,session);
        CREATE TABLE IF NOT EXISTS adaptive_intraday_samples(
            profile_date TEXT NOT NULL,
            session TEXT NOT NULL,
            code TEXT NOT NULL,
            payload TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(profile_date,session,code)
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_intraday_samples_date
            ON adaptive_intraday_samples(profile_date,session);
        CREATE TABLE IF NOT EXISTS adaptive_rewards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            regime TEXT NOT NULL,
            strategy_return_pct REAL NOT NULL,
            benchmark_return_pct REAL NOT NULL,
            excess_return_pct REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            turnover_pct REAL NOT NULL,
            raw_reward REAL NOT NULL,
            weighted_reward REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(account_id,horizon,start_date,end_date)
        );
        CREATE TABLE IF NOT EXISTS adaptive_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_date TEXT NOT NULL,
            profile_id INTEGER,
            regime TEXT NOT NULL,
            mode TEXT NOT NULL,
            stage TEXT NOT NULL,
            weights TEXT NOT NULL,
            scores TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(decision_date,regime,mode)
        );
        CREATE TABLE IF NOT EXISTS adaptive_feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            profile_date TEXT,
            new_rewards INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_config(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_alpha_samples(
            profile_date TEXT NOT NULL,
            code TEXT NOT NULL,
            industry TEXT,
            close_price REAL NOT NULL,
            regime TEXT NOT NULL,
            price_momentum REAL NOT NULL,
            main_flow REAL NOT NULL,
            turnover REAL NOT NULL,
            volume_ratio REAL NOT NULL,
            small_size REAL NOT NULL,
            value REAL NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(profile_date,code)
        );
        CREATE TABLE IF NOT EXISTS adaptive_alpha_returns(
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            code TEXT NOT NULL,
            forward_return_pct REAL NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(start_date,end_date,horizon,code)
        );
        CREATE TABLE IF NOT EXISTS adaptive_alpha_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            generation INTEGER NOT NULL,
            genome TEXT NOT NULL,
            train_fitness REAL NOT NULL,
            validation_fitness REAL NOT NULL,
            validation_spread_pct REAL NOT NULL,
            profile_days INTEGER NOT NULL,
            mature_rows INTEGER NOT NULL,
            status TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_alpha_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            profile_days INTEGER NOT NULL,
            mature_rows INTEGER NOT NULL,
            generations INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_evidence_chains(
            chain_id TEXT PRIMARY KEY,
            ledger_type TEXT NOT NULL,
            origin TEXT NOT NULL,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            side TEXT,
            signal_id INTEGER,
            risk_decision_id INTEGER,
            order_id INTEGER,
            fill_id INTEGER,
            order_status TEXT,
            signal_date TEXT,
            decision_at TEXT,
            order_at TEXT,
            fill_at TEXT,
            feature_version TEXT,
            parameter_version TEXT,
            snapshot_at TEXT,
            integrity_status TEXT NOT NULL,
            integrity_flags TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_adaptive_evidence_order
            ON adaptive_evidence_chains(order_id) WHERE order_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_adaptive_evidence_account
            ON adaptive_evidence_chains(account_id,order_at DESC);
        CREATE TABLE IF NOT EXISTS adaptive_canary_state(
            id INTEGER PRIMARY KEY CHECK(id=1),
            mode TEXT NOT NULL,
            stage TEXT NOT NULL,
            max_nav_pct REAL NOT NULL,
            max_new_slots INTEGER NOT NULL,
            effective_at TEXT,
            frozen_reason TEXT,
            evidence TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_execution_evidence(
            evidence_date TEXT NOT NULL,
            metric TEXT NOT NULL,
            status TEXT NOT NULL,
            value REAL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(evidence_date, metric)
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_execution_evidence_metric
            ON adaptive_execution_evidence(metric, evidence_date DESC);
        """
    )
    conn.execute(
        """INSERT OR IGNORE INTO adaptive_canary_state(
           id,mode,stage,max_nav_pct,max_new_slots,effective_at,frozen_reason,evidence,updated_at)
           VALUES(1,'shadow','D1-D3',0,0,NULL,'等待证据链验收','{}',?)""",
        (_now(),),
    )
    risk_evolution.ensure_schema(conn)
    selection_evolution.ensure_schema(conn)
    deepseek_advisor.ensure_schema(conn)
    news_learning.ensure_schema(conn)
    trade_attribution.ensure_schema(conn)
    ai_analysis.ensure_schema(conn)
    _ensure_config_defaults(conn)


def _ensure_config_defaults(conn):
    """幂等写入默认配置（INSERT OR IGNORE），供快速/完整建表路径共用。"""
    defaults = {
        "mode": "shadow",
        "human_approval_required": True,
        "min_samples_shadow": 6,
        "min_samples_advisory": 12,
        "min_samples_eligible": 30,
        "min_regimes_advisory": 2,
        "max_strategy_weight_pct": 60.0,
        "min_strategy_weight_pct": 15.0,
        "max_drawdown_guardrail_pct": 8.0,
        "exploration_strength": 0.65,
        # Kept as explicit policy keys for backwards-compatible UI/config
        # rendering, but fail closed: unattended evolution is shadow-only.
        "risk_auto_apply_conservative": False,
        "risk_shadow_nav_days": 1,
        "risk_shadow_trade_events": 1,
        "risk_shadow_reward_samples": 2,
        "risk_shadow_regimes": 1,
        "risk_fast_nav_days": 3,
        "risk_fast_trade_events": 2,
        "risk_fast_reward_samples": 4,
        "risk_fast_regimes": 1,
        "risk_standard_nav_days": 5,
        "risk_standard_trade_events": 4,
        "risk_standard_reward_samples": 6,
        "risk_standard_regimes": 2,
        "risk_mature_nav_days": 10,
        "risk_mature_trade_events": 8,
        "risk_mature_reward_samples": 12,
        "risk_mature_regimes": 2,
        "llm_advisor_enabled": False,
        # 盘后逐笔归因默认开启确定性记录；配置了 DeepSeek 密钥时再做 AI
        # 批量解释，密钥/网络不可用不会阻断学习链。
        "trade_attribution_ai_enabled": True,
        # DeepSeek 只在模拟盘内对已知权重、入场阈值和条件做有界建议；
        # 同日自动生效仍受质量、跨源和冷却门禁约束。
        "llm_realtime_tuning_enabled": True,
        "llm_realtime_auto_apply": False,
        "llm_weight_auto_apply": False,
        "llm_realtime_min_interval_minutes": 15,
        "llm_realtime_min_valid_rows": 1000,
        "llm_realtime_require_cross_source": True,
        "llm_realtime_max_accounts": 3,
        "selection_auto_apply_bounded": False,
        "selection_shadow_nav_days": 1,
        "selection_shadow_trade_events": 1,
        "selection_shadow_reward_samples": 2,
        "selection_shadow_regimes": 1,
        "selection_fast_nav_days": 3,
        "selection_fast_trade_events": 2,
        "selection_fast_reward_samples": 4,
        "selection_fast_regimes": 1,
        "selection_standard_nav_days": 5,
        "selection_standard_trade_events": 4,
        "selection_standard_reward_samples": 6,
        "selection_standard_regimes": 2,
        "selection_mature_nav_days": 10,
        "selection_mature_trade_events": 8,
        "selection_mature_reward_samples": 12,
        "selection_mature_regimes": 2,
        # 神经网络只允许在满足样本外门槛后由人工确认进入有界影子排序；
        # 缺省关闭影响，绝不自动放权。
        "neural_network_approved": False,
        "neural_shadow_enabled": True,
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO adaptive_config(key,value,updated_at) VALUES(?,?,?)",
            (key, _json(value), _now()),
        )


def _config(conn):
    result = {}
    for row in conn.execute("SELECT key,value FROM adaptive_config"):
        try:
            result[row["key"]] = json.loads(row["value"])
        except (ValueError, TypeError):
            result[row["key"]] = row["value"]
    # Existing installations may have persisted the old opt-out defaults.
    # Normalize them at every read so a stale database/config cannot silently
    # reactivate an apply path.  Human apply wrappers do not use these flags.
    for key in _AUTO_APPLY_KEYS:
        result[key] = False
    result["evolution_mode"] = _SHADOW_POLICY
    return result


# Apply/rollback touches two independent SQLite files.  The evolution modules
# commit the paper-account write before the adaptive ledger transaction exits;
# a later ledger/schema/commit failure would otherwise leave the two stores at
# different versions.  These small snapshots let the public engine boundary
# compensate both sides without changing any order or candidate rules.
_EVOLUTION_TABLES = {
    "risk": ("adaptive_risk_candidates", "adaptive_risk_events", "adaptive_risk_deployments"),
    "selection": ("adaptive_selection_candidates", "adaptive_selection_events", None),
}


def _candidate_snapshot(kind, candidate_id):
    tables = _EVOLUTION_TABLES[kind]
    with _connect() as conn:
        candidate = conn.execute(
            f"SELECT * FROM {tables[0]} WHERE id=?", (int(candidate_id),)
        ).fetchone()
        if not candidate:
            return None
        events = [dict(row) for row in conn.execute(
            f"SELECT * FROM {tables[1]} WHERE candidate_id=? ORDER BY id", (int(candidate_id),)
        )]
        deployments = []
        if tables[2]:
            deployments = [dict(row) for row in conn.execute(
                f"SELECT * FROM {tables[2]} WHERE candidate_id=? ORDER BY id", (int(candidate_id),)
            )]
    return {"candidate": dict(candidate), "events": events, "deployments": deployments}


def _restore_table_rows(conn, table, candidate_id, rows):
    """Restore candidate-linked audit rows after a failed cross-db commit."""
    if not table:
        return
    conn.execute(f"DELETE FROM {table} WHERE candidate_id=?", (int(candidate_id),))
    for row in rows:
        columns = list(row)
        placeholders = ",".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )


def _restore_candidate_snapshot(kind, snapshot):
    if not snapshot:
        return
    candidate_table, event_table, deployment_table = _EVOLUTION_TABLES[kind]
    candidate = snapshot["candidate"]
    candidate_id = int(candidate["id"])
    with _connect() as conn:
        current = conn.execute(
            f"SELECT id FROM {candidate_table} WHERE id=?", (candidate_id,)
        ).fetchone()
        if current:
            columns = [column for column in candidate if column != "id"]
            conn.execute(
                f"UPDATE {candidate_table} SET "
                + ",".join(f"{column}=?" for column in columns)
                + " WHERE id=?",
                tuple(candidate[column] for column in columns) + (candidate_id,),
            )
        else:
            columns = list(candidate)
            conn.execute(
                f"INSERT INTO {candidate_table} ({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(candidate[column] for column in columns),
            )
        _restore_table_rows(conn, event_table, candidate_id, snapshot["events"])
        _restore_table_rows(conn, deployment_table, candidate_id, snapshot["deployments"])


def _paper_account_snapshot(account_id):
    """Capture only the paper rows touched by an evolution apply/rollback."""
    if not os.path.exists(PAPER_DB_PATH):
        return None
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
    try:
        account = paper.execute(
            "SELECT id,params,version,updated_at FROM paper_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not account:
            return {"account": None, "versions": [], "versions_complete": False}
        versions = []
        versions_complete = True
        try:
            versions = [dict(row) for row in paper.execute(
                "SELECT id,version FROM paper_parameter_versions WHERE account_id=?", (account_id,)
            )]
        except sqlite3.Error:
            versions_complete = False
        return {"account": dict(account), "versions": versions, "versions_complete": versions_complete}
    finally:
        paper.close()


def _restore_paper_account(snapshot, account_id, candidate_id, event_name, expected_version=None):
    """Compensate a paper commit while preserving pre-existing audit history."""
    if not snapshot or snapshot.get("account") is None or not os.path.exists(PAPER_DB_PATH):
        return
    paper = sqlite3.connect(PAPER_DB_PATH, timeout=30)
    paper.row_factory = sqlite3.Row
    try:
        current = paper.execute(
            "SELECT version FROM paper_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not current:
            return
        # Do not overwrite a newer unrelated manual operation.  In normal use
        # the expected version is deterministic for apply and is read back for
        # rollback before this helper is called.
        if expected_version and str(current["version"] or "") != str(expected_version):
            # P3 审计修复（E5）：版本不一致时不再静默返回——旧路径 outbox
            # 已终结、候选已还原但 paper 侧未恢复，双账本分歧无任何告警。
            print(json.dumps({
                "alarm": "restore_version_mismatch",
                "account_id": account_id, "candidate_id": candidate_id,
                "expected_version": str(expected_version),
                "current_version": str(current["version"] or ""),
                "note": "补偿恢复被跳过：paper 账户版本与预期不符，需人工核对双账本",
            }, ensure_ascii=False), flush=True)
            return
        account = snapshot["account"]
        paper.execute(
            "UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
            (account.get("params"), account.get("version"), account.get("updated_at"), account_id),
        )
        # The failed operation's version is deterministic for apply and is
        # read from the account for rollback.  Remove only that version; never
        # delete unrelated parameter history that may have arrived meanwhile.
        existing_versions = {
            str(row.get("version") or "") for row in snapshot.get("versions", [])
        }
        if (snapshot.get("versions_complete", True) and expected_version
                and str(expected_version) not in existing_versions):
            try:
                paper.execute(
                    "DELETE FROM paper_parameter_versions WHERE account_id=? AND version=?",
                    (account_id, expected_version),
                )
            except sqlite3.Error:
                pass
        # Each evolution event includes the candidate id in its detail.  This
        # removes only rows created by the failed attempt, not prior audit data.
        # The pattern is anchored to the "candidate=<id>;" prefix (the exact
        # detail format) so candidate=12 cannot delete audit rows belonging to
        # candidate=123.
        try:
            paper.execute(
                "DELETE FROM paper_audit WHERE account_id=? AND event=? AND detail LIKE ?",
                (account_id, event_name, f"candidate={int(candidate_id)};%"),
            )
        except sqlite3.Error:
            pass
        paper.commit()
    finally:
        paper.close()


def _paper_current_version(account_id):
    if not os.path.exists(PAPER_DB_PATH):
        return None
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
    try:
        row = paper.execute("SELECT version FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        return row[0] if row else None
    finally:
        paper.close()


_MANUAL_APPLY_MAX_AGE_SECONDS = 30 * 60


def _aware_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _manual_apply_gate(kind, candidate_snapshot, account_id):
    """Revalidate a short-lived human candidate against current truth.

    Candidates are research artifacts, not standing orders.  Before a human
    click can change a paper account, require recent deterministic profile and
    snapshot data, a fresh independent-source check, a matching strategy
    version, and (when present) known disclosure timing.
    """
    if not candidate_snapshot:
        raise ValueError("进化候选不存在，拒绝应用")
    candidate = candidate_snapshot["candidate"]
    created = _aware_time(candidate.get("created_at") or candidate.get("updated_at"))
    now = dt.datetime.now(TZ)
    if created is None or (now - created).total_seconds() > _MANUAL_APPLY_MAX_AGE_SECONDS:
        raise ValueError("进化候选已超过30分钟有效期，请重新生成")
    if (created - now).total_seconds() > 60:
        raise ValueError("进化候选时间异常，拒绝应用")

    evidence = _loads(candidate.get("evidence"), {}) or {}
    candidate_version = str(evidence.get("strategy_version") or "")
    if not candidate_version:
        raise ValueError("候选缺少生成时的策略版本，请重新生成")
    with paper_reader.connect(PAPER_DB_PATH, timeout=30) as paper:
        row = paper.execute("SELECT version FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
    if not row or str(row[0] or "") != candidate_version:
        raise ValueError("策略版本已变化，请重新生成候选")

    profile_ok = False
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT profile_date,observed_at,source_at,quality,valid_rows
                     FROM adaptive_market_profiles ORDER BY updated_at DESC,id DESC LIMIT 1"""
            ).fetchone()
        observed = _aware_time(row["observed_at"]) if row else None
        profile_ok = bool(row and row["quality"] == "valid_close" and int(row["valid_rows"] or 0) >= 1000
                          and observed and (now - observed).total_seconds() <= 15 * 60)
    except (sqlite3.Error, TypeError, ValueError):
        profile_ok = False
    if not profile_ok:
        raise ValueError("当前市场数据画像未通过新鲜度/覆盖门禁")

    snapshot = _load_json(SNAPSHOT_PATHS[0], {}) or {}
    rows = snapshot.get("rows") if isinstance(snapshot, dict) else None
    saved = _aware_time(snapshot.get("saved_at")) if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or len(rows) < 1000 or saved is None or (now - saved).total_seconds() > 15 * 60:
        raise ValueError("当前全市场行情快照覆盖或新鲜度不足")

    # Re-run the same independent-source evidence check used by the AI gate;
    # it runs before opening the evolution write transaction.
    try:
        current_evidence, _ = deepseek_advisor.collect_evidence(
            _connect, PAPER_DB_PATH, SNAPSHOT_PATHS,
        )
        cross = ((current_evidence.get("market_snapshot") or {}).get("cross_source") or {})
        if str(cross.get("status") or "") != "verified":
            raise ValueError("当前双源行情校验未通过")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"当前双源行情校验失败: {type(exc).__name__}") from exc

    disclosure = evidence.get("disclosure") or evidence.get("disclosure_timeline")
    if disclosure is not None:
        coverage = _num(disclosure.get("coverage_pct"), 0.0) if isinstance(disclosure, dict) else 0.0
        if coverage < 85.0 or str(disclosure.get("status") or "").lower() in {"unknown", "stale", "unavailable"}:
            raise ValueError("财报披露时点覆盖不足，候选只能留在影子层")
    return {"strategy_version": candidate_version, "checked_at": _now(), "kind": kind}


def _latest_candidate_status(kind, account_id, status=None):
    table = _EVOLUTION_TABLES[kind][0]
    with _connect() as conn:
        where = "account_id=?"
        args = [account_id]
        if status:
            where += " AND status=?"
            args.append(status)
        return conn.execute(
            f"SELECT id,status FROM {table} WHERE {where} ORDER BY id DESC LIMIT 1",
            tuple(args),
        ).fetchone()


def _compensate_evolution(kind, candidate_snapshot, paper_snapshot, account_id,
                          candidate_id, event_name, expected_version=None):
    """Best-effort two-ledger compensation; preserve the original exception."""
    errors = []
    # Once the paper side is going to be restored, the durable intent must be
    # terminal as well.  Otherwise the next evolution cycle will replay the
    # operation that this compensation deliberately undid.
    if candidate_id:
        try:
            module = risk_evolution if kind == "risk" else selection_evolution
            with _connect() as conn:
                module.cancel_outbox(
                    conn, int(candidate_id), _now(),
                    reason=f"{event_name} 失败后的跨账本补偿",
                )
        except Exception as exc:
            errors.append(f"outbox:{type(exc).__name__}: {exc}")
    try:
        _restore_paper_account(
            paper_snapshot, account_id, candidate_id, event_name,
            expected_version=expected_version,
        )
    except Exception as exc:  # pragma: no cover - only exercised on storage failure
        errors.append(f"paper:{type(exc).__name__}: {exc}")
    try:
        _restore_candidate_snapshot(kind, candidate_snapshot)
    except Exception as exc:  # pragma: no cover - only exercised on storage failure
        errors.append(f"adaptive:{type(exc).__name__}: {exc}")
    return errors


def _snapshot_rows():
    for path in SNAPSHOT_PATHS:
        payload = _load_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            continue
        rows = payload["rows"]
        if rows:
            return rows, payload.get("saved_at"), os.path.basename(path)
    return [], None, None


def _profile_date(rows):
    dates = []
    for row in rows:
        quote_at = str(row.get("quote_at") or "")
        if len(quote_at) >= 10:
            dates.append(quote_at[:10])
    return Counter(dates).most_common(1)[0][0] if dates else dt.datetime.now(TZ).date().isoformat()


def _market_profile():
    rows, source_at, source_name = _snapshot_rows()
    profile_date = _profile_date(rows)
    same_day = [row for row in rows if str(row.get("quote_at") or "")[:10] == profile_date]
    valid = [
        row for row in same_day
        if _num(row.get("price"), 0) > 0 and _num(row.get("pct")) is not None
    ]
    pcts = [_num(row.get("pct"), 0.0) for row in valid]
    amounts = [_num(row.get("amount"), 0.0) for row in valid]
    turnovers = [_num(row.get("turnover"), 0.0) for row in valid]
    main_nets = [_num(row.get("main_net"), 0.0) for row in valid]
    breadth = 100.0 * sum(value > 0 for value in pcts) / len(pcts) if pcts else 0.0
    median_pct = _median(pcts)
    abs_move = _median([abs(value) for value in pcts])
    dispersion = statistics.pstdev(pcts) if len(pcts) > 1 else 0.0
    high_turnover = 100.0 * sum(value >= 8 for value in turnovers) / len(turnovers) if turnovers else 0.0
    upper_tail = 100.0 * sum(value >= 7 for value in pcts) / len(pcts) if pcts else 0.0
    lower_tail = 100.0 * sum(value <= -7 for value in pcts) / len(pcts) if pcts else 0.0
    positive_flow = 100.0 * sum(value > 0 for value in main_nets) / len(main_nets) if main_nets else 0.0
    total_amount = sum(max(value, 0.0) for value in amounts)
    total_main_net = sum(main_nets)
    flow_pressure = 100.0 * total_main_net / total_amount if total_amount > 0 else 0.0
    flow_abs = sorted((abs(value) for value in main_nets), reverse=True)
    flow_concentration = 100.0 * sum(flow_abs[:30]) / sum(flow_abs) if sum(flow_abs) > 0 else 0.0

    sectors = defaultdict(lambda: {"pcts": [], "amount": 0.0, "flow": 0.0, "leaders": 0})
    for row in valid:
        name = str(row.get("industry") or "未分类")
        pct = _num(row.get("pct"), 0.0)
        sectors[name]["pcts"].append(pct)
        sectors[name]["amount"] += max(_num(row.get("amount"), 0.0), 0.0)
        sectors[name]["flow"] += _num(row.get("main_net"), 0.0)
        sectors[name]["leaders"] += int(pct >= 5)
    sector_rows = []
    for name, data in sectors.items():
        if len(data["pcts"]) < 3:
            continue
        sector_rows.append({
            "name": name,
            "median_pct": round(_median(data["pcts"]), 3),
            "positive_ratio_pct": round(100.0 * sum(x > 0 for x in data["pcts"]) / len(data["pcts"]), 1),
            "main_net_yi": round(data["flow"] / 1e8, 2),
            "amount_yi": round(data["amount"] / 1e8, 1),
            "leader_count": data["leaders"],
            "sample_count": len(data["pcts"]),
        })
    sector_rows.sort(key=lambda item: (item["main_net_yi"], item["median_pct"]), reverse=True)
    sector_medians = [row["median_pct"] for row in sector_rows]
    sector_dispersion = statistics.pstdev(sector_medians) if len(sector_medians) > 1 else 0.0

    capital_score = _clamp(50 + flow_pressure * 8 + (positive_flow - 50) * 0.7, 0, 100)
    momentum_score = _clamp(50 + median_pct * 14 + (breadth - 50) * 0.8, 0, 100)
    volatility_score = _clamp(abs_move * 16 + dispersion * 11, 0, 100)
    sentiment_score = _clamp(50 + (breadth - 50) * 0.9 + median_pct * 10 + upper_tail * 1.5 - lower_tail * 2, 0, 100)
    crowding_score = _clamp(high_turnover * 2.4 + upper_tail * 4 + flow_concentration * 0.55, 0, 100)

    if breadth < 38 or median_pct < -0.8 or sentiment_score < 33:
        regime = "risk_off"
    elif breadth > 59 and median_pct > 0.45 and capital_score > 52:
        regime = "momentum"
    elif sector_dispersion > 1.35 and 38 <= breadth <= 65:
        regime = "rotation"
    elif volatility_score > 68 or crowding_score > 74:
        regime = "high_volatility"
    else:
        regime = "balanced"

    quality = "valid_close" if len(valid) >= 1000 and len(valid) / max(len(rows), 1) >= 0.75 else "degraded"
    drivers = [
        {"name": "资金动量", "score": round(capital_score, 1), "detail": f"主力净额/成交额 {flow_pressure:+.2f}% · 流入覆盖 {positive_flow:.1f}%"},
        {"name": "价格动量", "score": round(momentum_score, 1), "detail": f"上涨家数 {breadth:.1f}% · 中位涨跌 {median_pct:+.2f}%"},
        {"name": "波动结构", "score": round(volatility_score, 1), "detail": f"中位绝对波动 {abs_move:.2f}% · 截面离散 {dispersion:.2f}"},
        {"name": "情绪广度", "score": round(sentiment_score, 1), "detail": f"强势尾部 {upper_tail:.1f}% · 弱势尾部 {lower_tail:.1f}%"},
        {"name": "拥挤风险", "score": round(crowding_score, 1), "detail": f"高换手占比 {high_turnover:.1f}% · 头部资金集中 {flow_concentration:.1f}%"},
    ]
    features = {
        "source": source_name,
        "source_at": source_at,
        "profile_date": profile_date,
        "breadth_up_pct": round(breadth, 2),
        "median_pct": round(median_pct, 3),
        "market_amount_yi": round(total_amount / 1e8, 1),
        "main_net_yi": round(total_main_net / 1e8, 2),
        "main_flow_pressure_pct": round(flow_pressure, 3),
        "positive_main_flow_pct": round(positive_flow, 2),
        "median_abs_move_pct": round(abs_move, 3),
        "cross_section_dispersion": round(dispersion, 3),
        "sector_dispersion": round(sector_dispersion, 3),
        "high_turnover_ratio_pct": round(high_turnover, 2),
        "flow_concentration_pct": round(flow_concentration, 2),
        "capital_momentum_score": round(capital_score, 1),
        "price_momentum_score": round(momentum_score, 1),
        "volatility_score": round(volatility_score, 1),
        "sentiment_score": round(sentiment_score, 1),
        "crowding_score": round(crowding_score, 1),
        "top_sectors": sector_rows[:6],
        "weak_sectors": sorted(sector_rows, key=lambda item: (item["main_net_yi"], item["median_pct"]))[:6],
    }
    return {
        "profile_date": profile_date,
        "observed_at": _now(),
        "source_at": source_at,
        "regime": regime,
        "regime_label": REGIME_LABELS[regime],
        "quality": quality,
        "valid_rows": len(valid),
        "features": features,
        "drivers": drivers,
    }


def _store_profile(conn, profile):
    now = _now()
    conn.execute(
        """INSERT INTO adaptive_market_profiles(profile_date,observed_at,source_at,regime,quality,valid_rows,features,drivers,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(profile_date) DO UPDATE SET observed_at=excluded.observed_at,source_at=excluded.source_at,
             regime=excluded.regime,quality=excluded.quality,valid_rows=excluded.valid_rows,
             features=excluded.features,drivers=excluded.drivers,updated_at=excluded.updated_at""",
        (profile["profile_date"], profile["observed_at"], profile["source_at"], profile["regime"],
         profile["quality"], profile["valid_rows"], _json(profile["features"]), _json(profile["drivers"]), now, now),
    )
    return conn.execute(
        "SELECT id FROM adaptive_market_profiles WHERE profile_date=?", (profile["profile_date"],)
    ).fetchone()["id"]


def _store_intraday_profile(conn, profile, session="midday"):
    """保存盘中观测，绝不写入正式收盘画像或奖励样本。"""
    now = _now()
    conn.execute(
        """INSERT INTO adaptive_intraday_profiles(
           profile_date,session,observed_at,source_at,regime,quality,valid_rows,
           features,drivers,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(profile_date,session) DO UPDATE SET
             observed_at=excluded.observed_at,source_at=excluded.source_at,
             regime=excluded.regime,quality=excluded.quality,valid_rows=excluded.valid_rows,
             features=excluded.features,drivers=excluded.drivers,updated_at=excluded.updated_at""",
        (profile["profile_date"], session, profile["observed_at"], profile["source_at"],
         profile["regime"], profile["quality"], profile["valid_rows"],
         _json(profile["features"]), _json(profile["drivers"]), now, now),
    )
    return conn.execute(
        "SELECT id FROM adaptive_intraday_profiles WHERE profile_date=? AND session=?",
        (profile["profile_date"], session),
    ).fetchone()["id"]


def _store_intraday_samples(conn, profile, session="midday"):
    """保存午间逐股快照，作为盘中研究样本但不生成正式收益标签。"""
    rows, _, _ = _snapshot_rows()
    target_date = profile["profile_date"]
    observed_at = profile["observed_at"]
    saved = 0
    for row in rows:
        code = str(row.get("code") or "").strip()
        quote_at = str(row.get("quote_at") or "")
        if not code or quote_at[:10] != target_date:
            continue
        conn.execute(
            """INSERT INTO adaptive_intraday_samples(
               profile_date,session,code,payload,observed_at,created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(profile_date,session,code) DO UPDATE SET
                 payload=excluded.payload,observed_at=excluded.observed_at""",
            (target_date, session, code, _json(row), observed_at, _now()),
        )
        saved += 1
    return saved


def _rank_values(rows, getter, reverse=False):
    """Cross-sectional percentile transform to [-1, 1], deterministic on ties."""
    values = []
    for index, row in enumerate(rows):
        value = _num(getter(row))
        if value is not None:
            values.append((value, str(row.get("code") or ""), index))
    values.sort(key=lambda item: (item[0], item[1]), reverse=reverse)
    result = {}
    denominator = max(len(values) - 1, 1)
    for rank, (_, _, index) in enumerate(values):
        result[index] = 2 * rank / denominator - 1
    return result


def _capture_alpha_samples(conn, profile):
    """Persist rank-transformed daily features for future, never same-day, labels."""
    rows, _, _ = _snapshot_rows()
    profile_date = profile["profile_date"]
    rows = [
        row for row in rows
        if str(row.get("quote_at") or "")[:10] == profile_date
        and _num(row.get("price"), 0) > 0
        and str(row.get("code") or "").isdigit()
    ]
    if not rows:
        return 0
    transforms = {
        "price_momentum": _rank_values(rows, lambda row: row.get("pct")),
        "main_flow": _rank_values(rows, lambda row: row.get("main_pct")),
        "turnover": _rank_values(rows, lambda row: row.get("turnover")),
        "volume_ratio": _rank_values(rows, lambda row: row.get("vol_ratio")),
        "small_size": _rank_values(rows, lambda row: row.get("float_cap") or row.get("mktcap"), reverse=True),
        "value": _rank_values(rows, lambda row: row.get("pb"), reverse=True),
    }
    now = _now()
    inserted = 0
    for index, row in enumerate(rows):
        if any(index not in transforms[name] for name in ALPHA_FEATURES):
            continue
        before = conn.total_changes
        conn.execute(
            """INSERT OR REPLACE INTO adaptive_alpha_samples(
               profile_date,code,industry,close_price,regime,price_momentum,main_flow,turnover,
               volume_ratio,small_size,value,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (profile_date, str(row["code"]), str(row.get("industry") or "未分类"),
             _num(row.get("price"), 0), profile["regime"],
             *[round(transforms[name][index], 8) for name in ALPHA_FEATURES], now),
        )
        if conn.total_changes > before:
            inserted += 1
    return inserted


def _mature_alpha_returns(conn):
    """Mature returns with SQLite joins, never a full history Python map."""
    dates = [row[0] for row in conn.execute(
        "SELECT DISTINCT profile_date FROM adaptive_alpha_samples ORDER BY profile_date"
    )]
    inserted = 0
    for horizon in HORIZON_WEIGHTS:
        for index in range(len(dates) - horizon):
            start_date, end_date = dates[index], dates[index + horizon]
            present = conn.execute(
                """SELECT 1 FROM adaptive_alpha_returns
                     WHERE start_date=? AND end_date=? AND horizon=? LIMIT 1""",
                (start_date, end_date, horizon),
            ).fetchone()
            if present:
                continue
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO adaptive_alpha_returns(
                       start_date,end_date,horizon,code,forward_return_pct,created_at)
                   SELECT ?,?,?,first.code,
                          ROUND((last.close_price / first.close_price - 1) * 100, 8),?
                     FROM adaptive_alpha_samples first
                     JOIN adaptive_alpha_samples last ON last.code=first.code AND last.profile_date=?
                    WHERE first.profile_date=? AND first.close_price>0 AND last.close_price>0""",
                (start_date, end_date, horizon, _now(), end_date, start_date),
            )
            inserted += conn.total_changes - before
    return inserted


def _normalize_genome(weights):
    return AG.normalize_genome(weights, ALPHA_FEATURES)


def _alpha_dataset(conn, max_rows_per_window=ALPHA_MAX_ROWS_PER_WINDOW):
    """Build a bounded GA dataset without materializing the full join."""
    cap = max(100, int(max_rows_per_window))
    windows = conn.execute(
        """SELECT start_date,horizon,AVG(forward_return_pct) AS center
             FROM adaptive_alpha_returns
            GROUP BY start_date,horizon ORDER BY start_date,horizon"""
    ).fetchall()
    rows = []
    query = """SELECT s.profile_date,s.code,s.regime,s.price_momentum,s.main_flow,s.turnover,
                      s.volume_ratio,s.small_size,s.value,r.horizon,r.forward_return_pct
                 FROM adaptive_alpha_samples s JOIN adaptive_alpha_returns r
                   ON r.start_date=s.profile_date AND r.code=s.code
                WHERE r.start_date=? AND r.horizon=?
                ORDER BY ((CAST(s.code AS INTEGER) * 1103515245 + r.horizon * 12345) & 2147483647)
                LIMIT ?"""
    for window in windows:
        center = float(window["center"] or 0.0)
        for raw in conn.execute(query, (window["start_date"], window["horizon"], cap)):
            row = dict(raw)
            row["excess_return_pct"] = float(row["forward_return_pct"] or 0.0) - center
            rows.append(row)
    return rows


def _alpha_bounded_sample(rows, max_rows_per_window=ALPHA_MAX_ROWS_PER_WINDOW):
    """Deterministically cap each date/horizon cross-section for the GA lab."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["profile_date"], row["horizon"])].append(row)
    bounded = []
    cap = max(100, int(max_rows_per_window))
    for key in sorted(grouped):
        items = grouped[key]
        if len(items) > cap:
            salt = f"{key[0]}:{key[1]}:"
            items = sorted(
                items,
                key=lambda row: hashlib.sha256(
                    f"{salt}{row.get('code') or ''}".encode("utf-8")
                ).digest(),
            )[:cap]
        bounded.extend(items)
    return bounded


def _alpha_fitness(genome, rows):
    return AG.alpha_fitness(genome, rows, ALPHA_FEATURES, HORIZON_WEIGHTS)


def _mutate_genome(parent, rng):
    return AG.mutate_genome(parent, rng, ALPHA_FEATURES)


def _crossover(left, right, rng):
    return AG.crossover(left, right, rng, ALPHA_FEATURES)


def _run_alpha_lab(conn, run_date):
    profile_days = conn.execute(
        "SELECT COUNT(DISTINCT profile_date) FROM adaptive_alpha_samples"
    ).fetchone()[0]
    mature_rows = conn.execute("SELECT COUNT(*) FROM adaptive_alpha_returns").fetchone()[0]
    now = _now()
    if profile_days < ALPHA_MIN_PROFILE_DAYS or mature_rows < ALPHA_MIN_MATURE_ROWS:
        detail = {
            "reason": "样本门槛未满足",
            "required_profile_days": ALPHA_MIN_PROFILE_DAYS,
            "required_mature_rows": ALPHA_MIN_MATURE_ROWS,
            "feature_transformer": list(ALPHA_FEATURES),
            "neural_network": False,
        }
        conn.execute(
            """INSERT INTO adaptive_alpha_runs(run_date,status,profile_days,mature_rows,generations,detail,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_date) DO UPDATE SET status=excluded.status,
               profile_days=excluded.profile_days,mature_rows=excluded.mature_rows,detail=excluded.detail,updated_at=excluded.updated_at""",
            (run_date, "waiting_data", profile_days, mature_rows, 0, _json(detail), now, now),
        )
        return {"status": "waiting_data", "profile_days": profile_days, "mature_rows": mature_rows, "detail": detail}

    dataset = _alpha_dataset(conn)
    dates = sorted({row["profile_date"] for row in dataset})
    split = max(1, int(len(dates) * 0.70))
    train_dates, validation_dates = set(dates[:split]), set(dates[split:])
    train = [row for row in dataset if row["profile_date"] in train_dates]
    validation = [row for row in dataset if row["profile_date"] in validation_dates]
    if len(validation_dates) < 2:
        return {"status": "waiting_validation_window", "profile_days": profile_days, "mature_rows": mature_rows}

    rng = random.Random(f"{ENGINE_VERSION}:{run_date}")
    seeds = [
        _normalize_genome({"price_momentum": .35, "main_flow": .35, "turnover": .08, "volume_ratio": .12, "small_size": .05, "value": .05}),
        _normalize_genome({"price_momentum": -.15, "main_flow": .35, "turnover": -.15, "volume_ratio": .10, "small_size": .10, "value": .15}),
        _normalize_genome({"price_momentum": .20, "main_flow": .45, "turnover": .05, "volume_ratio": .15, "small_size": .10, "value": .05}),
    ]
    population = list(seeds)
    while len(population) < 32:
        population.append(_normalize_genome({name: rng.uniform(-1, 1) for name in ALPHA_FEATURES}))
    generations = 18
    fitness_cache = {}

    def fitness(genome, label, rows):
        key = (label, tuple(float(genome[name]) for name in ALPHA_FEATURES))
        if key not in fitness_cache:
            fitness_cache[key] = _alpha_fitness(genome, rows)
        return fitness_cache[key]

    for _ in range(generations):
        ranked = sorted(population, key=lambda genome: fitness(genome, "train", train)["fitness"], reverse=True)
        elites = ranked[:8]
        next_population = list(elites)
        while len(next_population) < 32:
            left, right = rng.choice(elites), rng.choice(elites)
            child = _crossover(left, right, rng)
            if rng.random() < 0.82:
                child = _mutate_genome(child, rng)
            next_population.append(child)
        population = next_population
    ranked = sorted(population, key=lambda genome: fitness(genome, "train", train)["fitness"], reverse=True)
    conn.execute("DELETE FROM adaptive_alpha_candidates WHERE run_date=?", (run_date,))
    leaders = []
    for genome in ranked[:5]:
        train_score = fitness(genome, "train", train)
        validation_score = fitness(genome, "validation", validation)
        genome_check = adversarial.validate_candidate_output(genome, max_relative_delta=4.0)
        status = (
            "shadow_candidate"
            if genome_check["ok"] and validation_score["fitness"] > 0 and validation_score["stability"] >= 0.55
            else "rejected"
        )
        if not genome_check["ok"]:
            validation_score = dict(validation_score)
            validation_score["adversarial_flags"] = genome_check["flags"]
        conn.execute(
            """INSERT INTO adaptive_alpha_candidates(run_date,generation,genome,train_fitness,validation_fitness,
               validation_spread_pct,profile_days,mature_rows,status,engine_version,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_date, generations, _json(genome), train_score["fitness"], validation_score["fitness"],
             validation_score["spread_pct"], profile_days, mature_rows, status, ENGINE_VERSION, now),
        )
        leaders.append({"genome": genome, "train": train_score, "validation": validation_score, "status": status})
    detail = {
        "population": 32,
        "generations": generations,
        "fitness_rows": len(dataset),
        "max_rows_per_window": ALPHA_MAX_ROWS_PER_WINDOW,
        "train_dates": sorted(train_dates),
        "validation_dates": sorted(validation_dates),
        "feature_transformer": list(ALPHA_FEATURES),
        "neural_network": False,
        "leaders": leaders,
    }
    conn.execute(
        """INSERT INTO adaptive_alpha_runs(run_date,status,profile_days,mature_rows,generations,detail,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_date) DO UPDATE SET status=excluded.status,
           profile_days=excluded.profile_days,mature_rows=excluded.mature_rows,generations=excluded.generations,
           detail=excluded.detail,updated_at=excluded.updated_at""",
        (run_date, "completed", profile_days, mature_rows, generations, _json(detail), now, now),
    )
    return {"status": "completed", "profile_days": profile_days, "mature_rows": mature_rows, "detail": detail}


def _reward_regime(conn, start_date):
    row = conn.execute(
        "SELECT regime FROM adaptive_market_profiles WHERE profile_date<=? ORDER BY profile_date DESC LIMIT 1",
        (start_date,),
    ).fetchone()
    return row["regime"] if row else "unclassified"


def _evaluate_rewards(conn):
    if not os.path.exists(PAPER_DB_PATH):
        return 0
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
    try:
        accounts = [row[0] for row in paper.execute("SELECT id FROM paper_accounts ORDER BY id")]
        # A cycle reset deletes paper_nav history but previously accumulated
        # adaptive_rewards survived the generation change, so evidence gates
        # could be satisfied by dead samples from an already-archived cycle.
        # Keep only rewards that overlap the live ledger generation: anything
        # ending before this account's oldest surviving NAV row is stale.
        for account_id in accounts:
            oldest_live_nav = paper.execute(
                "SELECT MIN(nav_date) FROM paper_nav WHERE account_id=?", (account_id,)
            ).fetchone()[0]
            if oldest_live_nav:
                conn.execute(
                    "DELETE FROM adaptive_rewards WHERE account_id=? AND end_date<?",
                    (account_id, oldest_live_nav),
                )
        new_count = 0
        for account_id in accounts:
            nav_rows = [dict(row) for row in paper.execute(
                "SELECT nav_date,nav,benchmark FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account_id,)
            )]
            # 一次性加载该账户全部成交金额并按日期建前缀和，替代循环内每个
            # 窗口重复执行一次 SUM 查询（原实现为 数千次 小查询）。
            fill_totals = {}
            for frow in paper.execute(
                "SELECT fill_date,amount FROM paper_fills WHERE account_id=? AND amount IS NOT NULL",
                (account_id,),
            ):
                _date_key = str(frow["fill_date"] or "")[:10]
                if _date_key:
                    fill_totals[_date_key] = fill_totals.get(_date_key, 0.0) + _num(frow["amount"], 0)
            fill_dates = sorted(fill_totals)
            fill_prefix = []
            _run = 0.0
            for _d in fill_dates:
                _run += fill_totals[_d]
                fill_prefix.append(_run)

            def _window_amount(start_date, end_date):
                left = _bisect_right(fill_dates, start_date)
                right = _bisect_right(fill_dates, end_date)
                return (fill_prefix[right - 1] if right > 0 else 0.0) - (fill_prefix[left - 1] if left > 0 else 0.0)

            for horizon, horizon_weight in HORIZON_WEIGHTS.items():
                for index in range(0, len(nav_rows) - horizon):
                    window = nav_rows[index:index + horizon + 1]
                    first, last = window[0], window[-1]
                    nav0, nav1 = _num(first["nav"], 0), _num(last["nav"], 0)
                    bench0, bench1 = _num(first["benchmark"], 0), _num(last["benchmark"], 0)
                    if nav0 <= 0 or nav1 <= 0 or bench0 <= 0 or bench1 <= 0:
                        continue
                    strategy_return = (nav1 / nav0 - 1) * 100
                    benchmark_return = (bench1 / bench0 - 1) * 100
                    excess = strategy_return - benchmark_return
                    peak, max_drawdown = nav0, 0.0
                    for point in window:
                        value = _num(point["nav"], nav0)
                        peak = max(peak, value)
                        max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
                    turnover = 100 * _num(_window_amount(first["nav_date"], last["nav_date"]), 0) / max((nav0 + nav1) / 2, 1)
                    raw_reward = (
                        0.62 * excess
                        + 0.18 * strategy_return
                        - 0.28 * abs(max_drawdown)
                        - 0.012 * turnover
                    )
                    weighted = raw_reward * horizon_weight
                    before = conn.total_changes
                    conn.execute(
                        """INSERT OR IGNORE INTO adaptive_rewards(
                           account_id,horizon,start_date,end_date,regime,strategy_return_pct,benchmark_return_pct,
                           excess_return_pct,drawdown_pct,turnover_pct,raw_reward,weighted_reward,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (account_id, horizon, first["nav_date"], last["nav_date"],
                         _reward_regime(conn, first["nav_date"]), round(strategy_return, 6),
                         round(benchmark_return, 6), round(excess, 6), round(max_drawdown, 6),
                         round(turnover, 6), round(raw_reward, 6), round(weighted, 6), _now()),
                    )
                    if conn.total_changes > before:
                        new_count += 1
        return new_count
    finally:
        paper.close()


def _reward_stats(conn, regime):
    rows = [dict(row) for row in conn.execute("SELECT * FROM adaptive_rewards ORDER BY end_date,id")]
    by_account = defaultdict(list)
    by_regime = defaultdict(list)
    horizon = defaultdict(lambda: defaultdict(list))
    regimes = set()
    for row in rows:
        by_account[row["account_id"]].append(row)
        horizon[row["account_id"]][row["horizon"]].append(row)
        if row["regime"] != "unclassified":
            regimes.add(row["regime"])
        if row["regime"] == regime:
            by_regime[row["account_id"]].append(row)
    return rows, by_account, by_regime, horizon, regimes


def _recency_weight(row, profile_date):
    """Give the current market regime more influence without discarding history."""
    try:
        end = dt.date.fromisoformat(str(row.get("end_date", ""))[:10])
        current = dt.date.fromisoformat(str(profile_date)[:10])
        age = max(0, (current - end).days)
    except (TypeError, ValueError):
        age = 30
    # Half-life is about 2 trading sessions; the floor keeps older samples
    # useful as a stabilizing prior rather than letting one day dominate.
    return 0.22 + 0.78 * math.exp(-0.35 * age)


def _bandit_decision(conn, profile, profile_id):
    cfg = _config(conn)
    rows, global_rows, regime_rows, horizons, regimes = _reward_stats(conn, profile["regime"])
    attribution = trade_attribution.summary_from_conn(conn, limit=240)
    attribution_by_account = attribution.get("by_account") or {}
    total_samples = len(rows)
    strategy_ids = sorted(ACCOUNT_LABELS)
    exploration = float(cfg["exploration_strength"])
    total_effective = sum(HORIZON_WEIGHTS.get(row["horizon"], 0) * _recency_weight(row, profile["profile_date"]) for row in rows)
    scores, evidence = {}, {}
    for account_id in strategy_ids:
        global_arm = global_rows.get(account_id, [])
        local_arm = regime_rows.get(account_id, [])
        global_weight = sum(HORIZON_WEIGHTS.get(row["horizon"], 0) * _recency_weight(row, profile["profile_date"]) for row in global_arm)
        local_weight = sum(HORIZON_WEIGHTS.get(row["horizon"], 0) * _recency_weight(row, profile["profile_date"]) for row in local_arm)
        global_mean = (
            sum(_num(row["raw_reward"]) * HORIZON_WEIGHTS.get(row["horizon"], 0) * _recency_weight(row, profile["profile_date"]) for row in global_arm) / global_weight if global_weight else 0.0
        )
        local_mean = (
            sum(_num(row["raw_reward"]) * HORIZON_WEIGHTS.get(row["horizon"], 0) * _recency_weight(row, profile["profile_date"]) for row in local_arm) / local_weight if local_weight else global_mean
        )
        posterior_mean = (local_mean * local_weight + global_mean * 2.0) / (local_weight + 2.0)
        uncertainty = exploration * math.sqrt(math.log(total_effective + 2.0) / (local_weight + 1.0))
        downside = _median([abs(min(0, row["drawdown_pct"])) * _recency_weight(row, profile["profile_date"]) for row in global_arm], 0.0)
        # 奖励仍以净值/超额收益为主；逐笔归因只作一个很小的解释性
        # 校正，防止“指数拖累”和“个股自身走弱”被当成同一种失败。
        attr = attribution_by_account.get(account_id) or {}
        attr_samples = int(attr.get("filled") or 0)
        mean_alpha = _num(attr.get("mean_alpha_pct"), 0.0) or 0.0
        negative_news = int(attr.get("negative_news_records") or 0)
        news_rate = negative_news / max(attr_samples, 1)
        attribution_bonus = _clamp(mean_alpha * 0.04, -0.20, 0.20) if attr_samples else 0.0
        attribution_bonus -= _clamp(news_rate * 0.04, 0.0, 0.08)
        # 新闻惩罚已并入 attribution_bonus；此处仅为 evidence 输出一致的可读
        # 字段（修复原引用未定义变量导致 _bandit_decision 必然 NameError）。
        news_overlay_bonus = -_clamp(news_rate * 0.04, 0.0, 0.08) if attr_samples else 0.0
        score = posterior_mean + uncertainty - downside * 0.08 + attribution_bonus
        scores[account_id] = score
        horizon_rows = []
        for horizon_value in HORIZON_WEIGHTS:
            subset = horizons[account_id].get(horizon_value, [])
            horizon_rows.append({
                "horizon": horizon_value,
                "samples": len(subset),
                "mean_excess_pct": round(statistics.mean([row["excess_return_pct"] for row in subset]), 3) if subset else None,
                "mean_reward": round(statistics.mean([row["raw_reward"] for row in subset]), 3) if subset else None,
            })
        evidence[account_id] = {
            "name": ACCOUNT_LABELS[account_id],
            "samples": len(global_arm),
            "regime_samples": len(local_arm),
            "posterior_mean": round(posterior_mean, 4),
            "exploration_bonus": round(uncertainty, 4),
            "downside_penalty": round(downside * 0.08, 4),
            "trade_attribution_samples": attr_samples,
            "mean_alpha_pct": round(mean_alpha, 4) if attr_samples else None,
            "negative_news_rate": round(news_rate, 4) if attr_samples else None,
            "attribution_bonus": round(attribution_bonus, 4),
            "news_overlay_bonus": round(news_overlay_bonus, 4),
            "horizons": horizon_rows,
        }

    temperature = 1.25
    peak = max(scores.values()) if scores else 0.0
    exp_scores = {key: math.exp((value - peak) / temperature) for key, value in scores.items()}
    exp_total = sum(exp_scores.values()) or 1.0
    raw_weights = {key: value / exp_total for key, value in exp_scores.items()}
    confidence = _clamp(total_samples / max(float(cfg["min_samples_advisory"]), 1.0), 0.0, 1.0)
    equal = 1.0 / max(len(strategy_ids), 1)
    mixed = {key: equal * (1 - confidence) + raw_weights[key] * confidence for key in strategy_ids}
    min_weight = float(cfg["min_strategy_weight_pct"]) / 100
    max_weight = float(cfg["max_strategy_weight_pct"]) / 100
    bounded = {key: _clamp(value, min_weight, max_weight) for key, value in mixed.items()}
    normalizer = sum(bounded.values()) or 1.0
    weights = {key: round(100 * value / normalizer, 1) for key, value in bounded.items()}

    if total_samples < int(cfg["min_samples_shadow"]):
        stage = "collecting"
    elif total_samples < int(cfg["min_samples_advisory"]):
        stage = "shadow"
    elif len(regimes) < int(cfg["min_regimes_advisory"]):
        stage = "regime_validation"
    elif total_samples < int(cfg["min_samples_eligible"]):
        stage = "advisory"
    else:
        stage = "eligible_for_review"
    status = "shadow_only" if cfg["mode"] == "shadow" else "human_review_required"
    decision_payload = {
        "total_mature_samples": total_samples,
        "regime_coverage": sorted(regimes),
        "regime_coverage_count": len(regimes),
        "confidence_pct": round(confidence * 100, 1),
        "human_approval_required": bool(cfg["human_approval_required"]),
        "data_quality": profile["quality"],
        "guardrail_pass": profile["quality"] == "valid_close",
        "trade_attribution_records": attribution.get("records", 0),
        "trade_attribution_reason_counts": attribution.get("reason_counts", {}),
    }
    now = _now()
    conn.execute(
        """INSERT INTO adaptive_decisions(decision_date,profile_id,regime,mode,stage,weights,scores,evidence,status,engine_version,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(decision_date,regime,mode) DO UPDATE SET profile_id=excluded.profile_id,stage=excluded.stage,
             weights=excluded.weights,scores=excluded.scores,evidence=excluded.evidence,status=excluded.status,
             engine_version=excluded.engine_version,updated_at=excluded.updated_at""",
        (profile["profile_date"], profile_id, profile["regime"], cfg["mode"], stage, _json(weights),
         _json({key: round(value, 4) for key, value in scores.items()}),
         _json({"strategies": evidence, "summary": decision_payload}), status, ENGINE_VERSION, now, now),
    )
    decision_id = conn.execute(
        "SELECT id FROM adaptive_decisions WHERE decision_date=? AND regime=? AND mode=?",
        (profile["profile_date"], profile["regime"], cfg["mode"]),
    ).fetchone()["id"]
    return {
        "id": decision_id,
        "decision_date": profile["profile_date"],
        "regime": profile["regime"],
        "mode": cfg["mode"],
        "stage": stage,
        "status": status,
        "weights": weights,
        "scores": {key: round(value, 4) for key, value in scores.items()},
        "evidence": evidence,
        "summary": decision_payload,
    }


def run_midday_observation(trigger="scheduled-midday"):
    """采集午间盘面快照；盘中数据只进观测表，不参与正式调参。"""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"status": "busy", "message": "已有自进化任务运行中"}
    started = _now()
    try:
        with _connect() as conn:
            profile = _market_profile()
            observation_id = _store_intraday_profile(conn, profile, session="midday")
            sample_rows = _store_intraday_samples(conn, profile, session="midday")
            conn.execute(
                """INSERT INTO adaptive_runs(
                   trigger,status,profile_date,new_rewards,detail,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (trigger, "intraday_observation", profile["profile_date"], 0,
                 _json({
                     "session": "midday",
                     "observation_id": observation_id,
                     "sample_rows": sample_rows,
                     "regime": profile["regime"],
                     "quality": profile["quality"],
                     "valid_rows": profile["valid_rows"],
                     "source_at": profile["source_at"],
                     "official_close_learning": False,
                 }), started, _now()),
            )
        result = overview()
        result["midday_observation"] = profile
        result["midday_observation"]["sample_rows"] = sample_rows
        return result
    except Exception as exc:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO adaptive_runs(
                   trigger,status,profile_date,new_rewards,detail,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (trigger, "intraday_failed", None, 0,
                 _json({"session": "midday", "error": f"{type(exc).__name__}: {exc}"}),
                 started, _now()),
            )
        raise
    finally:
        _RUN_LOCK.release()


def run_midday_advisor(trigger="scheduled-midday-dp"):
    """Run a bounded midday DeepSeek batch outside the five-minute quote loop.

    The batch reviews the just-saved midday snapshot and candidate evidence;
    it never writes weights, risk parameters, orders, or mature rewards.  The
    result is kept as an auditable advisory input for the close review.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        return {"status": "busy", "message": "已有自进化任务运行中"}
    started = _now()
    status = "advisor_skipped"
    detail = {"session": "midday-dp", "official_close_learning": False}
    try:
        with _connect() as conn:
            config = _config(conn)
        if not deepseek_advisor.enabled(config):
            detail.update({"reason": "advisor_disabled"})
        elif not deepseek_advisor.configured():
            detail.update({"reason": "api_key_missing"})
        else:
            deepseek_advisor.run_review(
                _connect, PAPER_DB_PATH, SNAPSHOT_PATHS,
                config=config, trigger=str(trigger or "scheduled-midday-dp")[:80],
            )
            if bool(config.get("llm_realtime_tuning_enabled", False)):
                try:
                    tuning = deepseek_advisor.run_realtime_tuning(
                        _connect, PAPER_DB_PATH, SNAPSHOT_PATHS,
                        config=config, profile=_market_profile(),
                        trigger=f"{str(trigger or 'scheduled-midday-dp')}:bounded-tuning",
                        mode="intraday",
                    )
                    detail["ai_tuning"] = {
                        "status": tuning.get("status"),
                        "applied_ids": tuning.get("applied_ids", []),
                        "reason": tuning.get("reason"),
                    }
                except Exception as exc:
                    detail["ai_tuning"] = {"status": "failed", "reason": type(exc).__name__}
            # A focused candidate challenge gives the close cycle a useful
            # second opinion without paying for the full six-task suite.
            try:
                deepseek_research.run_task(
                    _connect, PAPER_DB_PATH, "candidate_challenge",
                    trigger=f"{str(trigger or 'scheduled-midday-dp')}:candidate",
                )
                detail["candidate_challenge"] = "completed"
            except Exception as exc:
                detail["candidate_challenge"] = f"failed:{type(exc).__name__}"
            status = "advisor_batch"
            detail.update({"reason": "completed", "provider": "DeepSeek"})
        with _connect() as conn:
            conn.execute(
                "INSERT INTO adaptive_runs(trigger,status,profile_date,new_rewards,detail,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                (str(trigger or "scheduled-midday-dp")[:80], status, None, 0,
                 _json(detail), started, _now()),
            )
        return overview()
    except Exception as exc:
        detail.update({"reason": f"failed:{type(exc).__name__}"})
        with _connect() as conn:
            conn.execute(
                "INSERT INTO adaptive_runs(trigger,status,profile_date,new_rewards,detail,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                (str(trigger or "scheduled-midday-dp")[:80], "advisor_failed", None, 0,
                 _json(detail), started, _now()),
            )
        return overview()
    finally:
        _RUN_LOCK.release()


def run_learning_cycle(trigger="manual"):
    """Capture a close profile, mature rewards and produce a shadow decision.

    重构（A2/A3）：原实现把所有阶段塞进单个大事务，峰值内存叠加导致 OOM 且死亡无痕。
    现拆分为 6 个独立事务（每段结束即 commit + 进度 flush 落盘），大幅削减内存峰值；
    任一阶段被 SIGKILL 时，已完成阶段的数据已落库、心跳/悬挂检测可定位死点。
    """
    if not _RUN_LOCK.acquire(blocking=False):
        return {"status": "busy", "message": "已有自进化任务运行中"}
    started = _now()
    _learning_heartbeat_write(started)
    run_id = None
    advisor_config = None
    trade_attribution_result = {"status": "not_run"}
    try:
        # 悬挂检测：上一轮 started 却从未 finished 的残行 → killed（死亡有痕）
        with _connect() as conn:
            _learning_detect_stale(conn)
            cur = conn.execute(
                "INSERT INTO adaptive_runs(trigger,status,profile_date,new_rewards,detail,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                (trigger, "running", None, 0, _json({"stage": "init"}), started, started),
            )
            run_id = cur.lastrowid
        # 先完成逐笔盘后归因，再计算自进化奖励。归因失败只记录原因，
        # 不应让已有的净值学习链因为 AI 或行情服务瞬断而丢失。
        with _connect() as conn:
            advisor_config = _config(conn)
            # 读取当日午间观测，作为收盘学习的盘中上下文
            today = dt.datetime.now(TZ).date().isoformat()
            midday_ctx = conn.execute(
                "SELECT * FROM adaptive_intraday_profiles WHERE profile_date=? AND session=? ORDER BY id DESC LIMIT 1",
                (today, "midday")
            ).fetchone()
            midday_profile = dict(midday_ctx) if midday_ctx else None
        _learning_update_stage(run_id, "trade_attribution")
        try:
            trade_attribution_result = trade_attribution.run_close_attribution(
                _connect, PAPER_DB_PATH, config=advisor_config, trigger=str(trigger or "manual")[:80],
            )
        except Exception as exc:
            trade_attribution_result = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        # —— 阶段 1：市场画像 + 证据链 + alpha 样本/收益/GA 实验室（独立事务）——
        _learning_update_stage(run_id, "profile_evidence")
        with _connect() as conn:
            evidence_result = _sync_evidence_chains(conn)
            profile = _market_profile()
            profile_id = _store_profile(conn, profile)
        _learning_update_stage(run_id, "alpha_capture")
        with _connect() as conn:
            alpha_samples = _capture_alpha_samples(conn, profile)
        gc.collect()
        _learning_update_stage(run_id, "alpha_returns")
        with _connect() as conn:
            new_alpha_returns = _mature_alpha_returns(conn)
        gc.collect()
        _learning_update_stage(run_id, "alpha_lab")
        with _connect() as conn:
            alpha_lab = _run_alpha_lab(conn, profile["profile_date"])
        print(f"[learning-cycle] stage1 profile+alpha done regime={profile.get('regime')} "
              f"samples={alpha_samples} returns={new_alpha_returns} lab={alpha_lab.get('status')}", flush=True)
        # —— 阶段 2：多 horizon 奖励结算（独立事务）——
        _learning_update_stage(run_id, "rewards")
        with _connect() as conn:
            new_rewards = _evaluate_rewards(conn)
        print(f"[learning-cycle] stage2 rewards done new_rewards={new_rewards}", flush=True)
        # —— 阶段 3：执行证据 + bandit 影子决策（独立事务）——
        _learning_update_stage(run_id, "bandit_decision")
        with _connect() as conn:
            execution_evidence = _execution_evidence_state(conn, dt.datetime.now(TZ).date(), persist=True)
            decision = _bandit_decision(conn, profile, profile_id)
            decision_id = decision["id"]
        print(f"[learning-cycle] stage3 decision done decision_id={decision_id}", flush=True)
        # —— 阶段 4：风控进化评估（独立事务）——
        _learning_update_stage(run_id, "risk_evolution")
        with _connect() as conn:
            advisor_config = _config(conn)
            risk_result = risk_evolution.evaluate(conn, profile, advisor_config, PAPER_DB_PATH, _now)
        print(f"[learning-cycle] stage4 risk done candidates={risk_result.get('candidates', 0)}", flush=True)
        # —— 阶段 5：选股进化评估（独立事务）——
        _learning_update_stage(run_id, "selection_evolution")
        with _connect() as conn:
            advisor_config = _config(conn)
            selection_result = selection_evolution.evaluate(conn, profile, advisor_config, PAPER_DB_PATH, _now)
        print(f"[learning-cycle] stage5 selection done candidates={selection_result.get('candidates', 0)}", flush=True)
        # —— 阶段 5.5：双AI共识调参（独立于学习事务；缺 key 自动跳过，失败不阻塞）——
        # D1 接线：调参结果经 dual_ai_tuner.track_run 落入 evolution_tracking，
        # 供 evolution_loop 的 EVALUATE / MUTATE 消费，形成"越调越准"数据闭环。
        dual_ai_tuning = None
        _learning_update_stage(run_id, "dual_ai")
        try:
            with _connect() as conn:
                dual_ai_tuner.ensure_schema(conn)
                _keys = dual_ai_tuner.get_api_keys(conn)
            _mimo = _keys.get("mimo") or {}
            _ds = _keys.get("deepseek") or {}
            if _mimo.get("configured") and _mimo.get("enabled") \
                    and _ds.get("configured") and _ds.get("enabled"):
                dual_ai_tuning = run_dual_ai_tuning_fn(
                    trigger=str(trigger or "manual"), mode="intraday")
            else:
                dual_ai_tuning = {"status": "skipped",
                                  "reason": "双AI未就绪（需配置并启用 MiMo 与 DeepSeek）"}
        except Exception as exc:
            dual_ai_tuning = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(f"[learning-cycle] stage5.5 dual_ai done status={dual_ai_tuning.get('status')}", flush=True)
        # —— 阶段 6：完成记录（独立事务）——
        with _connect() as conn:
            try:
                import execution_quality_shadow as eqs
                execution_quality = eqs.audit(PAPER_DB_PATH, limit=3000)
            except Exception:
                execution_quality = {"status": "unavailable"}
            conn.execute(
                "UPDATE adaptive_runs SET status=?, profile_date=?, new_rewards=?, detail=?, finished_at=? WHERE id=?",
                ("completed", profile["profile_date"], new_rewards,
                 _json({"stage": "done", "regime": profile["regime"], "decision_id": decision_id,
                        "alpha_samples": alpha_samples, "new_alpha_returns": new_alpha_returns,
                        "alpha_lab_status": alpha_lab["status"],
                        "risk_candidates": risk_result.get("candidates", 0),
                        "risk_auto_applied": risk_result.get("auto_applied", []),
                        "risk_orders_attributed": risk_result.get("orders_attributed", 0),
                        "risk_daily_outcomes": risk_result.get("daily_outcomes", 0),
                        "risk_deployments": risk_result.get("deployments", {}),
                         "selection_candidates": selection_result.get("candidates", 0),
                         "selection_auto_applied": selection_result.get("auto_applied", []),
                        "dual_ai_tuning": {
                            "status": (dual_ai_tuning or {}).get("status"),
                            "run_id": (dual_ai_tuning or {}).get("id"),
                            "consensus": (dual_ai_tuning or {}).get("consensus"),
                            "consensus_reason": str((dual_ai_tuning or {}).get("consensus_reason") or "")[:120],
                            "merged_proposals": len((dual_ai_tuning or {}).get("merged_proposals") or []),
                        } if dual_ai_tuning else None,
                        "trade_attribution": {
                             "status": trade_attribution_result.get("status"),
                             "trade_date": trade_attribution_result.get("trade_date"),
                             "detail": trade_attribution_result.get("detail"),
                         },
                        "evidence_chain": evidence_result,
                        "midday_context": {
                             "regime": (midday_profile or {}).get("regime"),
                             "quality": (midday_profile or {}).get("quality"),
                             "valid_rows": (midday_profile or {}).get("valid_rows"),
                         } if midday_profile else None,
                        "execution_evidence": execution_evidence,
                        "execution_quality": {
                             "status": execution_quality.get("status"),
                             "orders": execution_quality.get("orders", 0),
                             "by_strategy": execution_quality.get("by_strategy", []),
                        },
                        "news_schedule": "dedicated_08:15_12:15_18:45"}), _now(), run_id),
            )
        # The language-model review is deliberately outside the learning
        # transaction. A timeout or provider failure must never roll back the
        # deterministic paper-trading evolution cycle.
        if deepseek_advisor.enabled(advisor_config) and deepseek_advisor.configured():
            try:
                deepseek_advisor.run_review(
                    _connect, PAPER_DB_PATH, SNAPSHOT_PATHS,
                    config=advisor_config, trigger=f"{trigger}:post-close",
                )
            except Exception:
                pass
            if trigger == "scheduled-close":
                try:
                    deepseek_research.run_suite(
                        _connect, PAPER_DB_PATH, trigger="scheduled-close",
                    )
                except Exception:
                    pass
        return overview()
    except Exception as exc:
        try:
            if run_id is not None:
                with _connect() as conn:
                    conn.execute(
                        "UPDATE adaptive_runs SET status=?, detail=?, finished_at=? WHERE id=?",
                        ("failed", _json({"error": f"{type(exc).__name__}: {exc}"}), _now(), run_id),
                    )
            else:
                with _connect() as conn:
                    conn.execute(
                        "INSERT INTO adaptive_runs(trigger,status,profile_date,new_rewards,detail,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                        (trigger, "failed", None, 0, _json({"error": f"{type(exc).__name__}: {exc}"}), started, _now()),
                    )
        except Exception:
            pass
        raise
    finally:
        _learning_heartbeat_clear()
        _RUN_LOCK.release()


def record_feedback(decision_id: int, account_id: str, verdict: str, note: str = ""):
    if account_id not in ACCOUNT_LABELS:
        raise ValueError("未知策略账户")
    if verdict not in {"approve", "watch", "reject"}:
        raise ValueError("verdict 必须是 approve、watch 或 reject")
    with _connect() as conn:
        decision = conn.execute("SELECT id FROM adaptive_decisions WHERE id=?", (decision_id,)).fetchone()
        if not decision:
            raise ValueError("自进化决策不存在")
        conn.execute(
            "INSERT INTO adaptive_feedback(decision_id,account_id,verdict,note,created_at) VALUES(?,?,?,?,?)",
            (decision_id, account_id, verdict, str(note or "")[:500], _now()),
        )
    return overview()


def apply_risk_candidate(candidate_id: int, approved_by: str = "human"):
    """Promote an eligible candidate; loosening is only reachable through this explicit boundary."""
    approved_by = adversarial.require_human_confirmation(approved_by, confirmed=True)
    candidate_id = int(candidate_id)
    candidate_snapshot = _candidate_snapshot("risk", candidate_id)
    if candidate_snapshot and candidate_snapshot["candidate"].get("status") == "applied":
        return overview()
    if candidate_snapshot:
        evidence_check = adversarial.validate_evidence(
            _loads(candidate_snapshot["candidate"].get("evidence"), {}),
            max_age_seconds=365 * 24 * 60 * 60,
        )
        if not evidence_check["ok"]:
            raise ValueError("候选证据未通过对抗性校验：" + ",".join(evidence_check["flags"]))
    paper_snapshot = _paper_account_snapshot(
        str(candidate_snapshot["candidate"]["account_id"]) if candidate_snapshot else ""
    )
    account_id = str(candidate_snapshot["candidate"]["account_id"]) if candidate_snapshot else ""
    _manual_apply_gate("risk", candidate_snapshot, account_id)
    expected_version = None
    if candidate_snapshot:
        run_date = str(candidate_snapshot["candidate"].get("run_date") or "").replace("-", "")
        expected_version = f"risk-evo-{run_date}-{candidate_id}"
    try:
        with _connect() as conn:
            risk_evolution.apply_candidate(
                conn, PAPER_DB_PATH, candidate_id, _now,
                approved_by=approved_by, require_conservative=False,
            )
    except Exception as exc:
        errors = _compensate_evolution(
            "risk", candidate_snapshot, paper_snapshot, account_id, candidate_id,
            "adaptive_risk_applied", expected_version,
        )
        if errors and hasattr(exc, "add_note"):
            exc.add_note("; ".join(errors))
        raise
    return overview()


def rollback_risk(account_id: str, reason: str = "人工回滚"):
    adversarial.require_human_confirmation("human", confirmed=True)
    if account_id not in ACCOUNT_LABELS:
        raise ValueError("未知策略账户")
    latest = _latest_candidate_status("risk", account_id)
    applied = _latest_candidate_status("risk", account_id, "applied")
    # A retry after a completed rollback is a safe no-op.  This matters when a
    # browser retries a timed-out request after the paper commit succeeded.
    if not applied and latest and latest["status"] == "rolled_back":
        return overview()
    candidate_id = int(applied["id"]) if applied else 0
    candidate_snapshot = _candidate_snapshot("risk", candidate_id) if candidate_id else None
    paper_snapshot = _paper_account_snapshot(account_id)
    try:
        with _connect() as conn:
            risk_evolution.rollback(conn, PAPER_DB_PATH, account_id, _now, str(reason or "人工回滚")[:300])
    except Exception as exc:
        expected_version = _paper_current_version(account_id)
        errors = _compensate_evolution(
            "risk", candidate_snapshot, paper_snapshot, account_id, candidate_id,
            "adaptive_risk_rolled_back", expected_version,
        )
        if errors and hasattr(exc, "add_note"):
            exc.add_note("; ".join(errors))
        raise
    return overview()


def rollback_selection(account_id: str, reason: str = "人工回滚"):
    adversarial.require_human_confirmation("human", confirmed=True)
    if account_id not in ACCOUNT_LABELS:
        raise ValueError("未知策略账户")
    latest = _latest_candidate_status("selection", account_id)
    applied = _latest_candidate_status("selection", account_id, "applied")
    if not applied and latest and latest["status"] == "rolled_back":
        return overview()
    candidate_id = int(applied["id"]) if applied else 0
    candidate_snapshot = _candidate_snapshot("selection", candidate_id) if candidate_id else None
    paper_snapshot = _paper_account_snapshot(account_id)
    try:
        with _connect() as conn:
            selection_evolution.rollback(conn, PAPER_DB_PATH, account_id, _now, str(reason or "人工回滚")[:300])
    except Exception as exc:
        expected_version = _paper_current_version(account_id)
        errors = _compensate_evolution(
            "selection", candidate_snapshot, paper_snapshot, account_id, candidate_id,
            "adaptive_selection_rolled_back", expected_version,
        )
        if errors and hasattr(exc, "add_note"):
            exc.add_note("; ".join(errors))
        raise
    return overview()


def apply_selection(candidate_id: int, approved_by: str = "human"):
    """人工确认选股结构候选；参数级候选不会走此入口。"""
    approved_by = adversarial.require_human_confirmation(approved_by, confirmed=True)
    candidate_id = int(candidate_id)
    candidate_snapshot = _candidate_snapshot("selection", candidate_id)
    if candidate_snapshot and candidate_snapshot["candidate"].get("status") == "applied":
        return overview()
    if candidate_snapshot:
        evidence_check = adversarial.validate_evidence(
            _loads(candidate_snapshot["candidate"].get("evidence"), {}),
            max_age_seconds=365 * 24 * 60 * 60,
        )
        if not evidence_check["ok"]:
            raise ValueError("候选证据未通过对抗性校验：" + ",".join(evidence_check["flags"]))
    paper_snapshot = _paper_account_snapshot(
        str(candidate_snapshot["candidate"]["account_id"]) if candidate_snapshot else ""
    )
    account_id = str(candidate_snapshot["candidate"]["account_id"]) if candidate_snapshot else ""
    _manual_apply_gate("selection", candidate_snapshot, account_id)
    expected_version = None
    if candidate_snapshot:
        run_date = str(candidate_snapshot["candidate"].get("run_date") or "").replace("-", "")
        expected_version = f"select-evo-{run_date}-{candidate_id}"
    try:
        with _connect() as conn:
            selection_evolution.apply_candidate(
                conn, PAPER_DB_PATH, candidate_id, _now, approved_by=approved_by,
            )
    except Exception as exc:
        errors = _compensate_evolution(
            "selection", candidate_snapshot, paper_snapshot, account_id, candidate_id,
            "adaptive_selection_applied", expected_version,
        )
        if errors and hasattr(exc, "add_note"):
            exc.add_note("; ".join(errors))
        raise
    return overview()


def approve_neural_network(confirmed: bool = False, approved_by: str = "human-ui"):
    """人工确认神经网络进入有界影子排序。

    This endpoint never enables direct order control.  It is intentionally
    fail-closed until the point-in-time, multi-horizon sample gates are met.
    """
    if not confirmed:
        raise ValueError("请先在页面点击人工确认")
    actor = str(approved_by or "human-ui")[:80]
    with _connect() as conn:
        state = neural_shadow.control_status(conn)
        if not state["readiness"].get("admitted"):
            blockers = "；".join(state["readiness"].get("blockers") or []) or "样本外门槛未满足"
            raise ValueError(f"神经网络尚未达到人工放权门槛：{blockers}")
        now = _now()
        conn.execute(
            "INSERT INTO adaptive_config(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            ("neural_network_approved", _json(True), now),
        )
        conn.execute(
            "INSERT INTO adaptive_config(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            ("neural_network_approved_by", _json(actor), now),
        )
    return overview()


def run_advisor_review(trigger="manual-ui", purpose="data_quality"):
    """Run an evidence-only DeepSeek review and return the refreshed view."""
    with _connect() as conn:
        cfg = _config(conn)
    if purpose == "data_quality":
        deepseek_advisor.run_review(
            _connect, PAPER_DB_PATH, SNAPSHOT_PATHS,
            config=cfg, trigger=str(trigger or "manual-ui")[:80],
        )
    else:
        deepseek_research.run_task(
            _connect, PAPER_DB_PATH, purpose,
            trigger=str(trigger or "manual-ui")[:80],
        )
    return overview()


def run_scheduled_ai_analysis(trigger="manual-ui", window="manual", scope="all"):
    """Run one idempotent time-window AI analysis outside trading paths."""
    with _connect() as conn:
        cfg = _config(conn)
    return ai_analysis.run_analysis(
        _connect, PAPER_DB_PATH, SNAPSHOT_PATHS, deepseek_advisor,
        config=cfg, trigger=str(trigger or "manual-ui")[:80],
        window=str(window or "manual")[:30], scope=str(scope or "all")[:30],
    )


def ai_analysis_timeline(limit=40, trade_date=None):
    return ai_analysis.timeline(_connect, limit=limit, trade_date=trade_date)


def run_ai_tuning(trigger="manual-ai-tuning", mode="intraday"):
    """Explicitly run the bounded DeepSeek paper-account tuner."""
    with _connect() as conn:
        cfg = _config(conn)
    if not deepseek_advisor.enabled(cfg):
        raise RuntimeError("advisor_disabled")
    if not deepseek_advisor.configured():
        raise RuntimeError("api_key_missing")
    result = deepseek_advisor.run_realtime_tuning(
        _connect, PAPER_DB_PATH, SNAPSHOT_PATHS, config=cfg,
        profile=_market_profile(), trigger=str(trigger or "manual-ai-tuning")[:80],
        mode=str(mode or "intraday")[:30],
    )
    view = overview()
    view["ai_tuning_result"] = result
    return view


def run_advisor_suite(trigger="manual-suite"):
    with _connect() as conn:
        cfg = _config(conn)
    if not deepseek_advisor.enabled(cfg):
        raise RuntimeError("advisor_disabled")
    if not deepseek_advisor.configured():
        raise RuntimeError("api_key_missing")
    deepseek_advisor.run_review(_connect, PAPER_DB_PATH, SNAPSHOT_PATHS, config=cfg, trigger=trigger)
    deepseek_research.run_suite(_connect, PAPER_DB_PATH, trigger=trigger)
    return overview()


def run_news_learning(trigger="manual-ui"):
    news_learning.run_cycle(trigger=trigger)
    return overview()


def trade_attributions(limit=160, account_id=None, trade_date=None):
    """查询逐笔盘后归因，供自进化二级页面/审计接口使用。"""
    with _connect() as conn:
        return trade_attribution.records(conn, limit=limit, account_id=account_id, trade_date=trade_date)


def _payload_dict(value):
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _chain_id(account_id, order_id):
    raw = f"{EVIDENCE_ENGINE_VERSION}:{account_id}:{int(order_id)}"
    return "ev-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _extract_versions(signal_payload, risk_payload):
    pick = signal_payload.get("pick") or {}
    score = pick.get("score_components") or {}
    feature_version = signal_payload.get("_feature_version") or score.get("version") or pick.get("factor_version") or "legacy-unversioned"
    parameter_version = (
        risk_payload.get("_parameter_version")
        or risk_payload.get("risk_version")
        or (risk_payload.get("risk") or {}).get("version")
        or (risk_payload.get("profile") or {}).get("version")
        or (risk_payload.get("position_count_gate") or {}).get("allocation_version")
        or "baseline-unversioned"
    )
    snapshot_at = (
        risk_payload.get("_snapshot_at")
        or risk_payload.get("quote_at")
        or (risk_payload.get("quote") or {}).get("quote_at")
        or pick.get("quote_at")
        or pick.get("factor_asof")
    )
    return str(feature_version), str(parameter_version), snapshot_at


def _sync_evidence_chains(conn):
    """Build an immutable, auditable bridge across the existing paper ledger.

    Filled orders and non-filled decisions are deliberately separated.  The
    latter are counterfactual evidence and can never become Bandit rewards.
    """
    if not os.path.exists(PAPER_DB_PATH):
        return {"orders": 0, "linked": 0, "valid": 0, "actual": 0, "counterfactual": 0}
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
    try:
        orders = paper.execute(
            """SELECT id,account_id,code,side,signal_id,status,reason,created_at,executed_at,
                      CASE WHEN json_valid(risk_payload) THEN json_extract(risk_payload,'$.adaptive_canary') END AS adaptive_canary,
                      CASE WHEN json_valid(risk_payload) THEN json_extract(risk_payload,'$.kind') END AS entry_kind,
                      CASE WHEN json_valid(risk_payload) THEN COALESCE(json_extract(risk_payload,'$.risk_version'),json_extract(risk_payload,'$.risk.version'),json_extract(risk_payload,'$.profile.version'),json_extract(risk_payload,'$.position_count_gate.allocation_version')) END AS parameter_version,
                      CASE WHEN json_valid(risk_payload) THEN COALESCE(json_extract(risk_payload,'$.quote_at'),json_extract(risk_payload,'$.quote.quote_at')) END AS snapshot_at
                 FROM paper_orders ORDER BY id"""
        )
        signals = {
            int(row["id"]): dict(row) for row in paper.execute(
                """SELECT id,signal_date,
                          CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.pick.score_components.version'),json_extract(payload,'$.pick.factor_version')) END AS feature_version
                     FROM paper_signals"""
            )
        }
        fills_by_order = defaultdict(list)
        for row in paper.execute("SELECT id,order_id,quote_at FROM paper_fills ORDER BY id"):
            fills_by_order[int(row["order_id"])].append(dict(row))
        decisions = defaultdict(list)
        for row in paper.execute("SELECT id,account_id,code,side,created_at FROM paper_risk_decisions ORDER BY id"):
            decisions[(row["account_id"], row["code"], row["side"])].append(dict(row))

        actual = counterfactual = valid = linked = total = 0
        now = _now()
        for raw_order in orders:
            total += 1
            order = dict(raw_order)
            order_id = int(order["id"])
            account_id = str(order.get("account_id") or "")
            signal = signals.get(int(order["signal_id"])) if order.get("signal_id") else None
            signal_payload = {"_feature_version": (signal or {}).get("feature_version")}
            risk_payload = {
                "adaptive_canary": order.get("adaptive_canary"),
                "kind": order.get("entry_kind"),
                "_parameter_version": order.get("parameter_version"),
                "_snapshot_at": order.get("snapshot_at"),
            }
            fill_rows = fills_by_order.get(order_id, [])
            filled = str(order.get("status") or "") == "filled" and bool(fill_rows)
            ledger_type = "actual" if filled else "counterfactual"
            origin = "adaptive_canary" if risk_payload.get("adaptive_canary") else "baseline"
            candidates = decisions.get((account_id, order.get("code"), order.get("side")), [])
            risk_decision = None
            order_at = str(order.get("created_at") or "")
            for item in reversed(candidates):
                if str(item.get("created_at") or "") <= order_at:
                    risk_decision = item
                    break
            feature_version, parameter_version, snapshot_at = _extract_versions(signal_payload, risk_payload)
            flags = []
            # A normal entry must be traceable to both its selection signal
            # and a risk decision.  Risk exits and model-owned actions on an
            # already-open position (T 回补 / 专属加仓 / 换仓接力) have no new
            # selection signal by design; their contemporaneous risk decision
            # plus decision snapshot is their evidence root.  Manual or
            # unknown buy origins stay strict and cannot use this exemption.
            entry_kind = str(risk_payload.get("kind") or "").strip().lower()
            signal_optional_buy = entry_kind in {
                "swing_scale_in", "intraday_buyback", "t_rebuy",
                "rotation_replacement", "rotation_buy",
            }
            needs_signal = (
                str(order.get("side") or "").lower() == "buy"
                and not signal_optional_buy
            )
            if needs_signal and not signal:
                flags.append("missing_signal")
            if not risk_decision:
                flags.append("missing_risk_decision")
            if str(order.get("status") or "") == "filled" and not fill_rows:
                flags.append("filled_without_fill")
            if fill_rows and str(order.get("status") or "") != "filled":
                flags.append("fill_on_nonfilled_order")
            if feature_version == "legacy-unversioned":
                flags.append("missing_feature_version")
            if parameter_version == "baseline-unversioned":
                flags.append("missing_parameter_version")
            integrity = "valid" if not flags else ("legacy_gap" if set(flags) <= {"missing_feature_version", "missing_parameter_version", "missing_signal", "missing_risk_decision"} else "invalid")
            fill = fill_rows[-1] if fill_rows else {}
            payload = {
                "engine_version": EVIDENCE_ENGINE_VERSION,
                "entry_kind": entry_kind or None,
                "reason": order.get("reason"),
                "counterfactual_reason": None if filled else order.get("reason"),
                "fill_count": len(fill_rows),
                "actual_reward_eligible": bool(filled and integrity != "invalid"),
            }
            values = (
                _chain_id(account_id, order_id), ledger_type, origin, account_id,
                str(order.get("code") or ""), order.get("side"), order.get("signal_id"),
                (risk_decision or {}).get("id"), order_id, fill.get("id"), order.get("status"),
                (signal or {}).get("signal_date"), (risk_decision or {}).get("created_at"),
                order.get("created_at"), fill.get("quote_at") or order.get("executed_at"),
                feature_version, parameter_version, snapshot_at, integrity, _json(flags),
                _json(payload), now, now,
            )
            conn.execute(
                """INSERT INTO adaptive_evidence_chains(
                   chain_id,ledger_type,origin,account_id,code,side,signal_id,risk_decision_id,
                   order_id,fill_id,order_status,signal_date,decision_at,order_at,fill_at,
                   feature_version,parameter_version,snapshot_at,integrity_status,integrity_flags,
                   payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(chain_id) DO UPDATE SET ledger_type=excluded.ledger_type,
                   origin=excluded.origin,risk_decision_id=excluded.risk_decision_id,
                   fill_id=excluded.fill_id,order_status=excluded.order_status,fill_at=excluded.fill_at,
                   feature_version=excluded.feature_version,parameter_version=excluded.parameter_version,
                   snapshot_at=excluded.snapshot_at,integrity_status=excluded.integrity_status,
                   integrity_flags=excluded.integrity_flags,payload=excluded.payload,updated_at=excluded.updated_at""",
                values,
            )
            actual += int(ledger_type == "actual")
            counterfactual += int(ledger_type == "counterfactual")
            valid += int(integrity == "valid")
            linked += int(bool(risk_decision and (not needs_signal or signal)))
        return {
            "orders": total, "linked": linked, "valid": valid,
            "actual": actual, "counterfactual": counterfactual,
            "link_pct": round(100 * linked / max(total, 1), 2),
            "valid_pct": round(100 * valid / max(total, 1), 2),
        }
    finally:
        paper.close()


def _closed_loop_state(conn, evidence=None):
    evidence = evidence or _sync_evidence_chains(conn)
    profile_days = conn.execute("SELECT COUNT(DISTINCT profile_date) FROM adaptive_market_profiles").fetchone()[0]
    recent_actual = conn.execute(
        "SELECT COUNT(*) FROM adaptive_evidence_chains WHERE ledger_type='actual' AND integrity_status!='invalid'"
    ).fetchone()[0]
    invalid = conn.execute(
        "SELECT COUNT(*) FROM adaptive_evidence_chains WHERE integrity_status='invalid'"
    ).fetchone()[0]
    accounts = conn.execute(
        "SELECT COUNT(DISTINCT account_id) FROM adaptive_evidence_chains WHERE ledger_type='actual'"
    ).fetchone()[0]
    row = conn.execute("SELECT * FROM adaptive_canary_state WHERE id=1").fetchone()
    state = dict(row) if row else {}
    window_start = str(state.get("updated_at") or "")
    window = dict(conn.execute(
        """SELECT COUNT(*) total,
           SUM(CASE WHEN risk_decision_id IS NOT NULL
                     AND COALESCE(integrity_flags,'') NOT LIKE '%missing_signal%'
                    THEN 1 ELSE 0 END) linked,
           SUM(CASE WHEN integrity_status='invalid' THEN 1 ELSE 0 END) invalid,
           SUM(CASE WHEN ledger_type='actual' THEN 1 ELSE 0 END) actual
           FROM adaptive_evidence_chains WHERE order_at>=?""",
        (window_start,),
    ).fetchone())
    window_total = int(window.get("total") or 0)
    window_linked = int(window.get("linked") or 0)
    window_invalid = int(window.get("invalid") or 0)
    window.update({
        "start_at": window_start,
        "link_pct": round(100 * window_linked / max(window_total, 1), 2) if window_total else 100.0,
        "valid": window_total - window_invalid,
        "valid_pct": round(100 * (window_total - window_invalid) / max(window_total, 1), 2) if window_total else 100.0,
    })
    blockers = []
    if window.get("link_pct", 0) < 100:
        blockers.append("新闭环窗口仍有委托未关联信号或风控决策")
    if window_invalid:
        blockers.append(f"新闭环窗口发现 {window_invalid} 条账本一致性异常")
    if profile_days < 10:
        blockers.append(f"完整画像日 {profile_days}/10")
    if recent_actual < 6 or accounts < 2:
        blockers.append(f"可归因成交 {recent_actual} 笔、覆盖 {accounts}/3 策略")
    try:
        state["evidence"] = json.loads(state.get("evidence") or "{}")
    except (TypeError, ValueError):
        state["evidence"] = {}
    state.update({
        "engine_version": EVIDENCE_ENGINE_VERSION,
        "evidence_chain": evidence,
        "admission_window": window,
        "legacy_debt": {
            "unlinked_orders": max(0, int(evidence.get("orders", 0)) - int(evidence.get("linked", 0))),
            "invalid_orders": invalid,
            "note": "历史缺字段保留审计，不冒充完整链；准入只使用新闭环窗口。",
        },
        "profile_days": profile_days,
        "attributable_fills": recent_actual,
        "strategy_coverage": accounts,
        "blockers": blockers,
        "eligible_next_stage": not blockers,
        "limits": {"max_nav_pct": CANARY_MAX_NAV_PCT, "max_new_slots": CANARY_MAX_NEW_SLOTS},
        "timeline": [
            {"stage": "D1-D3", "mode": "影子闭环", "nav_pct": 0},
            {"stage": "D4-D5", "mode": "零额度干跑", "nav_pct": 0},
            {"stage": "D6-D7", "mode": "模拟盘灰度", "nav_pct": 5},
            {"stage": "D8-D10", "mode": "模拟盘扩大灰度", "nav_pct": 10},
        ],
    })
    return state


def _shadow_history_rows(history):
    """Normalize a cached history into close/high/low rows without I/O.

    The risk overlay deliberately accepts a small, dependency-free input
    contract so it can be tested independently from the data provider.  A
    pandas DataFrame, a list of mappings, or a list of close prices are all
    valid.  Malformed values are discarded instead of being inferred.
    """
    if history is None:
        return []
    if hasattr(history, "to_dict"):
        try:
            history = history.to_dict("records")
        except Exception:
            history = []
    if isinstance(history, dict):
        history = history.get("rows") or history.get("data") or []
    if not isinstance(history, (list, tuple)):
        return []
    rows = []
    for item in history:
        if isinstance(item, dict):
            def pick(*keys):
                for key in keys:
                    if key in item:
                        value = _num(item.get(key), None)
                        if value is not None:
                            return value
                return None
            close = pick("close", "close_price", "price", "收盘")
            high = pick("high", "high_price", "最高")
            low = pick("low", "low_price", "最低")
            previous = pick("prev_close", "previous_close", "昨收")
            if close is not None and close > 0:
                rows.append({"close": close, "high": high, "low": low, "prev_close": previous})
        elif isinstance(item, (list, tuple)) and len(item) == 1:
            value = _num(item[0], None)
            if value is not None and value > 0:
                rows.append({"close": value, "high": None, "low": None, "prev_close": None})
        else:
            value = _num(item, None)
            if value is not None and value > 0:
                rows.append({"close": value, "high": None, "low": None, "prev_close": None})
    return rows


def _shadow_volatility(history, window):
    """Return annualised close-to-close volatility or an explicit unknown."""
    rows = _shadow_history_rows(history)
    if len(rows) < int(window) + 1:
        return {"status": "unknown", "value_pct": None, "samples": max(0, len(rows) - 1),
                "required": int(window) + 1, "reason": "历史K线不足"}
    closes = [row["close"] for row in rows[-(int(window) + 1):]]
    returns = [(closes[idx] / closes[idx - 1]) - 1.0 for idx in range(1, len(closes))
               if closes[idx - 1] > 0 and closes[idx] > 0]
    if len(returns) < int(window):
        return {"status": "unknown", "value_pct": None, "samples": len(returns),
                "required": int(window), "reason": "有效收盘价不足"}
    value = statistics.stdev(returns) * math.sqrt(252.0) * 100.0 if len(returns) > 1 else 0.0
    return {"status": "known", "value_pct": round(value, 4), "samples": len(returns),
            "window": int(window)}


def _shadow_atr_pct(history, window=20):
    """Return ATR as a percent of the latest close, or unknown.

    True ranges require high/low and a prior close.  Close-only histories are
    therefore not silently treated as ATR data.
    """
    rows = _shadow_history_rows(history)
    if len(rows) < int(window) + 1:
        return {"status": "unknown", "value_pct": None, "samples": 0,
                "required": int(window) + 1, "reason": "历史K线不足"}
    selected = rows[-(int(window) + 1):]
    true_ranges = []
    for idx in range(1, len(selected)):
        row = selected[idx]
        prev_close = selected[idx - 1]["close"]
        high, low = row.get("high"), row.get("low")
        if high is None or low is None or prev_close <= 0 or high < low:
            continue
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    latest = selected[-1]["close"]
    if len(true_ranges) < int(window) or latest <= 0:
        return {"status": "unknown", "value_pct": None, "samples": len(true_ranges),
                "required": int(window), "reason": "高低价或昨收缺失"}
    return {"status": "known", "value_pct": round(statistics.mean(true_ranges) / latest * 100.0, 4),
            "samples": len(true_ranges), "window": int(window)}


def _shadow_hhi(values):
    """Compute a normalized Herfindahl index from non-negative exposures."""
    clean = [float(value) for value in values if _num(value, None) is not None and float(value) > 0]
    total = sum(clean)
    return round(sum((value / total) ** 2 for value in clean), 6) if total > 0 else None


def _portfolio_shadow_risk(positions, histories=None, asof=None, industry_shock_pct=-8.0,
                           limit_down_pct=-10.0):
    """Build read-only portfolio risk metrics for the adaptive overview.

    No caller in this module may use the result to submit, cancel, or resize
    an order.  Unknown marks/history stay unknown; cost is only a labelled
    fallback valuation and never masquerades as a real-time quote.
    """
    histories = histories if isinstance(histories, dict) else {}
    positions = positions if isinstance(positions, (list, tuple)) else []
    normalized = []
    unknown_marks = []
    for raw in positions:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not code:
            continue
        qty = _num(raw.get("qty"), 0.0) or 0.0
        if qty <= 0:
            continue
        mark = _num(raw.get("market_price"), None)
        mark = mark if mark is not None else _num(raw.get("current_price"), None)
        mark = mark if mark is not None else _num(raw.get("price"), None)
        cost = _num(raw.get("cost"), None)
        if mark is not None and mark > 0:
            value, source = qty * mark, "quote"
        elif cost is not None and cost > 0:
            value, source = qty * cost, "cost_fallback"
        else:
            value, source = None, "unknown"
            unknown_marks.append(code)
        normalized.append({
            "account_id": str(raw.get("account_id") or "unknown"),
            "account_name": raw.get("account_name"),
            "code": code,
            "name": str(raw.get("name") or ""),
            "industry": str(raw.get("industry") or "未知"),
            "qty": int(qty) if float(qty).is_integer() else round(qty, 4),
            "mark_price": mark,
            "cost": cost,
            "market_value": round(value, 2) if value is not None else None,
            "valuation_source": source,
        })
    valued = [row for row in normalized if row["market_value"] is not None]
    total_value = sum(row["market_value"] for row in valued)
    by_strategy = defaultdict(float)
    by_code = defaultdict(float)
    by_industry = defaultdict(float)
    code_accounts = defaultdict(set)
    for row in valued:
        by_strategy[row["account_id"]] += row["market_value"]
        by_code[row["code"]] += row["market_value"]
        by_industry[row["industry"]] += row["market_value"]
    for row in normalized:
        code_accounts[row["code"]].add(row["account_id"])
    def shares(values):
        return {key: round(value / total_value * 100.0, 3) for key, value in sorted(values.items(), key=lambda item: item[1], reverse=True)} if total_value > 0 else {}
    strategy_shares = shares(by_strategy)
    industry_shares = shares(by_industry)
    cross_strategy = []
    for code, accounts in sorted(code_accounts.items()):
        if len(accounts) > 1:
            cross_strategy.append({
                "code": code,
                "strategies": sorted(accounts),
                "strategy_count": len(accounts),
                "market_value": round(by_code.get(code, 0.0), 2),
                "share_pct": round(by_code.get(code, 0.0) / total_value * 100.0, 3) if total_value > 0 else None,
            })
    volatility = {}
    for code in sorted({row["code"] for row in normalized}):
        history = histories.get(code)
        volatility[code] = {
            "20d": _shadow_volatility(history, 20),
            "60d": _shadow_volatility(history, 60),
            "atr20_pct": _shadow_atr_pct(history, 20),
        }
    if total_value > 0:
        index_stress = {
            "index_minus_2_pct": round(total_value * -0.02, 2),
            "index_minus_5_pct": round(total_value * -0.05, 2),
        }
        industry_stress = [{
            "industry": industry, "shock_pct": float(industry_shock_pct),
            "loss": round(value * industry_shock_pct / 100.0, 2),
            "exposure": round(value, 2), "share_pct": industry_shares.get(industry),
        } for industry, value in sorted(by_industry.items(), key=lambda item: item[1], reverse=True)]
        worst_code, worst_value = max(by_code.items(), key=lambda item: item[1])
        single_stress = {"code": worst_code, "shock_pct": float(limit_down_pct),
                         "loss": round(worst_value * limit_down_pct / 100.0, 2),
                         "exposure": round(worst_value, 2)}
    else:
        index_stress, industry_stress, single_stress = {
            "status": "unknown", "reason": "没有可估值持仓"
        }, [], {"status": "unknown", "reason": "没有可估值持仓"}
    return {
        "mode": "shadow",
        "version": "portfolio-risk-shadow-v2",
        "asof": asof or _now(),
        "data_quality": {
            "status": "known" if valued and not unknown_marks else ("partial" if valued else "unknown"),
            "valued_positions": len(valued), "total_positions": len(normalized),
            "unknown_mark_codes": sorted(set(unknown_marks)),
            "history_codes": sorted(str(key) for key in histories if str(key) in {row["code"] for row in normalized}),
            "note": "cost_fallback 仅用于影子估值；没有行情或历史不补造数据。",
        },
        "positions": {"total": len(normalized), "valued": len(valued), "unknown": len(normalized) - len(valued),
                      "total_value": round(total_value, 2) if total_value > 0 else None},
        "exposure": {
            "by_strategy": [{"account_id": key, "market_value": round(value, 2), "share_pct": strategy_shares.get(key)} for key, value in sorted(by_strategy.items(), key=lambda item: item[1], reverse=True)],
            "by_industry": [{"industry": key, "market_value": round(value, 2), "share_pct": industry_shares.get(key)} for key, value in sorted(by_industry.items(), key=lambda item: item[1], reverse=True)],
            "cross_strategy_same_code": cross_strategy,
        },
        "concentration": {
            "strategy_hhi": _shadow_hhi(by_strategy.values()),
            "industry_hhi": _shadow_hhi(by_industry.values()),
            "code_hhi": _shadow_hhi(by_code.values()),
            "top_strategy_share_pct": max(strategy_shares.values()) if strategy_shares else None,
            "top_industry_share_pct": max(industry_shares.values()) if industry_shares else None,
            "top_code_share_pct": round(max(by_code.values()) / total_value * 100.0, 3) if total_value > 0 else None,
        },
        "volatility": volatility,
        "stress": {
            "index": index_stress,
            "industry": {"assumption_pct": float(industry_shock_pct), "scenarios": industry_stress},
            "single_limit_down": single_stress,
        },
        "flags": [
            *(["行情估值不完整"] if unknown_marks else []),
            *(["行业集中度较高"] if max(industry_shares.values(), default=0.0) >= 35.0 else []),
            *(["同股跨策略重复暴露"] if cross_strategy else []),
        ],
    }


def _portfolio_shadow_arbitration():
    """Rank candidates and attach read-only portfolio risk metrics.

    This remains advisory only: risk output is persisted in the overview
    response and never changes a paper signal or submits an order.
    """
    if not os.path.exists(PAPER_DB_PATH):
        return {
            "mode": "shadow", "version": "portfolio-arbiter-v1", "candidates": [],
            "reason": "paper_db_missing", "risk_metrics": _portfolio_shadow_risk([]),
        }
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
    try:
        positions = [dict(row) for row in paper.execute(
            "SELECT account_id,code,name,industry,qty,cost FROM paper_positions WHERE qty>0"
        )]
        quote_map = {}
        for snapshot_path in SNAPSHOT_PATHS:
            snapshot = _load_json(snapshot_path, {}) or {}
            for item in (snapshot.get("rows") or []) if isinstance(snapshot, dict) else []:
                if isinstance(item, dict) and item.get("code"):
                    quote_map[str(item["code"])] = _num(item.get("price"), None)
            if quote_map:
                break
        for position in positions:
            price = quote_map.get(str(position.get("code") or ""))
            if price is not None and price > 0:
                position["market_price"] = price
        histories = {}
        try:
            import data_fetcher as shared_data
            for code in {str(row.get("code") or "") for row in positions}:
                if code:
                    frame = shared_data.load_shared_kline(code)
                    if frame is not None:
                        histories[code] = frame
        except Exception:
            # The overlay must still render if the optional cache/provider is
            # unavailable; volatility fields remain explicit ``unknown``.
            histories = {}
        today = dt.datetime.now(TZ).date().isoformat()
        portfolio_risk = _portfolio_shadow_risk(positions, histories, asof=today)
        held_codes = Counter(str(row.get("code") or "") for row in positions)
        industries = Counter(str(row.get("industry") or "未知") for row in positions)
        signals = [dict(row) for row in paper.execute(
            """SELECT * FROM paper_signals WHERE status IN ('pending','deferred_capacity')
               AND intended_date=?
               ORDER BY COALESCE(t_score,0) DESC,COALESCE(rank_score,0) DESC,id DESC LIMIT 120""",
            (today,),
        )]
        rows = []
        for signal in signals:
            payload = _payload_dict(signal.get("payload"))
            pick = payload.get("pick") or {}
            code = str(signal.get("code") or "")
            name = str(signal.get("name") or pick.get("name") or "")
            allowed_prefix = code.startswith(("000", "001", "002", "003", "300", "301", "302", "600", "601", "603", "605"))
            risk_name = "ST" in name.upper() or "退" in name
            if len(code) != 6 or not code.isdigit() or not allowed_prefix or risk_name:
                continue
            industry = str(signal.get("industry") or pick.get("industry") or "未知")
            base = _num(signal.get("t_score"), None)
            if base is None:
                base = _num(signal.get("rank_score"), None)
            if base is None:
                base = _num(pick.get("score"), 0.0)
            base = float(base or 0.0)
            if abs(base) <= 1.5:
                base *= 100.0
            duplicate_penalty = 8.0 * max(0, held_codes.get(code, 0))
            industry_penalty = 2.5 * max(0, industries.get(industry, 0) - 1)
            status_penalty = 4.0 if signal.get("status") == "deferred_capacity" else 0.0
            utility = base - duplicate_penalty - industry_penalty - status_penalty
            reasons = []
            if duplicate_penalty:
                reasons.append(f"同股跨策略敞口 -{duplicate_penalty:.1f}")
            if industry_penalty:
                reasons.append(f"行业集中 -{industry_penalty:.1f}")
            if status_penalty:
                reasons.append("容量延期 -4.0")
            rows.append({
                "signal_id": int(signal["id"]), "account_id": signal["account_id"],
                "account_name": ACCOUNT_LABELS.get(signal["account_id"], signal["account_id"]),
                "code": code, "name": name, "industry": industry,
                "signal_date": signal.get("signal_date"), "status": signal.get("status"),
                "base_score": round(base, 3), "utility": round(utility, 3),
                "penalties": {"duplicate": duplicate_penalty, "industry": industry_penalty, "capacity": status_penalty},
                "reasons": reasons or ["未触发额外组合惩罚"],
            })
        rows.sort(key=lambda item: (item["utility"], item["base_score"]), reverse=True)
        selected = []
        account_daily = Counter()
        for item in rows:
            if len(selected) >= CANARY_MAX_NEW_SLOTS:
                break
            if account_daily[item["account_id"]] >= 1:
                continue
            selected.append(dict(item, shadow_action="灰度候选"))
            account_daily[item["account_id"]] += 1
        return {
            "mode": "shadow", "version": "portfolio-arbiter-v1",
            "policy": "跨策略只比较新增候选；同股、行业集中和容量延期扣分。当前不下单、不卖出、不改信号。",
            "candidate_count": len(rows), "max_canary_slots": CANARY_MAX_NEW_SLOTS,
            "selected": selected, "candidates": rows[:12],
            "held_slots": len(positions), "duplicate_code_count": sum(1 for value in held_codes.values() if value > 1),
            "risk_metrics": portfolio_risk,
        }
    finally:
        paper.close()


def _disclosure_scope_codes(day: dt.date) -> list[str]:
    """Collect a bounded disclosure scope from the paper ledger only.

    The public notice feed is intentionally *not* queried for the full market.
    Current positions get priority, followed by codes that had a signal on the
    current trading day.  This keeps the point-in-time evidence refresh small
    and reproducible while leaving the strategy universe untouched.
    """
    if not os.path.exists(PAPER_DB_PATH):
        return []
    codes: list[str] = []
    seen: set[str] = set()
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=8)
    try:
        queries = (
            "SELECT code FROM paper_positions WHERE qty>0 ORDER BY account_id,code LIMIT ?",
            """SELECT code FROM paper_signals
               WHERE signal_date=? OR intended_date=?
               ORDER BY id DESC LIMIT ?""",
        )
        rows = paper.execute(queries[0], (DISCLOSURE_MAX_CODES,)).fetchall()
        for row in rows:
            code = str(row["code"] or "").strip()
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        remaining = max(0, DISCLOSURE_MAX_CODES - len(codes))
        if remaining:
            rows = paper.execute(
                queries[1], (day.isoformat(), day.isoformat(), remaining),
            ).fetchall()
            for row in rows:
                code = str(row["code"] or "").strip()
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
    except sqlite3.Error:
        # A missing/old paper schema must not block the adaptive overview.
        return codes
    finally:
        paper.close()
    return codes[:DISCLOSURE_MAX_CODES]


def _disclosure_input_state(day: dt.date | None = None, *, refresh: bool = False) -> dict:
    """Read/refresh disclosure timestamps for a small, auditable code set.

    This is a shadow data-quality signal.  It does not gate orders or alter
    factors.  Network refreshes are deferred until after the close; before
    then the overview reports the last known cache state without touching the
    upstream endpoint.
    """
    day = day or dt.datetime.now(TZ).date()
    checked_at = _now()
    # Page reads must not issue up to 150 public-notice requests.  Freshness is
    # collected by the scheduled learning path; the UI reads its last evidence
    # and explicitly shows when a scheduled refresh is still pending.
    with _DISCLOSURE_STATE_LOCK:
        cached = _DISCLOSURE_STATE_CACHE.get("data")
        if (not refresh and cached is not None
                and _DISCLOSURE_STATE_CACHE.get("day") == day.isoformat()):
            result = dict(cached)
            result["checked_at"] = result.get("checked_at") or checked_at
            result["read_mode"] = "cached_read_model"
            return result
    codes = _disclosure_scope_codes(day)
    base = {
        "mode": "shadow",
        "source": "eastmoney-public-notice",
        "source_kind": "public_aggregator",
        "scope": "current_positions_and_today_signals",
        "max_codes": DISCLOSURE_MAX_CODES,
        "requested_codes": len(codes),
        "covered_codes": 0,
        "coverage_pct": 0.0,
        "fresh": 0,
        "stale": 0,
        "unknown": 0,
        "latest_published_at": None,
        "checked_at": checked_at,
        "status": "empty" if not codes else "pending",
        "reason": None,
        "records": [],
    }
    if not codes:
        base["reason"] = "当前持仓和当日信号没有可核验代码"
        return base
    if not refresh:
        base["status"] = "awaiting_scheduled_refresh"
        base["reason"] = "页面读取不访问披露源；由盘后学习任务刷新披露证据"
        base["unknown"] = len(codes)
        base["read_mode"] = "read_model_no_network"
        return base
    if dt.datetime.now(TZ).time() < DISCLOSURE_REFRESH_AFTER:
        base["status"] = "deferred_pre_close"
        base["reason"] = "披露时间仅在盘后刷新，避免盘中请求和未完成日数据"
        return base
    try:
        import disclosure_timeline as timeline

        records = timeline.fetch_disclosure_timeline(
            codes,
            timeout=3.5,
            cache_ttl=DISCLOSURE_CACHE_TTL_SECONDS,
            page_size=100,
            max_pages=1,
        )
    except Exception as exc:
        # A notice endpoint outage must remain visible as a quality issue, not
        # turn into a strategy failure or a fabricated negative result.
        base["status"] = "source_error"
        base["reason"] = f"披露源读取失败：{type(exc).__name__}: {exc}"[:300]
        base["unknown"] = len(codes)
        base["records"] = []
        return base
    fresh_statuses = {"live", "cached"}
    fresh = stale = unknown = 0
    latest = None
    compact: list[dict] = []
    for item in records or []:
        status = str(item.get("status") or "unknown")
        if status in fresh_statuses:
            fresh += 1
        elif status == "stale":
            stale += 1
        else:
            unknown += 1
        published = item.get("published_at")
        if published and (latest is None or str(published) > str(latest)):
            latest = published
        compact.append({
            "code": item.get("code"),
            "report_period": item.get("report_period"),
            "published_at": published,
            "status": status,
            "source": item.get("source"),
            "reason": item.get("reason"),
        })
    covered = fresh + stale
    unknown_total = unknown + max(0, len(codes) - len(records or []))
    base.update({
        "covered_codes": covered,
        "coverage_pct": round(100.0 * covered / max(len(codes), 1), 2),
        "fresh": fresh,
        "stale": stale,
        "unknown": unknown_total,
        "latest_published_at": latest,
        "status": "ok" if unknown_total == 0 and stale == 0 else "partial",
        "reason": "来源时间戳仅作影子证据，不直接改变财务值或交易门槛",
        "records": compact[:DISCLOSURE_MAX_CODES],
    })
    with _DISCLOSURE_STATE_LOCK:
        _DISCLOSURE_STATE_CACHE.update(day=day.isoformat(), data=dict(base), ts=time.time())
    return base


def _execution_evidence_state(conn, day: dt.date | None = None, *, persist: bool = False) -> dict:
    """Build the evidence gate for any future evolution authority.

    Returns only measured facts from the paper ledger: whether the day’s
    concrete candidates had a published-report timestamp, whether waiting
    candidates were re-scored on live quotes, whether a slot was borrowed,
    and whether the independent replay invariant check passed.  It never
    changes a strategy, order, risk limit, or model weight.
    """
    day = day or dt.datetime.now(TZ).date()
    state = {
        "version": "execution-evidence-v1",
        "evidence_date": day.isoformat(),
        "candidate_disclosure": {"candidates": 0, "reported": 0, "coverage_pct": 0.0, "status": "empty"},
        "waitlist_realtime": {"candidates": 0, "rescored": 0, "coverage_pct": 0.0, "status": "empty"},
        "slot_borrow": {"events": [], "status": "unavailable"},
        "ledger_replay": {"status": "unavailable", "errors": [], "warnings": []},
        "five_day_disclosure_gate": {"ready": False, "days": 0, "required_days": 5, "required_coverage_pct": 85.0, "history": []},
    }
    if not os.path.exists(PAPER_DB_PATH):
        state["ledger_replay"] = {"status": "missing_paper_ledger", "errors": [], "warnings": []}
        return state
    paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
    try:
        signal_rows = paper.execute(
            "SELECT payload FROM paper_signals WHERE signal_date=? OR intended_date=?",
            (day.isoformat(), day.isoformat()),
        ).fetchall()
        candidates = reported = rescored = 0
        for row in signal_rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            pick = payload.get("pick") or {}
            # A signal row always represents one concrete selection candidate.
            if pick or payload:
                candidates += 1
            evidence = pick.get("financial_evidence") or payload.get("financial_evidence") or {}
            if str(evidence.get("profit_source") or pick.get("profit_source") or "") == "reported":
                reported += 1
            if isinstance(payload.get("waitlist_realtime"), dict):
                rescored += 1
        state["candidate_disclosure"] = {
            "candidates": candidates, "reported": reported,
            "coverage_pct": round(100.0 * reported / max(candidates, 1), 2),
            "status": "ok" if candidates and reported == candidates else ("partial" if reported else ("empty" if not candidates else "shadow")),
        }
        state["waitlist_realtime"] = {
            "candidates": candidates, "rescored": rescored,
            "coverage_pct": round(100.0 * rescored / max(candidates, 1), 2),
            "status": "ok" if not candidates or rescored == candidates else ("partial" if rescored else "not_run"),
        }
        row = paper.execute(
            "SELECT inputs,effective_at FROM paper_position_limit_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            try:
                inputs = json.loads(row["inputs"] or "{}")
            except (TypeError, ValueError):
                inputs = {}
            events = list(inputs.get("slot_borrow_events") or [])[-9:]
            state["slot_borrow"] = {
                "events": events, "count": len(events), "effective_at": row["effective_at"],
                "status": "ok",
            }
    except sqlite3.Error as exc:
        state["candidate_disclosure"]["status"] = "paper_schema_error"
        state["candidate_disclosure"]["reason"] = type(exc).__name__
    finally:
        paper.close()
    try:
        import paper_replay_regression
        replay = paper_replay_regression.validate(PAPER_DB_PATH)
        state["ledger_replay"] = {
            "status": "ok" if replay.get("ok") else "failed",
            "errors": replay.get("errors") or [], "warnings": replay.get("warnings") or [],
            "checks": replay.get("checks") or {},
        }
    except Exception as exc:
        state["ledger_replay"] = {"status": "unavailable", "errors": [type(exc).__name__], "warnings": []}

    if persist:
        now = _now()
        metrics = {
            "candidate_disclosure": state["candidate_disclosure"],
            "waitlist_realtime": state["waitlist_realtime"],
            "ledger_replay": state["ledger_replay"],
        }
        for metric, detail in metrics.items():
            value = detail.get("coverage_pct")
            conn.execute(
                """INSERT INTO adaptive_execution_evidence(evidence_date,metric,status,value,detail,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(evidence_date,metric) DO UPDATE SET status=excluded.status,value=excluded.value,
                   detail=excluded.detail,updated_at=excluded.updated_at""",
                (day.isoformat(), metric, str(detail.get("status") or "unknown"), value,
                 json.dumps(detail, ensure_ascii=False, default=str), now, now),
            )
    history = [_decode_row(row, "detail") for row in conn.execute(
        "SELECT * FROM adaptive_execution_evidence WHERE metric='candidate_disclosure' ORDER BY evidence_date DESC LIMIT 5"
    ).fetchall()]
    state["five_day_disclosure_gate"]["history"] = history
    state["five_day_disclosure_gate"]["days"] = len(history)
    state["five_day_disclosure_gate"]["ready"] = bool(
        len(history) == 5 and all(float(row.get("value") or 0.0) >= 85.0 for row in history)
    )
    return state


def _data_input_state(conn):
    """Profile the five input families and state their safe authority."""
    now = dt.datetime.now(TZ)
    full_path = os.path.join(CACHE_DIR, "market_snapshot_full.json")
    manifest_path = os.path.join(CACHE_DIR, "kline_manifest.json")
    factor_path = os.path.join(CACHE_DIR, "selection_factors.csv")
    health_path = os.path.join(CACHE_DIR, "data_source_health.json")
    snapshot = _load_json(full_path, {}) or {}
    market_rows = snapshot.get("rows") if isinstance(snapshot, dict) else []
    market_rows = market_rows if isinstance(market_rows, list) else []
    valid_price = sum(1 for row in market_rows if (_num(row.get("price"), 0) or 0) > 0 and row.get("quote_at"))
    ohlc_valid = 0
    for row in market_rows:
        price = _num(row.get("price"), 0) or 0
        values = [_num(row.get(key), None) for key in ("open_price", "high", "low", "prev_close")]
        if price > 0 and all(value is not None and value > 0 and 0.2 <= value / price <= 5 for value in values):
            ohlc_valid += 1
    saved_at = snapshot.get("saved_at") if isinstance(snapshot, dict) else None
    freshness_minutes = None
    try:
        stamp = dt.datetime.fromisoformat(str(saved_at).replace("Z", "+00:00"))
        freshness_minutes = max(0.0, (now - stamp.astimezone(TZ)).total_seconds() / 60)
    except (TypeError, ValueError):
        pass
    health = _load_json(health_path, {}) or {}
    manifest = _load_json(manifest_path, {}) or {}
    stocks = manifest.get("stocks") if isinstance(manifest, dict) else {}
    stocks = stocks if isinstance(stocks, dict) else {}
    kline_dates = Counter(str(item.get("last_date") or "") for item in stocks.values())
    # Do not label a handful of intraday/early provider bars as the historical
    # library's completed date.  The shared universe layer owns the completed
    # daily-bar cutoff; the dashboard must report coverage against that target.
    try:
        import universe as U
        completed_kline_date = U.latest_complete_trade_date().isoformat()
    except Exception:
        completed_kline_date = max(kline_dates) if kline_dates else None
    kline_latest_count = kline_dates.get(completed_kline_date, 0) if completed_kline_date else 0
    factor_rows = 0
    factor_columns = []
    try:
        with open(factor_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            factor_columns = next(reader, [])
            factor_rows = sum(1 for _ in reader)
    except OSError:
        pass
    news_events = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
    major_events = conn.execute("SELECT COUNT(*) FROM market_major_events").fetchone()[0]
    paper_counts = {"orders": 0, "fills": 0, "risk_decisions": 0, "nav_days": 0}
    if os.path.exists(PAPER_DB_PATH):
        paper = paper_reader.connect(PAPER_DB_PATH, timeout=30)
        try:
            paper_counts = {
                "orders": paper.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0],
                "fills": paper.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
                "risk_decisions": paper.execute("SELECT COUNT(*) FROM paper_risk_decisions").fetchone()[0],
                "nav_days": paper.execute("SELECT COUNT(DISTINCT nav_date) FROM paper_nav").fetchone()[0],
            }
        finally:
            paper.close()
    fundamental_point_in_time = {"published_at", "announcement_at"}.intersection(set(factor_columns))
    disclosure = _disclosure_input_state(now.date())
    categories = [
        {
            "id": "market", "name": "市场数据", "status": "usable" if len(market_rows) >= 4000 and valid_price / max(len(market_rows), 1) >= .95 else "blocked",
            "coverage_pct": round(100 * valid_price / max(len(market_rows), 1), 2),
            "freshness_minutes": round(freshness_minutes, 1) if freshness_minutes is not None else None,
            "records": len(market_rows), "sources": ["东方财富全市场", "腾讯独立交叉源", "前复权日K"],
            "detail": f"实时有效 {valid_price}/{len(market_rows)}；OHLC范围有效 {ohlc_valid}/{len(market_rows)}；完整日K目标 {completed_kline_date or '未知'} 覆盖 {kline_latest_count}/{len(stocks)}。",
            "authority": "可用于行情、趋势与执行门禁；全市场少于4000行或双源失败时禁止新增。",
        },
        {
            "id": "fundamental", "name": "基本面数据", "status": "shadow" if not fundamental_point_in_time else "usable",
            "coverage_pct": round(100 * factor_rows / max(len(market_rows), 1), 2), "records": factor_rows,
            "sources": ["东方财富财报", "实时PE/PB", "一季报/半年报/年报"],
            "detail": (
                "已有净利润、年净利润、ROE、营收/净利同比、PE/PB；公告披露时间"
                f"影子覆盖 {disclosure['covered_codes']}/{disclosure['requested_codes']}，"
                f"新鲜 {disclosure['fresh']}、过期 {disclosure['stale']}、未知 {disclosure['unknown']}。"
            ),
            "disclosure_timeline": disclosure,
            "authority": "已证实披露的候选财报可用于有上限的排序加分；未知披露只作影子。连续5个交易日覆盖≥85%且样本外验证通过前，不升级利润硬门。",
        },
        {
            "id": "alternative", "name": "另类数据", "status": "shadow",
            "coverage_pct": None, "records": int(news_events) + int(major_events),
            "sources": ["公司公告", "快讯/新闻", "板块热度", "资金流代理"],
            "detail": f"事件账本 {news_events} 条、重大事件 {major_events} 条；没有合规L2逐笔、盘口队列和真实主力账户数据。",
            "authority": "新闻/公告可做门禁和影子学习；洗盘/出货、主力意图仅为代理判断，不可单独下单。",
        },
        {
            "id": "transaction", "name": "交易数据", "status": "usable",
            "coverage_pct": 100.0, "records": paper_counts["orders"],
            "sources": ["模拟委托", "模拟成交", "T+1批次", "费用/滑点", "风控决策"],
            "detail": f"委托 {paper_counts['orders']}、成交 {paper_counts['fills']}、风控决策 {paper_counts['risk_decisions']}、净值日 {paper_counts['nav_days']}。",
            "authority": "可用于模拟盘归因与反馈；未接QMT/PTrade，不能代表券商真实回报或盘口成交。",
        },
        {
            "id": "macro", "name": "宏观数据", "status": "partial",
            "coverage_pct": None, "records": 7,
            "sources": ["沪深300", "全市场宽度", "道指", "纳指100", "恒生", "美元指数"],
            "detail": "已有指数、市场宽度与海外风险代理；尚缺利率、汇率曲线、商品、社融/CPI/PMI等点时宏观序列。",
            "authority": "当前仅用于市场灯号和风险缩放，不得作为单股直接买卖信号。",
        },
    ]
    blockers = []
    if ohlc_valid < int(len(market_rows) * .95):
        blockers.append("市场OHLC字段覆盖未达95%；已启用范围清洗，等待下一份全量快照刷新")
    if not fundamental_point_in_time:
        blockers.append("财务因子缺披露时间，无法证明严格点时可见")
    blockers.extend(["无L2逐笔/盘口，主力意图只能影子", "宏观数据只覆盖市场代理，尚非完整宏观库"])
    return {
        "version": "data-input-bus-v1", "asof": _now(), "categories": categories,
        "blockers": blockers, "market_health": health,
        "rules": ["来源与时间戳必须随证据链保存", "缺失不允许静默代理", "交易权限按数据等级分层", "全市场覆盖不足时停止新增但保留硬风险退出"],
    }


def _decode_row(row, *json_fields):
    if row is None:
        return None
    data = dict(row)
    for field in json_fields:
        try:
            data[field] = json.loads(data[field])
        except (ValueError, TypeError, KeyError):
            data[field] = {} if field != "drivers" else []
    return data


def _compact_trade_attribution_overview(payload):
    """Keep the adaptive landing page fast; detailed evidence has its own API.

    A single attribution row can carry full news, market and AI context.  Those
    blobs are valuable when an operator opens the attribution drill-down, but
    shipping thirty of them with every self-evolution tab render made the
    overview response several megabytes.  The landing page only renders the
    compact fields below.
    """
    payload = dict(payload or {})
    fields = (
        "id", "account_id", "order_status", "fill_date", "name", "code", "side",
        "qty", "fill_price", "close_price", "reason_codes", "benchmark_move_pct",
        "stock_alpha_pct", "stock_move_pct", "ai_status", "news_impact_score",
    )
    recent = []
    for row in payload.get("recent") or []:
        item = {field: row.get(field) for field in fields}
        # Stored JSON is small enough to show but must remain bounded even for
        # malformed legacy records.
        reason = item.get("reason_codes")
        if isinstance(reason, str) and len(reason) > 1200:
            item["reason_codes"] = reason[:1200] + "…"
        recent.append(item)
    payload["recent"] = recent[:12]
    return payload


def _overview_uncached():
    with _connect() as conn:
        # 闭环指标：执行质量审计和午间观测上下文
        try:
            import execution_quality_shadow as eqs
            execution_quality = eqs.audit(PAPER_DB_PATH, limit=3000)
        except Exception:
            execution_quality = {"status": "unavailable"}
        today = dt.datetime.now(TZ).date().isoformat()
        midday_ctx = conn.execute(
            "SELECT * FROM adaptive_intraday_profiles WHERE profile_date=? AND session='midday' ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
        midday_profile = dict(midday_ctx) if midday_ctx else None

        cfg = _config(conn)
        profile = _decode_row(
            conn.execute("SELECT * FROM adaptive_market_profiles ORDER BY profile_date DESC,id DESC LIMIT 1").fetchone(),
            "features", "drivers",
        )
        decision = _decode_row(
            conn.execute("SELECT * FROM adaptive_decisions ORDER BY decision_date DESC,id DESC LIMIT 1").fetchone(),
            "weights", "scores", "evidence",
        )
        rewards = [dict(row) for row in conn.execute(
            "SELECT * FROM adaptive_rewards ORDER BY end_date DESC,id DESC LIMIT 120"
        )]
        runs = [_decode_row(row, "detail") for row in conn.execute(
            "SELECT * FROM adaptive_runs ORDER BY id DESC LIMIT 12"
        )]
        feedback = [dict(row) for row in conn.execute(
            "SELECT * FROM adaptive_feedback ORDER BY id DESC LIMIT 30"
        )]
        profiles = [_decode_row(row, "features", "drivers") for row in conn.execute(
            "SELECT * FROM adaptive_market_profiles ORDER BY profile_date DESC,id DESC LIMIT 20"
        )]
        intraday_profiles = [_decode_row(row, "features", "drivers") for row in conn.execute(
            "SELECT * FROM adaptive_intraday_profiles ORDER BY profile_date DESC,id DESC LIMIT 20"
        )]
        intraday_sample_rows = conn.execute(
            "SELECT COUNT(*) FROM adaptive_intraday_samples"
        ).fetchone()[0]
        alpha_run = _decode_row(
            conn.execute("SELECT * FROM adaptive_alpha_runs ORDER BY run_date DESC,id DESC LIMIT 1").fetchone(),
            "detail",
        )
        alpha_candidates = [_decode_row(row, "genome") for row in conn.execute(
            "SELECT * FROM adaptive_alpha_candidates ORDER BY run_date DESC,validation_fitness DESC,id DESC LIMIT 8"
        )]
        alpha_profile_days = conn.execute(
            "SELECT COUNT(DISTINCT profile_date) FROM adaptive_alpha_samples"
        ).fetchone()[0]
        alpha_sample_rows = conn.execute("SELECT COUNT(*) FROM adaptive_alpha_samples").fetchone()[0]
        alpha_mature_rows = conn.execute("SELECT COUNT(*) FROM adaptive_alpha_returns").fetchone()[0]
        mature_reward_count = conn.execute("SELECT COUNT(*) FROM adaptive_rewards").fetchone()[0]
        risk_optimizer = risk_evolution.overview(conn, cfg, PAPER_DB_PATH)
        selection_optimizer = selection_evolution.overview(conn, cfg, PAPER_DB_PATH)
        advisor = deepseek_advisor.overview(conn, cfg)
        advisor["tasks"] = deepseek_research.task_catalog()
        # A4：AI 调参门禁连续拦截可观测——空转数周无人察觉的根因
        ai_tuning_rows = [r[0] for r in conn.execute(
            "SELECT status FROM adaptive_ai_tuning_runs ORDER BY id DESC LIMIT 40"
        ).fetchall()]
        _consecutive_blocked = 0
        _last_blocked_status = None
        for _s in ai_tuning_rows:
            if isinstance(_s, str) and _s.startswith("blocked"):
                _consecutive_blocked += 1
                if _last_blocked_status is None:
                    _last_blocked_status = _s
            else:
                break
        ai_tuning_health = {
            "consecutive_blocked": _consecutive_blocked,
            "last_blocked_status": _last_blocked_status,
            "alarm": _consecutive_blocked >= 5,
            "note": ("AI 调参门禁已连续拦截 %d 次：请检查第二数据源(cross_source)状态，或在配置中放宽 llm_realtime_require_cross_source"
                     % _consecutive_blocked) if _consecutive_blocked >= 5 else None,
        }
        news_center = news_learning.overview(conn)
        trade_attribution_view = _compact_trade_attribution_overview(trade_attribution.overview(conn))
        evidence_row = conn.execute(
            """SELECT COUNT(*) AS orders,
                      SUM(CASE WHEN risk_decision_id IS NOT NULL AND COALESCE(integrity_flags,'') NOT LIKE '%missing_signal%' THEN 1 ELSE 0 END) AS linked,
                      SUM(CASE WHEN integrity_status='valid' THEN 1 ELSE 0 END) AS valid,
                      SUM(CASE WHEN ledger_type='actual' THEN 1 ELSE 0 END) AS actual
                 FROM adaptive_evidence_chains"""
        ).fetchone()
        evidence_orders = int(evidence_row["orders"] or 0)
        evidence_actual = int(evidence_row["actual"] or 0)
        evidence_result = {
            "orders": evidence_orders,
            "linked": int(evidence_row["linked"] or 0),
            "valid": int(evidence_row["valid"] or 0),
            "actual": evidence_actual,
            "counterfactual": max(0, evidence_orders - evidence_actual),
        }
        evidence_result["link_pct"] = round(100 * evidence_result["linked"] / max(evidence_orders, 1), 2)
        evidence_result["valid_pct"] = round(100 * evidence_result["valid"] / max(evidence_orders, 1), 2)
        closed_loop = _closed_loop_state(conn, evidence_result)
        portfolio_shadow = _background_shadow_read(
            "portfolio-shadow", 60.0, _portfolio_shadow_arbitration,
            {"version": "portfolio-shadow-v2", "read_only": True, "trading_impact": "none"},
        )
        data_inputs = _data_input_state(conn)
        execution_evidence = _execution_evidence_state(conn, dt.datetime.now(TZ).date(), persist=False)
        factor_quality = _background_shadow_read(
            "factor-quality", 120.0,
            lambda: factor_quality_shadow.report_from_cache(
                CACHE_DIR,
                tracking_db_path=os.path.join(CACHE_DIR, "selection_tracking.db"),
            ),
            {"version": factor_quality_shadow.QUALITY_VERSION, "read_only": True, "trading_impact": "none"},
        )
        neural_control = neural_shadow.control_status(conn)

    # 新闻/公告不再在自进化和风控中心各自维护一套口径；自进化只读取
    # 风控中心最近一次动态快照，并在此基础上做兑现学习。
    risk_snapshot = paper_risk_center.load_snapshot() or {}
    dynamic_risk = risk_snapshot.get("dynamic_risk") or paper_risk_center.dynamic_risk_state(
        market=risk_snapshot.get("market") or {},
        news_events=(risk_snapshot.get("news") or {}).get("events") or [],
        news_error=(risk_snapshot.get("news") or {}).get("error"),
        positions=risk_snapshot.get("positions") or [],
    )

    horizon_summary = []
    for account_id in sorted(ACCOUNT_LABELS):
        for horizon in HORIZON_WEIGHTS:
            subset = [row for row in rewards if row["account_id"] == account_id and row["horizon"] == horizon]
            horizon_summary.append({
                "account_id": account_id,
                "name": ACCOUNT_LABELS[account_id],
                "horizon": horizon,
                "samples": len(subset),
                "mean_return_pct": round(statistics.mean([row["strategy_return_pct"] for row in subset]), 3) if subset else None,
                "mean_excess_pct": round(statistics.mean([row["excess_return_pct"] for row in subset]), 3) if subset else None,
                "mean_reward": round(statistics.mean([row["raw_reward"] for row in subset]), 3) if subset else None,
            })
    stage = decision["stage"] if decision else "not_started"
    stage_labels = {
        "not_started": "尚未运行",
        "collecting": "样本积累",
        "shadow": "影子学习",
        "regime_validation": "跨盘面验证",
        "advisory": "建议模式",
        "eligible_for_review": "可申请人工放权",
    }
    feedback_by_strategy = defaultdict(list)
    for row in feedback:
        feedback_by_strategy[row["account_id"]].append(row)
    # 闭环完整性检查
    loop_integrity = {
        "news_to_bandit": bool(decision and any(
            (evidence or {}).get("news_overlay_bonus", 0) != 0
            for evidence in (decision.get("evidence") or {}).values() if isinstance(evidence, dict)
        )),
        "execution_quality_recorded": bool(execution_quality.get("status") == "ready"),
        "midday_context_available": bool(midday_profile),
        "trade_attribution_linked": bool((evidence_result or {}).get("linked", 0) > 0),
        "news_factor_active": bool(
            (news_center.get("factor") or {}).get("status") == "micro_eligible"
        ),
        "factor_quality_shadow": bool(factor_quality.get("status") == "ready"),
        "neural_gate": neural_control.get("status", "unknown"),
    }

    return {
        "engine": {
            "version": ENGINE_VERSION,
            "mode": cfg["mode"],
            "mode_label": "模拟盘双轨进化；1日影子观测、3日小步调整，不连接实盘" if cfg["mode"] == "shadow" else "模拟盘建议模式",
            "stage": stage,
            "stage_label": stage_labels.get(stage, stage),
            "principle": "以模拟盘净值、成交和回撤为主证据；1日只观测，3日小步调整，5日标准验证，10日成熟确认；风险放宽仍需人工审批。",
            "human_approval_required": bool(cfg["human_approval_required"]),
            "mature_reward_count": mature_reward_count,
            "profile_count": len(profiles),
            "intraday_observation_count": len(intraday_profiles),
            "intraday_sample_rows": intraday_sample_rows,
        },
        "market_profile": profile,
        "decision": decision,
        "horizon_summary": horizon_summary,
        "feedback": feedback,
        "feedback_by_strategy": dict(feedback_by_strategy),
        "runs": runs,
        "profiles": profiles,
        "intraday_profiles": intraday_profiles,
        "intraday_sample_rows": intraday_sample_rows,
        "alpha_lab": {
            "architecture": "可审计特征变换器 → 遗传算法候选 Alpha → 样本外验证 → Bandit 候选池",
            "neural_network": bool(neural_control.get("approved")),
            "neural_control": neural_control,
            "features": list(ALPHA_FEATURES),
            "profile_days": alpha_profile_days,
            "sample_rows": alpha_sample_rows,
            "mature_rows": alpha_mature_rows,
            "required_profile_days": ALPHA_MIN_PROFILE_DAYS,
            "required_mature_rows": ALPHA_MIN_MATURE_ROWS,
            "latest_run": alpha_run,
            "candidates": alpha_candidates,
        },
        "risk_optimizer": risk_optimizer,
        "selection_optimizer": selection_optimizer,
        "deepseek_advisor": advisor,
        "news_learning": news_center,
        "trade_attribution": trade_attribution_view,
        "closed_loop": closed_loop,
        "portfolio_shadow": portfolio_shadow,
        "data_inputs": data_inputs,
        "execution_evidence": execution_evidence,
        "loop_integrity": loop_integrity,
        "execution_quality_summary": {
            "status": execution_quality.get("status"),
            "orders": execution_quality.get("orders", 0),
            "by_strategy": execution_quality.get("by_strategy", []),
        },
        "factor_quality_shadow": factor_quality,
        "neural_control": neural_control,
        "dynamic_risk": dynamic_risk,
        "guardrails": [
            {"name": "无未来函数", "status": "active", "detail": "只在持有周期完整结束后生成奖励。"},
            {"name": "多周期兑现", "status": "active", "detail": "1/3/5 日仍分别兑现 20%/35%/45%；新近样本按半衰期加权，短期信号不能独占结论。"},
            {"name": "回撤与换手惩罚", "status": "active", "detail": "超额收益会扣除窗口回撤和成交周转成本。"},
            {"name": "策略权重边界", "status": "active", "detail": f"单策略建议权重限制在 {cfg['min_strategy_weight_pct']:.0f}%—{cfg['max_strategy_weight_pct']:.0f}%。"},
            {"name": "受约束风控晋级", "status": "locked", "detail": "达标后仅保守收紧可自动次日生效；放宽风险必须人工批准，并可一键回滚。"},
        ],
        "ai_tuning_health": ai_tuning_health,
        "data_note": "1日只做影子观测；连续3日且数据质量通过后才允许小步自动调整，5日/10日用于验证与成熟确认。",
    }


def overview(*, force: bool = False):
    """Return a short-lived read snapshot for the self-evolution page.

    This is deliberately a UI cache only: scheduled learning and every order
    path still read/write their own authoritative ledgers.
    """
    now = time.time()
    if not force and _OVERVIEW_CACHE["data"] is not None and now - _OVERVIEW_CACHE["ts"] < _OVERVIEW_CACHE_TTL_SECONDS:
        return _OVERVIEW_CACHE["data"]
    with _OVERVIEW_CACHE_LOCK:
        now = time.time()
        if not force and _OVERVIEW_CACHE["data"] is not None and now - _OVERVIEW_CACHE["ts"] < _OVERVIEW_CACHE_TTL_SECONDS:
            return _OVERVIEW_CACHE["data"]
        result = _overview_uncached()
        _OVERVIEW_CACHE.update(data=result, ts=time.time())
        return result


def self_test():
    profile = _market_profile()
    assert profile["regime"] in REGIME_LABELS
    assert 0 <= profile["features"]["capital_momentum_score"] <= 100
    with _connect() as conn:
        _store_profile(conn, profile)
        _capture_alpha_samples(conn, profile)
        _mature_alpha_returns(conn)
        alpha_lab = _run_alpha_lab(conn, profile["profile_date"])
        _evaluate_rewards(conn)
        decision = _bandit_decision(conn, profile, conn.execute(
            "SELECT id FROM adaptive_market_profiles WHERE profile_date=?", (profile["profile_date"],)
        ).fetchone()["id"])
        assert abs(sum(decision["weights"].values()) - 100) <= 0.2
        assert alpha_lab["status"] in {"waiting_data", "waiting_validation_window", "completed"}
    return {"profile": profile["regime"], "valid_rows": profile["valid_rows"],
            "alpha_lab": alpha_lab["status"], "status": "ok"}


if __name__ == "__main__":
    print(_json(self_test()))

# ─── 双AI共识调参函数 ───


# ─── 自进化调参系统函数 ───

def evolution_status_fn():
    """返回自进化调参系统的完整状态。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.evolution_status(conn)


def get_evolution_params_fn():
    """获取当前进化参数。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.get_current_params(conn)


def update_evolution_params_fn(adjustments, reason="manual"):
    """手动调整进化参数。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.manual_adjust(conn, adjustments, reason=reason)


def trigger_evolution_fn():
    """手动触发一次进化。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.evolve(conn, reason="manual_trigger")


def auto_evolution_fn():
    """自动检查并执行进化。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        result = self_evolution.auto_evolve_if_needed(conn)
        if result is None:
            return {"evolved": False, "reason": "当前无需进化"}
        return result


def evolution_metrics_fn(window=20):
    """获取调参性能指标。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.get_performance_metrics(conn, window)


def evolution_history_fn(limit=20):
    """获取进化参数历史。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.get_evolution_history(conn, limit)


def evolution_log_fn(limit=50):
    """获取进化事件日志。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        return self_evolution.get_evolution_log(conn, limit)


def evaluate_tuning_fn(tracking_id, eval_score, eval_detail=None):
    """事后评估一次调参的效果。"""
    with _connect() as conn:
        self_evolution.ensure_schema(conn)
        self_evolution.evaluate_run(conn, tracking_id, eval_score, eval_detail)
        return {"success": True, "tracking_id": tracking_id, "eval_score": eval_score}


def modlens_status_fn():
    """返回 modlens 视觉桥接模块的状态。"""
    try:
        import modlens_bridge
        return modlens_bridge.self_check()
    except ImportError:
        return {"modlens_available": False, "error": "modlens_bridge 未安装"}


def modlens_read_image_fn(path, prompt=None):
    """使用 modlens 读取图片。"""
    import modlens_bridge
    result = modlens_bridge.read_image(path, prompt)
    result["formatted_text"] = modlens_bridge.format_for_prompt(result)
    return result


def dual_ai_status_fn():
    """返回双AI共识调参系统的状态。"""
    with _connect() as conn:
        dual_ai_tuner.ensure_schema(conn)
        return dual_ai_tuner.dual_ai_status(conn)


def get_dual_ai_api_keys_fn():
    """返回双AI API Key配置状态（不泄露明文）。"""
    with _connect() as conn:
        dual_ai_tuner.ensure_schema(conn)
        return dual_ai_tuner.get_api_keys(conn)


def update_dual_ai_api_key_fn(provider, api_key=None, base_url=None, model=None, enabled=None):
    """更新某个AI供应商的API配置。"""
    with _connect() as conn:
        dual_ai_tuner.ensure_schema(conn)
        return dual_ai_tuner.update_api_key(conn, provider, api_key=api_key, base_url=base_url, model=model, enabled=enabled)


def run_dual_ai_tuning_fn(trigger="manual", mode="intraday"):
    """执行一次双AI并行调参。"""
    with _connect() as conn:
        cfg = _config(conn)
    profile = _current_profile_snapshot()
    return dual_ai_tuner.run_dual_ai_tuning(
        connect_factory=_connect,
        paper_db_path=PAPER_DB_PATH,
        snapshot_paths=SNAPSHOT_PATHS,
        evidence_collector=deepseek_advisor.collect_evidence,
        tuning_accounts_fn=deepseek_advisor._tuning_accounts,
        config=cfg,
        profile=profile,
        trigger=trigger,
        mode=mode,
    )


def _current_profile_snapshot():
    """读取当前市场画像快照，供双AI调参使用。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT profile_date, regime, quality FROM adaptive_market_profiles "
                "ORDER BY profile_date DESC LIMIT 1"
            ).fetchone()
            if row:
                return {"profile_date": row[0], "regime": row[1], "quality": row[2]}
    except Exception:
        pass
    return {}
