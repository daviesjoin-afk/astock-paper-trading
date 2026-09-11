# -*- coding: utf-8 -*-
"""进化参数的生命周期契约：候选 / 校验 / 显式激活 / 回滚。

本模块存在的唯一理由是把三件事彻底拆开：

    candidate  ──validate──▶  validated  ──activate──▶  active pointer

修复的根因：旧实现用 ``ORDER BY id DESC LIMIT 1`` 推断"当前生效参数"，
于是 **插入一条候选行 == 立即成为 current**。而 ``self_evolution`` 的
current params 会被 ``dual_ai_tuner`` 当作调参边界直接消费，所以一条
从未被校验、从未被批准、从未被激活的候选，会立刻改变调参器行为。

契约（永久不变量）：

    candidate != validated
    validated != active
    latest    != active
    created   != approved

只有 ``evolution_active_params`` 里的显式指针能决定 runtime current params。
``latest row`` 不再是任何语义的权威来源。

Scope 模型：

    __global__            全局参数指针
    strategy:<strategy_id> 策略专属指针

策略 scope **没有指针是合法状态**，语义是"继承当前全局 active 参数"，
不是错误。因此策略候选必须记录它创建时的 *effective base*（可能是全局
指针指向的行），否则全局指针前移后旧候选仍会被错误激活。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from adaptive_common import _json, _loads, _now

# ─── scope ───

SCOPE_GLOBAL = "__global__"

#: activation history 的 action 取值
ACTION_BOOTSTRAP = "bootstrap"
ACTION_LEGACY_BOOTSTRAP = "legacy_bootstrap"
ACTION_ACTIVATE = "activate"
ACTION_ROLLBACK = "rollback"

#: 只有这些 action 记录了"一次真正的切换"，rollback 目标由它们决定。
_SWITCH_ACTIONS = (ACTION_ACTIVATE, ACTION_BOOTSTRAP, ACTION_LEGACY_BOOTSTRAP)

#: evolution_params.validation_state 取值
STATE_CANDIDATE = "candidate"
STATE_VALIDATED = "validated"
STATE_REJECTED = "rejected"

#: evolution_log 的事件类型
LOG_CANDIDATE_CREATED = "candidate_created"
LOG_CANDIDATE_VALIDATED = "candidate_validated"
LOG_CANDIDATE_REJECTED = "candidate_rejected"
LOG_CANDIDATE_ACTIVATED = "candidate_activated"
LOG_CANDIDATE_STALE_REJECTED = "candidate_stale_rejected"
LOG_ROLLBACK = "rollback"
LOG_LEGACY_BOOTSTRAP = "legacy_bootstrap"

#: 迁移标记：legacy bootstrap 只能跑一次。
#: 若可重复执行，"迁移后新建、从未激活"的策略候选行会被当成旧系统的
#: latest row 静默激活 —— 那正是本模块要消灭的行为。
META_LEGACY_BOOTSTRAP = "legacy_bootstrap_done"

#: 允许申报"风险放大已获授权（Challenger 胜出）"的候选来源。
#: 其它来源（AI 自动进化 / 人工）一律按 challenger_win=False 校验，
#: 只能收紧风险，不能放大。
_EXPANSION_AUTHORIZED_SOURCES = ("challenger_promotion",)

#: 生命周期列（幂等补齐，绝不重写历史行的 params/reason/created_at）
_LIFECYCLE_COLUMNS = (
    ("strategy_id", "TEXT"),
    ("validation_state", "TEXT"),
    ("validated_at", "TEXT"),
    ("validation_detail", "TEXT"),
    ("base_params_id", "INTEGER"),
    # 候选创建时携带的证据量。必须随候选一起持久化，否则校验阶段无法
    # 确定性重放画像的 min_samples 判定（不能依赖调用方再传一次）。
    ("evidence_count", "INTEGER"),
)

#: 读生命周期行时统一 SELECT 的列。
_PARAMS_SELECT = """SELECT id, version, params, source, reason, parent_id,
                           performance_snapshot, strategy_id, created_at,
                           validation_state, validated_at, validation_detail,
                           base_params_id, evidence_count
                    FROM evolution_params"""


class EvolutionLifecycleError(RuntimeError):
    """存储层生命周期异常：指针丢失/损坏。必须 fail closed，绝不回落 latest。"""


class EvolutionCandidateError(RuntimeError):
    """候选生命周期异常基类。"""

    http_status = 409


class CandidateNotFound(EvolutionCandidateError):
    http_status = 404


class CandidateNotValidated(EvolutionCandidateError):
    http_status = 409


class CandidateStale(EvolutionCandidateError):
    http_status = 409


class CandidateRejected(EvolutionCandidateError):
    http_status = 409


class ActivationConflict(EvolutionCandidateError):
    http_status = 409


# ─── 小工具 ───


def scope_key(strategy_id: Optional[str]) -> str:
    """把 strategy_id 映射为 scope key；None/空串表示全局 scope。"""
    text = str(strategy_id or "").strip()
    return SCOPE_GLOBAL if not text else f"strategy:{text}"


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _row_to_dict(cursor, row) -> dict:
    """把一行结果转成 dict —— 不依赖调用方是否设置了 ``row_factory``。

    ``sqlite3.Row`` 可迭代，所以这里的 zip 对 Row 和普通 tuple 都成立。
    本模块被大量不同的连接（生产库、测试库、临时库）调用，绝不能假设
    ``conn.row_factory`` 已设置为 Row。
    """
    return {desc[0]: value
            for desc, value in zip(cursor.description, row, strict=False)}


def _query_one(conn, sql: str, params=()):
    cursor = conn.execute(sql, tuple(params))
    row = cursor.fetchone()
    return None if row is None else _row_to_dict(cursor, row)


def _query_all(conn, sql: str, params=()):
    cursor = conn.execute(sql, tuple(params))
    return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _meta_get(conn, key: str) -> Optional[str]:
    if not _table_exists(conn, "evolution_meta"):
        return None
    row = conn.execute("SELECT value FROM evolution_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO evolution_meta(key, value, updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET
               value=excluded.value, updated_at=excluded.updated_at""",
        (key, str(value), _now()),
    )


