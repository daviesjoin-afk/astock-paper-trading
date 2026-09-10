"""Validated runtime settings for the paper-trading workspace.

The paper ledger is deliberately the source of truth for operator-facing
settings.  Values are JSON encoded so the store remains easy to migrate, while
the validation layer keeps risk controls inside conservative bounds.  Secrets
are intentionally out of scope; AI credentials stay in the adaptive database
and are only exposed through its masked API.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

import strategy_registry as SR


# Built-in values are seed preferences only.  The registry, not this mapping,
# decides which strategy ids exist or are eligible for a new cycle.
STRATEGY_DEFAULTS = {
    "tq_breakout": {"style": "strong", "max_positions": 3, "max_weight_pct": 32.0, "max_exposure_pct": 95.0},
    "trend_pullback": {"style": "pullback", "max_positions": 3, "max_weight_pct": 34.0, "max_exposure_pct": 95.0},
    "sector_rotation": {"style": "sector", "max_positions": 3, "max_weight_pct": 32.0, "max_exposure_pct": 92.0},
    "reported_profit_breakout": {"style": "quality", "max_positions": 3, "max_weight_pct": 32.0, "max_exposure_pct": 90.0},
    "main_force_top10": {"style": "main_force", "max_positions": 3, "max_weight_pct": 34.0, "max_exposure_pct": 95.0},
}

# ``0`` means no planned end date (long-term).  The UI presents this as
# ``long_term`` but the numeric representation keeps date arithmetic simple.
CYCLE_DURATION_OPTIONS = (15, 30, 60, 90, 180, 0)

DEFAULTS = {
    "default_starting_capital": 300000.0,
    "cycle_duration_days": 0,
    "enabled_strategies": [],
    "shared_pool_position_limit": 15,
    "shared_pool_exposure_cap": 0.82,
    "single_position_max_amount": 0.0,
    "minimum_entry_slot_utilization": 0.60,
    "evolution_interval_hours": 24,
    "strategy_overrides": {},
    # 执行画像执行器开关（PR-11）：批量窗口 / 人工核验 / TTL 清扫。
    "execution_batch_gate": True,
    "execution_verification_gate": False,
    "execution_ttl_sweep": True,
    # 组合级单票上限（占共享池净值百分比）；0 = 关闭（按策略权重各自约束）。
    "symbol_aggregate_cap_pct": 0.0,
    # PR-30 组合级主题（theme）聚合上限（占共享池净值百分比）；0 = 关闭。
    "theme_aggregate_cap_pct": 0.0,
}

SETTING_GROUPS = {
    "simulation": ("default_starting_capital", "cycle_duration_days", "enabled_strategies"),
    "risk": (
        "shared_pool_position_limit", "shared_pool_exposure_cap",
        "single_position_max_amount", "minimum_entry_slot_utilization",
        "symbol_aggregate_cap_pct", "theme_aggregate_cap_pct",
    ),
    "strategy": ("strategy_overrides",),
    "evolution": ("evolution_interval_hours",),
    "execution": ("execution_batch_gate", "execution_verification_gate", "execution_ttl_sweep"),
}

METADATA = {
    "default_starting_capital": {"label": "默认启动金额", "unit": "元", "apply_mode": "next_cycle", "recommended": 300000, "description": "创建新模拟周期时预填的共享资金池金额。"},
    "cycle_duration_days": {"label": "模拟周期", "unit": "交易日", "apply_mode": "next_cycle", "recommended": 0, "description": "新周期的计划观察时长；长期表示不设自动到期。"},
    "enabled_strategies": {"label": "启用策略", "apply_mode": "next_cycle", "recommended": [], "description": "下一周期参与分配、扫描和风控的策略集合，候选来自可运行的策略注册表，历史周期不重写；留空表示零策略 idle 周期（无新信号，风控与存量退出照常）。"},
    "shared_pool_position_limit": {"label": "共享池持仓上限", "unit": "席", "apply_mode": "immediate", "recommended": 15, "description": "共享资金池的有效持仓席位硬上限。"},
    "shared_pool_exposure_cap": {"label": "共享池敞口上限", "unit": "%", "apply_mode": "immediate", "recommended": 82, "description": "所有策略合计持仓与待成交金额的硬敞口。"},
    "single_position_max_amount": {"label": "单票最大金额", "unit": "元", "apply_mode": "immediate", "recommended": 0, "description": "0 表示按策略权重自动计算；大于 0 时作为额外绝对上限。"},
    "minimum_entry_slot_utilization": {"label": "最小建仓席位利用率", "unit": "%", "apply_mode": "immediate", "recommended": 60, "description": "动态最小建仓金额使用的席位金额比例，剩余空间留给风控加仓。"},
    "evolution_interval_hours": {"label": "自进化周期", "unit": "小时", "apply_mode": "next_run", "recommended": 24, "description": "后台收盘学习任务之间的最短间隔。"},
    "strategy_overrides": {"label": "策略参数", "apply_mode": "next_cycle", "recommended": {}, "description": "每套策略的风格、席位数和风险权重；按可运行策略的风险画像生成。"},
    "execution_batch_gate": {"label": "批量撮合窗口", "apply_mode": "immediate", "recommended": True, "description": "轮动画像的委托挂起至收盘前批量窗口统一撮合；窗口内到达的委托仍立即成交。"},
    "execution_verification_gate": {"label": "事件人工核验", "apply_mode": "immediate", "recommended": False, "description": "开启后事件画像的每笔买入都需人工放行；关闭时按普通限价路径执行。"},
    "execution_ttl_sweep": {"label": "执行时限清扫", "apply_mode": "immediate", "recommended": True, "description": "清扫到期挂起委托：严格时限画像作废，其余自动放回重试管道。"},
    "symbol_aggregate_cap_pct": {"label": "组合级单票上限", "unit": "%", "apply_mode": "immediate", "recommended": 0, "description": "所有策略对同一标的的持仓+在途合计占共享池净值上限；0 表示关闭（仅按各策略权重约束）。"},
    "theme_aggregate_cap_pct": {"label": "组合级主题上限", "unit": "%", "apply_mode": "immediate", "recommended": 0, "description": "所有策略对同一主题（行业聚合组）的持仓+在途合计占共享池净值上限；0 表示关闭。"},
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str, fallback: Any = None) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def eligible_strategy_ids(conn: sqlite3.Connection) -> list[str]:
    """Return the registry's current new-cycle candidates, never a static list."""
    SR.ensure_schema(conn)
    return list(SR.active_ids(conn=conn))


