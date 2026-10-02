# Strategy Platform（策略平台）

本文描述**当前**策略平台的数据模型与运行语义：内置策略模板（builtin）+ 声明式用户策略（user）。

- 历史版本与旧架构记录见 [`CHANGELOG.md`](../CHANGELOG.md)、[`RELEASE-v0.11.0.md`](RELEASE-v0.11.0.md)、[`RELEASE-v0.12.0.md`](RELEASE-v0.12.0.md) 与 [`archive/`](archive/README.md)，它们保留当时的真实口径，不回写。
- 系统分层与调用边界见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)；自进化见 [`EVOLUTION_ARCHITECTURE.md`](EVOLUTION_ARCHITECTURE.md)；模块地图见 [`REPOSITORY_LAYOUT.md`](REPOSITORY_LAYOUT.md)。

> **一句话**：策略是**声明式定义 + 不可变版本**。用户策略**不运行 Python**，只提供 DSL；最终下单数量、止损深度、资金分配与执行方式由平台编译、收紧与决定。

## 1. 身份（identity）

| 概念 | 位置 | 说明 |
| --- | --- | --- |
| `id` | `strategy_definitions.id` | 稳定身份，创建后不可改：`^[a-z][a-z0-9_]{2,63}$`（`strategy_registry._ID_PATTERN`） |
| `name` | `strategy_definitions.name` | 展示名，可改；不参与身份判定 |
| `origin` | `strategy_definitions.origin` | `builtin` 或 `user`（`strategy_registry.ORIGINS`）。内置 id 保留，用户不能占用 |
| `implementation_key` | 同上 | 内置模板的原生实现入口；用户策略为空（走 DSL 通道） |
| `supports_new_cycle` | 同上 | **派生字段**：`status == 'active'` 且运行时就绪时为 1；仅供下一周期资格判定 |
| `sort_order` | 同上 | 仅展示排序 |

身份一旦建立：内置策略不能被删除或改名；用户策略可以被归档，id 不再复用。

## 2. 版本与校验和（version / checksum）

- 不可变快照存在 `paper_strategy_versions`：每次定义变更（名称、说明、元数据、DSL、参数值）都追加一个新版本，旧版本永久保留。
- 头部指针（head）决定"下一周期用哪个版本"；**正在运行的周期绑定自己的版本**（`strategy_registry.bind_cycle_versions`），因此之后的编辑不会把历史信号/订单/审计的证据重新贴到新定义上。
- 写入使用乐观并发：调用方必须给出 `expected_version`，不匹配即冲突（HTTP 409），绝不静默覆盖。
- `structure_checksum` 是 DSL 规范化的内容校验和（`strategy_dsl_schema.checksum`）。订单、成交、持仓、审计行都带 `strategy_id` + `strategy_version` + `strategy_checksum`，指向**确切字节**；这也是删除受限的根因（见第 11 节）。
- 版本号单调递增，不回收；回滚通过"以旧版本内容创建一个新版本"表达，而不是改写历史。

## 3. 声明式 DSL（不执行用户代码）

用户策略的唯一可执行载体是 DSL AST：

- **白名单节点**：布尔（`and` / `or` / `not`）、比较、`cross_above` / `cross_below`、算术（`mul`）、`field`、`indicator`、`const`、`parameter`（`strategy_dsl_schema`）。
- **白名单字段与指标**：`field.name` 与 `indicator.name` 都在允许集合内校验；rolling window 必须是 1..`MAX_ROLLING_WINDOW` 的整数，或一个整数型 `parameter`。
- **没有任何代码执行通道**：DSL 模块内不存在 `eval` / `exec` / `__import__` / 动态导入；用户无法提交 Python、模块名或回调。
- **离线求值**：`strategy_dsl_evaluator.evaluate` 是纯函数，输入是已读取的行情/因子表，输出候选；无网络 I/O、无订单写入，`fail-closed`（求值异常=不产生候选）。
- **内置模板**保留原生实现路径（`implementation_key`），可以没有 DSL AST；**用户策略必须有合法 DSL AST**，否则 `runtime_ready=false`（见第 5 节）。

DSL 的边界（三条硬约束）：

1. 策略**不能指定最终下单数量**：`qty` / `quantity` / `shares` / `amount` / `target_qty` 等字段属于执行器，出现即整条信号终态拒绝（`order_intent.reject_qty_claims`）。数量由 `paper_sizing.price_aware_qty` 依据现金、风险预算、敞口、行业与整手约束计算。
2. 策略**不能放宽系统风控**：只能收紧（见第 9 节与 [`../ARCHITECTURE.md`](../ARCHITECTURE.md) 的风险层次）。
3. 策略**不能自行决定执行方式**：订单类型、TTL、紧急度来自编译出的 Execution Profile。

