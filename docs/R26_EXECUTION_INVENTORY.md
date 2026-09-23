# R26 执行事实清单（Execution Inventory）

本文回答一个问题：**面对一张模拟订单，怎么沿着一条明确链路回答"为什么成交 / 为什么
没成交 / 为什么只成交一部分"。** 它描述的是**实际行为**，与 `ARCHITECTURE.md` 的
"Simulation Execution Fidelity（R26）"一节一致；两者任一改动必须同步修改。

基线：master `ee1b2aeb82c935965253b1c32dec923596459fc5`；
R26 分支 `codex/r26-simulation-execution-fidelity`。

---

## 1. 链路

```text
R24 verified market evidence
+ R25 approved signal / frozen provenance
        ↓
durable order intent        （paper_orders，caller 创建，只表达"想做什么"）
        ↓
Execution Simulation Authority
        ├── execution_planner.execution_context_from_facts   冻结事实
        ├── execution_planner.evaluate_simulated_execution   纯规则决策
        └── execution_planner.commit_fill                    唯一原子账务提交
        ↓
execution decision         （ExecutionDecision：可否成交/数量/价格/费用/reason/liquidity）
        ↓
zero / partial / full fill  （FillEvent，event_key 幂等）
        ↓
cash / lots / position / accounting
        ↓
read model / frontend       （只渲染后端事实，不重算规则）
```

关键不变式：**approved signal ≠ guaranteed fill**。已批准的信号只形成订单意图；能不能
成交由执行权威在拿到可信行情后决定。

---

## 2. 十个问题，各自的唯一 owner

| 问题 | 唯一 owner | 说明 |
| --- | --- | --- |
| 这是哪张 durable order？它来自哪个 frozen signal/cycle/strategy？ | `paper_orders` 行（`signal_id` / `cycle_id` / `strategy_id` / `strategy_version` / `strategy_checksum`） | 执行只读这些冻结字段，绝不用当前 strategy head 改写历史归属 |
| 本次使用哪份行情？`execution_asof` 是什么？行情是否可信？ | `market_data_contract`（`symbol_quote_snapshot` + `classify` + `EXECUTION_QUOTE_POLICY`） | 逐票报价 → snapshot 的映射只有一处；`freshness` / `verification` 由契约判定，execution 不转发经 signal 层 |
| 当时是不是交易时段？ | `execution_planner._session_phase`（`Asia/Shanghai`，显式 `ZoneInfo`） | 09:30–11:30 / 13:00–14:57 为连续竞价；11:30–13:00 午休；14:57–15:00 集合竞价（本轮不撮合 ⇒ 不成交） |
| 证券当时可交易吗？是否锁死涨跌停？ | `tradability_archive`（PIT 证据）+ evaluator 的稳定 reason | 停牌 → `SUSPENDED`；方向性封板 → `PRICE_LIMIT_LOCKED`；证据不足 → `TRADABILITY_UNKNOWN`（fail closed） |
| 卖出是否满足 T+1？可卖数量为什么是这些？ | `paper_position_lots.available_date` + 订单 `cycle_id` | `sellable_quantity` 由订单所属周期 + `available_date <= asof` 聚合；`_consume_available_lots` 在提交时二次防线 |
| 当时模拟流动性允许多少？之前已消耗多少累计参与量？ | `execution_planner.consumed_session_quantity` + `available_liquidity` | 容量 = 累计成交额/价格 × 参与率 − 本 session 截至 `execution_asof` 已消耗（`quote_at` 缺失的 legacy 流水按已消耗处理） |
| 成交价如何计算？滑点/费用多少？ | `estimated_fill_price` / `estimate_execution_fees`（费率常量来自 `paper_trading_rules`） | caller 提交的 `fill_price` / `fees` 一律被权威重算，不被相信 |
| 这个 FillEvent 是否已经执行过？order 总共成交多少、还剩多少？ | `_fill_event_key`（唯一键）+ `paper_fills` 聚合 + `filled_qty` / `remaining_qty` | event key 绑定 order × 行情观测 × ruleset，不含墙上时钟 |
| 风险动作是否已经真正执行过？ | `execution_verification.has_verified_positive_execution` | 用"正成交"（verified 或 partial）口径，而非整单 `status='filled'` |
| 历史/归档是否保存了全部 FillEvent？重启后结论一致吗？ | `paper_fills` + `paper_archives` 快照（按 order_id 聚成列表） | 归档保留每一笔；`stock_trade_history` 同时返回活动与归档 |

---

## 3. 状态与口径

### 订单存储状态 → 信号状态（唯一映射）

| `paper_orders.status` | `paper_signals.status` |
| --- | --- |
| `pending_limit` / `pending_execution` | `pending` |
| `partially_filled` | `partially_filled`（**不得降级成 `pending`**） |
| `deferred_capacity` | `deferred_capacity` |
| `execution_retry` / `manual_execution_retry` | `pending` |

生命周期解释由 `execution_lifecycle.canonical_state` 独占；各模块不得自行
`if status in {...}` 做生命周期判断。

