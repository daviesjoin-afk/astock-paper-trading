# feat(experiment): add bounded search controller foundation

R36-A。前置：PR #230 已按 exact head `3ec2414ca4116754228dcf6f0a6e2b30d8c091eb` 合并
（merge commit `71d5dd82128ff69df862c09472a2e8782d6fc55e`），R35 正式 COMPLETE。

```text
R34     COMPLETE
R35-A   COMPLETE
R35-B   COMPLETE
R35-C   COMPLETE
R35     COMPLETE

R36-A   IN REVIEW
R36     IN PROGRESS
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
R36-B: NOT STARTED
```

R36-A exact base SHA = `71d5dd82128ff69df862c09472a2e8782d6fc55e`

## 四句边界

```text
A queued experiment is not an evaluation result.
A completed queue job does not mean a candidate passed.
Search order is not candidate ranking.
The search controller may schedule evidence production;
it may not manufacture evidence or promotion authority.
```

## 1. 为什么是这一层

R35 结束时仓库已有 candidate 台账与 AI bounded candidate generation，但缺中间的控制面：

```text
一批 candidate → 谁应该被实验？→ 预算是多少？→ 当前排了哪些实验工作？
→ 哪个工作已领取？→ 失败能否重试？→ 还剩多少预算？
```

R36-A 只建立这个 **control plane**。它不回答"哪个 candidate 更好 / Sharpe 更高 / 应该
晋级"——那些属于 R36-C 与 R31。

```text
exact R35 generation_batch_id
    ↓ verify exact generation batch
exact candidate pool（每个候选再从 canonical ledger 自证）
    ↓ explicit SearchBudget
ExperimentSearchRun
    ↓ immutable PIT-validation job declarations
append-only job events
    ↓
deterministic queue state projection
```

## 2. 本轮不运行 R29/R30

R36-A **不执行** `run_validation()` / `run_robustness()`。当前 `ExperimentSpec` 的
canonical strategy identity 是正式 `StrategyVersion`，而 R35 candidate 可以改变
`entry_spec` / `exit_spec` / `factor_spec` / `parameter_spec`。拿

```text
parent strategy checksum + candidate parameters
```

冒充 candidate experiment identity，会让两个"不同 entry/exit/factor、但同 parent +
parameters"的候选得到**同一** experiment identity。

**R36-B 专门解决 StrategyCandidate → canonical experiment subject 的桥接**；R36-A 不提前
伪造这层。

authority 分工不允许合并：

```text
R36-A   只调度“需要验证什么”
R29/R30 决定“验证事实是什么”
R36-C   才决定“根据事实选谁”
R31     决定“谁可以晋级”
```

## 3. 三个 module = 三个真实 authority

```text
experiment_search_contract.py     一次 bounded search request 是什么（纯契约）
experiment_search_repository.py   run / job / event 三张台账的持久化 owner
experiment_search_service.py      batch → pool → run+jobs → claim 的编排
```

依赖方向只能向下：

```text
experiment_search_service
    ↓
experiment_search_contract
experiment_search_repository
strategy_candidate_repository
```

禁止反向，也禁止依赖 evaluation runner / promotion / lifecycle / execution / portfolio /
AI provider。刻意不拆 `search_manager` / `queue_manager` / `budget_utils` /
`search_facade` / `controller_common`。

## 4. 身份

```text
SearchSpec fingerprint  = sha256(canonical SearchSpec)        content identity
search_run_id           = secrets.token_hex(32)               request event identity
job_id = job_fingerprint = sha256(run+candidate+stage+contract)  declared unit identity
event_id                = secrets.token_hex(32)               event identity
event_seq               = INTEGER PRIMARY KEY AUTOINCREMENT   唯一 ordering authority
```

同一 SearchSpec 今天跑一次、明天再跑一次：`search_input_fingerprint` 相同、
`search_run_id` 不同。因此 run id 绝不用 timestamp / PID / 进程内计数器 / 内容 hash。

