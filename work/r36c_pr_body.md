# feat(experiment): add evidence-bound candidate selection policy

R36-C。前置：R36-B2（`feat(experiment): run candidate robustness from exact PIT evidence`）。

R36-C exact base SHA: 3fede53025d758c3d1ffd5bfc168960b2d811f7e
R36-C exact head SHA: (the pushed PR head; recorded in the GitHub PR description)
分支：`codex/r36c-selection-iteration-policy`

```text
R34     COMPLETE
R35     COMPLETE

R36-A   COMPLETE
R36-B1  COMPLETE
R36-B2  COMPLETE
R36-B   COMPLETE

R36-C   IN REVIEW

R36-D   NOT STARTED
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
```

## 1. 核心链路

R36-B1/B2 让候选拥有 canonical 的 R29 PIT 与 R30 robustness 证据；R36-C 是第一个允许把
这些 exact 证据转成**搜索局部**选择决策的阶段。

```text
exact candidate set + exact R29 evidence + exact R30 evidence
+ selection policy（在观察实验结果之前 pin 定）
    ↓ eligibility gates
deterministic Pareto fronts
    ↓
eliminate / retain / advance
```

八句边界：

```text
Selection consumes canonical evidence; it does not manufacture
or reinterpret R29/R30 facts.

Operational failure or missing execution evidence is not candidate
underperformance and must not become elimination.

Selection policy is pinned in the search request before experiment
results are observed.

Selection policy is not part of experiment identity; changing a
selection policy must not change the R29/R30 experiment fingerprints.

Pareto front membership is a search-local selection fact, not
promotion, lifecycle state, or a global strategy ranking.

Candidate-id ordering is serialization order only and has no
selection meaning.

An "advance" disposition means only that R36-D may consume the
candidate as a next-generation parent input.

Only R31 owns strategy promotion.
```

**硬边界：`advance` 只是 R36-D 的输入资格。** 它绝不表示 promoted / validated lifecycle
state / shadow / paper / production_sim / champion / winner / best strategy。

## 2. authority 报告

```text
Candidate identity authority:
  strategy_candidate

Candidate PIT evidence authority:
  existing R29

Candidate robustness evidence authority:
  existing R30

Search request authority:
  experiment_search_contract

Selection policy authority:
  candidate_selection

Pareto/disposition authority:
  candidate_selection

Selection persistence authority:
  candidate_selection_repository

Selection orchestration authority:
  candidate_selection_service

Search queue authority:
  experiment_search_repository

Promotion authority:
  UNCHANGED / NOT CALLED

Lifecycle authority:
  UNCHANGED / NOT CALLED

Next-generation generation authority:
  NOT ADDED

AI authority:
  NOT USED
```

## 3. 新增 / 变更接口

**`backend/candidate_selection.py`**（新）：纯领域。`CandidateSelectionPolicy`、
`CandidateSelectionEvidence`、`CandidateSelectionReport`、`evaluate_selection(...)`。
`SELECTION_POLICY_VERSION = candidate-selection-policy-v1`、
`SELECTION_REPORT_VERSION = candidate-selection-report-v1`。**无** DB、无网络、无文件系统、
无 `datetime.now()`、无策略注册表、无 current/latest 查找、无 lifecycle/promotion/AI。
策略字段：`policy_version`；`objectives`（闭集 allowlist、canonical 排序、拒绝重复、至少
一个）；`min_baseline_return`；`max_baseline_drawdown_abs`；`min_trade_count`；
`min_data_coverage`；`require_no_robustness_unavailable`；
`require_no_robustness_failed`；`require_no_threshold_breaches`；
`max_observed_fragilities`；`advance_through_front`；`retain_through_front`。业务字段全部
显式（service 无隐藏默认），`None` 表示"该 gate 被显式关闭"。目标词汇闭集且**方向固定**
（caller 不能声明方向）：`baseline_return` MAXIMIZE；`baseline_drawdown_abs` MINIMIZE；
`baseline_turnover` MINIMIZE；`robustness_worst_return` MAXIMIZE；
`robustness_worst_drawdown_abs` MINIMIZE；`robustness_max_return_degradation` MINIMIZE；
`robustness_fragility_count` MINIMIZE。**没有** weighted score / composite score /
utility function / AI ranking / epsilon dominance。

**`backend/candidate_selection_repository.py`**（新）：append-only canonical
selection-report 持久化，唯一新表 `experiment_search_selection_reports`。

**`backend/candidate_selection_service.py`**（新）：exact R29/R30 证据闭包与编排，
`select_search_candidates(...)`。

**`backend/experiment_search_contract.py`**：新增 `experiment-search-contract-v3` = v2 +
`selection_policy` + `selection_policy_fingerprint`。`candidate-experiment-plan-v2` 逐字节
不变；v1/v2 projection/fingerprint 冻结。v2 search 仍 PIT/R30 可执行，但 selection 不可用，
稳定 reason `search_run_selection_policy_unavailable`。只改 selection policy **必须**改变
`search_input_fingerprint`，但**必须不**改变 `CandidateExperimentSpec` fingerprint、R29
experiment fingerprint、R30 `RobustnessPlan` fingerprint。

**`backend/paper_schema_migrations.py`** / **`backend/db_migrate.py`** /
**`backend/paper_trading.py`**：migration **v36** 建 `experiment_search_selection_reports`
（append-only、幂等、**无 backfill**）；正常 `init_db()` / fresh bootstrap 也创建该表。