def _override_from_profile(conn: sqlite3.Connection, strategy_id: str) -> dict[str, Any]:
    """Compile a settings default from the strategy's durable risk profile."""
    # Preserve the reviewed five built-in seed defaults.  They are defaults,
    # not an eligibility boundary; every user definition below is generated
    # from its own compiled risk profile.
    if strategy_id in STRATEGY_DEFAULTS:
        return dict(STRATEGY_DEFAULTS[strategy_id])
    readiness = SR.runtime_readiness(conn, strategy_id)
    profile = readiness.get("risk_profile") or {}
    soft = profile.get("soft_limits") or {}
    fingerprint = readiness.get("fingerprint") or {}
    archetype = str(fingerprint.get("archetype") or "").lower()
    style = {
        "breakout": "strong", "momentum": "strong", "trend": "pullback",
        "mean_reversion": "pullback", "rotation": "sector", "event": "quality",
        "event_driven": "quality", "flow": "main_force", "flow_momentum": "main_force",
    }.get(archetype, "strong")
    return {
        "style": style,
        "max_positions": int(soft.get("max_positions", 3)),
        "max_weight_pct": round(float(soft.get("max_weight", 0.30)) * 100, 6),
        "max_exposure_pct": round(float(soft.get("max_exposure", 0.82)) * 100, 6),
    }


