# -*- coding: utf-8 -*-
"""通用 AI 审核编排器：两个完全独立的可配置 AI 槽位（``ai1`` / ``ai2``）。

本模块是 AI 审核链路的**唯一权威**。业务层不再认识任何厂商身份：

- 稳定 id 只有 ``ai1`` / ``ai2``；厂商名字降级为用户可改的 ``display_name``，
  只用于展示与审计文案，**永不参与任何分支判断**。
- 两个槽位的 ``api_key`` / ``base_url`` / ``model`` / ``enabled`` / ``timeout``
  完全独立；任一槽位的写入、禁用或调用失败都不得改写另一个槽位。
- 请求体是**最小公共 OpenAI 兼容负载**（model / messages / response_format /
  max_tokens / stream），不按槽位身份增删字段，也没有 ``if slot == "ai1"`` 式分支。
- ``review_mode`` 只有两种取值：

  ``single``
      只用 ``single_reviewer_slot`` 指定的那一个槽位。缺 Key / 被禁用 / 调用失败
      一律 fail-closed（直接记为未完成），结果 ``mode="single_review"``、
      ``consensus=False``、``status="single_review"``，**永不等于 consensus**，
      也**永不写入**审计表的 ``merged_proposals`` 列。
  ``dual``
      两个槽位都必须"已配置且启用"，各自独立调用，全部成功后过共识门禁。
      **dual 绝不降级成 single**：任一槽位缺失或失败都直接判未达成共识。

安全边界（不可被 review_mode 绕过）
----------------------------------
``evolution_apply.apply_tuner_proposals`` 只接受 ``status == "consensus"`` 的运行。
single 模式的运行状态是 ``single_review``，因此单 AI 结果**在物理上**不可能穿过
人工应用门禁，也不会改写 #141 的因果链（effective apply → account_scope_freeze →
reward_attribution → multi-account_evidence → account_mean_evaluation →
reward_uniqueness → Shanghai_timezone）。

审计契约
--------
每次执行都往 ``dual_ai_tuning_runs``（历史表名，保持向后兼容）写入一行：
现有列继续按原语义填充（``ai1`` → ``mimo_*``、``ai2`` → ``deepseek_*``，
供下游 ``self_evolution`` / ``evolution_apply`` 继续消费），同时新增
``review_mode`` / ``result_mode`` / ``config_snapshot`` / ``reviewers`` 四个通用列。
``config_snapshot`` 只记录执行时刻的 ``display_name`` / ``base_url`` / ``model`` /
``enabled``，**永不记录 API Key**。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from adaptive_common import _json, _loads, _now

# ─── 槽位与审核模式 ───
AI_SLOTS = ("ai1", "ai2")
REVIEW_MODES = ("single", "dual")
DEFAULT_REVIEW_MODE = "dual"
DEFAULT_SINGLE_REVIEWER_SLOT = "ai1"
DEFAULT_DISPLAY_NAMES = {"ai1": "AI 1", "ai2": "AI 2"}

# 结果模式（与外层 review_mode 区分：这是"实际怎么跑的"）
MODE_SINGLE_REVIEW = "single_review"
MODE_DUAL_REVIEW = "dual_review"

AI_REVIEW_VERSION = "ai-review-v1"

# ─── 字段边界 ───
MAX_API_KEY_LENGTH = 200
MAX_BASE_URL_LENGTH = 500
MAX_MODEL_LENGTH = 100
MAX_DISPLAY_NAME_LENGTH = 40
DEFAULT_TIMEOUT_SECONDS = 40
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 300

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

# 旧库迁移：历史上 dual_ai_api_keys 里只有这两个厂商身份。
# 这里是一次性回填映射，**不是**运行期分支；迁移后的 display_name 沿用旧标签，
# 仅为方便操作员认出"哪个槽位原来是哪家"，之后完全可改。
LEGACY_PROVIDER_SLOTS = (("mimo", "ai1"), ("deepseek", "ai2"))
LEGACY_DISPLAY_LABELS = {"ai1": "MiMo", "ai2": "DeepSeek"}

# 旧版 dual_ai_tuner 对每个厂商**自带**默认端点/模型/超时，因此"只设了 API Key"的
# 部署过去也能跑。这些值**只在一次性 bootstrap 迁移**
# （``migrate_legacy_env_providers``）里使用，用来把这类老部署补齐成完整槽位配置
# 并落库；落库之后运行期只读数据库，厂商默认值不会进入通用调用层。
# 取值与基线 ``dual_ai_tuner.MIMO_DEFAULTS`` / ``DEEPSEEK_DEFAULTS`` 逐字一致，
# 以保证升级前后行为不变。
LEGACY_PROVIDER_DEFAULTS = {
    "mimo": {"base_url": "https://api.mimo.ai/v1", "model": "mimo-v1", "timeout_seconds": 40},
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash",
                 "timeout_seconds": 35},
}

# 无行（从未在界面配置过）时的通用环境变量兜底：AI_SLOT_AI1_API_KEY 等。
# 刻意用"槽位"而不是"厂商"命名，避免把厂商身份重新焊回业务层。
SLOT_ENV_PREFIX = "AI_SLOT_"

# ─── 共识阈值（与单AI路径 deepseek_advisor._bounded_tuning_patch 的边界一致）───
CONSENSUS_WEIGHT_DIRECTION_THRESHOLD = 0.005  # 权重方向一致性阈值
CONSENSUS_WEIGHT_MAGNITUDE_RATIO = 0.60       # 幅度比：较小/较大 >= 此值视为一致
CONSENSUS_DELTA_DIRECTION_THRESHOLD = 0.001   # 入场阈值方向一致性
CONSENSUS_CONDITION_MAGNITUDE_RATIO = 0.50    # 条件参数幅度一致性
CONSENSUS_MIN_CONFIDENCE = 70.0
CONSENSUS_MAX_WEIGHT_STEP = 0.03
CONSENSUS_MAX_DELTA_STEP = 0.005

_AUDIT_EXTRA_COLUMNS = {
    "review_mode": "TEXT",
    "result_mode": "TEXT",
    "config_snapshot": "TEXT",
    "reviewers": "TEXT",
}


def _num(value, default=None):
    try:
        v = float(value)
        return v if abs(v) < 1e15 else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 槽位与配置
# ─────────────────────────────────────────────────────────────────────────────

def resolve_slot(value):
    """把任意输入归一成稳定槽位 id；无法识别直接拒绝（fail-closed）。"""
    text = str(value or "").strip().lower()
    if text in AI_SLOTS:
        return text
    for legacy, slot in LEGACY_PROVIDER_SLOTS:
        if text == legacy:
            return slot
    raise ValueError("slot 必须是 ai1 或 ai2")


def normalize_base_url(value):
    """规范化 base_url：去空白、去尾斜杠；若用户直接粘贴完整接口地址也接受。"""
    text = str(value or "").strip().rstrip("/")
    if text.lower().endswith(_CHAT_COMPLETIONS_SUFFIX):
        text = text[: -len(_CHAT_COMPLETIONS_SUFFIX)].rstrip("/")
    return text


def chat_completions_url(base_url):
    """由 base_url 拼出 Chat Completions 地址。``/v1`` 与 ``/v1/`` 等价。"""
    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("base_url_missing")
    return base + _CHAT_COMPLETIONS_SUFFIX


def is_usable_base_url(value):
    """base_url 是否是**真能发请求**的地址：scheme ∈ {http, https} 且 hostname 非空。

    这是 readiness 用的宽松判定（不抛异常），既兜住保存校验，也兜住历史脏数据。
    """
    text = normalize_base_url(value)
    if not text or any(ch.isspace() for ch in text):
        return False
    try:
        parts = urllib.parse.urlsplit(text)
        hostname = parts.hostname
    except ValueError:  # 例如非法 IPv6 字面量
        return False
    return parts.scheme.lower() in ("http", "https") and bool(hostname)


def validate_base_url(value):
    """保存时的严格校验：不合法直接 ``ValueError``，把清晰原因回给调用方。

    刻意**不**依赖浏览器 ``<input type=url>`` —— 接口可能被直接调用，校验必须在
    后端。空字符串表示"未配置 / 清除该字段"，允许通过（由 readiness 拦下）。
    """
    text = normalize_base_url(value)
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        raise ValueError("base_url 不能包含空白字符")
    try:
        parts = urllib.parse.urlsplit(text)
        hostname = parts.hostname
    except ValueError as exc:
        raise ValueError("base_url 无法解析：%s" % exc) from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not hostname:
        raise ValueError("base_url 缺少主机名")
    return text


def _clamp_timeout(value):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, number))


def _env_key(slot, suffix):
    return SLOT_ENV_PREFIX + slot.upper() + "_" + suffix


# 槽位 → 历史厂商环境变量前缀（mimo → MIMO / deepseek → DEEPSEEK）。
# 只用于"数据库里从未配置过该槽位"时的一次性兜底，属于配置来源，不是运行期的
# 厂商身份分支：调用层永远不认识任何厂商。
_LEGACY_ENV_PREFIX = {slot: legacy.upper() for legacy, slot in LEGACY_PROVIDER_SLOTS}


def env_slot_defaults(slot):
    """无数据库行时的通用兜底（槽位命名，与厂商无关）。

    优先读 ``AI_SLOT_AI{1,2}_*``；若这组全为空，再退回**历史厂商环境变量**
    （``MIMO_*`` / ``DEEPSEEK_*``），使"只靠环境变量配置过旧调参器"的部署在升级后
    不会静默失去凭据。这里只读运维自己设置的值，**不内置任何厂商的默认地址/模型**
    ——那正是本 PR 要消灭的厂商耦合，缺失时应由使用方显式填写。

    本函数只在**数据库里没有该槽位的任何行**时被调用（见 ``get_slot_config`` /
    ``update_slot``），因此显式 ``clear_api_key`` 过的槽位不会被环境变量复活。
    """
    slot = resolve_slot(slot)
    api_key = str(os.getenv(_env_key(slot, "API_KEY")) or "").strip()
    base_url = normalize_base_url(os.getenv(_env_key(slot, "BASE_URL")) or "")
    model = str(os.getenv(_env_key(slot, "MODEL")) or "").strip()
    raw_timeout = os.getenv(_env_key(slot, "TIMEOUT_SECONDS"))
    if not (api_key or base_url or model or raw_timeout):
        legacy = _LEGACY_ENV_PREFIX.get(slot)
        if legacy:
            api_key = str(os.getenv(legacy + "_API_KEY") or "").strip()
            base_url = normalize_base_url(os.getenv(legacy + "_BASE_URL") or "")
            model = str(os.getenv(legacy + "_MODEL") or "").strip()
            raw_timeout = os.getenv(legacy + "_TIMEOUT_SECONDS")
    timeout = _clamp_timeout(raw_timeout) if raw_timeout not in (None, "") else DEFAULT_TIMEOUT_SECONDS
    return {"api_key": api_key, "base_url": base_url, "model": model, "timeout_seconds": timeout}


_SLOT_TABLES_DDL = """
        CREATE TABLE IF NOT EXISTS ai_provider_slots(
            slot TEXT PRIMARY KEY CHECK(slot IN ('ai1','ai2')),
            display_name TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL DEFAULT '',
            base_url TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            timeout_seconds INTEGER NOT NULL DEFAULT 40,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_review_settings(
            id INTEGER PRIMARY KEY CHECK(id = 1),
            review_mode TEXT NOT NULL DEFAULT 'dual' CHECK(review_mode IN ('single','dual')),
            single_reviewer_slot TEXT NOT NULL DEFAULT 'ai1'
                CHECK(single_reviewer_slot IN ('ai1','ai2')),
            updated_at TEXT NOT NULL
        );
"""


def _ensure_slot_tables(conn):
    conn.executescript(_SLOT_TABLES_DDL)


def ensure_schema(conn):
    """创建 / 升级 AI 审核相关表。只做加法：不 DROP、不改写既有列。"""
    _ensure_slot_tables(conn)
    conn.executescript(
        """
        -- 审计表沿用历史名 dual_ai_tuning_runs（下游 evolution_apply /
        -- self_evolution 按名字与列名消费），这里继续作为权威定义。
        CREATE TABLE IF NOT EXISTS dual_ai_tuning_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            profile_date TEXT,
            market_regime TEXT,
            mimo_status TEXT,
            mimo_model TEXT,
            mimo_response TEXT,
            mimo_proposals TEXT,
            mimo_latency_ms INTEGER,
            mimo_error TEXT,
            deepseek_status TEXT,
            deepseek_model TEXT,
            deepseek_response TEXT,
            deepseek_proposals TEXT,
            deepseek_latency_ms INTEGER,
            deepseek_error TEXT,
            consensus_result TEXT,
            consensus_reason TEXT,
            merged_proposals TEXT,
            applied_ids TEXT,
            evidence_hash TEXT NOT NULL,
            evidence TEXT NOT NULL,
            total_latency_ms INTEGER,
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            -- 通用槽位审计列（旧库由 _ensure_columns 幂等补齐）
            review_mode TEXT,
            result_mode TEXT,
            config_snapshot TEXT,
            reviewers TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dual_ai_runs_recent
            ON dual_ai_tuning_runs(id DESC);
        """
    )
    _ensure_columns(conn, "dual_ai_tuning_runs", _AUDIT_EXTRA_COLUMNS)
    conn.execute(
        "INSERT OR IGNORE INTO ai_review_settings(id, review_mode, single_reviewer_slot, updated_at) "
        "VALUES(1, ?, ?, ?)",
        (DEFAULT_REVIEW_MODE, DEFAULT_SINGLE_REVIEWER_SLOT, _now()),
    )
    migrate_legacy_providers(conn)
    # 数据库里没有可迁移的历史配置时，再看一次性环境变量 bootstrap：
    # 只配了厂商 API Key 的老部署需要在这里补齐默认端点/模型并落库。
    migrate_legacy_env_providers(conn)


def _table_exists(conn, name):
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _ensure_columns(conn, table, columns):
    """幂等补列：已存在即跳过，适合被旧版本建过的表。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    if not existing:
        return
    for name, decl in columns.items():
        if name in existing:
            continue
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))


