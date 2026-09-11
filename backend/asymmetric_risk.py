# -*- coding: utf-8 -*-
"""非对称风险进化（PR：asymmetric risk evolution）——**唯一风险放大门**。

风险参数的调整必须**不对称**：

- **收紧**（降 exposure / 降 weight / 减席位 / 止损更紧 / 缩短持有）永远是
  安全方向 → 低证据门槛即可立即生效；
- **放大**（升 exposure / 升 weight / 加席位 / 止损放宽 / 延长持有）必须满足
  四重门槛，**缺一不可**：
  1. 更严格的证据量（≥ ``EXPANSION_MIN_SAMPLES``，远高于收紧门槛）；
  2. **观察期**：提案必须先在 ``risk_expansion_proposals`` 落库登记，经过
     ``OBSERVATION_WINDOW_DAYS`` 天才允许生效——窗口未满只能"登记提案"，
     不能改运行参数（绝不信任调用方自报的天数）；
  3. 单轮幅度硬上限（``MAX_SINGLE_ROUND_STEP``），任何超额一律拒绝；
  4. **Challenger 胜出**：只有 Champion/Challenger 晋升路径
     （``challenger_win=True``）才能把放大落到生产参数。

PR-33：所有风险方向（risk_per_trade / max exposure / max positions /
stop loosen / holding extension …）统一建模为 ``higher_is_riskier`` /
``lower_is_riskier`` / ``neutral``；AI（自进化调参）、UI（设置页）、
Champion promotion（挑战者晋升）、manual API（策略定义 PATCH）四条路径
全部收敛到 :func:`evaluate_risk_adjustments`，没有任何一条能绕开本门。

证据缺失时按 fail-closed 处理：只允许收紧。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Iterable, Mapping

__all__ = [
    "ASYMMETRIC_RISK_VERSION",
    "RISK_DIRECTION_KEYS",
    "RISK_DIRECTION_BY_KEY",
    "RISK_DIRECTIONS",
    "PROPOSAL_STATUSES",
    "TIGHTEN_MIN_SAMPLES",
    "EXPANSION_MIN_SAMPLES",
    "OBSERVATION_WINDOW_DAYS",
    "MAX_SINGLE_ROUND_STEP",
    "classify_risk_change",
    "ensure_proposals_table",
    "ensure_proposal_lifecycle_columns",
    "ensure_proposals_registered",
    "observation_days_for",
    "pending_proposals",
    "register_proposal",
    "resolve_proposal",
    "promote_proposal",
    "reject_proposal",
    "supersede_proposals",
    "evaluate_risk_adjustments",
    "guard_risk_adjustments",
    "validate_risk_updates",
]

ASYMMETRIC_RISK_VERSION = "asymmetric-risk-evolution-v2"

# 风险方向语义：与 DSL 声明式参数（strategy_dsl_schema.RISK_DIRECTIONS）一致。
RISK_DIRECTIONS = ("higher_is_riskier", "lower_is_riskier", "neutral")

# 有方向语义的风险参数（strategy_overrides 的子集，兼容旧调用方）。
RISK_DIRECTION_KEYS = ("max_exposure_pct", "max_weight_pct", "max_positions")

# 统一风险方向表：所有"风险方向"参数都必须在此登记，否则视为中性、不受本门约束。
# - higher_is_riskier：数值越大越激进（敞口、权重、席位、单笔风险、持有天数…）
# - lower_is_riskier ：数值越小越激进（负域止损 -0.05 → -0.08 是放宽）
RISK_DIRECTION_BY_KEY: dict[str, str] = {
    # ---- 账户/策略 override ----
    "max_exposure_pct": "higher_is_riskier",
    "max_weight_pct": "higher_is_riskier",
    "max_positions": "higher_is_riskier",
    # ---- 编译风险画像（soft risk）----
    "risk_per_trade": "higher_is_riskier",
    "single_risk": "higher_is_riskier",
    "max_exposure": "higher_is_riskier",
    "max_industry": "higher_is_riskier",
    # ---- 纪律类（止损/持有/加仓）----
    "hard_stop": "lower_is_riskier",
    "trail_after": "higher_is_riskier",
    "trail_stop": "higher_is_riskier",
    "atr_stop_multiplier": "higher_is_riskier",
    "holding_days": "higher_is_riskier",
    "hold_max": "higher_is_riskier",
    "max_pyramiding": "higher_is_riskier",
    "entry_slices": "higher_is_riskier",
}

# 未知键的默认方向（向后兼容旧行为：数值变大 = 放大）。
DEFAULT_DIRECTION = "higher_is_riskier"

# 收紧：安全方向，低证据门槛。
TIGHTEN_MIN_SAMPLES = 5
# 放大：必须显著更多证据。
EXPANSION_MIN_SAMPLES = 20
# 放大必须经过的观察期（自然日）。
OBSERVATION_WINDOW_DAYS = 10

# 单轮放大幅度硬上限（超出即拒绝，不是夹回边界）。
MAX_SINGLE_ROUND_STEP: dict[str, float] = {
    "max_exposure_pct": 3.0,      # 最多 +3 个百分点
    "max_weight_pct": 3.0,        # 最多 +3 个百分点
    "max_positions": 1,           # 最多 +1 席
    "risk_per_trade": 0.01,       # 单笔风险最多 +1pp
    "single_risk": 0.01,
    "max_exposure": 0.03,
    "max_industry": 0.03,
    "hard_stop": 0.01,            # 止损最多放宽 1pp
    "trail_after": 0.01,
    "trail_stop": 0.01,
    "atr_stop_multiplier": 0.5,
    "holding_days": 2,            # 持有期最多 +2 天
    "hold_max": 2,
    "max_pyramiding": 1,
    "entry_slices": 1,
}

DIRECTION_LABELS = {
    "tighten": "风险收紧",
    "expand": "风险放大",
    "none": "无方向变化",
}

# 提案生命周期：pending（观察中）→ promoted / rejected / superseded。
PROPOSAL_STATUSES = ("pending", "promoted", "rejected", "superseded")


def _direction_for(key: str, declared: Any = None) -> str:
    """解析参数的风险方向：优先 DSL 声明，其次方向表，最后默认。"""
    text = str(declared or "").strip().lower()
    if text in RISK_DIRECTIONS:
        return text
    return RISK_DIRECTION_BY_KEY.get(str(key or ""), DEFAULT_DIRECTION)


def _as_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _is_expansion(direction: str, old: float, new: float) -> bool:
    """按方向语义判断一次数值变化是否等于"风险放大"。

    方向由声明方给定，因此正负域的语义由声明方负责：
    负域止损（-0.05 → -0.08 更松）声明 ``lower_is_riskier``；
    正域止损距离（0.05 → 0.08 更宽）应声明 ``higher_is_riskier``。
    """
    if direction == "lower_is_riskier":
        return new < old
    return new > old


def classify_risk_change(key: str, old: Any, new: Any, direction: Any = None) -> str:
    """判定一次参数变化的方向：tighten / expand / none。"""
    old_value = _as_number(old)
    new_value = _as_number(new)
    if old_value is None or new_value is None:
        return "none"
    if abs(new_value - old_value) < 1e-9:
        return "none"
    return "expand" if _is_expansion(_direction_for(key, direction), old_value, new_value) else "tighten"


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
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT,
            resolved_by TEXT,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_risk_proposals_lookup
            ON risk_expansion_proposals(strategy_id, key, status, proposed_at DESC);
        """
    )
    ensure_proposal_lifecycle_columns(conn)


