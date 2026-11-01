# 核心账本表 read/write owner 矩阵（PR-10）

基线：`master` @ `5e0dc37`。本文件与 `backend/test_ledger_ownership.py` 是同一份契约的
两种载体：文档说明语义，守卫**机械校验**写入方边界。

## 1. 盘点方法

* 只扫生产模块（排除 `test_*`、`__init__`）；
* 只统计**以 SQL 关键字开头**的字符串字面量（docstring / 注释里的表名不计）；
* `paper_schema_migrations` / `db_migrate`（schema）、`demo_seed`（种子）、
  `recover_legacy_order_cycle_provenance`（一次性历史修复）记为 **maintenance**，
  不参与 owner 比较，也不受守卫约束；
* 表名以 `paper_schema_migrations.REQUIRED_TABLES` 为准核对，不假定表名。

## 2. 写入矩阵（本次实测）

| 表 | 状态 | 运行时写入方 | 说明 |
| --- | --- | --- | --- |
| `paper_fills` | **owned** | `execution_planner` | 成交流水只有 `commit_fill` 一处写入 |
| `paper_positions` | **owned** | `paper_lot_ledger` | 投影表，只能由 lot owner 同步 |
| `paper_capital_reservations` | **pending** | `paper_capital_reservations`（owner）+ `execution_dispatch` | 第二批已把 `paper_trading` 与 `entry_lifecycle` 两处收敛（后者改为调用方注入 release 回调）；`execution_dispatch` 见 §5 |
| `paper_position_lots` | **pending** | `paper_lot_ledger`（owner）+ `execution_planner` | `commit_fill` 在同一事务里回填 `source_fill_id` |
| `paper_risk_decisions` | **pending** | `paper_risk_writes` + `paper_trading` | `_risk_log` 仍自行组装 provenance |
| `paper_audit` | **pending** | `paper_repository` + `adaptive_engine` | 账本审计端口 vs adaptive 恢复路径 |
| `paper_orders` | **pending** | `paper_trading`、`manual_orders`、`execution_planner`、`execution_dispatch`、`execution_verification`、`entry_lifecycle`、`paper_risk_writes` | 订单状态机按生命周期阶段分写 |
| `paper_signals` | **pending** | `paper_trading`、`manual_orders`、`execution_dispatch`、`entry_lifecycle`、`signal_service` | 信号同样按阶段分写 |
| `paper_accounts` | **pending** | `paper_trading`、`paper_shared_cash`、`paper_user_cycle_attachment`、`paper_user_account_provisioning`、`strategy_registry`、`paper_order_execution_authority`、`paper_cycle_ownership`、`adaptive_engine`、`adaptive_risk`、`adaptive_selection`、`adaptive_selection_compat`、`evolution_apply`、`ai_adoption_writer` | 不同 owner 各写自己的字段（现金/周期挂接/策略参数） |

**`owned` = 单写方**；**`pending` = 多写方是现状，本 PR 只锁定集合（只减不增）**。

## 3. 读取侧分层（多读方是允许的，但要区分口径）

| 层 | 模块 | 说明 |
| --- | --- | --- |
| 权威事实读 | `paper_position_read_model`（当前持仓权威）、`paper_capital_reservations`（预占证据）、`paper_portfolio_read_model` | 只读，且不回落投影 |
| 只读投影 | `dashboard_queries`、`paper_repository`、`paper_account_metrics`、`strategy_health_service` | 展示/统计用，不参与交易判定 |
| 派生视图 | `paper_replay_regression`、`intraday_t_pair_report`、`diagnose_cycle_lifecycle_conflict` | 诊断/回归，可随时丢弃 |

`paper_positions` 是**兼容投影**，执行权威是 `paper_position_lots`；读取方一律走
`paper_position_read_model`，不得用投影反推可卖数量。

## 4. 本 PR 的迁移（最小可验证切片）

**`paper_trading._reconcile_signal_order_states` 的预占释放**

旧调用图：

```
paper_trading._reconcile_signal_order_states
  └─ UPDATE paper_capital_reservations SET status='released' ...   ← 第二份实现
```

