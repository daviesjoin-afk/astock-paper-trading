# -*- coding: utf-8 -*-
"""R36-A —— Experiment Search Controller 的 orchestration boundary。

一句话职责：

```text
exact generation batch
    ↓ verified candidate pool
bounded search run + queued jobs
    ↓
claim next operational job
```

依赖方向**只能**是：

```text
experiment_search_service
    ↓
experiment_search_contract
experiment_search_repository
strategy_candidate_repository
```

绝不反向（``strategy_candidate`` 不得依赖 search service），也绝不依赖
``experiment_validation_runner`` / ``robustness_runner`` / promotion /
strategy_lifecycle / execution / portfolio / AI provider。

─────────────── 本轮只调度"需要验证什么" ───────────────

```text
R36-A  只调度“需要验证什么”
R29/R30 决定“验证事实是什么”
R36-C  才决定“根据事实选谁”
R31    决定“谁可以晋级”
```

这些 authority 不允许合并。因此本 service **不执行** R29/R30 runner：当前
``ExperimentSpec`` 的 canonical strategy identity 是正式 ``StrategyVersion``，而 R35
candidate 可以改变 entry/exit/factor/parameter —— 拿 parent checksum + candidate
parameters 冒充 candidate experiment identity，会让"不同 entry/exit/factor 但同 parent +
parameters"的两个候选得到同一 experiment identity。**R36-B** 专门解决这条桥接，
R36-A 不提前伪造它。
"""
from __future__ import annotations

import sqlite3

import experiment_search_contract as ESC
import experiment_search_repository as ESR
import strategy_candidate as SC
import strategy_candidate_repository as SCRepo

__all__ = [
    "ExperimentSearchError",
    "REASON_BATCH_NOT_FOUND",
    "REASON_CANDIDATE_NOT_IN_BATCH",
    "REASON_CANDIDATE_UNVERIFIABLE",
    "REASON_CANDIDATE_SUBSET_MISMATCH",
    "REASON_RUN_NOT_FOUND",
    "REASON_JOB_NOT_FOUND",
    "REASON_ILLEGAL_TRANSITION",
    "REASON_ATTEMPT_BUDGET_EXHAUSTED",
    "claim_next_job",
    "create_search_run",
    "fail_claimed_job",
    "get_search_run",
    "list_search_jobs",
    "record_job_event",
]


