# feat(experiment): run candidate robustness from exact PIT evidence

R36-B2。前置：R36-B1（`feat(experiment): execute candidate subjects through PIT validation`）已合并。

```text
R34     COMPLETE
R35     COMPLETE
R36-A   COMPLETE
R36-B1  COMPLETE

R36-B2  IN REVIEW
R36-B   IN PROGRESS

R36-C   NOT STARTED
R36-D   NOT STARTED
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
```

R36-B2 exact base SHA: 5ed22af508abd6fce44fb29890eca2ae1f948d15
R36-B2 exact head SHA: 39c3e9690a8dac1eeed4635f8e784b50f69e58ca
分支：`codex/r36b2-candidate-robustness-execution`

## 1. 核心链路

R36-B1 让 `StrategyCandidate` 拥有 canonical experiment subject 与真实 R29 PIT 证据。
R36-B2 把这条证据接到**既有** R30 robustness runner，**不新增 runner、不新增执行循环、
不新增 PIT validator**。

```text
exact canonical READY R29 baseline（candidate subject）
    ↓
RobustnessPolicy（候选执行前 pin 定；同一 search 内所有候选共享）
    ↓ bind(exact baseline_run_key, baseline_experiment_fingerprint)
RobustnessPlan
    ↓ existing R30 robustness_runner（候选路径 candidate-replay-v1）
canonical R30 report
    ↓ exact report_key
robustness queue job completed
```

**硬边界：robustness evidence ≠ selection decision。** 一份 canonical R30 report 说明
扰动下发生了什么，**不**说明候选 passed / good / promotable。

```text
Robustness starts from an exact canonical READY R29 baseline;
it never reconstructs or guesses a baseline.

A robustness policy is pinned before candidate execution and is
shared across candidates in the same search.

A candidate robustness report remains evidence about the same
StrategyCandidate; perturbations do not create or mutate candidates.

A completed robustness job means a canonical R30 report exists.
It does not mean the candidate passed robustness.

R36-B2 may produce robustness facts.
Only R36-C may interpret those facts for selection.
```

## 2. authority 报告

```text
Candidate identity authority:
  strategy_candidate

Candidate replay authority:
  candidate_experiment

Candidate PIT authority:
  existing R29

Robustness policy authority:
  robustness_contract

Robustness plan authority:
  existing RobustnessPlan

Robustness execution authority:
  existing robustness_runner

Robustness evidence authority:
  existing robustness_repository

Search queue authority:
  experiment_search_repository

Selection authority:
  NOT ADDED

Ranking authority:
  NOT ADDED

Promotion authority:
  NOT ADDED

AI authority:
  NOT USED
```

## 3. 新增 / 变更接口

`backend/robustness_contract.py`：新增 immutable `RobustnessPolicy`
（`ROBUSTNESS_POLICY_VERSION = "r30-robustness-policy-v1"`）。字段恰好是既有
`RobustnessPlan` 的 stress policy 减去 `baseline_run_key` /
`baseline_experiment_fingerprint` / `created_at`（`random_seed`、`regime_policy`、
cost / slippage / execution_delay / signal_delay / liquidity / missing_data / parameter /
start_date / end_date / universe stresses、`allowed_parameter_paths`、
`max_drawdown_limit`、`max_return_degradation`）。提供 canonical `projection()`、
`fingerprint` 与 `bind(baseline_run_key, baseline_experiment_fingerprint) -> RobustnessPlan`。
既有 `RobustnessPlan` 的 projection/fingerprint/scenarios **逐字节不变**，只复用同一套
校验规则。

`backend/experiment_search_contract.py`：新增 `ExperimentSearchPlanV2`
（`candidate-experiment-plan-v2`）= v1 全部 + `robustness_policy` + 派生
`robustness_policy_fingerprint`；v1 `ExperimentSearchPlan` 的 projection/fingerprint 不变。
新增 `RobustnessSearchJobSpec`（`experiment-search-robustness-job-contract-v1`，stage
`robustness`），job id hash search_run_id、candidate_id、stage、baseline_run_key、
baseline_experiment_fingerprint、robustness_plan_fingerprint、job_contract_version。
新增统一 decoder `search_job_from_projection()`，按 `(stage, job_contract_version)` 分派：
`pit_validation` v1 → `SearchJobSpec`，`robustness` v1 → `RobustnessSearchJobSpec`，
其余 fail closed。`JOB_STAGES` 现在列出两个 stage，但 `SearchJobSpec` 仍只接受
`pit_validation`。

`backend/experiment_search_repository.py`：新增 `get_search_job(conn, job_id)`
自证精确读取（job_json / job_id / job_fingerprint / search_run_id / candidate_id /
stage / job_contract_version，任一不符 → `corrupt_search_job`）；新增
`record_verified_robustness_completion_event(...)`，evidence_owner 固定
`robustness_report`、evidence_id 固定 exact report_key。通用
`record_job_event(completed)` 继续拒绝。**没有新表、没有 migration。**

`backend/experiment_search_service.py`：新增通用 `fail_claimed_job(...)`；B1 与 B2 的
队列运营失败都走它。

