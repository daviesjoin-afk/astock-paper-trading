# R26 生产执行链路 Inventory（修改前）

基线：`ee1b2aeb82c935965253b1c32dec923596459fc5`（R25 #183 merge commit；R24 #182 已在父链）。

## 生产调用路径

| caller / path | 当前职责 / DB 表与写入点 | 规则与行情来源 | current / network / transaction | R26 owner 与动作 |
|---|---|---|---|---|
| `paper_trading.execute_open → _buy_order → manual_orders.commit_strategy_entry_fill → execution_planner.commit_fill` | 已批准 signal 复核后直接新建 `paper_orders(status='filled')`，再落 1 条 `paper_fills`、买入 lot、扣款和同步 `paper_positions` | `_buy_order` 计算数量、`price * (1 + SLIPPAGE)`、涨停封顶、commission；`quote_map` 来自 `_quotes`；R25 signal/strategy/cycle stamp 从 signal lineage 读取 | `execute_open` 在 `immediate` writer 前取完整市场、新闻和 quote。订单预写 `filled` 与真正落 fill 虽在同事务，但 `desired qty == filled qty` | 保留策略 approval 与 sizing；将可执行性、最终 fill qty/price/fee/as-of 移入 Execution Simulation Authority；订单先建 `working` |
| `manual_orders.submit_manual_order → _manual_order_plan → _execute_manual_plan → execution_planner.commit_fill` | `paper_orders` 手动委托 writer；market 单立即全额 fill；limit 单进入 pending；cancel 在 `manual_orders.cancel_manual_order` 更新订单并释放预占 | `_manual_order_plan` 复核报价、T+1、跌停、整手、price/slippage、commission/stamp；市场买入还检查账户/策略准入 | 初次 quote、`MDSvc.refresh_rows` 在 `immediate` writer 前获取；写事务内无 quote 参数时只读本地 cache 并 fail closed；执行时刻由 `_now()` 生成 | 业务策略准入留在 caller；迁移 execution facts、规则和 lifecycle 到唯一 authority |
| `manual_orders.process_pending_manual_orders` | pending manual order 的 expiry、revalidate、reservation、full fill；共用 `commit_fill` | 同一 `_manual_order_plan`，limit trigger 和价格/数量由 caller 重算 | quote、全市场刷新先于 `immediate`；当前 `asof_date` 只传日期，fill timestamp 可用当前 `_now()` | 改为冻结 `ExecutionContext`，只将不可变 decision 交给 writer |
| `paper_risk_service` risk sell pass | 写入 risk sell order；跌停时另写 `unfilled_limit_down`；正常卖出调用 `commit_fill` 并消费 lot、入账净款 | caller 计算 available qty、limit-down 百分比、`price * (1 - SLIPPAGE)`、commission + stamp duty | `quote` 由 risk service ports 提供；执行时可用 quote 时间或 `_now()` fallback | 仅保留风险策略决定“想卖”；交易资格与成交写入交给 execution authority |
| `paper_trading._intraday_sell / _commit_strategy_buy` 及波段加仓/回补 | 特殊策略直接计划 full qty；买入委托/成交最终通过 `manual_orders._commit_strategy_buy → execution_planner.commit_fill`；日内卖出另走直接调用 | 多处重复应用 slippage、fees、T+1/available qty；共享市场 quote | 依赖调用方传入的 quote 与当前策略上下文 | 逐个接到同一 authority；删除特例成交路径 |
| `execution_dispatch` 与 `entry_lifecycle` | 管理批量/人工核验等 hold 状态、signal retry 和订单终态；读写 `paper_orders` / `paper_signals`，不写 `paper_fills` | 窗口/TTL 仅是 dispatch eligibility，不是交易时段撮合 | 使用显式 `now` 可注入，但部分 API 默认 `_now()` | dispatch 继续只管是否进入执行队列；订单状态改变须委托统一 lifecycle owner |
| `paper_trading._record_entry_frozen_waitlist` | 写入不成交的 strategy waitlist marker order；带 cycle / strategy / signal lineage | 不读取/不产生 fill | 数据已在 caller | 保留为 intent/queue 创建，状态变更交 lifecycle owner |

## Writer 与规则 owner 统计（静态生产代码扫描）