## 4. 生命周期（lifecycle）

R31 起，身份/版本与生命周期分别由 `strategy_registry`、`strategy_lifecycle` 唯一拥有；promotion evidence 由 `strategy_promotion` 判定。状态值统一为 lowercase snake_case：

```text
draft → candidate → research → validated → shadow → paper → production_sim
paper / production_sim → degraded / paused / retiring / quarantined
degraded → paused / retiring / quarantined
paused → retiring / quarantined
retiring → archived
archived → （终态，无出边）
candidate / research → rejected
research → validation_failed
```

| 状态 | 含义 | 允许的下一步 |
| --- | --- | --- |
| `draft` / `candidate` / `research` | 编辑、候选和研究阶段，不进入正式新周期 | 由显式 transition table 决定 |
| `validated` | exact version 已通过 R29 PIT/OOS validation | `shadow`，或安全迁移 |
| `shadow` | exact version 已通过 R29+R30 promotion policy；R32-C 提供隔离式 Challenger run evidence，R32-D 提供 Active/Challenger 比对证据 | `paper` 仍由 R31 Promotion Policy 判定；R32-C/D 都不做晋级 |
| `paper` | 可进入新的 paper simulation 周期 | `production_sim` 暂因下游 runtime evidence owner 缺失而 blocked，也可安全降级/暂停/退休 |
| `production_sim` | 当前最高正式模拟运行级别 | 安全降级、暂停或退休 |
| `degraded` / `paused` | 禁止新正式周期；保留版本与历史 | 暂停或退休 |
| `retiring` | 停止新周期并进入退休流程 | `archived` |
| `archived` / `rejected` / `validation_failed` | 终态，保留定义、版本和审计 | — |
| `quarantined` | 安全隔离，不等于归档 | 无恢复 evidence owner 时 fail closed |

新正式周期只允许 `paper` 与 `production_sim`。每个状态属于 `strategy_id + strategy_version + strategy_checksum`；新版本自动从 `draft` 开始，旧版本历史和已 pin 周期保持原样。AI 可以创建 promotion proposal，但不能写 lifecycle state。

## 5. 运行时就绪与 RuntimeContext

`strategy_registry.runtime_readiness()` 为 `draft → candidate` 编译四项契约，全部通过才算就绪：

| 检查 | 内容 |
| --- | --- |
| `dsl_compiled` | DSL 规范化通过；用户策略**必须**有 AST |
| `fingerprint_valid` | 风险指纹可编译（`strategy_risk_fingerprint`） |
| `risk_profile_compiled` | 风险画像可编译（`strategy_risk_profiles`） |
| `execution_profile_compiled` | 执行画像可编译（`execution_profiles`） |

### R32 阶段状态与可比较运行上下文

R31、R32-A（Comparable Runtime Context）、R32-B（Active Comparable Evidence Closure）、R32-C（Isolated Shadow Runtime）、R32-D（Active/Challenger Comparison Evidence）已完成；R32-E1（Comparable Evidence Provenance Closure）正在审核；R32-E2～E4、R33 **NOT STARTED**。Shadow 使用与 Active capture 对齐的 market、quote、tradability、factor fingerprints 和显式 decision instant，同时保留各自 exact strategy identity 与策略专属状态；它只追加写 ShadowRun evidence，不写正式账本。R32-D 只从一份 exact Active evidence、一条 exact ShadowRun 和一份显式 spec 生成不可变比对报告（共享环境七维相等是硬前提，缺失即 UNAVAILABLE/PARTIAL，不查 latest/current），报告只陈述事实与 delta，不产生 winner、综合分或晋级结论。完整契约见 [`R32_COMPARABLE_RUNTIME_CONTEXT.md`](R32_COMPARABLE_RUNTIME_CONTEXT.md)、[`R32B_ACTIVE_COMPARABLE_EVIDENCE.md`](R32B_ACTIVE_COMPARABLE_EVIDENCE.md)、[`R32C_ISOLATED_SHADOW_RUNTIME.md`](R32C_ISOLATED_SHADOW_RUNTIME.md) 和 [`R32D_CHALLENGER_COMPARISON_EVIDENCE.md`](R32D_CHALLENGER_COMPARISON_EVIDENCE.md)。

