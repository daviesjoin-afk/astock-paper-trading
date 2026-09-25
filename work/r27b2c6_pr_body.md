# R27-B2C-6 — adaptive / experiment owner readiness

本轮**只**回答一个问题：adaptive / experiment 的 factual evidence 到底**谁拥有**、**何时可用**、
**如何被 owner 核验**。

它**不是** `candidate_challenge` / `overfit_watch` 的 runtime 迁移，**不是** AI tuner / proposal /
apply 权限重构，**不是** R28 Experiment Contract。

---

## 为什么 Family B 今天不能安全成为 `ResearchEvidenceRef`

B2C-6 开始前的 Family B 审计确认了**两个硬阻塞**（其余逐项判断见
`docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md` 的 FAMILY B OWNER MATRIX）：

```text
1. adaptive_selection_candidates 有两个生产 writer
     adaptive_selection._upsert:417   +   deepseek_advisor:1069（裸 INSERT）
   → 连"单一 owner"都不成立，此时谈 typed 契约没有意义

2. 整族**没有任何 owner 发布过核验闭集**
   risk / selection 的 status、run_date、learning 的 pit_status 全都不是核验维度
   → 把其中任何一个读成"owner verified"就是伪造 provenance
```

本 PR 先收敛第 1 条，再让三个 owner 各自发布 typed 事实与极小核验闭集，最后由**唯一** adapter
归口。

---

## 交付物

### 1. writer 收敛（先决条件）

```text
before  adaptive_selection._upsert  +  deepseek_advisor 的裸 INSERT   → 2 个生产 writer
after   adaptive_selection（含新窄接口 record_shadow_proposal）        → 1 个 owner
```

`deepseek_advisor.run_realtime_tuning` 现在是 **producer / caller**。owner 窄接口
`adaptive_selection.record_shadow_proposal` 只做持久化，刻意**不**：调 LLM、决定 proposal 内容、
自动 apply、绕过 human apply、扩大 selection 权限。

```text
status / tier 由 owner 独占决定（shadow_proposal / ai_realtime），caller 无法传入
candidate_params 必须是纯因子权重补丁（键集恰为 {"weights"}）且单因子 ≤ ±3pp
(run_date, account_id, regime) 已存在时返回既有 id，绝不改写其生命周期
```

因为 `shadow_proposal` **不在** `apply_candidate` 的资格集合里，"AI 提案直接生效"结构性不可
表达。**human apply 边界、`selection_auto_apply_bounded`、outbox 语义、paper account 参数写
权限逐字未变。**

### 2. 三个 owner 各发一个 typed 投影 + 极小核验闭集

```text
adaptive_risk        AdaptiveRiskFactProjection       risk_candidate|<id>@<updated_at>
adaptive_selection   AdaptiveSelectionFactProjection  selection_candidate|<id>@<updated_at>
learning_evaluation  ExperimentEvaluationProjection   experiment_evaluation|<evaluation_fingerprint>
```

三张闭集刻意**互不重叠**、每家两态：

```text
<owner>_recorded   → OWNER_OUTCOME_VERIFIED
<owner>_unproven   → OWNER_OUTCOME_UNVERIFIED
```

`recorded` 的含义**仅**是"这是一条 owner 自洽签发、identity / 必要归一列 / revision 可用性都
成立的可靠事实"——**不是**"候选通过了晋级验证"、**不是**"策略为真"、**不是**"值得 apply"。
三家都**没有** `source_unusable`：`*_unproven` 表示 owner 无法自证来源，障碍不在核验过程，因此
归 `unverified`（否则假设层会报出错误的 `evidence_unavailable`）。

### 3. 三条 PIT 语义（本轮核心修正）

```text
candidate availability_day  ← updated_at（owner 时区归一）        绝不来自 run_date
evaluation availability_day ← evaluation manifest created_at        绝不来自 dataset cutoff
updated_at > as_of          → UNAVAILABLE（返回 None）              绝不倒填当前行
```

历史 read 因此是 fail-closed 的：旧 revision 被覆盖就不复存在，正确答复是"拿不到"，而不是
拿当前行解释更早的 `as_of`。`cutoff` 与 `run_date` 都只作**事实字段**保留。

