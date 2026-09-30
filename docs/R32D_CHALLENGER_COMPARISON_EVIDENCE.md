# R32-D：Active / Challenger Comparison Evidence

状态：R32-D IN REVIEW。本文记录开始编码前的生产路径盘点与本阶段契约，以及实际落地的验证结果。R32-D 只建立 Active 与 Challenger 之间**不可变、确定性、可审计、可重放**的 comparison evidence；生命周期转换、自动晋级、winner、综合打分、AI 选策略与前端工作区都不在范围内（属 R31 / R32-E / R33）。

实现位置：`shadow_comparison`（纯 domain/report contract）、`shadow_comparison_repository`（append-only/idempotent 持久化）、`shadow_comparison_service`（显式证据加载与编排）、migration v25（`paper_schema_migrations.ensure_shadow_comparison_reports`）。

## 一、开始编码前的 production inventory

盘点的是合并 PR #218 之后（base `3ddc12a`）的实际代码，不是猜测。

### 1. Active 的 decision-snapshot-v3 在哪里生产

`paper_decision_audit.build_decision_snapshot()`（`backend/paper_decision_audit.py:209`，版本常量 `DECISION_SNAPSHOT_VERSION = "decision-snapshot-v3"` 在 `:17`）是唯一 serializer；`paper_trading` 只是兼容 facade（`_decision_snapshot` `paper_trading.py:1231`、`_with_decision_snapshot` `:1265`）。它产出选股/风控侧的 audit envelope（quote / kline / financial / news / factors / threshold / runtime_context / data_quality / final），**不产出** entry gate 结果。

envelope 的落点是 JSON 列，不是独立表：

- `paper_signals.payload`（`paper_trading.py:1904`）：由 `signal_service.commit_signal()` 注入 `signal_decision` / `signal_evidence`（`signal_service.py:444-447`），并携带调用方写入的 `decision_snapshot`。
- `paper_orders.risk_payload`（`paper_trading.py:1915`）：`_json(risk)`（写入点 `:10096`）携带 `decision_snapshot` 与 `paper_trading` 自己的 gate dict（`risk["chase_entry"]` `:9412`、`risk["three_day_timing_gate"]` `:9431`、`risk["entry_price_gate"]` `:9435`、`risk["execution_dispatch"]` `:10031`）。
- `paper_risk_decisions.payload`（`paper_trading.py:1956`）：`_risk_log`（`:3280-3297`）写入。

### 2. Active snapshot 的稳定持久 identity

**没有**独立的 Active decision evidence 表，也没有专门的 evidence id 列。可用作外部引用的候选：

| 候选 | 位置 | 是否稳定 |
| --- | --- | --- |
| `paper_orders.id` | `paper_trading.py:1911` PK AUTOINCREMENT | **稳定**（不可变主键） |
| `paper_signals.id` | `paper_trading.py:1901` PK AUTOINCREMENT | 稳定 |
| `paper_signals` 自然键 | `UNIQUE(account_id, signal_date, code)` `:1908` | 稳定 |
| `paper_orders.signal_id` | `:1912` | 不唯一（只有 retry 的部分唯一索引 `:1624-1626`） |
| `paper_orders.execution_version` | migration 列 | **不稳定**：blocked 尝试（`execution_planner.py:1922`）与成交（`:2008`）都会自增 |
| `paper_orders.cycle_id` / strategy stamp | `:1917-1921` | 多行重复，不能单独定行 |

因此：**exact Active evidence 的 identity = `(source table, 主键)` + 对该行 evidence 投影的 canonical fingerprint**。因为 `paper_orders` 的 `status` / `execution_evidence` 会被原地更新，fingerprint 必须是对**加载当刻**的投影取哈希，并把 `source_identity` / `source_schema_version` / `source_fingerprint` 三者一起保存（只存一个无法解释的 hash 是不够的）。同一 order id 在行内容变化后重放会得到不同 fingerprint —— 这是可检测的，不是静默漂移。

可复用的 canonical 哈希只有一份：`shadow_runtime.canonical_json()` / `shadow_runtime.fingerprint()`（`shadow_runtime.py:77-83`）。R32-D 复用它，不写第二份实现。

### 3. Active Signal / Entry / Execution 的 exact evidence 分别在哪里