`backend/robustness_runner.py`：现在同时支持正式 `StrategyVersion` 与
`StrategyCandidate`。正式路径/身份不变（runner/report version
`r30-robustness-runner-v1`）；候选路径使用 `r30-candidate-robustness-runner-v1`，
候选 baseline identity 携带 `experiment_subject` + `experiment_plan_fingerprint`，
**绝不**携带父策略的 strategy_id/version/checksum。baseline verifier 拆成
common + formal + candidate 三部分，共享 run-key / experiment / result / owner /
revision 校验。候选 baseline 必须 READY + result completed、candidate subject 精确匹配、
run_key 经 `ExperimentValidationRepository.build_run_key` 重建。候选 universe = owner
历史 universe 再 `candidate_replay.filter_candidate_members()`，universe stress 从
**已过滤集合** drop（绝不先全市场 drop 再 filter）。候选回放始终走
`candidate-replay-v1`；baseline replay 必须与 R29 metrics bit-identical
（`baseline_replay_not_bit_identical`）。cost/slippage/delay/liquidity 复用既有 execution
stress loop。parameter stress 只经**唯一** mutation authority
`strategy_parameter_schema.apply_parameter_stress(...)`（正式与候选同一条），绝不创建或
修改 candidate。date-range stress 在派生 spec（携带 `robustness_scenario_fingerprint`、
保留 `experiment_plan_fingerprint` lineage）上重跑 R29 PIT（`R29.run_validation`）；
candidate as-of 不可被越过（`candidate_asof_leakage` → scenario unavailable、无 metrics）。
scenario `unavailable` / case `failed` 仍产生 canonical report，因此对应队列 job 运营上
仍 `completed`。

`backend/candidate_robustness_service.py`：本阶段**唯一**新增生产模块，只有四个函数：
`declare_candidate_robustness_jobs`、`prepare_candidate_robustness`、
`run_candidate_robustness`、`complete_candidate_robustness_job`。declare 要求：search run
自证；plan-v2；policy fingerprint 自证；所有 PIT job 终态（completed/cancelled，否则
`pit_stage_not_terminal`）；对每个 completed PIT job 用
`ExperimentValidationRepository.get_run(run_key=...)` 重读 exact `evidence_id` run_key；
只有 `subject_kind=strategy_candidate`、candidate_id 精确匹配、`validation_status=ready`、
`result.status=completed` 才得到 job；`policy.bind` 精确 baseline 得到 `RobustnessPlan`；
N jobs + N queued events 原子追加（all-or-nothing）；同身份幂等，同
(search, candidate, stage) 但不同 baseline/plan 硬冲突。**任何地方都没有
latest/recent 查找。** completion 重读 exact report key、重读 exact R29 baseline key，
从 search plan-v2 policy 重建 expected `RobustnessPlan`，要求
expected == job == report plan fingerprints，并在追加 completed 事件前重读 R29
subject/experiment/result 身份。落库后崩溃用 exact report_key 再次调用 completion 即可
补写，**不重跑 R30**；幂等，不同证据 → `completion_evidence_conflict`。fake / 其它候选 /
其它 search / 其它 baseline / 其它 policy 的 report 都不能完成 job。

## 4. 不做

```text
NO frontend            NO new HTTP API        NO selection / ranking / promotion
NO new permanent table NO migration           NO duplicate robustness runner
NO duplicate execution loop                    NO duplicate PIT validator
NO candidate mutation by stress                NO latest / recent baseline lookup
NO candidate_score / ranking / winner / best_candidate / selected / rejected /
   promotable / promotion_decision
```

## 5. 固定维护性报告

```text
Business authorities added:
  reusable robustness policy
  candidate robustness orchestration

Duplicate robustness runner:
  NO

Duplicate execution loop:
  NO

Duplicate PIT validator:
  NO

New permanent DB tables:
  0

New DB write authority:
  0

New facade/wrapper:
  0

Implicit latest R29 lookup:
  0

Implicit latest R30 lookup:
  0

Runtime-default robustness policy:
  0

Candidate masquerading as StrategyVersion:
  0

Queue robustness metric copies:
  0

Direct robustness-report write sites:
  unchanged owner count

Direct robustness completion write sites:
  1 specialized verified path

Selection/ranking fields:
  0

Promotion dependency:
  0

AI provider dependency:
  0

Modules needed to understand candidate robustness rule:
  target <= 4

paper_trading.py LOC/defs:
  trend only, not blocker
```

## 6. 验证

```text
R36-B2 focused tests                 57 tests (test_r36b2_candidate_robustness_execution) PASS
R36-B1 regression                    40 tests PASS
R36-A regression                     49 tests PASS
R29 regression                       PASS
R30 regression                       PASS (formal identity frozen)

M-CRB                                24/24 DETECTED; survived=0; fake=0; timeout=0;
                                     restore SHA256=PASS; baseline after restore=GREEN
M-CEX                                21/21 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-SC                                 15/15 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-G                                  8/8 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-X                                  10/10 DETECTED; survived=0; fake=0; timeout=0; restore=PASS
M-AIG                                17/17 DETECTED; survived=0; fake=0; timeout=0; restore=PASS

backend full suite                   PASS
ruff check backend                   PASS
python -m compileall -q backend      PASS
git diff --check                     PASS
exact head CI                        (filled by Lead after CI completes)
unresolved threads                   (filled by Lead after review opens)

new robustness jobs count            2 (focused E2E: 2 eligible READY candidates -> 2 jobs + 2 queued events)
canonical reports count              2 (focused E2E: 2 canonical R30 reports, both jobs operationally completed)
```

本 PR body 中不编造测试数量或 hash；由 Lead 填最终 exact head 与计数。

## 7. 状态

```text
R34     COMPLETE
R35     COMPLETE
R36-A   COMPLETE
R36-B1  COMPLETE

R36-B2  IN REVIEW
R36-B   IN PROGRESS

R36-C   NOT STARTED
R36-D   NOT STARTED
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
```