def migrate_legacy_providers(conn):
    """把旧 ``dual_ai_api_keys`` 的厂商配置**一次性**回填成槽位配置。

    这是引导式（bootstrap）迁移，不是每次启动都跑的同步：

    - 只要 ``ai_provider_slots`` 里已经有任何一行，就整体跳过 —— 因为空 Key 可能
      是用户显式 ``clear_api_key`` 的结果，任何"按空值补回填"的逻辑都会让"清除"
      变成不可持久、甚至把旧凭据悄悄复活。
    - 旧表本身保留（只读，不 DROP），便于回滚与审计。

    返回 ``{slot: legacy_provider}``（本次真正迁移了哪些），已迁移过则返回 ``{}``。
    """
    if not _table_exists(conn, "dual_ai_api_keys"):
        return {}
    _ensure_slot_tables(conn)
    if conn.execute("SELECT 1 FROM ai_provider_slots LIMIT 1").fetchone() is not None:
        return {}
    try:
        rows = conn.execute(
            "SELECT provider, api_key, base_url, model, enabled FROM dual_ai_api_keys"
        ).fetchall()
    except sqlite3.Error:
        return {}
    legacy = {str(row[0]): row for row in rows}
    migrated = {}
    for legacy_provider, slot in LEGACY_PROVIDER_SLOTS:
        row = legacy.get(legacy_provider)
        if row is None:
            continue
        if not str(row[1] or "").strip():
            continue
        display_name = LEGACY_DISPLAY_LABELS.get(slot, DEFAULT_DISPLAY_NAMES[slot])
        conn.execute(
            "INSERT INTO ai_provider_slots(slot,display_name,api_key,base_url,model,"
            "enabled,timeout_seconds,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slot, display_name, str(row[1]), normalize_base_url(row[2]),
             str(row[3] or "").strip(), int(bool(row[4])), DEFAULT_TIMEOUT_SECONDS, _now()),
        )
        migrated[slot] = legacy_provider
    return migrated


