# feat(ai): establish research information contract

**R27-A — AI Information & Research Contract**

```text
R24 Market Data Reading ─┐
R25 Signal Evidence      ├─→ owner-issued evidence ref ─→ explicit hypothesis relation
R26 Execution Evidence   │        （只读、纯契约）              │
历史 strategy/portfolio ─┘                                     ↓
                                                       ResearchHypothesis
                                                       research / advisory only
                                                       ✗ pending signal
                                                       ✗ paper_orders / paper_fills
                                                       ✗ risk decision
                                                       ✗ strategy promotion
```

本轮建立 AI 信息/研究层的**最小稳定边界**：让未来 AI 能读取可信事实并产出研究性
结论，但**绝不**成为行情、Signal、Risk、Execution、Promotion 的 authority。

范围刻意收窄：**1 个 production 模块 + 1 个测试文件 + 1 个 mutation 矩阵**。
不含 LLM provider、自动选股、自动下单、Signal 写入、策略生成与晋升、prompt 管理
平台、agent framework、vector DB、RAG。持久化留给 R27-B。

---

## 1. 两个正交维度（本 PR 的核心）

```text
fact verification    事实 owner 回答："这条事实是否通过它自己那套核验？"
hypothesis relation  research reasoning 显式声明："它对当前 thesis 是 supports /
                     contradicts / context？"
```

这两者**必须分开**，且本契约刻意**不做**任何语义绑定：

```text
✗ verification=verified    →  relation=supports   （verified 事实自动支持 thesis）
✗ disagreement/unavailable →  thesis 被反驳        （只说明 evidence 本身不可靠）
```

一条 cross-source verified 的报价只说明"这个价格事实可信"。它可能被声明为
`relation=context`，对"下一交易日 momentum 会继续"毫无支撑力：

```text
Fact:               600000 price = 10.50, verification = cross-source verified
Relation to thesis: context
→ 不能自动支持 "momentum continues tomorrow"
```

同理，provider disagreement / unavailable 只说明**这条 evidence 不能成为可靠依据**，
**不等于**"thesis 被可信事实反驳" —— 后者才是 `unsupported`。

relation 是最小闭集（`supports` / `contradicts` / `context`），刻意不含
strong_support / weak_support / neutral_positive / negative / uncertain 等评分档位：
本轮不做评分模型，也没有 `quality_score` / `weighted_support` / Bayesian 合并。

### 假设状态（由证据派生，不由调用方声明）

```text
无 evidence                          → insufficient_evidence / no_evidence
有 verified contradicts              → unsupported / evidence_contradicted
有 verified supports 且无 contradicts → supported
只有 context / 未验证 supports        → insufficient_evidence / evidence_not_verified
只有 unavailable / disagreement      → insufficient_evidence / evidence_unavailable
```

注意末两行：**来源不可用不是 `unsupported`** —— 它只让证据不足以判断。

`confidence` 是 AI 的自评（`[0,1]`），**不参与** status 判定。`confidence=1.0`
配 no evidence 仍然是 `insufficient_evidence`。

---

## 2. 四个概念

| 概念 | 回答的问题 | 关键约束 |
| --- | --- | --- |
| `InformationEvent` | AI 看到了什么事实？ | `kind` 由 `evidence_ref.source_type` **派生**，错标无法表达 |
| `ResearchEvidenceRef` | 事实来自谁、哪一天、owner 的核验结论是什么？ | **无公开 raw 构造器**；只能由 owner factory 签发 |
| `HypothesisEvidence` | 这条事实对 thesis 是什么关系？ | `relation` 显式传入，**不**从 verification 派生 |
| `ResearchHypothesis` | 基于这些证据提出了什么假设？ | `status` **派生**；`is_authoritative` 恒为 `False` |