## 4. 证据闭包与 disposition

**whole-search gate：** candidate pool 只来自 exact `search_spec.candidate_ids`，每个候选
经 `get_candidate` 自证。每个候选需要 exact `pit_validation` job；PIT job
failed/queued/claimed/cancelled → **整次拒绝** `selection_operational_evidence_incomplete`
（运营失败绝不等于淘汰；cancelled 也 fail-closed）。completed PIT job 且
`evidence_owner=experiment_validation_run`、`evidence_id=exact run_key` 时经
`get_run(run_key=...)` 重读；canonical R29 blocked / `result != completed` 是**真实证据** →
ineligible / eliminate，**不**需要 R30。READY R29 候选必须有 exact robustness job，缺失或
其 job 非 completed → **整次拒绝** `selection_robustness_stage_incomplete`。completed
robustness job 经 `get_report_by_key` 重读并重核 R29↔R30 绑定（candidate_id、baseline_run_key、
baseline experiment fingerprint、baseline result fingerprint、embedded subject、pinned
robustness plan fingerprint），复用既有 canonical helper，不复制 B2 逻辑。**任何地方都没有**
recent/latest/`ORDER BY DESC LIMIT 1`/`MAX(created_at)`/current 查找。

**Pareto / disposition：** 非支配排序只用 strict deterministic comparison；同一 front 同一
disposition（front 是原子选择单位，**没有** top-N hash 截断）；`candidate_id ASC` 只是序列化
顺序，不是 ranking。`eligible=false` → eliminate；`pareto_front <= advance_through_front` →
advance；`<= retain_through_front` → retain；否则 eliminate；
`next_generation_eligible = (disposition == advance)`；ineligible 候选 `pareto_front=null`、
`next_generation_eligible=false`。

**report：** `selection_report_key = sha256({report_version, search_run_id,
search_input_fingerprint, selection_policy_fingerprint, evidence_set_fingerprint})`；
`created_at` 在身份之外。全员 blocked 是合法 report（`eligible_count=0`、
`eliminate_count=N`）；但 operational evidence incomplete **不得**产生 report。

## 5. 不做（NON-SCOPE）

```text
NO Bayesian optimization              NO evolutionary mutation / crossover
NO acquisition functions              NO candidate generation / mutation
NO new generation batches             NO automatic research prompts
NO AI proposal loops                  NO promotion
NO lifecycle transition               NO challenger creation
NO shadow activation                  NO weighted score
NO single winner                      NO top-N hash truncation
NO portfolio allocation               NO frontend
NO public HTTP API
```

R36-C **不 import** `strategy_promotion` / `promotion_science` / `strategy_lifecycle`，也
**不 import** `adaptive_selection` / `adaptive_selection_compat` / `selection_runner` /
`paper_selection` / `strategy_selection_resolver` / `strategy_selection_provenance` /
`selection_tracking` / `learning_evaluation`。

## 6. 固定维护性报告

```text
Business authorities added:
  candidate selection policy
  candidate selection report
  candidate selection persistence/orchestration

Duplicate R29 evaluator:
  NO

Duplicate R30 evaluator:
  NO

Duplicate runtime selection engine:
  NO

New permanent DB tables:
  1

New DB write authorities:
  1 selection-report owner

New facade/wrapper:
  0

Implicit latest R29 lookup:
  0

Implicit latest R30 lookup:
  0

Implicit current strategy lookup:
  0

Weighted scalar score:
  NO

Candidate-id semantic tie breaker:
  NO

Selection writes candidate ledger:
  NO

Selection writes lifecycle:
  NO

Selection calls promotion:
  NO

Selection calls AI:
  NO

Queue receives selection results:
  NO

Direct selection-report DB write sites:
  1

Modules needed to understand core R36-C rule:
  target <= 3

paper_trading.py LOC/defs:
  trend only, not blocker
```

## 7. 验证

```text
R36-C focused tests                  59 tests (test_r36c_candidate_selection) PASS
R36-B2 regression                    59 tests PASS
R36-B1 regression                    41 tests PASS
R36-A regression                     49 tests PASS
R29 regression                       PASS
R30 regression                       PASS (formal identity frozen)

M-SEL                                27/27 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-CRB                                24/24 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-CEX                                21/21 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-SC                                 15/15 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-G                                   8/8 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-X                                  10/10 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-AIG                                17/17 DETECTED; survived=0; fake=0; timeout=0; restore=PASS

backend full suite                   5676 tests OK (skipped=5)
frontend suite                       174/174 pass; frontend/dist unchanged
docker --network none                CI docker-smoke (recorded on the GitHub PR)
browser e2e                          CI browser-e2e (chromium) (recorded on the GitHub PR)
security scan                        PASS (0 findings; 2 pre-existing image manual-review entries)
exact-head CI                        recorded on the GitHub PR
unresolved threads                   recorded on the GitHub PR

selection report count               1 (focused E2E)
eligible count                       2
advance count                        2
retain count                         0
eliminate count                      0
candidate ledger row delta           0
lifecycle event delta                0
promotion proposal delta             0
```

本 PR body 不编造最终 exact head SHA / CI 结论：它们记录在 GitHub PR 上（本文件的 head
行在 push 后回填）。

## 8. 状态

```text
R34     COMPLETE
R35     COMPLETE

R36-A   COMPLETE
R36-B1  COMPLETE
R36-B2  COMPLETE
R36-B   COMPLETE

R36-C   IN REVIEW

R36-D   NOT STARTED
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
```
