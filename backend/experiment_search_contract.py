# -*- coding: utf-8 -*-
"""R36-A —— Experiment Search Controller 的**纯 domain contract**。

一句话 authority：本模块定义"**一次 bounded experiment search request 是什么**"。

它**不**拥有：

* candidate identity —— 那是 :mod:`strategy_candidate`（本模块只引用 ``candidate_id``）；
* generation batch —— 那是 :mod:`strategy_candidate_repository`；
* 实验事实 / 结果 —— 那是 R28/R29/R30；
* selection / ranking / promotion —— 属于 R36-C 与 R31。

它是纯契约：**没有 DB、没有网络、没有 registry、没有 current state、没有时钟**。
所有事实由调用方显式提供，或由 exact batch 的既有事实派生。

─────────────── 四句边界（本项目所有 R36 文档都重复这四句）───────────────

```text
A queued experiment is not an evaluation result.
A completed queue job does not mean a candidate passed.
Search order is not candidate ranking.
The search controller may schedule evidence production;
it may not manufacture evidence or promotion authority.
```

─────────────── 预算不是"截断许可" ───────────────

``SearchBudget.max_candidates`` 是**上限**，不是"从大集合里挑前 N 个"的许可。R36-A 没有
selection authority，因此当 exact batch 的候选数超过预算、而调用方又没有显式给出
``candidate_ids`` 时，唯一诚实的动作是 **fail closed** —— 悄悄取前 N 个等于替用户决定
"删掉哪些候选"，那已经是 selection policy（R36-C）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import strategy_candidate_search_space as SS
import experiment_contract as EC
import robustness_contract as RC
import walk_forward_validation as WFV

__all__ = [
    "ALLOWED_TRANSITIONS",
    "EVENT_KINDS",
    "JOB_STAGE_PIT_VALIDATION",
    "JOB_STAGE_ROBUSTNESS",
    "JOB_STAGES",
    "QUEUE_POLICY_CANDIDATE_ID_ASC",
    "QUEUE_POLICY_VERSION",
    "ROBUSTNESS_JOB_CONTRACT_VERSION",
    "SEARCH_CONTRACT_VERSION",
    "SEARCH_JOB_CONTRACT_VERSION",
    "SearchContractError",
    "SearchBudget",
    "SearchJobSpec",
    "RobustnessSearchJobSpec",
    "ExperimentSearchSpec",
    "ExperimentSearchPlan",
    "ExperimentSearchPlanV2",
    "canonical_candidate_ids",
    "experiment_plan_from_projection",
    "is_search_identity",
    "is_terminal_state",
    "next_state_allowed",
    "search_job_from_projection",
    "search_run_id",
    "search_spec_from_projection",
]

#: search request contract 版本（进 SearchSpec fingerprint）。
SEARCH_CONTRACT_VERSION = "experiment-search-contract-v1"
SEARCH_CONTRACT_VERSION_V2 = "experiment-search-contract-v2"
EXPERIMENT_PLAN_CONTRACT_VERSION = "candidate-experiment-plan-v1"
EXPERIMENT_PLAN_CONTRACT_VERSION_V2 = "candidate-experiment-plan-v2"

#: job declaration contract 版本（进 job fingerprint）。
SEARCH_JOB_CONTRACT_VERSION = "experiment-search-job-contract-v1"
#: R36-B2 robustness job declaration contract 版本（进 robustness job fingerprint）。
ROBUSTNESS_JOB_CONTRACT_VERSION = "experiment-search-robustness-job-contract-v1"

#: 两个 stage 各有独立的 job contract；PIT identity 保持 v1 不变。
JOB_STAGE_PIT_VALIDATION = "pit_validation"
JOB_STAGE_ROBUSTNESS = "robustness"
JOB_STAGES = (JOB_STAGE_PIT_VALIDATION, JOB_STAGE_ROBUSTNESS)

#: queue policy：claim 顺序的**唯一**权威。
#:
#: 顺序必须是 deterministic 且**非 ranking**。R36-A 没有 candidate quality authority，
#: 因此只有 candidate id 升序这一种中性顺序 —— 绝不能是"预期收益高的先跑"、
#: "AI confidence 高的先跑"、"Sharpe 高的先跑"，那些都是 selection policy。
QUEUE_POLICY_CANDIDATE_ID_ASC = "candidate-id-ascending-v1"
QUEUE_POLICY_VERSION = QUEUE_POLICY_CANDIDATE_ID_ASC

#: job event kind 词表。
EVENT_KINDS = ("queued", "claimed", "completed", "failed", "cancelled")

#: **唯一**的状态转换权威（数据表达，不是 if/elif 链）。
#:
#: ``None`` 表示 job 尚无事件（声明刚创建）。终态（``completed`` / ``cancelled``）没有
#: 出边。``failed → claimed`` 是**重试**入口，其 attempt 上限由 SearchBudget 裁决。
ALLOWED_TRANSITIONS = {
    None: frozenset({"queued"}),
    "queued": frozenset({"claimed", "cancelled"}),
    # R36-A 尚无 candidate → experiment evidence 绑定，不能声明完成。
    "claimed": frozenset({"failed"}),
    "failed": frozenset({"claimed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

#: R36 预算上限与 R35 candidate generation 上限**对齐**：搜索不可能比生成更多候选。
#: 复用实际常量，绝不复制 magic number。
MAX_SEARCH_CANDIDATES = SS.MAX_CANDIDATES_PER_GENERATION_REQUEST

#: 单 job 最大尝试次数。刻意很小：R36-A 的 retry 是"执行失败重试"，不是搜索迭代。
MAX_ATTEMPTS_PER_JOB = 3

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def is_search_identity(value: Any) -> bool:
    """True when ``value`` is a canonical 64-hex search identity.

    repository 与 service 用它校验 id 形状，避免各自复制一份正则。
    """
    return bool(_SHA256.match(str(value or "")))


class SearchContractError(ValueError):
    """Stable rejection from the search contract (always fail closed)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)