刻意**不**造 10 个 dataclass，也刻意不建 `AIService` / `AIManager` / `AIOrchestrator` /
`AIContextBuilder` / `AIAdapterFactory` / `AIRepository` / `AIHelper` / `AIUtils` /
`ResearchManager` / `EvidenceRegistry`。新增 production 模块只有
`ai_research_contract.py` 一个，没有 adapter / projection 层 —— 需要读事实时直接调
owner 的公开 `projection()`（R24 已提供），多一层 wrapper 只增加调用链。

### 为什么 `status` / `kind` / `is_authoritative` 是派生只读属性

如果 `status` 可以由调用方给出，那么"给一个没有证据的假设贴上 `supported`"
就只是一个关键字参数 —— 而这恰恰是本轮要根除的**默认批准**。派生之后，一个假设的
强度**只**由它引用的、owner 已核验的事实 + 显式 relation 决定。

---

## 3. owner-issued evidence（P1-2）

调用方**不能**仅凭传字符串声明一条 owner authority 事实：

```python
# ✗ 抛 TypeError —— 没有公开 raw 构造器
ResearchEvidenceRef(
    source_type="market_data", source_id="fake",
    verification="verified", verification_method="cross_source",
)

# ✓ 唯一的签发入口：必须交入 owner 的 typed projection
ref = evidence_ref_from_market_reading(
    market_data_contract.MarketDataReading(...), source_id="LIVE_MARKET_POLICY",
)
```

`evidence_ref_from_market_reading` 要求真正的 `MDC.MarketDataReading`
（duck-typed 对象拒绝），并从中**逐字**复制 `verification` /
`verification_method` / `as_of`：于是 `single_source` / `not_attempted` 在签发时
**不可能**变成 `verified`，`as_of` 也无法被调用方覆盖（PIT 证明链完整）。

没有可 import 的哨兵，也没有 `issued=True` 之类的开关 —— 那种"标记位"调用方一样
能写，只提供虚假的安全感。

诚实声明这一层的强度：这是**类型层构造边界**，不是密码学封印。Python 无法阻止有人
`object.__new__` 或伪造一个 reading。它保证的是：AI 代码里**不再出现自由形式的核验
字符串**，任何 AI 事实都必须由一个 owner 类型对象承载。

当前 `SUPPORTED_OWNER_ADAPTERS` **只有 market_data**。signal / execution / news 等
仍在 `EVIDENCE_SOURCE_TYPES` 闭集里作为已声明的未来来源，但没有 factory 可以签发
—— 少支持一个 source 好过允许伪造一个 authority。不假装已接好。

---

## 4. 冲突 fail closed（P2-1）

identity 是 `(source_type, source_id, as_of)`。

```text
完全相同（含 detail）             → 安全去重
同 identity 但事实状态不同         → EvidenceConflict
同一条 evidence 同时两种 relation  → EvidenceRelationConflict
```

两种冲突都**与顺序无关**（先按 identity 分组再判定）：`[A, B]` 与 `[B, A]` 必然同一
结果。first-wins 会让研究结论依赖 collection order，而顺序不是业务语义。刻意不做
"保守合并"：fail closed 更清楚。

---

## 5. 深冻结（P2-2）

`payload` / `detail` 递归冻结：

```text
Mapping → MappingProxyType（逐键递归）
list / tuple → tuple（逐项递归）
set / frozenset → frozenset（逐项递归）
标量 → 原样
其他任意可变对象 → TypeError（fail closed）
```

只做浅冻结会让调用方**保留的原始对象**继续改写"已冻结"的研究内容：

```python
original = {"nested": {"items": [1, 2]}}
event = InformationEvent(..., payload=original)
original["nested"]["items"].append(3)   # 契约内部值必须不变 → (1, 2)
event.payload["nested"]["x"] = 4        # TypeError
```

---

## 6. Authority 边界

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

依赖方向单向：