class ExperimentSearchError(RuntimeError, ValueError):
    """Stable rejection from the search controller (always fail closed)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)


REASON_BATCH_NOT_FOUND = "generation_batch_not_found"
REASON_CANDIDATE_NOT_IN_BATCH = "candidate_not_in_generation_batch"
REASON_CANDIDATE_UNVERIFIABLE = "candidate_ledger_self_verification_failed"
REASON_CANDIDATE_SUBSET_MISMATCH = "candidate_subset_does_not_match_batch"
REASON_RUN_NOT_FOUND = "search_run_not_found"
REASON_JOB_NOT_FOUND = "search_job_not_found"
REASON_ILLEGAL_TRANSITION = "illegal_job_state_transition"
REASON_ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"


# ---------------------------------------------------------------------------
# exact generation batch → verified candidate pool
# ---------------------------------------------------------------------------


def _load_exact_batch(conn: sqlite3.Connection, generation_batch_id: str) -> dict:
    """Read **exactly** one generation batch by id.

    ``get_generation_batch`` 只按显式 id 取，且自身校验 id 形状。这里刻意**没有**
    "最近一批"兜底：``ORDER BY created_at DESC LIMIT 1`` 之类的隐式输入会让一次 search
    悄悄换掉它的输入集合，而 fingerprint 却看不出来。

    repository 会重算原始 material 的输入指纹，并核对 payload 与台账列。
    本层仍检查返回的 batch_id 与查找键一致，且输入指纹形状合法；任何校验失败都在
    读取候选集合、写入 search 台账之前拒绝。
    """
    if not isinstance(generation_batch_id, str) or not generation_batch_id.strip():
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, "generation_batch_id required")
    requested = generation_batch_id.strip()
    try:
        batch = SCRepo.get_generation_batch(conn, requested, verify_input=True)
    except (SCRepo.StrategyCandidateRepositoryError, sqlite3.Error) as exc:
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND,
                                    type(exc).__name__) from None
    if batch is None:
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, requested)
    if not isinstance(batch, dict):
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, "batch payload is not a mapping")
    # payload 自述身份必须与查找键一致：请求 A 绝不能拿到 B 的候选。
    if str(batch.get("batch_id") or "") != requested:
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, "batch identity mismatch")
    if not ESC.is_search_identity(str(batch.get("generation_input_fingerprint") or "")):
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND,
                                    "batch payload lacks a canonical input fingerprint")
    return batch


def _verified_candidate_pool(conn: sqlite3.Connection, generation_batch_id: str) -> tuple[str, ...]:
    """The exact batch's candidate ids, each re-verified against the canonical ledger.

    每个 id 都再次经 ``get_candidate`` 自证 —— 绝不只相信 ``proposal_json`` /
    ``batch candidate_count`` / 客户端声称的 id 列表。那条路径会把"这条候选真的在账本里
    且指纹自洽"降级成"某处 JSON 里有这个字符串"。

    candidate 反序列化在 :func:`strategy_candidate.candidate_from_projection` 里对
    **未知 schema 版本** fail closed（显式 v1/v2 allowlist），因此损坏或未来版本的行
    无法成为 search input。
    """
    proposals = SCRepo.list_batch_proposals(conn, generation_batch_id)
    ids = []
    for proposal in proposals:
        candidate_id = str(proposal.get("candidate_id") or "")
        if not candidate_id:
            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE, "missing id")
        ids.append(candidate_id)
    # 去重后排序：canonical pool 顺序不承载语义。
    unique = sorted(set(ids))
    if not unique:
        raise ExperimentSearchError(REASON_BATCH_NOT_FOUND, "batch has no candidates")
    for candidate_id in unique:
        try:
            candidate = SCRepo.get_candidate(conn, candidate_id)
        except SC.CandidateValidationError as exc:
            # 未知 schema / 篡改 / 指纹不符一律 fail closed。异常文案本身是稳定的
            # machine token（例如 ``unsupported_candidate_schema_version:...``）。
            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE,
                                        str(exc)) from None
        except (SCRepo.StrategyCandidateRepositoryError, sqlite3.Error) as exc:
            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE,
                                        type(exc).__name__) from None
        if candidate is None:
            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE, candidate_id)
        if not SC.verify_candidate_fingerprint(candidate):
            raise ExperimentSearchError(REASON_CANDIDATE_UNVERIFIABLE, candidate_id)
    return tuple(unique)


def _resolve_subset(pool: tuple[str, ...], candidate_ids) -> tuple[str, ...]:
    """Resolve the caller's candidate subset against the exact batch.

    语义：

    * ``candidate_ids`` 省略 ⇒ 选择**整个** batch；
    * 显式给出 ⇒ 选择那个确切子集。

    两种情况下，超出预算都是 **fail closed**，绝不截断：R36-A 没有 selection
    authority，替用户"删掉几个"已经是 R36-C 的决定。
    """
    if candidate_ids is None:
        return pool
    requested = ESC.canonical_candidate_ids(candidate_ids)
    allowed = set(pool)
    outside = [item for item in requested if item not in allowed]
    if outside:
        # batch 外的 candidate 混入：拒绝，绝不"忽略多余项继续"。
        raise ExperimentSearchError(REASON_CANDIDATE_NOT_IN_BATCH, outside[0])
    return requested


def create_search_run(conn: sqlite3.Connection, *, generation_batch_id: str,
                      budget: ESC.SearchBudget, candidate_ids=None,
                      experiment_plan: ESC.ExperimentSearchPlan | None = None,
                      created_at: str | None = None) -> dict:
    """Atomically create one search run + N job declarations + N queued events.

    调用方拥有连接与事务。**原子性**是本函数的契约：任一 job 或事件写入失败，调用方的
    事务回滚后不应留下"run 有、jobs 一半、queued events 另一半"。

    顺序刻意是"先把输入固定下来，再写台账"：

    ```text
    exact batch → verified candidate pool → explicit subset → SearchBudget → SearchSpec
    ```
    """
    if not isinstance(budget, ESC.SearchBudget):
        raise ExperimentSearchError("canonical_search_budget_required")
    batch = _load_exact_batch(conn, generation_batch_id)
    pool = _verified_candidate_pool(conn, str(batch["batch_id"]))
    try:
        selected = _resolve_subset(pool, candidate_ids)
        spec = ESC.ExperimentSearchSpec(
            generation_batch_id=str(batch["batch_id"]),
            generation_input_fingerprint=str(batch["generation_input_fingerprint"]),
            candidate_ids=selected,
            budget=budget,
            search_contract_version=(ESC.SEARCH_CONTRACT_VERSION_V2 if experiment_plan is not None
                                     else ESC.SEARCH_CONTRACT_VERSION),
            experiment_plan=experiment_plan,
        )
    except ESC.SearchContractError as exc:
        # 契约拒绝统一映射成本层的稳定 reason，绝不泄漏裸 SearchContractError。
        raise ExperimentSearchError(exc.reason, exc.detail) from None

    run_id = ESC.search_run_id()
    ESR.record_search_run(conn, spec=spec, run_id=run_id, created_at=created_at)
    jobs = spec.job_specs(run_id)
    for job in jobs:
        ESR.record_job(conn, job=job, created_at=created_at)
        # 每条 job 创建时必须恰好落一条 queued 事件：否则 job 会停在"没有 current state"
        # 的悬空态，而队列只会显示空白而不是 queued。
        ESR.record_job_event(
            conn, job_id=job.job_id, search_run_id=run_id, event_kind="queued",
            attempt_number=None, actor=None, created_at=created_at)
    return {
        "authority": "read_only_search_control_plane_projection",
        "search_run_id": run_id,
        "search_input_fingerprint": spec.search_input_fingerprint,
        "generation_batch_id": spec.generation_batch_id,
        "generation_input_fingerprint": spec.generation_input_fingerprint,
        "candidate_count": spec.candidate_count,
        "budget": spec.budget.projection(),
        "search_spec": spec.projection(),
        "job_ids": [job.job_id for job in jobs],
        "jobs_created": len(jobs),
        "queued_events_created": len(jobs),
    }


# ---------------------------------------------------------------------------
# queue operations
# ---------------------------------------------------------------------------


def _job_state(conn: sqlite3.Connection, job_id: str) -> tuple[str | None, list[dict]]:
    events = ESR.list_job_events(conn, job_id)
    if not events:
        return None, []
    # event_seq 是唯一顺序权威（repository 已按它排序）。
    return str(events[-1]["event_kind"]), list(events)


def _attempt_count(events: list[dict]) -> int:
    return sum(1 for item in events if item["event_kind"] == "claimed")


def _run_budget(conn: sqlite3.Connection, run_id: str) -> dict:
    run = ESR.get_search_run(conn, run_id)
    if run is None:
        raise ExperimentSearchError(REASON_RUN_NOT_FOUND, run_id)
    return run


def record_job_event(conn: sqlite3.Connection, *, job_id: str, event_kind: str,
                     actor: str | None = None, reason: str | None = None,
                     evidence_owner: str | None = None, evidence_id: str | None = None,
                     created_at: str | None = None) -> dict:
    """Append one state transition, validated against the **transition table**.

    合法性只由 ``ESC.ALLOWED_TRANSITIONS`` 一处裁决（数据表达，不是 if/elif 链）。
    ``attempt_number`` 由 canonical event history **推导**，调用方无权声明"这次是
    attempt 1"然后覆盖。
    """
    job_id = str(job_id or "")
    row = conn.execute(
        "SELECT search_run_id FROM experiment_search_jobs WHERE job_id=?",
        (job_id,)).fetchone()
    if row is None:
        raise ExperimentSearchError(REASON_JOB_NOT_FOUND, job_id)
    run_id = str(row[0])
    run = _run_budget(conn, run_id)
    max_attempts = int(run["budget"]["max_attempts_per_job"])

    current, events = _job_state(conn, job_id)
    if not ESC.next_state_allowed(current, event_kind):
        raise ExperimentSearchError(
            REASON_ILLEGAL_TRANSITION, f"{current or '<none>'}->{event_kind}")

    attempt_number: int | None = None
    if event_kind == "claimed":
        used = _attempt_count(events)
        if used >= max_attempts:
            # retry 预算耗尽：绝不无限重试。
            raise ExperimentSearchError(REASON_ATTEMPT_BUDGET_EXHAUSTED,
                                        f"{used}/{max_attempts}")
        attempt_number = used + 1
    ESR.record_job_event(
        conn, job_id=job_id, search_run_id=run_id, event_kind=event_kind,
        attempt_number=attempt_number, actor=actor, reason=reason,
        evidence_owner=evidence_owner, evidence_id=evidence_id, created_at=created_at)
    return {"job_id": job_id, "search_run_id": run_id, "event_kind": event_kind,
            "attempt_number": attempt_number}


def fail_claimed_job(conn: sqlite3.Connection, *, job_id: str, reason: str,
                     created_at: str | None = None, actor: str | None = None) -> dict:
    """Operationally fail one currently-claimed job, in its own short transaction.

    This is the single generic queue-failure authority shared by R36-B1 and R36-B2.
    It only appends a ``failed`` event when the job's latest event is ``claimed``, so
    a crash-safe retry cannot fail a job that already reached a terminal state. It
    carries no evidence owner: evidence completion has two explicit verified adapters.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        events = ESR.list_job_events(conn, job_id)
        if events and events[-1]["event_kind"] == "claimed":
            result = record_job_event(conn, job_id=job_id, event_kind="failed",
                                      actor=actor, reason=reason, created_at=created_at)
        else:
            result = {"job_id": job_id, "event_kind": None, "attempt_number": None}
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return result