def _required_int(value: Any, *, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SearchContractError("invalid_search_budget", f"{what} must be an int")
    if not low <= value <= high:
        raise SearchContractError(
            "invalid_search_budget", f"{what} must be within [{low}, {high}]")
    return int(value)


def _required_id(value: Any, *, what: str) -> str:
    text = str(value or "")
    if not _SHA256.match(text):
        raise SearchContractError(f"invalid_{what}", "must be 64 lowercase hex chars")
    return text


@dataclass(frozen=True, slots=True)
class SearchBudget:
    """确定性预算：**只有**候选数与单 job 尝试数两个维度。

    刻意**不**引入 ``max_wall_seconds`` / ``deadline`` / CPU / token 预算：那会立刻把机器
    负载、wall clock、worker capability 带进 search identity，使同一次搜索请求的
    fingerprint 依赖"当时机器有多快"。资源/成本预算以后单独版本化。

    刻意**不**引入 priority：所有 job 同权，否则 priority 会马上变成"哪个 candidate 更值得
    先测"，那属于 R36-C。
    """

    max_candidates: int
    max_attempts_per_job: int = MAX_ATTEMPTS_PER_JOB

    def __post_init__(self):
        object.__setattr__(self, "max_candidates", _required_int(
            self.max_candidates, what="max_candidates", low=1, high=MAX_SEARCH_CANDIDATES))
        object.__setattr__(self, "max_attempts_per_job", _required_int(
            self.max_attempts_per_job, what="max_attempts_per_job",
            low=1, high=MAX_ATTEMPTS_PER_JOB))

    def projection(self) -> dict:
        return {"max_candidates": self.max_candidates,
                "max_attempts_per_job": self.max_attempts_per_job}


def canonical_candidate_ids(values: Any) -> tuple[str, ...]:
    """Normalise an explicit candidate subset into canonical order.

    **输入顺序不承载语义**：``[A, B, C]`` 与 ``[C, A, B]`` 必须得到同一个 fingerprint。
    因此存储前统一按 ``candidate_id`` 升序排列。

    重复 id **拒绝**（不是静默去重）：调用方声称的集合与它给的数量不一致时，先去重再继续
    等于替它猜意图，并会让 ``candidate_count`` 与真实集合脱节。
    """
    if values is None:
        raise SearchContractError("invalid_candidate_subset", "must be a sequence")
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise SearchContractError("invalid_candidate_subset", "must be a sequence of ids")
    ids = [_required_id(item, what="candidate_id") for item in values]
    if not ids:
        raise SearchContractError("invalid_candidate_subset", "must not be empty")
    if len(set(ids)) != len(ids):
        raise SearchContractError("duplicate_candidate_id", "candidate ids must be unique")
    return tuple(sorted(ids))


@dataclass(frozen=True, slots=True)
class SearchJobSpec:
    """一条 job declaration：``one search run + one exact candidate + one stage``。

    这是 **declared unit of work**，不是 occurrence event —— 因此可以使用
    deterministic content identity（``job_id == job_fingerprint``）。与之相对，
    **执行历史**只能通过 append-only job events 表达。

    ``job_id`` 由 ``(search_run_id, candidate_id, stage, contract version)`` 派生：
    同一个 search run 里的同一个 candidate/stage 恒得同一个 job id；换 search run、
    换 candidate 或换 stage 都会得到不同的 job id。
    """

    search_run_id: str
    candidate_id: str
    stage: str = JOB_STAGE_PIT_VALIDATION
    job_contract_version: str = SEARCH_JOB_CONTRACT_VERSION

    def __post_init__(self):
        object.__setattr__(self, "search_run_id",
                           _required_id(self.search_run_id, what="search_run_id"))
        object.__setattr__(self, "candidate_id",
                           _required_id(self.candidate_id, what="candidate_id"))
        stage = str(self.stage or "")
        if stage not in JOB_STAGES:
            raise SearchContractError("unsupported_job_stage", stage or "<empty>")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "job_contract_version",
                           str(self.job_contract_version or ""))

    @property
    def job_fingerprint(self) -> str:
        material = {
            "candidate_id": self.candidate_id,
            "job_contract_version": self.job_contract_version,
            "search_run_id": self.search_run_id,
            "stage": self.stage,
        }
        return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()

    @property
    def job_id(self) -> str:
        """Job 是 declared unit，因此 id 就是它的 content fingerprint。"""
        return self.job_fingerprint

    def projection(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_fingerprint": self.job_fingerprint,
            "search_run_id": self.search_run_id,
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "job_contract_version": self.job_contract_version,
        }


