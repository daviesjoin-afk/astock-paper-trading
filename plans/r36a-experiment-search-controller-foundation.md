# R36-A — Experiment Search Controller Foundation

## 0. 四句边界

```text
A queued experiment is not an evaluation result.
A completed queue job does not mean a candidate passed.
Search order is not candidate ranking.
The search controller may schedule evidence production;
it may not manufacture evidence or promotion authority.
```

## 1. 为什么必须先做控制面

R35 结束时仓库已有：

```text
R28  ExperimentSpec / ExperimentResult
R29  PIT validation + experiment_validation_runner/repository
R30  robustness plan + robustness runner/repository
R35  StrategyCandidate / CandidateSearchSpace / generation batch
     proposal provenance / AI hypothesis → constrained candidates
```

缺的是中间这层：

```text
一批 candidate → 谁应该被实验？→ 预算是多少？→ 当前排了哪些实验工作？
→ 哪个工作已领取？→ 失败能否重试？→ 还剩多少预算？
```

R36-A 只建立这个 **control plane**。它**不**回答"哪个 candidate 更好 / Sharpe 更高 /
应该晋级" —— 那些属于 R36-C 与 R31。

## 2. 本轮明确不运行 R29/R30

R36-A **不执行** `experiment_validation_runner.run_validation()` /
`robustness_runner.run_robustness()`。

原因：当前 `ExperimentSpec` 的 canonical strategy identity 是正式 `StrategyVersion`，而
R35 candidate 可以改变 `entry_spec` / `exit_spec` / `factor_spec` / `parameter_spec`。
若拿：

```text
parent strategy checksum + candidate parameters
```

冒充 candidate experiment identity，则两个"不同 entry/exit/factor、但同 parent +
parameters"的候选可能得到**同一** experiment identity。

**R36-B 专门解决 StrategyCandidate → canonical experiment subject 的桥接**；R36-A 不提前
伪造这层。

authority 分工不允许合并：

```text
R36-A   只调度“需要验证什么”
R29/R30 决定“验证事实是什么”
R36-C   才决定“根据事实选谁”
R31     决定“谁可以晋级”
```

## 3. 三个 production module = 三个真实 authority

```text
backend/experiment_search_contract.py     一次 bounded search request 是什么（纯契约）
backend/experiment_search_repository.py   run / job / event 三张台账的持久化 owner
backend/experiment_search_service.py      batch → pool → run+jobs → claim 的编排
```

依赖方向只能是：

```text
experiment_search_service
    ↓
experiment_search_contract
experiment_search_repository
strategy_candidate_repository
```

禁止反向（`strategy_candidate` 不得依赖 search service），也禁止依赖
`experiment_validation_runner` / `robustness_runner` / promotion / strategy_lifecycle /
execution / portfolio / AI provider。

刻意**不**再拆 `search_manager` / `queue_manager` / `job_helper` / `budget_utils` /
`search_facade` / `controller_common`：三个模块已足够表达三个 authority。

## 4. 身份设计

```text
SearchSpec fingerprint   = sha256(canonical SearchSpec)   content identity
search_run_id            = secrets.token_hex(32)          request event identity
job_id = job_fingerprint = sha256(run + candidate + stage + contract)  声明身份
event_id                 = secrets.token_hex(32)         事件身份
event_seq                = INTEGER PRIMARY KEY AUTOINCREMENT  运营 ordering authority
```

同一个完全相同的 SearchSpec 今天跑一次、明天再跑一次：

```text
search_input_fingerprint 相同
search_run_id 不同
```

因此 `search_run_id` 绝不能用 timestamp / PID / 进程内计数器 / 内容 hash 冒充。

SearchSpec 包含：`search_contract_version` / `generation_batch_id` /
`generation_input_fingerprint` / `candidate_ids` / `budget` / `queue_policy_version`。
刻意**不**包含：`created_at` / `search_run_id` / `worker_id` / `status` / `result` /
`score` / `rank`。

