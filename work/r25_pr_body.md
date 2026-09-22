# refactor(signal): establish evidence-bound signal pipeline

**R25 — Signal Pipeline Boundary**

```text
Selection Candidate
        ↓
Signal Evidence
        ↓
Signal Decision（approved / blocked + reason）
        ↓
Frozen Signal Context
        ↓
Signal Commit（唯一 INSERT owner）
        ↓
Immutable Signal Ledger
        ↓
Risk → Order → Fill
```

本轮把"生成一条 signal"从分散在 candidate 获取 / evidence 收集 / 策略条件 /
approval-block / risk log / INSERT / bootstrap / runtime read 多路径的过程，收敛成
一个**可追踪、单一 authority、显式输入、不可事后重解释**的 Signal Pipeline。

---

## 1. 核心问题：为什么这一条 signal 被写进系统？

R25 之前，回答这个问题要在 provider 调用、helper、DB 写入之间重新推导。现在链路是
显式的，且每一步都有可复核的持久化产物：

| 步骤 | 产物 | 落库位置 |
| --- | --- | --- |
| Candidate | 候选 dict（**不**直接变成 DB 行） | —— |
| Evidence | `SignalEvidence`（verification / method / as-of / policy） | `payload.signal_evidence` |
| Decision | `SignalDecision`（outcome / reason / status） | `payload.signal_decision` |
| Frozen Context | `SignalWriteContext`（cycle + 不可变 strategy pin） | 行上的 `cycle_id` / `strategy_*` |
| Commit | 唯一 writer 落库 | `paper_signals` |

reviewer 不再需要看 helper：裁决、证据、冻结上下文三样都在同一行里。

---

## 2. Signal authority before / after

```text
signal decision owners:            before = 2（close / bootstrap 各推导一次 status）
                                   after  = 1（同一 decide_signal）

signal persistence owners:         before = 2 个写入点 / 1 个模块
                                   after  = 1 个 writer / 1 个模块

bootstrap 独立 persistence path:   before = YES
                                   after  = NO

direct Market Data provider access from signal: before = 3 处写事务内网络调用
                                   after  = 0

implicit current-state lookup in signal:  before = 0（R23 已消除）
                                   after  = 0（保持不变）
```

**必须如实说明的一点**：signal persistence 的**模块数**并没有从 N 降到 1。base 上
`INSERT INTO paper_signals` 已经是 2 条语句、1 个模块（都在 `paper_trading.py`：
`generate_signals` 与 `_bootstrap_signals_for_today`）。R25 真正改变的是：

- 两条内联 SQL 收敛到**一个 writer 契约**（`signal_service.commit_signal`），
  provenance 四列只能来自 frozen context，行数据无法携带另一套戳；
- 两种冲突语句由**一个构造入口**（`conflict_statement`）产生，因此回归测试断言的
  SQL 与生产执行的 SQL 不可能分叉；
- bootstrap 不再有独立的写入语义与独立的 status 推导。

---

## 3. Candidate / Evidence / Decision 如何分离

**Candidate ≠ Signal。** 候选 dict 此前"加字段加到自动变成 DB signal"——中间没有
lifecycle boundary。现在落库必须经过一个显式决策对象：

```python
decision = SIG.decide_signal(passed=..., reason=..., evidence=...)
# outcome ∈ {approved, blocked}; status 是 paper_signals.status; evidence 是证据投影
```

`evidence` 是**紧凑投影**，不是第二份行情 payload：只携带
`verification` / `verification_method` / `asof_day` / `observed_at` / `policy`
与少量解释字段。不复制全市场 snapshot。

---

## 4. 哪个 owner 写 signal ledger

`backend/signal_service.py`（唯一，且是**纯边界**）：

- `commit_signal(conn, *, context, row, conflict)` 是 `paper_signals` 的**唯一**
  生产写入点；
- `conflict_statement(conflict)` 是两种语句的唯一构造入口；
- 依赖方向单向：`paper_trading → signal_service → market_data_contract /
  strategy_selection_resolver`。它**不** import `paper_trading`、不 import FastAPI、
  不取数据、不读时钟、不开事务（guard `SIGG03` AST 钉住）。

没有 `signal_repository.py`，没有 `service → repository → helper → connection`
长链：`Signal Service → SQL`，直接。

---

## 5. bootstrap 是否统一

统一。两条路径都经 `commit_signal`，冲突语义显式二选一：