@dataclass(frozen=True, slots=True)
class RobustnessSearchJobSpec:
    """R36-B2 第二条 job contract：``search + candidate + robustness``。

    身份必须绑定 **exact** baseline run、baseline experiment fingerprint 与
    ``RobustnessPlan`` fingerprint —— 因为不同 baseline / plan 是不同的 work unit。
    因此 job id 不能只由 ``(search_run_id, candidate_id, stage)`` 派生。
    """

    search_run_id: str
    candidate_id: str
    baseline_run_key: str
    baseline_experiment_fingerprint: str
    robustness_policy_fingerprint: str
    robustness_plan_fingerprint: str
    stage: str = JOB_STAGE_ROBUSTNESS
    job_contract_version: str = ROBUSTNESS_JOB_CONTRACT_VERSION

    def __post_init__(self):
        object.__setattr__(self, "search_run_id",
                           _required_id(self.search_run_id, what="search_run_id"))
        object.__setattr__(self, "candidate_id",
                           _required_id(self.candidate_id, what="candidate_id"))
        for name in ("baseline_run_key", "baseline_experiment_fingerprint",
                     "robustness_policy_fingerprint", "robustness_plan_fingerprint"):
            object.__setattr__(self, name, _required_id(getattr(self, name), what=name))
        if str(self.stage or "") != JOB_STAGE_ROBUSTNESS:
            raise SearchContractError("unsupported_job_stage", str(self.stage or "<empty>"))
        object.__setattr__(self, "stage", JOB_STAGE_ROBUSTNESS)
        if str(self.job_contract_version or "") != ROBUSTNESS_JOB_CONTRACT_VERSION:
            raise SearchContractError("unsupported_robustness_job_contract",
                                      str(self.job_contract_version or "<empty>"))

    @property
    def job_fingerprint(self) -> str:
        material = {
            "search_run_id": self.search_run_id,
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "baseline_run_key": self.baseline_run_key,
            "baseline_experiment_fingerprint": self.baseline_experiment_fingerprint,
            "robustness_policy_fingerprint": self.robustness_policy_fingerprint,
            "robustness_plan_fingerprint": self.robustness_plan_fingerprint,
            "job_contract_version": self.job_contract_version,
        }
        return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()

    @property
    def job_id(self) -> str:
        return self.job_fingerprint

    def projection(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_fingerprint": self.job_fingerprint,
            "search_run_id": self.search_run_id,
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "baseline_run_key": self.baseline_run_key,
            "baseline_experiment_fingerprint": self.baseline_experiment_fingerprint,
            "robustness_policy_fingerprint": self.robustness_policy_fingerprint,
            "robustness_plan_fingerprint": self.robustness_plan_fingerprint,
            "job_contract_version": self.job_contract_version,
        }