| 决策阶段 | 证据位置 | 形态 |
| --- | --- | --- |
| Signal | `paper_signals.payload["signal_decision"]` + `payload["signal_evidence"]` | `signal_service.decide_signal()` / `signal_evidence()` 的投影（`signal_service.py:319` / `:233`） |
| Entry | **不存在** | `execution_planner.evaluate_entry_state()`（`execution_planner.py:1494`，返回 `{allowed,reasons,gates,policy}`，`:1585-1597`）在 Active 路径上**不落库**；`risk_payload` 里是 `paper_trading` 自己的 gate dict，形状不同，不是同一份事实 |
| Execution | `paper_orders.execution_evidence`（TEXT，migration 列）+ `execution_reasons` / `execution_asof` / `ruleset_version` / `remaining_qty` | `execution_planner._decision_evidence()`（`execution_planner.py:1768-1804`）的投影，含 `runtime_context`、`runtime_context_fingerprint`、`runtime_context_availability`、`runtime_context_unavailability_reason` |

### 4. Active runtime context availability 的持久化

由 `_decision_evidence()` 写进 `paper_orders.execution_evidence`（`execution_planner.py:1789-1803`）：`runtime_context` 为 `ComparableRuntimeContext.projection()`，availability 为 `"AVAILABLE"`/`"UNAVAILABLE"`，不可用时带稳定 reason（取值域是 `simulation_runtime_context.ACTIVE_CONTEXT_UNAVAILABLE_REASONS`，`simulation_runtime_context.py:19-29`）。选股侧的同一信息另有一份在 `decision_snapshot.runtime_context`（`paper_decision_audit.py:455-462`）。

R32-D 只认 Execution Authority 那一份（`paper_orders.execution_evidence.runtime_context`）作为 Active 环境身份来源，避免同一事实有两个权威。

### 5. ShadowRun 中可直接用于 comparison 的字段

`shadow_runs.evidence_json` → `shadow_runtime.ShadowRunEvidence`：

- run 级：`run_id` / `run_fingerprint`、`spec`（challenger 与 active_comparator exact stamp、`environment_fingerprint`、`session_date`、`decision_at`、`reference_capital`、`previous_shadow_run_id`）、`environment.identity`（七个共享维度 + `environment_fingerprint`）+ `environment.active_runtime_context`（R32-B Active capture 凭证）、`challenger_runtime_inputs.entry_policy`（冻结的 entry policy identity）、`before_state` / `after_state`（reference cash、positions、sellable、consumed、turnover）。
- observation 级：`decisions[i]` = `{symbol, side, candidate{desired_quantity, reference_price, order_type, entry_gate_state, risk_policy_identity}, signal(outcome/status/reason/evidence), entry(allowed/reasons/gates/policy/requires_manual_entry_review), execution(...)}`。

因此 signal / entry / execution / entry policy / risk identity 都能直接读回，无需重算。

### 6. Active 与 Shadow 的 exact observation key

两边都能稳定给出的维度是 `(symbol, side)`：Shadow 侧的 `decisions` 已按 `(symbol, side)` 去重（`shadow_runtime.evaluate_shadow` 拒绝重复腿），Active 侧的 `paper_orders` 有 `code` / `side`。会话与决策时刻不放进 key，而是由 spec 与两份证据共同校验（同一 comparison 只覆盖一个 session + decision instant，见 §三）。

Active 侧同一 `(symbol, side)` 出现多行（例如 retry）时**没有**稳定顺序，属于 ambiguous duplicate → fail closed，不按 DB row order 决定。

### 7. 可复用的既有 comparison / report authority

- `tradability_shadow.ShadowComparison`（`backend/tradability_shadow.py:657` 起，表 `tradability_shadow_comparisons`）：比较的是 production verdict vs tradability archive，**不是**策略 vs ShadowRun；无可复用语义，只作模式参考。
- `strategy_champion.compare_for_promotion()`（`backend/strategy_champion.py:268`）：champion/challenger 指标比较，属晋级路径，R32-D 不调用（§禁止 winner）。
- `robustness_contract.report_fingerprint` + `robustness_repository`：另一条 append-only report 路径，语义不同。
- Active vs ShadowRun 的 comparison 函数**当前不存在**。

结论：R32-D 需要新建 comparison authority，且必须复用 `shadow_runtime` 的 canonical JSON / SHA-256，不另立第二份。