`queue_policy_version` 必须进身份：否则未来修改 claim 顺序会改变一次 search 的运行语义，
却不改变输入 identity。

## 5. exact generation batch only

创建 search run 必须显式带 `generation_batch_id`，经既有
`strategy_candidate_repository.get_generation_batch()` /
`list_batch_proposals()` 读取。

禁止 `latest batch` / `recent batch` / `current candidate batch`，禁止
`ORDER BY created_at DESC LIMIT 1` 寻找输入。没有 exact batch 就 FAIL CLOSED。

candidate pool 来自 exact batch，且**每个** candidate 必须再经 `get_candidate()` 自证。
禁止只相信 `proposal_json` / `batch candidate_count` / 客户端 `candidate_ids`。

预算不是截断许可：

```text
batch 40 candidates + max_candidates=20 + 未显式给 candidate_ids  → FAIL CLOSED
batch 10 candidates + candidate_ids = explicit 5 + max_candidates=5 → OK
```

显式子集要求：每个 ID canonical、无重复、全部属于该 batch、全部能自证。输入顺序不承载
语义，存储前按 `candidate_id` 升序，因此 `[A,B,C]` 与 `[C,A,B]` 得到同一 fingerprint。

## 6. candidate unknown-schema hardening（顺手收掉的 follow-up）

`candidate_from_projection()` 现在只接受**显式 allowlist**：

```text
v1 (strategy-candidate-v1)
v2 (strategy-candidate-v2)
```

未知（`candidate-v999` / `future-v3` / 空版本 / 任意拼写）一律
`unsupported_candidate_schema_version` fail closed。绝不"不是 v2 就按 legacy v1 解释" ——
那条兜底会让未来版本的行在**错误语义**下"自证通过"。v1 历史行与 v2 新行继续自证。

这不是扩大 R36-A：controller 要消费 candidate ledger，反序列化边界必须在此之前关闭。

## 7. 状态机用数据表达

唯一转换权威是一张表：

```python
ALLOWED_TRANSITIONS = {
    None:     {"queued"},
    "queued": {"claimed", "cancelled"},
    "claimed": {"completed", "failed"},
    "failed":  {"claimed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
```

`attempt_number` 由 canonical event history **推导**，调用方无权声明。第一次 claim = 1，
失败后重试 = 2，上限 `SearchBudget.max_attempts_per_job`（≤3），绝不无限 retry。

`failed` 只表示执行失败（worker error / runner exception / resource unavailable），
**不**表示 PIT blocked / robustness failed / candidate performance bad。业务结论属于
`experiment_validation_repository`。

## 8. 原子创建与并发 claim

创建时 `1 search run + N job declarations + N queued events` 必须一个事务完成，任一失败
全部回滚，不能留下"run 有、jobs 一半、queued events 另一半"。

`claim_next_job(search_run_id, actor)` 必须在 `BEGIN IMMEDIATE` 短事务里完成：
读 run → 计算所有 job current state → 选下一条 → 检查 attempt 预算 → append claimed
事件 → commit。网络 / 实验计算 / 回测**绝不**在该事务里。

claim 顺序是 `candidate_id ASC`（`QUEUE_POLICY_VERSION = "candidate-id-ascending-v1"`），
是**中性顺序而非 ranking**。

## 9. 三项表（migration v35）

```text
experiment_search_runs         search_run_id PK / input fingerprint / batch / budget / spec
experiment_search_jobs         job_id PK = job_fingerprint / UNIQUE(run,candidate,stage)
experiment_search_job_events   event_seq PK AUTOINCREMENT / event_id UNIQUE
```

三张表都是 append-only，各有 `BEFORE UPDATE` / `BEFORE DELETE` abort trigger。
FK：`jobs.search_run_id → runs`、`events.job_id → jobs`（建表顺序 runs → jobs → events，
不使用已被 R35-B 证明不可靠的"事务内 PRAGMA foreign_keys=OFF"）。

