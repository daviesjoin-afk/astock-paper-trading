# feat(ai): establish research information contract

**R27-A — AI Information & Research Contract**

```text
R24 Market Data Reading ─┐
R25 Signal Evidence      ├─→ ai_research_contract ─→ research / advisory / hypothesis
R26 Execution Evidence   │        （只读、纯契约）        ✗ pending signal
历史 strategy/portfolio ─┘                               ✗ paper_orders / paper_fills
                                                          ✗ risk decision
                                                          ✗ strategy promotion
```

本轮建立 AI 信息/研究层的**最小稳定边界**：让未来 AI 能读取可信事实并产出研究性
结论，但**绝不**成为行情、Signal、Risk、Execution、Promotion 的 authority。

范围刻意收窄：**1 个 production 模块 + 1 个测试文件 + 1 个 mutation 矩阵**。
不含 LLM provider、自动选股、自动下单、Signal 写入、策略生成与晋升、prompt 管理
平台、agent framework、vector DB、RAG。持久化留给 R27-B。

---

## 1. 三个概念，只有三个

| 概念 | 回答的问题 | 关键约束 |
| --- | --- | --- |
| `InformationEvent` | AI 看到了什么事实？ | `kind` 由 `evidence_ref.source_type` **派生**，错标无法表达 |
| `ResearchEvidenceRef` | 事实来自谁、哪一天、核验到什么程度？ | `source_type` 是闭集；`as_of` 必填；`verification` 逐字来自 owner |
| `ResearchHypothesis` | 基于这些事实提出了什么假设？ | `status` **派生**；`is_authoritative` 恒为 `False` |

刻意**不**造 10 个 dataclass，也刻意不建 `AIService` / `AIManager` / `AIOrchestrator` /
`AIContextBuilder` / `AIAdapterFactory` / `AIRepository` / `AIHelper` / `AIUtils`。
本轮新增模块只有 `ai_research_contract.py`，且它没有 adapter / projection 层 ——
需要读事实时直接调用 owner 的公开 `projection()`（R24 / R25 都已提供），
再多一层 wrapper 只会增加调用链而不减少认知复杂度。

### 为什么 `status` / `kind` 是派生只读属性

如果 `status` 可以由调用方给出，那么"给一个没有证据的假设贴上 `supported`"
就只是一个关键字参数 —— 而这恰恰是本轮要根除的**默认批准**。派生之后，一个假设的
强度**只**由它引用的、已存在的、且各自 owner 已经核验过的事实决定。
同理，`InformationEvent.kind` 派生自 `source_type`，于是"引用了 execution 事实却标成
market 事实"不是被禁止，而是**无法表达** —— 也就不存在"忘记检查"的路径。

---

## 2. Authority 边界

```text
AI ≠ Market Data Authority
AI ≠ Signal Authority
AI ≠ Execution Authority
AI ≠ Risk Authority
AI ≠ Promotion Authority
```

| 检查项 | 结果 |
| --- | --- |
| AI 层写入 `paper_signals` / `paper_orders` / `paper_fills` | **0** |
| AI 层产生 risk decision / strategy promotion | **0** |
| AI 层持有的 writable connection / 事务入口 | **0** |
| authority 反向 import AI 层 | **0**（guard AIG-02，28 个 authority 模块） |
| 未登记就消费 AI 层的生产模块 | **0**（guard AIG-03，`ALLOWED_AI_CONSUMERS` 当前为空） |

AI 层只消费已经在系统中存在的事实，且**只** import `market_data_contract`（R24 纯契约）
与标准库：

```text
ai_research_contract → market_data_contract        ✓ 允许（读事实 + 复用核验词汇）
market_data_contract → ai_research_contract        ✗ 拒绝
signal_service       → ai_research_contract        ✗ 拒绝
execution_planner    → ai_research_contract        ✗ 拒绝
paper_risk_service   → ai_research_contract        ✗ 拒绝
```

依赖方向是**单向**的：AI 是消费者，不得反向进入现有 authority。接入生产消费者必须
先把模块登记进 `ALLOWED_AI_CONSUMERS`，使"谁依赖了 AI"成为一次**有意识的决定**，
而不是静默扩散。

### 核验词汇只有一个 owner