新调用图：

```
paper_trading._reconcile_signal_order_states
  └─ paper_capital_reservations.release_order_reservation(conn, order_id, now=now)
        └─ UPDATE paper_capital_reservations SET status='released' ...
```

* owner 新增的是**业务操作** `release_order_reservation(conn, order_id, *, now)`，不是
  任意 SQL 包装；时间戳由调用方传入（owner 不读时钟）；
* 语义逐字保留：只处理 `status='reserved'`、`released_at` 用 `COALESCE` 保留首次时间、
  `order_key` 按 `CAST(... AS TEXT)` 比较（兼容整数存储的旧键）；
* 事务边界不变：仍在调用方同一个连接、同一个事务里执行；owner 不 commit。

## 5. 未迁移清单（含原因，均为后续独立切片）

| 项 | 原因 | 下一步 |
| --- | --- | --- |
| ~~`entry_lifecycle._release_reservation`~~ | **已迁移（第二批）**：`expire_stale_orders/signals` 现在接收 `release_reservation` 回调，`paper_slot_service` 注入 `PCR.release_order_reservation`；foundation 层不再内联预占 SQL | — |
| `execution_dispatch._release_reservation` | `execution_dispatch`（execution 域）import paper 域 owner 会新增跨域边，而跨域预算**正好用满**（629/629） | 同上：调用方注入回调，或先消除一条既有边 |
| `paper_position_lots.source_fill_id` | `commit_fill` 在成交事务内回填血缘，改走 lot owner 需要新窄接口且动到最敏感的提交链 | lot owner 增加 `link_source_fill(conn, lot_id, fill_id)`，随后替换 |
| `paper_risk_decisions` 双写 | `_risk_log` 自行组装 decision provenance；`paper_risk_writes` 只有原语 | 先统一 provenance 组装，再收口 |
| `paper_audit` 双写 | adaptive 侧恢复路径直写 | 走 `paper_repository.audit` 端口 |
| `paper_orders` / `paper_signals` / `paper_accounts` 多写 | 状态机按生命周期阶段分权，强行合并会把阶段判定塞进单一 owner | 逐阶段收口（先做"终态写入"单一出口） |

## 6. 机器可验证的边界（`backend/test_ledger_ownership.py`，9 项）

1. 矩阵里每张表都存在于 `REQUIRED_TABLES`（防止表名写错）；
2. **非 owner 写入不回流**：任何新模块写这些表 → 立即变红；
3. `owned` 表必须**恰好**一个运行时写入方；
4. 预占释放的 UPDATE 只允许 owner + 两个已登记例外（多一个、少一个都变红）；
5. owner 接口必须是业务操作（参数 `conn/order_id/now`），不得退化成任意 SQL sink；
6. 冻结等价：释放后 `status='released'`、`released_at` 取传入时间；重复释放返回 0 且不改写首次时间；只动 `reserved` 行；整数存储键仍命中。

## 7. 预算与性能

| 指标 | 基线 | 本 PR |
| --- | --- | --- |
| `cross_domain_edges` | 629（= 上限） | **629**（未新增边） |
| `max_file_lines` | 14623 | **14618**（-5） |
| `injected_private_fields` / `from_import_private_names` / `dynamic_name_bridges` / `private_cross_access` | 0 / 0 / 72 / 6 | 不变 |
| `raw_sql_modules` / `duplicate_helpers` | 141 / 296 | 不变 |

SQL 调用次数：对账路径由"内联 UPDATE"变为"调用 owner 的同一句 UPDATE"，**语句数不变**，
只多一次 Python 函数调用；无新增查询、无新增事务。

## 8. 风险与回滚

* 风险：`CAST(order_key AS TEXT)` 语义若被误改会让旧整数键的预占无法释放 —— 已用冻结测试覆盖；
* 风险：owner 抛错时对账中断 —— 与旧实现一致（旧代码同样不吞异常）；
* 回滚：`git revert` 单个提交即可；owner 新函数是纯新增，回滚不影响其他调用方。

MERGE: NOT MERGED · DEPLOY: NOT DEPLOYED
