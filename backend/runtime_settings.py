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


STRATEGIES = (
    "tq_breakout",
    "trend_pullback",
    "sector_rotation",
    "reported_profit_breakout",
    "main_force_top10",
)

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
    "enabled_strategies": list(STRATEGIES),
    "shared_pool_position_limit": 15,
    "shared_pool_exposure_cap": 0.82,
    "single_position_max_amount": 0.0,
    "minimum_entry_slot_utilization": 0.60,
    "evolution_interval_hours": 24,
    "strategy_overrides": {key: dict(value) for key, value in STRATEGY_DEFAULTS.items()},
}

SETTING_GROUPS = {
    "simulation": ("default_starting_capital", "cycle_duration_days", "enabled_strategies"),
    "risk": (
        "shared_pool_position_limit", "shared_pool_exposure_cap",
        "single_position_max_amount", "minimum_entry_slot_utilization",
    ),
    "strategy": ("strategy_overrides",),
    "evolution": ("evolution_interval_hours",),
}

METADATA = {
    "default_starting_capital": {"label": "默认启动金额", "unit": "元", "apply_mode": "next_cycle", "recommended": 300000, "description": "创建新模拟周期时预填的共享资金池金额。"},
    "cycle_duration_days": {"label": "模拟周期", "unit": "交易日", "apply_mode": "next_cycle", "recommended": 0, "description": "新周期的计划观察时长；长期表示不设自动到期。"},
    "enabled_strategies": {"label": "启用策略", "apply_mode": "next_cycle", "recommended": list(STRATEGIES), "description": "下一周期参与分配、扫描和风控的策略集合；历史周期不重写。"},
    "shared_pool_position_limit": {"label": "共享池持仓上限", "unit": "席", "apply_mode": "immediate", "recommended": 15, "description": "共享资金池的有效持仓席位硬上限。"},
    "shared_pool_exposure_cap": {"label": "共享池敞口上限", "unit": "%", "apply_mode": "immediate", "recommended": 82, "description": "所有策略合计持仓与待成交金额的硬敞口。"},
    "single_position_max_amount": {"label": "单票最大金额", "unit": "元", "apply_mode": "immediate", "recommended": 0, "description": "0 表示按策略权重自动计算；大于 0 时作为额外绝对上限。"},
    "minimum_entry_slot_utilization": {"label": "最小建仓席位利用率", "unit": "%", "apply_mode": "immediate", "recommended": 60, "description": "动态最小建仓金额使用的席位金额比例，剩余空间留给风控加仓。"},
    "evolution_interval_hours": {"label": "自进化周期", "unit": "小时", "apply_mode": "next_run", "recommended": 24, "description": "后台收盘学习任务之间的最短间隔。"},
    "strategy_overrides": {"label": "策略参数", "apply_mode": "next_cycle", "recommended": STRATEGY_DEFAULTS, "description": "每套策略的风格、席位数和风险权重；仅允许在白名单范围内调整。"},
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str, fallback: Any = None) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def ensure_schema(conn: sqlite3.Connection) -> None:
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
            value = DEFAULTS[key]
            conn.execute(
                "INSERT INTO paper_runtime_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?)",
                (key, _json(value), dt.datetime.now().isoformat(timespec="seconds"), "system-default"),
            )


def defaults() -> dict[str, Any]:
    return json.loads(_json(DEFAULTS))


def _flat_read(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(conn)
    result = defaults()
    for row in conn.execute("SELECT key,value FROM paper_runtime_settings"):
        if row[0] in result:
            value = _decode(row[1], result[row[0]])
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


def metadata() -> dict[str, Any]:
    return json.loads(_json(METADATA))


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


def validate(updates: dict[str, Any]) -> dict[str, Any]:
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
        if not isinstance(value, list) or not value:
            raise ValueError("至少启用一套策略")
        normalized = []
        for item in value:
            item = str(item)
            if item not in STRATEGIES:
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
    if "evolution_interval_hours" in updates:
        checked["evolution_interval_hours"] = _number(updates["evolution_interval_hours"], "自进化周期", 1, 168, integer=True)
    if "strategy_overrides" in updates:
        raw = updates["strategy_overrides"]
        if not isinstance(raw, dict):
            raise ValueError("策略参数必须是对象")
        checked_overrides = {}
        for strategy_id, defaults_for_strategy in STRATEGY_DEFAULTS.items():
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
    return checked


def update(conn: sqlite3.Connection, updates: dict[str, Any], actor: str = "human-ui") -> dict[str, Any]:
    checked = validate(updates)
    current = _flat_read(conn)
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
    value = get(conn, "enabled_strategies", list(STRATEGIES))
    return [item for item in value if item in STRATEGIES] or list(STRATEGIES)


def cycle_duration_label(days: int | None) -> str:
    return "长期" if not days else f"{int(days)} 个交易日"