就绪后，所有消费者读同一份不可变契约 `StrategyRuntimeContext`（`strategy_runtime.StrategyRuntimeContext`）：

```text
strategy_id · version · checksum · definition · compiled_dsl
risk_fingerprint · risk_profile · execution_profile
lifecycle_stage · capital_scale · allocation_runtime
evolution_control · parameter_schema · settings_revision
```

- 缓存键包含 `version` + `checksum` + `settings_revision` + canonical state + `lifecycle_stage`：设置变更或 lifecycle transition 会立即产生新上下文，不会复用旧额度。
- `runtime_ready=false` 只降级为"不可激活/不可进周期"，不会把策略从历史里抹掉。

## 6. 资金生命周期（capital lifecycle）

分配层有五档资金阶段（`paper_allocation.LIFECYCLE_STAGES`），每档一个资金系数，作用于该策略的可分配预算：

| 阶段 | 系数 | 语义 |
| --- | ---: | --- |
| `shadow` | 0.0 | 影子运行：只记录意图与证据，不部署资金 |
| `pilot` | 0.25 | 试点：小额真实（模拟）资金验证 —— **用户自建/AI 生成策略的起点** |
| `standard` | 1.0 | 标准：全额部署（内置模板的起点） |
| `mature` | 1.0 | 成熟：全额部署，规模上限由账户配置决定 |
| `quarantined` | 0.0 | 隔离：停止新开仓，等待人工处理 |

阶段推导顺序（`strategy_runtime.lifecycle_stage_for`）：

1. 若定义含 `metadata.lifecycle_stage`（或 `metadata.allocation.lifecycle_stage`），它只调节资金分配画像，不改变 canonical state，也不能赋予正式周期资格；
2. 否则按 canonical state 推导：`draft/candidate/research/shadow→shadow`、`validated→pilot`、`degraded/paused/retiring/archived/rejected/validation_failed/quarantined→quarantined`、`paper/production_sim→builtin: standard / user: pilot`；
3. 未知状态 `fail-closed → quarantined`。

**试点是默认值，不是建议**：新用户策略以 25% 预算上线，验证后再人工晋升到 `standard`。系数只缩放该策略的可部署预算，不改变共享池硬上限、单票/行业上限、T+1 或行情门禁。

## 7. 下一周期资格与周期快照（eligibility / cycle snapshot）

三件事必须分清：

| 概念 | 由谁决定 | 何时生效 |
| --- | --- | --- |
| 能不能进**下一**周期 | `strategy_lifecycle.allows_formal_cycle(exact current state)`：只允许 `paper` / `production_sim` | 创建/启动新周期时 |
| 是否**真的**进下一周期 | 设置中心的 `enabled_strategies`（候选来自 lifecycle eligible projection，逐项校验） | 创建/启动新周期时写入周期快照 |
| 是否在**当前**周期执行 | 周期快照与其 pinned exact version，再应用当前安全状态 | 整个周期内不改写版本绑定 |

- **生命周期状态 ≠ 当前周期成员关系**。`paper`/`production_sim` 只说明该 exact current version 有资格进入新周期；已运行周期继续使用它 pinned 的版本，不会被改写或自动收编新版本。
- 新周期创建时，`enabled_strategies` 与该周期的账户挂接、版本绑定一起落库（`paper_cycles.enabled_strategies` + `strategy_registry.bind_cycle_versions`）。旧周期与归档快照保持不可变。
- **零策略是合法状态**：显式空列表 = Idle 周期，不产生新信号，风控扫描、存量退出与系统调度照常（`paper_trading._cycle_participant_resolution` 的 `cycle_idle`）。"未配置"与"显式空"语义不同：缺失/损坏才回落到内置集合。
- 执行层参与者的权威口径（`paper_trading.current_cycle_participant_ids`）：

```text
参与者 = paper_cycles.enabled_strategies
       ∩ paper_accounts.cycle_id == 当前周期
       − pinned exact version 当前不允许正式执行的 lifecycle state
```

  解析来源会写进审计（`cycle_snapshot` / `cycle_enabled_unbound_fallback` / `cycle_idle` / `cycle_not_configured` / `no_cycle`），便于回放时解释"为什么这轮没有它"。

## 8. 经济所有权 ≠ 执行参与（PR-48）

这是最容易误解的一条，也是运行时的硬不变量：