## 二、权威与输入边界

- **Active evidence authority**：`paper_signals` / `paper_orders` / `paper_risk_decisions` 的既有 writer。R32-D 只读，且只读被显式点名的行；不回填、不修改任何 Active 事实。
- **Shadow evidence authority**：`shadow_runs` append-only 证据（R32-C）。R32-D 只按显式 `shadow_run_id` 读取，不回写 comparison 结果进 ShadowRun。
- **Comparison authority**：`shadow_comparison`（纯 domain/report contract）+ `shadow_comparison_repository`（只按 report identity 幂等追加/读取）+ `shadow_comparison_service`（按显式 ID 加载证据并编排）。
- comparison 的输入**只有三种**：explicit Active evidence + explicit ShadowRun + explicit spec。禁止 `latest` / `current` / `head` / provider / archive current lookup：策略最新版本、当前 lifecycle、最新 ShadowRun、最新行情/tradability、当前组合、当前 NAV、当前 Entry Policy、当前 Risk Policy 一律不读。历史证据缺失就是 `UNAVAILABLE` / `PARTIAL`，绝不重新查询现在去“修复”历史。

## 三、契约

### 3.1 ActiveComparisonEvidence

comparison-facing 的不可变引用，**只封装**已持久化的 exact Active evidence，不重算任何 Active 决策：

```text
ActiveComparisonEvidence
    source_schema_version      # "paper-orders-active-evidence-v1"
    source_identity            # {"source": "paper_orders", "order_ids": [...]}
    source_fingerprint         # canonical SHA-256 over orders 投影
    orders                     # tuple[ActiveOrderEvidence]
```

每条 `ActiveOrderEvidence` 只保留持久列与已落库 JSON 的原文投影：order_id / account_id / signal_id / symbol / side / order_status / order_reason / order_type / requested_quantity / planned_price / filled_quantity / filled_price / amount / fees / realized_pnl / created_at / cycle_id / strategy stamp / signal evidence / execution evidence / runtime context（含 availability 与 reason）。

`(symbol, side)` 重复即 fail closed。

**Active order IDs 本身不是 exact evidence identity。** `paper_orders` 行会被原地更新（成交推进、execution_version 自增、status 变化），所以同一 order id 在不同时刻是两个不同的证据。比较必须 pin 住它实际消费的那份投影的 fingerprint：

```text
ComparisonSpec.active_evidence_id  = 必填
        ↓
按显式 order IDs 加载 exact 行
        ↓
投影成 canonical ActiveComparisonEvidence
        ↓
计算 source_fingerprint
        ↓
必须 == declared active_evidence_id
否则 active_evidence_fingerprint_mismatch，fail closed
```

禁止把新算出的 fingerprint 当成"同一次 comparison"自动接受。想比较漂移后的内容，必须**显式重新声明**新的 fingerprint——那是一次不同的 comparison，report identity 也随之不同。`active_evidence_id` 不接受 `None` 或非法 SHA-256。

**capture owner 与调用顺序。** Active evidence 的唯一 capture owner 是
`shadow_comparison_service.capture_active_comparison_evidence(conn, *, active_order_ids)`：它独占 Active 证据的 SQL、投影与 fingerprint，**只依赖显式 order IDs**，不接收 `ComparisonSpec`——spec 要 pin 的正是这次 capture 的 fingerprint，让 capture 依赖 spec 就是循环依赖。正式调用顺序：

```text
evidence = capture_active_comparison_evidence(conn, active_order_ids=ids)
spec = ComparisonSpec(..., active_order_ids=ids,
                      active_evidence_id=evidence.source_fingerprint, ...)
build_and_append_comparison(conn, spec=spec)   # 内部重新 capture 当前行再比对
```

`build_and_append_comparison()` 在写入前**重新 capture** 当前行并比对已声明的 fingerprint：不一致即 `active_evidence_fingerprint_mismatch` 且不写任何 report。旧签名 `load_active_comparison_evidence(conn, spec)` 已删除（迁移后 caller = 0），不留 compatibility wrapper，也不存在第二套 Active evidence query/projection。

### 3.2 ComparisonSpec