@dataclass(frozen=True, slots=True)
class ExperimentSearchPlan:
    code_revision: str
    dataset_fingerprint: str
    market_archive_fingerprint: str
    universe_archive_fingerprint: str
    session_calendar_fingerprint: str
    start_date: str
    end_date: str
    asof_policy: dict
    execution_assumptions: dict
    cost_model: dict
    validation_portfolio: dict
    walk_forward_config: WFV.WalkForwardConfig
    random_seed: int
    financial_archive_fingerprint: str | None = None
    plan_contract_version: str = EXPERIMENT_PLAN_CONTRACT_VERSION

    def __post_init__(self):
        if self.plan_contract_version != EXPERIMENT_PLAN_CONTRACT_VERSION:
            raise SearchContractError("unsupported_experiment_plan_contract")
        if not isinstance(self.walk_forward_config, WFV.WalkForwardConfig):
            raise SearchContractError("canonical_walk_forward_config_required")
        object.__setattr__(self, "session_calendar_fingerprint",
                           EC._stable_fingerprint(self.session_calendar_fingerprint, name="session_calendar_fingerprint"))
        if self.financial_archive_fingerprint is not None:
            object.__setattr__(self, "financial_archive_fingerprint",
                               EC._stable_fingerprint(self.financial_archive_fingerprint, name="financial_archive_fingerprint"))
        normalized = EC._normalize_experiment_environment({
            **self.experiment_environment(), "tradability_fingerprint": "0" * 64,
            "contract_version": EC.CANDIDATE_EXPERIMENT_CONTRACT_VERSION})
        for name in ("code_revision", "dataset_fingerprint", "start_date", "end_date",
                     "asof_policy", "execution_assumptions", "cost_model", "random_seed"):
            object.__setattr__(self, name, normalized[name])
        object.__setattr__(self, "market_archive_fingerprint", normalized["market_data_fingerprint"])
        object.__setattr__(self, "universe_archive_fingerprint", normalized["universe_fingerprint"])
        object.__setattr__(self, "validation_portfolio", EC._freeze_json(self.validation_portfolio))

    def experiment_environment(self):
        return {"code_revision": self.code_revision, "dataset_fingerprint": self.dataset_fingerprint,
                "universe_fingerprint": self.universe_archive_fingerprint,
                "market_data_fingerprint": self.market_archive_fingerprint,
                "parameter_set": {"validation_portfolio": self.validation_portfolio,
                                  "validation_calendar_fingerprint": self.session_calendar_fingerprint,
                                  "financial_archive_fingerprint": self.financial_archive_fingerprint,
                                  "walk_forward_config_fingerprint": self.walk_forward_config.fingerprint},
                "start_date": self.start_date, "end_date": self.end_date, "asof_policy": self.asof_policy,
                "execution_assumptions": self.execution_assumptions, "cost_model": self.cost_model,
                "random_seed": self.random_seed}

    def projection(self):
        return {"plan_contract_version": self.plan_contract_version,
                "code_revision": self.code_revision, "dataset_fingerprint": self.dataset_fingerprint,
                "market_archive_fingerprint": self.market_archive_fingerprint,
                "universe_archive_fingerprint": self.universe_archive_fingerprint,
                "financial_archive_fingerprint": self.financial_archive_fingerprint,
                "session_calendar_fingerprint": self.session_calendar_fingerprint,
                "date_range": {"start": self.start_date, "end": self.end_date},
                "asof_policy": EC._thaw_json(self.asof_policy),
                "execution_assumptions": EC._thaw_json(self.execution_assumptions),
                "cost_model": EC._thaw_json(self.cost_model),
                "validation_portfolio": EC._thaw_json(self.validation_portfolio),
                "walk_forward_config": self.walk_forward_config.projection(), "random_seed": self.random_seed}

    @property
    def fingerprint(self):
        return EC._digest(self.projection())