def migrate_legacy_env_providers(conn):
    """把"只配置了厂商 API Key"的老部署一次性补齐成槽位配置并落库。

    旧实现（基线 ``dual_ai_tuner``）对每个厂商自带默认 ``base_url`` / ``model``，
    所以只设了 ``MIMO_API_KEY``（或 ``DEEPSEEK_API_KEY``）的部署过去也能正常审核。
    通用化之后运行期不再内置任何厂商默认值；若不迁移，这类部署升级后会静默变成
    "未就绪"（本 PR 引入的向后兼容回归），因此兼容被**收敛在这一层**：

    - **逐槽位**判定（"该槽位尚无持久化配置"）：仅在 ``ai_provider_slots`` 里
      没有这个槽位的任何一行时执行 —— 绝不覆盖用户配置，也不复活已
      ``clear_api_key`` 的槽位（清除会留下行）；
    - 仅在该槽位**没有**新式 ``AI_SLOT_*_API_KEY`` 时处理，绝不抢在新式配置前面；
    - 只把**缺失**的 base_url / model / 超时用旧厂商默认值补齐，显式设过的沿用。

    落库后运行期一律读数据库，厂商默认值不会进入通用调用层。
    返回 ``{slot: legacy_provider}``（本次真正初始化了哪些）。
    """
    _ensure_slot_tables(conn)
    migrated = {}
    for legacy, slot in LEGACY_PROVIDER_SLOTS:
        if _slot_row(conn, slot) is not None:
            continue  # 该槽位已有持久化配置（含"已清除"）→ 一律不动
        prefix = legacy.upper()
        if str(os.getenv(_env_key(slot, "API_KEY")) or "").strip():
            continue  # 新式环境变量已提供 Key → 交给通用兜底，不落库
        api_key = str(os.getenv(prefix + "_API_KEY") or "").strip()
        if not api_key:
            continue
        defaults = LEGACY_PROVIDER_DEFAULTS.get(legacy, {})
        base_url = (normalize_base_url(os.getenv(prefix + "_BASE_URL") or "")
                    or defaults.get("base_url", ""))
        model = str(os.getenv(prefix + "_MODEL") or "").strip() or defaults.get("model", "")
        raw_timeout = os.getenv(prefix + "_TIMEOUT_SECONDS")
        timeout = (_clamp_timeout(raw_timeout) if raw_timeout not in (None, "")
                   else int(defaults.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)))
        conn.execute(
            "INSERT INTO ai_provider_slots(slot,display_name,api_key,base_url,model,"
            "enabled,timeout_seconds,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slot, LEGACY_DISPLAY_LABELS.get(slot, DEFAULT_DISPLAY_NAMES[slot]), api_key,
             base_url, model, 1, timeout, _now()),
        )
        migrated[slot] = legacy
    return migrated


def _slot_row(conn, slot):
    return conn.execute(
        "SELECT display_name, api_key, base_url, model, enabled, timeout_seconds, updated_at "
        "FROM ai_provider_slots WHERE slot=?",
        (slot,),
    ).fetchone()


def get_slot_config(conn, slot):
    """返回单个槽位的完整调用配置。

    有数据库行 → 数据库就是权威（含空值，否则 ``clear_api_key`` 会被环境变量悄悄
    复活）；没有行 → 使用通用环境变量兜底，再退回空默认值。

    与 ``get_review_settings`` 一致，读函数自身保证 schema 已建，避免在全新库上
    "读槽位"直接 ``no such table`` 崩掉。
    """
    slot = resolve_slot(slot)
    ensure_schema(conn)
    row = _slot_row(conn, slot)
    if row is not None:
        return {
            "slot": slot,
            "display_name": str(row[0] or "") or DEFAULT_DISPLAY_NAMES[slot],
            "api_key": str(row[1] or ""),
            "base_url": normalize_base_url(row[2]),
            "model": str(row[3] or ""),
            "enabled": bool(row[4]),
            "timeout_seconds": _clamp_timeout(row[5]),
            "updated_at": row[6],
            "source": "database",
        }
    env = env_slot_defaults(slot)
    return {
        "slot": slot,
        "display_name": DEFAULT_DISPLAY_NAMES[slot],
        "api_key": env["api_key"],
        "base_url": env["base_url"],
        "model": env["model"],
        "enabled": bool(env["api_key"]),
        "timeout_seconds": env["timeout_seconds"],
        "updated_at": None,
        "source": "environment" if env["api_key"] else "default",
    }


def _key_preview(value):
    text = str(value or "")
    if not text:
        return ""
    if len(text) > 12:
        return text[:8] + "****" + text[-4:]
    return "****"


def slot_public_view(cfg):
    """GET 层视图：**绝不含明文 Key**，只给是否已配置与掩码预览。"""
    api_key = str(cfg.get("api_key") or "")
    return {
        "slot": cfg["slot"],
        "display_name": cfg["display_name"],
        "configured": bool(api_key.strip()),
        "key_preview": _key_preview(api_key),
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "enabled": bool(cfg.get("enabled")),
        "timeout_seconds": cfg.get("timeout_seconds"),
        "updated_at": cfg.get("updated_at"),
        "source": cfg.get("source"),
        # 就绪 = 真正能发出一次请求所需的**全部**字段：Key、启用、合法地址、模型。
        # 地址不只看非空——``abc`` / ``123`` / ``://wrong`` 这类不是可请求的 URL，
        # 历史脏数据也要被这里拦下（保存路径另有 validate_base_url 严格拒绝）。
        "ready": (
            bool(api_key.strip())
            and bool(cfg.get("enabled"))
            and is_usable_base_url(cfg.get("base_url"))
            and bool(str(cfg.get("model") or "").strip())
        ),
    }


def get_slots(conn):
    """两个槽位的公开视图（掩码，可直接回给前端）。"""
    return {slot: slot_public_view(get_slot_config(conn, slot)) for slot in AI_SLOTS}


def _clean_text(value, limit, field):
    if not isinstance(value, str):
        raise ValueError("%s必须是字符串" % field)
    text = value.strip()
    if len(text) > limit:
        raise ValueError("%s长度不能超过%d" % (field, limit))
    return text


