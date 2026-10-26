# -*- coding: utf-8 -*-
"""有界自动采用的机制层（方案 §5）。机制在，授权不在——默认什么都不自动做。

设计前提（读代码时别弄反）：

- **凭据由平台签发**，不由模型自报。模型输出里出现 ``capability_id`` 一律整条拒绝
  （见 :mod:`ai_recommendation_store`）。
- **采用器独立于研究者**。产出意见的一方不能自己批准自己。
- **默认没有授权**：没有凭据就没有自动采用；有凭据但缺预算字段、过期、被撤销、
  越界、基线变了、证据版本不符，全部拒绝并在拒绝前**不写**任何东西。
- **预算按授权范围累计**，换候选不清零。收紧参数同样消耗变更预算。
- 真正写正式参数的唯一入口仍留给外部（``apply_writer``）。这里不给默认实现：
  没接上就是 ``apply_not_wired``，绝不偷偷落地。

所以这个模块现在的实际效果是：**ADVISORY 行为不变**，但 S4 需要的校验链已经存在，
并且每条都可在测试里复现。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math

CAPABILITY_SCHEMA_VERSION = "ai-capability-v1"
ADOPTER_VERSION = "independent-adopter-v1"

MODE_OFF = "OFF"
MODE_ADVISORY = "ADVISORY"
MODE_BOUNDED_AUTO = "BOUNDED_AUTO"
MODE_REQUIRE_AI = "REQUIRE_AI"
MODES = (MODE_OFF, MODE_ADVISORY, MODE_BOUNDED_AUTO, MODE_REQUIRE_AI)

#: 稳定拒绝原因。展示层负责翻译，代码只认这些字符串。
R_NO_CREDENTIAL = "no_capability_credential"
R_INCOMPLETE_CREDENTIAL = "incomplete_capability_credential"
R_EXPIRED = "capability_expired"
R_REVOKED = "capability_revoked"
R_STRATEGY_OUT_OF_SCOPE = "strategy_out_of_scope"
R_TARGET_KIND_OUT_OF_SCOPE = "target_kind_out_of_scope"
R_FIELD_OUT_OF_SCOPE = "field_out_of_scope"
R_CHANGE_TOO_LARGE = "change_exceeds_per_apply_limit"
R_BUDGET_EXHAUSTED = "daily_change_budget_exhausted"
R_STALE_BASE = "stale_base"
R_EVIDENCE_POLICY_MISMATCH = "evidence_policy_mismatch"
R_APPROVER_IS_PRODUCER = "approver_must_not_be_the_producer"
R_APPLY_NOT_WIRED = "apply_route_not_wired"

#: 凭据必须显式给出这些字段；缺任何一项都不能进入 BOUNDED_AUTO。
REQUIRED_FIELDS = (
    "capability_id", "mode", "strategy_ids", "allowed_target_kinds", "allowed_fields",
    "bounds_version", "evidence_policy_id", "max_change_per_apply",
    "max_change_per_day", "loss_stop_policy_id", "max_trials_per_day",
    "daily_cost_cap", "max_tool_steps_per_task", "expires_at", "revocation_epoch",
)


class AdoptionRefused(ValueError):
    """拒绝采用。``reason`` 是上面那组稳定字符串之一。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = str(reason)
        self.detail = detail


def _finite(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, label) from exc
    if not math.isfinite(number) or number <= 0:
        raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, label)
    return number


def _instant(value, label: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, label) from exc
    if parsed.tzinfo is None:
        raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, f"{label}_must_be_timezone_aware")
    return parsed