`verified` / `single_source` / `disagreement` / `unavailable` / `not_attempted` 是 R24 的
既有维度。本契约**引用**它们而**不重新判定**：

- `verification` / `verification_method` 逐字来自 owner 的 `projection()`；
- 合法性由 R24 **独占**判定 —— `_authoritative_verification_pair` 直接构造一个
  `MarketDataSnapshot` 来问它允许哪些组合，而不是在这里重写"`verified` 允许配哪些
  method"（与 `signal_service._cross_source_verified` 同一手法）。R24 收紧定义时本层
  自动跟随；
- 非法组合（`verified` + `none`）在**构造期**被拒绝，而不是被静默降级。

于是 `single_source` / `not_attempted` **不可能**在本层被升级成 `verified`。

### 证据强度不合成质量分数

证据对结论的**方向**由 R24 verification 派生：

```text
verified                        → supporting
single_source / not_attempted   → degraded      （不足以支撑结论）
disagreement / unavailable      → rejecting     （证据反对结论，绝不静默挑一个源）
```

刻意不合成 `quality_score = 83`：把多个正交维度压成一个分数会让消费者只能猜，
且无法把"被否证"与"从没核验"区分开。假设状态同样由证据推出：

```text
无证据                       → insufficient_evidence / no_evidence
有反对证据                   → unsupported / evidence_contradicted
                              （核验源不可用记 evidence_unavailable，与否证区分）
至少一条 supporting 且无反对 → supported
其余（有事实但全部未通过核验）→ insufficient_evidence / evidence_not_verified
```

`confidence` 是 AI 对自己说法的自评，**不参与** status 判定 —— 一旦参与就会得到
"AI 越自信结论越强"的环路，那正是 §五 禁止的自动升级。它是给人看的运维信号，不是证据。

---

## 3. AI 产物为什么只是 research

```text
研究词汇:   supported │ insufficient_evidence │ unsupported
Signal 生命周期: pending │ approved │ blocked │ waitlist │ recovery
```

两个词汇表**不相交**（AI-06 用集合断言钉住），因此"AI 结论直接落成一条 pending
signal"在词汇层面就不成立。行为层还有第二道门：`commit_signal` 要求一个真正的
`SignalDecision`，把研究假设（或其投影、或其 status 字符串）当裁决传进去会在
**触碰任何连接之前**失败 —— AIG-05 用 `conn=None` 验证这一点（校验顺序决定了它先于
`conn.execute`，因此不需要 sqlite 即可证明）。

`ResearchHypothesis.projection()` 主动下发 `authority="research"` 与
`is_authoritative=False`，使"这只是研究"这一结论可追溯，而不是让调用方从 `status`
自己猜。`require_supported()` 对未支撑的假设抛 `UnsupportedResearch` ——
fail closed，绝不返回默认值。

---

## 4. PIT / 历史研究

- `as_of` 必须**显式且可证明**（`YYYY-MM-DD`）。无法解析即**构造期拒绝**，绝不回落
  `today()`；契约本身不 import `datetime` / `time`，不调用 `now()` / `today()` /
  `randint` / `getenv`（AIG-04 用 AST 钉住，AI-05 用调用名断言钉住）—— 这是"无法
  用 current state 回填历史"的**结构性**保证，而不是一条约定。
- 任一证据的 `as_of` 晚于假设的 `as_of` → **拒绝构造**，而不是静默过滤。
  静默丢弃会掩盖"AI 在用未来信息解释过去"这件事本身。
- 历史假设无法引用 current strategy head / active cycle：那些没有可证明的 `as_of`，
  连构造成 `ResearchEvidenceRef` 的资格都没有。
- `InformationEvent` 同样受 PIT 约束（观测日不得早于所引事实的业务日）。
- 缺失即缺失：没有可引用的证据 → `insufficient_evidence`；R24 `unavailable` 的
  reading 没有可证明的 as-of → 拒绝映射，而不是造一条"空事实"。

---

## 5. 持久化范围

