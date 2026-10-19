# R36-C — Candidate Selection & Iteration Policy

## 1. 目标与范围

R36-B1/B2 让 `StrategyCandidate` 拥有 canonical 的 R29 PIT 证据与 R30 robustness 证据。
但**证据不是选择**。R36-C 是第一个允许把 exact R29/R30 证据转成**搜索局部**选择决策的
阶段。

```text
exact candidate set + exact R29 evidence + exact R30 evidence
+ selection policy（在观察实验结果之前 pin 定）
    ↓ eligibility gates
deterministic Pareto fronts
    ↓
eliminate / retain / advance
```

范围：

```text
SELECTION POLICY（搜索请求内 pin 定）
ELIGIBILITY GATES（baseline / robustness / metric availability）
DETERMINISTIC PARETO FRONTS（strict deterministic comparison）
DISPOSITIONS（eliminate / retain / advance）
CANONICAL SELECTION REPORT（append-only，per-search 唯一）
SEARCH-LEVEL EVIDENCE CLOSURE（whole-search gate）
```

## 2. authorities

```text
candidate_selection.py            纯搜索选择 contract / gates / Pareto fronts / report identity
candidate_selection_repository.py append-only canonical selection-report owner
candidate_selection_service.py    exact R29/R30 evidence binding 与 search-level orchestration
```

依赖方向：

```text
candidate_selection_service
    ↓
candidate_selection (pure)
candidate_selection_repository
experiment_search_repository
experiment_validation_repository
robustness_repository
strategy_candidate_repository
candidate_experiment
```

**禁止 import**（其它权威）：

```text
strategy_promotion            promotion_science        strategy_lifecycle
adaptive_selection            adaptive_selection_compat
selection_runner              paper_selection
strategy_selection_resolver   strategy_selection_provenance
selection_tracking            learning_evaluation
```

这些分别是：promotion / lifecycle、legacy/adaptive 参数选择、runtime 选股、paper account
选择、runtime 策略 provenance、shadow learning evaluation。R36-C 一个都不调用。

## 3. 身份边界：search v3 vs experiment plan v2

选择策略 pin 在 **search request identity**，不在 experiment plan。

```text
candidate-experiment-plan-v2      逐字节不变
experiment-search-contract-v3     = v2 + selection_policy + selection_policy_fingerprint
```

- v1 / v2 的 projection/fingerprint **冻结**。
- v2 search 仍 PIT/R30 可执行，但 selection **不可用**，稳定 reason
  `search_run_selection_policy_unavailable`。
- 只改 selection policy **必须**改变 `search_input_fingerprint`，但**必须不**改变：
  `CandidateExperimentSpec` fingerprint、R29 experiment fingerprint、R30
  `RobustnessPlan` fingerprint。
- 带 `selection_policy` 必须 v3；v2 携带 policy 或 v3 缺 policy 一律拒绝。

## 4. 证据闭包（whole-search gate）

```text
candidate pool = exact search_spec.candidate_ids（每个再 get_candidate 自证）
每个候选需要 exact pit_validation job（canonical get_search_job）
PIT job failed/queued/claimed/cancelled → REFUSE WHOLE SELECTION
    reason = selection_operational_evidence_incomplete
completed PIT job：evidence_owner=experiment_validation_run、evidence_id=exact run_key
    → ExperimentValidationRepository.get_run(run_key=...)
canonical R29 blocked / result != completed → REAL evidence（ineligible, eliminate）
    reasons pit_validation_not_ready / pit_result_not_completed；不需要 R30
READY R29（validation_status=ready AND result.status=completed）必须有 exact robustness job
    缺失 → REFUSE WHOLE SELECTION（selection_robustness_stage_incomplete）
    其 job queued/claimed/failed/cancelled → REFUSE WHOLE SELECTION（缺执行事实）
completed robustness job：evidence_owner=robustness_report、evidence_id=exact report_key
    → RobustnessRepository.get_report_by_key
重新核验 R29↔R30 绑定（复用既有 canonical helper，不复制 B2 逻辑）
```

**运营失败或缺失执行证据不是候选 underperformance，绝不变成 elimination。**
**任何地方都没有** recent_runs / recent_reports / `ORDER BY DESC LIMIT 1` /
`MAX(created_at)` / latest / current 结果查找。

## 5. policy contract

```text
policy_version
objectives                      闭集 allowlist，canonical 排序，拒绝重复，至少一个
min_baseline_return             None = gate 关闭（显式契约语义）
max_baseline_drawdown_abs
min_trade_count
min_data_coverage
require_no_robustness_unavailable
require_no_robustness_failed
require_no_threshold_breaches
max_observed_fragilities
advance_through_front           >= 1
retain_through_front            >= advance_through_front
```

业务字段全部**显式**（service 里没有隐藏默认）。`None` 表示"该 gate 被显式关闭"。

**闭集目标词汇 + 固定方向（caller 不能声明方向）：**

```text
baseline_return                      MAXIMIZE
baseline_drawdown_abs                MINIMIZE
baseline_turnover                    MINIMIZE
robustness_worst_return              MAXIMIZE
robustness_worst_drawdown_abs        MINIMIZE
robustness_max_return_degradation    MINIMIZE
robustness_fragility_count           MINIMIZE
```

**没有** weighted score / composite score / utility function / AI ranking / epsilon
dominance。

**固定 robustness 聚合**（只有 completed case 参与）：

