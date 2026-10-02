# R33 Strategy Health / Retirement

R33 的目标是把「什么算可证明的健康事实」先钉死，再谈自动降级/退休。分三个可独立审核的 PR：

```text
R33-A  Exact Strategy Health Evidence        ← COMPLETE（PR #222，merge 3b9082d）
R33-B  Retirement Policy                     ← IN REVIEW（详见 docs/R33_RETIREMENT_POLICY.md）
R33-C  Health Monitor / Workspace / Closure   NOT STARTED
```

R33-A **只建立事实层**：immutable / exact-version / deterministic / auditable / replayable 的
`StrategyHealthSnapshot`。它不回答「该不该退休」，也**不执行任何 lifecycle 修改**。

R33-B **只建立 policy**：把快照解释成 `StrategyRetirementDecision`（动作候选或证据不足），
并最多产出一个**未执行**的 `LifecycleTransitionProposal`。它同样不写 lifecycle。

```text
Health Snapshot → Retirement Policy → Transition Proposal → [STOP]
```

自动退休 / 归档 / 删除 / 停交易：**R33-A 与 R33-B 都不做**。

## 0. 基线

```text
base = b3c1218cdce226a610a7794787bba91c186c9e8d   (R32-FINAL merge commit)
branch = codex/r33a-strategy-health-evidence
```

## 1. 现有 authority（R33 不改）

```text
strategy_registry   = strategy identity / immutable version owner
strategy_lifecycle  = lifecycle state + transition owner
strategy_promotion  = lifecycle promotion evidence owner
strategy_champion   = parameter-head activation owner
```

R33 不得创建第二套 Lifecycle State Machine / Promotion Policy / Champion Policy。
`StrategyHealthSnapshot` 是**新的事实 authority**，不是新的 lifecycle authority。

## 2. 事实 inventory（每个候选：owner / identity / 可读历史 / provenance / 可变性 / 可用于 R33 / 是否已有阈值）

### 2.1 Runtime integrity

| 项 | 事实 |
| --- | --- |
| owner | `backend/strategy_registry.py`（`runtime_readiness`，`:586-631`） |
| 事实 | `dsl_compiled` / `fingerprint_valid` / `risk_profile_compiled` / `execution_profile_compiled` / `runtime_ready` |
| identity | 当前实现**只绑 head**（`:596`），返回体带 `version`/`checksum` |
| 纯函数? | 是：合成逻辑只是 `(spec.origin, definition.dsl_ast, definition.metadata)` 的纯编译（`DSL.normalize` → `compile_strategy_risk_fingerprint` → `compile_strategy_risk_profile` → `execution_profile_for`） |
| 可读历史 | 不持久化，任何时刻重算；**对 immutable version 的 definition 重算是稳定的** |
| provenance | DERIVED（owner 编译原语 + owner 持久化的 exact definition） |
| 可变性 | 输入（version definition）不可变；结论不落库 |
| 可用于 R33 | 是 —— 但必须显式绑 exact version；**head ready 不能证明历史 version ready** |
| 已有阈值 | 无（只有 `interval_hours=24`、默认 `max_positions` 等非健康阈值，`:32,:193-198`） |

### 2.2 Lifecycle integrity

| 项 | 事实 |
| --- | --- |
| owner | `backend/strategy_lifecycle.py` |
| 事实 | exact state（`get_state(id, version, checksum)` `:289-300`）、有序不可变事件（`strategy_lifecycle_events` `:91-118`，含 `event_fingerprint`/`from_state`/`to_state`/`transition_kind`/`promotion_decision_fingerprint`/actor）、`allows_formal_cycle` `:68` |
| identity | `(strategy_id, strategy_version, strategy_checksum)` 精确 |
| 可读历史 | 事件账本可读且不可变；**没有 as-of 快照 API**（不给「某时刻的 state」） |
| provenance | OWNER_ISSUED |
| 可变性 | state 行 CAS 可变；事件 append-only |
| 可用于 R33 | 是 —— 只报告事实，**不做**「state=degraded ⇒ health BAD」这类政策判断（留 R33-B） |
| 已有阈值 | 无 |

### 2.3 Execution evidence health

