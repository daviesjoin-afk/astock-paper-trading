# R33-B Strategy Retirement Policy

R33-B 只产出 **policy recommendation**：解释 R33-A 已经落库的健康事实。
它**不执行任何 lifecycle mutation**，不自动退休/归档/删除/停交易。

```text
R33-A 负责：事实（StrategyHealthSnapshot）
R33-B 负责：解释事实（StrategyRetirementDecision）
R31   负责：lifecycle state transition（strategy_lifecycle.transition）
```

依赖方向（禁止反向）：

```text
strategy_health → retirement_policy → application service → repository
strategy_lifecycle 不 import retirement_policy；policy 不 import strategy_lifecycle
```

## 1. Inventory：现有 lifecycle 能力与 authority（§4）

### 1.1 真实状态词表（`backend/strategy_lifecycle.py:16-20`）

```text
draft · candidate · research · validated · shadow · paper · production_sim
· degraded · paused · retiring · archived · rejected · validation_failed · quarantined
```

工单 §4 示例里的 `ACTIVE` / `RETIRED` 在仓库中**不存在**：形式上的「活跃」是
`FORMAL_CYCLE_STATES = {paper, production_sim}`（`:39`），终态退休位是 `archived`。

### 1.2 关键边与集合

| 集合 | 值 | 位置 |
| --- | --- | --- |
| `SAFETY_TRANSITION_TARGETS` | `paused, quarantined, retiring, archived, rejected` | `:40-43` |
| `RESUME_TRANSITION_TARGETS` | `paper, production_sim`（仅从 `paused`） | `:43` |
| `degraded` 的入边 | 只来自 `paper` / `production_sim` | `:28-29` |
| `archived` 的入边 | 只来自 `retiring`（以及 safety 路径） | `:31` |

### 1.3 transition authority：谁可以写

唯一写入口 `strategy_lifecycle.transition`（`:323-442`，CAS + append-only 事件账本）。生产调用者恰好两个：

| 调用者 | kind | 约束 |
| --- | --- | --- |
| `strategy_promotion.apply_proposal:640` | `promotion` | 必须带 eligible 的 `PromotionDecision`（含 fingerprint），且 proposal 未 stale |
| `strategy_service.transition:709` | `safety` / `resume` | `actor_type` 必须 human；safety/resume 必须有 reason |

其余命中全在 `test_*.py`。**R33-B 不新增任何调用者。**

### 1.4 为什么 promotion / champion 不是 retirement policy

- `strategy_promotion.PROMOTION_RULES`（`:168-176`）只覆盖**向上**的边
  （draft→candidate→research→validated→shadow→paper→production_sim），**从不指向**
  `degraded` / `retiring` / `archived`；它的输入是「晋级证据是否完备」，与「是否该停」无关。
- `strategy_champion` 的操作对象是**正式参数头**（`self_evolution.activate_params_candidate`），
  根本不碰 lifecycle；它的指标容差（`PROMOTION_TOLERANCE`）是参数头晋级规则。

因此 R33-B 是一条**并列**的新 policy，不是对上述任一者的替代或包装。

## 2. Policy Contract（§5、§6、§9）

```text
StrategyRetirementDecision
  decision_id == decision_fingerprint      （与 R33-A / R32 同构的单一身份）
  snapshot_id / snapshot_fingerprint       ← 必须进指纹：同一 snapshot 必得同一 decision
  strategy_id / strategy_version / strategy_checksum
  decision ∈ {NO_ACTION, DEGRADE_CANDIDATE, RETIRE_CANDIDATE, ARCHIVE_READY,
              INSUFFICIENT_EVIDENCE}
  evidence_summary                         ← 只回放 snapshot 的事实摘要，不新增事实
  blocking_reasons[]                       ← 稳定、机器可读
  required_evidence[] / satisfied_evidence[]
  policy_version                           ← r33b.v1，进指纹
```

`created_at` **不进指纹**（与 R33-A 同口径）：它是持久化元数据，由 repository 在追加时落库。

决策词表固定为「动作候选」，**不新增** `BAD_STRATEGY` / `FAILED_STRATEGY` / `UNHEALTHY`
这类业务评价词；policy 也不产出 score / rank / tier / health_score。

## 3. v1 规则（§7、§8、§10）

```text
Rule 1（证据完整性闸门，永远最先）
  任一维度为 PARTIAL / UNAVAILABLE（且不是 NOT_APPLICABLE）
      → INSUFFICIENT_EVIDENCE（blocking reasons 逐维点名 dimension + status）

Rule 2
  证据完整（每维 AVAILABLE 或 NOT_APPLICABLE）且无 retirement signal
      → NO_ACTION

Rule 3（保留接口，本 PR 不实现阈值）
  evaluate_retirement_conditions(snapshot)  → 未来 policy version 在此产出阈值化条件
```