SearchSpec = `search_contract_version` + `generation_batch_id` +
`generation_input_fingerprint` + `candidate_ids` + `budget` + `queue_policy_version`。
刻意不含 `created_at` / `search_run_id` / `worker_id` / `status` / `result` / `score` /
`rank`。`queue_policy_version` 进身份是必须的：否则改 claim 顺序会改变运行语义而不改变
输入 identity。

## 5. exact batch only，且预算不是截断许可

创建必须显式带 `generation_batch_id`，经既有 `get_generation_batch()` /
`list_batch_proposals()` 读取。**没有** latest / recent / current batch 兜底，**没有**
`ORDER BY created_at DESC LIMIT 1`；没有 exact batch 就 FAIL CLOSED。

pool 里每个 candidate 必须再经 `get_candidate()` 自证 —— 不信 `proposal_json` /
`batch candidate_count` / 客户端 `candidate_ids`。

```text
batch 40 + max_candidates=20 + 未显式给 candidate_ids  → FAIL CLOSED
batch 10 + explicit 5 + max_candidates=5                → OK
```

R36-A 没有 selection authority，替用户"删掉哪 20 个"已经是 R36-C 的决定。重复 id 同样
reject（不静默去重）：调用方声称的集合与数量不一致时，去重等于替它猜意图。

## 6. 顺手收掉 candidate unknown-schema 债务

`candidate_from_projection()` 现在是**显式 v1/v2 allowlist**。未知
（`candidate-v999` / `future-v3` / 空版本 / 任意拼写）一律
`unsupported_candidate_schema_version` fail closed，绝不"不是 v2 就按 legacy v1 解释" ——
那条兜底会让未来版本的行在**错误语义**下"自证通过"。v1 历史行与 v2 新行继续自证。

这不是扩大 R36-A：controller 要消费 candidate ledger，反序列化边界必须在此之前关闭。

## 7. queue 状态：append-only events，无可变列

唯一转换权威是一张数据表（不是 if/elif 链）：

```python
ALLOWED_TRANSITIONS = {
    None: {"queued"},
    "queued": {"claimed", "cancelled"},
    "claimed": {"completed", "failed"},
    "failed": {"claimed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
```

`attempt_number` 由 canonical event history **推导**（1 → 2 → …），上限
`max_attempts_per_job ≤ 3`，绝不无限 retry。

R35 的 proposal history 刻意没有 latest；R36 的 job queue **需要** current operational
state，因此显式建立 `event_seq` 作为唯一 ordering authority。`created_at` 与 `event_id`
字典序**绝不**用于推断先后（两条事件可以共享同一 timestamp）。current state 是
**operational projection**，不是实验结果 authority：

```text
job state = completed → executor 成功产生了一份外部 evidence
绝不表示 candidate passed / good / promotable
```

三张表都**没有** `status` / `attempts` / `claimed_at` / `finished_at`，也**没有**任何
实验指标列。`failed` 只表示执行失败（worker error / runner exception / resource
unavailable），不表示 PIT blocked / robustness failed / performance bad。

## 8. 原子创建与并发 claim

`1 run + N job declarations + N queued events` 一个事务完成，任一失败全部回滚，绝不留下
"run 有、jobs 一半、events 另一半"。

`claim_next_job()` 在 `BEGIN IMMEDIATE` 短事务内完成读 run → 计算 current state → 选
下一条 → 检查 attempt 预算 → append claimed 事件 → commit。网络 / 实验计算 / 回测绝不
在该事务里。

claim 顺序 = `candidate_id ASC`（`QUEUE_POLICY_VERSION = "candidate-id-ascending-v1"`），
是中性顺序**不是 ranking**。

## 9. migration v35

```text
experiment_search_runs         PK search_run_id / input fingerprint / batch / budget / spec
experiment_search_jobs         PK job_id = job_fingerprint / UNIQUE(run,candidate,stage)
experiment_search_job_events   PK event_seq AUTOINCREMENT / UNIQUE event_id
```

