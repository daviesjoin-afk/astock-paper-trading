# Strategy Platform（策略平台）

本文描述**当前**策略平台的数据模型与运行语义：内置策略模板（builtin）+ 声明式用户策略（user）。

- 历史版本与旧架构记录见 [`CHANGELOG.md`](../CHANGELOG.md)、[`RELEASE-v1.1.0.md`](RELEASE-v1.1.0.md)、[`RELEASE-v1.2.0.md`](RELEASE-v1.2.0.md) 与 [`archive/`](archive/README.md)，它们保留当时的真实口径，不回写。
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

状态机（`strategy_registry.LIFECYCLE_STATUSES` / `_TRANSITIONS`）：

```text
draft      → validated | archived
validated  → draft | active | archived      （回退到 draft 会保留"已离开 draft"的历史）
active     → paused | retiring
paused     → active | retiring | archived
retiring   → archived
archived   → （终态，无出边）
```

| 状态 | 含义 | 允许的下一步 |
| --- | --- | --- |
| `draft` | 编辑中；不参与任何周期 | `validated`、`archived` |
| `validated` | 已通过校验（含运行时就绪）；等待激活 | `draft`、`active`、`archived` |
| `active` | 允许进入**下一**周期；执行层资格仍受周期快照约束（第 7、8 节） | `paused`、`retiring` |
| `paused` | 生命周期暂停：**立即退出执行层**（不再产生新信号/新委托），但保留周期内经济所有权 | `active`、`retiring`、`archived` |
| `retiring` | 只退出、不新开；存量持仓按各自风控退出规则收起 | `archived` |
| `archived` | 终态只读；定义与全部版本永久保留，可回放/可审计 | — |

激活（`validated → active`）必须通过**运行时就绪**检查；就绪失败时激活被拒绝（不是"先激活再修"）。

## 5. 运行时就绪与 RuntimeContext

`strategy_registry.runtime_readiness()` 在激活前编译四项契约，全部通过才算就绪：

| 检查 | 内容 |
| --- | --- |
| `dsl_compiled` | DSL 规范化通过；用户策略**必须**有 AST |
| `fingerprint_valid` | 风险指纹可编译（`strategy_risk_fingerprint`） |
| `risk_profile_compiled` | 风险画像可编译（`strategy_risk_profiles`） |
| `execution_profile_compiled` | 执行画像可编译（`execution_profiles`） |

就绪后，所有消费者读同一份不可变契约 `StrategyRuntimeContext`（`strategy_runtime.StrategyRuntimeContext`）：

```text
strategy_id · version · checksum · definition · compiled_dsl
risk_fingerprint · risk_profile · execution_profile
lifecycle_stage · capital_scale · allocation_runtime
evolution_control · parameter_schema · settings_revision
```

- 缓存键包含 `version` + `checksum` + `settings_revision` + `status` + `lifecycle_stage`：设置变更、状态迁移（如 `active → paused`）会立即产生新上下文，不会复用旧额度。
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

1. 定义元数据显式指定 `metadata.lifecycle_stage`（或 `metadata.allocation.lifecycle_stage`）——人工晋升/隔离入口；
2. 否则按状态推导：`draft→shadow`、`validated→pilot`、`paused/retiring/archived→quarantined`、`active→builtin: standard / user: pilot`；
3. 未知状态 `fail-closed → quarantined`。

**试点是默认值，不是建议**：新用户策略以 25% 预算上线，验证后再人工晋升到 `standard`。系数只缩放该策略的可部署预算，不改变共享池硬上限、单票/行业上限、T+1 或行情门禁。

## 7. 下一周期资格与周期快照（eligibility / cycle snapshot）

三件事必须分清：

| 概念 | 由谁决定 | 何时生效 |
| --- | --- | --- |
| 能不能进**下一**周期 | Registry：`active` ∧ `supports_new_cycle` | 创建/启动新周期时 |
| 是否**真的**进下一周期 | 设置中心的 `enabled_strategies`（候选来自 Registry，逐项校验） | 创建/启动新周期时写入周期快照 |
| 是否在**当前**周期执行 | 周期快照（本次创建时冻结的集合） | 整个周期内不变 |

- **激活 ≠ 参与当前周期**。`active` 只说明"下一周期可以启用"；已在运行的周期不会被改写，也不会自动收编新激活的策略。
- 新周期创建时，`enabled_strategies` 与该周期的账户挂接、版本绑定一起落库（`paper_cycles.enabled_strategies` + `strategy_registry.bind_cycle_versions`）。旧周期与归档快照保持不可变。
- **零策略是合法状态**：显式空列表 = Idle 周期，不产生新信号，风控扫描、存量退出与系统调度照常（`paper_trading._cycle_participant_resolution` 的 `cycle_idle`）。"未配置"与"显式空"语义不同：缺失/损坏才回落到内置集合。
- 执行层参与者的权威口径（`paper_trading.current_cycle_participant_ids`）：

```text
参与者 = paper_cycles.enabled_strategies
       ∩ paper_accounts.cycle_id == 当前周期
       − 生命周期为 paused 的策略
```

  解析来源会写进审计（`cycle_snapshot` / `cycle_enabled_unbound_fallback` / `cycle_idle` / `cycle_not_configured` / `no_cycle`），便于回放时解释"为什么这轮没有它"。

## 8. 经济所有权 ≠ 执行参与（PR-48）

这是最容易误解的一条，也是运行时的硬不变量：

| | Cycle Ledger Ownership（经济所有权） | Execution Participation（执行资格） |
| --- | --- | --- |
| 含义 | 该策略在当前周期账本里占有的初始资金/净值归属 | 该策略是否还能产生新信号、新委托 |
| 受什么影响 | 周期创建时的快照与资金分配 | 生命周期 `paused`、周期快照、风控门禁 |
| 暂停时 | **不变**（仍计入共享池合计与 NAV） | **被剔除**（不再新开仓） |

因此：

- `pause` 只关闭执行资格，**不会**把这个策略的当前周期资金/净值从账本里删掉；
- 恢复（`resume`）只是把执行资格放回来，**不会**凭空放大资本（账本合计仍是原值）；
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
∧ lifecycle_status == 'draft'
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

设置中心里的策略勾选框只是"下一周期参与集合"的选择器：候选由 Registry 提供（`active` ∧ `supports_new_cycle`），保存后写入运行库并留审计；被 `paused` 的策略给出"去策略工坊恢复"的入口，而不是在设置页里改生命周期。

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
