# R26 模拟成交链路与验证

本次改造基于 `ee1b2aeb82c935965253b1c32dec923596459fc5`。模拟成交只落入 paper ledger，
不会连接券商或提交真实订单。

## 唯一执行 authority

`backend/execution_planner.py::execute_order` 是唯一模拟执行入口。它从持久化订单和本次
已校验行情构造不可变执行上下文，先作出完整、部分、等待或阻断决定，再在同一账务事务内
写资金预占、现金、持仓 lot、fill event、订单状态、验证戳和审计。每次新行情重试都有独立
幂等事件键，重复同一行情不会重复记账。

| 调用场景 | 委托创建或准入 | 成交与账本写入 |
| --- | --- | --- |
| 自动策略买入 | `paper_trading._buy_order`、`manual_orders.commit_strategy_entry_fill` | `execution_planner.execute_order` |
| 手动新单与等待单 | `manual_orders.submit_manual_order`、`process_pending_manual_orders` | `execution_planner.execute_order` |
| 策略等待/部分成交续撮 | `manual_orders.process_pending_strategy_executions` | `execution_planner.execute_order` |
| 盘中做 T | `paper_trading._intraday_sell` | `execution_planner.execute_order` |
| 风控退出 | `paper_risk_service.run` | `execution_planner.execute_order` |
| 策略加仓/回补 | `manual_orders._commit_strategy_buy` | `execution_planner.execute_order` |

行情由调用方在打开账本写事务前取得。等待委托的重试器先批量取行情，再持有写事务；执行
authority 内没有行情 provider 调用。提交订单不等于可成交，可成交也不等于已成交；数据库
只以实际 fill event 证明已经完成的数量。

## 执行与账本规则

- `paper_trading_rules.simulated_execution_terms` 是实际成交价、滑点和费用的唯一计算入口。
  执行决策同时记录规则版本、行情时点、验证状态、流动性依据、价格依据和阻断原因。
- 执行必须使用当日已验证的实时行情，并校验交易时段、证券状态、涨跌停边界、对手盘/成交
  量容量、限价和可卖数量。缺少关键行情证据时不成交。
- 成交量按显式参与率和盘口容量限额，可以部分成交。每笔 fill 有自己的唯一事件键；剩余数
  量保留在原委托，后续新行情可继续执行。取消只取消未成交余量，不删除历史成交。
- 股票卖出按已解锁 lot 数量执行 T+1；全平允许卖出不足 100 股的剩余零股，部分零股委托
  仍被拒绝。
- 买入每笔 fill 创建一个 lot，并通过 `source_fill_id` 精确关联来源成交；读模型按逐笔事件
  验证、FIFO 消耗和 as-of 日期重建持仓、现金和已实现盈亏。部分订单不会被升级成整单完成。
- 运行页和归档页展示委托量、成交量、剩余量、成交事件、费用/滑点、行情证据、执行时点、
  阻断理由和服务端允许的操作。前端只展示服务端事实，不重算成交或风险规则。

## 本地验证

`backend/test_r26_execution_production.py` 通过真实 paper schema 和生产 `execute_order` 路径
离线回放：首日部分买入、同日卖出被 T+1 阻断、下一交易日卖出解锁份额、买入余量由另一行情
事件成交，最后核对多个 fill、逐 lot 来源关联、持仓和现金均通过有证据读模型验证。

还覆盖 `test_production_path_golden_replay.py`（自动策略完整生产回放）、`test_execution_decision.py`
（模拟规则）、`test_execution_lifecycle.py`（状态语义）、`test_portfolio_read_model.py`、
`test_paper_repository.py` 与 `test_paper_archive_projection.py`（账本/API/归档投影）。CI 的
Python 3.11 / 3.12 结果仍以 GitHub exact-head 检查为准。