三张表 append-only（各有 UPDATE / DELETE abort trigger）。FK：jobs→runs、events→jobs，
建表顺序 runs→jobs→events，不使用已被 R35-B 证明不可靠的"事务内
`PRAGMA foreign_keys=OFF`"。迁移 forward-only、幂等、**无历史 backfill**：历史里不存在
"某次 search 调度了哪些实验"这个事实，反推属于捏造 provenance。

## 10. 不做

```text
NO frontend          NO public API          NO 时间预算 / deadline / CPU / token budget
NO priority 字段      NO Bayesian / evolutionary / acquisition_function / crossover
NO 结果表（experiment_search_results / search_candidate_metrics / optimisation_results）
NO 指标列（sharpe / return / drawdown / rank / score / promotion / winner）
NO 读 current/latest strategy 或 research
NO 把 R35 generator 能力搬到 R36
```

## 11. Code review 抓到的两个真实缺陷（已修）

1. **正常 bootstrap 不建 search 表**（P2）：v35 migration 只是**升级**路径，而
   `paper_trading.init_db()` 的两条路径都只建到 `ensure_strategy_candidates`。于是应用
   自己 open / create 的库没有 `experiment_search_*`，第一次 search 写入直接
   `no such table`。现在两条 init_db 路径都幂等建表，并有测试直接跑真实 `init_db()` 后
   **真的写一次 search**；M-SC11 钉住。
2. **batch payload 身份未与查找键核对**（P2）：`get_generation_batch()` 按 id 取行后直接
   返回 `batch_json` 解析结果，**不**校验 payload 自述身份。若某行损坏、或存了另一个
   batch 的 JSON，请求 A 会静默拿到 B 的候选集合，并把 B 的事实记成 A —— 一次"精确
   batch"请求却调度了另一个 batch。现在 service 双向核对 `batch_id` 与 canonical
   `generation_input_fingerprint`，不一致 fail closed；S1c / S1d 覆盖，M-SC12 钉住。

## 12. 验证

```text
R36-A focused tests                 46 tests OK（S1–S22）
candidate schema hardening          v1 PASS / v2 PASS / unknown REJECT
mutation M-SC1…M-SC12               detected = 12/12
                                    survived = 0; fake = 0; timeout = 0
                                    restore SHA256 = PASS; baseline GREEN
```

`M-SC3` 第一版 **SURVIVED**，暴露一个真实缺口：`get_candidate()` 自身已重算指纹自证，
所以单删 service 那行显式 `verify` 无测试可发现；但同一段代码
`candidate is None → continue` 会让**账本里不存在的幽灵 candidate** 进入队列。已补 S3c
（stub `list_batch_proposals` 造幽灵 id，要求稳定 reason 且零行写入），M-SC3 同时打破
两半后 DETECTED。

## 13. 固定维护性报告

```text
Business authority added:
  search request contract
  search queue/event persistence
  search orchestration

Business authority removed:
  none

Candidate identity authority:
  strategy_candidate

Generation batch authority:
  strategy_candidate_repository/service

Search request authority:
  experiment_search_contract

Queue state authority:
  experiment_search_job_events.event_seq

Experiment result authority:
  unchanged / R28-R30

Selection authority:
  NOT ADDED

Promotion authority:
  NOT ADDED

New facade/wrapper:
  0

New AI-specific table:
  0

New search-control tables:
  3

Implicit latest/current candidate batch lookup:
  0

Implicit latest/current strategy lookup:
  0

Mutable queue status columns:
  0

Direct search-run DB write sites:
  before 0 / after 1

Direct search-job DB write sites:
  before 0 / after 1

Direct job-event DB write sites:
  before 0 / after 1

Evaluation metric columns in controller:
  0

Search controller -> evaluation runner dependency:
  NO

Search controller -> promotion dependency:
  NO

Search controller -> execution dependency:
  NO

Search controller -> AI provider dependency:
  NO

Large if/elif state machine:
  NO
  （prefer transition table）

Modules needed to understand core R36-A rule:
  3

paper_trading.py LOC/defs:
  trend only, not blocker
```
