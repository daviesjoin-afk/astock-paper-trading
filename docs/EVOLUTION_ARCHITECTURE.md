# Evolution Architecture（自进化架构）

本文描述**当前**自进化 / Champion-Challenger 链路：证据如何变成提案、提案如何被验证、谁有权写入生产参数，以及为什么 AI 不能自己放宽风险。

策略模型本身（身份、版本、生命周期、周期快照）见 [`STRATEGY_PLATFORM.md`](STRATEGY_PLATFORM.md)；分层与调用边界见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)。

## 1. 总览

```text
Evidence（账本 / 影子观测 / 研究样本）
        ↓
Proposal（候选参数、分配覆盖、选股覆盖）
        ↓
Validation（A/B 对照 + 观察期门禁）
        ↓
Asymmetric Risk Gate（唯一风险放大门）
        ↓
Shadow Challenger（隔离影子账本）
        ↓
Promotion（Champion/Challenger 晋升，唯一头部切换）
        ↓
Runtime / Version Application（写新版本，不改写历史）
```

**一句话**：自进化可以**收紧**得很快，**放大**必须走完证据 + 观察期 + 幅度上限 + Challenger 胜出 + 人工确认。

## 2. Evidence（证据）

| 来源 | 内容 | 边界 |
| --- | --- | --- |
| 纸盘账本 | 订单、成交、持仓、NAV、风险决策、审计事件 | 只读端口读取（`paper_ledger_reader` 使用 `mode=ro` + `query_only`） |
| 影子观测 | `adaptive_shadow_risk`、`neural_shadow`、研究台账 | 不写正式账本、不提交订单 |
| 新闻/公告证据 | `news_learning` | 影子证据流 |
| 生产路径回放 | `paper_replay_regression`、golden replay | 确定性、可逐字节比对 |

证据缺失或不新鲜时 **fail-closed**：不产生提案，或只允许收紧方向。

## 3. Proposal（提案）

- **参数提案**：`self_evolution` 维护参数版本与步长/阈值边界；`dual_ai_tuner` 产出共识候选。
- **分配提案**：Bandit 权重 → 共享资金分摊覆盖（`evolution_apply.apply_allocation`）。
- **选股覆盖提案**：因子权重/入场阈值覆盖（`evolution_apply.apply_tuner_proposals`）。
- **风险放大提案**：必须先登记到 `risk_expansion_proposals`（`asymmetric_risk`），登记时刻起算观察期；**绝不信任调用方自报的观察天数**。
- **Challenger 提案**：`strategy_champion.open_challenger` 创建不可变候选参数 + 隔离影子账本。

提案只是数据：不落生产参数、不进正式账本。

## 4. Validation（验证）

`evolution_validation`：

1. `record_ab_snapshots`（收盘学习调用）：对每个已部署的进化版本，比较部署后观察窗口与部署前**等长**基线窗口的账户净值表现，写入 `adaptive_ab_tests`；观察不满 `MIN_OBSERVATION_DAYS`（5 个净值日）只记 `verdict='observing'`，**不下结论**。
2. `pre_apply_gate`：同一账户同一通道上一次覆盖仍在观察期内（<5 个净值日）时拒绝新覆盖，避免连续覆盖导致效果无法归因（既有风控/选股通道另有自己的分层影子门禁：1 日观测 / 3 日小步 / 5 日标准 / 10 日成熟）。

不使用未来数据；不对短窗口假装有统计意义。

## 5. Asymmetric Risk Gate（非对称风险门）

`asymmetric_risk.evaluate_risk_adjustments` 是**唯一风险放大门**，所有风险方向统一建模为 `higher_is_riskier` / `lower_is_riskier` / `neutral`：

| 方向 | 门槛 |
| --- | --- |
| **收紧**（降敞口/降权重/减席位/止损更紧/缩短持有） | 安全方向 → 低证据门槛即可生效 |
| **放大**（升敞口/升权重/加席位/放宽止损/延长持有） | 四重门槛缺一不可：① ≥ `EXPANSION_MIN_SAMPLES` 的严格证据；② 观察期已满（提案先登记）；③ 单轮幅度 ≤ `MAX_SINGLE_ROUND_STEP`；④ `challenger_win=True`（Champion/Challenger 晋升路径） |

证据缺失 → 只允许收紧。

**四条写入路径全部收敛到同一道门**（PR-33）：AI 自进化调参、设置页（UI）、Champion 晋升、手动 API（策略定义 PATCH）。没有任何一条能绕开它。