```text
comparison_schema_version
active_order_ids                # 显式枚举，不提供 find/latest
active_evidence_id              # 必填：exact Active evidence fingerprint
shadow_run_id
active_strategy_stamp           # 必须等于 ShadowRun.spec.active_comparator
challenger_strategy_stamp       # 必须等于 ShadowRun.spec.challenger
environment_fingerprint
session_date
decision_at
expected_observations           # 显式 (symbol, side) 清单，决定 coverage 的分母
```

`comparison_scope_identity` 由上述显式字段派生（canonical fingerprint），不作为独立可漂移输入。

`expected_observations` 必须显式给出：coverage 的分母**不能**由两腿实际观察到的 key 推导——那正是「只统计成功对齐的 observation」这类失真（§coverage）。service 不替调用方推断 scope。

### 3.3 Environment equality（硬前提）

只有 Active 侧与 Shadow 侧的**七个共享维度**相等才允许比较，逐维绑定：

```text
session_date
decision_at
market_policy_name
market_snapshot_fingerprint
symbol_quote_fingerprints
tradability_evidence_fingerprints
execution_ruleset_identity
```

两侧都计算同一个 canonical 共享环境 fingerprint（Active 侧来自 `paper_orders.execution_evidence.runtime_context`，Shadow 侧来自 `environment.identity`），不只看一个未经验证的字符串。不一致 → `availability = UNAVAILABLE`、`reason = environment_mismatch`，任何 delta 都不计算。

**不**比较两腿的 `ComparableRuntimeContext.context_fingerprint`：其中包含 strategy stamp、entry/execution state、entry policy、risk identity，两腿不同是正常的（R32-A/C contract）。比较用的是共享环境维度，不是完整 strategy runtime fingerprint。ShadowRun 自带的 Active capture context fingerprint 与本次 comparison 所用 Active evidence 的 context fingerprint 之间的关系只作为证据字段记录，不作判据。

### 3.4 Observation identity 与缺失语义

- key = `(symbol, side)`，在同一 comparison scope 内必须唯一；重复即 fail closed。
- 只有一腿有 observation，而另一腿**没有明确的 evaluated 证据**时，另一腿记 `MISSING`：

```text
absence != false
absence != reject
absence != zero
absence != active_only/challenger_only 的业务结论
```

“Challenger 评估过但 signal 未通过”与“Challenger 从未评估该 key”是**两件不同的事**：前者有 `signal.outcome` / `entry.allowed` 证据（AVAILABLE），后者是 MISSING。

### 3.5 七个比较维度

| 维度 | Active 侧来源 | Challenger 侧来源 | 是否 required |
| --- | --- | --- | --- |
| Signal Delta | `paper_signals.payload.signal_decision` / `signal_evidence` | `decisions[i].signal` | 是 |
| Decision Delta | order 自己 `risk_payload.decision_snapshot.final`（buy-path admission） | `decisions[i].entry`（entry gate 原文） | 是 |
| Turnover | `paper_orders.amount`（原始成交名义额） | `after_state.gross_turnover` - `before_state.gross_turnover` + `spec.reference_capital` | normalized 为 best-effort |
| Execution | `paper_orders.execution_evidence` 原文（+ ledger 列作为独立事实） | `decisions[i].execution` 原文 | 是 |
| Risk Rejection | **UNAVAILABLE**（见 3.5.3） | `entry.reasons` + `candidate.risk_policy_identity` | 是 |
| Performance | **UNAVAILABLE**（见 3.6） | `before_state`/`after_state` + 同一 frozen environment 的 quote 价格 | best-effort |
| Coverage | — | — | 是 |

**容器存在 ≠ owner 证据存在。** 每个维度对每条腿给出显式的 evidence state：

```text
PRESENT          该 owner 确实产出了这一阶段的决策证据
NOT_APPLICABLE   owner 自己的证据证明这一阶段根本没发生
MISSING          阶段适用，但 owner 没有为它产出证据
UNAVAILABLE      证据存在但不可用于该阶段
```

维度只有在**两条腿都是 PRESENT** 时才 `AVAILABLE`；一侧 PRESENT、一侧 NOT_APPLICABLE 是 `PARTIAL`（没有可比的对手方事实）；两侧都没有证据是 `UNAVAILABLE`。

### 3.5.1 Decision 维度