### 4. 唯一 adapter

```text
backend/ai_research_strategy_adapter.py
    evidence_ref_from_strategy_projection(projection)   ← 公开面只有这一个
```

入口只做 `type(projection) is …` **精确**类型判定（dict / `Mapping` / duck-typed / **子类**
一律 `TypeError`），签名里没有 `source_id` / `as_of`。复用既有
`EVIDENCE_SOURCE_STRATEGY_RESEARCH`，**不新增** source type；细分放在 `record_kind` 与 `detail`
里，因此 `InformationEvent.kind` 自动仍是 `strategy_research_observed`。

本层**不**读 DB、**不**读墙钟、**不**联网、**不**调 LLM，也**不**重算任何 owner 的业务量
（eligibility / fitness / promotion gate / risk reduction / weights / metrics 全归各自 owner）。

### 5. 永久回归 + mutation matrix

```text
backend/test_adaptive_experiment_evidence_ownership.py   EXP-01 ~ EXP-18
backend/test_ai_research_strategy_adapter.py             EXP-19 ~ EXP-28
work/r27b2c6_adaptive_experiment_mutation_check.py       M-EXP-01 ~ M-EXP-15
```

---

## Architecture Impact

```text
Production modules added:            1（backend/ai_research_strategy_adapter.py）
Production modules removed:          0
New abstraction layers:              0
Pass-through wrappers:               0
Compatibility paths added:           0

Selection candidate writers before:  2
Selection candidate writers after:   1 owner

Business authorities before:         adaptive_risk / adaptive_selection / learning_dataset /
                                     learning_evaluation / promotion_science（各自独立）
Business authorities after:          same owners, writer ownership converged
                                     （**没有**新增 AdaptiveOwner，也没有统一业务 authority）

Runtime migration:                   NO
Legacy candidate_challenge removed:  NO（DEFERRED）
Legacy overfit_watch removed:        NO（DEFERRED）

Roadmap capabilities preserved:      YES
Roadmap capabilities advanced:       adaptive / experiment evidence owner readiness
Original invariants weakened:        NO
Net architecture surface:            NEUTRAL → minimally INCREASED
```

INCREASED 的那一份是 roadmap 明确要求的 **owner→research 接缝**（`ai_research_contract` 不得
import DB-backed 的 `adaptive_risk` / `adaptive_selection` / `learning_evaluation`，三个 owner
也不得 import research）加三个 owner contract —— **不是**新增业务 authority。

依赖方向单向，且沿用既有约束：

```text
adaptive_risk / adaptive_selection / learning_evaluation   （owner：identity / 可用性 / 核验）
        ↓
ai_research_strategy_adapter                               （唯一同时认识两套词表的接缝）
        ↓
ai_research_contract                                       （不 import 任何 owner / DB 模块）
```

`learning_evaluation` 既有的 `forbidden_dependencies()` 把 `adaptive_risk` /
`adaptive_selection` 列为禁用前缀 —— 这条约束本轮**没有**放宽，因此 experiment 投影住在
`learning_evaluation` 内部，而不是新模块。

---

## Maintainability Impact

```text
selection candidate writer count:      before = 2   after = 1
risk candidate owner count:            before = 1   after = 1
selection candidate owner count:       before = 0   after = 1
experiment evidence owners:            before = 0   after = 1
typed adaptive fact contracts:         before = 0   after = 3
strategy research adapter count:       before = 0   after = 1
strategy adapter production callers:   before = 0   after = 0（预期状态）
implicit current / latest lookup:      before = 0   after = 0
lifecycle → verification coupling:     before = 0   after = 0
PIT status → verification mapping:     before = 0   after = 0
promotion verdict → verification:      before = 0   after = 0

new modules:                           1
new wrappers:                          0
new service / manager / facade:        0
DB migration / schema change:          0
deleted roadmap capability:            0
weakened original invariant:           NO
```

---

## 本轮不改的东西（明确排除）