```text
robustness_worst_return            = MIN(case.metrics.return over completed cases)
robustness_worst_drawdown_abs      = MAX(abs(case.metrics.drawdown) over completed cases)
robustness_max_return_degradation  = MAX(max(0, -case.baseline_delta.return) over completed cases)
```

unavailable/failed case 计入 gate，但**绝不**进入 worst metrics，**绝不**用 0 或 baseline
填充。缺 metric → `selection_metric_unavailable` / `selection_objective_unavailable`，
**绝不**当 0。

## 6. Pareto 语义

- Pareto 非支配排序只用 strict deterministic comparison。
- 同一 front = 同一 disposition（front 是原子选择单位，**没有** top-N hash 截断）。
- `candidate_id ASC` 只是序列化顺序，**不是** ranking，**不是** tie breaker。
- 输入顺序不影响 `report_fingerprint` 与 front 分配。

## 7. disposition 语义

```text
eligible = false                                          → eliminate
eligible = true  and pareto_front <= advance_through_front → advance
eligible = true  and advance_through_front < front
                 <= retain_through_front                   → retain
eligible = true  and pareto_front > retain_through_front   → eliminate

next_generation_eligible = (disposition == advance)
ineligible → pareto_front = null, next_generation_eligible = false
```

**"advance" 只表示"可作为 R36-D 下一代搜索的输入"**，绝不表示 promoted / validated
lifecycle state / shadow / paper / production_sim / champion / winner / best strategy。

## 8. selection report

```text
report_version; search_run_id; search_input_fingerprint
selection_policy; selection_policy_fingerprint; evidence_set_fingerprint
candidate_count; eligible_count; advance_count; retain_count; eliminate_count
candidates[]: candidate_id, pit_run_key, pit_result_fingerprint,
              robustness_report_key, robustness_report_fingerprint,
              eligibility, blocking_reasons, selection_features,
              pareto_front, disposition, next_generation_eligible

selection_report_key = sha256({report_version, search_run_id, search_input_fingerprint,
                               selection_policy_fingerprint, evidence_set_fingerprint})
created_at 在身份之外
```

全员 blocked 是合法 report（`eligible_count=0`、`eliminate_count=N`）；但 operational
evidence incomplete **不得**产生 report。

版本：

```text
SELECTION_POLICY_VERSION = candidate-selection-policy-v1
SELECTION_REPORT_VERSION = candidate-selection-report-v1
```

## 9. migration v36（无 backfill）

一张新永久表 `experiment_search_selection_reports`：

```text
report_key PRIMARY KEY
search_run_id UNIQUE（FK → experiment_search_runs）
search_input_fingerprint
selection_policy_fingerprint
evidence_set_fingerprint
report_version
candidate_count / eligible_count / advance_count / retain_count / eliminate_count
report_json
created_at
payload_fingerprint
```

- **没有** scalar score / weighted_score / utility / winner / best_candidate / promotion /
  promotable 列。
- append-only（**无** UPDATE、**无** DELETE trigger）。
- 每次 search 恰好一份 canonical report；相同 retry 幂等；同 search 不同 report →
  `selection_report_conflict`；并发相同写入幂等（INSERT、catch unique conflict、重读
  winner、比较 exact payload）。
- 直接写入权威 = `candidate_selection_repository`，写入点 = 1。
- migration 为 **v36**（#233 合并后 max 为 v35）；**绝不**回填历史 selection 行（0 行才是
  正确历史）。正常 `init_db()` / fresh bootstrap 也创建该表。

## 10. 测试（SEL-01..SEL-41）

永久测试 `backend/test_r36c_candidate_selection.py`：policy contract、v1/v2/v3 身份、exact
evidence closure、operational fail-closed、R29↔R30 绑定、fixed directions、missing metric、
baseline/robustness gates、Pareto fronts、dispositions、scale、report identity、append-only、
自证读取、幂等/冲突/并发、无 score/winner、无 promotion/lifecycle/AI 依赖、migration v36 与
bootstrap。

详细逐项见 [`../docs/TEST_MATRIX.md`](../docs/TEST_MATRIX.md) 的 R36-C 段。

## 11. mutations（M-SEL1..M-SEL24）

M-SEL1–M-SEL24 分别破坏 exact 证据读取、policy 身份、operational fail-closed、R29↔R30
绑定、objective 方向、metric 语义、front 原子性与 report 持久化。每例编译真实 mutant、
运行对应 detector、恢复原始字节并核对 SHA256，最后重跑基线。要求 detected=24/24，
survived=fake=timeout=0。详细列表见 `docs/TEST_MATRIX.md`。

## 12. 明确 non-scope

```text
NO Bayesian optimization
NO evolutionary mutation / crossover
NO acquisition functions
NO candidate generation / mutation
NO new generation batches
NO automatic research prompts
NO AI proposal loops
NO promotion
NO lifecycle transition
NO challenger creation
NO shadow activation
NO weighted score
NO single winner
NO top-N hash truncation
NO portfolio allocation
NO frontend
NO public HTTP API
```

R36-C **不 import** `strategy_promotion` / `promotion_science` / `strategy_lifecycle`，也
**不 import** `adaptive_selection` / `adaptive_selection_compat` / `selection_runner` /
`paper_selection` / `strategy_selection_resolver` / `strategy_selection_provenance` /
`selection_tracking` / `learning_evaluation`。

**"advance" 只是 R36-D 的输入资格**；promotion 永远属于 R31。