@dataclass(frozen=True, slots=True)
class ExperimentSearchPlanV2:
    """``candidate-experiment-plan-v2`` = v1 全部内容 + pinned robustness policy。

    v1 的 projection/fingerprint 逐字不变；v2 只是在同一 environment 上追加
    ``robustness_policy`` 与其 fingerprint。策略必须先 pin 再执行，且同一次 search
    内所有候选共享同一套 stress policy，因此 policy 进入 search identity。
    """

    plan: ExperimentSearchPlan
    robustness_policy: RC.RobustnessPolicy
    plan_contract_version: str = EXPERIMENT_PLAN_CONTRACT_VERSION_V2

    def __post_init__(self):
        if self.plan_contract_version != EXPERIMENT_PLAN_CONTRACT_VERSION_V2:
            raise SearchContractError("unsupported_experiment_plan_contract")
        if not isinstance(self.plan, ExperimentSearchPlan):
            raise SearchContractError("canonical_experiment_plan_required")
        if not isinstance(self.robustness_policy, RC.RobustnessPolicy):
            raise SearchContractError("canonical_robustness_policy_required")
        if self.plan.plan_contract_version != EXPERIMENT_PLAN_CONTRACT_VERSION:
            raise SearchContractError("canonical_experiment_plan_required")

    @classmethod
    def from_v1(cls, plan: ExperimentSearchPlan,
                robustness_policy: RC.RobustnessPolicy) -> "ExperimentSearchPlanV2":
        return cls(plan=plan, robustness_policy=robustness_policy)

    # ── v1-compatible surface (so existing consumers keep working) ──
    def __getattr__(self, name):
        # Only reached for attributes not defined on the dataclass itself.
        return getattr(object.__getattribute__(self, "plan"), name)

    @property
    def robustness_policy_fingerprint(self) -> str:
        return self.robustness_policy.fingerprint

    def experiment_environment(self):
        return self.plan.experiment_environment()

    def projection(self) -> dict:
        value = self.plan.projection()
        value["plan_contract_version"] = EXPERIMENT_PLAN_CONTRACT_VERSION_V2
        value["robustness_policy"] = self.robustness_policy.projection()
        value["robustness_policy_fingerprint"] = self.robustness_policy.fingerprint
        return value

    @property
    def fingerprint(self) -> str:
        return EC._digest(self.projection())


def _experiment_plan_v2_from_projection(value: dict) -> ExperimentSearchPlanV2:
    args = dict(value)
    args.pop("plan_contract_version")
    policy_projection = args.pop("robustness_policy")
    policy_fingerprint = args.pop("robustness_policy_fingerprint")
    policy_args = dict(policy_projection)
    policy_args.pop("policy_version")
    policy = RC.RobustnessPolicy(
        policy_version=policy_projection.get("policy_version",
                                             RC.ROBUSTNESS_POLICY_VERSION),
        **policy_args)
    if policy.fingerprint != policy_fingerprint:
        raise ValueError("robustness policy fingerprint mismatch")
    plan = experiment_plan_from_projection(
        {**args, "plan_contract_version": EXPERIMENT_PLAN_CONTRACT_VERSION})
    plan_v2 = ExperimentSearchPlanV2(plan=plan, robustness_policy=policy)
    if plan_v2.projection() != value:
        raise ValueError("noncanonical plan v2")
    return plan_v2