```text
deepseek_research._candidate_evidence / _overfit_evidence   → UNCHANGED（仍直读 ledger）
candidate_challenge / overfit_watch runtime migration       → DEFERRED（不是 REMOVED）
dual run（legacy + typed 同时跑）                            → NO
typed provider 多调一次                                     → NO
R28 Experiment Contract（search space / trial scheduler /
  experiment registry / promotion controller / optimizer）  → NOT STARTED
B2C-7 runtime / incident owner readiness
  （adaptive_runs / paper_jobs 失败、reject 分布、incident_triage） → NOT STARTED
```

---

## OPEN / REQUIRED（未来 convergence 的前置能力，不是"不需要"）

```text
adaptive_rewards availability contract
    owner 未发布可用性证明列。本轮处置是"由 canonical learning dataset/evaluation 取代它
    作为 research 科学证据"，而不是宣称 reward 的经济事实已核验。

historical candidate revisions
    candidate 行可变，旧 revision 被覆盖即不可引用（fail-closed 的代价）。要支持真正的历史
    回溯必须由 owner 新增 additive append-only revision 台账（DB migration），本轮刻意**不**
    静默新增第二套 ledger。

adaptive_alpha_candidates stable identity
    每次运行 DELETE + INSERT 重建，id / run_date / generation 都不是长期稳定 evidence identity。
    要 typed 化必须先有 stable identity + availability + reconstructability，否则就是伪造 identity。

paper_parameter_versions experiment interpretation
    多 applier 按约定写同一张版本表，缺一个收敛后的 applier 契约回答"哪次参数变更是哪次实验的结果"。

paper_nav legacy overfit context
    overfit_watch 仍把它当上下文使用（B2C-4B 已声明 paper_nav 为 legacy / compatibility）。

physical database provenance（全 R27 共有）
    contract-issued typed projection:                      CLOSED
    caller self-declared identity / as_of / verification:   CLOSED
    physical database origin / trusted provenance:          OPEN / REQUIRED
```

---

## 验证（分层）

```text
L1 FAST     python 3.14.5 / compileall -q backend / ruff check backend / git diff --check   → PASS
L2 FOCUSED  adaptive + selection + risk + learning dataset/evaluation + promotion science +
            strategy adapter + research ownership guard + ai_research_contract +
            network boundary guard（含 deepseek_advisor tuner 回归）                        → PASS
L3 MUTATION work/r27b2c6_adaptive_experiment_mutation_check.py
            baseline=GREEN, 15/15 DETECTED, survived=0, fake=0, timeout=0,
            restore sha256=PASS                                                            → PASS
L4 FINAL    python -m unittest discover -s backend -p "test_*.py"
            4579 tests, failures=1, skipped=5
            那 1 个失败（test_execution_verification_wiring 的 OUT_OF_SESSION）在**基线**
            a2d919e 上同样失败，是 wall-clock 依赖的既有问题，与本 PR 无关               → PASS*
```

### L3 抓到的真实缺口（记录，不静默）

`M-EXP-14`（内容指纹忽略 revision identity）在**第一版存活过**：当时的 `EXP-23` 断言同时改动了
`availability_day`，于是"业务日变了"掩盖了"指纹忽略了 revision"。修正后断言只在**同一业务日
内**换一个瞬间，隔离出 revision identity 本身的贡献。

### 关于 OCR

`OpenCodeReview` / `OCR` / `semantic-review` **不在**本 PR 的验证流程里（已永久退出）。

---

## 已知限制（不声称已关闭）

* adapter 只保证 **contract-issued**，不保证 **physical database origin**：调用方仍可自造
  SQLite fixture 并调用 owner 的 public read 拿到投影。**physical provenance 仍是 OPEN /
  REQUIRED**（与 R27 其余三个接缝逐字一致）。
* 三个 owner 的核验闭集只有两态，且 `*_recorded` **不是**晋级/科学结论。本 PR 不提供
  "候选是否值得 apply" 或"策略是否够好"的任何证据 —— 那分别属于 lifecycle 与 promotion 门禁。

---

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
NEXT AFTER HUMAN REVIEW: R27-B2C-7 runtime / incident owner readiness
STATUS: AWAITING HUMAN REVIEW