def ensure_proposal_lifecycle_columns(conn) -> None:
    """为旧库补齐提案生命周期列（resolved_at / resolved_by / note）。"""
    try:
        ensure_proposals_table_schema_only(conn)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(risk_expansion_proposals)").fetchall()}
    except sqlite3.Error:
        return
    for column, definition in (("resolved_at", "TEXT"), ("resolved_by", "TEXT"),
                               ("note", "TEXT")):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE risk_expansion_proposals ADD COLUMN {column} {definition}")


def ensure_proposals_table_schema_only(conn) -> None:
    """只建表、不补列的原子动作（供 ensure_proposal_lifecycle_columns 复用）。"""
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


def register_proposal(conn, strategy_id: str, key: str, old_value: Any, new_value: Any,
                      *, evidence_count: int | None, actor: str = "") -> dict[str, Any] | None:
    """登记一条放大提案并启动观察时钟；同 strategy+key+目标值已有 pending 则返回 None。"""
    if conn is None or evidence_count is None or evidence_count < EXPANSION_MIN_SAMPLES:
        return None
    target = _as_number(new_value)
    if target is None:
        return None
    ensure_proposals_table(conn)
    if _proposal_key_match(conn, strategy_id, key, target) is not None:
        return None
    now = dt.datetime.now().isoformat(timespec="seconds")
    # 同一 strategy+key 的其它 pending 提案被后来的目标值取代。
    supersede_proposals(conn, strategy_id, key, keep_value=target)
    cursor = conn.execute(
        """INSERT INTO risk_expansion_proposals(
               strategy_id,key,old_value,new_value,evidence_count,proposed_at,status,note)
           VALUES(?,?,?,?,?,?, 'pending',?)""",
        (str(strategy_id), str(key), _as_number(old_value), target,
         int(evidence_count), now, str(actor or "")),
    )
    conn.commit()
    return {
        "id": int(cursor.lastrowid), "strategy_id": str(strategy_id), "key": str(key),
        "old_value": _as_number(old_value), "new_value": target,
        "evidence_count": int(evidence_count), "proposed_at": now, "status": "pending",
    }