Active 的 admission 取 order 自己那一行 `risk_payload` 里的 `decision_snapshot.final`（`paper_trading` buy-path 的 admission 结果：`approved` / `rejected` / `deferred_*` / `execution_gate_*`）——这是**精确 linkage**（就是该 order 自己的列），而且不是 `paper_orders.status`。两条腿的 admission 来自**不同 authority、不同词汇**，没有任何 owner 定义过映射，因此 comparison 只并列两份原文，并显式标注 `cross_vocabulary_relation_defined = False`、`cross_vocabulary_admission_relation = UNAVAILABLE`。**不发明映射，也不把 order lifecycle status 冒充成 admission。** 若干路径（例如 frozen-entry waitlist）的 `risk_payload` 不含 decision snapshot → Active admission 记 `MISSING`，维度不 `AVAILABLE`。

order lifecycle 事实（`status` / `reason` / `order_type` / `realized_pnl` / `created_at`）单独放在 run-level `active_order_lifecycle` 段，并显式标注 `used_as_entry_admission = false`、`used_as_execution_result = false`、`used_as_risk_evidence = false`。

### 3.5.2 Execution 维度

Challenger 侧只有**真的存在 `execution` Mapping** 时才算 execution evidence。`candidate` 存在而 `execution = None` 时：

- signal 被 owner 判 blocked（或 entry 明确 `allowed=false`）→ `NOT_APPLICABLE`：owner 自己的决策证明执行阶段没发生；
- entry 已准许但 `execution` 缺失 → `MISSING`：该有证据却没有。

Active 侧只有 `paper_orders.execution_evidence`（Execution Authority 自己那一份）存在时才是 `PRESENT`；缺失记 `UNAVAILABLE`（**不**用 ledger status 反推执行结果）。fill quantity 只能来自 owner 的 `fill_quantity`：`None` 永远是 `None`，只有 owner 明确产出 0 时才报 0（`0` 是 owner-issued 事实，不是补出来的）。聚合在没有任何可比 observation 时返回 `None` 而不是 0。

### 3.5.3 Risk Rejection 维度

先做 inventory：`paper_risk_decisions`（`paper_trading.py:1953-1958`）只有 `account_id / code / side / decision / reason / payload / created_at / strategy stamp`，**没有 order 引用**。按 `(account, code, side)` 匹配就是按时间序猜一条（表本身无唯一约束、每次扫描都追加），被显式禁止；`ORDER BY id DESC` / "latest risk decision" 同样禁止；order 的 `status` / `reason` 也被禁止当作 risk 证据。

因此 **Active risk rejection = `UNAVAILABLE`**，并在 payload 里写明原因（`no_order_linked_risk_authority_evidence`、`order_lifecycle_status_used = false`）。Challenger 侧 `entry` 缺失时 `entry_provenance` 记 `UNAVAILABLE`（根本没有 entry decision），`risk_policy_identity` 继续 `DECLARED`。

### 3.6 Turnover 与 Performance 的分母

- Shadow 的 denominator = `ShadowRunSpec.reference_capital`（owner-issued，冻结在 run 里）。
- Active 侧的 raw turnover 有 exact 持久证据（`paper_orders.amount`），可以比较原始值。
- Active 侧**没有**与 Shadow 同语义的 denominator 落在这次比较的 exact evidence 里（`paper_accounts.initial_cash` 是另一张表的列，不在证据 envelope 内）。因此 `active_normalized_turnover = UNAVAILABLE`、`normalized_turnover_delta = UNAVAILABLE`（reason `active_turnover_denominator_absent`）。**不**读当前 NAV / 当前 cash 来"方便比较"。
- Performance：Shadow 侧可以用 `after_state` 的 reference cash/positions 配**同一 frozen environment** 里的 quote 价格做估值；任一持仓 symbol 在该 environment 里没有 exact frozen valuation price 时，`performance = PARTIAL / UNAVAILABLE`，**不**读最新行情补齐。Active 侧 performance 为 `UNAVAILABLE`（reason `active_performance_evidence_absent`）：本次比较的 Active evidence envelope 不含绑定该 decision instant 的估值证据。drawdown 需要区间 NAV 序列，两侧都不提供，恒为 `UNAVAILABLE`。

### 3.7 Provenance

每个非派生结论都带 provenance，取值域：