- `paper_orders`：7 个生产 `INSERT` 语句位置：`paper_trading.py`（3，含 waitlist / strategy buy / sell）、`manual_orders.py`（2，manual immediate / limit）、`paper_risk_service.py`（2，unfilled / risk sell）。另有读模型、归档和非生产 `demo_seed.py`。这些是不同业务的委托意图创建点；R26 不把它们合并，统一的是执行评估、成交状态与 fill/账务提交。
- `paper_fills`：1 个生产 writer：`execution_planner.commit_fill`；`demo_seed.py` 是非生产 seed writer。尽管只有一个 SQL owner，调用方目前可不经统一 execution decision 直接调用它。
- Execution 导致的位置 mutation：1 个权威 lot 写点 `_record_lot`、1 个权威 FIFO 消耗点 `_consume_available_lots`，均由 `commit_fill` 调用；另有 1 个展示投影写点 `_sync_positions` 写 `paper_positions`。
- Execution 导致的 cash mutation：2 个方向函数 `_debit_shared_cash` / `_credit_shared_cash`，由 `commit_fill` 调用并委托 `paper_shared_cash`；其他 `paper_accounts.cash` 写入属于账户/周期设置或恢复路径。
- T+1：目前有 2 个 execution decision/commit owners（`manual_orders._manual_order_plan` 先判断 sellable qty；`paper_trading._consume_available_lots` 最终按 `available_date <= asof_day` 防止超卖）。买 lot 的 `available_date` 由 `_record_lot` + `paper_trading_rules.asset_type/next_weekday` 产生。风险卖出和自动卖出还依赖 caller 的持仓投影 `available_qty`。
- Limit-up/down：`paper_trading_rules.limit_pct` 是阈值定义处；execution caller 至少有 strategy buy（跌停拒绝、涨停封顶）、manual plan（跌停卖出拒绝）、risk sell（跌停无 fill）三处独立决策。缺少基于 locked 状态与可执行队列的唯一判断；触及价格阈值与锁死没有可靠区分。
- Suspension/tradability：没有独立执行 authority。多数路径借助 quote freshness/存在性 fail closed；缺行情字段或停牌证据没有一个稳定统一的 order reason。
- Fees：`paper_trading_rules.commission` / `_commission` 是 commission 公式 owner，但 stamp duty 的适用与加总分布在 manual、risk sell、intraday 路径；`commit_fill` 只持久化 caller 传入的 fee。
- Slippage：`paper_trading_rules.SLIPPAGE` 给常量，但价格调整重复在 `paper_trading._buy_order`、intraday sell/buy、manual planner、risk sell、加仓等路径；不存在独立可追踪的 ruleset version。
- signal 旁路：不存在生产代码直接 `INSERT paper_fills` 的 signal 旁路；但 signal approval 后 `_buy_order` 直接写 `filled` order 并请求 full-qty `commit_fill`，没有独立 `executable now / partial / blocked` decision，语义上把 approved signal 当成可全额成交。
- 隐式 current：存在。`_quotes` 是实时行情 provider 入口并可回退到本地 universe/full-market cache；历史 `asof_date` 不能证明这些 cache 是 PIT。`commit_fill` 的 `executed_at` 与 `_record_lot.acquired_at` 使用 `_now()`；risk sell 的 `quote_at` 可 fallback `_now()`。策略买入会从 R25 signal 冻结 provenance 取 stamp，但 risk/manual 个别 order 会从调用时 cycle/account stamp 取值。
- writer transaction 内网络：主 `execute_open`、pending manual 目前把行情/provider 获取放在 `immediate` writer 前；`manual_orders._manual_order_plan` 在 `conn.in_transaction` 时不调用 provider，`_market_state(...allow_network=False)` 也有 fail-closed 路径。模块级 `_quotes`、`_market_state` 仍包含 provider I/O，API 没有要求 execution caller 必须从事务外传入冻结证据，因此边界未由 execution contract 强制；R26 将以 monkeypatch provider 的 writer test 和 architecture guard 固定此边界。

## 修改前正常行为刻画

- 生产路径测试 `test_production_path_golden_replay.ProductionPathGoldenReplayTests` 已覆盖真实 `generate_signals → run_slot('open') → order/fill → position/cash`，使用离线注入行情，不手写 order/fill；`ProductionInvariantTests.test_stale_signal_does_not_fill`、`test_same_day_sell_is_rejected_by_t_plus_one` 及 `test_full_replay_digest_is_deterministic` 已覆盖拒绝、T+1 和 replay。
- 自动买入通过 R25 approved/pending signal 后，在 `execute_open` 的同一 SQLite 写事务中进行后续策略复核、数量/价格/费用计划，并插入一笔 `status='filled'` order；`commit_fill` 写单一 full-qty fill、扣 amount+fees、写 buy lot、同步 `paper_positions`。买入 fill price 是 caller 预先计算值（通常 quote × 1.001 并按部分路径做 limit clamp），commission 使用固定比例；当时没有执行端 liquidity cap。
- Risk sell 与 manual immediate order 也调用 `commit_fill` 全额成交。Risk sell 对 quote pct 的跌停阈值设 no-fill order；T+1 由 position available quantity 和 FIFO lot consumption 双重阻止。
- signal / order 行带 cycle 与 strategy provenance；普通策略买单由 signal provenance resolver 继承。fill 表只有 quote timestamp 和 assumption，缺少冻结 market evidence、规则版本、执行 reason vocabulary 与 idempotency event key。
- pending manual limit 支持 retry/expiry/cancel，但 canonical fill 仍是单次 full fill；没有累计 `filled_qty / remaining_qty` 或多个 fill lifecycle。