```text
ai_research_contract → market_data_contract        ✓ 允许（读事实 + 复用核验词汇）
market_data_contract → ai_research_contract        ✗ 拒绝
signal_service       → ai_research_contract        ✗ 拒绝
execution_planner    → ai_research_contract        ✗ 拒绝
paper_risk_service   → ai_research_contract        ✗ 拒绝
```

核验词汇只有一个 owner：`verification` / `verification_method` 逐字来自 owner 投影，
其合法性由 R24 **独占**判定（构造一个 `MarketDataSnapshot` 来问它允许哪些组合），
而不是在本层重写"`verified` 允许配哪些 method"。

### AI 产物为什么只是 research

```text
研究词汇:       supported │ insufficient_evidence │ unsupported
Signal 生命周期: pending │ approved │ blocked │ waitlist │ recovery
```

两个词汇表**不相交**（AI-20 集合断言钉住）。`commit_signal` 另有一道门：它要求真正的
`SignalDecision`，把研究假设（或其投影、或其状态字符串）传进去会在**触碰任何连接
之前**失败 —— AIG-05 用 `conn=None` 验证（校验顺序决定了它先于 `conn.execute`，
因此不需要 sqlite）。刻意**不**修改 `signal_service` 来迁就 AI。

`ResearchHypothesis.projection()` 主动下发 `authority="research"` 与
`is_authoritative=False`；`require_supported()` 对未支撑的假设抛
`UnsupportedResearch`（fail closed，绝不返回默认值）。

---

## 7. PIT / 历史研究

- `as_of` 必须**显式且可证明**。无法解析即构造期拒绝，绝不回落 `today()`；契约不
  import `datetime` / `time`，不调用 `now()` / `today()` / `randint` / `getenv`
  （AIG-04 + AI-05 用 AST 钉住）—— 这是"无法用 current state 回填历史"的**结构性**
  保证，而不是一条约定。
- 任一证据 `as_of` 晚于假设 `as_of` → **拒绝构造**，而不是静默过滤。静默丢弃会掩盖
  "在用未来信息解释过去"这件事本身。
- 历史假设无法引用 current strategy head / active cycle：那些没有可证明的 `as_of`，
  连签发出证据引用的资格都没有。
- 只引用 `verification`，**不**引用 freshness：freshness 回答"对当前时间是否仍新鲜"，
  与"来源是否经过核验"是两个维度，不压成 `trusted=True`，也不发明 AI quality score。

---

## 8. 持久化范围

本轮**不**建立持久化。仓库里没有既有的 hypothesis / research-ledger owner
（`paper_schema_migrations.py` 无任何 research / hypothesis / evidence 型 DDL），
为这个 PR 新建一套 AI 数据库体系会提前引入**第二个事实存放点**。
本模块是纯契约：无 DB、无 writer、无网络、无文件、无环境变量读取（AIG-04 同时断言
契约源码里不出现任何 SQL 文本）。

---

## 9. 验证

### Targeted tests

`backend/test_ai_research_contract.py` —— **33 tests，全部通过**，约 2.4 秒（不起数据库）。