def supersede_proposals(conn, strategy_id: str, key: str, *, keep_value: Any = None,
                        keep_proposal_id: int | None = None) -> int:
    """把同一 strategy+key 的旧 pending 提案标记为 superseded。"""
    if conn is None:
        return 0
    try:
        ensure_proposals_table(conn)
        rows = conn.execute(
            """SELECT id,new_value FROM risk_expansion_proposals
                WHERE strategy_id=? AND key=? AND status='pending'""",
            (str(strategy_id), str(key)),
        ).fetchall()
    except sqlite3.Error:
        return 0
    targets = []
    for row in rows:
        row_id = int(row[0])
        if keep_proposal_id is not None and row_id == int(keep_proposal_id):
            continue
        if keep_value is not None and abs(float(row[1]) - float(keep_value)) < 1e-9:
            continue
        targets.append(row_id)
    if not targets:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "UPDATE risk_expansion_proposals SET status='superseded',resolved_at=?,note=? WHERE id=?",
        [(now, "superseded by a newer proposal", row_id) for row_id in targets],
    )
    conn.commit()
    return len(targets)


def resolve_proposal(conn, proposal_id: int, status: str, *, actor: str = "",
                     note: str = "") -> bool:
    """终结一条提案（promoted / rejected / superseded）。"""
    if conn is None or status not in PROPOSAL_STATUSES or status == "pending":
        return False
    try:
        ensure_proposals_table(conn)
        cursor = conn.execute(
            """UPDATE risk_expansion_proposals
                  SET status=?,resolved_at=?,resolved_by=?,note=?
                WHERE id=? AND status='pending'""",
            (status, dt.datetime.now().isoformat(timespec="seconds"),
             str(actor or ""), str(note or ""), int(proposal_id)),
        )
        conn.commit()
    except sqlite3.Error:
        return False
    return cursor.rowcount == 1


def promote_proposal(conn, strategy_id: str, key: str, new_value: Any,
                     *, actor: str = "") -> bool:
    """放大真正落到生产参数后，把对应 pending 提案标记为 promoted。"""
    proposal = _proposal_key_match(conn, strategy_id, key, _as_number(new_value))
    if proposal is None:
        return False
    return resolve_proposal(conn, int(proposal["id"]), "promoted",
                            actor=actor, note="expansion applied to production parameters")