| 路径 | 冲突策略 | 语义 |
| --- | --- | --- |
| close（`generate_signals`） | `CONFLICT_IGNORE` | `INSERT OR IGNORE`：同键重跑幂等，**绝不**覆盖既有 signal |
| bootstrap（`_bootstrap_signals_for_today`） | `CONFLICT_REFRESH` | `ON CONFLICT ... DO UPDATE`：**只刷新业务列** |

`strategy_id` / `strategy_version` / `strategy_checksum` / `cycle_id`
**永不出现在 SET 子句**——升级前的 NULL cycle 保持 NULL，已有 stamp 不被改写。
回归 `SIG07` 静态断言列集合，`test_strategy_selection_provenance.RF04` 用真实
历史形状（stamp 有值 + cycle NULL）验证刷新不抛错且不回填。

**本轮还修掉一个继承来的真实缺陷**：bootstrap 的 status 被推导了两次（一处用于
落库、另一处用于 decision），`decide_signal` 的结果从未被消费——两套条件一旦漂移，
写进账本的状态就不再是决策的结果。现在入场冻结只判定一次，`status` 与 `reason`
都由同一个决策产出。

---

## 6. Market Data contract 如何消费

signal 侧**不重算**任何行情语义：

- 逐票行情 → `SignalEvidence` 的映射复用 R24 的
  `market_data_contract.verification_from_cross_status`（既有业务术语，不新造枚举）；
- 逐票双源结论由 `SignalEvidence.cross_source_verified` 显式回答，判据**委托**给
  R24 的 `is_cross_source_verified`（构造 `MarketDataSnapshot` 再问它），而不是在
  signal 侧写 `verification == "verified"`；
- **修正了一处 B 类误用**：`_signal_approval` 此前用 legacy 字符串
  `quote_validation != "cross_source_checked"` 表达双源要求。由于
  `coverage_integrity` 也映射成 `verified`，一旦 signal 路径改读 R24 reading 就会
  把"覆盖完整性通过"误当双源。现在统一走 `evidence.cross_source_verified`。
- freshness policy 数值（240s / 90% / 4000 行）仍归 R24；signal 只引用 policy 名称。

`SIGG03` 保证 `signal_service` 不 import 任何 provider / cache / transport 模块。

---

## 7. cycle / strategy provenance 如何冻结

- 两条路径都在 commit phase 解析**一次** frozen context（R23 的
  `signal_write_context_or_error`），并用写锁内重读的账户周期做 stale 比较；
- rollover 使整批 stale：审计 `signal_stale_cycle_context`、`created == 0`，
  **绝不**把旧周期候选迁到新周期或贴 current head；
- `_risk_log` 与 signal 行共享**同一个** frozen stamp（不再逐 candidate 重解析）；
- DB 层另有 R23 的 trigger 作第二道门
  （`trg_paper_signals_cycle_provenance_insert`、`..._strategy_stamp_immutable`）。

## 8. 为什么不会重解释历史 signal

- 读路径只读持久化 stamp：不再调 `SR.get_version`、不重解 current cycle、
  不用 current market data 回填；
- 约 30 处production `UPDATE paper_signals` **全部**只改 `status` / `reason` /
  `payload`，没有任何一处触碰 provenance 四列（并有 trigger 兜底）；
- signal → order 血缘仍由唯一 owner `signal_order_provenance` 解析，
  **未**新建第二套 lineage；`adaptive_evidence_chains` 只是派生的可观察投影。

## 9. 前端如何显示 decision / evidence

后端在 `GET /api/paper/overview` 的 signal 投影里新增稳定的
`signal_decision`（outcome / status / reason / evidence 状态）。前端：

- 新增"裁决/证据"列，直接渲染后端投影；
- 渲染 `cross_source_verified`（后端算好的业务谓词），**不**比较
  `verification === "verified"`；
- 补齐 signal 生命周期状态的中文标签（此前 `deferred_capacity` /
  `entry_frozen_waitlist` 等直接打印 raw token）。

新增 `frontend/tests/signal-decision.test.mjs`（7 条）专门钉住"前端不重算 signal
规则、不把 verified 当双源、缺失 payload 不默认可信"。

---

## 10. R25 / R27 安全说明

**R25 does NOT allow AI to write signals.**

未来 R27 的 AI 可以是 **Candidate Producer**（输出 InformationEvent /
ResearchHypothesis / CandidateSuggestion），但**不是**
Signal Persistence Owner、**不是** Risk Authority、**不是** Promotion Authority。
本轮不建 AI adapter / LLM signal manager，只保证 contract 能接。

