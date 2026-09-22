# R22 Authority Audit

日期：2026-09-21
基线：`a628b194cdf984b11a8cbd3eaba0266f28342abc`（R21 / PR #178 合并）
范围：portfolio truth、historical read model、cycle/as-of ownership
结论：当前架构已经把 `paper_position_lots` 定为数量权威，但“显式历史读取”仍有真实缺口，不能把 live projection 当成历史事实。

## 1. quantity authority 是谁？

权威来源：`paper_position_lots` 的 durable lot 事实。

- 每笔已验证 BUY 经由 `execution_planner.commit_fill` 写入 `paper_position_lots`；
- `remaining_qty` 只描述**当前**剩余量，不能直接代表某个 `asof_day` 的历史数量；
- 正确历史数量应由 `lot.qty - asof 当日及以前已验证 SELL 消耗量` 重建；
- `paper_positions.qty` 只是兼容投影，不是执行或历史权威。

现状问题：`paper_position_read_model.positions_for_cycle` 虽然按 `cycle_id` 过滤，但仍直接读取 `remaining_qty>0`，所以同一周期内 D 之后发生的 fill 会改写 D 的历史视图。

## 2. acquisition cost authority 是谁？

权威来源：`paper_position_lots.cost`。

- `commit_fill` 把 BUY 手续费摊入 lot cost；
- cost 是 lot acquisition cost，不是 projection 的显示成本；
- `display_cost` 只有在能证明同周期、as-of 前所有成交现金流完整时才能用 `verified_cash_flow`；
- 不能拿当前 `paper_positions.cost` 反推历史 acquisition cost。

现状问题：`_verified_cash_flows` 没有周期和 as-of 条件，同账户/代码的后续周期 fill 会改变历史 `display_cost`。

## 3. realized PnL authority 是谁？

权威来源：`execution_planner.commit_fill` 在已验证 SELL 成交时写入的 durable execution fact（`paper_orders.realized_pnl` + 被消耗 lot 的权威成本）。

- R20 已经把 SELL fill commit 收敛到 `execution_planner.commit_fill`；
- R22 只读，不复制 commit、不重算 consumed lot、不重新 credit/debit cash；
- pending、rejected、failed-verification、未验证 fill 不能进入 realized PnL；
- 对显式 as-of，只允许 `fill_date <= asof_day` 且订单周期等于请求周期的 verified SELL。

## 4. commissions / fees authority 是谁？

权威来源：`execution_planner.commit_fill` 写入的 `paper_orders.fees` / `paper_fills.fees`。

- BUY 手续费在 lot `cost` 中体现；
- SELL 手续费进入 committed realized PnL 和现金净流入；
- projection 不得重新估算或改写 fees。

## 5. cash authority 是谁？

目标权威：周期声明资本 + 请求周期在 `asof_day` 前已验证成交的净现金流。

- `paper_cycles.capital` 是周期固定资本事实；
- `paper_fills` + 来源 `paper_orders` 是成交现金流事实；
- `paper_accounts.cash` 当前是 live attribution bucket / settlement allocation，不是历史现金权威；
- 无法完整证明某段历史现金时，历史 cash 必须是 unknown，不能回退当前 `_shared_cash`。

现状问题：`_shared_account_exposure` 和 `_shared_cash` 读取当前 account cash；显式 `asof_day` 不会限制现金范围。

## 6. market value 使用什么价格事实？

规则：只使用 caller 显式提供的 bounded valuation，或 durable/stored 的 as-of valuation evidence。

- R22 不接 provider reconstruction，不 fetch latest/current quote；
- 缺少历史价格时 `market_value = unknown`；
- 不得用 cost fallback 伪装成历史 market value；
- `unrealized_pnl` 同样依赖显式 valuation evidence，缺失即 unknown。

## 7. NAV 最终依赖哪些事实？

`nav = bounded cash + bounded market value`。

- cash unknown 或 market value unknown ⇒ NAV unknown；
- 不得混入 `paper_positions` 当前投影、current account cash、latest quote；
- 历史缺失必须显式返回 unavailable/unknown 状态。

## 8. paper_positions 当前是 authority 还是 projection？

`paper_positions` 是 compatibility projection / current read optimization，零 execution authority。

现有代码已有 R14 注释和测试约束：权威方向只能是 `paper_position_lots -> paper_positions`，反向禁止。R22 不得把 projection 重新升级成历史事实。

## 9. 哪些 historical read 会重新读取 active/current state？

已识别：

- `paper_trading._position_rows`：无 `asof_day` 时走 active cycle；
- `paper_trading._shared_account_exposure`：通过 `_position_rows` 走 active cycle，并通过 `_shared_cash` 读当前现金；
- `paper_trading._account_metrics`：默认读 active-cycle positions、latest/current NAV 与 all-time sells；
- `paper_trading._account_metric_inputs` / `paper_repository.account_metric_inputs`：NAV、sell、fill 统计没有 cycle/as-of 条件；
- `dashboard_queries.dashboard`：显式用 `dt.date.today()` 和 active cycle；
- `_sync_positions` / `_record_nav`：读取 `_position_rows`/account cash 写当前投影；
- `paper_position_read_model.current_positions`：按设计解析 active cycle；只有 `positions_for_cycle` 是 exact-cycle 入口。

R22 需要给真正的历史读模型新增显式 `PortfolioReadContext(cycle_id, asof_day)`，并让历史读取只走该上下文。

## 10. 哪些 read 会读取 cycle 之后发生的 fill/order/lot？

已识别：

- `positions_for_cycle`：读取 `paper_position_lots.remaining_qty`，会看到同一周期 D 后的 SELL 消耗，也会看到 D 后新增但已经存在的 lot；
- `_verified_cash_flows`：按 account/code 汇总所有周期、所有日期已成交订单；
- `_shared_cash` / `_shared_account_exposure`：读取当前 `paper_accounts.cash`；
- `_account_metrics` / `_account_metric_inputs`：all-time sells 与 current NAV；
- `_record_nav`：用 current positions + current account cash 写 NAV 行；
- `dashboard_queries`：today/active-cycle read 作为 current view 合法，但不能被当成历史回放证据。

## R22 authority model（目标）

```text
verified execution / durable lot facts
        ↓
cycle/as-of bounded portfolio facts
        ↓
immutable PortfolioReadContext
        ↓
API / dashboard / risk / research consumers
```

R22 只读消费，不新增成交、cash 或 lot mutation path。
## R22 实现落点

- 新增 `backend/paper_portfolio_read_model.py` 作为严格 `(cycle_id, asof_day)` 只读组合权威；它直接消费 durable lot / verified fill facts，未知价格保持 unknown。
- `paper_position_read_model` 继续作为 R19/R20/R21 的 current/live compatibility read；R22 不改变它的 live 语义，避免把 wall-clock 生成的 live lot 误判为历史未来事实。
- `_shared_account_exposure` 增加显式 `cycle_id` 入口，把 risk application 的持仓读取钉回已认领周期；legacy/unverified cash 只能在该 R21 compatibility port 内回退到周期初始资本，严格 PortfolioReadContext 仍返回 unknown；绝不读取 current account cash。
- 没有新增 schema migration；没有产生新的成交、lot 或 cash mutation path。