迁移 forward-only、幂等、**无历史 backfill**：升级前的历史里不存在"某次 search 调度了
哪些实验"这个事实，从既有 candidates 或历史 validation runs 反推属于捏造 provenance。

## 10. current state 的语义边界

```sql
MAX(event_seq) for exact job_id
```

这是 **operational queue projection**，不是实验业务结果 authority：

```text
job state = completed → executor 成功产生了一份外部 evidence
绝不表示 candidate passed / good / promotable
```

禁止在 `experiment_search_jobs` 加 `status` / `attempts` / `claimed_at` / `finished_at`
然后 UPDATE —— 那会造成"append-only 证据 + 可变快照"两套 authority。

## 11. 不做的事

```text
NO frontend（先钉 identity / budget / queue / concurrency / event ordering）
NO public API（只提供 connection-explicit service functions）
NO 时间预算（max_wall_seconds / deadline / timeout / CPU / token）
NO priority 字段
NO Bayesian / evolutionary / acquisition_function / mutation_operator / crossover
NO 把 R35 generator 能力搬到 R36
NO experiment_search_results / search_candidate_metrics / optimisation_results
NO evaluation metric 列（sharpe / return / drawdown / rank / score / promotion / winner）
NO 读 current/latest strategy 或 current/latest research
```

时间预算会立刻把机器负载、wall clock、worker capability 带进 search identity；priority 会
立刻变成"哪个 candidate 更值得先测"；算法在没有可靠 budget / queue / event history /
evidence binding 之前只会产生无法审计的自动优化循环。

## 12. 测试与 mutation

测试矩阵 S1–S22（`backend/test_r36a_experiment_search_controller.py`），覆盖 exact batch
only、pool 归属、自证、schema allowlist、order-independence、duplicate、budget no
truncation、content vs event identity、原子创建、deterministic job id、初始态、claim 状态
机、retry 预算、终态、event_seq ordering、非 ranking 顺序、并发、无指标、无 evaluation
依赖、无 AI 依赖。

mutation 前缀 `M-SC1`…`M-SC10`（`work/r36a_search_mutation_check.py`）。

**M-SC3 第一版 SURVIVED，暴露一个真实缺口**：`get_candidate()` 自身已会重算指纹自证，
因此单删 service 那行显式 `verify` 不会被任何测试发现；但同一段代码里
`candidate is None → continue` 会让一个**账本里不存在的幽灵 candidate** 进入队列。现在
补了 S3c（stub `list_batch_proposals` 造幽灵 id，要求 `candidate_ledger_self_verification_failed`
且零行写入），M-SC3 同时打破两半后 DETECTED。

## 13. Code review 抓到的两个真实缺陷（已修）

1. **正常 bootstrap 不建 search 表**：v35 migration 只是**升级**路径，而
   `paper_trading.init_db()` 的两条路径都只建到 `ensure_strategy_candidates`。于是应用
   自己 open / create 的库没有 `experiment_search_*`，第一次 search 写入直接
   `no such table`。现在两条 init_db 路径都幂等建表，并由
   `test_normal_bootstrap_creates_the_search_tables` 直接跑真实 `init_db()` 后**真的写一次
   search**（M-SC11 钉住）。
2. **batch payload 身份未与查找键核对**：`get_generation_batch()` 按 id 取行后直接返回
   `batch_json` 解析结果，**不**校验 payload 自述身份。若某行损坏或存了另一个 batch 的
   JSON，请求 A 会静默拿到 B 的候选集合，并把 B 的事实记成 A。现在 service 双向核对
   `batch_id` 与 canonical `generation_input_fingerprint`，不一致 fail closed
   （S1c / S1d，M-SC12 钉住）。

## 14. 合并后路线

```text
R36-B  Candidate Experiment Execution（StrategyCandidate → canonical experiment subject
       → ExperimentSpec v2 → R29 PIT validation → exact evidence → R30 robustness）
R36-C  Explicit Selection & Iteration Policy
R36-D  Bayesian / Evolutionary Search
R37    Closed-loop Learning
```