```text
OWNER_ISSUED     owner 签发并按其语义落库的事实（Active 持久列、Shadow entry/execution 输出）
CAPTURED_INPUT   由调用方捕获、作为运行输入冻结保存（Shadow 的 entry_gate_state、frozen environment）
DECLARED        调用方自述，无 owner provenance（Shadow 的 risk_policy_identity）
DERIVED         comparison 层由上列事实计算得到（delta / ratio / coverage）
UNAVAILABLE     证据缺失
```

关键边界（沿用 R32-C 的结论）：`shadow_run` 的 entry policy 已是 **owner-issued frozen input**（`challenger_runtime_inputs.entry_policy`）；而 `candidate.risk_policy_identity` 仍只是 **DECLARED / CAPTURED_INPUT**。R32-D **不得**把它标成 `OWNER_VERIFIED`，也不因为 provenance 不够去查询 current Risk Policy。当前没有 Risk Authority 的 owner-issued immutable identity，因此保持 DECLARED。

同样地，Active 侧**没有** order-linked Risk Authority 证据（见 3.5.3），所以 `active.risk_rejection` 记 `UNAVAILABLE`；`active.order_lifecycle_columns` / `active.admission_decision` / `active.execution_evidence` / `active.runtime_context` 才是 `OWNER_ISSUED`。报告全文不出现 `OWNER_VERIFIED`。

### 3.8 Coverage（一级业务事实）

```text
expected_observations              # 由 spec 显式给出，永远是分母
available_observations             # 两腿证据都在，且所有 required 维度 owner-evidence 完整
partial_observations               # 两腿都有证据但 required 不完整，或只有一腿有证据
aligned_incomplete_observations    # partial 中"对齐但 required 未完整"的那部分
missing_observations               # 该 key 两腿都没有明确证据
unavailable_observations           # 报告级失败（environment/identity）时全部记此桶
coverage_ratio                     # available / expected
blocking_reasons
```

`available = 0` 时 ratio 为 `0.0`，绝不因为过滤掉缺失项而显示 `1.00`；`CoverageEvidence` 构造期就拒绝 ratio 与 `available/expected` 不一致的取值。

### 3.9 Availability

```text
UNAVAILABLE  environment / identity / 基本 provenance 无法建立（含 environment_mismatch、Active runtime context 不可用）
PARTIAL      environment 可比，但 coverage 不完整或某个 required 维度不是 owner-evidence 完整
AVAILABLE    所有 required 维度在全部 expected observation 上 owner-evidence 完整，且 coverage = 1
```

required 维度 = signal / decision / execution / risk_rejection；best-effort 维度（normalized turnover、performance）不阻止 `AVAILABLE`。§7 的原则是**不为了让测试显示 AVAILABLE 而降低 required 定义**：由于 Active 侧今天没有 order-linked risk rejection 证据，`risk_rejection` 永远不可能 `AVAILABLE`，**因此当前生产 comparison 的最高可用性是 `PARTIAL`，`AVAILABLE` 在补齐 Active owner 证据之前不可达**。这是显式记录的边界，不是被放宽的判据；R32-E 要么拿到 owner-issued Active risk 证据，要么**显式**决定重划 required 集合（那是一次显式决策，而不是静默降级）。

### 3.10 ShadowComparisonReport

```text
schema_version
report_id                      # == report_fingerprint
report_fingerprint
comparison_spec                # spec 投影（含 comparison_scope_identity 与 pinned active_evidence_id）
active_evidence                # 完整 canonical Active envelope：source table / order IDs /
                               # source schema version / source fingerprint / 本次消费的 exact 投影
active_order_lifecycle         # order lifecycle 事实（显式标注不作为 admission / execution / risk 证据）
shadow_run_id
shadow_run_fingerprint
environment_identity           # 七维投影 + 两侧共享环境 fingerprint + equality + 关系字段
active_strategy_stamp / challenger_strategy_stamp
availability
coverage
observations                   # 每个 expected key 的 signal/decision/execution/risk，含每腿 evidence state
turnover
execution
risk_rejection
performance
provenance
blocking_reasons
```

`active_evidence` 保存完整的消费投影，因此 report 自身就能重算 `source_fingerprint` 并证明它引用的 Active 证据（测试 D1b 就是这么做的）。