def claim_next_job(conn: sqlite3.Connection, search_run_id: str,
                   actor: str | None = None, created_at: str | None = None) -> dict:
    """Claim the next claimable job of one exact search run (concurrency-safe).

    调用方必须以 ``BEGIN IMMEDIATE`` 打开事务（本函数不做事务管理）：读取 → 计算 current
    state → 选择 → 检查 attempt 预算 → append claimed 事件 → 由调用方 commit。网络、
    实验计算与回测**绝不**在这个事务里。

    选择顺序使用 :data:`experiment_search_contract.QUEUE_POLICY_VERSION` 定义的中性顺序
    （``candidate_id ASC``）—— 它不是 ranking。按预期收益 / AI confidence / Sharpe 排序
    都属于后续 selection policy（R36-C）。
    """
    run = _run_budget(conn, str(search_run_id or ""))
    max_attempts = int(run["budget"]["max_attempts_per_job"])
    jobs = ESR.list_search_jobs(conn, run["search_run_id"])
    for job in jobs:
        current, events = _job_state(conn, job["job_id"])
        if not ESC.next_state_allowed(current, "claimed"):
            continue
        if _attempt_count(events) >= max_attempts:
            continue
        return record_job_event(conn, job_id=job["job_id"], event_kind="claimed",
                                actor=actor, created_at=created_at)
    # 没有可 claim 的 job：显式返回，而不是返回"第一行"假装成功。
    return {"job_id": None, "search_run_id": run["search_run_id"],
            "event_kind": None, "attempt_number": None, "claimed": False}


