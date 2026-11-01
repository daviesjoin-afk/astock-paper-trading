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
| `paper_capital_reservations` | **owned** | `paper_capital_reservations` | 三批收敛完成：`paper_trading` / `entry_lifecycle` / `execution_dispatch` 全部改为调用装配层注入的 release 回调 |
| `paper_position_lots` | **owned** | `paper_lot_ledger` | 创建/消耗/成交血缘都归 lot owner；`commit_fill` 通过注入的 `link_source_fill` 回调回填 `source_fill_id` |
| `paper_risk_decisions` | **owned** | `paper_risk_writes` | 第五批统一来源信息校验及组装；两个 `_risk_log` 只准备快照、策略戳和时间 |
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
| ~~`execution_dispatch._release_reservation`~~ | **已迁移（第三批）**：`run_execution_dispatch` / `retire_gated_orders` / `resolve_verification` 接收必填 `release_reservation`，由 `paper_trading` / `paper_slot_service` 注入；本模块不 import 业务 owner，跨域边不增 | — |
| ~~`paper_position_lots.source_fill_id`~~ | **已迁移（第四批）**：lot owner 新增 `link_source_fill(conn, lot_id, fill_id)`，`commit_fill` 通过注入的依赖字段调用；同一 `conn`、同一事务、`source_fill_id` 的 legacy NULL 语义不变 | — |
| ~~`paper_risk_decisions` 双写~~ | **已迁移（第五批）**：两个 `_risk_log` 统一调用 `paper_risk_writes.insert_risk_decision`；来源信息由写入 owner 组装，词汇及结构仍属 schema owner | — |
| `paper_audit` 双写 | adaptive 侧恢复路径直写 | 走 `paper_repository.audit` 端口 |
| `paper_orders` / `paper_signals` / `paper_accounts` 多写 | 状态机按生命周期阶段分权，强行合并会把阶段判定塞进单一 owner | 逐阶段收口（先做"终态写入"单一出口） |

## 6. 机器可验证的边界（`backend/test_ledger_ownership.py`，9 项）

> **已知限制（必须与结论一起读）**：守卫扫描的是**静态 SQL 字面量**。f-string / 运行时拼接
> 出来的 SQL 不在它的视野里，因此当前矩阵的准确表述是"静态 SQL 写入清单及其守卫"，
> 不等于"所有可能的运行时写入方式都已覆盖"。动态 SQL 写入审计（例如在 `paper_storage`
> 的连接层按语句归因）是后续独立工作项。

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

## 9. 第五批 风险决策来源信息与写入收敛

`paper_trading._risk_log` 与 `paper_risk_service.run` 内的 `_risk_log` 均调用
`paper_risk_writes.insert_risk_decision`。运行时静态 SQL 写入方由 2 个降为 1 个，
矩阵和守卫同步收紧为 **owned**。

调用方继续负责各自的决策快照、策略版本解析、时钟及 AI 意见消费；写入 owner 负责
订单关联的权限声明校验和 `decision_provenance` 组装，合法词汇与结构继续调用
`paper_schema_migrations.risk_decision_provenance`。不根据订单号或 payload 猜测权限。
缺少权限声明的关联订单在快照准备前拒绝，直接调用写入 owner 同样拒绝。

保留两条旧路径的 JSON 顺序：主入口不排序、风险服务排序。保留历史未关联订单的
NULL 与无显式权限声明时的旧 payload；不补造历史 provenance 或 cycle。
仍使用调用方的同一连接、同一事务、同一条 INSERT，owner 不 commit。

冻结回归覆盖两条路径的完整行（包括原始 JSON 文本）、七种合法权限、来源信息覆盖、
非法/缺失权限拒绝、调用方 payload 不变、事务整体回滚以及快照/策略戳/意见消费一次。
已有风险服务、买入权限来源、卖出原因和影子比较回归验证业务链。

| 指标 | 第四批基线 | 第五批 |
| --- | --- | --- |
| `paper_risk_decisions` 静态 SQL 写入方 | 2 | **1** |
| `cross_domain_edges` | 629 | 629 |
| `max_file_lines` | 14623 | 14617 |
| `foundation_violations` / `import_cycles` | 0 / 0 | 0 / 0 |

冻结离线场景（全仓风险退出、继续持有）与第四批版本逐字段比较：决策行原始 JSON、
成交、NAV、剩余 lot 全部一致。SQL trace 证明每次仍只有一条 INSERT。
306 项相关回归通过；四项语义变异（移除权限要求、误标权限、提前 commit、强制 JSON
排序）均被新测试拦截。以上是结构与行为证据，不声称实盘性能提升。

回滚：revert 本批提交即可；没有 schema 或数据迁移。