| ID | 断言 |
| --- | --- |
| AI-01 | verified market evidence → 合法 `InformationEvent`；`kind` 与 `source_type` 一一对应（6 种全覆盖）；状态逐字保留 |
| AI-02 | 未核验事实保持原状：单源 / not_attempted 不被升级；非法核验组合构造期被拒 |
| AI-03 | 引用未来证据 → **拒绝构造**（错误点名那条未来事实）；历史引用"今天"同样被拒 |
| AI-04 | 无证据 → `insufficient_evidence`；`confidence=1.0` 不能替代证据；`require_supported` fail closed |
| AI-05 | 历史假设不读 current state：非法 as-of 构造期拒绝；无 snapshot 的 reading 拒绝签发；契约无时钟依赖 |
| AI-09 | **verified + relation=context → 不 supported** |
| AI-10 | verified + relation=supports → supported |
| AI-11 | verified + relation=contradicts → unsupported；支持+反对并存 → 反对优先 |
| AI-12 | 未核验事实 + supports → insufficient_evidence |
| AI-13 | **unavailable / disagreement + supports → insufficient_evidence（不是 unsupported）**；与"可信反对"reason 不同 |
| AI-14 | 同 identity 冲突事实状态 → `EvidenceConflict`，**A,B 与 B,A 同一结果**；完全相同安全去重 |
| AI-15 | 同一条 evidence 两种 relation → `EvidenceRelationConflict`，顺序无关 |
| AI-16 | relation 是最小闭集，评分档位词全部被拒 |
| AI-17 | 嵌套 payload 深冻结：原始 dict 继续改写不影响契约内部值；嵌套写入被拒；list→tuple、set→frozenset |
| AI-18 | 非 JSON-like 值（任意可变对象）fail closed；标量与 None 允许 |
| AI-19 | 投影自带 `authority="research"` / `is_authoritative=False` / relation / verification |
| AI-20 | 研究词汇与 signal 生命周期**不相交** |
| AI-OWNER-01 | **raw 构造被拒**（3 种 source_type）；不得存在可 import 的构造哨兵 |
| AI-OWNER-02 | factory 保留 owner 的 identity / as_of / verification / method 原值 |
| AI-OWNER-03 | single_source reading 不得变成 verified |
| AI-OWNER-04 | 无法证明 as_of → fail closed；duck-typed 对象拒绝 |
| AI-OWNER-05 | 未来 owner projection 不得进入历史 hypothesis |
| AIG-01 | 契约只依赖 stdlib + `market_data_contract`（未登记 import 即失败） |
| AIG-02 | **28 个 authority 模块**无一反向 import AI 层 |
| AIG-03 | 未登记就消费 AI 层的生产模块 = 0 |
| AIG-04 | 无 DB / 网络 / 文件 / 环境依赖；源码无 SQL 文本 |
| AIG-05 | AI 产物无法被 commit 成 signal（4 种伪造形态，`conn=None` 行为证明） |
| AIG-06 | 契约无任何写入 / 提交入口 |
| 非空性 | 5 条：import 扫描命中 `import` 与 `from … import`；调用扫描命中写调用；SQL 扫描命中 INSERT 且**忽略 docstring** |

### 语义 mutation

`work/r27_ai_mutation_check.py` —— **6/6 CAUGHT，survived=0，fake=0**，还原 sha256 校验通过。

| ID | mutation | 期望变红的唯一永久回归 | 结果 |
| --- | --- | --- | --- |
| M-AI1 | 移除 future-evidence check | AI-03 | CAUGHT |
| M-AI2 | 未核验事实当作已核验（unverified + supports → supported） | AI-12 | CAUGHT |
| M-AI3 | relation 强制成 supports（verified 事实自动支持 thesis） | AI-09 | CAUGHT |
| M-AI4 | 重新打开 raw 构造（authority spoof） | AI-OWNER-01 | CAUGHT |
| M-AI5 | 冲突 duplicate 退化为 first-wins | AI-14 | CAUGHT |
| M-AI6 | deep freeze 退回浅冻结 | AI-17 | CAUGHT |

沿用 R23/R24/R25 的 runner 约定：逐次唯一 `PYTHONPYCACHEPREFIX`（否则 baseline 与
mutant 共享字节码缓存，整张矩阵静默失效）、anchor 必须唯一命中一次、
`SyntaxError` / `ImportError` / `NameError` 计为 FAKE、串行执行（不并行改同一文件）、
结束后 byte-identical 还原并校验 sha256。

### 本地已跑

```text
ruff check（修改的 3 文件）                                PASS
compileall（修改的 3 文件）                                PASS
targeted AI contract tests                                 33/33 PASS
consumer regression
  test_market_data_boundary（R24）                         PASS
  test_signal_pipeline（R25）                              PASS
  test_r26_execution_authority_guard（R26）                PASS
  test_execution_planner（R26）                            PASS
  小计                                                    166 tests PASS
```