写死的安全语义（§8）：

- `UNKNOWN` / `PARTIAL` **永不**触发退休；
- `risk_evidence` UNAVAILABLE **既不能**当作「风险很好」，**也不能**当作「风险很差」；
- `performance` UNAVAILABLE **不能**当作收益 0；
- `activity` 0 trades **不是**自动退休信号（合法无信号是合法事实）；
- 缺证据 ⇒ `INSUFFICIENT_EVIDENCE`，**不是**失败。

### 3.1 v1 的现实后果（必须如实说明）

R33-A 已登记两个缺 owner 的事实：`performance` 恒为 `UNAVAILABLE`（仓库没有
exact-strategy-version 历史绩效 owner），`activity_coverage` 因 signals 无法归属而恒为 `PARTIAL`。
因此 **v1 对任何真实快照都只会产出 `INSUFFICIENT_EVIDENCE`**：

```text
NO_ACTION / DEGRADE_CANDIDATE / RETIRE_CANDIDATE / ARCHIVE_READY 在 v1 对真实数据不可达
```

这是刻意的安全结果（「不知道」不等于「失败」），并且有三个候选动作只在**未来 policy version**
拿到 owner 化证据后才可能产出。这也意味着：**当前仓库不可能推荐任何退休**，直到
§4 的缺口被 owner 化补齐（见 §5）。

## 4. Threshold inventory（§11）

未来可能需要，但**仓库里不存在**，因此本 PR 一个都不新增，全部标记 `POLICY DECISION REQUIRED`：

| 候选阈值 | 现状 |
| --- | --- |
| drawdown threshold | 不存在（`paper_portfolio_read_model` 明确不发布 nav/return） |
| loss duration（连续亏损天数） | 不存在 |
| execution failure rate | 不存在（只有手续费对账容差 `0.01`） |
| risk violation frequency | 不存在（`paper_risk_decisions` 只按 authority 分档） |
| strategy-version-scoped performance owner | **不存在** —— 这是 R33-B 无法产出候选动作的根因 |

已存在且属于**其它 owner** 的数值规则（不得搬来当退休阈值）：
`strategy_champion.PROMOTION_TOLERANCE`（参数头）、`promotion_science`（科学门禁）、
`execution_evidence` 手续费容差。

## 5. Persistence / Repository（§13、§14）

新表 `strategy_retirement_decisions`（migration **v28**，DDL 单一来源在
`paper_schema_migrations`，并接入 `init_db` 快路径）：append-only，字段
`decision_id · decision_fingerprint · snapshot_id · snapshot_fingerprint · strategy_id ·
strategy_version · decision_type · policy_version · evidence_json · created_at`。

repository 只有 `append_decision()` / `get_decision(decision_id)`：
禁止 `get_latest_decision()` / `get_current_decision()`，禁止 `current_retirement_state`，
禁止 overwrite（同 id 不同内容 ⇒ 冲突），repository 不做业务判断。

## 6. Lifecycle integration（§15）

本 PR **不调用** `strategy_lifecycle.transition`。允许生成 `LifecycleTransitionProposal`
（`proposal_id · decision_id · target_state · reason`），但：

```text
proposal != transition；不自动执行
```

target_state 只做固定映射（DEGRADE_CANDIDATE→degraded、RETIRE_CANDIDATE→retiring、
ARCHIVE_READY→archived），并由 **lifecycle owner 自己的 `TRANSITION_TABLE`** 校验该边在被捕获的
lifecycle state 下是否合法；不合法就不产出 proposal（fail closed），绝不改写 lifecycle。

## 7. API（§16）

```text
POST /api/strategies/{id}/retirement/evaluate      body: {snapshot_id}   → decision（+ proposal|None）
GET  /api/strategies/{id}/retirement/decisions/{decision_id}             → decision
```

必须显式 snapshot id：**禁止** latest health / current health；**禁止** `GET /retirement/status`
这类会诱导「当前状态」的端点。

## 8. 最终架构（R33-B 到此为止，§21）

```text
StrategyHealthSnapshot
        ↓
Retirement Policy（pure）
        ↓
StrategyRetirementDecision（append-only evidence）
        ↓
LifecycleTransitionProposal（仅结构，未执行）
        ↓
[STOP —— 本文只定义 R33-B policy；受控执行由 R33-C proposal/approval workflow 实现，并经 R31 transition authority]
```

AI 只能 summarize / explain；**永远不能**产出或 apply retirement transition。
