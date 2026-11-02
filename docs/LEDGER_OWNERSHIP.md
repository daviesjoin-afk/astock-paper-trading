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
| `paper_audit` | **owned** | `paper_repository` | 第六批：追加及失败进化操作的清理都归表 owner；补偿方保留事务与异常处理 |
| `paper_orders` | **owned** | `paper_order_ledger` | A/B/D 运行时写入收口，子查询读表不算写入 |
| `paper_signals` | **owned** | `signal_service` | 信号持久化统一入口 |
| `paper_accounts` | **owned** | `paper_account_ledger` | 四组业务职责保留；账户表单一持久化入口，跨字段更新仍为单条 SQL |
| `paper_cycles` | **owned** | `paper_cycle_service` | 周期创建/配置/启停/归档统一入口 |

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
| ~~`paper_audit` 双写~~ | **已迁移（第六批）**：adaptive 补偿路径调用 `paper_repository.remove_failed_evolution_audit`；保留原 DELETE、事务位置及异常处理 | — |
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

## 10. 第六批 审计清理与追加归同一表 owner

基线 `master` @ `e09b473`（#313 squash 合并后）。此次只把
`adaptive_engine._restore_paper_account` 的一条 DELETE 搬到
`paper_repository.remove_failed_evolution_audit(conn, account_id, event_name, candidate_id)`。
新增的是失败进化操作的清理接口，原有 `audit` 追加接口不变。

SQL 与参数逐字保留：账户、事件和 `candidate=<int id>;` detail 前缀必须同时匹配，
12 不会误删 123。仍使用 SQLite 原有 LIKE 行为，不解析、标准化或重写 detail；
二进制 detail 的匹配也由原 SQLite 构建选项决定。保留行的所有字段和行号不变，
因此事件内容、来源身份、原始 detail 及相对顺序不变。

调用位置仍在账户恢复和参数版本清理之后、原 commit 之前。owner 使用传入连接，
只执行原来的一条 DELETE，不 commit，也不捕获异常；补偿调用方原有的
`except sqlite3.Error: pass`、其他异常传播及 finally close 保留。较新账户版本的
恢复跳过规则及告警不变。

静态 SQL 写入方由 2 个降为 1 个，矩阵收紧为 **owned**。这项结论仍只针对静态 SQL
守卫覆盖范围，不证明所有运行时拼接 SQL 都只有一个写入方。

为明确表 owner 依赖，增加 `adaptive_engine → paper_repository` 导入。后者已登记为
基础 owner，按现有口径业务跨域边仍为 629，循环和基础层反向依赖均为 0；
所有预算不变，说明见 `docs/COUPLING_BUDGET.md`。
冻结测试覆盖完整保留行、原始 detail、显式/unknown 策略戳、写入顺序、事务回滚、
SQLite 错误容忍、非 SQLite 错误传播及版本冲突跳过。没有 schema 或数据迁移，
回滚本批提交即可。没有真实交易时段验证，未部署。

## 11. 第七批 订单自身过期的持久化切片

基线 `master` @ `5c46100`（#314 合并后）。仅迁移
`entry_lifecycle.expire_stale_orders` 内两条 UPDATE：订单终态交给已有订单写入模块
`execution_dispatch.expire_order(conn, order_id, *, reason, cancelled_at)`；关联信号交给
已有信号持久化模块 `signal_service.expire_for_expired_order(conn, signal_id, *, reason)`。
两个接口没有任意 status、SQL 或字段参数，不解析当前周期、不读时钟、不 commit。

`paper_trading` 使用已有 EPD/SIG 依赖显式绑定，经 `paper_slot_service.run_preflight`
传给生命周期层的两个必填回调。生命周期层不反向 import 业务模块。过期资格、
expires_at/TTL 优先级、边界、可恢复状态、原因文本、时钟、统计均保留原处。
同一连接、同一事务、每行仍依次释放预占、更新订单、按原条件更新关联信号。
预占释放的 SQLite 错误仍按原逻辑容忍，其他错误继续传播；调用方决定提交或回滚。

`expire_stale_signals` 保持原实现：先处理失效信号，再释放预占并把关联活动订单回收为
`superseded`。订单自身过期路径仍写 `expired`；两条路径不合并状态或顺序。
orders/signals 整表仍为 **pending**，原写入模块集合不增加；新增的只是本切片无内联写入、
窄接口与组装绑定守卫，不宣称整张表或所有运行时拼接 SQL 已收敛。

`backend/fixtures/order_expiry_5c46100.txt` 保留原函数作为冻结对照，比较完整订单/信号/
预占行、原因、时间戳、返回统计、SQL trace、异常及事务状态。覆盖有效期边界、显式期限
优先级、无关联/缺失信号、不同状态、重复清扫、两条路径不同终态与调用方提交/回滚。
没有 schema 或数据迁移；回滚本批提交即可。未做真实交易时段验证，未部署。

## 12. cycles 的验收覆盖补核查

此次只补齐登记和守卫，不迁移周期生产逻辑，也不把分阶段写入视为已发现的多写缺陷。

| 阶段 | 当前职责模块和入口 | 事务归属 |
| --- | --- | --- |
| 兼容引导 | `paper_trading._ensure_cycle`：必要时创建 legacy 周期、校正 legacy 参考资金，2 条静态写入 | 使用调用方连接，属于现有引导/初始化事务 |
| 新建与配置 | `paper_trading.create_cycle`、`configure_capital`：新建、资金配置，2 条静态写入 | 前者使用传入连接；后者在原 `_db()` 中写周期与账户资金 |
| 启停 | `paper_trading.set_accounts_status`：状态/started_at 更新，1 条静态写入 | 与账户状态、审计共用原 `_db()` 事务 |
| 归档 | `paper_cycle_service.archive_cycle`：归档状态，1 条静态写入 | 使用传入连接，快照、清理、审计同一事务；start/reset 在同一 `_db()` 中归档后新建 |

读侧：`paper_order_execution_authority.active_cycle_id_readonly` 只读活动周期身份；
`paper_cycle_ownership` 负责显式 idle、参与者和周期经济归属读取；
`paper_portfolio_read_model` 消费周期上下文，`paper_cycle_service.cycle_snapshot` 构造归档读模型。
`paper_trading.load_active_cycle` 在缺周期时会调用 ensure，**不是只读接口**。
这些职责不能机械合并为一个会隐式创建当前周期的 reader。

