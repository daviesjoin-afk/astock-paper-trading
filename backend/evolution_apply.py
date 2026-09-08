# -*- coding: utf-8 -*-
"""自进化落地通道（A 批闭环）：影子结论 → 人工门禁 → 模拟盘运行参数。

闭合三条此前断头的链路，全部 fail-closed（需 confirmed=True 的人工确认）：

A1 ``apply_allocation``        — Bandit 策略权重 → 共享资金池分摊覆盖
                                  （paper_accounts.params.adaptive_allocation，
                                  由 paper_trading._strategy_pool_budget 消费）。
A3 ``apply_tuner_proposals``   — 双AI共识提案 → 选股因子权重/入场阈值覆盖
                                  （paper_accounts.params.adaptive_selection，
                                  由 paper_trading._adaptive_selection 消费）。
A2 由 dual_ai_tuner 直接接线：调参边界读取 self_evolution 当前参数版本。

安全边界（与 adaptive_engine._manual_apply_gate 同级）：
- 人工确认 + 自动身份拒绝（evolution_adversarial.require_human_confirmation）；
- 影子/证据不足阶段一律拒绝（allocation 需 advisory/eligible_for_review）；
- 候选/决策/运行记录 30 分钟有效期；
- 幅度硬边界来自 self_evolution 当前参数版本（权重步长/入场阈值步长）；
- 每次 apply 先保存被覆盖的前值，rollback_* 精确回滚，不重放、不猜测。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import zoneinfo
import paper_repository as PRP

import evolution_adversarial as adversarial
from adaptive_common import _json, _loads, _now

TZ = zoneinfo.ZoneInfo("Asia/Shanghai")
_APPLY_MAX_AGE_SECONDS = 30 * 60

# Bandit 权重可应用的最低阶段：advisory 之后才允许人工批准落地。
_ALLOCATION_APPLY_STAGES = {"advisory", "eligible_for_review"}


def _aware(value):
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


def _paper_connect(paper_db_path):
    conn = sqlite3.connect(paper_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _account_params(row):
    return _loads(row["params"] if "params" in row.keys() else "{}", {}) or {}


def _baseline_weights(account_params, source_strategy):
    """账户当前的因子权重基线：优先生效覆盖，否则策略注册表基准。

    与 paper_trading._adaptive_selection 的校验口径一致：权重键必须与
    strategies.PAPER_WEIGHTS[模型族] 完全一致，否则视为不可表达。
    """
    import strategies as S
    meta = account_params.get("adaptive_selection_meta") or {}
    overlay = account_params.get("adaptive_selection") or {}
    source = str(source_strategy or "")
    if overlay.get("weights") and str(meta.get("status") or "") == "active":
        base = dict(overlay["weights"])
        if base and set(base) <= set(S.PAPER_WEIGHTS.get(source, {})):
            return base
    weights = S.PAPER_WEIGHTS.get(source)
    if not weights:
        raise ValueError(f"账户（模型族 {source or '未知'}）没有可表达的因子权重基线")
    return dict(weights)


def _baseline_conditions(account_params):
    overlay = account_params.get("adaptive_selection") or {}
    meta = account_params.get("adaptive_selection_meta") or {}
    if str(meta.get("status") or "") == "active":
        return dict(overlay.get("conditions") or {})
    return {}


def _write_account_params(conn, account_id, params):
    conn.execute(
        "UPDATE paper_accounts SET params=?,updated_at=? WHERE id=?",
        (_json(params), _now(), account_id),
    )


def _audit(conn, account_id, event, detail):
    try:
        PRP.audit(
            conn, str(account_id or "")[:40], str(event)[:60], _json(detail), _now(),
        )
    except sqlite3.Error:
        pass  # 审计失败不阻断主流程（与其它影子路径一致）


# ---------------------------------------------------------------------------
# A1：Bandit 策略权重 → 共享资金池分摊
# ---------------------------------------------------------------------------

def _validate_allocation_weights(weights, strategy_ids, min_pct, max_pct):
    if not isinstance(weights, dict) or set(weights) != set(strategy_ids):
        raise ValueError("权重必须覆盖全部注册策略且不包含未知策略")
    total = 0.0
    for key, value in weights.items():
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"策略 {key} 权重不是有限数值")
        if v < min_pct - 1e-6 or v > max_pct + 1e-6:
            raise ValueError(f"策略 {key} 权重 {v:.1f}% 超出 [{min_pct:.1f}, {max_pct:.1f}] 边界")
        total += v
    if not 99.5 <= total <= 100.5:
        raise ValueError(f"权重合计 {total:.1f}% ≠ 100%")
    return {key: round(float(value), 1) for key, value in weights.items()}


def apply_allocation(adaptive_connect, paper_db_path, decision_id: int,
                     approved_by: str = "human", confirmed: bool = False):
    """把一条 Bandit 决策的策略权重写入共享资金池分摊（人工批准）。

    成功后每个账户 params 中写入 ``adaptive_allocation``（含 weight_pct /
    decision_id / status=active / effective_date），并在
    ``adaptive_allocation_previous`` 保存被替换的前值供回滚。
    """
    actor = adversarial.require_human_confirmation(approved_by, confirmed=confirmed)
    decision_id = int(decision_id)
    with adaptive_connect() as conn:
        row = conn.execute(
            "SELECT * FROM adaptive_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if not row:
            raise ValueError("Bandit 决策不存在，拒绝应用")
        if str(row["status"] or "") == "applied":
            raise ValueError("该决策已应用，请勿重复应用")
        if str(row["stage"] or "") not in _ALLOCATION_APPLY_STAGES:
            raise ValueError(
                f"决策仍处于 {row['stage']} 阶段（影子/盘面验证不足），拒绝应用"
            )
        updated = _aware(row["updated_at"])
        now = dt.datetime.now(TZ)
        if updated is None or (now - updated).total_seconds() > _APPLY_MAX_AGE_SECONDS:
            raise ValueError("决策快照已超过30分钟有效期，请重新运行学习生成")

        import adaptive_engine as engine
        cfg = engine._config(conn)
        min_pct = float(cfg["min_strategy_weight_pct"])
        max_pct = float(cfg["max_strategy_weight_pct"])
        weights = _validate_allocation_weights(
            _loads(row["weights"], {}) or {}, sorted(engine.ACCOUNT_LABELS), min_pct, max_pct,
        )
        decision_date = str(row["decision_date"] or "")

    # B 批观察期门禁：任一目标账户的上一 allocation 覆盖仍在观察期即拒绝。
    import evolution_validation as validation
    for account_id in weights:
        validation.pre_apply_gate(adaptive_connect, paper_db_path, account_id, "allocation")

    effective_date = dt.datetime.now(TZ).date().isoformat()
    paper = _paper_connect(paper_db_path)
    try:
        paper.execute("BEGIN")
        previous_map = {}
        rows = paper.execute(
            "SELECT id,params FROM paper_accounts WHERE id IN (%s)"
            % ",".join("?" * len(weights)),
            tuple(weights.keys()),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        for account_id in weights:
            row = by_id.get(account_id)
            if row is None:
                raise ValueError(f"模拟盘账户 {account_id} 不存在，拒绝应用")
            params = _account_params(row)
            previous_map[account_id] = params.get("adaptive_allocation_previous")
            params["adaptive_allocation_previous"] = params.get("adaptive_allocation")
            params["adaptive_allocation"] = {
                "weight_pct": weights[account_id],
                "decision_id": decision_id,
                "decision_date": decision_date,
                "status": "active",
                "effective_date": effective_date,
                "applied_at": _now(),
                "approved_by": actor,
            }
            _write_account_params(paper, account_id, params)
        paper.commit()
    except Exception:
        paper.rollback()
        paper.close()
        raise
    # paper 侧已提交；adaptive 侧标记失败时补偿恢复前值。
    try:
        with adaptive_connect() as conn:
            conn.execute(
                "UPDATE adaptive_decisions SET status='applied',updated_at=? WHERE id=?",
                (_now(), decision_id),
            )
    except Exception:
        _restore_allocation_snapshot(paper_db_path, {k: v for k, v in
                                                     ((a, previous_map.get(a)) for a in weights)})
        paper.close()
        raise
    paper.close()
    return {"applied": True, "decision_id": decision_id, "weights": weights,
            "effective_date": effective_date, "approved_by": actor}


def _restore_allocation_snapshot(paper_db_path, previous_map):
    """把 {account_id: previous_allocation_or_None} 写回（补偿/回滚共用）。"""
    paper = _paper_connect(paper_db_path)
    try:
        paper.execute("BEGIN")
        for account_id, previous in previous_map.items():
            row = paper.execute(
                "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                continue
            params = _loads(row["params"], {}) or {}
            if previous is None:
                params.pop("adaptive_allocation", None)
            else:
                params["adaptive_allocation"] = previous
            params.pop("adaptive_allocation_previous", None)
            _write_account_params(paper, account_id, params)
            _audit(paper, account_id, "adaptive_allocation_rolled_back",
                   {"restored": bool(previous is not None)})
        paper.commit()
    finally:
        paper.close()


def rollback_allocation(paper_db_path, account_id: str, reason: str = "人工回滚",
                        approved_by: str = "human", confirmed: bool = False):
    """回滚单一策略的资金分摊覆盖到 apply 前的值（可能为“无覆盖”）。"""
    adversarial.require_human_confirmation(approved_by, confirmed=confirmed)
    account_id = str(account_id or "")
    paper = _paper_connect(paper_db_path)
    try:
        row = paper.execute(
            "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise ValueError("未知策略账户")
        params = _loads(row["params"], {}) or {}
        current = params.get("adaptive_allocation")
        if not current:
            raise ValueError("该账户没有可回滚的资金分摊覆盖")
        previous = params.get("adaptive_allocation_previous")
        paper.close()
    except Exception:
        paper.close()
        raise
    _restore_allocation_snapshot(paper_db_path, {account_id: previous})
    return {"rolled_back": True, "account_id": account_id,
            "restored_previous": previous is not None, "reason": str(reason)[:300]}


def allocation_overview(adaptive_connect, paper_db_path):
    """供 overview/API 展示：当前生效的分摊覆盖 + 最新可应用决策。"""
    active = {}
    try:
        paper = _paper_connect(paper_db_path)
        rows = paper.execute(
            "SELECT id,params FROM paper_accounts"
        ).fetchall()
        paper.close()
        for row in rows:
            params = _loads(row["params"], {}) or {}
            alloc = params.get("adaptive_allocation") or {}
            if alloc.get("status") == "active":
                active[row["id"]] = alloc
    except sqlite3.Error:
        active = {}
    latest = None
    with adaptive_connect() as conn:
        row = conn.execute(
            "SELECT id,decision_date,stage,status,weights,updated_at FROM adaptive_decisions "
            "ORDER BY decision_date DESC,id DESC LIMIT 1"
        ).fetchone()
        if row:
            updated = _aware(row["updated_at"])
            fresh = bool(updated and (dt.datetime.now(TZ) - updated).total_seconds() <= _APPLY_MAX_AGE_SECONDS)
            latest = {
                "id": row["id"], "decision_date": row["decision_date"],
                "stage": row["stage"], "status": row["status"],
                "weights": _loads(row["weights"], {}) or {},
                "fresh": fresh,
                "can_apply": bool(fresh and row["status"] != "applied"
                                  and row["stage"] in _ALLOCATION_APPLY_STAGES
                                  and row["id"] not in {v.get("decision_id") for v in active.values()}),
            }
    return {"active": active, "latest_decision": latest}


# ---------------------------------------------------------------------------
# A3：双AI共识提案 → 选股因子权重覆盖
# ---------------------------------------------------------------------------

def apply_tuner_proposals(adaptive_connect, paper_db_path, run_id: int,
                          approved_by: str = "human", confirmed: bool = False,
                          base_weights_fn=None,
                          max_weight_delta=None, max_entry_delta=None):
    """把一条已达成共识的双AI调参运行写入选股覆盖（人工批准）。

    写入通道与选股进化候选相同：``paper_accounts.params.adaptive_selection``
    + ``adaptive_selection_meta``，由 ``paper_trading._adaptive_selection``
    消费。若账户已有生效的选股进化覆盖则拒绝（避免静默互相覆盖）。
    """
    actor = adversarial.require_human_confirmation(approved_by, confirmed=confirmed)
    run_id = int(run_id)
    with adaptive_connect() as conn:
        row = conn.execute(
            "SELECT * FROM dual_ai_tuning_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError("调参运行不存在")
        if str(row["status"] or "") != "consensus":
            raise ValueError("只有 consensus 状态的运行可以应用")
        applied_ids = _loads(row["applied_ids"], None)
        if applied_ids:
            raise ValueError("该运行已应用过，请先回滚或选择新的运行")
        merged = _loads(row["merged_proposals"], []) or []
        if not merged:
            raise ValueError("该运行没有可应用的共识提案")
        created = _aware(row["created_at"])
        now = dt.datetime.now(TZ)
        if created is None or (now - created).total_seconds() > _APPLY_MAX_AGE_SECONDS:
            raise ValueError("调参运行已超过30分钟有效期，请重新运行调参")
        try:
            import self_evolution as _SE
            _SE.ensure_schema(conn)
            evolution = _SE.get_current_params(conn).get("params") or {}
        except sqlite3.Error:
            evolution = {}
    # B 批观察期门禁：提案目标账户的上一 tuner 覆盖仍在观察期即拒绝。
    import evolution_validation as validation
    for proposal in merged:
        validation.pre_apply_gate(
            adaptive_connect, paper_db_path,
            str(proposal.get("account_id") or ""), "tuner")
    max_weight_delta = float(
        max_weight_delta if max_weight_delta is not None
        else evolution.get("max_weight_delta", 0.03)
    )
    max_entry_delta = float(
        max_entry_delta if max_entry_delta is not None
        else evolution.get("max_delta_threshold", 0.005)
    )

    import strategy_registry as registry
    known_accounts = registry.labels()

    effective_date = dt.datetime.now(TZ).date().isoformat()
    applied_accounts = []
    paper = _paper_connect(paper_db_path)
    try:
        paper.execute("BEGIN")
        for proposal in merged:
            account_id = str(proposal.get("account_id") or "")
            if account_id not in known_accounts:
                raise ValueError(f"提案包含未知账户 {account_id}，拒绝应用")
            row = paper.execute(
                "SELECT id,params,source_strategy FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"模拟盘账户 {account_id} 不存在，拒绝应用")
            params = _account_params(row)
            meta = params.get("adaptive_selection_meta") or {}
            if params.get("adaptive_selection") and str(meta.get("status") or "") == "active":
                raise ValueError(
                    f"账户 {account_id} 已有生效的选股进化覆盖（{meta.get('version')}），"
                    "请先回滚该覆盖再应用 AI 提案"
                )
            if base_weights_fn is not None:
                base_weights = dict(base_weights_fn(account_id) or {})
            else:
                base_weights = _baseline_weights(_account_params(row), row["source_strategy"])
            new_weights = {k: float(v) for k, v in (proposal.get("weights") or {}).items()}
            if not base_weights or set(new_weights) != set(base_weights):
                raise ValueError(
                    f"账户 {account_id} 提案权重因子与当前因子集不一致，拒绝应用"
                )
            for factor, value in new_weights.items():
                if abs(value - float(base_weights.get(factor, 0.0))) > max_weight_delta + 1e-6:
                    raise ValueError(
                        f"账户 {account_id} 因子 {factor} 变化超过 ±{max_weight_delta:.3f}，拒绝应用"
                    )
            entry_delta = float(proposal.get("entry_score_delta") or 0.0)
            if abs(entry_delta) > max_entry_delta + 1e-9:
                raise ValueError(
                    f"账户 {account_id} 入场阈值变化超过 ±{max_entry_delta:.4f}，拒绝应用"
                )
            conditions_raw = proposal.get("conditions") or {}
            skipped_conditions = []
            conditions = {}
            for key, value in conditions_raw.items():
                if key == "enabled":
                    continue
                base_val = _baseline_conditions(params).get(key)
                if base_val is None:
                    # 无基线条件的提案增量一律跳过（不猜基线、不静默放宽）。
                    skipped_conditions.append(key)
                    continue
                conditions[key] = float(value)
            params["adaptive_selection_previous"] = {
                "overlay": params.get("adaptive_selection"),
                "meta": meta,
            }
            params["adaptive_selection"] = {
                "weights": new_weights,
                "conditions": conditions,
                "entry_score_delta": max(-0.02, min(0.03, entry_delta)),
            }
            params["adaptive_selection_meta"] = {
                "version": f"llm-tuner-{run_id}",
                "tier": "llm_consensus",
                "status": "active",
                "effective_date": effective_date,
                "applied_at": _now(),
                "approved_by": actor,
                "run_id": run_id,
            }
            _write_account_params(paper, account_id, params)
            _audit(paper, account_id, "adaptive_tuner_applied",
                   {"run_id": run_id, "version": f"llm-tuner-{run_id}",
                    "skipped_unknown_conditions": skipped_conditions})
            applied_accounts.append(account_id)
        paper.commit()
    except Exception:
        paper.rollback()
        paper.close()
        raise
    try:
        with adaptive_connect() as conn:
            conn.execute(
                "UPDATE dual_ai_tuning_runs SET applied_ids=? WHERE id=?",
                (_json(applied_accounts), run_id),
            )
            try:
                import self_evolution as _SE
                _SE.ensure_schema(conn)
                conn.execute(
                    "UPDATE evolution_tracking SET applied=1,applied_count=? "
                    "WHERE run_id=?", (len(applied_accounts), run_id),
                )
            except sqlite3.Error:
                pass
    except Exception:
        _restore_tuner_snapshots(paper_db_path, run_id, applied_accounts)
        paper.close()
        raise
    paper.close()
    return {"applied": True, "run_id": run_id, "accounts": applied_accounts,
            "effective_date": effective_date, "approved_by": actor}


def _restore_tuner_snapshots(paper_db_path, run_id, account_ids):
    paper = _paper_connect(paper_db_path)
    try:
        paper.execute("BEGIN")
        for account_id in account_ids:
            row = paper.execute(
                "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                continue
            params = _loads(row["params"], {}) or {}
            previous = params.get("adaptive_selection_previous") or {}
            if previous.get("overlay"):
                params["adaptive_selection"] = previous["overlay"]
                params["adaptive_selection_meta"] = previous.get("meta") or {}
            else:
                params.pop("adaptive_selection", None)
                params.pop("adaptive_selection_meta", None)
            params.pop("adaptive_selection_previous", None)
            _write_account_params(paper, account_id, params)
            _audit(paper, account_id, "adaptive_tuner_rolled_back", {"run_id": run_id})
        paper.commit()
    finally:
        paper.close()


def rollback_tuner_overlay(adaptive_connect, paper_db_path, account_id: str,
                           reason: str = "人工回滚",
                           approved_by: str = "human", confirmed: bool = False):
    """回滚单一策略的 llm-tuner 选股覆盖到 apply 前的状态。"""
    adversarial.require_human_confirmation(approved_by, confirmed=confirmed)
    account_id = str(account_id or "")
    paper = _paper_connect(paper_db_path)
    try:
        row = paper.execute(
            "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise ValueError("未知策略账户")
        params = _loads(row["params"], {}) or {}
        meta = params.get("adaptive_selection_meta") or {}
        if str(meta.get("tier") or "") != "llm_consensus":
            raise ValueError("该账户当前覆盖不是 AI 调参版本，请使用选股进化的回滚入口")
        if not params.get("adaptive_selection_previous"):
            raise ValueError("缺少 apply 前的快照，无法回滚")
        run_id = meta.get("run_id")
        paper.close()
    except Exception:
        paper.close()
        raise
    _restore_tuner_snapshots(paper_db_path, run_id, [account_id])
    if run_id:
        try:
            with adaptive_connect() as conn:
                row = conn.execute(
                    "SELECT applied_ids FROM dual_ai_tuning_runs WHERE id=?", (run_id,)
                ).fetchone()
                if row:
                    remaining = [a for a in (_loads(row["applied_ids"], []) or [])
                                 if a != account_id]
                    conn.execute(
                        "UPDATE dual_ai_tuning_runs SET applied_ids=? WHERE id=?",
                        (_json(remaining) if remaining else None, run_id),
                    )
        except sqlite3.Error:
            pass
    return {"rolled_back": True, "account_id": account_id, "run_id": run_id,
            "reason": str(reason)[:300]}