**没有** winner / loser / promote / overall score / ranking / better_strategy 字段。报告只给事实与 delta；晋级属于 R31/R32-E policy wiring。R32-D 不新增 AI 权限，也不让 AI 写报告事实、改 coverage 或改 lifecycle。

### 3.11 report fingerprint 与持久化

- `report_fingerprint = shadow_runtime.fingerprint(material)`：canonical JSON（`sort_keys`、`separators`、`ensure_ascii=False`、`allow_nan=False`）+ SHA-256。相同 exact inputs → 相同 fingerprint；不依赖 dict 插入顺序、DB row order、机器时间、UUID、`hash()` 或 `repr()`。
- 持久化只有一张 append-only 表 `shadow_comparison_reports`（migration v25，DDL 单一 owner `paper_schema_migrations.ensure_shadow_comparison_reports`，并挂到 `init_db` 的两条路径）。不建 `comparison_signal` / `comparison_execution` / `comparison_risk` / `comparison_performance` 这类平行 authority 表；详细内容放 canonical evidence envelope。
- 幂等：`report_id == report_fingerprint`，`INSERT OR IGNORE` 后必须核对已存在行内容**逐字节相同**，hash conflict / content conflict 一律显式报错，不静默接受。
- DB 写权限：只写 `shadow_comparison_reports`。`paper_accounts` / `paper_positions` / `paper_position_lots` / `paper_orders` / `paper_fills` / `paper_signals` / `paper_capital_reservations` / `paper_risk_decisions` / lifecycle / `shadow_runs` 全部不得被 comparison 修改。

### 3.12 失败语义

| 情形 | 结果 |
| --- | --- |
| spec 与 ShadowRun 的 stamp / session / decision / environment 不一致 | `UNAVAILABLE` + 稳定 reason |
| spec 未声明合法 `active_evidence_id` | 构造期拒绝（`ComparisonSpec` 不接受 `None`/非法 SHA-256） |
| 按显式 order IDs 加载后算出的 fingerprint 与声明的 `active_evidence_id` 不一致（行被原地更新过） | service 抛 `active_evidence_fingerprint_mismatch`，**不生成任何 report** |
| Active order_ids 与加载到的 evidence 集合不一致 | fail closed（构造期拒绝） |
| Active runtime context 不可用 | `UNAVAILABLE` + `active_runtime_context_unavailable:<reason>` |
| 七维共享环境不一致 | `UNAVAILABLE` + `environment_mismatch` |
| 同一 scope 内 `(symbol, side)` 重复 | fail closed |
| 某 key 只有一腿有明确评估证据 | 该 key PARTIAL，另一腿 `MISSING`（不是 false/reject/zero） |
| 某 required 维度缺 owner 证据（含 order-linked Active risk 证据不存在） | `PARTIAL`，不升级成 `AVAILABLE` |

## 四、R32-E 边界

R32-D 只产出 comparison evidence。把 report 接到生命周期、晋级、运营界面与生产调用者属 R32-E；R32-E 在把这些事实写进 promotion evidence 前，必须先处理两处 provenance 缺口：Active 侧没有 owner-issued entry evidence，以及 Shadow 侧 `risk_policy_identity` 仍只是 DECLARED。DEPLOY = NOT DEPLOYED。

## 五、验证结果

复现命令（workspace 根目录）：

```text
focused:   cd backend && python -m unittest test_shadow_comparison test_shadow_runtime \
             test_execution_planner test_paper_decision_audit test_simulation_runtime_context -q
mutation:  python work/r32d_shadow_comparison_mutation_check.py
full:      python -m unittest discover -s backend -p "test_*.py"
docker:    docker run --rm --network none --tmpfs /app/data_cache:rw,size=64m,uid=10001,gid=10001 \
             astock-codex:ci python -m unittest discover -s backend -p "test_*.py"
```

| 层 | 结果 |
| --- | --- |
| focused R32-C/D + Active audit | 171 tests OK |
| ruff / compileall | PASS |
| 语义 mutation M-D1–M-D16 | **16/16 DETECTED**；survived / fake / timeout = 0；restore SHA256 PASS；恢复后基线 GREEN |
| backend 完整套件 | 5093 tests，OK (skipped=5) |
| Docker `--network none` | 5093 tests，OK (skipped=30) |
| frontend | build + 150 unit tests 通过；`frontend/dist` 无漂移 |
| Chromium E2E | 34 passed（CI 形态 `workers=1`） |
| Security Leak Scan | 0 values（2 项截图属既有人工复核条目） |