def experiment_plan_from_projection(value):
    if not isinstance(value, dict):
        raise SearchContractError("corrupt_search_run")
    try:
        version = value.get("plan_contract_version")
        if version == EXPERIMENT_PLAN_CONTRACT_VERSION_V2:
            return _experiment_plan_v2_from_projection(value)
        if version != EXPERIMENT_PLAN_CONTRACT_VERSION:
            raise ValueError("unsupported_experiment_plan_contract")
        args = dict(value)
        args.pop("plan_contract_version")
        dates = args.pop("date_range")
        args.update(start_date=dates["start"], end_date=dates["end"])
        args["walk_forward_config"] = WFV.WalkForwardConfig.from_projection(args["walk_forward_config"])
        plan = ExperimentSearchPlan(plan_contract_version=EXPERIMENT_PLAN_CONTRACT_VERSION, **args)
        if plan.projection() != value:
            raise ValueError("noncanonical plan")
        return plan
    except (TypeError, ValueError, KeyError) as exc:
        raise SearchContractError("corrupt_search_run") from exc


def search_job_from_projection(value):
    """Unified, fail-closed job decoder dispatching on ``stage`` + contract version."""
    if not isinstance(value, dict):
        raise SearchContractError("corrupt_search_job")
    try:
        projection = dict(value)
        job_id = projection.pop("job_id")
        job_fingerprint = projection.pop("job_fingerprint")
        stage = projection.get("stage")
        contract = projection.get("job_contract_version")
        if stage == JOB_STAGE_PIT_VALIDATION and contract == SEARCH_JOB_CONTRACT_VERSION:
            job = SearchJobSpec(
                search_run_id=projection.get("search_run_id"),
                candidate_id=projection.get("candidate_id"),
                stage=JOB_STAGE_PIT_VALIDATION,
                job_contract_version=SEARCH_JOB_CONTRACT_VERSION)
        elif stage == JOB_STAGE_ROBUSTNESS and contract == ROBUSTNESS_JOB_CONTRACT_VERSION:
            job = RobustnessSearchJobSpec(
                search_run_id=projection.get("search_run_id"),
                candidate_id=projection.get("candidate_id"),
                baseline_run_key=projection.get("baseline_run_key"),
                baseline_experiment_fingerprint=projection.get("baseline_experiment_fingerprint"),
                robustness_policy_fingerprint=projection.get("robustness_policy_fingerprint"),
                robustness_plan_fingerprint=projection.get("robustness_plan_fingerprint"))
        else:
            raise SearchContractError("unknown_search_job_contract",
                                      f"{stage or '<empty>'}:{contract or '<empty>'}")
        if job.job_id != job_id or job.job_fingerprint != job_fingerprint or job.projection() != value:
            raise ValueError("job identity mismatch")
        return job
    except SearchContractError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise SearchContractError("corrupt_search_job") from exc