| | Cycle Ledger Ownership（经济所有权） | Execution Participation（执行资格） |
| --- | --- | --- |
| 含义 | 该策略在当前周期账本里占有的初始资金/净值归属 | 该策略是否还能产生新信号、新委托 |
| 受什么影响 | 周期创建时的快照与资金分配 | pinned exact version 的安全状态、周期快照、风控门禁 |
| 暂停时 | **不变**（仍计入共享池合计与 NAV） | **被剔除**（不再新开仓） |

因此：

- 安全迁移只关闭新执行资格，**不会**把这个策略的当前周期资金/净值从账本里删掉；
- 任何后续状态变化都不会凭空放大资本（账本合计仍是原值）；
- 周期资本合计恒等于快照分配之和，与"当前有几个策略在动"无关（回归：`backend/test_cycle_ledger_ownership.py`）。

## 9. 风险与执行画像（risk / execution profiles）

```text
DSL AST ─► Risk Fingerprint ─► Risk Profile ─► Enforcement ─► 生产风控参数
                                                 │
                              Execution Profile ─┘
```

- **Risk Fingerprint**：从 DSL 结构推导策略意图（archetype、置信度、结构指纹）。
- **Risk Profile**：把指纹编译成模板化的 `hard_rules` / `soft_limits` / `evolvable_params` / `user_locked_params`。
- **Enforcement**（`strategy_risk_enforcement`）：把画像接入生产参数时**一律取更紧**——帽类取 `min(生产现值, 模板值)`，纪律类取更早/更浅/更小；解析失败 `fail-closed` 落到最保守模板。审计写 `risk["compiled_risk_profile"]`（含每键 before/after），可回放"为什么这笔单被收紧"。
- **Execution Profile**：订单类型、limit offset、TTL、紧急度、是否批处理、是否要求成交核验等执行属性；自动策略与手动委托共用同一个 Execution Planner。

**策略只能收紧风险，不能放宽。** 系统级规则（T+1、证券范围、行情新鲜度、全池敞口、系统回撤）由平台全局拥有，策略画像里不含这些键，无法触碰。

## 10. 克隆（clone）

- 克隆内置模板或已有策略会创建一个**新的用户策略**：新 id、`origin=user`、版本从 v1 开始、生命周期 `draft`、资金阶段 `pilot`。
- 源策略的不可变版本被复制为新的定义快照；之后两者各自演进，互不影响。
- 内置策略本身不可编辑：Web 的路径是"复制内置策略 → 在副本上改"（`wbCloneFirstBuiltin` / `wbCloneStrategy`）。

## 11. 删除限制（hard delete，PR55-HF 不变量）

**规则**：物理删除**只**允许用于**从未离开 draft 生命周期、且没有任何历史/经济/执行引用**的用户策略。一旦存在正式生命周期历史，就只能归档，不能物理删除。

形式化条件（全部必须成立，`strategy_registry.hard_delete_unused_draft`）：

```text
origin == 'user'
∧ canonical lifecycle state == 'draft'
∧ never_left_draft            # 生命周期历史从未离开 draft（PR-56 canonical 谓词）
∧ no historical reference     # 账本/审计/执行引用为空
```

关键细节：

- **`draft → validated → draft` 的回退不能重新获得删除资格**：生命周期历史本身就是"已正式使用"的证据（`_ever_left_draft` / `_left_draft_evidence`）。
- **多版本草稿仍可删**：只要从未离开 draft，v2/v3 也只是 scratch 状态。
- **数据库层是 default-deny**：`paper_strategy_versions` 上的触发器拒绝一切删除，除非同一事务内存在针对"`origin='user'` 且 `status='draft'`"定义的一次性授权行；授权行只由上述函数在两道门都通过后铸造，且随事务消失。
- **内置策略永远不可删**；`archived` 不是删除——定义、全部版本、审计与历史周期都保留，可回放。
- API 语义：删除请求若命中历史引用，返回 domain 错误并附带可执行的替代路径提示（"请使用归档（transition → retiring → archived）而非删除"）。

## 12. 与设置中心、策略工坊的边界

| 界面 | 负责 | 不负责 |
| --- | --- | --- |
| 策略工坊（Strategy Workbench） | **策略定义**：身份、DSL、版本、生命周期、预览 | 不决定资金/席位/共享池参数 |
| 设置中心（Settings） | **运行参数**：资金、周期、下一周期参与集合、共享池风控、AI 开关 | **不实现第二套 DSL 编辑器**，不编辑策略定义 |