@dataclasses.dataclass(frozen=True, slots=True)
class CapabilityCredential:
    """平台签发的授权范围。任何缺项都视为"没有授权"，不补默认值。"""

    capability_id: str
    mode: str
    strategy_ids: tuple[str, ...]
    allowed_target_kinds: tuple[str, ...]
    allowed_fields: tuple[str, ...]
    bounds_version: str
    evidence_policy_id: str
    max_change_per_apply: float
    max_change_per_day: float
    loss_stop_policy_id: str
    max_trials_per_day: int
    daily_cost_cap: float
    max_tool_steps_per_task: int
    expires_at: str
    revocation_epoch: int
    issued_by: str
    schema_version: str = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self):
        missing = []
        for name in REQUIRED_FIELDS:
            value = getattr(self, name, None)
            if value is None or value == "" or value == () or value == []:
                missing.append(name)
        if not str(self.issued_by or "").strip():
            missing.append("issued_by")
        if missing:
            raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, ",".join(sorted(missing)))
        if self.mode not in MODES:
            raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, f"unknown_mode:{self.mode}")
        object.__setattr__(self, "strategy_ids", tuple(str(item) for item in self.strategy_ids))
        object.__setattr__(self, "allowed_target_kinds",
                           tuple(str(item) for item in self.allowed_target_kinds))
        object.__setattr__(self, "allowed_fields",
                           tuple(str(item) for item in self.allowed_fields))
        object.__setattr__(self, "max_change_per_apply",
                           _finite(self.max_change_per_apply, "max_change_per_apply"))
        object.__setattr__(self, "max_change_per_day",
                           _finite(self.max_change_per_day, "max_change_per_day"))
        object.__setattr__(self, "daily_cost_cap",
                           _finite(self.daily_cost_cap, "daily_cost_cap"))
        if int(self.max_trials_per_day) < 1 or int(self.max_tool_steps_per_task) < 1:
            raise AdoptionRefused(R_INCOMPLETE_CREDENTIAL, "limits_must_be_positive")
        object.__setattr__(self, "max_trials_per_day", int(self.max_trials_per_day))
        object.__setattr__(self, "max_tool_steps_per_task",
                           int(self.max_tool_steps_per_task))
        object.__setattr__(self, "revocation_epoch", int(self.revocation_epoch))
        _instant(self.expires_at, "expires_at")

    @property
    def auto_applies(self) -> bool:
        return self.mode == MODE_BOUNDED_AUTO


@dataclasses.dataclass(frozen=True, slots=True)
class BudgetState:
    """授权范围上的累计用量。换候选**不**清零——这正是它要防的漂移。"""

    window_key: str
    changes_used: float = 0.0
    cost_used: float = 0.0
    trials_used: int = 0

    def projection(self) -> dict:
        return {"window_key": self.window_key, "changes_used": self.changes_used,
                "cost_used": self.cost_used, "trials_used": self.trials_used}


@dataclasses.dataclass(frozen=True, slots=True)
class AdoptionRequest:
    """一次采用请求。全部是显式输入，不接受"当前状态"这种隐含来源。"""

    credential: CapabilityCredential | None
    strategy_id: str
    target_kind: str
    field_changes: tuple[tuple[str, float], ...]
    current_baseline: tuple[tuple[str, float], ...]
    requested_baseline: tuple[tuple[str, float], ...]
    evidence_policy_id: str
    revocation_epoch: int
    producer: str
    approver: str
    cost: float = 0.0
    now: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class AdoptionDecision:
    allowed: bool
    reason: str | None
    applied: bool
    credential_id: str | None
    changes: tuple[tuple[str, float], ...]
    budget_after: BudgetState | None
    adopter_version: str = ADOPTER_VERSION

    def projection(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "applied": self.applied,
                "credential_id": self.credential_id,
                "changes": dict(self.changes),
                "budget_after": None if self.budget_after is None
                else self.budget_after.projection(),
                "adopter_version": self.adopter_version}


def _window_key(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).date().isoformat()