矩阵将 cycles 以 **pending** 登记为上述两个静态运行时写入模块，通用 owner 守卫由此覆盖
新增未登记写入；新增只读连接测试验证活动周期/显式空周期读取不能创建或修改账本。
这不是 cycles 已完成单写方或所有读取路径收口的结论。PR-10 后续仍须逐阶段治理订单、
信号、账户字段，并逐表核验周期、读取口径和动态 SQL；完成 PR-10 验收后才进入 PR-11。

## 13. 第八批 信号失效与关联订单回收的持久化

基线 `master` @ `71584c8`（#315 合并后）。只迁移 `entry_lifecycle.expire_stale_signals`
中的两条 UPDATE：`signal_service.expire_stale_signal(conn, signal_id, *, reason)`
负责原重试状态信号写 expired；`execution_dispatch.supersede_for_expired_signal(conn, order_id, *, cancelled_at)`
负责按原状态集合将关联活动买单写 superseded，并追加原固定回收原因。
原 SQL、状态条件和参数顺序均保留，不给信号增加时间字段，不提供任意 set_status。

组装根使用已有 SIG/EPD 依赖，经 preflight 显式注入两个新增必填回调；没有内部 SQL
回退或默认空操作。新接口使用传入连接、不读时钟或当前周期、不提交、不吞异常。
信号失效的跨日/TTL判断、原因文本、统计位置、查询、时钟继续由生命周期层负责。

严格保留两阶段：先扫描并更新**所有**失效信号，再查询其活动买单，逐单释放预占并写
superseded。不能改为逐信号交错回收。后续信号写失败时仍不进入订单回收阶段；
释放预占的 SQLite 错误仍按原逻辑容忍，其他错误继续传播，已有写入留在原事务中由
调用方提交或回滚。订单自身过期的 expired 路径保持 #315 合并后的实现不变。

生命周期层至此不再直接写订单或信号，允许静态写入模块集合分别由7→6、5→4，
整表仍为 **pending**。此结论限于静态 SQL 守卫，不代表所有动态拼接 SQL 已覆盖。
cycles、账户字段和其他订单/信号阶段的既有待办继续保留，本批不扩大到成交链或 PR-11。

冻结对照为 `backend/fixtures/signal_expiry_71584c8.txt`。比较完整行、原始字段、原因、
时间戳、统计、SQL trace、异常和事务状态；新增多信号顺序、后续信号失败、已终态信号
遗留订单、重复清扫及回收前订单转 filled 等用例。未迁移的订单自身过期函数保留冻结
AST 对照。没有 schema 或数据迁移，回滚本批提交即可；未做真实交易时段验证，未部署。

本批实测：`cross_domain_edges` 629、`import_cycles`/`foundation_violations` 均0，
`max_file_lines` 14618→14619（组装根新增一行绑定），其他指标不退化，所有预算不变。

## 14. 第九批 等待池重复记录回收的持久化

基线 `master` @ `48efdef`（#316 合并后）。迁移的是
`paper_trading._recover_stale_runtime_state` 里的**一对** superseded 写入：
旧等待池订单退役，及其关联信号的替代标记。两处合成
`execution_dispatch.retire_duplicate_waitlist(conn, *, waitlist_status, signal_statuses)`。

* 两条 SQL、`EXISTS` 自连接谓词、`reason` 拼接字面量、`status IN (...)` 集合全部逐字保留；
* 顺序是契约：先用订单的原因标记写 superseded，再用
  `reason LIKE '%等待池记录已保留%'` 反查同批信号 —— 先信号后订单会漏掉本轮新标记的行，
  owner 内固定执行顺序，调用方无法拆开；
* 判定（哪条算"较新"、用哪两个活动状态）仍由启动恢复路径给参数，owner 不做时钟读取、
  不做周期解析、不 commit、不吞异常，全部写入留在调用方同一事务；
* 旧行 `reason` 为 NULL / 空串 / 已有文本时，`COALESCE` 语义与原实现一致。

为什么两条放同一个 owner：信号那条 UPDATE 在 `WHERE id IN (SELECT ... FROM paper_orders ...)`
里读订单表。若把它拆给 `signal_service`，该模块会新增对 `paper_orders` 的跨表依赖，
把 `cross_domain_edges` 推高；而 `execution_dispatch` 本就在两张表的写入集合内，
既有的域内边不变。因此本批不新增写入口，也不新增跨域边 —— 写方集合仍是7/5个模块。

整表仍为 **pending**：`paper_trading` 在等待池之外的订单/信号写入（entry 路径、
归档、`_reconcile_signal_order_states`）没有迁移，本批只收口这一处操作级边界。

冻结对照为 `backend/fixtures/waitlist_dedupe_48efdef.txt`（逐字复制 48efdef 的回收块）。
`backend/test_waitlist_dedupe_write.py` 用同一场景分别跑冻结块与新 owner，比较完整行、
原因、未涉及行、SQL 顺序、重复执行幂等性、终态信号保护与事务未提交状态。
没有 schema 或数据迁移，回滚本批提交即可；未做真实交易时段验证，未部署。

本批实测：`cross_domain_edges` 629（不变）、`import_cycles`/`foundation_violations` 均0，
`max_file_lines` 14619→14599（`paper_trading` 净减20行），`duplicate_helpers` 296、
`dynamic_name_bridges` 72、`unsuffixed_module_ratio` 0.3906 均不退化，所有预算不变。

## 15. 第十批 活动委托/信号对账的持久化

基线 `master` @ `1d41508`（#317 合并后）。迁移 `paper_trading._reconcile_signal_order_states`
里的**三条**写入，函数结构、查询、判定与统计不变：

| 写入 | owner 入口 | 保留的语义 |
| --- | --- | --- |
| 重复 `execution_retry` 退旧 | `execution_dispatch.supersede_duplicate_execution_retry(conn, order_id)` | `WHERE id=? AND status='execution_retry'`、原固定原因 |
| 终态信号的遗留活动委托回收 | `execution_dispatch.supersede_for_terminal_signal(conn, order_id, *, reason)` | `ENTRY_RETRY_ORDER_STATUSES` 七状态保护、`reason` 前缀拼接 |
| 信号状态对账 | `signal_service.reconcile_signal_status(conn, signal_id, *, status)` | `WHERE status<>?`（已是目标态则整行不动，不刷原因）与原固定原因 |

* 三条 SQL 逐字保留；占位符按 `ENTRY_RETRY_ORDER_STATUSES` 长度静态展开（7 个）。
  状态**取值**仍来自唯一词表（SQL 里没有第二份状态字符串）。维护代价是明确的、有限的：
  集合**长度**变化时必须同步占位符数量；只改状态值而不改长度则不需要改 SQL，
  长度守卫也不会因此报错。该守卫只防参数数量漂移，不单独证明状态集合的业务语义。