---

## 11. 复杂度报告

```text
signal decision owners:        before = 2   after = 1
signal persistence owners:     before = 2 写入点 / 1 模块   after = 1 writer / 1 模块
bootstrap independent writer:  before = YES  after = NO
direct provider access from signal:  before = 3（写事务内）  after = 0
implicit current-state lookup:       before = 0  after = 0

new production modules:  1（backend/signal_service.py）
new facade/wrapper:      0
removed facade/wrapper:  0（删掉的是 2 处内联 SQL 写入点）
理解"为什么产生一条 signal"需要看：
  before = 多个模块（paper_trading + provider + helper + DB）
  after  = 1 行账本记录 + 1 个 writer 模块

paper_trading.py  BASE LOC=14895 defs=302  →  HEAD LOC=14993 defs=304
  （趋势观察，非门槛：+98 行是 bootstrap 的预取阶段与说明不变量的注释）
```

---

## 12. 验证（分层）

```text
L0  ruff backend（全树）           All checks passed
    compileall                     OK

L1/L2  R25 targeted                254/254 OK
       test_signal_pipeline         20
       test_provenance_inflight_change 11
       test_strategy_selection_provenance 44（含修复的 RF04/RF05）
       test_paper_trading_architecture_guard 112（含修复的 guard14r/14s）
       test_market_data_boundary     54
       signal-consumer regression    213/213 OK
       read-model / frontend-contract 101/101 OK

L3  backend full                   Ran 4078 tests, OK, skipped=5
    frontend unit                  125/125（118 既有 + 7 新增）
    frontend build + check:dist    PASS
    browser E2E (paper-runtime)    2 passed（workers=1, retries=0）
    security (worktree / all)      clean
    mutation --non-vacuity         8/8 CAUGHT, survived=0, fake=0, other=0
                                   restore sha256 PASS
```

### 继承的半成品与本轮修复

本轮起点包含一份**未提交的 R25 半成品**（`signal_service.py` + `paper_trading.py`），
它当时已经打红 4 个既有测试，且 bootstrap 写事务内仍有 provider I/O（而它自己新增的
注释宣称"写锁内零网络"）。本轮完成的工作包括：

1. 修复 `test_RF04` / `test_RF05`（它们按字面量从 `paper_trading.py` grep 生产 SQL，
   迁移写入点后抛 `ValueError` 而非断言失败）——改为向
   `signal_service.conflict_statement` 索取语句，探针不再随文件布局失明。
2. 修复 `guard14r` / `guard14s`（它们要求 INSERT 字面量出现在 `generate_signals` 里）
   ——改为断言"该路径把 frozen context 交给唯一 writer"且"commit 的 row 不含
   provenance 列"，并把 commit 定位改到 `SIG.commit_signal(` +
   `_db(immediate=True)` 块。
3. **真正修掉** write-lock 内的 provider I/O：把候选冷却 / recheck 注入改为只读连接
   完成，并把行情 / 新闻 / 板块流预取移到写事务之前。新增 AST guard `SIGG04` 与行为
   探针 `SIG14`（包住 `_db` 与 provider，写事务内调用即失败）两层守住。
4. 修掉 bootstrap 的 status 双套条件死代码。

### mutation 矩阵发现的测试鉴别力问题

首轮矩阵只有 4/8。3 条 SURVIVED 是**真实的测试弱点**，已修正：

- `SIG04` 只断言 `cross_source_verified == False`；一条把 `not_attempted` 贴上
  `cross_source` 标签的变异仍然满足该断言 —— 补上
  `verification_method == none`（这才是"没核验过"与"核验过但没通过"的分界）。
- `SIG09` 只断言 "context=None 会抛异常"；一条 `context = row` 的变异随后照样会抛 ——
  改为同时拒绝伪装成 context 的 dict，并断言没有写入任何行。

教训：断言"抛了异常"不等于断言"因正确理由拒绝"。

---

## 13. R25 明确不做

Simulation Execution Fidelity 重构、成交/滑点/手续费模型、AI Information Layer、
LLM candidate generator、策略/因子/收益优化、Strategy Lifecycle、Promotion Engine、
Shadow Framework —— 分别属于 R26 / R27 / R28+ / R31+。

---

```text
MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
STATUS: AWAITING HUMAN REVIEW

Exact-head verification:
GitHub Actions checks on current PR HEAD
```