def params_digest(params: Any) -> str:
    """参数快照的稳定摘要，用于检测候选行是否在创建后被篡改。"""
    payload = json.dumps(
        params, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_params(row) -> Optional[dict]:
    try:
        value = json.loads(row["params"])
    except (TypeError, ValueError, KeyError):
        return None
    return value if isinstance(value, dict) else None


def _params_of(conn, params_id: int) -> Optional[dict]:
    row = _query_one(conn, "SELECT params FROM evolution_params WHERE id=?",
                     (int(params_id),))
    if row is None:
        return None
    return _row_params(row)


# ─── schema ───


def ensure_schema(conn) -> None:
    """幂等创建生命周期表并补齐列；随后跑一次 legacy bootstrap。"""
    _ensure_params_columns(conn)
    conn.executescript(
        """
        -- 每个 scope 最多一个显式 active 指针。
        CREATE TABLE IF NOT EXISTS evolution_active_params(
            scope_key TEXT PRIMARY KEY,
            params_id INTEGER NOT NULL,
            activated_at TEXT NOT NULL,
            activated_by TEXT NOT NULL,
            reason TEXT,
            FOREIGN KEY(params_id) REFERENCES evolution_params(id)
        );

        -- append-only：谁、什么时候、从哪个 active、切到哪个 active、为什么。
        CREATE TABLE IF NOT EXISTS evolution_activation_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key TEXT NOT NULL,
            from_pointer_params_id INTEGER,
            from_effective_params_id INTEGER,
            to_params_id INTEGER,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evolution_activation_history_scope
            ON evolution_activation_history(scope_key, id DESC);

        -- 一次性迁移标记等元数据。
        CREATE TABLE IF NOT EXISTS evolution_meta(
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    bootstrap_legacy_pointers(conn)


def ensure_ready(conn) -> None:
    """热路径用的惰性迁移入口：schema 已就绪时**只读**，不做任何写入。

    只有下面三种情况才真正跑 ``ensure_schema``：

    1. 生命周期表还不存在（新库或未迁移的旧库）；
    2. ``evolution_params`` 已有行，但既没有迁移标记也没有全局指针
       —— 进程若崩在 CREATE TABLE 与 bootstrap 之间，这里自愈；
    3. 库中还没有任何参数行 —— 此时顺手落迁移标记，避免以后新建的
       候选行在某个时刻被误当成 legacy latest row 激活。
    """
    if not (_table_exists(conn, "evolution_active_params")
            and _table_exists(conn, "evolution_meta")):
        ensure_schema(conn)
        return
    if _meta_get(conn, META_LEGACY_BOOTSTRAP) is not None:
        return
    if not _table_exists(conn, "evolution_params"):
        return
    if conn.execute("SELECT 1 FROM evolution_params LIMIT 1").fetchone() is None:
        _meta_set(conn, META_LEGACY_BOOTSTRAP, _now())
        conn.commit()
        return
    ensure_schema(conn)


def _ensure_params_columns(conn) -> None:
    """幂等补齐 evolution_params 的生命周期列（含旧库的 strategy_id）。"""
    if not _table_exists(conn, "evolution_params"):
        return
    existing = _columns(conn, "evolution_params")
    for name, sql_type in _LIFECYCLE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE evolution_params ADD COLUMN {name} {sql_type}")


# ─── legacy bootstrap ───


def _pointer_row(conn, scope: str):
    return _query_one(
        conn,
        """SELECT params_id, activated_at, activated_by, reason
           FROM evolution_active_params WHERE scope_key=?""",
        (scope,),
    )


def _history_exists(conn, scope: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM evolution_activation_history WHERE scope_key=? LIMIT 1", (scope,)
    ).fetchone()
    return row is not None


def _legacy_scopes(conn) -> dict:
    """旧系统语义下的"当前行"：全局取最新无 strategy 行；每个策略取各自最新行。"""
    scopes: dict[str, int] = {}
    row = conn.execute(
        "SELECT id FROM evolution_params WHERE strategy_id IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        scopes[SCOPE_GLOBAL] = int(row[0])
    rows = conn.execute(
        """SELECT strategy_id, MAX(id) FROM evolution_params
           WHERE strategy_id IS NOT NULL GROUP BY strategy_id"""
    ).fetchall()
    for strategy_id, params_id in rows:
        scopes[scope_key(strategy_id)] = int(params_id)
    return scopes


def bootstrap_legacy_pointers(conn) -> dict:
    """把旧库迁移到显式指针模型，保持旧 runtime 事实。

    旧系统的"当前"就是 latest row，因此 migration 把每个 scope 的 latest row
    设为该 scope 的初始指针 —— 这是**保留**旧 runtime truth，不是重新解释历史。

    **只跑一次**：以 ``evolution_meta.legacy_bootstrap_done`` 为标记。重复执行
    会把迁移之后新建、从未激活的策略候选行误判成旧系统 latest row 并静默激活。
    单个 scope 上"已有指针、或已有 activation history"也一律跳过，保证
    "策略回滚到继承全局"之后不会被重新 bootstrap 出指针。
    """
    if not _table_exists(conn, "evolution_params"):
        return {"bootstrapped": [], "skipped": "no_params_table"}
    _ensure_params_columns(conn)
    if _meta_get(conn, META_LEGACY_BOOTSTRAP) is not None:
        return {"bootstrapped": [], "skipped": "already_bootstrapped"}
    bootstrapped = []
    for scope, params_id in _legacy_scopes(conn).items():
        if _pointer_row(conn, scope) is not None:
            continue
        if _history_exists(conn, scope):
            continue
        detail = _validate_params_payload(conn, params_id, scope)
        now = _now()
        state = STATE_VALIDATED if detail["valid"] else STATE_CANDIDATE
        conn.execute(
            """UPDATE evolution_params
               SET validation_state=?, validated_at=?, validation_detail=?
               WHERE id=? AND (validation_state IS NULL OR validation_state='')""",
            (state, now if detail["valid"] else None,
             _json({"legacy_bootstrap": True, **detail}), int(params_id)),
        )
        _write_pointer(conn, scope, params_id, actor=ACTION_LEGACY_BOOTSTRAP,
                       reason="legacy bootstrap: 旧系统 latest row 即当时 current")
        _append_history(conn, scope,
                        from_pointer_params_id=None, from_effective_params_id=None,
                        to_params_id=params_id, action=ACTION_LEGACY_BOOTSTRAP,
                        actor=ACTION_LEGACY_BOOTSTRAP,
                        reason="旧库迁移：保留迁移瞬间的 latest row 语义")
        _log_event(conn, LOG_LEGACY_BOOTSTRAP, params_id, {
            "scope_key": scope, "validation_state": state,
            "violations": detail["violations"],
        })
        bootstrapped.append({"scope_key": scope, "params_id": params_id, "state": state})
    _meta_set(conn, META_LEGACY_BOOTSTRAP, _now())
    conn.commit()
    return {"bootstrapped": bootstrapped}


# ─── 指针读写 ───


def _write_pointer(conn, scope: str, params_id: int, *, actor: str, reason: str) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO evolution_active_params(scope_key, params_id, activated_at, activated_by, reason)
           VALUES(?,?,?,?,?)
           ON CONFLICT(scope_key) DO UPDATE SET
               params_id=excluded.params_id,
               activated_at=excluded.activated_at,
               activated_by=excluded.activated_by,
               reason=excluded.reason""",
        (scope, int(params_id), now, actor, reason),
    )


def _append_history(conn, scope: str, *, from_pointer_params_id, from_effective_params_id,
                    to_params_id, action: str, actor: str, reason: str) -> int:
    cursor = conn.execute(
        """INSERT INTO evolution_activation_history(
               scope_key, from_pointer_params_id, from_effective_params_id,
               to_params_id, action, actor, reason, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (scope, from_pointer_params_id, from_effective_params_id,
         to_params_id, action, actor, reason, _now()),
    )
    return int(cursor.lastrowid)


def _log_event(conn, event_type: str, params_id, detail: dict, metrics: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO evolution_log(event_type, params_id, detail, metrics, created_at)
           VALUES(?,?,?,?,?)""",
        (event_type, params_id, _json(detail), _json(metrics) if metrics is not None else None, _now()),
    )


def _params_row(conn, params_id: int):
    return _query_one(conn, f"{_PARAMS_SELECT} WHERE id=?", (int(params_id),))


def _shape(row, *, scope: str, inherited_from: Optional[str] = None) -> dict:
    return {
        "id": row["id"],
        "params": _row_params(row) or {},
        "version": row["version"],
        "source": row["source"],
        "reason": row["reason"],
        "parent_id": row["parent_id"],
        "performance_snapshot": _loads(row["performance_snapshot"], {}),
        "strategy_id": row["strategy_id"],
        "created_at": row["created_at"],
        "validation_state": row["validation_state"],
        "validated_at": row["validated_at"],
        "validation_detail": _loads(row["validation_detail"], {}),
        "base_params_id": row["base_params_id"],
        "evidence_count": row["evidence_count"],
        "scope_key": scope,
        "inherited_from": inherited_from,
    }


def resolve_scope_active(conn, scope: str) -> Optional[dict]:
    """读取某 scope 的显式 active 指针；没有指针返回 None。

    指针存在但指向的行不存在 → ``EvolutionLifecycleError``（fail closed）。
    """
    pointer = _pointer_row(conn, scope)
    if pointer is None:
        return None
    row = _params_row(conn, int(pointer["params_id"]))
    if row is None:
        raise EvolutionLifecycleError(
            f"active pointer for scope {scope!r} references missing params_id="
            f"{pointer['params_id']}; refusing to fall back to the latest row"
        )
    shaped = _shape(row, scope=scope)
    shaped["activated_at"] = pointer["activated_at"]
    shaped["activated_by"] = pointer["activated_by"]
    shaped["activation_reason"] = pointer["reason"]
    return shaped


def resolve_effective(conn, strategy_id: Optional[str] = None) -> dict:
    """解析某策略（或全局）**有效** active 参数。

    返回 ``{source, scope_key, active, pointer_params_id}``：

    - ``source='strategy'``：策略有专属指针；
    - ``source='global'``：策略无专属指针（或查询全局），继承全局 active；
    - ``source='default'``：库中完全没有参数行（全新库）。
    """
    if strategy_id:
        scope = scope_key(strategy_id)
        active = resolve_scope_active(conn, scope)
        if active is not None:
            return {"source": "strategy", "scope_key": scope, "active": active,
                    "pointer_params_id": active["id"]}
    global_active = resolve_scope_active(conn, SCOPE_GLOBAL)
    if global_active is not None:
        return {
            "source": "global" if strategy_id else "global",
            "scope_key": SCOPE_GLOBAL,
            "active": global_active,
            "pointer_params_id": global_active["id"],
            "inherited_from": SCOPE_GLOBAL if strategy_id else None,
        }
    return {"source": "default", "scope_key": scope_key(strategy_id), "active": None,
            "pointer_params_id": None}


def has_any_params(conn) -> bool:
    if not _table_exists(conn, "evolution_params"):
        return False
    return conn.execute("SELECT 1 FROM evolution_params LIMIT 1").fetchone() is not None


# ─── 确定性校验（不改变任何指针）───


def _validate_params_payload(conn, params_id: int, scope: str) -> dict:
    """对一行参数做纯结构校验，返回 ``{valid, violations, detail}``。

    只做"结构合法"判断：类型、必备键、有限数值、全局边界、画像可调/锁定、
    单步步长、风险方向、scope 一致性、base 存在性、摘要一致性。
    **不**做任何统计显著性/收益比较（那是 PR-4 的 promotion gate）。
    """
    import self_evolution as SE

    violations: list[str] = []
    row = _query_one(
        conn,
        """SELECT params, strategy_id, base_params_id, source, evidence_count
           FROM evolution_params WHERE id=?""",
        (int(params_id),),
    )
    if row is None:
        return {"valid": False, "violations": ["候选行不存在"], "detail": {}}

    params = _row_params(row)
    if params is None:
        return {"valid": False, "violations": ["params 不是合法 JSON 对象"], "detail": {}}

    strategy_id = row["strategy_id"]
    expected_scope = scope_key(strategy_id)
    if scope != expected_scope:
        violations.append(f"scope 不一致：候选属于 {expected_scope}，校验请求 {scope}")

    # 必备键 + 有限数值 + 全局边界
    for key, bounds in SE.BOUNDS.items():
        if key not in params:
            violations.append(f"缺少必备参数 {key}")
            continue
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            violations.append(f"{key} 必须是数值，实际 {type(value).__name__}")
            continue
        if value != value or value in (float("inf"), float("-inf")):
            violations.append(f"{key} 必须是有限数值")
            continue
        if not (bounds["min"] - 1e-12 <= float(value) <= bounds["max"] + 1e-12):
            violations.append(
                f"{key}={value} 超出全局边界 [{bounds['min']}, {bounds['max']}]"
            )

    base_id = row["base_params_id"]
    base_params = None
    if base_id is not None:
        base_params = _params_of(conn, int(base_id))
        if base_params is None:
            violations.append(f"base_params_id={base_id} 指向不存在的参数行")

    # 策略侧的比较基准必须是"策略实际看到的生效参数"，即画像收紧后的值。
    # 若拿原始 base 行比较，一旦画像边界比全局更紧，任何候选都会被误判
    # 成"步长超限"——因为 clamp 本身就会造成一个差值。
    effective_base = base_params
    if strategy_id and base_params is not None:
        try:
            import evolution_profiles as EP
            effective_base = EP.clamp_to_profile(strategy_id, base_params)
        except Exception:  # noqa: BLE001 - 画像不可用则退回原始 base
            effective_base = base_params

    # 策略画像：可调/锁定/单步步长由画像校验器统一裁决
    if strategy_id and effective_base is not None:
        try:
            import evolution_profiles as EP
            changes = {
                key: value for key, value in params.items()
                if effective_base.get(key) != value
            }
            check = EP.validate_strategy_adjustment(
                str(strategy_id), effective_base, changes,
                evidence_count=row["evidence_count"],
            )
            if not check.get("allowed"):
                violations.extend(check.get("violations") or [])
        except Exception as exc:  # noqa: BLE001 - 画像校验异常按不通过处理
            violations.append(f"画像校验失败：{type(exc).__name__}")

    # 风险方向：放大只允许由 Challenger 胜出路径申报。
    # 校验本身不授予权限 —— 只有来源在白名单里的候选才按 challenger_win=True
    # 复核；其余来源（AI 自动进化 / 人工）一律按 False 复核，只能收紧。
    expansions: list = []
    tightenings: list = []
    if strategy_id and effective_base is not None:
        try:
            import asymmetric_risk as AR
            risk_changes = [
                {"key": key, "old": effective_base.get(key), "new": value}
                for key, value in params.items()
                if key in AR.RISK_DIRECTION_BY_KEY and effective_base.get(key) != value
            ]
            if risk_changes:
                authorized = str(row["source"] or "") in _EXPANSION_AUTHORIZED_SOURCES
                gate = AR.evaluate_risk_adjustments(
                    conn, str(strategy_id), risk_changes,
                    evidence_count=None, challenger_win=authorized,
                    actor="validation", auto_register=False,
                )
                expansions = list(gate.get("expansions") or [])
                tightenings = list(gate.get("tightenings") or [])
                if not gate.get("allowed"):
                    violations.extend(gate.get("violations") or [])
        except Exception as exc:  # noqa: BLE001
            violations.append(f"风险方向校验失败：{type(exc).__name__}")

    # 全局边界（min/max）已经检查过。这里**不**再做单步步长检查：
    # 单步预算是"生成策略"的属性，不是校验的属性 —— evolve 自己就会把
    # hold_bias 一次推两个步长（高失败率时的保守修正），人工纠偏更是
    # 允许一步到位。把它放进校验会拒绝掉这些合法输出，是净回归。

    detail = {
        "params_digest": params_digest(params),
        "scope_key": expected_scope,
        "checked_keys": sorted(SE.BOUNDS),
        "base_params_id": base_id,
        # 已获授权的风险放大：只在**真正激活**时才闭环成 promoted，
        # 未激活/被拒绝的候选永远不会推进提案生命周期。
        "expansions": expansions,
        "tightenings": tightenings,
    }
    return {"valid": not violations, "violations": violations, "detail": detail}


def validate_candidate(conn, params_id: int) -> dict:
    """对候选跑一次确定性校验并落库状态；**绝不改变 active 指针**。"""
    row = _params_row(conn, params_id)
    if row is None:
        raise CandidateNotFound(f"候选 {params_id} 不存在")
    scope = scope_key(row["strategy_id"])
    if row["validation_state"] == STATE_REJECTED:
        raise CandidateRejected(f"候选 {params_id} 已被拒绝，不能重新校验")

    outcome = _validate_params_payload(conn, int(params_id), scope)
    now = _now()
    state = STATE_VALIDATED if outcome["valid"] else STATE_REJECTED
    conn.execute(
        """UPDATE evolution_params SET validation_state=?, validated_at=?, validation_detail=?
           WHERE id=?""",
        (state, now if outcome["valid"] else None,
         _json({"legacy_bootstrap": False, **outcome["detail"],
                "violations": outcome["violations"]}), int(params_id)),
    )
    _log_event(
        conn,
        LOG_CANDIDATE_VALIDATED if outcome["valid"] else LOG_CANDIDATE_REJECTED,
        int(params_id),
        {"scope_key": scope, "violations": outcome["violations"],
         "params_digest": outcome["detail"].get("params_digest")},
    )
    conn.commit()
    return {"params_id": int(params_id), "scope_key": scope,
            "validation_state": state, "valid": outcome["valid"],
            "violations": outcome["violations"], "detail": outcome["detail"]}


def create_candidate(conn, params: dict, *, strategy_id: Optional[str] = None,
                     source: str, reason: str, parent_id: Optional[int] = None,
                     performance_snapshot: Optional[dict] = None,
                     evidence_count: Optional[int] = None,
                     validate: bool = True) -> dict:
    """持久化一个候选快照（不可变），可选立刻做确定性校验。

    绝不触碰 active 指针，也绝不触发任何与"生效"绑定的副作用。
    """
    ensure_schema(conn)
    scope = scope_key(strategy_id)
    effective = resolve_effective(conn, strategy_id)
    base_id = effective["pointer_params_id"]
    version = _version()
    now = _now()
    cursor = conn.execute(
        """INSERT INTO evolution_params(
               version, params, source, reason, parent_id, performance_snapshot,
               created_at, strategy_id, validation_state, base_params_id,
               evidence_count)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (version, _json(params), source, reason, parent_id,
         _json(performance_snapshot) if performance_snapshot is not None else None,
         now, str(strategy_id) if strategy_id else None, STATE_CANDIDATE, base_id,
         None if evidence_count is None else int(evidence_count)),
    )
    params_id = int(cursor.lastrowid)
    _log_event(conn, LOG_CANDIDATE_CREATED, params_id, {
        "scope_key": scope, "source": source, "reason": reason,
        "base_params_id": base_id,
        "base_source": effective["source"],
    })
    conn.commit()

    result = {"params_id": params_id, "scope_key": scope, "source": source,
              "base_params_id": base_id, "base_source": effective["source"],
              "created_at": now, "params": dict(params)}
    if validate:
        outcome = validate_candidate(conn, params_id)
        result.update({"validation_state": outcome["validation_state"],
                       "valid": outcome["valid"],
                       "violations": outcome["violations"],
                       "validation_detail": outcome["detail"]})
    return result


def find_equivalent_candidate(conn, params: dict, *, strategy_id,
                              base_params_id, limit: int = 50):
    """同 scope、同 base、参数逐值相同的既有候选（未被拒绝）。

    用于抑制"重复提交同一调整"造成的候选堆积。要求 base 相同是必须的：
    base 不同的等价候选永远过不了 stale CAS，留着只是噪声。
    """
    target = params_digest(params)
    for row in _scope_rows(conn, scope_key(strategy_id), limit=limit):
        if row["validation_state"] == STATE_REJECTED:
            continue
        if row["base_params_id"] != base_params_id:
            continue
        if params_digest(row["params"]) == target:
            return row["id"]
    return None


def _version() -> str:
    import self_evolution as SE
    return SE.EVOLUTION_VERSION


# ─── 显式激活 ───


def activate_params_candidate(conn, params_id: int, *, actor: str,
                              reason: Optional[str] = None,
                              side_effects: bool = True) -> dict:
    """唯一允许推进 active 指针的常规入口。

    前置条件（任一不满足即拒绝，绝不静默覆盖或自动 rebase）：

    - 候选存在且未被拒绝；
    - ``validation_state == validated``；
    - 候选创建时的 base 仍等于当前 effective active（stale CAS）。

    指针推进用条件 UPDATE + rowcount 实现 CAS，并与 history / log 同处一个
    隐式事务：任何一步失败都不会留下半推进的指针。
    """
    ensure_schema(conn)
    row = _params_row(conn, params_id)
    if row is None:
        raise CandidateNotFound(f"候选 {params_id} 不存在")

    state = row["validation_state"]
    strategy_id = row["strategy_id"]
    scope = scope_key(strategy_id)
    if state == STATE_REJECTED:
        raise CandidateRejected(f"候选 {params_id} 已被拒绝")
    if state != STATE_VALIDATED:
        raise CandidateNotValidated(
            f"候选 {params_id} 尚未通过校验（validation_state={state!r}）"
        )

    effective = resolve_effective(conn, strategy_id)
    current_pointer = _pointer_row(conn, scope)
    current_pointer_id = int(current_pointer["params_id"]) if current_pointer else None
    effective_id = effective["pointer_params_id"]

    if current_pointer_id == int(params_id):
        return {"activated": False, "already_active": True, "params_id": int(params_id),
                "scope_key": scope, "active_params_id": current_pointer_id}

    expected_base = row["base_params_id"]
    if expected_base != effective_id:
        _log_event(conn, LOG_CANDIDATE_STALE_REJECTED, int(params_id), {
            "scope_key": scope,
            "candidate_base_params_id": expected_base,
            "current_effective_params_id": effective_id,
            "reason": "候选基线已过期",
        })
        conn.commit()
        raise CandidateStale(
            "evolution candidate is stale; regenerate from current active parameters"
        )

    now = _now()
    if current_pointer is None:
        try:
            conn.execute(
                """INSERT INTO evolution_active_params(
                       scope_key, params_id, activated_at, activated_by, reason)
                   VALUES(?,?,?,?,?)""",
                (scope, int(params_id), now, actor, reason),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ActivationConflict("active pointer 并发变化，请重试") from exc
    else:
        cursor = conn.execute(
            """UPDATE evolution_active_params
               SET params_id=?, activated_at=?, activated_by=?, reason=?
               WHERE scope_key=? AND params_id=?""",
            (int(params_id), now, actor, reason, scope, current_pointer_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            _log_event(conn, LOG_CANDIDATE_STALE_REJECTED, int(params_id), {
                "scope_key": scope,
                "expected_pointer_params_id": current_pointer_id,
                "reason": "active pointer 已被并发推进",
            })
            conn.commit()
            raise ActivationConflict("active pointer 并发变化，请重试")

    _append_history(conn, scope,
                    from_pointer_params_id=current_pointer_id,
                    from_effective_params_id=effective_id,
                    to_params_id=int(params_id), action=ACTION_ACTIVATE,
                    actor=actor, reason=reason or "")
    _log_event(conn, LOG_CANDIDATE_ACTIVATED, int(params_id), {
        "scope_key": scope, "from_pointer_params_id": current_pointer_id,
        "from_effective_params_id": effective_id, "actor": actor,
        "reason": reason or "",
    })
    if side_effects:
        _apply_activation_side_effects(conn, row, scope, actor=actor)
    conn.commit()
    return {"activated": True, "already_active": False, "params_id": int(params_id),
            "scope_key": scope, "active_params_id": int(params_id),
            "from_pointer_params_id": current_pointer_id,
            "from_effective_params_id": effective_id}


def _apply_activation_side_effects(conn, row, scope: str, *, actor: str) -> None:
    """只在这里执行与"真正激活"绑定的生命周期副作用。

    候选创建/校验阶段记录的放大提案，必须在激活时才闭环成 promoted；
    未激活、已过期、被拒绝的候选永远不会走到这里。
    """
    if not row["strategy_id"]:
        return
    detail = _loads(row["validation_detail"], {})
    expansions = detail.get("expansions") or []
    if not expansions:
        return
    try:
        import asymmetric_risk as AR
    except Exception:  # noqa: BLE001
        return
    for expansion in expansions:
        try:
            AR.promote_proposal(conn, str(row["strategy_id"]), expansion.get("key"),
                                expansion.get("new"), actor=actor)
        except Exception:  # noqa: BLE001 - 提案闭环失败不得回滚已完成的激活
            continue


# ─── 回滚 ───


def rollback_active(conn, *, strategy_id: Optional[str] = None, actor: str,
                    reason: Optional[str] = None) -> dict:
    """按 activation history 回滚，绝不看"第二新版本"。

    - 上一次切换是从别的显式指针切过来的 → 恢复那个指针；
    - 上一次切换是从"继承全局"切过来的 → 删除本 scope 指针，恢复继承；
    - 已处于回滚目标 → 安全 no-op。

    **只撤销最近一次切换，且幂等**：同一请求重试不会继续向历史深处滚动。
    这是刻意的 fail-closed 取舍 —— 无法区分"重试"和"想再退一步"时，宁可不动。
    需要回到更早的版本时，应由操作者显式选择并激活对应候选，而不是让回滚
    反复猜。判断依据始终是 history，而不是行的 id 顺序。
    """
    ensure_schema(conn)
    scope = scope_key(strategy_id)
    pointer = _pointer_row(conn, scope)
    if pointer is None:
        return {"rolled_back": False, "reason": "该 scope 没有显式 active 指针",
                "scope_key": scope}

    entry = _query_one(
        conn,
        f"""SELECT from_pointer_params_id, from_effective_params_id, to_params_id, action
            FROM evolution_activation_history
            WHERE scope_key=? AND action IN ({','.join('?' for _ in _SWITCH_ACTIONS)})
            ORDER BY id DESC LIMIT 1""",
        (scope, *_SWITCH_ACTIONS),
    )
    if entry is None:
        return {"rolled_back": False, "reason": "没有可回滚的激活历史",
                "scope_key": scope}

    current_id = int(pointer["params_id"])
    target_pointer = entry["from_pointer_params_id"]
    if target_pointer is None:
        # 之前是"继承全局"：删除指针即可恢复继承，不伪造一个指向全局行的策略指针。
        if scope == SCOPE_GLOBAL:
            return {"rolled_back": False, "reason": "全局参数没有可回滚目标",
                    "scope_key": scope}
        cursor = conn.execute(
            "DELETE FROM evolution_active_params WHERE scope_key=? AND params_id=?",
            (scope, current_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {"rolled_back": False, "reason": "active 指针已变化，回滚未执行",
                    "scope_key": scope}
        _append_history(conn, scope, from_pointer_params_id=current_id,
                        from_effective_params_id=current_id,
                        to_params_id=None, action=ACTION_ROLLBACK, actor=actor,
                        reason=reason or "回滚到继承全局 active")
        _log_event(conn, LOG_ROLLBACK, current_id, {
            "scope_key": scope, "to": "inherit_global", "actor": actor})
        conn.commit()
        return {"rolled_back": True, "scope_key": scope, "target": "inherit_global",
                "previous_params_id": current_id}

    target = int(target_pointer)
    if target == current_id:
        return {"rolled_back": False, "already_at_target": True,
                "scope_key": scope, "active_params_id": current_id}

    cursor = conn.execute(
        """UPDATE evolution_active_params SET params_id=?, activated_at=?, activated_by=?, reason=?
           WHERE scope_key=? AND params_id=?""",
        (target, _now(), actor, reason, scope, current_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        return {"rolled_back": False, "reason": "active 指针已变化，回滚未执行",
                "scope_key": scope}
    _append_history(conn, scope, from_pointer_params_id=current_id,
                    from_effective_params_id=current_id,
                    to_params_id=target, action=ACTION_ROLLBACK, actor=actor,
                    reason=reason or "回滚到上一次 active")
    _log_event(conn, LOG_ROLLBACK, target, {
        "scope_key": scope, "from_params_id": current_id, "to_params_id": target,
        "actor": actor})
    conn.commit()
    return {"rolled_back": True, "scope_key": scope, "target_params_id": target,
            "previous_params_id": current_id}


# ─── 读模型 ───


def _scope_rows(conn, scope: str, *, limit: int = 50) -> list:
    if scope == SCOPE_GLOBAL:
        where, args = "strategy_id IS NULL", ()
    else:
        where, args = "strategy_id=?", (scope[len("strategy:"):],)
    rows = _query_all(
        conn, f"""{_PARAMS_SELECT} WHERE {where} ORDER BY id DESC LIMIT ?""",
        (*args, int(limit)),
    )
    return [_shape(row, scope=scope) for row in rows]


def latest_candidate(conn, strategy_id: Optional[str] = None) -> Optional[dict]:
    scope = scope_key(strategy_id)
    rows = _scope_rows(conn, scope, limit=1)
    return rows[0] if rows else None


def validated_pending(conn, strategy_id: Optional[str] = None) -> list:
    scope = scope_key(strategy_id)
    pointer = _pointer_row(conn, scope)
    active_id = int(pointer["params_id"]) if pointer else None
    return [
        row for row in _scope_rows(conn, scope, limit=50)
        if row["validation_state"] == STATE_VALIDATED and row["id"] != active_id
    ]


def activation_history(conn, strategy_id: Optional[str] = None, limit: int = 50) -> list:
    scope = scope_key(strategy_id)
    return _query_all(
        conn,
        """SELECT id, scope_key, from_pointer_params_id, from_effective_params_id,
                  to_params_id, action, actor, reason, created_at
           FROM evolution_activation_history WHERE scope_key=?
           ORDER BY id DESC LIMIT ?""",
        (scope, int(limit)),
    )


def lifecycle_view(conn, strategy_id: Optional[str] = None) -> dict:
    """读模型：明确区分 ACTIVE / CANDIDATE / VALIDATED-PENDING。"""
    scope = scope_key(strategy_id)
    active = resolve_scope_active(conn, scope)
    if active is None and strategy_id:
        inherited = resolve_scope_active(conn, SCOPE_GLOBAL)
        if inherited is not None:
            active = {**inherited, "inherited_from": SCOPE_GLOBAL}
    return {
        "scope_key": scope,
        "active": active,
        "latest_candidate": latest_candidate(conn, strategy_id),
        "validated_pending": validated_pending(conn, strategy_id),
        "history": activation_history(conn, strategy_id, limit=20),
    }


def pending_candidate_counts(conn) -> dict:
    """所有 scope 的待激活候选数量（状态页用）。"""
    rows = conn.execute(
        """SELECT COALESCE(strategy_id, ?) AS scope, COUNT(*) AS n
           FROM evolution_params
           WHERE validation_state=?
           GROUP BY COALESCE(strategy_id, ?)""",
        (SCOPE_GLOBAL, STATE_VALIDATED, SCOPE_GLOBAL),
    ).fetchall()
    counts = {}
    for row in rows:
        scope = row[0]
        pointer = _pointer_row(conn, scope if scope == SCOPE_GLOBAL else scope_key(scope))
        active_id = int(pointer["params_id"]) if pointer else None
        if active_id is None:
            counts[scope] = int(row[1])
            continue
        remaining = conn.execute(
            """SELECT COUNT(*) FROM evolution_params
               WHERE COALESCE(strategy_id, ?)=? AND validation_state=? AND id!=?""",
            (SCOPE_GLOBAL, scope, STATE_VALIDATED, active_id),
        ).fetchone()[0]
        if remaining:
            counts[scope] = int(remaining)
    return counts


def assert_pointer_integrity(conn) -> None:
    """指针指向的行必须存在；否则 fail closed（不回落 latest）。"""
    for (scope, params_id) in conn.execute(
        "SELECT scope_key, params_id FROM evolution_active_params"
    ).fetchall():
        if _params_row(conn, int(params_id)) is None:
            raise EvolutionLifecycleError(
                f"active pointer for scope {scope!r} references missing params_id={params_id}"
            )