* "哪条算最新重试"、终态集合、`signal_for_order` 映射、`partially_filled` 不降级规则、
  预占释放调用（`PCR.release_order_reservation` / `_finish_capital_reservation`）、
  `_now()` 读取、统计与返回字典仍留在 `paper_trading`。
* 三个 owner 都不读时钟、不释放预占、不提交、不吞异常；写入仍在调用方同一事务。
* 信号写入归 `signal_service` 不新增跨表边：该表本就在其写入集合内，SQL 也不读订单表
  （订单与信号的 JOIN 留在编排层的 SELECT）。

整表仍为 **pending**。本批只收口对账这一个阶段的写入，不宣称订单/信号整表单写；
`_supersede_signal_execution_retries`、entry 路径、归档路径的既有写入仍留在 `paper_trading`。

冻结对照为 `backend/fixtures/reconcile_1d41508.txt`（逐字复制 1d41508 的整个函数）。
`backend/test_signal_order_reconcile_write.py`（12 项）用最小 schema 跑真实对账路径：
冻结函数与新实现分别执行，比较返回值、三张表完整行、**SQL trace 逐条顺序**与时钟调用次数；
覆盖重复重试退旧+释放、五类终态信号的回收原因、信号漂移与部分成交不降级、
无操作行不重写、混合场景、缺表早退。没有 schema 或数据迁移，回滚本批提交即可。

冻结的边界要说清楚：fixture 冻结的是**函数体**，它调用的 `PSM`、`PCR`、状态常量与
`_finish_capital_reservation` 仍是当前实现 —— 对本轮未触碰这些依赖的切片是合理边界，
但不等于整条历史依赖栈都被冻结。

重复执行幂等（本批修正）：同一对连接连跑两轮，**逐轮**对照（第 n 轮旧实现 == 第 n 轮新实现），
再单独断言跨轮账本不变、第二轮没有 UPDATE、`retry_index` 由 `created` 转 `present`、
`orders_reconciled` 归零。上一版只在两套新库上各跑一轮再比较首轮快照，第二轮结果被丢弃，
属于观测缺陷：让实现只在第二轮额外改写 reason 的变异能通过旧写法、被新写法在 `round 2` 判红。

`retry_index` 三态的实际来源：`created` 由本文件各场景首轮断言；`present` 由修正后的第二轮
断言；`blocked` 由既有 `backend/test_ledger_bootstrap_lifecycle.py` 的专项回归承担
（该文件对 `SIGNAL_RETRY_INDEX_BLOCKED` 有多处断言），本文件不重复构造。

本批实测：`cross_domain_edges` 629（不变）、`import_cycles`/`foundation_violations` 均0、
`max_file_lines` 14599→14580、`max_function_lines` 1184、`duplicate_helpers` 296、
`dynamic_name_bridges` 72、`unsuffixed_module_ratio` 0.3906，所有预算不变。

## 16. PR-10 收尾清单与 A–E 工作包（冻结）

基线 `master` @ `8e3c22d`（#318 合并后）。本节把 PR-10 剩下的工作一次性固定下来：
四张 `pending` 表（`paper_orders` / `paper_signals` / `paper_accounts` / `paper_cycles`）
的运行时写入逐条登记到操作、判定者、最终 owner、事务边界与工作包，不再按"一条 UPDATE
一批"的方式推进。清单由 `backend/tools/ledger_write_inventory.py` 扫描源码生成，
人工判定落在 `backend/ledger_owner_plan.json`，守卫见
`backend/test_ledger_owner_plan.py`（计划与源码不一致时直接变红）。

### 16.1 扫描口径

* 只登记**真实写目标**：`UPDATE / INSERT [OR ...] INTO / DELETE FROM / REPLACE INTO`
  后的第一张表；语句里 `FROM` / `JOIN` 引用的表记为读引用（`read_refs`），
  不再像 `write_sites()` 那样把子查询里的订单表算成订单写入。
* 运行时之外的模块分角色登记，不混入写方集合：`test` / `seed`（`demo_seed`）/
  `migration`（`paper_schema_migrations`、`db_migrate`、`recover_legacy_...`）/
  `fixture`（`execution_quality_shadow`）。
* 动态拼接（f-string / `+` / `%`）单独标 `kind=dynamic`，已知动态路径按归属人工登记，
  不静默跳过。

### 16.2 最终写入 owner 模型

| 角色 | 归属 | 说明 |
| --- | --- | --- |
| 业务判定者 | 各阶段原有模块 | 决定"能不能做、目标状态是什么"，不搬家 |
| 事务协调者 | 各入口原有函数 | 决定顺序、提交与回滚，不拆分 |
| 表写入 owner | 下表 | 只做该表已授权的持久化 |

| 表 | 最终写入 owner | 状态 |
| --- | --- | --- |
| `paper_orders` | `paper_order_ledger` | existing（A 包第一片已建立） |
| `paper_signals` | `signal_service` | existing |
| `paper_accounts`（全部字段） | `paper_account_ledger` | existing；四组业务模块只保留判断/编排/兼容接口 |
| `paper_cycles` | `paper_cycle_service` | existing |

`paper_order_ledger` 只承担**订单行持久化**：创建 INSERT、状态迁移、成交结果字段、
核验戳、归档删除。成交判定仍唯一属于 `commit_fill`；`paper_order_ledger` 不允许
自己算成交价、费用、可卖量或 BUY 数量，也不 commit。

账户持久化通过窄业务操作进入同一表 owner。现金/开户/周期/配置仍保留业务职责；
周期成员配置只接受原有列白名单，跨字段初始化保持单条 SQL 与原事务。

### 16.3 工作包与出口

| 包 | 范围 | 出口证据 |
| --- | --- | --- |
| **A** | 订单/信号非成交生命周期：重试接管、等待池替代、手动取消/终止、派发等待与回收 | 调用方不再自行拼订单/信号状态 UPDATE；退役范围、原因、时钟、预占调用、统计与基线逐项对照 |
| **B** | 订单创建、成交结果、核验戳与信号执行反馈 | 保留 `commit_fill` 唯一成交权威；自动/手动/延期/部分成交/核验/失败路径投影一致 |
| **C** | `paper_accounts` 四组：创建补齐、现金、身份挂接、配置补偿 | 四组业务职责保留，账户表持久化单 owner；现金算法与版本检查不变 |
| **D** | `paper_cycles` 完整生命周期与跨表归档事务 | 创建/配置/启停/归档/恢复都有 owner；归档不删历史事实、不覆盖未知归属 |
| **E** | 十张核心表读取契约、守卫校准、跨包最终回归与关闭报告 | 逐表权威读/投影/允许调用方/事务归属成文；关键负向测试与完整 CI 通过 |