回归用例：D1 确定性、D1b fingerprint 绑定 exact 证据身份并可自验、D2 七维环境逐维 fail closed、D3/D15 Active runtime context / execution envelope 缺失不重建、D4 缺失即 MISSING、D5 coverage 分母、D6 历史重放、D7 纯层无 DB/时钟/latest 入口、D7b 显式 ID 缺失 fail closed、D8 provenance、D9/D9b 估值只取 frozen environment、D10 幂等与 append-only、D11 正式账本零写入、D12 无 winner/score、D13 order id 行漂移 fail closed、D14 candidate 无 execution 不算 execution evidence、D15b owner-issued 0 vs 缺失 None、D16 无 entry 的 Challenger 不算 admission/risk 证据、D17 order status 不是 Risk Authority 证据、D18 缺失永不变成 0/false、**D19 owner 原始分数走 canonical `admission_score` 键并贯穿持久化**、**D20 capture owner 只依赖显式 order IDs（旧 spec 驱动签名已删除）**、**D20b §二.6 正式调用顺序端到端**。

## 六、架构报告

- 新增业务 authority：`shadow_comparison_reports`（比对事实的唯一追加式证据 owner）+ 比对报告契约与派生 delta。移除：无。
- 复制实现移除：**NO**——没有可删除的旧 comparison 原型；`tradability_shadow.ShadowComparison`（production verdict vs tradability archive）与 `strategy_champion.compare_for_promotion`（晋级路径）都不是 R32-D 的前身。
- 新增 facade/wrapper：0（一个 application service 边界 + 一个 persistence owner + 一个纯 domain 模块）。移除旧 facade/wrapper：0。
- 新增隐式 current-state 查询：0；`latest` / `current` / `head` / provider / archive 查询为 0（结构证明 + 运行证明）。
- 直接写正式账本的调用点：before 0 / after 0。比对证据追加点：before 0 / after 1（`shadow_comparison_repository.append_report`）。报告 DDL 调用点：1 个模块（`paper_schema_migrations`），migration v25 + 2 个 `init_db` 调用点。
- 新增大型 if/elif 决策链：NO。
- 前端复制业务规则：NO。移除兼容路径：NO。
- 理解本次核心业务规则需要查看的模块：before 11 / after 14（`shadow_comparison`、`shadow_comparison_service`、`shadow_comparison_repository`）。
- `paper_trading.py`：15,851 LOC / 325 defs → 15,855 LOC / 325 defs（仅 R32-D 首轮的 `init_db` ensure 调用；本轮未改动）。
- fingerprint 实现仍只有一份：`shadow_runtime.canonical_json` / `fingerprint`；`shadow_comparison` 不 import `hashlib`。
- 本轮的取舍：report 现在持久化**完整 canonical Active envelope**（source table / order IDs / schema version / source fingerprint / 消费的 exact 投影），因此 report 自身可重算 source fingerprint；代价是 evidence 更大，这是"可审计 Active envelope"的显式成本。服务因此按显式 order id 读取 `risk_payload`（该列为精确 linkage 的 admission 证据），不再因为"可能很大"而跳过它。

### 6.1 architecture convergence（本轮）

```text
Old production API removed:      shadow_comparison_service.load_active_comparison_evidence(conn, spec)
Old callers migrated:            5（1 个生产调用点 + 4 个测试调用点）
Old caller count after migration: 0（全仓库 0 处引用）
Compatibility path remaining:    0（未包 compatibility wrapper；placeholder-spec 引导路径已删除）
Duplicate capture implementation: 0（SQL / 投影 / fingerprint 只有 capture_active_comparison_evidence 一处）
Net production LOC:              +32 / -11（shadow_comparison.py +1/-1，shadow_comparison_service.py +31/-10）
```

未新增 facade / manager / helper 模块；改动集中在既有 `shadow_comparison_service.py`（capture owner）与 `shadow_comparison.py` 的一处 owner-key 读取修正。新增 mutation：`M-D14`（`admission_score` 退回旧键）、`M-D15`（capture 的 fingerprint 不再由 exact 行内容决定）、`M-D16`（capture 重新接受 `ComparisonSpec` 参数）。