def promote_proposal_tx(conn, strategy_id: str, key: str, new_value: Any,
                       *, actor: str = "", note: str = "") -> int:
    """**事务内**闭环一条 pending 提案：不建表、不 commit、不吞异常。

    返回受影响行数（0 = 没有匹配的 pending 提案，**调用方必须视为失败**）。

    为什么不能直接用 ``promote_proposal()``：它走
    ``_proposal_key_match → ensure_proposals_table → executescript``，并经由
    ``resolve_proposal`` 内部 ``conn.commit()``。``executescript`` 与显式
    commit 都会把外层**尚未提交**的写入提前落盘 —— 激活路径上这会先把
    active pointer / history / log 提交掉，之后的 rollback 就撤不回来了。

    调用方负责在开启事务前先把表建好（``ensure_proposals_table``），本函数
    遇到任何 SQLite 错误都直接冒泡，绝不静默返回 0。
    """
    target = _as_number(new_value)
    if target is None:
        return 0
    row = conn.execute(
        """SELECT id FROM risk_expansion_proposals
            WHERE strategy_id=? AND key=? AND status='pending'
              AND ABS(new_value-?)<1e-9
            ORDER BY proposed_at DESC LIMIT 1""",
        (str(strategy_id), str(key), target),
    ).fetchone()
    if row is None:
        return 0
    cursor = conn.execute(
        """UPDATE risk_expansion_proposals
              SET status='promoted', resolved_at=?, resolved_by=?, note=?
            WHERE id=? AND status='pending'""",
        (dt.datetime.now().isoformat(timespec="seconds"), str(actor or ""),
         str(note or ""), int(row[0])),
    )
    return int(cursor.rowcount)


def reject_proposal(conn, proposal_id: int, *, actor: str = "", note: str = "") -> bool:
    """人工拒绝一条放大提案（不可再被任何路径自动生效）。"""
    return resolve_proposal(conn, proposal_id, "rejected", actor=actor, note=note or "manual rejection")