A–D 完成、且守卫与源码一致之后，四张表才允许从 `pending` 改 `owned`；只改标签不算完成。
E 不是新一轮重构：不要求清空全仓直接 SELECT、不设行数目标、不建通用数据库审计框架。

### 16.4 当前写入清单（自动生成）

原始冻结 ID 与迁移映射另见 `backend/ledger_frozen_baseline.json` / `backend/ledger_migration_map.json`；以下仅是当前写方清单。
以下表格由 `python backend/tools/ledger_write_inventory.py --markdown` 生成，
`test_ledger_owner_plan.py` 会比对"源码扫描结果 ↔ 计划 ↔ 本节内容"三者一致。

<!-- ledger-owner-plan:start -->

### 工作包 A

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_orders` | 用户撤单：订单转 cancelled（可撤销状态白名单） | `paper_order_ledger::cancel_order` | 手动入口校验 + 用户请求 | `paper_order_ledger` | 服务入口传入的连接 | `test_dependency_contract`、`test_operator_boundary`、`test_order_ledger` |
| `paper_orders` | 订单自身过期终态 expired | `paper_order_ledger::expire_order` | entry_lifecycle 判定，dispatch 落库 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_order_ledger` |
| `paper_orders` | 订单创建（INSERT）：冻结等待池标记（B 包将复用同一入口） | `paper_order_ledger::insert_order` | paper_trading 入场闸门 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_dependency_contract`、`test_execution_verification`、`test_order_ledger` |
| `paper_orders` | 等待池重复记录退役（订单，批量） | `paper_order_ledger::retire_duplicate_waitlist_orders` | paper_trading 启动恢复 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_order_ledger`、`test_waitlist_dedupe_write` |
| `paper_orders` | 挂起订单退役（batch/verification 等待超时） | `paper_order_ledger::retire_gated_order` | execution_dispatch 等待窗口 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_execution_dispatch`、`test_order_ledger` |
| `paper_orders` | 重复执行重试退旧 superseded | `paper_order_ledger::supersede_duplicate_execution_retry` | paper_trading 对账循环 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_ledger_ownership`、`test_order_ledger`、`test_signal_order_reconcile_write` |
| `paper_orders` | 失效信号名下买单回收 superseded | `paper_order_ledger::supersede_for_expired_signal` | entry_lifecycle 失效判定 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_order_ledger` |
| `paper_orders` | 终态信号遗留委托回收 | `paper_order_ledger::supersede_for_terminal_signal` | paper_trading 对账循环 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_ledger_ownership`、`test_order_ledger`、`test_signal_order_reconcile_write` |
| `paper_orders` | 重试接管/Dispatch 退役：订单转 superseded（含取消时间） | `paper_order_ledger::supersede_order` | execution_dispatch 重试接管判定 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_cycle_archive_writes`、`test_entry_lifecycle`、`test_ledger_ownership` |
| `paper_orders` | 新一轮执行重试接管：退役全部旧重试 | `paper_order_ledger::supersede_previous_execution_retry` | paper_trading 重试发起 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_order_ledger` |
| `paper_orders` | 派发终止：订单转调用方判定的终态 | `paper_order_ledger::terminate_order` | execution_dispatch 派发阶段 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_order_ledger` |
| `paper_signals` | 重试接管：过期信号一并收敛 expired | `signal_service::expire_after_order_retry` | execution_dispatch 重试接管判定 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 订单过期联动信号 expired | `signal_service::expire_for_expired_order` | entry_lifecycle 过期判定 | `signal_service` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_order_ttl_lifecycle` |
| `paper_signals` | 信号自身失效 expired | `signal_service::expire_stale_signal` | entry_lifecycle 失效判定 | `signal_service` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_paper_slot_service` |
| `paper_signals` | 信号状态对账 | `signal_service::reconcile_signal_status` | paper_trading 对账循环 | `signal_service` | 启动恢复/初始化连接 | `test_ledger_ownership`、`test_signal_order_reconcile_write` |
| `paper_signals` | 重试接管：信号回到复试管道 | `signal_service::reopen_for_retry` | execution_dispatch 重试接管判定 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 等待池重复记录退役（信号） | `signal_service::retire_waitlist_signals` | paper_trading 启动恢复 | `signal_service` | 启动恢复/初始化连接 | `test_ledger_owner_plan`、`test_waitlist_dedupe_write` |
| `paper_signals` | 派发终止：信号写终态（rejected/expired 白名单） | `signal_service::terminate_for_dispatch` | execution_dispatch 派发阶段 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 信号 payload 标记写入 | `signal_service::write_payload` | execution_dispatch 批次/核验标记 | `signal_service` | 调用方同一连接与原事务 | 待补 |

### 工作包 B

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_orders` | 卖出实现盈亏累加 | `paper_order_ledger::accumulate_realized_pnl` | commit_fill 卖出结算 | `paper_order_ledger` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_dependency_contract`、`test_execution_planner`、`test_execution_verification` |
| `paper_orders` | 执行结果推进（状态 + 执行原因，乐观锁） | `paper_order_ledger::advance_execution_state` | paper_trading 执行循环 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 执行阻塞结果写库（含乐观锁版本） | `paper_order_ledger::record_execution_block` | commit_fill 执行决策 | `paper_order_ledger` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_dependency_contract`、`test_execution_planner`、`test_execution_verification` |
| `paper_orders` | 成交结果写库（状态/成交量/均价/金额/费用/证据/版本推进） | `paper_order_ledger::record_fill_result` | commit_fill 唯一成交权威 | `paper_order_ledger` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_dependency_contract`、`test_execution_planner`、`test_execution_verification` |
| `paper_orders` | 已实现盈亏回填（重建口径） | `paper_order_ledger::record_realized_pnl` | paper_trading 重建口径 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 执行核验结论单行写入 | `paper_order_ledger::stamp_execution` | execution_verification 核验结论 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 执行核验结论批量写入（含历史回填） | `paper_order_ledger::stamp_execution_rows` | execution_verification 核验结论 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 较早意图被同标的新委托替代（批量） | `paper_order_ledger::supersede_older_intents` | paper_trading 实时复核 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 订单状态推进（status/reason/risk_payload，可清空成交字段） | `paper_order_ledger::update_order_outcome` | manual_orders 手动入口/执行循环 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_dependency_contract` |
| `paper_orders` | 按 execution_version 乐观锁改写 risk_payload | `paper_order_ledger::update_risk_payload` | paper_trading 日内卖出收尾 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 执行事件回写信号状态/原因（活动状态集保护） | `signal_service::advance_signal_feedback` | paper_trading 执行循环 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 信号写入语句（INSERT OR IGNORE / REFRESH） | `signal_service::conflict_statement` | signal_service 冲突策略 | `signal_service` | 调用方同一连接与原事务 | `test_signal_pipeline`、`test_strategy_selection_provenance` |
| `paper_signals` | 容量/等待：信号转 deferred_capacity（可刷新 payload） | `signal_service::defer_for_capacity` | paper_trading 入场与派发闸门 | `signal_service` | 调用方同一连接与原事务 | `test_execution_profiles`、`test_pr10_guard_negative` |
| `paper_signals` | 信号按业务原因直接失效 | `signal_service::expire_signal` | paper_trading 开盘窗口规则 | `signal_service` | 调用方同一连接与原事务 | `test_cycle_archive_writes`、`test_entry_lifecycle`、`test_ledger_ownership` |
| `paper_signals` | 按状态集合把信号写为 expired | `signal_service::expire_within` | paper_trading 入场新鲜度判定 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 成交完成：信号转 filled | `signal_service::mark_filled` | commit_fill 成交结果 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | Q3 影子观察：信号转 shadow_q3 | `signal_service::mark_shadow` | paper_trading 入场决策 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 成交/执行结果回写信号状态与原因（状态白名单） | `signal_service::record_signal_outcome` | commit_fill 成交结果 | `signal_service` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_dependency_contract` |
| `paper_signals` | 订单意图收紧：信号转 rejected | `signal_service::reject_signal` | paper_trading 意图规则 | `signal_service` | 调用方同一连接与原事务 | `test_execution_profiles`、`test_pr10_guard_negative`、`test_waitlist_dedupe_write` |
| `paper_signals` | 成交失败回到 pending | `signal_service::reopen_pending` | commit_fill 失败结果 | `signal_service` | 调用方同一连接与原事务 | `test_dependency_contract` |
| `paper_signals` | 当日引导：替代 blocked/可复评拒绝信号 | `signal_service::supersede_blocked_for_day` | paper_trading 引导规则 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 当日引导：90 分钟未成交的 pending 信号替代 | `signal_service::supersede_pending_for_day` | paper_trading 引导规则 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 按身份集合把信号转 superseded（固定后缀） | `signal_service::supersede_signals` | paper_trading 实时复核替代 | `signal_service` | 调用方同一连接与原事务 | `test_cycle_archive_writes`、`test_signal_supersede_set` |
| `paper_signals` | 执行反馈：状态/原因/payload 回写信号 | `signal_service::update_signal_feedback` | paper_trading 执行循环 | `signal_service` | 调用方同一连接与原事务 | 待补 |

### 工作包 C

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_accounts` | 账户现金增量调整（对账补偿） | `paper_account_ledger::apply_cash_delta` | paper_trading 现金对账口径 | `paper_account_ledger` | 调用方同一连接与原事务 | `test_paper_shared_cash` |
| `paper_accounts` | 共享现金扣减 | `paper_account_ledger::debit_cash` | paper_shared_cash 资金规则 | `paper_account_ledger` | 调用方同一连接与原事务 | `test_paper_shared_cash` |
| `paper_accounts` | 账户退出周期（cycle_id 清空 + 暂停 + 资金归零） | `paper_account_ledger::detach_cycle` | paper_trading 周期引导/创建 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 既有账户档案字段补齐（mode 为空才写默认） | `paper_account_ledger::ensure_profile` | 账号引导规则 | `paper_account_ledger` | 启动引导 / 调用方同一连接与原事务 | `test_paper_user_account_provisioning` |
| `paper_accounts` | 用户账户周期挂接对账 | `paper_account_ledger::initialize_user_cycle` | 账户挂接规则 | `paper_account_ledger` | 服务入口传入的连接 | `test_paper_user_cycle_attachment` |
| `paper_accounts` | 账户创建（内置/用户策略，列白名单） | `paper_account_ledger::insert_account` | 账号引导与 provisioning 规则 | `paper_account_ledger` | 启动引导 / 调用方同一连接与原事务 | `test_paper_shared_cash`、`test_paper_user_account_provisioning`、`test_paper_user_cycle_attachment` |
| `paper_accounts` | 共享风控冷却窗口写入 | `paper_account_ledger::set_cooldown` | paper_trading 风控状态 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 周期资金配置（初始资金/现金/基准净值） | `paper_account_ledger::set_cycle_capital` | 配置入口校验 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 账户启停状态（活动周期过滤） | `paper_account_ledger::set_status` | 启停入口校验 | `paper_account_ledger` | 调用方同一连接与原事务 | `test_learning_evaluation` |
| `paper_accounts` | 账户风格字段写入 | `paper_account_ledger::set_style` | 配置入口校验 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 新策略首次版本号写入 | `paper_account_ledger::set_version` | 账号引导规则 | `paper_account_ledger` | 启动引导 / 调用方同一连接与原事务 | `test_execution_evidence`、`test_execution_planner`、`test_order_cycle_provenance` |
| `paper_accounts` | 账户参数快照/版本/时间戳写入 | `paper_account_ledger::write_params` | adaptive/evolution/周度复盘各自的采纳与回滚判定 | `paper_account_ledger` | 调用方同一连接与原事务 | `test_paper_account_ledger` |