设置中心里的策略勾选框只是"下一周期参与集合"的选择器：候选来自 canonical lifecycle owner，当前 exact version 处于 `paper` 或 `production_sim` 时才可选；保存后写入运行库并留审计。被 `paused` 的策略给出"去策略工坊恢复"的入口，而不是在设置页里改生命周期。

## 13. 代码与测试索引

| 主题 | 代码 | 回归 |
| --- | --- | --- |
| 身份 / 版本 / 生命周期 | `backend/strategy_registry.py` | `backend/test_strategy_registry_lifecycle.py`、`backend/test_strategy_version_immutability.py` |
| 应用服务 / 事务边界 | `backend/strategy_service.py` | `backend/test_strategy_service.py`、`backend/test_strategy_api_contract.py` |
| DSL | `backend/strategy_dsl_schema.py`、`backend/strategy_dsl_evaluator.py` | `backend/test_strategy_dsl.py`、`backend/test_strategy_invariants.py` |
| 运行时契约 | `backend/strategy_runtime.py` | `backend/test_strategy_runtime.py`、`backend/test_strategy_spec_resolution.py` |
| 风险画像与收紧 | `backend/strategy_risk_*.py`、`backend/asymmetric_risk.py` | `backend/test_strategy_risk_enforcement.py`、`backend/test_asymmetric_risk.py` |
| 订单意图 / 执行计划 | `backend/order_intent.py`、`backend/execution_planner.py` | `backend/test_order_intent_contract.py`、`backend/test_execution_planner.py` |
| 分配与资金阶段 | `backend/paper_allocation.py` | `backend/test_paper_allocation.py`、`backend/test_strategy_properties.py` |
| 周期快照 / 所有权 | `backend/paper_trading.py` | `backend/test_cycle_participant_resolver.py`、`backend/test_cycle_ledger_ownership.py` |
| 删除限制 | `backend/strategy_registry.py` | `backend/test_strategy_hard_delete_lifecycle.py`、`backend/test_strategy_archive_replay.py` |
| 端到端产品线 | — | `backend/test_strategy_product_line_e2e.py`、`backend/test_production_path_golden_replay.py` |
| 浏览器端 | `frontend/src/features/strategies.js` | `frontend/e2e/specs/strategy-*.spec.js` |
| Active/Challenger 工作区（read model + 单一读端点） | `backend/strategy_service.py`（`challenger_read_model`）、`backend/api_strategies.py`（`GET /api/strategies/{id}/challenger`）、`frontend/src/features/strategies.js`（`wbChallengerHtml`） | `backend/test_r32_final_promotion.py`（W1–W7）、`frontend/tests/challenger-workspace.test.mjs`（W8–W16） |

## R32 最终：两条晋级链与 Entry 侧边界

```text
生命周期晋级（Lifecycle Promotion）
  Active Evidence + ShadowRun → ShadowComparisonReport → strategy_promotion
  → PromotionDecision → strategy_lifecycle.transition() → shadow/paper 状态

参数头晋升（Parameter-Head Champion Activation）
  Challenger 参数 → strategy_champion.compare_for_promotion（指标/容差）
  → promote_challenger → self_evolution.activate_params_candidate → 正式参数头
```

两条链互不越权（无 cross-mutation），UI 与文档都不把它们合并成同一个 Ready/Promote 按钮或综合 readiness。

Entry 侧按 owner 分裂，不追求「单一 Entry Authority」：Entry/Admission 只拥有准入及其 owner
evidence；`risk_scale` 是 sizing/market-exposure policy（`paper_trading._strategy_market_policy`，
被 `_buy_order` 作为 sizing 输入消费于 `paper_trading.py:9772`）；红灯板块热点的 `shadow_exception`
是 research/shadow observation policy；dispatch/fill 归 Execution Authority。
`execution_planner.EntryGateState` 不承载 sizing 或 observation 字段，manual 路径因此不会继承
Active 的 per-strategy 缩放。

### R33-A：Exact Strategy Health Evidence（事实层）

```text
POST /api/strategies/{id}/health/snapshots        （显式 capture：exact version + 显式窗口）
GET  /api/strategies/{id}/health/snapshots/{id}   （只按 id 精确读取；没有 latest 端点）

Exact Strategy Version
   ↓  registry runtime readiness（exact version）/ lifecycle / execution 核验结论 /
      risk decision_provenance / 显式 comparison report
StrategyHealthSnapshot（append-only，snapshot_id == snapshot_fingerprint）
   ↓
[STOP —— 退休 policy 属 R33-B，本 PR 未实现]
```