### 两个必须分开的成交口径

| 问题 | 谓词 | 部分成交 |
| --- | --- | --- |
| 整单是否**完整**成交 | `EV.VERIFIED_PREDICATE` | 否 |
| 是否**已成交过一部分** | `EV.POSITIVE_EXECUTION_PREDICATE` | 是 |
| 该委托是否**可能携带流水** | `EV.FILL_CARRYING_PREDICATE` | 是（选取条件，与证据分开） |

### reason vocabulary（稳定，唯一命名）

`MARKET_UNAVAILABLE` / `MARKET_STALE` / `MARKET_UNVERIFIED` / `MARKET_DISAGREEMENT` /
`OUT_OF_SESSION` / `TRADABILITY_UNKNOWN` / `SUSPENDED` / `PRICE_LIMIT_LOCKED` /
`INVALID_QUANTITY` / `T1_NOT_SELLABLE` / `INSUFFICIENT_LIQUIDITY` / `INSUFFICIENT_CASH` /
`LIMIT_PRICE_NOT_REACHED` / `CANCELLED` / `ORDER_NOT_WORKING`

存储在 `paper_orders.execution_reasons`（逐笔在 `paper_fills.execution_evidence`）。

---

## 4. 权威与 writer 数量

| 能力 | owner | 数量 |
| --- | --- | --- |
| 执行决策 | `execution_planner.evaluate_simulated_execution`（纯函数） | 1 |
| `paper_fills` 生产 writer | `execution_planner.commit_fill` | 1（`demo_seed` 为非生产夹具） |
| 订单状态迁移 | `commit_fill` 内 CAS（`execution_version`）+ `execution_lifecycle` 解释 | 1 |
| T+1 | evaluator（`sellable_quantity`）+ `_consume_available_lots` 防线 | 1 + 1 防线 |
| 涨跌停 / 停牌 | evaluator（事实来自 `tradability_archive`） | 1 |
| 流动性参与率 | evaluator（`available_liquidity` − `consumed_session_quantity`） | 1 |
| 滑点 / 费用 | `estimated_fill_price` / `estimate_execution_fees`（常量在 `paper_trading_rules`） | 1 |
| 一次性风险动作去重 | `execution_verification.has_verified_positive_execution` | 1 |
| 成交事件身份 | `execution_planner._fill_event_key` | 1 |
| session 阶段 | `execution_planner._session_phase` | 1 |
| 逐笔血缘 | `paper_position_lots.source_fill_id`（`commit_fill` 内回填） | 1 |

`paper_orders` 仍有 7 个 INSERT 位置（意图创建 / 手动 / 风险退出 / 等待标记）——
这是**有意的**：它们表达不同的业务意图，不是一个 authority。统一的是**执行决策、
成交状态与 fill/账务提交**。

---

## 5. 数据契约（R26 新增字段）

| 表 | 字段 | 语义 | 历史数据 |
| --- | --- | --- | --- |
| `paper_orders` | `filled_qty` / `remaining_qty` | 累计已成交 / 剩余量；`qty` 永远是**原始委托量** | 由 `SUM(paper_fills.qty)` 按证据回填；无流水保持 0 |
| | `execution_asof` | 本次决策使用的执行时点 | 不回填 |
| | `execution_reasons` | 稳定 reason codes（JSON 数组） | 不回填 |
| | `execution_evidence` | 决策投影（含 market / tradability / liquidity） | 不回填 |
| | `pricing_basis` | 计价依据 | 不回填 |
| | `slippage` | 本次滑点 | 不回填 |
| | `ruleset_version` | 成交规则版本 | 不回填 |
| | `execution_version` | CAS 版本，防并发双成交 | 默认 0 |
| `paper_fills` | `event_key` | **幂等身份**（唯一索引，仅非空行） | NULL ⇒ 无身份，不参与去重 |
| | `execution_asof` / `pricing_basis` / `slippage` | 逐笔执行事实 | 不回填 |
| | `market_evidence` / `execution_evidence` | 逐笔市场与决策证据 | 不回填 |
| | `ruleset_version` | 逐笔规则版本 | 不回填 |
| `paper_position_lots` | `source_fill_id` | 该 lot 由哪一笔 FillEvent 出资 | **保持 NULL**（不可证明就不猜） |

---

## 6. 绝不发生的事

- approved signal 直接变成 full fill；
- 多个模块各自决定 T+1 / 涨跌停 / 成交数量 / 滑点 / 费用 / fill 状态；
- 部分成交被读成"什么都没发生"（→ 重复减仓）或"完整成交"（→ 夸大绩效）；
- 同一个累计成交量被重复消费参与额度；
- 归档把一笔订单的多笔成交压成一笔；
- 用当前策略 / 当前行情 / 当前周期去补历史事实；
- 为 legacy 数据猜造 `source_fill_id`、`filled_qty` 或任何执行证据；
- 前端重算成交规则；
- 执行依赖 signal 层获取行情事实。