def update_slot(conn, slot, api_key=None, base_url=None, model=None, enabled=None,
                display_name=None, timeout_seconds=None, clear_api_key=False):
    """写入单个槽位配置。只动这一个槽位，另一个槽位零影响。

    语义：
    - ``api_key`` 省略 (``None``) 或空字符串 → **保留旧 Key**；
    - ``clear_api_key=True`` → 显式清空（优先级最高，需显式声明）。
    """
    slot = resolve_slot(slot)
    ensure_schema(conn)
    row = _slot_row(conn, slot)
    if row is not None:
        state = {
            "display_name": str(row[0] or ""),
            "api_key": str(row[1] or ""),
            "base_url": normalize_base_url(row[2]),
            "model": str(row[3] or ""),
            "enabled": bool(row[4]),
            "timeout_seconds": _clamp_timeout(row[5]),
        }
    else:
        env = env_slot_defaults(slot)
        state = {
            "display_name": DEFAULT_DISPLAY_NAMES[slot],
            "api_key": env["api_key"],
            "base_url": env["base_url"],
            "model": env["model"],
            "enabled": bool(env["api_key"]),
            "timeout_seconds": env["timeout_seconds"],
        }

    if display_name is not None:
        state["display_name"] = _clean_text(display_name, MAX_DISPLAY_NAME_LENGTH, "display_name")
    if api_key is not None:
        cleaned_key = _clean_text(api_key, MAX_API_KEY_LENGTH, "api_key")
        if cleaned_key:  # 空输入 = 保持旧 Key
            state["api_key"] = cleaned_key
    if base_url is not None:
        # 保存即校验：scheme / hostname 不合法直接拒绝（空串 = 清空该字段）。
        # 不能只靠浏览器 <input type=url>，接口可能被直接调用。
        state["base_url"] = validate_base_url(
            _clean_text(base_url, MAX_BASE_URL_LENGTH, "base_url"))
    if model is not None:
        state["model"] = _clean_text(model, MAX_MODEL_LENGTH, "model")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled必须是布尔值")
        state["enabled"] = enabled
    if timeout_seconds is not None:
        state["timeout_seconds"] = _clamp_timeout(timeout_seconds)
    if clear_api_key:
        state["api_key"] = ""

    now = _now()
    conn.execute(
        "INSERT INTO ai_provider_slots(slot,display_name,api_key,base_url,model,enabled,"
        "timeout_seconds,updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slot) DO UPDATE SET display_name=excluded.display_name, "
        "api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model, "
        "enabled=excluded.enabled, timeout_seconds=excluded.timeout_seconds, "
        "updated_at=excluded.updated_at",
        (slot, state["display_name"], state["api_key"], state["base_url"], state["model"],
         int(state["enabled"]), state["timeout_seconds"], now),
    )
    return get_slots(conn)


# ─────────────────────────────────────────────────────────────────────────────
# 审核模式设置
# ─────────────────────────────────────────────────────────────────────────────

def get_review_settings(conn):
    ensure_schema(conn)
    row = conn.execute(
        "SELECT review_mode, single_reviewer_slot, updated_at FROM ai_review_settings WHERE id=1"
    ).fetchone()
    if row is None:
        return {
            "review_mode": DEFAULT_REVIEW_MODE,
            "single_reviewer_slot": DEFAULT_SINGLE_REVIEWER_SLOT,
            "updated_at": None,
        }
    mode = str(row[0] or DEFAULT_REVIEW_MODE)
    slot = str(row[1] or DEFAULT_SINGLE_REVIEWER_SLOT)
    return {
        "review_mode": mode if mode in REVIEW_MODES else DEFAULT_REVIEW_MODE,
        "single_reviewer_slot": slot if slot in AI_SLOTS else DEFAULT_SINGLE_REVIEWER_SLOT,
        "updated_at": row[2],
    }


def update_review_settings(conn, review_mode=None, single_reviewer_slot=None):
    """更新全局审核模式。任何非法取值直接拒绝，不做静默回落。"""
    if review_mode is None and single_reviewer_slot is None:
        raise ValueError("至少需要提供 review_mode 或 single_reviewer_slot")
    ensure_schema(conn)
    current = get_review_settings(conn)
    if review_mode is not None:
        mode = str(review_mode).strip().lower()
        if mode not in REVIEW_MODES:
            raise ValueError("review_mode 只能是 single 或 dual")
        current["review_mode"] = mode
    if single_reviewer_slot is not None:
        current["single_reviewer_slot"] = resolve_slot(single_reviewer_slot)
    conn.execute(
        "UPDATE ai_review_settings SET review_mode=?, single_reviewer_slot=?, updated_at=? WHERE id=1",
        (current["review_mode"], current["single_reviewer_slot"], _now()),
    )
    return get_review_settings(conn)