def evaluate(request: AdoptionRequest, *, budget: BudgetState | None = None,
             apply_writer=None) -> AdoptionDecision:
    """独立采用器的判定。拒绝时**不写任何东西**，也不会退化成"部分采用"。"""
    credential = request.credential
    if credential is None:
        raise AdoptionRefused(R_NO_CREDENTIAL)
    if not credential.auto_applies:
        # ADVISORY/REQUIRE_AI 下不做自动采用：这不是错误，是当前的唯一模式。
        raise AdoptionRefused(R_NO_CREDENTIAL, f"mode:{credential.mode}")
    if request.producer and request.producer == request.approver:
        raise AdoptionRefused(R_APPROVER_IS_PRODUCER)
    now = _instant(request.now or dt.datetime.now(dt.timezone.utc).isoformat(), "now")
    if _instant(credential.expires_at, "expires_at") <= now:
        raise AdoptionRefused(R_EXPIRED)
    if int(request.revocation_epoch) != credential.revocation_epoch:
        raise AdoptionRefused(R_REVOKED,
                              f"{request.revocation_epoch}!={credential.revocation_epoch}")
    if str(request.strategy_id) not in credential.strategy_ids:
        raise AdoptionRefused(R_STRATEGY_OUT_OF_SCOPE, request.strategy_id)
    if str(request.target_kind) not in credential.allowed_target_kinds:
        raise AdoptionRefused(R_TARGET_KIND_OUT_OF_SCOPE, request.target_kind)
    if str(request.evidence_policy_id) != credential.evidence_policy_id:
        raise AdoptionRefused(R_EVIDENCE_POLICY_MISMATCH)
    if request.current_baseline != request.requested_baseline:
        # 写前复核：基线在决策之后变了，就不能覆盖后来的人工作业。
        raise AdoptionRefused(R_STALE_BASE)

    current = dict(request.current_baseline)
    total_change = 0.0
    for name, value in request.field_changes:
        if name not in credential.allowed_fields:
            raise AdoptionRefused(R_FIELD_OUT_OF_SCOPE, name)
        if name not in current:
            raise AdoptionRefused(R_FIELD_OUT_OF_SCOPE, f"not_in_baseline:{name}")
        delta = abs(float(value) - float(current[name]))
        if delta > credential.max_change_per_apply + 1e-12:
            raise AdoptionRefused(R_CHANGE_TOO_LARGE, name)
        total_change += delta

    window = _window_key(now)
    used = budget if budget is not None and budget.window_key == window \
        else BudgetState(window_key=window)
    if used.changes_used + total_change > credential.max_change_per_day + 1e-12:
        raise AdoptionRefused(R_BUDGET_EXHAUSTED, f"changes:{used.changes_used}+{total_change}")
    if used.cost_used + float(request.cost) > credential.daily_cost_cap + 1e-12:
        raise AdoptionRefused(R_BUDGET_EXHAUSTED, "daily_cost_cap")
    if used.trials_used + 1 > credential.max_trials_per_day:
        raise AdoptionRefused(R_BUDGET_EXHAUSTED, "max_trials_per_day")

    after = BudgetState(window_key=window,
                        changes_used=round(used.changes_used + total_change, 8),
                        cost_used=round(used.cost_used + float(request.cost), 8),
                        trials_used=used.trials_used + 1)
    if apply_writer is None:
        # 没有接入唯一写入者时绝不落地：拒绝，而不是"先记下来假装已采用"。
        raise AdoptionRefused(R_APPLY_NOT_WIRED)
    apply_writer(request)
    return AdoptionDecision(allowed=True, reason=None, applied=True,
                            credential_id=credential.capability_id,
                            changes=tuple(request.field_changes), budget_after=after)


def describe_capability(credential: CapabilityCredential | None) -> dict:
    """给运维看的现状：凭据范围、剩余额度、是否会自动采用。"""
    if credential is None:
        return {"mode": MODE_ADVISORY, "capability_id": None, "auto_applies": False,
                "note": "没有凭据：全流程只出建议，不自动采用"}
    return {"mode": credential.mode, "capability_id": credential.capability_id,
            "auto_applies": credential.auto_applies,
            "issued_by": credential.issued_by,
            "expires_at": credential.expires_at,
            "revocation_epoch": credential.revocation_epoch,
            "scope": {"strategy_ids": list(credential.strategy_ids),
                      "target_kinds": list(credential.allowed_target_kinds),
                      "fields": list(credential.allowed_fields)},
            "limits": {"max_change_per_apply": credential.max_change_per_apply,
                       "max_change_per_day": credential.max_change_per_day,
                       "max_trials_per_day": credential.max_trials_per_day,
                       "daily_cost_cap": credential.daily_cost_cap,
                       "max_tool_steps_per_task": credential.max_tool_steps_per_task}}


def credential_fingerprint(credential: CapabilityCredential) -> str:
    import hashlib

    material = json.dumps({
        "capability_id": credential.capability_id, "mode": credential.mode,
        "strategy_ids": list(credential.strategy_ids),
        "target_kinds": list(credential.allowed_target_kinds),
        "fields": list(credential.allowed_fields),
        "bounds_version": credential.bounds_version,
        "evidence_policy_id": credential.evidence_policy_id,
        "limits": [credential.max_change_per_apply, credential.max_change_per_day,
                   credential.max_trials_per_day, credential.daily_cost_cap,
                   credential.max_tool_steps_per_task],
        "expires_at": credential.expires_at,
        "revocation_epoch": credential.revocation_epoch,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
