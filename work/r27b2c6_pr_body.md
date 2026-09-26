# R27-B2C-6 — adaptive / experiment owner readiness

本轮**只**回答一个问题：adaptive / experiment 的 factual evidence 到底**谁拥有**、**何时可用**、
**如何被 owner 核验**。

它**不是** `candidate_challenge` / `overfit_watch` 的 runtime 迁移，**不是** AI tuner / proposal /
apply 权限重构，**不是** R28 Experiment Contract。

---

## 为什么 Family B 今天不能安全成为 `ResearchEvidenceRef`

B2C-6 开始前的 Family B 审计确认了两个硬阻塞（逐项判断见
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

`recorded` 的含义**仅**是"这是一条 owner 自洽签发的可靠事实"——**不是**"候选通过了晋级验证"、
**不是**"策略为真"、**不是**"值得 apply"。三家都**没有** `source_unusable`：`*_unproven` 表示
owner 无法自证来源，障碍不在核验过程，因此归 `unverified`（否则假设层会报出错误的
`evidence_unavailable`）。

### 3. 三条 PIT 语义

```text
candidate availability_day  ← updated_at（owner 时区归一）        绝不来自 run_date
evaluation availability_day ← evaluation manifest created_at        绝不来自 dataset cutoff
updated_at > as_of          → UNAVAILABLE（返回 None）              绝不倒填当前行
```

历史 read 因此是 fail-closed 的。`cutoff` 与 `run_date` 都只作**事实字段**保留。

### 4. 唯一 adapter

```text
backend/ai_research_strategy_adapter.py
    evidence_ref_from_strategy_projection(projection)   ← 公开面只有这一个
```

入口只做 `type(projection) is …` **精确**类型判定（dict / `Mapping` / duck-typed / **子类**
一律 `TypeError`），签名里没有 `source_id` / `as_of`。复用既有
`EVIDENCE_SOURCE_STRATEGY_RESEARCH`，**不新增** source type。本层**不**读 DB、**不**读墙钟、
**不**联网、**不**调 LLM，也**不**重算任何 owner 的业务量。

---

## review 修正（三条 blocker）

### 修正一：writer origin 不能由**词汇**证明

第一版只用**词汇归属**（account / model / lifecycle / tier 是否落在 owner 词表内）判定
`selection_candidate_recorded`。这是错的，而且错在**已知的历史事实**上：收敛前这张表有两个
生产 writer，而旧的 `deepseek_advisor` 直写行用的正是 `shadow_proposal` / `ai_realtime` ——
两个值现在**都是** owner 合法词汇。于是新 owner 会**反向认证**一批它明确没有独占写入的行：

```text
historical deepseek direct write  →  合法词汇  →  selection_candidate_recorded
                                  →  ResearchEvidenceRef.is_verified = True     ✗
```

词表回答的是"这行**看起来像** owner 的产物"，不是"这行**是** owner 写的"。现在来源只能由
owner 在 durable `evidence` 列的 `owner_writer_origin` 键上**覆盖式**盖章来证明：

```text
记号位置   durable ``evidence`` 列（owner 已持久化、可严格验证 ⇒ 不需要 schema migration，
           也不新增第二套 ledger）
盖章方式   写路径**覆盖式**盖章：caller 传同名键会被 owner 覆写 ⇒ 调用方无法"申明"来源
读侧判据   必须逐字匹配 owner 签发值，否则 unproven；词汇检查降级为**自洽性**第二层
```

因此 `owner 行 → recorded`；**历史第二 writer 的行、以及记号引入前旧 owner 的行，一律
`unproven`** —— 两者在数据上无法区分，诚实结论就是"不可证"，代价是历史行全部 unproven。

**risk 侧刻意没有这一层**，因为它只有一个业务 writer 模块。这条不对称是**被断言的**
（`EXP-05b`）：一旦出现第二个业务 writer 写 `adaptive_risk_candidates`，测试就红，届时必须为
risk 补上同样的证明。`adaptive_engine._restore_candidate_snapshot` 是跨库补偿 —— 用动态表名
把**同一行**的旧快照逐列还原，不产生新的 origin，因此不算第二个 writer。

**已知限制（不声称已关闭）**：记号是 durable 列里的字符串，未来**新增的**直接 DB writer 若逐字
抄写它仍能伪装。本层保证的是"历史第二 writer 的行不可能带它" + "owner 写路径一定盖它且覆盖
caller 传值"；**physical database origin 仍是 OPEN / REQUIRED**。

### 修正二：proposal 槽位冲突必须显式拒绝，不能谎报保存

第一版对"`(run_date, account_id, regime)` 槽位已存在"一律 `return existing["id"]`：

```text
DeepSeek 生成 proposal B → 槽位已被 candidate A 占用 → 返回 A.id → B 没落库
                        → run_realtime_tuning 仍报告 shadow_proposal / "仅保存候选"   ✗
```

API 层的成功与 durable 状态被分开 —— 比收敛前的裸 INSERT 更危险（旧逻辑遇到 UNIQUE 冲突至少
会失败）。现在两种情形给出**不同**结论：