production code 只新增纯契约（无 DB、无 I/O），因此未本地跑全量。exact-head CI 由
GitHub 承担；本轮未改 frontend，故不重复本地 browser 五连跑。

#### Exact-head verification

```text
base:            b7c8cd5b414fc7352e83d4e7e18fd834850d783c
                 （Merge pull request #188 —— R27_MASTER_SHA）

production head: 见下方 push 后的 R27A_HEAD_SHA（承载全部 production / test /
                 ARCHITECTURE 改动的那一个提交）

exact-head CI:   PASS 9/9 —— tests 3.11 / tests 3.12 / syntax / quality /
                 docker-smoke / frontend / browser-e2e / security-leak-scan ×2
```

merge authority 是**当前 PR HEAD 的 exact-head CI**（从 GitHub API 读取），
不是文档里记录的某个 SHA：后续只改这份 body 源文件的文档提交不会使上面的
production head 失效，而它同样必须自己通过一遍 exact-head CI。

---

## 10. 可维护性

- **只有 1 个 production 模块**，且它能回答下列全部问题：
  - 拥有哪个业务问题？ → "AI 看到的哪些已存在事实，以什么关系，能支撑什么研究结论"
  - 输入是什么？ → owner 的 typed projection（经 factory）+ 显式 relation
  - 输出是什么？ → `InformationEvent` / `ResearchEvidenceRef` /
    `HypothesisEvidence` / `ResearchHypothesis`
  - 谁允许调用它？ → 未来登记在 `ALLOWED_AI_CONSUMERS` 的消费者（当前 0）
  - 它能调用谁？ → 只有 `market_data_contract`（纯契约）与标准库
  - 为什么不能放进现有模块？ → 研究语义不属于行情 / signal / 执行 / 风控任何
    authority；塞进去会让那个 authority 反向依赖研究层
  - 为什么认知复杂度下降？ → 把"AI 看到什么、事实多可信、它对结论什么关系、够不够"
    从散落的叙事字符串收敛成 4 个不可变类型 + 派生判据
- **依赖方向**：单向 `AI → R24`，由 AIG-01/AIG-02/AIG-03 三道 AST guard 钉住。
- **理解一个假设所需模块数**：1（`ai_research_contract.py`）＋ 读取事实时 1 个 owner。
  一个开发者可以在**同一个模块**里顺着 fact → relation → hypothesis 读完整个逻辑，
  因此**不做**机械 LOC 拆分。
- **重复规则引入**：0。核验合法性委托 R24、双源判据委托 `is_cross_source_verified`、
  事实来自 owner projection。
- 简单 `if` 保留：`_derive_status` 是约 15 行直白分支，**没有**为它建 rule engine。
- status 判定顺序即业务语义（可信对照优先），不引入 policy engine。

### 新增 facade / manager / helper 数量：0

`evidence_ref_from_market_reading` 是**唯一的**公开签发入口，且是必要而非装饰：
它让"逐票报价如何成为一条可判定的 owner 事实"只有一个 owner，否则每个消费方都会
各自解释 `quote_validation` / `verification`，同一份事实在不同路径上得到不同结论。

---

## 11. 完成标准自查

| 问题 | 答案 |
| --- | --- |
| AI 看的是哪份事实？ | owner 的 typed projection，引用化为 `ResearchEvidenceRef` |
| 事实来自谁？ | owner factory 签发；`source_type` 闭集 |
| 事实的 as_of 是什么？ | 必填、可证明，只取自 owner 投影（调用方不可覆盖） |
| 事实是否 verified？ | `verification` + `verification_method` 逐字来自 owner，合法性由 R24 独占判定 |
| AI 结论引用了哪些 evidence？ | `ResearchHypothesis.evidence`，去重后逐条投影（含 relation） |
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