维度固定七项（`runtime_integrity` / `lifecycle_integrity` / `execution_evidence` /
`risk_evidence` / `activity_coverage` / `performance` / `comparable_evidence`），每项带
`status ∈ {AVAILABLE, PARTIAL, UNAVAILABLE, NOT_APPLICABLE}`、`facts`、
`provenance ∈ {OWNER_ISSUED, CAPTURED_INPUT, DERIVED, UNAVAILABLE}`、`source_identity`、
`source_fingerprint`、`blocking_reasons`。

**本轮已登记的缺口（不得用替代物顶上）**：

- `performance`：仓库**没有**「exact strategy version 历史绩效（NAV/return/drawdown/PnL）」的
  owner（`paper_performance` 是日内持仓 P&L；`paper_portfolio_read_model` 明确不发布
  nav/market_value/return），因此该维度恒为 `UNAVAILABLE`（`strategy_performance_owner_unavailable`）。
- `activity_coverage.signals`：`paper_signals` 没有 strategy stamp，而 cycle pin 是
  `(cycle_id, account_id)`，无法把一条 signal 归属到 exact version → 记
  `signal_strategy_attribution_unavailable`，不编造。
- 退休阈值（连续亏损 N 天 / 收益低于 X% / 回撤超过 Y% …）**仓库里不存在**，
  标记 `POLICY DECISION REQUIRED FOR R33-B`，本轮不新增。

### R33-B：Strategy Retirement Policy（policy 层，不执行 lifecycle）

```text
POST /api/strategies/{id}/retirement/evaluate          body: {snapshot_id}   （必须显式 snapshot id）
GET  /api/strategies/{id}/retirement/decisions/{id}    （只按 id 精确读取；没有 status 端点）

StrategyHealthSnapshot → strategy_retirement_policy（纯函数）→ StrategyRetirementDecision
   → LifecycleTransitionProposal（仅结构，executed=false）→ [STOP]
```

决策词表固定为动作候选：`NO_ACTION` / `DEGRADE_CANDIDATE` / `RETIRE_CANDIDATE` / `ARCHIVE_READY` /
`INSUFFICIENT_EVIDENCE`（**没有** BAD_STRATEGY / FAILED_STRATEGY / UNHEALTHY，也没有 score / rank / tier）。
v1 只实现两条规则：证据不完整（任一维 PARTIAL/UNAVAILABLE）⇒ `INSUFFICIENT_EVIDENCE`；
证据完整且无信号 ⇒ `NO_ACTION`。三个动作候选属保留词表 —— 仓库还没有可 owner 化的退休阈值
（drawdown / 连续亏损 / 执行失败率 / 风险违规频率全部 `POLICY DECISION REQUIRED`），
也没有 exact-version 历史绩效 owner，因此**v1 对真实快照只会给出 `INSUFFICIENT_EVIDENCE`**。
policy 不读原始表、不重算收益或风险、不写 lifecycle，也不接受任何「最新/当前」输入。

### Workspace identity 契约

Workspace **绝不**从 registry current head 推导 Active / Challenger 的比对身份：

```text
comparison_report_id 被明确提供且读取成功
  → Active identity      = report.active_strategy_stamp      (exact id/version/checksum)
  → Challenger identity  = report.challenger_strategy_stamp  (exact id/version/checksum)

report.challenger_strategy_stamp.strategy_id != path strategy_id
  → fail closed：shadow_comparison_identity_mismatch（400）
version 显式提供且 != report.challenger_strategy_stamp.version
  → fail closed：shadow_comparison_identity_mismatch（400）

comparison_report_id 缺失 / 取不到
  → 没有 Active comparator fact：active.available=false + unavailable_reason
    （Challenger 仅作为 registry_candidate 展示，comparison_bound=false）
  → challenger 与 registry_head 是两个独立事实，允许不同：
    requested candidate = explicit version（未提供则等于 head）
    registry_head       = 真正的 current head（永不被 requested candidate 覆盖）
```

因此历史 report 保持 exact identity：registry head 前进后，Workspace 仍显示 report 里那个旧版本，
而 Lifecycle Promotion 可以按自己的策略返回 `strategy_version_changed` —— 两个事实同时呈现，
不会把旧 Challenger 静默换成 current head。Active 的 lifecycle state 用**它自己的** exact
`SL.get_state(active_id, active_version, checksum)` 查询，查不到就是 `None`，绝不拿 Challenger 的状态填。