| 项 | 事实 |
| --- | --- |
| owner | `execution_evidence.py` / `execution_lifecycle.py` / `execution_verification.py` |
| 词表 | 证据三态 `known/unknown/not_applicable`（`execution_evidence.py:109-112`）；fill verdict 六态（`:157-170`）；闸门四态 `verified/partial/unknown/not_executed`（`execution_verification.py:73-82`）；证据来源 `paper_orders+paper_fills` / `legacy_row_without_fill_evidence` / `no_evidence_available` / `evidence_inconsistent`（`:85-94`） |
| 唯一谓词 | `execution_verification.VERIFIED_PREDICATE`（`:118-120`，要求 `execution_verified=1 AND execution_status='verified'`），凡 SQL 必须引用它 |
| identity | **读路径目前不按 strategy_version 过滤**；但 `paper_orders` 自带 `strategy_id/strategy_version/strategy_checksum`（`paper_trading.py:1922`），fills 经 `order_id → paper_orders` 可达 |
| 明确窗口 | `paper_orders.created_at` / `paper_fills.fill_date`（唯一带窗口的 owner 查询是 `has_verified_positive_execution`） |
| 可读历史 | 是（`paper_fills` append-only；`paper_orders` 的 status/qty **会被原地改写**，因此只读 owner 已落库的结论列，不重新解释 status） |
| provenance | OWNER_ISSUED（`execution_status`/`execution_verified`/`execution_evidence_source` 是 owner 盖章结论） |
| 可用于 R33 | 是 —— 未知必须保持未知 |
| 已有阈值 | 只有手续费对账容差 `0.01`（`execution_evidence.py:195`）；**fill ratio / TTL 阈值不存在** |

### 2.4 Risk evidence health

| 项 | 事实 |
| --- | --- |
| owner | `paper_risk_service.py`（写 `authority="RISK"` 的行）、`paper_risk_decision.py`（纯判定引擎） |
| 表 | `paper_risk_decisions`（含 `strategy_id/strategy_version/strategy_checksum` + `order_id`） |
| authority 词表 | `paper_schema_migrations.RISK_DECISION_AUTHORITIES = ("EXECUTION","RISK","ENTRY","ALLOCATION","TIMING","INTRADAY","AUDIT")`（`:1231-1233`），行在 `payload.decision_provenance` 里**自己声明** |
| identity | 行带 exact version 三列；但**没有** version+window 的 owner getter |
| provenance | OWNER_ISSUED 仅当 `authority == "RISK"`（R32-E1 契约）；其余 authority 不得计成风险事件 |
| 可变性 | append-only |
| 可用于 R33 | 是 —— 必须区分 RISK / ENTRY / EXECUTION / ALLOCATION / TIMING；**missing risk evidence ≠ 没有风险事件** |
| 已有阈值 | 无（阈值在 `strategy_champion.PROMOTION_TOLERANCE` 与 `promotion_science` 里，属参数头/科学门禁，不属风险层） |

### 2.5 Performance evidence

| 项 | 事实 |
| --- | --- |
| `paper_performance.py` | 日内、按 position/code、需当日行情（`:40-61`）；不是策略级历史绩效 |
| `paper_portfolio.py` | 纯聚合（account_id, code）成本；无分母 |
| `paper_portfolio_read_model.py` | `(cycle_id, account_id, asof_day)` 有界；own cash/realized_pnl/cost，**明确不发布** nav/market_value/unrealized_pnl/daily_return（`:27-48`, `:1355-1364`） |
| **策略级历史绩效 owner** | **不存在**（NAV/return/drawdown/PnL 无 exact strategy version 作用域的 owner） |
| 结论 | R33-A 的 performance 维度**恒为 UNAVAILABLE**（reason `strategy_performance_owner_unavailable`），并登记为 R33-B 的前置缺口。**不得**把日内 PnL 升级成策略历史绩效权威 |

### 2.6 Activity / observation coverage

| 项 | 事实 |
| --- | --- |
| cycle pin | `paper_cycle_strategy_versions(cycle_id, account_id, strategy_id, strategy_version, strategy_checksum, bound_at)`（`strategy_registry.py:262-272`，insert-once）→ 「该 cycle 由哪个 exact version 运行」是 owner 事实 |
| 订单/成交 | `paper_orders` 带 stamp；`paper_fills` 经 order_id |
| signals | `paper_signals` **没有** strategy stamp（只有 `cycle_id`），而 cycle 的 pin 是 `(cycle_id, account_id)` → **signal 无法归属到 exact version**，因此 activity 里的 signals 只能是 `UNAVAILABLE`（reason `signal_strategy_attribution_unavailable`），不得编造 |
| 语义 | `0 trades` 不等于 unhealthy：合法无信号是合法事实 |
| 已有阈值 | 无 |

### 2.7 Comparable / shadow evidence