## 初始复杂度观察

- `paper_trading.py` 是共享策略协调器，当前 14,000+ 行；执行价格、fees、资格与流程横跨 `paper_trading.py`、`manual_orders.py`、`paper_risk_service.py`、`execution_planner.py`、`paper_trading_rules.py`。
- 已存在的 `execution_planner.py` 已拥有 `ExecutionPolicy`、准入 plan、重验证与唯一 fill SQL owner。R26 优先扩展此现有业务边界承接 simulation decision / commit；不另造 forwarding service/facade。
- 下游 debt：没有历史逐分钟成交/盘口排队证据，无法诚实模拟交易所 order book；R26 应采用 fail-closed 的可解释日内成交量参与率近似，不做完整撮合引擎。

## 修改后状态（R26）

- **唯一执行决定权威：** `execution_planner.evaluate_simulated_execution` 纯函数决定可否执行、数量、价格、费用和稳定 reason；`execution_planner.commit_fill` 是唯一写 fill、消耗/增加 lot、现金、position 投影并盖执行证据的事务入口。`commit_fill` 先冻结 quote/R24 与 tradability archive 事实，再评估；不请求 provider，也不读当前 strategy head / active cycle 来补历史上下文。
- **Order writers：7 → 7。** 7 个 `paper_orders` 意图写点保留，覆盖 strategy/manual/risk/queue；R26 没有伪称把不同业务意图的 INSERT 合成一个 writer。approved strategy signal 现在先写 `pending_execution`，不再预写 `filled` 或把 desired qty 直接送去全额结算。
- **Fill writers：1 → 1。** SQL 写入位置未变，但唯一 `paper_fills` writer 现在必须通过 simulation decision 才能插入，并带 event key、数量、price basis、market/tradability evidence、as-of 与 ruleset。
- **Position execution mutation sites：3 → 3。** `_record_lot`、`_consume_available_lots`、`_sync_positions` 仍是两个账本 lot 原语和一个展示 projection；调用边统一经过 `commit_fill`，部分成交只按真实成交量增减。
- **Cash execution mutation sites：2 → 2。** `_debit_shared_cash` / `_credit_shared_cash` 没变；执行账务由 authority 在 fill/order 同事务更新。
- **T+1：** 原有两道判断（manual plan + FIFO 可卖 lot defense）；R26 decision owner 为 `evaluate_simulated_execution`，FIFO 仍保留为提交时防线。报告口径：规则 owner 2 个散点 → 1 个 authority + 1 个账本防线。
- **Price limit / tradability：** caller 上的涨跌停成交判断由 execution evaluator 收敛为 1 个 owner；停牌/锁板/缺失 tradability 由 0 个一致的执行判定点 → 1 个 evaluator 处理，事实来自 `tradability_archive`。
- **Fees / slippage：** 执行费用和滑点的策略点由 manual/risk/strategy 多路径 → `execution_planner` 的 `estimate_execution_terms` 与 `evaluate_simulated_execution` 共用实现；费率继续来自既有 `paper_trading_rules`。策略预估和资金 sizing 只调用同一 helper，不再自己乘 `SLIPPAGE` 或拼印花税。
- **时间/as-of：** 原 fill `executed_at`、买 lot `acquired_at` 与部分 risk `quote_at` 可以回落 `_now()`；R26 fill 与 lot 时间使用 decision 的显式 `execution_asof`，quote 缺时间、历史日无当日 evidence 就不成交。Intent `created_at` 仍是下单时钟时间，这是委托事实，不冒充成交时点。
- **Network/writer boundary：** R26 pending execution 先取 quote、再开 SQLite writer；`commit_fill` 只读本地事实。provider 被 monkeypatch 成异常的 writer-boundary 用例通过。
- **复杂度与 facade：** 没有新增 production module；新增 1 个真正运行待成交事件的 scheduler/orchestrator（`process_pending_execution_orders`），没有转发 facade。删除了“approved signal → 预写 filled → 同步整笔成交”执行捷径。price/fee/limit/T+1 规则从多个 caller 收敛到单个 pure evaluator；策略 risk/approval 决策仍由原有 owner 负责。
- **关键复杂度观察：** 旧系统要在 `_buy_order`、`_manual_order_plan`、risk sell 和 intraday special path 之间追执行规则；现在“为什么成交/未成交”从 evaluator + `commit_fill` + R24/tradability evidence 可追。`paper_risk_service.run` 仍是 risk policy 热点，未把 risk policy 挪进 execution。
- **`paper_trading.py`：** 基线 `15,000 LOC / 282` 个顶层函数；修改后 `15,211 LOC / 284` 个。文件增加 211 行（主要是 pending intent 重试与执行队列协调），LOC 只作观察；没有新增 wrapper facade 或独立 production module。