@dataclass(frozen=True, slots=True)
class ExperimentSearchSpec:
    """一次 bounded experiment search request 的 **content identity**。

    只包含"这次请求要验证什么"：exact generation batch、它的 input fingerprint、已归一
    的 candidate 子集、预算与 queue policy 版本。

    刻意**不**包含：``created_at`` / ``search_run_id`` / ``worker_id`` / ``status`` /
    ``result`` / ``score`` / ``rank`` —— 那些要么是事件身份，要么是运营状态，要么是
    selection authority，都不属于"请求内容"。
    """

    generation_batch_id: str
    generation_input_fingerprint: str
    candidate_ids: tuple[str, ...]
    budget: SearchBudget
    search_contract_version: str = SEARCH_CONTRACT_VERSION
    queue_policy_version: str = QUEUE_POLICY_VERSION
    experiment_plan: ExperimentSearchPlan | ExperimentSearchPlanV2 | None = None

    def __post_init__(self):
        if self.search_contract_version not in (SEARCH_CONTRACT_VERSION, SEARCH_CONTRACT_VERSION_V2):
            raise SearchContractError("unsupported_search_contract_version")
        if self.search_contract_version == SEARCH_CONTRACT_VERSION_V2:
            if not isinstance(self.experiment_plan, (ExperimentSearchPlan,
                                                     ExperimentSearchPlanV2)):
                raise SearchContractError("search_run_experiment_plan_unavailable")
        elif self.experiment_plan is not None:
            raise SearchContractError("legacy_search_must_not_have_experiment_plan")
        object.__setattr__(self, "generation_batch_id", _required_id(
            self.generation_batch_id, what="generation_batch_id"))
        object.__setattr__(self, "generation_input_fingerprint", _required_id(
            self.generation_input_fingerprint, what="generation_input_fingerprint"))
        ids = canonical_candidate_ids(self.candidate_ids)
        if len(ids) > self.budget.max_candidates:
            # 预算不是截断许可：超了就拒绝，绝不擅自取前 N 个。
            raise SearchContractError(
                "candidate_count_exceeds_budget",
                f"{len(ids)} > {self.budget.max_candidates}")
        object.__setattr__(self, "candidate_ids", ids)
        object.__setattr__(self, "search_contract_version",
                           str(self.search_contract_version or ""))
        object.__setattr__(self, "queue_policy_version",
                           str(self.queue_policy_version or ""))

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_ids)

    @property
    def search_input_fingerprint(self) -> str:
        """``"这次 search 请求的内容是什么"`` —— 与执行时刻、run id 无关。"""
        return hashlib.sha256(_canonical(self._identity_material()).encode("utf-8")).hexdigest()

    def _identity_material(self) -> dict:
        material = {
            "search_contract_version": self.search_contract_version,
            "generation_batch_id": self.generation_batch_id,
            "generation_input_fingerprint": self.generation_input_fingerprint,
            "candidate_ids": list(self.candidate_ids),
            "budget": self.budget.projection(),
            # 未来修改 claim 顺序会改变一次 search 的运行语义，因此 policy 必须进身份。
            "queue_policy_version": self.queue_policy_version,
        }
        if self.search_contract_version == SEARCH_CONTRACT_VERSION_V2:
            material.update(experiment_plan=self.experiment_plan.projection(),
                            experiment_plan_fingerprint=self.experiment_plan.fingerprint)
        return material

    def projection(self) -> dict:
        return {**self._identity_material(),
                "search_input_fingerprint": self.search_input_fingerprint,
                "candidate_count": self.candidate_count}

    def job_specs(self, search_run: str) -> tuple[SearchJobSpec, ...]:
        """Declare one job per candidate, in canonical candidate order."""
        return tuple(SearchJobSpec(search_run_id=search_run, candidate_id=candidate_id)
                     for candidate_id in self.candidate_ids)


def search_run_id() -> str:
    """一次 search 请求的 **event identity**：opaque CSPRNG，不是内容指纹。

    同一个完全相同的 SearchSpec 今天跑一次、明天再跑一次：``search_input_fingerprint``
    相同，``search_run_id`` 必须不同。因此绝不能用
    timestamp / PID / 进程内计数器 / 内容 hash 冒充事件唯一性。
    """
    import secrets
    return secrets.token_hex(32)


def search_spec_from_projection(value):
    try:
        args = dict(value)
        fingerprint = args.pop("search_input_fingerprint")
        count = args.pop("candidate_count")
        args["budget"] = SearchBudget(**args["budget"])
        if "experiment_plan" in args:
            plan_fingerprint = args.pop("experiment_plan_fingerprint")
            args["experiment_plan"] = experiment_plan_from_projection(args["experiment_plan"])
            if args["experiment_plan"].fingerprint != plan_fingerprint:
                raise ValueError("plan mismatch")
        spec = ExperimentSearchSpec(**args)
        if spec.search_input_fingerprint != fingerprint or spec.candidate_count != count or spec.projection() != value:
            raise ValueError("search mismatch")
        return spec
    except (TypeError, ValueError, KeyError) as exc:
        raise SearchContractError("corrupt_search_run") from exc


def next_state_allowed(current, target: str) -> bool:
    """The transition table is the only authority for legal job state changes."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal_state(state) -> bool:
    return state in ("completed", "cancelled")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