def review_settings_view(conn):
    settings = get_review_settings(conn)
    slots = get_slots(conn)
    active = settings["single_reviewer_slot"]
    return {
        "version": AI_REVIEW_VERSION,
        "review_mode": settings["review_mode"],
        "single_reviewer_slot": active,
        "updated_at": settings["updated_at"],
        "slots": slots,
        "slot_order": list(AI_SLOTS),
        # 单/双就绪直接取自槽位视图的 ready（含 base_url + model 校验），
        # 保证调度预检与页面展示的口径完全一致。
        "single_ready": bool(slots[active]["ready"]),
        "dual_ready": all(slots[s]["ready"] for s in AI_SLOTS),
        "review_modes": [
            {"value": "single", "label": "单AI审阅", "description": "只用选定的一个槽位；结果仅供参考，不构成共识。"},
            {"value": "dual", "label": "双AI共识", "description": "两个槽位独立分析，方向一致且幅度接近才合并。"},
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 通用调用层（无厂商身份分支）
# ─────────────────────────────────────────────────────────────────────────────

def build_request_body(slot_config, system_prompt, user_prompt, max_tokens=1800):
    """构造最小公共 OpenAI 兼容请求体。

    刻意**只**包含所有兼容端点都认识的字段；没有任何按槽位身份增删字段的逻辑
    （历史上 DeepSeek 会被额外塞 ``thinking``，那正是本 PR 要消灭的厂商耦合）。
    """
    model = str(slot_config.get("model") or "").strip()
    if not model:
        raise RuntimeError("%s_model_missing" % slot_config.get("slot"))
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max(400, min(int(max_tokens), 4000)),
        "stream": False,
    }


def _call_slot(slot_config, system_prompt, user_prompt, max_tokens=1800):
    """调用单个槽位，返回 (parsed_json, input_tokens, output_tokens, latency_ms)。"""
    api_key = str(slot_config.get("api_key") or "")
    if not api_key.strip():
        raise RuntimeError("%s_api_key_missing" % slot_config.get("slot"))
    url = chat_completions_url(slot_config.get("base_url"))
    body = build_request_body(slot_config, system_prompt, user_prompt, max_tokens=max_tokens)
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=float(slot_config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latency_ms = round((time.monotonic() - started) * 1000)
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage") or {}
    parsed = json.loads(content)
    return parsed, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), latency_ms


def slot_config_snapshot(cfg):
    """审计用配置快照 —— 只有地址与模型，**没有 API Key**。"""
    return {
        "display_name": cfg.get("display_name") or "",
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "enabled": bool(cfg.get("enabled")),
        "timeout_seconds": cfg.get("timeout_seconds"),
    }


def test_slot(conn, slot):
    """对一个槽位做一次真实连通性探测。返回结构**绝不含 API Key**。

    语义（"连接正常"必须等于"这个槽位真的能被系统用起来"）：

        ``ok`` = 网络成功 AND HTTP 成功 AND JSON 可解析 AND 响应满足最低协议结构

    最低协议结构 = 助手消息内容解析后是一个 **JSON 对象**（审核管线本身只接受
    对象），非对象一律 ``ok=false`` + ``error="invalid_response_schema"``，
    绝不让协议不合法的响应显示成"连接成功"。

    只做一次极小的 Chat Completions 往返，用于页面上的"测试连接"按钮；
    CI 不会调用它（会触发真实付费请求），所以测试用例只覆盖拒绝路径。
    """
    slot = resolve_slot(slot)
    cfg = get_slot_config(conn, slot)
    result = {
        "slot": slot,
        "display_name": cfg["display_name"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "ok": False,
        "latency_ms": None,
        "error": None,
    }
    if not str(cfg["api_key"]).strip():
        result["error"] = "api_key_missing"
        return result
    if not cfg["enabled"]:
        result["error"] = "slot_disabled"
        return result
    if not is_usable_base_url(cfg["base_url"]):
        result["error"] = "invalid_base_url"
        return result
    if not str(cfg.get("model") or "").strip():
        result["error"] = "model_missing"
        return result
    try:
        parsed, _in_tokens, _out_tokens, latency = _call_slot(
            cfg, "你是连通性探针。只输出严格JSON。", '只输出 {"ok": true}', max_tokens=64)
    except Exception as exc:  # noqa: BLE001 - 探测结果必须结构化返回
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        return result
    result["latency_ms"] = latency
    result["response_ok"] = isinstance(parsed, dict)
    if not result["response_ok"]:
        result["error"] = "invalid_response_schema"
        return result
    result["ok"] = True
    return result


def _build_tuning_system_prompt():
    """构建调参系统提示词。"""
    return (
        "你是A股模拟盘的受约束调参器，不是交易员。只输出严格JSON。\n"
        "你不能下单、不能修改公共选股、不能新增未知因子、不能修改风控上限。\n"
        "只有在证据充分且置信度>=70时提出很小的模拟盘内部补丁；证据不足就hold。\n"
        "weights必须是该账户已有因子并且总和约等于1，conditions只能使用已有条件名。\n"
        "本次是盘中小步调参，单个权重最多移动3个百分点，入场阈值最多移动0.005，\n"
        "不得切换条件enabled。不要把行情推测写成事实。"
    )


def _build_tuning_user_prompt(evidence, accounts, mode):
    """构建调参用户提示词。"""
    example = {
        "decision": "propose|hold",
        "confidence": 0,
        "market_regime": "momentum|rotation|risk_off|high_volatility|balanced|unclassified",
        "summary": "中文说明",
        "proposals": [{
            "account_id": "tq_breakout|trend_pullback|sector_rotation",
            "reason": "只说明证据和预期改善",
            "weights": {"仅使用当前账户已有因子": 0.25},
            "entry_score_delta": 0.0,
            "conditions": {"仅使用已有条件名": 0.0},
        }],
    }
    return (
        "调参模式=" + str(mode) + "\n格式示例=" + json.dumps(example, ensure_ascii=False) +
        "\n证据=" + json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) +
        "\n账户状态=" + json.dumps(accounts, ensure_ascii=False, separators=(",", ":"))
    )


# ─────────────────────────────────────────────────────────────────────────────
# 共识门禁（槽位无关）
# ─────────────────────────────────────────────────────────────────────────────

def _check_consensus(proposals_by_slot, accounts_map, evolution=None,
                     evolution_by_account=None, labels=None):
    """检查两个槽位的提案是否达成共识。

    共识条件（与历史实现逐条等价，只把 MiMo/DeepSeek 换成槽位 + 展示名）：
    1. 两个槽位的 ``decision`` 都是 ``propose``；
    2. 同一 ``account_id`` 的权重调整方向一致；
    3. 调整幅度比 >= ``weight_magnitude_ratio``；
    4. 入场阈值调整方向一致；
    5. 双方置信度均 >= 70。

    返回 ``(consensus: bool, reason: str, merged: list)``。
    """
    evolution = evolution or {}
    labels = labels or {}
    left_label = str(labels.get("ai1") or DEFAULT_DISPLAY_NAMES["ai1"])
    right_label = str(labels.get("ai2") or DEFAULT_DISPLAY_NAMES["ai2"])
    left_proposals = list(proposals_by_slot.get("ai1") or [])
    right_proposals = list(proposals_by_slot.get("ai2") or [])

    max_proposals = max(1, int(_num(evolution.get("max_proposals_per_run"), 3)))
    if not left_proposals or not right_proposals:
        return False, "至少一个AI未提出有效提案", []

    left_map = {}
    for p in left_proposals:
        aid = str(p.get("account_id", ""))
        if aid:
            left_map[aid] = p
    right_map = {}
    for p in right_proposals:
        aid = str(p.get("account_id", ""))
        if aid:
            right_map[aid] = p

    common_accounts = set(left_map.keys()) & set(right_map.keys())
    if not common_accounts:
        return False, ("两个AI针对不同账户提出提案（%s:%s, %s:%s）" % (
            left_label, list(left_map.keys()), right_label, list(right_map.keys()))), []

    merged = []
    disagreements = []

    for account_id in sorted(common_accounts):
        lp = left_map[account_id]
        rp = right_map[account_id]
        base = accounts_map.get(account_id, {})
        account_evolution = dict(evolution)
        account_evolution.update((evolution_by_account or {}).get(account_id) or {})
        weight_magnitude_ratio = max(0.1, min(1.0, _num(
            account_evolution.get("consensus_weight_ratio"), CONSENSUS_WEIGHT_MAGNITUDE_RATIO)))
        weight_step = max(0.005, min(0.05, _num(
            account_evolution.get("max_weight_delta"), CONSENSUS_MAX_WEIGHT_STEP)))
        entry_step = max(0.001, min(0.01, _num(
            account_evolution.get("max_delta_threshold"), CONSENSUS_MAX_DELTA_STEP)))

        def _norm_confidence(value):
            v = _num(value, 0.0)
            if 0 < v <= 1:
                v *= 100
            return v

        l_conf = _norm_confidence(lp.get("confidence"))
        r_conf = _norm_confidence(rp.get("confidence"))
        if l_conf < CONSENSUS_MIN_CONFIDENCE or r_conf < CONSENSUS_MIN_CONFIDENCE:
            disagreements.append(
                "[%s] 置信度不足：%s=%.0f，%s=%.0f（要求均≥%.0f）" % (
                    account_id, left_label, l_conf, right_label, r_conf, CONSENSUS_MIN_CONFIDENCE))
            continue

        lw = lp.get("weights") or {}
        rw = rp.get("weights") or {}
        base_weights = base.get("weights") or {}
        if (not isinstance(lw, dict) or not isinstance(rw, dict)
                or not isinstance(base_weights, dict) or not base_weights):
            disagreements.append("[%s] 权重格式无效" % account_id)
            continue
        unknown_left = sorted(set(lw) - set(base_weights))
        unknown_right = sorted(set(rw) - set(base_weights))
        if unknown_left or unknown_right:
            # 未知因子绝不能被平均进候选：零值默认会把拼写错误伪装成"新因子"
            # 并泄漏进影子账本。
            disagreements.append("[%s] 未知因子拒绝：%s=%s, %s=%s" % (
                account_id, left_label, unknown_left, right_label, unknown_right))
            continue
        all_factors = set(list(base_weights.keys()) + list(lw.keys()) + list(rw.keys()))

        weight_consensus = True
        weight_details = {}
        for factor in all_factors:
            base_val = _num(base_weights.get(factor), 0.0)
            l_val = _num(lw.get(factor), base_val)
            r_val = _num(rw.get(factor), base_val)
            l_delta = l_val - base_val
            r_delta = r_val - base_val

            if l_delta * r_delta < -CONSENSUS_WEIGHT_DIRECTION_THRESHOLD ** 2:
                weight_consensus = False
                disagreements.append("[%s] %s: %s=%+.4f vs %s=%+.4f 方向相反" % (
                    account_id, factor, left_label, l_delta, right_label, r_delta))
                continue

            if abs(l_delta) > 0.005 and abs(r_delta) > 0.005:
                ratio = min(abs(l_delta), abs(r_delta)) / max(abs(l_delta), abs(r_delta))
                if ratio < weight_magnitude_ratio:
                    weight_consensus = False
                    disagreements.append("[%s] %s: 幅度比=%.2f < %s" % (
                        account_id, factor, ratio, weight_magnitude_ratio))
                    continue

            weight_details[factor] = round((l_val + r_val) / 2, 6)

        l_delta = _num(lp.get("entry_score_delta"), 0.0)
        r_delta = _num(rp.get("entry_score_delta"), 0.0)
        delta_consensus = True
        if (abs(l_delta) > CONSENSUS_DELTA_DIRECTION_THRESHOLD
                and abs(r_delta) > CONSENSUS_DELTA_DIRECTION_THRESHOLD):
            if l_delta * r_delta < 0:
                delta_consensus = False
                disagreements.append("[%s] 入场阈值: %s=%+.4f vs %s=%+.4f 方向相反" % (
                    account_id, left_label, l_delta, right_label, r_delta))
        merged_delta = round((l_delta + r_delta) / 2, 6)

        lc = lp.get("conditions") or {}
        rc = rp.get("conditions") or {}
        base_conditions = base.get("conditions") or {}
        condition_consensus = True
        merged_conditions = {}
        for key in set(list(base_conditions.keys()) + list(lc.keys()) + list(rc.keys())):
            if key == "enabled":
                merged_conditions["enabled"] = base_conditions.get("enabled", {})
                continue
            base_val = _num(base_conditions.get(key), 0.0)
            l_val = _num(lc.get(key), base_val)
            r_val = _num(rc.get(key), base_val)
            l_delta_c = l_val - base_val
            r_delta_c = r_val - base_val
            if abs(l_delta_c) > 0.01 and abs(r_delta_c) > 0.01:
                if l_delta_c * r_delta_c < 0:
                    condition_consensus = False
                    disagreements.append("[%s] 条件 %s: 方向相反" % (account_id, key))
                    continue
                ratio = min(abs(l_delta_c), abs(r_delta_c)) / max(abs(l_delta_c), abs(r_delta_c))
                if ratio < CONSENSUS_CONDITION_MAGNITUDE_RATIO:
                    condition_consensus = False
                    disagreements.append("[%s] 条件 %s: 幅度比=%.2f" % (account_id, key, ratio))
                    continue
            merged_conditions[key] = round((l_val + r_val) / 2, 6)

        if weight_consensus and delta_consensus and condition_consensus:
            bounded_weights = {}
            for factor, value in weight_details.items():
                base_val = _num(base_weights.get(factor), value)
                bounded = min(max(value, base_val - weight_step), base_val + weight_step)
                bounded_weights[factor] = min(max(bounded, 0.0), 1.0)
            try:
                import adaptive_selection as _selection
                if bounded_weights:
                    bounded_weights = dict(_selection._normalize(bounded_weights))
            except Exception:
                pass
            if set(bounded_weights) != set(base_weights) or any(
                    abs(_num(bounded_weights.get(key), 0.0) - _num(base_weights.get(key), 0.0))
                    > weight_step + 1e-6
                    for key in base_weights):
                disagreements.append(
                    "[%s] 归一化后单因子权重变化超过±%.3f，拒绝共识" % (account_id, weight_step))
                continue
            current_entry = _num(base.get("entry_score_delta"), merged_delta)
            merged_delta = round(
                current_entry + max(-entry_step, min(entry_step, merged_delta - current_entry)), 6)
            for key, value in list(merged_conditions.items()):
                current = _num(base_conditions.get(key), value)
                step = max(abs(_num(current, 0.0)) * 0.20, 0.05)
                merged_conditions[key] = round(current + max(-step, min(step, value - current)), 6)
            merged.append({
                "account_id": account_id,
                "reason": "双AI共识：%s→%s | %s→%s" % (
                    left_label, str(lp.get("reason", ""))[:60],
                    right_label, str(rp.get("reason", ""))[:60]),
                "weights": bounded_weights,
                "entry_score_delta": merged_delta,
                "conditions": merged_conditions,
                "consensus_source": "dual_ai_agreement",
                "ai1_confidence": l_conf,
                "ai2_confidence": r_conf,
            })

    if disagreements:
        return False, "分歧：" + "; ".join(disagreements[:5]), []

    if not merged:
        return False, "无有效共识提案", []

    merged = merged[:max_proposals]
    return True, "双AI对 %d 个账户达成共识" % len(merged), merged


# ─────────────────────────────────────────────────────────────────────────────
# 编排器
# ─────────────────────────────────────────────────────────────────────────────

def _empty_reviewer(slot, cfg, status, error=None):
    return {
        "slot": slot,
        "display_name": cfg.get("display_name") or DEFAULT_DISPLAY_NAMES[slot],
        "model": cfg.get("model") or "",
        "status": status,
        "decision": None,
        "confidence": None,
        "market_regime": None,
        "summary": None,
        "proposals": [],
        "proposals_count": 0,
        "latency_ms": None,
        "error": error,
    }


def _call_reviewer(slot, cfg, system_prompt, user_prompt):
    """调用一个槽位并把结果整理成统一的 reviewer 结构；异常一律转成 failed。

    本函数**必须永不抛出**：单AI模式是裸调用（不像双AI那样在 future 里再包一层
    try），任何逃逸的异常都会变成服务端 500 且**不写审计行**。因此"合法 JSON 但
    不是对象"（例如 ``[]``）也在函数内归一为结构化失败，而不是让 ``.get`` 抛
    AttributeError 逃出去。
    """
    try:
        parsed, in_tok, out_tok, latency = _call_slot(cfg, system_prompt, user_prompt)
        if not isinstance(parsed, dict):
            raise RuntimeError("%s_response_not_object" % slot)
        proposals = parsed.get("proposals") or []
        if not isinstance(proposals, list):
            proposals = []
        return {
            "slot": slot,
            "display_name": cfg.get("display_name") or DEFAULT_DISPLAY_NAMES[slot],
            "model": cfg.get("model") or "",
            "status": "completed",
            "decision": parsed.get("decision", "hold"),
            "confidence": parsed.get("confidence", 0),
            "market_regime": parsed.get("market_regime", "unclassified"),
            "summary": parsed.get("summary", ""),
            "proposals": proposals,
            "proposals_count": len(proposals),
            "latency_ms": latency,
            "error": None,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        }
    except Exception as exc:  # noqa: BLE001 - 上游必须拿到结构化失败而不是异常
        return _empty_reviewer(slot, cfg, "failed", error="%s: %s" % (type(exc).__name__, str(exc)[:200]))


def _evolution_bounds(connect_factory, accounts_map):
    """读取当前进化参数作为调参边界；读失败回落默认值，绝不阻塞主流程。"""
    try:
        import self_evolution as _SE
        with connect_factory() as conn:
            _SE.ensure_schema(conn)
            params = _SE.get_current_params(conn).get("params") or {}
            by_account = {}
            for account_id in (accounts_map or {}):
                try:
                    by_account[account_id] = _SE.get_strategy_params(
                        conn, account_id).get("params") or {}
                except Exception:
                    continue
        return params, by_account
    except Exception:
        return {}, {}


def _base_outcome(result_mode, review_mode, status, reviewers, consensus, reason,
                  proposals, run_id, total_latency, evidence_hash, config_snapshot,
                  single_reviewer_slot=None):
    return {
        "id": run_id,
        "version": AI_REVIEW_VERSION,
        "mode": result_mode,
        "review_mode": review_mode,
        "status": status,
        "consensus": bool(consensus),
        "reason": reason,
        "proposals": proposals,
        "reviewers": reviewers,
        "config_snapshot": config_snapshot,
        "single_reviewer_slot": single_reviewer_slot,
        "total_latency_ms": total_latency,
        "evidence_hash": evidence_hash,
    }


def _write_audit(connect_factory, trigger, mode, status, review_mode, result_mode,
                 profile, evidence, evidence_hash, config_snapshot, reviewers,
                 consensus, reason, audit_merged, started_at, started,
                 total_latency_ms=None, track_evolution=False):
    """写入审计行（沿用历史表与列语义），必要时接线进化追踪。"""
    finished_at = _now()
    total_latency = (total_latency_ms if total_latency_ms is not None
                     else round((time.monotonic() - started) * 1000))
    left = reviewers.get("ai1") or {}
    right = reviewers.get("ai2") or {}
    with connect_factory() as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            """INSERT INTO dual_ai_tuning_runs(
                trigger, mode, status, profile_date, market_regime,
                mimo_status, mimo_model, mimo_response, mimo_proposals, mimo_latency_ms, mimo_error,
                deepseek_status, deepseek_model, deepseek_response, deepseek_proposals, deepseek_latency_ms, deepseek_error,
                consensus_result, consensus_reason, merged_proposals, applied_ids,
                evidence_hash, evidence, total_latency_ms, created_at, finished_at,
                review_mode, result_mode, config_snapshot, reviewers
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(trigger)[:80], mode, status,
                profile.get("profile_date"), profile.get("regime"),
                left.get("status"), (config_snapshot.get("ai1") or {}).get("model"),
                _json(left.get("proposals")) if left.get("status") == "completed" else None,
                _json(left.get("proposals")) if left.get("proposals") else None,
                left.get("latency_ms"), left.get("error"),
                right.get("status"), (config_snapshot.get("ai2") or {}).get("model"),
                _json(right.get("proposals")) if right.get("status") == "completed" else None,
                _json(right.get("proposals")) if right.get("proposals") else None,
                right.get("latency_ms"), right.get("error"),
                "consensus" if consensus else ("single_review" if result_mode == MODE_SINGLE_REVIEW else "no_consensus"),
                str(reason or "")[:500],
                _json(audit_merged) if audit_merged else None,
                None,  # applied_ids：只有真正 apply 之后由 evolution_apply 回填
                evidence_hash,
                _json(evidence),
                total_latency, started_at, finished_at,
                review_mode, result_mode,
                _json(config_snapshot), _json(reviewers),
            ),
        )
        run_id = cursor.lastrowid

        if track_evolution:
            try:
                import self_evolution as _SE
                _SE.ensure_schema(conn)
                _SE.init_params(conn)  # 幂等：仅首次写入默认参数版本
                _SE.track_run(
                    conn, run_id,
                    trigger=str(trigger)[:80], mode=mode, status=status,
                    market_regime=profile.get("regime"),
                    # applied_count 只在真正 apply 之后由 evolution_apply 回填：
                    # 把"有提案"写成"已落地"正是 reward 归因要拒绝的伪证据。
                    applied=False, applied_count=0,
                    mimo_latency_ms=left.get("latency_ms"),
                    deepseek_latency_ms=right.get("latency_ms"),
                    total_latency_ms=total_latency,
                    mimo_confidence=left.get("confidence"),
                    deepseek_confidence=right.get("confidence"),
                )
            except Exception:
                pass  # 追踪失败绝不阻塞调参主流程
    return run_id, total_latency


def run_ai_review(connect_factory, paper_db_path, snapshot_paths, evidence_collector,
                  tuning_accounts_fn, config=None, profile=None, trigger="scheduled",
                  mode="intraday", review_mode=None, single_reviewer_slot=None):
    """执行一次 AI 审核，返回统一结果模型。

    统一结果字段：``mode`` / ``review_mode`` / ``status`` / ``consensus`` /
    ``reason`` / ``proposals`` / ``reviewers`` / ``config_snapshot``。
    """
    profile = profile or {}
    started = time.monotonic()
    started_at = _now()
    mode = str(mode or "intraday")[:30]

    with connect_factory() as conn:
        ensure_schema(conn)
        stored = get_review_settings(conn)
        slot_configs = {slot: get_slot_config(conn, slot) for slot in AI_SLOTS}
        evidence, evidence_hash = evidence_collector(conn, paper_db_path, snapshot_paths)
        evidence["market_profile"] = {
            "profile_date": profile.get("profile_date"),
            "regime": profile.get("regime"),
            "quality": profile.get("quality"),
            "valid_rows": profile.get("valid_rows"),
            "source_at": profile.get("source_at"),
        }
        accounts = tuning_accounts_fn(paper_db_path)
        accounts_map = {str(a.get("account_id", "")): a for a in accounts}

    active_mode = str(review_mode if review_mode is not None else stored["review_mode"]).strip().lower()
    if active_mode not in REVIEW_MODES:
        active_mode = DEFAULT_REVIEW_MODE
    if single_reviewer_slot is not None:
        active_single_slot = resolve_slot(single_reviewer_slot)
    else:
        active_single_slot = stored["single_reviewer_slot"]

    config_snapshot = {slot: slot_config_snapshot(slot_configs[slot]) for slot in AI_SLOTS}
    labels = {slot: slot_configs[slot]["display_name"] for slot in AI_SLOTS}

    context = {
        "connect_factory": connect_factory,
        "evidence": evidence,
        "evidence_hash": evidence_hash,
        "accounts": accounts,
        "accounts_map": accounts_map,
        "slot_configs": slot_configs,
        "config_snapshot": config_snapshot,
        "labels": labels,
        "profile": profile,
        "trigger": trigger,
        "mode": mode,
        "review_mode": active_mode,
        "started": started,
        "started_at": started_at,
    }
    if active_mode == "single":
        return _run_single_review(context, active_single_slot)
    return _run_dual_review(context)


def _context_audit(context, result_mode, status, reviewers, consensus, reason,
                   audit_merged, proposals, track_evolution):
    run_id, total_latency = _write_audit(
        context["connect_factory"], context["trigger"], context["mode"], status,
        context["review_mode"], result_mode, context["profile"], context["evidence"],
        context["evidence_hash"], context["config_snapshot"], reviewers, consensus,
        reason, audit_merged, context["started_at"], context["started"],
        track_evolution=track_evolution,
    )
    return _base_outcome(
        result_mode, context["review_mode"], status, reviewers, consensus, reason,
        proposals, run_id, total_latency, context["evidence_hash"], context["config_snapshot"],
    )


def _run_single_review(context, slot):
    """单AI审阅：只用选定槽位，fail-closed，结果永不叫 consensus。"""
    slot_configs = context["slot_configs"]
    labels = context["labels"]
    reviewers = {s: _empty_reviewer(s, slot_configs[s], "not_selected") for s in AI_SLOTS}
    cfg = slot_configs[slot]

    if not str(cfg["api_key"]).strip():
        reviewers[slot] = _empty_reviewer(slot, cfg, "not_configured")
        return _context_audit(
            context, MODE_SINGLE_REVIEW, "single_slot_not_configured", reviewers, False,
            "%s 未配置 API Key，单AI审阅按 fail-closed 终止" % labels[slot],
            None, [], track_evolution=False,
        )
    if not cfg["enabled"]:
        reviewers[slot] = _empty_reviewer(slot, cfg, "disabled")
        return _context_audit(
            context, MODE_SINGLE_REVIEW, "single_slot_disabled", reviewers, False,
            "%s 已禁用，单AI审阅按 fail-closed 终止" % labels[slot],
            None, [], track_evolution=False,
        )

    system_prompt = _build_tuning_system_prompt()
    user_prompt = _build_tuning_user_prompt(context["evidence"], context["accounts"], context["mode"])
    reviewers[slot] = _call_reviewer(slot, cfg, system_prompt, user_prompt)
    result = reviewers[slot]
    if result["status"] != "completed":
        return _context_audit(
            context, MODE_SINGLE_REVIEW, "single_review_failed", reviewers, False,
            "%s 调用失败：%s" % (labels[slot], result.get("error")),
            None, [], track_evolution=False,
        )

    proposals = list(result.get("proposals") or [])
    reason = "单AI审阅（%s）完成，仅供参考；单AI结果不构成共识，不会进入应用门禁" % labels[slot]
    outcome = _context_audit(
        context, MODE_SINGLE_REVIEW, "single_review", reviewers, False, reason,
        None, proposals, track_evolution=False,
    )
    outcome["single_reviewer_slot"] = slot
    return outcome


def _run_dual_review(context):
    """双AI共识：两个槽位都必须就绪，各自独立调用，全部成功后过共识门禁。"""
    slot_configs = context["slot_configs"]
    labels = context["labels"]
    reviewers = {s: _empty_reviewer(s, slot_configs[s], "not_run") for s in AI_SLOTS}

    for slot in AI_SLOTS:
        cfg = slot_configs[slot]
        if not str(cfg["api_key"]).strip():
            reviewers[slot] = _empty_reviewer(slot, cfg, "not_configured")
            return _context_audit(
                context, MODE_DUAL_REVIEW, "%s_not_configured" % slot, reviewers, False,
                "%s API Key 未配置，双AI共识按 fail-closed 终止（不降级为单AI）" % labels[slot],
                None, [], track_evolution=False,
            )
    for slot in AI_SLOTS:
        if not slot_configs[slot]["enabled"]:
            reviewers[slot] = _empty_reviewer(slot, slot_configs[slot], "disabled")
            return _context_audit(
                context, MODE_DUAL_REVIEW, "%s_disabled" % slot, reviewers, False,
                "%s 已禁用，双AI共识按 fail-closed 终止（不降级为单AI）" % labels[slot],
                None, [], track_evolution=False,
            )

    system_prompt = _build_tuning_system_prompt()
    user_prompt = _build_tuning_user_prompt(context["evidence"], context["accounts"], context["mode"])

    with ThreadPoolExecutor(max_workers=len(AI_SLOTS), thread_name_prefix="ai-review") as pool:
        futures = {
            pool.submit(_call_reviewer, slot, slot_configs[slot], system_prompt, user_prompt): slot
            for slot in AI_SLOTS
        }
        for future in as_completed(futures):
            slot = futures[future]
            try:
                reviewers[slot] = future.result()
            except Exception as exc:  # noqa: BLE001
                reviewers[slot] = _empty_reviewer(slot, slot_configs[slot], "failed", error=str(exc)[:200])

    left = reviewers["ai1"]
    right = reviewers["ai2"]
    both_ok = left["status"] == "completed" and right["status"] == "completed"

    consensus = False
    reason = ""
    merged = []
    if both_ok:
        if left["decision"] == "hold" and right["decision"] == "hold":
            reason = "双AI一致认为当前证据不足，保持现状"
        elif left["decision"] == "propose" and right["decision"] == "propose":
            evolution, evolution_by_account = _evolution_bounds(
                context["connect_factory"], context["accounts_map"])
            consensus, reason, merged = _check_consensus(
                {"ai1": left["proposals"], "ai2": right["proposals"]},
                context["accounts_map"], evolution=evolution,
                evolution_by_account=evolution_by_account, labels=labels,
            )
        else:
            reason = "决策分歧：%s=%s, %s=%s" % (
                labels["ai1"], left["decision"], labels["ai2"], right["decision"])
    else:
        errors = []
        for slot in AI_SLOTS:
            reviewer = reviewers[slot]
            if reviewer["status"] == "failed":
                errors.append("%s失败: %s" % (labels[slot], reviewer.get("error")))
            elif reviewer["status"] != "completed":
                errors.append("%s未完成(%s)" % (labels[slot], reviewer["status"]))
        reason = "; ".join(errors) or "双AI调用未完成"

    status = "consensus" if consensus else ("both_hold" if both_ok and not merged else "no_consensus")
    return _context_audit(
        context, MODE_DUAL_REVIEW, status, reviewers, consensus, reason,
        merged, merged, track_evolution=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 读取
# ─────────────────────────────────────────────────────────────────────────────

def recent_runs(conn, limit=20):
    """读取最近的审核记录（通用槽位视图 + 历史别名，保持旧调用方可用）。"""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT id, trigger, mode, status, profile_date, market_regime,
                  mimo_status, mimo_model, mimo_latency_ms, mimo_error,
                  deepseek_status, deepseek_model, deepseek_latency_ms, deepseek_error,
                  consensus_result, consensus_reason, merged_proposals,
                  total_latency_ms, created_at, finished_at, applied_ids,
                  review_mode, result_mode, config_snapshot, reviewers
           FROM dual_ai_tuning_runs ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        stored_reviewers = _loads(row[24], None)
        if not isinstance(stored_reviewers, dict):
            stored_reviewers = {}
        left = stored_reviewers.get("ai1") or {
            "status": row[6], "model": row[7], "latency_ms": row[8], "error": row[9]}
        right = stored_reviewers.get("ai2") or {
            "status": row[10], "model": row[11], "latency_ms": row[12], "error": row[13]}
        merged = _loads(row[16], [])
        result.append({
            "id": row[0], "trigger": row[1], "mode": row[2], "status": row[3],
            "profile_date": row[4], "market_regime": row[5],
            "review_mode": row[21] or "dual",
            "result_mode": row[22] or MODE_DUAL_REVIEW,
            "config_snapshot": _loads(row[23], {}) if row[23] else {},
            "reviewers": {"ai1": left, "ai2": right},
            # 历史别名：老界面/老调用方仍按 mimo/deepseek 读
            "mimo": {"status": row[6], "model": row[7], "latency_ms": row[8], "error": row[9]},
            "deepseek": {"status": row[10], "model": row[11], "latency_ms": row[12], "error": row[13]},
            "consensus_result": row[14], "consensus_reason": row[15],
            "merged_proposals": merged, "proposals": merged,
            "total_latency_ms": row[17], "created_at": row[18], "finished_at": row[19],
            "applied_ids": _loads(row[20], None),
        })
    return result


def review_status(conn):
    """整体状态：审核模式 + 两个槽位的就绪度 + 最近记录。"""
    view = review_settings_view(conn)
    recent = recent_runs(conn, 5)
    last_consensus = next((r for r in recent if r.get("consensus_result") == "consensus"), None)
    return {
        "version": AI_REVIEW_VERSION,
        "review_mode": view["review_mode"],
        "single_reviewer_slot": view["single_reviewer_slot"],
        "slot_order": view["slot_order"],
        "slots": view["slots"],
        "single_ready": view["single_ready"],
        "dual_ready": view["dual_ready"],
        # 历史兼容字段（旧界面 + 旧调用方仍读 providers / *_ready）
        "providers": {"mimo": view["slots"]["ai1"], "deepseek": view["slots"]["ai2"]},
        "mimo_ready": view["slots"]["ai1"]["ready"],
        "deepseek_ready": view["slots"]["ai2"]["ready"],
        "recent_runs": recent,
        "last_consensus": last_consensus,
        "consensus_rules": {
            "weight_direction_threshold": CONSENSUS_WEIGHT_DIRECTION_THRESHOLD,
            "weight_magnitude_ratio": CONSENSUS_WEIGHT_MAGNITUDE_RATIO,
            "delta_direction_threshold": CONSENSUS_DELTA_DIRECTION_THRESHOLD,
            "condition_magnitude_ratio": CONSENSUS_CONDITION_MAGNITUDE_RATIO,
            "rule": "两个AI必须同时propose且方向一致、幅度接近，才合并为最终提案",
        },
    }