## 6. Shadow Challenger（影子挑战者）

`strategy_champion`（`strategy-champion-v3`）：

- **Champion 独占正式纸盘账户**；开 Challenger 只创建不可变候选参数与**隔离影子账本**，绝不写正式参数头。
- 影子运行支持 counterfactual 重放（`run_shadow_context` / `run_shadow_counterfactual`），产出与 Champion 同口径的指标。
- `compare_for_promotion` 在 `PROMOTION_TOLERANCE` 内比较，产出可审计的晋升判定。

## 7. Promotion（晋升）

- **晋升是唯一头部切换**（`promote_challenger`）。影子胜出是不够的：必须同时满足非对称风险门（放大方向）与人工确认。
- 失败或回退走 `rollback_challenger`，状态标 `rolled_back`；影子账本保留，可用于复盘。
- 晋升之后参数**以新版本落地**，历史版本与旧 checksum 不动。

## 8. Runtime / Version application（谁有权写生产参数）

**唯一入口**：`strategy_runtime.apply_parameter_adjustments`

- 只接受参数值调整，**不接受替换 AST**：可变字节仅限已固定定义中声明过的 `parameter.value`。
- 前置条件：策略生命周期允许进化（`evolution_control.enabled`）、存在可执行 DSL 参数 Schema。
- 必经 `asymmetric_risk.evaluate_risk_adjustments`；AI/自进化路径默认 `challenger_win=False`，因此**只能收紧**。
- 实际写入通过 `strategy_registry.save_definition(..., expected_version=...)` 追加新版本 + 新 checksum；`locked` 参数任何路径都不能改；`min_evidence` 不足直接拒绝。
- 写入后 `clear_cache()`，下一次读取拿到新上下文；放大的提案被标记 `promoted`，闭环收口。

其它通道的能力边界：

| 通道 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| `evolution_apply`（A 批落地） | 需 `confirmed=True` 的人工确认后覆盖分配/选股参数，并保存前值以支持精确回滚 | 不能跳过 `pre_apply_gate`、不能自动应用、不能改风险硬规则 |
| `evolution_loop`（代闭环） | OBSERVE → EVALUATE → MUTATE → VALIDATE → APPLY，带检查点续跑与 `time_budget_seconds` | APPLY 仍走同一门；缺数据进 `skipped` 而不是伪造结论 |
| 设置页 / 手动 API | 收紧策略风险参数；PATCH 策略定义 | 请求体里的 `risk_evidence` / `challenger_win` 在 API 模型层就被丢弃，固定 fail-closed 传入 |
| Adaptive/AI 模块 | 产出影子证据与候选 | **不得**直接导入纸盘订单 API、不得自动放宽风控（由 `backend/test_adaptive_dependency_boundary.py` 以 AST 守护） |

## 9. 不变量

1. AI/自进化**不能**独立绕过晋升与风险门：放大必须有观察期 + Challenger 胜出 + 人工确认。
2. 自动应用（auto-apply）恒为关闭；`AI 自动应用候选` 在设置中心是只读的。
3. 影子层永不写正式账本、永不提交订单。
4. 每次生效都留审计（提案、门禁判定、前值、新版本、checksum），可回滚、可回放。
5. 观察期与显著性不得被调用方参数覆盖（不信任自报天数）。

## 10. 代码与测试索引

| 主题 | 代码 | 回归 |
| --- | --- | --- |
| 闭环与代 | `backend/evolution_loop.py`、`backend/evolution_loop_runner.py` | `backend/test_evolution_loop.py` |
| 落地通道 | `backend/evolution_apply.py` | `backend/test_evolution_apply.py` |
| A/B 验证 | `backend/evolution_validation.py` | `backend/test_evolution_validation.py` |
| 非对称风险门 | `backend/asymmetric_risk.py` | `backend/test_asymmetric_risk.py`、`backend/test_asymmetric_risk_gate_wiring.py` |
| Champion/Challenger | `backend/strategy_champion.py` | `backend/test_strategy_champion.py` |
| 参数 Schema 与写入 | `backend/strategy_parameter_schema.py`、`backend/strategy_runtime.py` | `backend/test_strategy_parameter_schema.py` |
| 依赖边界守护 | — | `backend/test_adaptive_dependency_boundary.py` |
| 生产路径端到端 | — | `backend/test_production_path_golden_replay.py` |