```text
既有行内容与本次提案逐字相同 → 明确的**幂等成功**（返回既有行 id）
内容不同 / 既有行损坏无法比较 → 抛 SelectionProposalConflict：本次提案**没有**被持久化
```

`run_realtime_tuning` 逐条捕获并返回 `persisted_ids` / `conflicts`：部分冲突时理由写明
"N/M 个未持久化"，全部冲突时 `status='proposal_conflict'`。既有行 lifecycle 逐字不变。
幂等比较刻意**不**包含 `evidence`（proposer 重跑会带不同 confidence，那不是提案内容的差异）。

### 修正三：测试的墙钟依赖（execution 域，**只动测试**）

`docker-smoke` 在 master 上就已经红，失败的是
`test_execution_verification_wiring.IntradaySellStampTests.test_intraday_t_sell_stamps_execution_verification`
（`["OUT_OF_SESSION"]`）。根因不是产品缺陷，而是**测试把"跑在星期几"当成了前提**：

```text
self.today = dt.date.today()            ← runner 墙钟
execution_planner._session_phase(周末)  → "market_closed"
⇒ 周末跑 CI 时，10:00 的连续竞价卖点永远进不去
```

修正：把 fixture 的日期对齐到最近的**交易日**（周一~周五）。**执行侧的 session 规则一个字都
没改**；本 PR 的改动集合里除这条测试之外不含任何 execution / session / intraday / tradability
文件。修完后 `docker-smoke` 在 exact-head 上 **PASS**。

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

INCREASED 的那一份是 roadmap 明确要求的 **owner→research 接缝**加三个 owner contract ——
**不是**新增业务 authority。

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
selection writer-origin proof:         before = 0   after = 1（durable owner marker）
proposal collision semantics:           before = 静默返回既有 id
                                        after = 幂等重放 / 显式 conflict
new public exception types:            1（SelectionProposalConflict）
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
dual run（legacy + typed 同时跑） / typed provider 多调一次   → NO
R28 Experiment Contract                                     → NOT STARTED
B2C-7 runtime / incident owner readiness                    → NOT STARTED
```

---

## OPEN / REQUIRED（未来 convergence 的前置能力，不是"不需要"）

```text
adaptive_rewards availability contract        owner 未发布可用性证明列；本轮由 canonical
                                              learning dataset/evaluation 取代它作为研究证据
historical candidate revisions                行可变，旧 revision 被覆盖即不可引用；要回溯须
                                              owner 新增 additive append-only revision 台账
                                              （DB migration），本轮刻意不静默新增第二套 ledger
adaptive_alpha_candidates stable identity     每次运行 DELETE + INSERT 重建
paper_parameter_versions experiment interp.    多 applier 缺一个收敛后的 applier 契约
paper_nav legacy overfit context               overfit_watch 仍把它当上下文
physical database provenance                   contract-issued = CLOSED；
                                              physical origin = OPEN / REQUIRED
```

---

## 验证（分层）

```text
L1 FAST     python 3.14.5 / compileall -q backend / ruff check backend / git diff --check  → PASS
L2 FOCUSED  adaptive + selection + risk + learning dataset/evaluation + promotion science +
            strategy adapter + research ownership guard + ai_research_contract + network
            boundary guard + execution wiring + tuner 回归（540 tests）                    → PASS
L3 MUTATION work/r27b2c6_adaptive_experiment_mutation_check.py
            baseline=GREEN, 17/17 DETECTED, survived=0, fake=0, timeout=0,
            restore sha256=PASS                                                            → PASS
L4 FINAL    python -m unittest discover -s backend -p "test_*.py"
            4584 tests, failures=0, errors=0, skipped=5                                    → PASS
```

### L3 里的三个真实缺口（记录，不静默）

```text
M-EXP-14  第一版**存活**过（mutation matrix 自己抓到）：当时的 EXP-23 断言同时改动了
          availability_day，于是"业务日变了"掩盖了"指纹忽略了 revision identity"。
          修正后断言只在**同一业务日内**换一个瞬间。
M-EXP-16  **review 先发现**：来源判据退回词汇归属 → 历史第二 writer 的行被判成 owner verified。
          补上 mutation 后由 EXP-04d 捕获。
M-EXP-17  **review 先发现**：proposal 槽位冲突被静默吞掉并谎报保存。
          补上 mutation 后由 EXP-03e（真实驱动 tuner）捕获。
```

review 发现的漏洞已立刻转成 mutation —— 否则下一轮回归仍然守不住它。

关于 OCR：`OpenCodeReview` / `OCR` / `semantic-review` **不在**本 PR 的验证流程里。

---

## exact-head CI（head = `dcd99ed`）

```text
tests                  PASS  11m41s
syntax                 PASS  9s
quality                PASS  20s
frontend               PASS  11s
browser-e2e (chromium) PASS  2m27s
security-leak-scan     PASS  1m30s
docker-smoke           PASS  8m37s      ← 墙钟依赖修正后由 FAIL 转 PASS
```

`mergeStateStatus = CLEAN`，`mergeable = true`，unresolved threads = 0。

---

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
NEXT AFTER HUMAN REVIEW: R27-B2C-7 runtime / incident owner readiness
STATUS: AWAITING HUMAN REVIEW