| 项 | 事实 |
| --- | --- |
| owner | `shadow_comparison_repository.get_report(conn, report_id)`（`:68`，唯一 exact getter） |
| identity | `report_id == report_fingerprint`；含 `shadow_run_id/fingerprint`、`environment_identity`、两个 strategy stamp |
| latest 查找 | **不存在**（repository docstring 明示） |
| 可用于 R33 | 是 —— 但只接受**显式给出的 report id**；没给就是 `NOT_APPLICABLE`，绝不搜索最新报告 |

### 2.8 已存在的阈值（R33-A 只登记，不新增）

| 阈值 | 位置 | owner |
| --- | --- | --- |
| `max_drawdown_pct 0.5` / `turnover_amount_ratio 0.15` / `execution_fill_rate 5.0` / `concentration_hhi 0.05` | `strategy_champion.PROMOTION_TOLERANCE:33-38` | strategy_champion |
| `MIN_PAIRED_SNAPSHOTS=12` / `MIN_PAIRED_INTERVALS=10` / `MIN_DISTINCT_DAYS=5` / `HOLDOUT_FRACTION=0.40` / `MIN_HOLDOUT_INTERVALS=4` / `CONFIDENCE_Z=1.96` / `MIN_HOLDOUT_POSITIVE_RATIO=0.55` | `promotion_science.py:24-30` | promotion_science |
| 手续费对账容差 `0.01` | `execution_evidence.py:195` | execution_evidence |

**POLICY DECISION REQUIRED FOR R33-B**：连续亏损 N 天、收益低于 X%、回撤超过 Y%、胜率低于 Z%、
交易数少于 N、Sharpe 阈值 —— 仓库里**都不存在**，R33-A 不猜、不新增。

## 3. Health Snapshot identity

```text
snapshot_id == snapshot_fingerprint     （单一身份，与 R32 的 report_id == report_fingerprint 同构）

fingerprint 覆盖：
  health_contract_version
  strategy_id / strategy_version / strategy_checksum
  observation window（window_identity = 起止的确定性指纹）
  source_identities + source_fingerprints
  dimension（status / facts / provenance / blocking_reasons）
  coverage（expected/available/partial/unavailable/not_applicable/ratio）
不覆盖：created_at（持久化元数据，绝不进确定性指纹）
```

窗口必须**显式**：不存在 `strategy_id + latest`、不存在「当前日」隐式查询。

## 4. Dimension 通用 contract

每个维度：`status ∈ {AVAILABLE, PARTIAL, UNAVAILABLE, NOT_APPLICABLE}`、`facts`、
`provenance ∈ {OWNER_ISSUED, CAPTURED_INPUT, DERIVED, UNAVAILABLE}`、`source_identity`、
`source_fingerprint`、`blocking_reasons`。**不接受** `healthy=true/false` 这种单一 bool。

## 5. Coverage 语义

`coverage_ratio` **只是证据覆盖率**：`coverage_ratio = 1` 不等于 HEALTHY，
`coverage_ratio < 1` 不等于要 RETIRE。R33-A 不做任何由 coverage 到结论的映射。

## 6. Architecture graph（R33-A 到此为止）

```text
Exact Strategy Version
        ↓
Existing Owner Evidence（registry runtime / lifecycle / execution / risk / comparison）
        ↓
StrategyHealthSnapshot（append-only evidence）
        ↓
[STOP —— R33-A 到此结束]
```

## 7. R33-B 预留契约（**本 PR 不实现**）

未来 Retirement Policy 的输入只有：

```text
explicit health_snapshot_id
```

它**不得**自己重查 raw orders、自己重算 risk、自己找 latest snapshot。可能输出的动作词表
（`NO_ACTION` / `DEGRADE_CANDIDATE` / `RETIRE_CANDIDATE` / `ARCHIVE_READY`）**本 PR 不实现**。

R33-B 必须遵守的安全边界（提前登记，防止跑偏）：

- `UNKNOWN` / `PARTIAL` 证据**不能**自动 retire；
- 单次瞬时异常**不能**默认 retire；
- performance 事实指标**不等于** retirement decision；
- `quarantined` 留给 integrity/safety 类事实，**不是**「收益不好」的同义词；
- `archived` 不能因为 policy 想结束策略就无视既有 exposure / cycle ownership；
- AI 永远不能 apply retirement transition。

## 8. 本轮明确不做

自动 lifecycle transition、自动 retiring / archived、health score / rank / tier、AI 接入、
前端 Workspace、任何新阈值、任何 lifecycle 或正式账本写入。