本轮**不**建立持久化。仓库里没有既有的 hypothesis / research-ledger owner
（`paper_schema_migrations.py` 无任何 research / hypothesis / evidence 型 DDL），
为这个 PR 新建一套 AI 数据库体系会提前引入**第二个事实存放点**。
本模块是纯契约：无 DB、无 writer、无网络、无文件、无环境变量读取（guard AIG-04 同时
断言契约源码里不出现任何 SQL 文本）。

---

## 6. 验证

### Targeted tests

`backend/test_ai_research_contract.py` —— **19 tests，全部通过**，约 2 秒（不起数据库）。

| ID | 断言 |
| --- | --- |
| AI-01 | verified market evidence → 合法 `InformationEvent`；`kind` 与 `source_type` 一一对应（6 种全覆盖）；verification 逐字保留 |
| AI-02 | stale / unverified 证据**保留原状态**：单源 → `degraded`；`not_attempted` → `degraded` 且原因是 `evidence_not_verified`；非法核验组合构造期被拒 |
| AI-03 | 引用未来证据 → **拒绝构造**（错误信息点名那条未来事实）；同日合法；历史假设引用"今天"同样被拒 |
| AI-04 | 无证据 → `insufficient_evidence`（`confidence=1.0` 也不能替代证据）；被否证 → `unsupported`；支持+反对并存 → 反对优先；`require_supported` fail closed |
| AI-05 | 历史假设不读 current state：非法 as-of 全部构造期拒绝；契约无 `datetime` / `time` / 时钟调用 |
| AI-06 | 研究词汇与 signal 生命周期**不相交**（集合断言） |
| AI-07 | 投影自带 `authority="research"` / `is_authoritative=False`；证据不足时如实给出 reason |
| AI-08 | 来源闭集（`llm_output` 等 5 个非法值全被拒）；重复引用去重；payload / detail 冻结不可变；owner 投影映射逐字保留；`unavailable` reading 拒绝映射 |
| AIG-01 | 契约只依赖 stdlib + `market_data_contract`（未登记 import 即失败） |
| AIG-02 | **28 个 authority 模块**无一反向 import AI 层 |
| AIG-03 | 未登记就消费 AI 层的生产模块 = 0 |
| AIG-04 | 无 DB / 网络 / 文件 / 环境依赖；源码无 SQL 文本 |
| AIG-05 | AI 产物无法被 commit 成 signal（4 种伪造形态，`conn=None` 行为证明） |
| AIG-06 | 契约无任何写入 / 提交入口（`commit` / `execute` / `insert` …） |
| 非空性 | 5 条：import 扫描命中 `import` 与 `from … import`；调用扫描命中写调用；SQL 扫描命中 INSERT 且**忽略 docstring** |

### 语义 mutation

`work/r27_ai_mutation_check.py` —— **5/5 CAUGHT，survived=0，fake=0**，还原 sha256 校验通过。

| ID | mutation | 期望变红的唯一永久回归 | 结果 |
| --- | --- | --- | --- |
| M-AI1 | 移除 future-evidence check | AI-03 | CAUGHT |
| M-AI2 | 单源证据升级成 `supporting`（unverified → verified） | AI-02 | CAUGHT |
| M-AI3 | 空证据假设默认批准（→ `supported`） | AI-04 | CAUGHT |
| M-AI4 | `paper_risk_service` 反向 import AI 层 | AIG-02 | CAUGHT |
| M-AI5 | AI 自产文本加入 evidence source 闭集 | AI-08 | CAUGHT |

沿用 R23/R24/R25 的 runner 约定：逐次唯一 `PYTHONPYCACHEPREFIX`（否则 baseline 与
mutant 共享字节码缓存，整张矩阵静默失效）、anchor 必须唯一命中一次、
`SyntaxError` / `ImportError` / `NameError` 计为 FAKE、结束后 byte-identical 还原并校验 sha256。

### 本地已跑

```text
ruff check（新增 3 文件）                                  PASS
compileall（新增 3 文件）                                  PASS
targeted AI contract tests                                 19/19 PASS
consumer regression
  test_market_data_boundary（R24）                         PASS
  test_signal_pipeline（R25）                              PASS
  test_r26_execution_authority_guard（R26）                PASS
  小计                                                    111 tests PASS
邻近 AI / 守卫回归
  test_ai_controls / test_ai_review_slots / test_simplification /
  test_runtime_simplifications / test_repository_hygiene  133 tests PASS (skipped=1)
执行链路回归（R26 execution + 3 个 architecture guard）    221 tests PASS
```