### 工作包 D

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_accounts` | 新策略跟随运行周期直接激活 | `paper_account_ledger::activate_for_running_cycle` | paper_trading 周期引导 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 周期创建时的账户成员配置（列白名单） | `paper_account_ledger::apply_cycle_membership` | 周期创建规则 | `paper_account_ledger` | 调用方同一连接与原事务 | `test_paper_account_ledger` |
| `paper_accounts` | 首次挂接活动周期（资金/基准/日初净值） | `paper_account_ledger::attach_to_cycle` | paper_trading 周期引导 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 已挂接账户的周期画像字段 | `paper_account_ledger::set_cycle_profile` | paper_trading 周期引导 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 回填日初净值与日期 | `paper_account_ledger::set_daily_start_nav` | paper_trading 周期引导 | `paper_account_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_cycles` | 周期归档 archived | `paper_cycle_service::archive_cycle` | 归档入口规则 | `paper_cycle_service` | 服务入口传入的连接 | `test_order_cycle_provenance`、`test_paper_cycle_ownership`、`test_paper_cycle_service` |
| `paper_cycles` | 周期创建（兼容引导 / 新建，列白名单） | `paper_cycle_service::insert_cycle` | 周期创建与引导规则 | `paper_cycle_service` | 调用方同一连接与原事务 | 待补 |
| `paper_cycles` | 周期资金对账写入 | `paper_cycle_service::set_capital` | paper_trading 引导/配置校验 | `paper_cycle_service` | 调用方同一连接与原事务 | 待补 |
| `paper_cycles` | 周期启停（started_at 保留首次） | `paper_cycle_service::set_status` | 启停入口校验 | `paper_cycle_service` | 调用方同一连接与原事务 | `test_learning_evaluation` |
| `paper_orders` | 归档清理：删除超龄订单 | `paper_order_ledger::archive_delete_orders` | paper_trading 归档策略 | `paper_order_ledger` | 归档入口的 immediate 事务 | `test_cycle_archive_writes` |
| `paper_orders` | 归档清理：超龄订单转 superseded | `paper_order_ledger::archive_supersede_orders` | paper_trading 归档策略 | `paper_order_ledger` | 归档入口的 immediate 事务 | `test_cycle_archive_writes` |
| `paper_orders` | 周期漂移订单终态化（已成交不改） | `paper_order_ledger::terminalize_cycle_drift_order` | manual_orders 周期归属判定 | `paper_order_ledger` | 归档入口的 immediate 事务 | `test_cycle_archive_writes`、`test_dependency_contract` |
| `paper_signals` | 归档清理：删除超龄信号 | `signal_service::archive_delete_signals` | paper_trading 归档策略 | `signal_service` | 归档入口的 immediate 事务 | `test_cycle_archive_writes` |
| `paper_signals` | 归档清理：超龄信号转 expired | `signal_service::archive_expire_signals` | paper_trading 归档策略 | `signal_service` | 归档入口的 immediate 事务 | `test_cycle_archive_writes` |
| `paper_signals` | 归档清理：超龄信号转 superseded | `signal_service::archive_supersede_signals` | paper_trading 归档策略 | `signal_service` | 归档入口的 immediate 事务 | `test_cycle_archive_writes` |

<!-- ledger-owner-plan:end -->

### 16.5 工作包 A 进展

**A 第一片（本 PR）**：建立 `backend/paper_order_ledger.py` 作为 `paper_orders` 唯一持久化入口，
先接非成交生命周期的写入：

| 原写入点 | 新入口 |
| --- | --- |
| `execution_dispatch.expire_order` | `POL.expire_order` |
| `execution_dispatch.supersede_for_expired_signal` | `POL.supersede_for_expired_signal` |
| `execution_dispatch.supersede_duplicate_execution_retry` | `POL.supersede_duplicate_execution_retry` |
| `execution_dispatch.supersede_for_terminal_signal` | `POL.supersede_for_terminal_signal`（状态词表由调用方传入） |
| `execution_dispatch._retire_for_retry` 的订单写 | `POL.supersede_order` |
| `execution_dispatch._terminate` 的订单写 | `POL.terminate_order`（终态白名单校验） |
| `execution_dispatch.retire_gated_orders` 的订单写 | `POL.retire_gated_order` |
| `execution_dispatch.retire_duplicate_waitlist` 的订单写 | `POL.retire_duplicate_waitlist_orders` |

`execution_dispatch` 至此**不再写 `paper_orders`**（守卫 `test_order_ledger` 用源码断言守住），
它只保留派发阶段的判定、顺序、预占释放与信号写入。信号写入的收口（`_retire_for_retry` /
`_terminate` / `retire_duplicate_waitlist` / `_write_signal_marker` → `signal_service`）是 A 的下一片。

**A 第二片**：`execution_dispatch` 的信号写入并入 `signal_service`，它不再写 `paper_signals`
（信号写方集合 4 → 3 个模块：`paper_trading` / `manual_orders` / `signal_service`）：

| 原写入点 | 新入口 |
| --- | --- |
| `_write_signal_marker` 的 payload 写 | `SIG.write_payload` |
| `_retire_for_retry` 的"回到复试"分支 | `SIG.reopen_for_retry` |
| `_retire_for_retry` 的"信号过期"分支 | `SIG.expire_after_order_retry` |
| `_terminate` 的信号终态写 | `SIG.terminate_for_dispatch`（rejected / expired 白名单） |
| `retire_duplicate_waitlist` 的信号写 | `SIG.retire_waitlist_signals`（身份由 `POL.signal_ids_by_reason_marker` 反查，读取留在调用方） |

依赖方向按既有约定处理：`execution_dispatch` **不 import** `signal_service`，由组装根经
`paper_slot_service.run_preflight` 传入 `signals` 端口（与 `release_reservation` 同一种注入方式），
因此没有新增跨域边；`paper_slot_service` 也不再 import 信号 owner。

**A 第三片**：最后三条订单写入归位 —— `_supersede_signal_execution_retries` →
`POL.supersede_previous_execution_retry`、`manual_orders.cancel_manual_order` →
`POL.cancel_order`（只动调用方给的可撤销状态，返回受影响行数供调用方保留"已成交"错误）、
`_record_entry_frozen_waitlist` 的 INSERT → `POL.insert_order`（列集合逐字保留，业务侧算好
策略戳/血缘/payload 再传入；B 包创建路径复用同一入口）。

`manual_orders` 不 import owner：`cancel_order` 声明进它的 `ManualOrderDependencies` 容器、
由组装根绑定（与 lot / provenance 能力同法），因此没有新增 other 域 → paper 域的边。

**A 包写入面到此收口**：`ledger_owner_plan.json` 里 A 的 19 条操作全部迁移完毕
（当前模块 == 目标 owner 的检查为 0 条待办）。`paper_orders` / `paper_signals` 仍保持
`pending` —— B（创建、成交结果、核验戳）与 D（归档联动）还有写入没搬，标签不能提前改。

**B 包写入面收口**：创建、成交结果、核验戳与信号执行反馈全部经 owner。清单里 `B` 的 24 条操作
全部迁移完毕（`当前模块 != 目标 owner` 的 B 条目为 0）。写方模块：

| 表 | 现在 | 说明 |
| --- | --- | --- |
| `paper_orders` | `paper_trading` / `manual_orders` / `paper_order_ledger` | 前两者只剩 D 包的归档与周期切换写入 |
| `paper_signals` | `paper_trading` / `signal_service` | `paper_trading` 只剩 D 包的归档清理 |

剩余额度（`cross_domain_edges` +1 来自 `execution_verification → paper_order_ledger`）已在
`docs/COUPLING_BUDGET.md` 登记；B 期间没有再新增其它额度。

成本登记：新增一个含 SQL 的 owner 模块 → `raw_sql_modules` 141→142、
`paper_table_fanout_max` 50→51、`cross_domain_edges` 629→630（`execution_dispatch → paper_order_ledger`），
均通过 `FEATURE_ALLOWANCE` 显式登记并写入 `docs/COUPLING_BUDGET.md`；`HEAD_BUDGET` 与 lock 不动，
B/C/D 搬走写入后必须收回。


### A 包复审修复（2026-11-02）

等待池信号恢复使用单条 UPDATE + 订单集合子查询；绑定参数仅随固定状态集合变化。保留先订单后信号、不提交、不吞错的边界。32 参数上限下 80 条历史订单（含重复信号）、终态/活动信号对照及 ABORT 回滚验证通过。相关回归 32 项通过。

### A 独立交付预算修复（2026-11-02）

恢复集合子查询使信号 owner 新增订单引用，A 单独交付的 paper_table_fanout_max 曾达 52（上限 51）。账户指标原有的非 filled 委托计数改为调用订单 owner 的窄只读接口 nonfilled_order_count；原 SELECT、状态过滤、缓存分支与事务边界不变，避免另一模块直接绑定表结构。实际模块引用降回 51，没有提高预算、改变域分类或新增跨域边。
### B 包复审修复（2026-11-02）

supersede_signals 恢复同账户/标的旧订单集合子查询，保留先信号后订单顺序。80 条重复订单在 32 参数上限下通过，ABORT 不留下部分信号更新。三处改写守卫均有拒绝违例的负向测试；相关行为回归通过。

### C 包复审修复（2026-11-02）

原字段分组并不互斥，不作为单写验收依据。现金/开户/周期/配置保留业务职责，账户写入交给 paper_account_ledger；当前仅剩 paper_trading 的周期初始化待 D 包转接，因此账户表仍 pending。配置恢复用独立未提供标记区分省略与显式 None，完整快照正常恢复，缺字段快照保留 IntegrityError。禁删/替换守卫使用正确词边界，SQL 提取包含 REPLACE，DELETE/REPLACE 和现金跨字段违例均验证会被拦截。

### D 包复审修复（2026-11-02）

周期初始化/挂接/画像/资金配置与成员重置均进入 paper_account_ledger。账户表真实写入模块集合只剩该模块，才将矩阵标为 owned；四组业务模块仍保留判断与编排。跨字段成员重置仍为单条 UPDATE，NOT NULL 失败不留部分修改、成功后由调用方回滚的行为有直接回归。父分支更新后的周期/归档等 67 项回归及预算通过。
## 17. 十张核心表的读取契约

本节把「谁提供权威读取、哪些是展示投影、哪些只是当前事务内的查询」落到具体模块。
守卫见 `backend/test_ledger_read_contracts.py`：契约与源码扫描**逐一对齐**。

### `paper_accounts`

- 写 owner：`paper_shared_cash`, `paper_user_cycle_attachment`, `paper_user_account_provisioning`, `paper_account_config`
- 权威读者：`paper_shared_cash`, `paper_user_account_provisioning`, `paper_user_cycle_attachment`
- 展示/派生投影：`dashboard_queries`, `deepseek_advisor`, `intraday_t_pair_report`, `main_force_trade_evidence`, `paper_portfolio_read_model`, `paper_position_read_model`, `strategy_performance`
- 事务内查询：`adaptive_engine`, `adaptive_risk`, `adaptive_selection`, `adaptive_selection_compat`, `ai_adoption_writer`, `api_adaptive`, `evolution_apply`, `evolution_validation`, `manual_orders`, `paper_cycle_capital`, `paper_cycle_ownership`, `paper_dynamic_allocation`, `paper_order_execution_authority`, `paper_risk_service`, `paper_trading`, `strategy_registry`
- cycle 作用域：是
- 缺失/未知处理：读路径缺账户行返回空，绝不现场建账
- 读取是否可能触发写入：读面板只读；创建只走 provisioning 入口

### `paper_audit`

- 写 owner：`paper_repository`
- 权威读者：`paper_repository`
- 展示/派生投影：`metrics`, `paper_risk_evidence`
- 事务内查询：`diagnose_cycle_lifecycle_conflict`, `paper_risk_service`, `paper_trading`
- cycle 作用域：否
- 缺失/未知处理：append-only，历史行不重写
- 读取是否可能触发写入：只读

### `paper_capital_reservations`

- 写 owner：`paper_capital_reservations`
- 权威读者：`paper_capital_reservations`
- 事务内查询：`execution_planner`, `paper_replay_regression`
- cycle 作用域：是
- 缺失/未知处理：order_key 按 CAST(... AS TEXT) 比较，兼容旧整数键
- 读取是否可能触发写入：只读

### `paper_cycles`

- 写 owner：`paper_cycle_service`
- 权威读者：`paper_cycle_ownership`
- 展示/派生投影：`dashboard_queries`, `paper_portfolio_read_model`, `paper_position_read_model`, `strategy_performance`, `tradability_position_evidence`
- 事务内查询：`diagnose_cycle_lifecycle_conflict`, `paper_dynamic_allocation`, `paper_order_execution_authority`, `paper_risk_scan_state`, `paper_trading`, `strategy_lifecycle`
- cycle 作用域：是
- 缺失/未知处理：无活动周期返回 unavailable，读路径不得补建周期
- 读取是否可能触发写入：load_active_cycle 会 ensure，不是只读接口

### `paper_fills`

- 写 owner：`execution_planner`
- 权威读者：`execution_planner`
- 展示/派生投影：`dashboard_queries`, `deepseek_advisor`, `execution_evidence`, `notify_wxpusher`, `paper_account_metrics`, `paper_portfolio_read_model`, `strategy_health_service`, `strategy_performance`, `tradability_position_evidence`
- 事务内查询：`adaptive_engine`, `execution_verification`, `paper_replay_regression`, `paper_repository`, `paper_trading`, `trade_attribution`
- cycle 作用域：是
- 缺失/未知处理：成交归属经 order 的 cycle_id 解析，未知保持未知
- 读取是否可能触发写入：只读

### `paper_orders`

- 写 owner：`paper_order_ledger`
- 权威读者：`paper_order_ledger`
- 展示/派生投影：`dashboard_queries`, `deepseek_advisor`, `intraday_t_pair_report`, `main_force_trade_evidence`, `paper_account_metrics`, `paper_portfolio_read_model`, `paper_position_read_model`, `paper_position_review_evidence`, `paper_risk_evidence`, `shadow_comparison_service`, `strategy_health_service`, `strategy_performance`, `tradability_position_evidence`
- 事务内查询：`adaptive_engine`, `adaptive_risk`, `adaptive_selection`, `entry_lifecycle`, `execution_dispatch`, `execution_planner`, `execution_verification`, `manual_orders`, `news_learning`, `paper_cycle_service`, `paper_order_execution_authority`, `paper_replay_regression`, `paper_repository`, `paper_risk_service`, `paper_slot_occupancy`, `paper_trading`, `portfolio_order_intents`, `portfolio_workspace_service`, `rebalance_scanner`, `trade_attribution`
- cycle 作用域：是
- 缺失/未知处理：legacy 行的 cycle_id 保持 NULL，读侧不得用当前周期补写
- 读取是否可能触发写入：只读接口不得写入；stamp_* 与 init 修复属于写路径

### `paper_position_lots`

- 写 owner：`paper_lot_ledger`
- 权威读者：`paper_lot_ledger`
- 展示/派生投影：`paper_portfolio_read_model`, `paper_position_read_model`, `tradability_position_evidence`
- 事务内查询：`diagnose_cycle_lifecycle_conflict`, `execution_planner`, `manual_orders`, `paper_position_risk_state`, `paper_replay_regression`, `paper_risk_exit_eligibility`, `paper_trading`
- cycle 作用域：是
- 缺失/未知处理：source_order_id 为 NULL 的 durable lot 未平仓量记 unknown
- 读取是否可能触发写入：只读

### `paper_positions`

- 写 owner：`paper_lot_ledger`
- 权威读者：`paper_lot_ledger`
- 展示/派生投影：`deepseek_advisor`
- 事务内查询：`news_learning`, `paper_replay_regression`, `paper_trading`
- cycle 作用域：是
- 缺失/未知处理：派生投影；权威数量在 position_lots
- 读取是否可能触发写入：只读

### `paper_risk_decisions`

- 写 owner：`paper_risk_writes`
- 权威读者：`paper_risk_evidence`
- 展示/派生投影：`shadow_comparison_service`, `strategy_health_service`
- 事务内查询：`adaptive_engine`, `adaptive_risk`, `paper_trading`
- cycle 作用域：否
- 缺失/未知处理：authority 词汇与结构由 schema owner 定义
- 读取是否可能触发写入：只读

### `paper_signals`

- 写 owner：`signal_service`
- 权威读者：`signal_service`
- 展示/派生投影：`dashboard_queries`, `paper_replacement_evidence`, `selection_alpha_report`, `shadow_comparison_service`
- 事务内查询：`adaptive_engine`, `entry_lifecycle`, `execution_dispatch`, `execution_planner`, `news_learning`, `paper_risk_service`, `paper_trading`, `rebalance_scanner`, `strategy_selection_resolver`
- cycle 作用域：否
- 缺失/未知处理：created_at 缺失/非法时 signal_freshness 判不可用（fail-closed）
- 读取是否可能触发写入：load_active_cycle 之外的读路径不得 ensure

### 动态拼接写入登记

静态扫描看不到 f-string / 拼接出来的 SQL，因此逐条登记（守卫比对集合相等）：

| 写入口 | 目标表 | 为什么是动态 |
| --- | --- | --- |
| `paper_account_config::write_params` | `paper_accounts` | SET 子句按 version/now 是否给出拼装 |
| `paper_cycle_service::insert_cycle` | `paper_cycles` | 列集合由调用方给（白名单校验） |
| `paper_order_ledger::cancel_order` | `paper_orders` | 可撤销状态集由调用方传入，占位符数量随之变化 |
| `paper_order_ledger::insert_order` | `paper_orders` | 列集合由调用方给（白名单校验），占位符数量随列数变化 |
| `paper_order_ledger::retire_gated_order` | `paper_orders` | 挂起状态集由调用方传入 |
| `paper_order_ledger::supersede_for_terminal_signal` | `paper_orders` | 终态词表由调用方传入 |
| `paper_order_ledger::supersede_older_intents` | `paper_orders` | 状态集合由调用方传入 |
| `paper_order_ledger::update_order_outcome` | `paper_orders` | SET 子句按调用方给出的字段组合拼装 |
| `paper_user_account_provisioning::insert_account` | `paper_accounts` | 列集合由调用方给（白名单校验） |
| `paper_user_cycle_attachment::apply_cycle_membership` | `paper_accounts` | 列集合由调用方给（白名单校验） |
| `paper_user_cycle_attachment::set_status` | `paper_accounts` | 过滤条件由调用方给出（活动周期 clause） |
| `signal_service::advance_signal_feedback` | `paper_signals` | 活动状态集合由调用方传入 |
| `signal_service::conflict_statement` | `paper_signals` | 冲突语句按 upsert 列集拼装（既有实现） |
| `signal_service::expire_after_order_retry` | `paper_signals` | 终态集合由调用方传入 |
| `signal_service::expire_within` | `paper_signals` | 状态集合由调用方传入 |
| `signal_service::reopen_for_retry` | `paper_signals` | 终态集合由调用方传入 |
| `signal_service::retire_waitlist_signals` | `paper_signals` | 身份集合由调用方传入 |
| `signal_service::supersede_signals` | `paper_signals` | 身份与状态集合由调用方传入 |

## 18. PR-10 收口报告（复审后更正）

**结论：交付完成、验收未通过（等待本轮删边复审确认）。** 五包保留 Draft，不开始合并，不进入 PR-11。

A/B 已恢复数据库内集合式信号更新，参数数量不随历史记录增长；C/D 已将账户表
持久化统一到 `paper_account_ledger`，四组业务职责保留，跨字段更新不拆事务。
显式 NULL 保留数据库约束错误，开户禁删/替换守卫及 SQL 提取已修复。

68 个原始稳定操作 ID 均有迁移接口、调用位置、事务/异常边界及关联回归模块，
当前 70 个 writer 接口均能反查。原冻结是 57 个函数组/68 个按表操作；71 是旧
最终树 writer 函数组数，旧报告的 66 汇总撤回。该映射不是运行时所有分支等价的单独证明。

十张核心表目前各有一个真实运行时写方。跨域 import 边 629→636，新增七条属于
已审核设计成本；FEATURE_ALLOWANCE 增长提高有效预算上限。E 本轮复用既有账户指标读取
返回值，删除一条重复成交表 SQL 依赖，成交表真实读者 16→15、全部引用 18→17，
十表总引用 208→207。该方向性下降有源码与行为证据，等待复审确认；
不修改原验收定义，不以 writer 收口或 CI 通过代替该证据，也不自行宣布合并放行。

逐表前后对照、准确计数、原始映射与重现方法见 [PR10_REVIEW_EVIDENCE.md](PR10_REVIEW_EVIDENCE.md)。
本轮未连接服务器、未部署，未修改生产账本。

MERGE: NOT MERGED；DEPLOY: NOT DEPLOYED。