def pending_proposals(conn, strategy_id: str | None = None) -> list[dict[str, Any]]:
    """列出仍在观察期的放大提案。"""
    if conn is None:
        return []
    try:
        ensure_proposals_table(conn)
        if strategy_id:
            rows = conn.execute(
                """SELECT * FROM risk_expansion_proposals
                    WHERE strategy_id=? AND status='pending' ORDER BY id DESC""",
                (str(strategy_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM risk_expansion_proposals
                    WHERE status='pending' ORDER BY id DESC""").fetchall()
    except sqlite3.Error:
        return []
    result = []
    for row in rows:
        item = dict(row)
        item["observation_days"] = _observed_days(item.get("proposed_at"))
        item["remaining_days"] = max(0, OBSERVATION_WINDOW_DAYS - item["observation_days"])
        result.append(item)
    return result


def _observed_days(proposed_at: Any) -> int:
    try:
        moment = dt.datetime.fromisoformat(str(proposed_at)[:19])
    except (TypeError, ValueError):
        return 0
    return max(0, (dt.datetime.now() - moment).days)


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
    registered: list[dict[str, Any]] = []
    for strategy_id, proposed in dict(proposed_overrides or {}).items():
        current = dict((current_overrides or {}).get(strategy_id) or {})
        for key, new_raw in dict(proposed or {}).items():
            if key not in RISK_DIRECTION_BY_KEY:
                continue
            direction = classify_risk_change(key, current.get(key), new_raw)
            if direction != "expand":
                continue
            new_value = _as_number(new_raw)
            old_value = _as_number(current.get(key))
            if new_value is None or old_value is None:
                continue
            step = MAX_SINGLE_ROUND_STEP.get(key)
            if step is not None and abs(new_value - old_value) > float(step) + 1e-9:
                continue  # 超上限的放大永不登记
            registered_item = register_proposal(
                conn, strategy_id, key, old_value, new_value,
                evidence_count=evidence_count, actor="evolution",
            )
            if registered_item is not None:
                registered.append(registered_item)
    return registered


def observation_days_for(conn, strategy_id: str, key: str, new_value: Any) -> int | None:
    """从持久化提案的登记时间推算已观察天数；无提案返回 None。"""
    if conn is None:
        return None
    new_value_f = _as_number(new_value)
    if new_value_f is None:
        return None
    proposal = _proposal_key_match(conn, strategy_id, key, new_value_f)
    if proposal is None:
        return None
    return _observed_days(proposal["proposed_at"])


def evaluate_risk_adjustments(
    conn,
    strategy_id: str,
    changes: Iterable[Mapping[str, Any]],
    *,
    evidence_count: int | None,
    challenger_win: bool = False,
    actor: str = "",
    auto_register: bool = True,
) -> dict[str, Any]:
    """**唯一风险放大门**：对一组 {key, old, new, direction?} 变化做非对称校验。

    - **收紧**：安全方向。人工/运营路径（未提供证据上下文）可直接收紧；
      携带证据但不足 ``TIGHTEN_MIN_SAMPLES`` 时拒绝；
    - **放大**：``evidence_count >= EXPANSION_MIN_SAMPLES`` **且** 观察期
      （从落库提案的登记时间推算，调用方自报天数不被信任）满
      ``OBSERVATION_WINDOW_DAYS`` 天 **且** 单轮幅度不超过
      ``MAX_SINGLE_ROUND_STEP`` **且** ``challenger_win`` 为 True。

    返回 ``{allowed, violations, expansions, tightenings, window_pending, registered}``。
    """
    violations: list[str] = []
    expansions: list[dict[str, Any]] = []
    tightenings: list[dict[str, Any]] = []
    window_pending: list[dict[str, Any]] = []
    registered: list[dict[str, Any]] = []
    for change in changes or ():
        key = str(change.get("key") or "")
        if not key:
            continue
        old_value = _as_number(change.get("old"))
        new_value = _as_number(change.get("new"))
        if old_value is None or new_value is None:
            continue
        direction = _direction_for(key, change.get("direction"))
        if direction == "neutral":
            continue
        if abs(new_value - old_value) < 1e-9:
            continue
        label = f"{strategy_id}.{key}"
        kind = "expand" if _is_expansion(direction, old_value, new_value) else "tighten"
        if kind == "tighten":
            if evidence_count is not None and evidence_count < TIGHTEN_MIN_SAMPLES:
                violations.append(
                    f"{label} 风险收紧证据不足（{evidence_count}/{TIGHTEN_MIN_SAMPLES}）"
                )
                continue
            tightenings.append({
                "strategy_id": strategy_id, "key": key,
                "old": old_value, "new": new_value,
            })
            continue
        # ---- 放大：四重门槛 ----
        delta = abs(new_value - old_value)
        step = MAX_SINGLE_ROUND_STEP.get(key)
        step_ok = step is None or delta <= float(step) + 1e-9
        if not step_ok:
            violations.append(
                f"{label} 单轮放大超过硬上限 +{step}"
                f"（{old_value} → {new_value}），禁止单轮大幅扩大风险"
            )
            continue
        if evidence_count is None:
            violations.append(
                f"{label} 风险放大必须提供证据样本数"
                f"（≥{EXPANSION_MIN_SAMPLES}）、经过观察期并由 Challenger 胜出"
            )
            continue
        if evidence_count < EXPANSION_MIN_SAMPLES:
            violations.append(
                f"{label} 风险放大证据不足"
                f"（{evidence_count}/{EXPANSION_MIN_SAMPLES}）；"
                f"放大需要比收紧（{TIGHTEN_MIN_SAMPLES}）更严格的证据"
            )
            continue
        # 证据达标：先把观察时钟跑起来（哪怕 Challenger 还没赢）。
        newly_registered = False
        if auto_register:
            item = register_proposal(
                conn, strategy_id, key, old_value, new_value,
                evidence_count=evidence_count, actor=actor or "evolution",
            )
            if item is not None:
                registered.append(item)
                newly_registered = True
        observed = observation_days_for(conn, strategy_id, key, new_value) \
            if conn is not None else None
        if observed is None or newly_registered:
            window_pending.append({"strategy_id": strategy_id, "key": key, "new": new_value})
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
        if not challenger_win:
            violations.append(
                f"{label} 风险放大必须由 Challenger 胜出后晋升"
                "（challenger_win=True），任何自动/人工路径都不得直接放大"
            )
            continue
        expansions.append({
            "strategy_id": strategy_id, "key": key,
            "old": old_value, "new": new_value,
            "evidence_count": evidence_count,
            "observation_days": observed,
        })
    return {
        "allowed": not violations,
        "violations": violations,
        "expansions": expansions,
        "tightenings": tightenings,
        "window_pending": window_pending,
        "registered": registered,
        "version": ASYMMETRIC_RISK_VERSION,
    }


def dsl_parameter_changes(old_ast: Any, new_ast: Any) -> list[dict[str, Any]]:
    """对比两棵 DSL AST 的**已声明参数值**，产出风险方向变化清单。

    只比较两侧都存在的 ``{"op": "parameter"}`` 节点的 value：新增/删除参数
    没有可比基线，结构性变更由 ``StrategyParameterSchema``（结构校验和）把关。
    """
    from collections.abc import Mapping as _Mapping

    def walk(node: Any, out: dict[str, tuple[Any, Any]]) -> None:
        if isinstance(node, _Mapping):
            if node.get("op") == "parameter" and node.get("parameter_id") is not None:
                out[str(node["parameter_id"])] = (node.get("value"), node.get("risk_direction"))
            for item in node.values():
                walk(item, out)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, out)

    old_map: dict[str, tuple[Any, Any]] = {}
    new_map: dict[str, tuple[Any, Any]] = {}
    if old_ast is not None:
        walk(old_ast, old_map)
    if new_ast is not None:
        walk(new_ast, new_map)
    changes: list[dict[str, Any]] = []
    for parameter_id, (new_value, direction) in new_map.items():
        if parameter_id not in old_map:
            continue
        old_value = old_map[parameter_id][0]
        if _as_number(old_value) is None or _as_number(new_value) is None:
            continue
        changes.append({
            "key": parameter_id, "old": old_value, "new": new_value,
            "direction": direction,
        })
    return changes


def guard_definition_change(conn, strategy_id: str, old_ast: Any, new_ast: Any, *,
                            evidence_count: int | None, challenger_win: bool = False,
                            actor: str = "") -> dict[str, Any]:
    """策略定义（DSL）写入前的风险方向闸门；不通过直接抛 ``ValueError``。"""
    changes = dsl_parameter_changes(old_ast, new_ast)
    if not changes:
        return {"allowed": True, "violations": [], "expansions": [], "tightenings": [],
                "registered": [], "window_pending": [], "version": ASYMMETRIC_RISK_VERSION}
    return guard_risk_adjustments(
        conn, strategy_id, changes, evidence_count=evidence_count,
        challenger_win=challenger_win, actor=actor,
    )


def guard_risk_adjustments(conn, strategy_id: str, changes: Iterable[Mapping[str, Any]], *,
                           evidence_count: int | None, challenger_win: bool = False,
                           actor: str = "", auto_register: bool = True) -> dict[str, Any]:
    """过闸失败的便捷封装：直接抛 ``ValueError``（调用方无需重复拼错误信息）。"""
    gate = evaluate_risk_adjustments(
        conn, strategy_id, changes, evidence_count=evidence_count,
        challenger_win=challenger_win, actor=actor, auto_register=auto_register,
    )
    if not gate["allowed"]:
        raise ValueError("；".join(gate["violations"]))
    return gate


def validate_risk_updates(
    current_overrides: Mapping[str, Mapping[str, Any]],
    proposed_overrides: Mapping[str, Mapping[str, Any]],
    *,
    evidence_count: int | None,
    conn: sqlite3.Connection | None = None,
    challenger_win: bool = False,
) -> dict[str, Any]:
    """对一组 strategy_overrides 更新执行非对称风险校验（统一门的分层视图）。

    语义与 :func:`evaluate_risk_adjustments` 完全一致：收紧快速放行，放大需要
    证据 + 观察期 + 单轮上限 + Challenger 胜出。证据缺失时放大一律拒绝
    （fail-closed），收紧视为运营人工操作放行。
    """
    merged: dict[str, Any] = {
        "allowed": True, "violations": [], "expansions": [], "tightenings": [],
        "window_pending": [], "registered": [], "version": ASYMMETRIC_RISK_VERSION,
    }
    for strategy_id, proposed in dict(proposed_overrides or {}).items():
        current = dict((current_overrides or {}).get(strategy_id) or {})
        changes = [
            {
                "key": key,
                "old": current.get(key),
                "new": new_raw,
                "direction": RISK_DIRECTION_BY_KEY[key],
            }
            for key, new_raw in dict(proposed or {}).items()
            if key in RISK_DIRECTION_BY_KEY
        ]
        if not changes:
            continue
        gate = evaluate_risk_adjustments(
            conn, str(strategy_id), changes, evidence_count=evidence_count,
            challenger_win=challenger_win, actor="runtime_settings",
        )
        for field in ("violations", "expansions", "tightenings",
                      "window_pending", "registered"):
            merged[field].extend(gate[field])
    merged["allowed"] = not merged["violations"]
    return merged