production code 只新增纯契约（无 DB、无 I/O），因此未本地跑全量。
exact-head CI 由 GitHub 承担；本轮未改 frontend，故不重复本地 browser 五连跑。

#### Exact-head verification

```text
base:              d5418c80e8cfaede40c88ba4e62cecc658333d35
                  （refactor(execution): establish high-fidelity A-share simulation execution (#186)）
current exact-head: 0ac1c371184fa8f705cc7f333109c49639a44f15

exact-head CI:     PASS（9/9 —— tests 3.11 / tests 3.12 / syntax / quality /
                   docker-smoke / frontend / browser-e2e / security-leak-scan ×2）
```

merge authority 是**当前 PR HEAD 的 exact-head CI**，不是文档里写的某个旧 SHA。

---

## 7. 可维护性

- **只有 1 个 production 模块**，且它能回答下列全部问题：
  - 拥有哪个业务问题？ → "AI 看到的哪些已存在事实，能支撑什么研究结论"
  - 输入是什么？ → owner 的 `projection()` / 显式 `(source_type, source_id, as_of)`
  - 输出是什么？ → `InformationEvent` / `ResearchEvidenceRef` / `ResearchHypothesis`
  - 谁允许调用它？ → 未来登记在 `ALLOWED_AI_CONSUMERS` 的消费者（当前 0）
  - 它能调用谁？ → 只有 `market_data_contract`（纯契约）与标准库
  - 为什么不能放进现有模块？ → 它是研究语义，不属于行情 / signal / 执行 / 风控
    任何 authority；塞进去会让那个 authority 反向依赖研究层
  - 为什么认知复杂度下降？ → 把"AI 看到什么、结论引用了什么、够不够"从散落的
    叙事字符串收敛成 3 个不可变类型 + 1 个派生判据
- **依赖方向**：单向 `AI → R24`，由 AIG-01/AIG-02/AIG-03 三道 AST guard 钉住。
- **理解一个假设所需模块数**：1（`ai_research_contract.py`）＋ 读取事实时 1 个 owner
  （R24 `projection()`）。核验语义不再需要读者跳到第二个判据。
- **重复规则引入**：0。核验合法性委托 R24、双源判据委托 `is_cross_source_verified`、
  事实映射委托 owner 的 `projection()`。
- 简单 `if` 保留：`_derive_status` 是 12 行直白分支，**没有**为它建 rule engine。

### 新增 facade / wrapper 数量：0

`evidence_ref_from_reading` 是**唯一的**映射入口，且是必要而非装饰：它让"逐票报价 /
signal evidence 如何成为一条可判定的事实"只有一个 owner，否则每个消费方都会各自
解释 `verification` / `quote_validation`，同一份事实在不同路径上得到不同可信度结论。

---

## 8. 完成标准自查

| 问题 | 答案 |
| --- | --- |
| AI 看的是哪份事实？ | owner 的 `projection()`，引用化为 `ResearchEvidenceRef` |
| 事实来自谁？ | `source_type` 闭集（market_data / signal / execution / strategy_research / portfolio_research / news） |
| 事实的 as_of 是什么？ | 必填、可证明，来自事实自身（非读取时刻） |
| 事实是否 verified？ | `verification` + `verification_method` 逐字来自 owner，合法性由 R24 独占判定 |
| AI 结论引用了哪些 evidence？ | `ResearchHypothesis.evidence_refs`，去重后逐条投影 |
| AI 是否使用了未来事实？ | 不可能：构造期拒绝，且契约不读时钟 |
| AI 输出为什么只是 research？ | 词汇不相交 + `is_authoritative=False` + `commit_signal` 要求真裁决 |
| 现有 authority 是否完全不依赖 AI？ | 是：28 个 authority 模块反向 import = 0 |

---

## Scope deferred to R27-B

- actual AI provider / LLM adapter
- persistent research ledger（若无现成 owner，需先决定存放点）
- research UI

---

```text
MERGE:   NOT MERGED
DEPLOY:  NOT DEPLOYED
STATUS:  AWAITING HUMAN REVIEW
```