def strategy_override_defaults(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result = {}
    for strategy_id in eligible_strategy_ids(conn):
        try:
            result[strategy_id] = _override_from_profile(conn, strategy_id)
        except Exception:
            # A registry item is independently lifecycle-gated; keep settings
            # conservative if a legacy/native profile cannot be compiled here.
            result[strategy_id] = dict(STRATEGY_DEFAULTS.get(strategy_id, {
                "style": "strong", "max_positions": 3,
                "max_weight_pct": 30.0, "max_exposure_pct": 82.0,
            }))
    return result


def ensure_schema(conn: sqlite3.Connection) -> None:
    SR.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_runtime_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT 'system'
        );
        CREATE TABLE IF NOT EXISTS paper_runtime_settings_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runtime_settings_audit_recent
            ON paper_runtime_settings_audit(created_at DESC, id DESC);
        """
    )
    # Existing installations need these columns without rebuilding their
    # ledger.  NULL means legacy/all strategies and is handled by readers.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_cycles)").fetchall()}
    if "duration_days" not in columns:
        conn.execute("ALTER TABLE paper_cycles ADD COLUMN duration_days INTEGER")
    if "enabled_strategies" not in columns:
        conn.execute("ALTER TABLE paper_cycles ADD COLUMN enabled_strategies TEXT")
    for group in SETTING_GROUPS:
        for key in SETTING_GROUPS[group]:
            if conn.execute("SELECT 1 FROM paper_runtime_settings WHERE key=?", (key,)).fetchone():
                continue
            value = defaults(conn)[key]
            conn.execute(
                "INSERT INTO paper_runtime_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?)",
                (key, _json(value), dt.datetime.now().isoformat(timespec="seconds"), "system-default"),
            )


def defaults(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    result = json.loads(_json(DEFAULTS))
    if conn is not None:
        result["enabled_strategies"] = eligible_strategy_ids(conn)
        result["strategy_overrides"] = strategy_override_defaults(conn)
    return result


def _flat_read(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(conn)
    result = defaults(conn)
    for row in conn.execute("SELECT key,value FROM paper_runtime_settings"):
        if row[0] in result:
            value = _decode(row[1], result[row[0]])
            if row[0] == "strategy_overrides" and isinstance(value, dict):
                # Existing records are a user overlay.  Rebase it on today's
                # registry-derived profiles so newly active strategies receive
                # defaults and archived ones cease being configuration targets.
                generated = result["strategy_overrides"]
                result[row[0]] = {
                    strategy_id: {**generated[strategy_id], **value.get(strategy_id, {})}
                    for strategy_id in generated
                }
            else:
                result[row[0]] = value
    return result


def read(conn: sqlite3.Connection) -> dict[str, Any]:
    flat = _flat_read(conn)
    return {
        group: {key: flat[key] for key in keys}
        for group, keys in SETTING_GROUPS.items()
    }


def flat_read(conn: sqlite3.Connection) -> dict[str, Any]:
    return _flat_read(conn)


def metadata(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    result = json.loads(_json(METADATA))
    if conn is not None:
        result["enabled_strategies"]["recommended"] = eligible_strategy_ids(conn)
        result["strategy_overrides"]["recommended"] = strategy_override_defaults(conn)
    return result


def get(conn: sqlite3.Connection, key: str, fallback: Any = None) -> Any:
    try:
        row = conn.execute("SELECT value FROM paper_runtime_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return fallback
    if not row:
        return fallback
    return _decode(row[0], fallback)


def _number(value: Any, key: str, low: float, high: float, integer: bool = False) -> float | int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}必须是数字") from exc
    if number != number or number < low or number > high:
        raise ValueError(f"{key}必须在 {low:g} 至 {high:g} 之间")
    if integer:
        if int(number) != number:
            raise ValueError(f"{key}必须是整数")
        return int(number)
    return round(number, 6)


def validate(updates: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("设置更新必须是对象")
    unknown = set(updates) - set(DEFAULTS)
    if unknown:
        raise ValueError("不允许修改的设置: " + ",".join(sorted(unknown)))
    checked: dict[str, Any] = {}
    if "default_starting_capital" in updates:
        checked["default_starting_capital"] = _number(updates["default_starting_capital"], "默认启动金额", 1000, 10_000_000)
    if "cycle_duration_days" in updates:
        value = updates["cycle_duration_days"]
        if isinstance(value, str) and value.lower() in {"long_term", "long-term", "长期"}:
            value = 0
        checked["cycle_duration_days"] = _number(value, "模拟周期", 0, 180, integer=True)
        if checked["cycle_duration_days"] not in CYCLE_DURATION_OPTIONS:
            raise ValueError("模拟周期只支持 15、30、60、90、180 天或长期")
    if "enabled_strategies" in updates:
        value = updates["enabled_strategies"]
        if not isinstance(value, list):
            raise ValueError("enabled_strategies 必须是列表")
        # PR-47：空列表合法 = 下一周期零策略 idle（风控/退出/调度照常，
        # 只是没有新信号与新开仓）。
        normalized = []
        eligible = set(eligible_strategy_ids(conn)) if conn is not None else set(SR.active_ids())
        for item in value:
            item = str(item)
            if item not in eligible:
                raise ValueError(f"未知策略: {item}")
            if item not in normalized:
                normalized.append(item)
        checked["enabled_strategies"] = normalized
    if "shared_pool_position_limit" in updates:
        checked["shared_pool_position_limit"] = _number(updates["shared_pool_position_limit"], "共享池持仓上限", 1, 30, integer=True)
    if "shared_pool_exposure_cap" in updates:
        value = _number(updates["shared_pool_exposure_cap"], "共享池敞口上限", 0.35, 0.95)
        checked["shared_pool_exposure_cap"] = value
    if "single_position_max_amount" in updates:
        checked["single_position_max_amount"] = _number(updates["single_position_max_amount"], "单票最大金额", 0, 10_000_000)
    if "minimum_entry_slot_utilization" in updates:
        checked["minimum_entry_slot_utilization"] = _number(updates["minimum_entry_slot_utilization"], "最小建仓席位利用率", 0.30, 0.80)
    if "symbol_aggregate_cap_pct" in updates:
        checked["symbol_aggregate_cap_pct"] = _number(updates["symbol_aggregate_cap_pct"], "组合级单票上限", 0, 25)
    if "theme_aggregate_cap_pct" in updates:
        checked["theme_aggregate_cap_pct"] = _number(updates["theme_aggregate_cap_pct"], "组合级主题上限", 0, 40)
    if "evolution_interval_hours" in updates:
        checked["evolution_interval_hours"] = _number(updates["evolution_interval_hours"], "自进化周期", 1, 168, integer=True)
    if "strategy_overrides" in updates:
        raw = updates["strategy_overrides"]
        if not isinstance(raw, dict):
            raise ValueError("策略参数必须是对象")
        eligible = eligible_strategy_ids(conn) if conn is not None else list(SR.active_ids())
        unknown = set(raw) - set(eligible)
        if unknown:
            raise ValueError(f"未知策略: {sorted(unknown)[0]}")
        generated = strategy_override_defaults(conn) if conn is not None else {
            strategy_id: dict(STRATEGY_DEFAULTS.get(strategy_id, {
                "style": "strong", "max_positions": 3, "max_weight_pct": 30, "max_exposure_pct": 82,
            })) for strategy_id in eligible
        }
        checked_overrides = {}
        for strategy_id in eligible:
            defaults_for_strategy = generated[strategy_id]
            candidate = raw.get(strategy_id, defaults_for_strategy)
            if not isinstance(candidate, dict):
                raise ValueError(f"{strategy_id}策略参数必须是对象")
            style = str(candidate.get("style", defaults_for_strategy["style"]))
            if style not in {"strong", "pullback", "sector", "quality", "main_force"}:
                raise ValueError(f"{strategy_id}风格无效")
            checked_overrides[strategy_id] = {
                "style": style,
                "max_positions": _number(candidate.get("max_positions", defaults_for_strategy["max_positions"]), f"{strategy_id}.max_positions", 1, 6, integer=True),
                "max_weight_pct": _number(candidate.get("max_weight_pct", defaults_for_strategy["max_weight_pct"]), f"{strategy_id}.max_weight_pct", 8, 36),
                "max_exposure_pct": _number(candidate.get("max_exposure_pct", defaults_for_strategy["max_exposure_pct"]), f"{strategy_id}.max_exposure_pct", 35, 96),
            }
        checked["strategy_overrides"] = checked_overrides
    for key in ("execution_batch_gate", "execution_verification_gate", "execution_ttl_sweep"):
        if key in updates:
            checked[key] = _boolean(updates[key], key)
    return checked


def _boolean(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "on", "yes", "开启", "开"}:
        return True
    if text in {"false", "0", "off", "no", "关闭", "关"}:
        return False
    raise ValueError(f"{key}必须是布尔值")


def update(conn: sqlite3.Connection, updates: dict[str, Any], actor: str = "human-ui",
           *, risk_evidence: dict[str, Any] | None = None,
           challenger_win: bool = False) -> dict[str, Any]:
    checked = validate(updates, conn=conn)
    current = _flat_read(conn)
    # 非对称风险进化（PR）：strategy_overrides 的风险方向变化必须过闸——
    # 放大需要严格证据 + 持久化观察期 + 单轮幅度上限 + Challenger 胜出；
    # 收紧（安全方向）直接放行。未携带证据上下文的调用（如设置界面）
    # 只能收紧，不能放大。
    if "strategy_overrides" in checked:
        import asymmetric_risk as AR

        evidence = risk_evidence or {}
        evidence_count = evidence.get("evidence_count")
        gate = AR.validate_risk_updates(
            current["strategy_overrides"], checked["strategy_overrides"],
            evidence_count=evidence_count, conn=conn, challenger_win=challenger_win,
        )
        if not gate["allowed"]:
            # 证据达标但缺提案的放大意图：登记提案、启动观察时钟后仍拒绝本次。
            registered = AR.ensure_proposals_registered(
                conn, current["strategy_overrides"], checked["strategy_overrides"],
                evidence_count=evidence_count,
            )
            if registered:
                gate["violations"] = [
                    item + "（已登记提案，观察期重新起算）" if "尚未登记" in item else item
                    for item in gate["violations"]
                ]
            raise ValueError("；".join(gate["violations"]))
        for expansion in gate["expansions"]:
            AR.promote_proposal(conn, expansion["strategy_id"], expansion["key"],
                                expansion["new"], actor=actor)
    now = dt.datetime.now().isoformat(timespec="seconds")
    for key, value in checked.items():
        old = current.get(key)
        if old == value:
            continue
        conn.execute(
            "INSERT INTO paper_runtime_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (key, _json(value), now, actor),
        )
        conn.execute(
            "INSERT INTO paper_runtime_settings_audit(key,old_value,new_value,updated_by,created_at) VALUES(?,?,?,?,?)",
            (key, _json(old), _json(value), actor, now),
        )
    return read(conn)


def apply_evolution_risk_update(conn: sqlite3.Connection, updates: dict[str, Any],
                                evidence_count: int, *,
                                challenger_win: bool = False) -> dict[str, Any]:
    """进化流程的风险更新入口（生产路径）。

    观察期从 risk_expansion_proposals 的持久化登记时间推算——进化流程先
    调用一次（登记提案、启动观察时钟），观察期满后携带达标证据重试即可
    真正生效。

    PR-33：放大最终还需要 Challenger 胜出（``challenger_win=True``），
    只有 Champion/Challenger 晋升路径可以申报；AI/进化自动路径默认 False，
    因此只能收紧。
    """
    return update(conn, updates, actor="evolution",
                  risk_evidence={"evidence_count": evidence_count},
                  challenger_win=challenger_win)


def audit(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT id,key,old_value,new_value,updated_by,created_at FROM paper_runtime_settings_audit ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["old_value"] = _decode(item["old_value"], None)
        item["new_value"] = _decode(item["new_value"], None)
        result.append(item)
    return result


def enabled_strategies(conn: sqlite3.Connection) -> list[str]:
    """Return the configured next-cycle strategy ids (PR-47).

    显式空配置（存了 ``[]``）= 零策略 idle 周期：返回空列表，**不再**回落到
    全部 eligible 策略。缺失/旧库未配置时保持旧行为（回落 eligible 全集）。
    """
    eligible = eligible_strategy_ids(conn)
    try:
        row = conn.execute(
            "SELECT value FROM paper_runtime_settings WHERE key='enabled_strategies'"
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and _decode(row[0], None) == []:
        return []
    value = get(conn, "enabled_strategies", eligible)
    return [item for item in value if item in eligible] or eligible


def cycle_duration_label(days: int | None) -> str:
    return "长期" if not days else f"{int(days)} 个交易日"
