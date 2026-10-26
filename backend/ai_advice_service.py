# -*- coding: utf-8 -*-
"""入场建议的消费与归因（方案 §6.1/§6.3，S3 的第一项）。

这个模块回答一句话：**这次决策和纯规则相比，差在哪、为什么差**。

三条不可越过的边界：

1. AI 意见**不能把 rejected 改成 approved**。``prefer_entry`` 只能在已经通过硬门禁的
   候选上起作用；原因码 ``unknown`` 一律按硬约束处理（不解析 AI 文案去猜软硬）。
2. ADVISORY 模式下 ``final_decision`` 必须等于 ``rules_only_decision``。差别只体现在
   审计行里，不体现在下单上。
3. 意见**只消费一次**。同一份意见被重复消费要拒绝，而不是"再缩放一次"。

有界自动采用不在这个模块里：它必须先过 :mod:`ai_capability` 的凭据校验，
而那个校验在没有凭据时必然拒绝。所以本模块上线后行为仍是纯规则。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json

AUDIT_SCHEMA_VERSION = "entry-decision-audit-v1"

MODE_OFF = "OFF"
MODE_ADVISORY = "ADVISORY"
MODE_BOUNDED_AUTO = "BOUNDED_AUTO"

#: 影响"进不进场"的动作。其余动作（持仓/退出/分配）不属于本模块。
ENTRY_ACTIONS = ("skip_entry", "wait_entry", "prefer_entry", "reduce_entry")

#: 平台维护的原因分类合同。**不解析 AI 文案**：认不出的一律当硬约束。
REASON_CLASS_HARD = "hard"
REASON_CLASS_SOFT = "soft_experimentable"
REASON_CLASS_UNKNOWN = "unknown"

HARD_REASON_CODES = frozenset({
    "insufficient_cash", "t1_not_sellable", "suspended", "price_limit_locked",
    "quote_missing", "missing_reference_price", "stale_quote", "max_positions_reached",
    "industry_cap", "single_position_cap", "risk_rejected", "untradable_security",
})
SOFT_REASON_CODES = frozenset({
    "score_below_threshold", "soft_score_gate", "entry_score_delta",
    "condition_threshold", "weak_structure",
})

R_NO_OPINION = "no_ai_opinion"
R_OPINION_EXPIRED = "ai_opinion_expired"
R_OPINION_ACTION_OUT_OF_SCOPE = "ai_action_out_of_scope"
R_IDENTITY_MISMATCH = "ai_opinion_identity_mismatch"
R_ALREADY_CONSUMED = "ai_opinion_already_consumed"
R_HARD_CONSTRAINT = "hard_constraint_blocks_ai_effect"
R_MODE_IS_ADVISORY = "mode_is_advisory"
R_MODE_OFF = "mode_is_off"


class AdviceRejected(ValueError):
    """意见不参与决策。整条忽略并记原因，不猜测意图。"""


def classify_reason(code: str) -> str:
    """平台维护的分类合同：硬 / 可实验软 / 未知（按硬处理）。"""
    text = str(code or "").strip()
    if text in HARD_REASON_CODES:
        return REASON_CLASS_HARD
    if text in SOFT_REASON_CODES:
        return REASON_CLASS_SOFT
    return REASON_CLASS_UNKNOWN


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _instant(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


@dataclasses.dataclass(frozen=True, slots=True)
class RuleDecision:
    """规则侧已经算完的结论。AI 不能在这里改主意。"""

    code: str
    approved: bool
    reason_codes: tuple[str, ...] = ()

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(item for item in self.reason_codes if item)

    def has_hard_block(self) -> bool:
        return any(classify_reason(item) != REASON_CLASS_SOFT
                   for item in self.blocking_reasons)


@dataclasses.dataclass(frozen=True, slots=True)
class Opinion:
    """从意见台账读出来的一份 AI 意见。"""

    recommendation_id: str
    action: str
    valid_until: str | None
    exact_identity: dict
    reason: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class EntryDecisionAudit:
    code: str
    mode: str
    rules_only_decision: dict
    ai_opinion: dict | None
    final_decision: dict
    effect_reason: str
    consumed_recommendation_id: str | None
    audit_fingerprint: str
    audit_schema_version: str = AUDIT_SCHEMA_VERSION

    def projection(self) -> dict:
        return {"audit_schema_version": self.audit_schema_version,
                "code": self.code, "mode": self.mode,
                "rules_only_decision": self.rules_only_decision,
                "ai_opinion": self.ai_opinion,
                "final_decision": self.final_decision,
                "effect_reason": self.effect_reason,
                "consumed_recommendation_id": self.consumed_recommendation_id,
                "audit_fingerprint": self.audit_fingerprint}


def compose_entry_audit(
    *, code: str, mode: str, rule: RuleDecision, opinion: Opinion | None,
    expected_identity: dict, consumed: frozenset[str] = frozenset(), now: str | None = None,
) -> EntryDecisionAudit:
    """决定这份意见能不能影响最终动作，并留下可追溯的审计行。

    无论结论如何，``final_decision`` 在 OFF/ADVISORY 下都等于规则结论。
    """
    if mode not in (MODE_OFF, MODE_ADVISORY, MODE_BOUNDED_AUTO):
        raise AdviceRejected(f"unknown_mode:{mode}")
    rules_only = {"approved": bool(rule.approved),
                  "reasons": list(rule.blocking_reasons)}
    effect_reason = R_NO_OPINION
    effective_action = None
    consumed_id = None

    if opinion is not None:
        # 先判这份意见本身是否合格，再谈模式：过期/身份不符/越界是**运维要看见的问题**，
        # 不能因为"当前是 ADVISORY"就被一句泛泛的原因盖掉。
        if opinion.recommendation_id in consumed:
            effect_reason = R_ALREADY_CONSUMED
        elif dict(opinion.exact_identity or {}) != dict(expected_identity or {}):
            effect_reason = R_IDENTITY_MISMATCH
        elif opinion.action not in ENTRY_ACTIONS:
            effect_reason = R_OPINION_ACTION_OUT_OF_SCOPE
        else:
            expiry = _instant(opinion.valid_until) if opinion.valid_until else None
            moment = _instant(now) if now else None
            if expiry is not None and moment is not None and expiry <= moment:
                effect_reason = R_OPINION_EXPIRED
            elif rule.has_hard_block():
                # 有硬拦截时任何"想进场"的意见都不生效。
                effect_reason = R_HARD_CONSTRAINT
            elif mode == MODE_OFF:
                effect_reason = R_MODE_OFF
            elif mode == MODE_ADVISORY:
                effect_reason = R_MODE_IS_ADVISORY
            else:
                effect_reason = "ai_opinion_considered"
                effective_action = opinion.action
                consumed_id = opinion.recommendation_id

    if mode == MODE_BOUNDED_AUTO and effective_action == "prefer_entry" and not rule.approved:
        # 规则没过硬门禁：AI 不能把它改成 approved。这是最容易被误实现的一条。
        effect_reason = R_HARD_CONSTRAINT
        effective_action = None
        consumed_id = None

    final = dict(rules_only)
    if mode in (MODE_OFF, MODE_ADVISORY):
        # 显式再钉一次：这两种模式下最终动作必须与规则完全一致。
        final = dict(rules_only)

    material = {"audit_schema_version": AUDIT_SCHEMA_VERSION, "code": code,
                "mode": mode, "rules_only_decision": rules_only,
                "ai_opinion": None if opinion is None else {
                    "recommendation_id": opinion.recommendation_id,
                    "action": opinion.action},
                "final_decision": final, "effect_reason": effect_reason,
                "consumed_recommendation_id": consumed_id,
                "suggested_action": effective_action}
    return EntryDecisionAudit(
        code=str(code), mode=str(mode), rules_only_decision=rules_only,
        ai_opinion=None if opinion is None else {
            "recommendation_id": opinion.recommendation_id, "action": opinion.action,
            "valid_until": opinion.valid_until},
        final_decision=final, effect_reason=effect_reason,
        consumed_recommendation_id=consumed_id, audit_fingerprint=fingerprint(material))


def consumption_key(audit: EntryDecisionAudit, decision_id: str) -> str | None:
    """这份意见被哪次决策消费。``None`` 表示没有消费。"""
    if audit.consumed_recommendation_id is None:
        return None
    return f"{audit.consumed_recommendation_id}@{decision_id}"


# --------------------------------------------------------------------------- #
# 持仓与退出
# --------------------------------------------------------------------------- #

POSITION_ACTIONS = ("hold_position", "trim_position", "request_exit", "wait_soft_exit")

#: 这些是"系统必须退出"的理由：任何 AI 意见都不能压住它们。
HARD_EXIT_REASON_CODES = frozenset({
    "hard_stop", "system_drawdown", "risk_exit", "untradable", "delisting_risk",
    "position_risk_state_exit", "cycle_end",
})
#: 这些是"可以实验的软退出"：AI 才可能建议延后。
SOFT_EXIT_REASON_CODES = frozenset({
    "take_profit_target", "time_based_exit", "momentum_fade", "soft_trailing_stop",
})

R_HARD_EXIT_WINS = "hard_exit_cannot_be_deferred"
R_SOFT_EXIT_ONLY = "wait_soft_exit_only_applies_to_soft_exits"


def classify_exit_reason(code: str) -> str:
    text = str(code or "").strip()
    if text in HARD_EXIT_REASON_CODES:
        return REASON_CLASS_HARD
    if text in SOFT_EXIT_REASON_CODES:
        return REASON_CLASS_SOFT
    return REASON_CLASS_UNKNOWN


@dataclasses.dataclass(frozen=True, slots=True)
class ExitDecision:
    """规则侧已经算完的退出结论。"""

    code: str
    should_exit: bool
    reason_codes: tuple[str, ...] = ()

    def has_hard_exit(self) -> bool:
        # 认不出的退出理由按硬处理：宁可提前退出，也不让 AI 压住一条不认识的止损。
        return any(classify_exit_reason(item) != REASON_CLASS_SOFT
                   for item in self.reason_codes)


def compose_exit_audit(
    *, code: str, mode: str, rule: ExitDecision, opinion: Opinion | None,
    expected_identity: dict, consumed: frozenset[str] = frozenset(), now: str | None = None,
) -> EntryDecisionAudit:
    """退出建议的归因。硬退出优先于一切 AI 意见，包括"再拿一会儿"。"""
    if mode not in (MODE_OFF, MODE_ADVISORY, MODE_BOUNDED_AUTO):
        raise AdviceRejected(f"unknown_mode:{mode}")
    rules_only = {"should_exit": bool(rule.should_exit),
                  "reasons": list(rule.reason_codes)}
    effect_reason = R_NO_OPINION
    consumed_id = None
    suggested = None

    if opinion is not None:
        if opinion.recommendation_id in consumed:
            effect_reason = R_ALREADY_CONSUMED
        elif dict(opinion.exact_identity or {}) != dict(expected_identity or {}):
            effect_reason = R_IDENTITY_MISMATCH
        elif opinion.action not in POSITION_ACTIONS:
            effect_reason = R_OPINION_ACTION_OUT_OF_SCOPE
        elif rule.has_hard_exit() and opinion.action in ("hold_position", "wait_soft_exit"):
            # 最容易被误实现的一条：AI 说"再等等"不能压住硬止损。
            effect_reason = R_HARD_EXIT_WINS
        elif opinion.action == "wait_soft_exit" and not rule.should_exit:
            effect_reason = R_SOFT_EXIT_ONLY
        else:
            expiry = _instant(opinion.valid_until) if opinion.valid_until else None
            moment = _instant(now) if now else None
            if expiry is not None and moment is not None and expiry <= moment:
                effect_reason = R_OPINION_EXPIRED
            elif mode == MODE_OFF:
                effect_reason = R_MODE_OFF
            elif mode == MODE_ADVISORY:
                effect_reason = R_MODE_IS_ADVISORY
            else:
                effect_reason = "ai_opinion_considered"
                consumed_id = opinion.recommendation_id
                suggested = opinion.action

    final = dict(rules_only)
    if mode in (MODE_OFF, MODE_ADVISORY):
        final = dict(rules_only)

    material = {"audit_schema_version": AUDIT_SCHEMA_VERSION, "kind": "exit",
                "code": code, "mode": mode, "rules_only_decision": rules_only,
                "ai_opinion": None if opinion is None else {
                    "recommendation_id": opinion.recommendation_id,
                    "action": opinion.action},
                "final_decision": final, "effect_reason": effect_reason,
                "consumed_recommendation_id": consumed_id, "suggested_action": suggested}
    return EntryDecisionAudit(
        code=str(code), mode=str(mode), rules_only_decision=rules_only,
        ai_opinion=None if opinion is None else {
            "recommendation_id": opinion.recommendation_id, "action": opinion.action,
            "valid_until": opinion.valid_until},
        final_decision=final, effect_reason=effect_reason,
        consumed_recommendation_id=consumed_id, audit_fingerprint=fingerprint(material))


# --------------------------------------------------------------------------- #
# 组合分配
# --------------------------------------------------------------------------- #

R_ALLOCATION_UNKNOWN_STRATEGY = "allocation_unknown_strategy"
R_ALLOCATION_INFEASIBLE = "allocation_infeasible_within_caps"
R_ALLOCATION_NOT_ALL_POSITIVE = "allocation_needs_positive_targets"


@dataclasses.dataclass(frozen=True, slots=True)
class AllocationProposal:
    """AI 提出的目标比例（百分数）。股数与金额一律由平台计算。"""

    ratios: tuple[tuple[str, float], ...]
    note: str = ""


def normalize_allocation(
    proposal: AllocationProposal, *, participants, min_pct: float, max_pct: float,
) -> dict[str, float]:
    """把 AI 的目标比例归一化成"恰好 100%、每项在上下限内"。

    归一化用的是既有的 ``strategy_weight_scope.project_weights``：它本来就是这件事的
    唯一权威。不可行就拒绝，不悄悄夹取（那是另一种越权）。
    """
    import strategy_weight_scope as SWS

    ids = tuple(str(item) for item in participants or ())
    if not ids:
        raise AdviceRejected("no_participant")
    ratios = dict(proposal.ratios or ())
    unknown = sorted(set(ratios) - set(ids))
    if unknown:
        raise AdviceRejected(f"{R_ALLOCATION_UNKNOWN_STRATEGY}:{','.join(unknown)}")
    if any(float(value) < 0 for value in ratios.values()):
        raise AdviceRejected(R_ALLOCATION_NOT_ALL_POSITIVE)
    if not any(float(value) > 0 for value in ratios.values()):
        raise AdviceRejected(R_ALLOCATION_NOT_ALL_POSITIVE)
    try:
        projected = SWS.project_weights(ratios, ids, min_pct=min_pct, max_pct=max_pct)
    except ValueError as exc:
        raise AdviceRejected(f"{R_ALLOCATION_INFEASIBLE}:{exc}") from exc
    return {str(key): float(value) for key, value in projected.items()}


def compose_allocation_audit(*, cycle_id: int, mode: str, current: dict,
                             proposed: dict, participants, min_pct: float,
                             max_pct: float, opinion: Opinion | None,
                             expected_identity: dict, now: str | None = None) -> EntryDecisionAudit:
    """组合意见只生成解释；ADVISORY 下比例与既有 owner 完全一致。"""
    import math

    rules_only = {str(k): float(v) for k, v in (current or {}).items()}
    final = dict(rules_only)
    effect_reason = R_NO_OPINION
    if opinion is not None:
        if dict(opinion.exact_identity or {}) != dict(expected_identity or {}):
            effect_reason = R_IDENTITY_MISMATCH
        elif opinion.action != "propose_allocation":
            effect_reason = R_OPINION_ACTION_OUT_OF_SCOPE
        elif mode == MODE_OFF:
            effect_reason = R_MODE_OFF
        elif mode == MODE_ADVISORY:
            effect_reason = R_MODE_IS_ADVISORY
        elif mode == MODE_BOUNDED_AUTO:
            try:
                values = dict(proposed or {})
                if len(values) != len(set(participants)) or set(values) != set(participants):
                    raise AdviceRejected(R_ALLOCATION_UNKNOWN_STRATEGY)
                if any(not math.isfinite(float(v)) or float(v) < 0 for v in values.values()):
                    raise AdviceRejected(R_ALLOCATION_NOT_ALL_POSITIVE)
                final = normalize_allocation(AllocationProposal(ratios=tuple(values.items())),
                                             participants=participants, min_pct=min_pct,
                                             max_pct=max_pct)
                effect_reason = "ai_opinion_considered"
            except AdviceRejected as exc:
                effect_reason = str(exc)
                final = dict(rules_only)
        else:
            raise AdviceRejected(f"unknown_mode:{mode}")
    material = {"kind": "allocation", "cycle_id": int(cycle_id), "mode": mode,
                "rules_only_decision": rules_only, "final_decision": final,
                "opinion": None if opinion is None else opinion.recommendation_id,
                "effect_reason": effect_reason}
    return EntryDecisionAudit(
        code=f"cycle:{int(cycle_id)}", mode=mode, rules_only_decision=rules_only,
        ai_opinion=None if opinion is None else {
            "recommendation_id": opinion.recommendation_id, "action": opinion.action,
            "valid_until": opinion.valid_until},
        final_decision=final, effect_reason=effect_reason,
        consumed_recommendation_id=(opinion.recommendation_id
                                    if effect_reason == "ai_opinion_considered" else None),
        audit_fingerprint=fingerprint(material))


# --------------------------------------------------------------------------- #
# 生产消费：读取研究库意见，在账本事务内追加归因
# --------------------------------------------------------------------------- #

PRODUCTION_MODE = MODE_ADVISORY
R_PRODUCTION_UNAVAILABLE = "ai_advice_unavailable"


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone() is not None


def latest_opinion(conn, *, kind: str, strategy_id: str, exact_identity: dict,
                   code: str | None = None) -> Opinion | None:
    """只读连接研究库。精确身份不匹配的意见由 compose 记录后忽略。"""

    if not _has_table(conn, "ai_recommendations"):
        return None
    actions = ENTRY_ACTIONS if kind == "entry" else POSITION_ACTIONS if kind == "exit" \
        else ("propose_allocation",)
    marks = ",".join("?" for _ in actions)
    if kind == "allocation":
        # 组合意见的身份是 (cycle_id, policy_id)，没有策略名：取最近一条，
        # 身份是否匹配交给 compose_allocation_audit 记录，而不是静默丢弃。
        sql = (f"SELECT * FROM ai_recommendations WHERE action IN ({marks})"
               " ORDER BY created_at DESC LIMIT 1")
        params = tuple(actions)
    else:
        sql = (f"SELECT * FROM ai_recommendations WHERE strategy_id=? AND action IN ({marks})"
               " ORDER BY created_at DESC LIMIT 1")
        params = (str(strategy_id), *actions)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
        identity = json.loads(row["exact_identity"] or "{}")
    except (TypeError, ValueError):
        return None
    if code is not None and payload.get("code") not in (None, code):
        return None
    return Opinion(recommendation_id=row["recommendation_id"], action=row["action"],
                   valid_until=row["valid_until"], exact_identity=identity,
                   reason=row["reason"])


def consume_safe(ledger_conn, *, kind: str, decision_id: str, strategy_id: str,
                 code: str, rule, exact_identity: dict, queue_path=None,
                 current=None, proposed=None, participants=(), min_pct=5.0,
                 max_pct=60.0, cycle_id=None, now=None) -> dict:
    """实际生产消费点：ADVISORY 审计失败只记错误，不修改任何规则结果。"""
    import agent_task_queue as AQ

    try:
        with AQ.connect_readonly(queue_path) as research:
            opinion = latest_opinion(research, kind=kind, strategy_id=strategy_id,
                                     exact_identity=exact_identity, code=code)
        if kind == "entry":
            audit = compose_entry_audit(
                code=code, mode=PRODUCTION_MODE, rule=rule, opinion=opinion,
                expected_identity=exact_identity, now=now)
        elif kind == "exit":
            audit = compose_exit_audit(
                code=code, mode=PRODUCTION_MODE, rule=rule, opinion=opinion,
                expected_identity=exact_identity, now=now)
        elif kind == "allocation":
            audit = compose_allocation_audit(
                cycle_id=int(cycle_id), mode=PRODUCTION_MODE, current=current,
                proposed=proposed, participants=participants, min_pct=min_pct,
                max_pct=max_pct, opinion=opinion, expected_identity=exact_identity,
                now=now)
        else:
            raise AdviceRejected(f"unknown_advice_kind:{kind}")
        record_audit(ledger_conn, audit, decision_id=str(decision_id),
                     strategy_id=strategy_id, now=now)
        return {"status": "recorded", "audit": audit.projection(),
                "final_decision": audit.final_decision}
    except Exception as exc:  # noqa: BLE001 - AI 不得影响交易规则
        error = f"{type(exc).__name__}: {exc}"
        try:
            import paper_repository as R
            R.audit(ledger_conn, None, "ai_advice_consumption_failed", _canonical({
                "kind": kind, "decision_id": str(decision_id),
                "strategy_id": strategy_id, "code": code, "error": error}))
        except Exception:
            pass
        return {"status": "failed", "reason": R_PRODUCTION_UNAVAILABLE,
                "error": error, "final_decision": current if kind == "allocation" else None}


def consume_risk_decision_safe(ledger_conn, *, account_id, code, side, decision,
                               strategy_stamp, queue_path=None, now=None) -> dict:
    """交易 owner 的唯一接线：风险决定已经落库，这里只追加 ADVISORY 归因。

    必须在 ``paper_risk_decisions`` 的 INSERT 之后立刻调用 —— ``decision_id``
    就用那一行的自增 id。规则结论（``decision``）原样作为理由码，不再按文案
    猜它是"硬止损"还是"止盈"。
    """
    try:
        insert_id = ledger_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        strategy_id, strategy_version, strategy_checksum = tuple(strategy_stamp or (None,) * 3)
        identity = {"strategy_id": strategy_id, "strategy_version": strategy_version,
                    "strategy_checksum": strategy_checksum}
        rule_code = str(decision)
        if str(side) == "buy":
            approved = "approved" in rule_code
            # 通过的决定没有"拦截理由"：把决定名当理由码会把它误判成硬拦截。
            rule = RuleDecision(code=str(code), approved=approved,
                                reason_codes=() if approved else (rule_code,))
            kind = "entry"
        else:
            rule = ExitDecision(code=str(code), should_exit=True, reason_codes=(rule_code,))
            kind = "exit"
        return consume_safe(ledger_conn, kind=kind, decision_id=f"risk:{int(insert_id)}",
                            strategy_id=str(strategy_id or account_id), code=str(code),
                            rule=rule, exact_identity=identity, queue_path=queue_path,
                            now=now)
    except Exception as exc:  # noqa: BLE001 - 意见链绝不改变规则交易结论
        error = f"{type(exc).__name__}: {exc}"
        try:
            import paper_repository as R
            R.audit(ledger_conn, None, "ai_advice_consumption_failed", _canonical({
                "account_id": account_id, "code": code, "side": side,
                "decision": decision, "error": error}))
        except Exception:
            pass
        return {"status": "failed", "reason": R_PRODUCTION_UNAVAILABLE, "error": error}


# --------------------------------------------------------------------------- #
# 落库与复盘归因
# --------------------------------------------------------------------------- #

AUDIT_TABLE = "ai_decision_audits"


class AuditStoreError(ValueError):
    pass


def record_audit(conn, audit: EntryDecisionAudit, *, decision_id: str,
                 strategy_id: str | None = None, now=None) -> dict:
    """把一次归因写成一条**追加式**审计行。

    同一 ``(decision_id, audit_fingerprint)`` 重复写入返回既有行（幂等），不复制。
    本函数只写解释表；正式决策仍由原职责模块落账。
    """
    import sqlite3

    if not str(decision_id or "").strip():
        raise AuditStoreError("decision_id_required")
    if not _has_table(conn, AUDIT_TABLE):
        # 读路径可以容忍旧库，写路径不行：说清楚要跑哪个迁移。
        raise AuditStoreError(f"{AUDIT_TABLE}_missing: run migration v40")
    row = conn.execute(
        "SELECT * FROM ai_decision_audits WHERE decision_id=? AND audit_fingerprint=?",
        (str(decision_id), audit.audit_fingerprint)).fetchone()
    if row is not None:
        return dict(row)
    stamp = now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    audit_id = fingerprint({"decision_id": str(decision_id),
                            "audit_fingerprint": audit.audit_fingerprint})
    payload = {
        "audit_id": audit_id, "decision_id": str(decision_id), "code": audit.code,
        "strategy_id": strategy_id, "mode": audit.mode,
        "rules_only_decision": _canonical(audit.rules_only_decision),
        "ai_opinion": None if audit.ai_opinion is None else _canonical(audit.ai_opinion),
        "final_decision": _canonical(audit.final_decision),
        "effect_reason": audit.effect_reason,
        "consumed_recommendation_id": audit.consumed_recommendation_id,
        "audit_fingerprint": audit.audit_fingerprint,
        "audit_schema_version": audit.audit_schema_version,
        "created_at": str(stamp),
    }
    try:
        conn.execute(
            "INSERT INTO ai_decision_audits (audit_id, decision_id, code, strategy_id,"
            " mode, rules_only_decision, ai_opinion, final_decision, effect_reason,"
            " consumed_recommendation_id, audit_fingerprint, audit_schema_version,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["audit_id"], payload["decision_id"], payload["code"],
             payload["strategy_id"], payload["mode"], payload["rules_only_decision"],
             payload["ai_opinion"], payload["final_decision"],
             payload["effect_reason"], payload["consumed_recommendation_id"],
             payload["audit_fingerprint"], payload["audit_schema_version"],
             payload["created_at"]),
        )
    except sqlite3.IntegrityError:
        # 并发写同一条：读回既有行，不覆盖。
        row = conn.execute(
            "SELECT * FROM ai_decision_audits WHERE decision_id=? AND audit_fingerprint=?",
            (str(decision_id), audit.audit_fingerprint)).fetchone()
        if row is None:
            raise
        return dict(row)
    return payload


def audits_for_decision(conn, decision_id: str) -> list[dict]:
    if not _has_table(conn, AUDIT_TABLE):
        return []
    rows = conn.execute(
        "SELECT * FROM ai_decision_audits WHERE decision_id=? ORDER BY created_at",
        (str(decision_id),)).fetchall()
    return [dict(row) for row in rows]


def attribution_summary(conn, *, strategy_id: str | None = None,
                        limit: int = 5000) -> dict:
    """复盘归因：这段决策里 AI 到底改变了什么、没改变什么、为什么。

    刻意不计算"AI 让收益提高了多少"——那需要成对实验，不是审计行能回答的。
    """
    if not _has_table(conn, AUDIT_TABLE):
        return {"audits": 0, "by_effect_reason": {}, "by_mode": {},
                "opinions_consumed": 0, "decisions_changed_by_ai": 0,
                "note": f"{AUDIT_TABLE} 尚未建立（需 migration v40）；不是\"没有差异\""}
    sql = ("SELECT mode, effect_reason, consumed_recommendation_id, final_decision,"
           " rules_only_decision FROM ai_decision_audits")
    params: list = []
    if strategy_id is not None:
        sql += " WHERE strategy_id=?"
        params.append(str(strategy_id))
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    by_reason: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    consumed = 0
    changed = 0
    for row in rows:
        reason = str(row["effect_reason"])
        by_reason[reason] = by_reason.get(reason, 0) + 1
        mode = str(row["mode"])
        by_mode[mode] = by_mode.get(mode, 0) + 1
        if row["consumed_recommendation_id"]:
            consumed += 1
        if str(row["final_decision"]) != str(row["rules_only_decision"]):
            changed += 1
    return {
        "audits": total,
        "by_effect_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        "by_mode": dict(sorted(by_mode.items(), key=lambda kv: -kv[1])),
        "opinions_consumed": consumed,
        "decisions_changed_by_ai": changed,
        "note": "本表只解释差异；AI 是否更好要看成对实验，不看这里的计数",
    }