def get_search_run(conn: sqlite3.Connection, search_run_id: str) -> dict:
    """Exact-run read model: run facts + jobs + their operational current state."""
    run = ESR.get_search_run(conn, str(search_run_id or ""))
    if run is None:
        raise ExperimentSearchError(REASON_RUN_NOT_FOUND, str(search_run_id or ""))
    states = {item["job_id"]: item for item in ESR.project_job_states(conn, run["search_run_id"])}
    jobs = []
    for job in ESR.list_search_jobs(conn, run["search_run_id"]):
        state = states.get(job["job_id"], {})
        jobs.append({
            "job_id": job["job_id"],
            "candidate_id": job["candidate_id"],
            "stage": job["stage"],
            "current_state": state.get("current_state"),
            "attempt_count": state.get("attempt_count", 0),
        })
    return {
        "authority": "read_only_search_control_plane_projection",
        "search_run_id": run["search_run_id"],
        "search_input_fingerprint": run["search_input_fingerprint"],
        "generation_batch_id": run["generation_batch_id"],
        "generation_input_fingerprint": run["generation_input_fingerprint"],
        "candidate_count": run["candidate_count"],
        "budget": run["budget"],
        "created_at": run["created_at"],
        "jobs": jobs,
    }


def list_search_jobs(conn: sqlite3.Connection, search_run_id: str) -> tuple[dict, ...]:
    """Operational queue projection for one exact search run."""
    return ESR.project_job_states(conn, str(search_run_id or ""))
