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
| `paper_orders` | **pending** | `paper_trading`、`manual_orders`、`execution_planner`、`execution_dispatch`、`execution_verification`、`paper_risk_writes` | 第八批后生命周期层只判断/编排，过期与失效回收写入已移交；第九批移交等待池重复记录回收，第十批移交对账路径的两条回收写入，其他阶段仍分写 |
| `paper_signals` | **pending** | `paper_trading`、`manual_orders`、`execution_dispatch`、`signal_service` | 第八批后生命周期层不再直写；第九批把等待池回收的信号写并入同一 owner；第十批把对账的信号状态写入归 `signal_service`，其他阶段仍分写 |
| `paper_accounts` | **pending** | `paper_trading`、`paper_shared_cash`、`paper_user_cycle_attachment`、`paper_user_account_provisioning`、`strategy_registry`、`paper_order_execution_authority`、`paper_cycle_ownership`、`adaptive_engine`、`adaptive_risk`、`adaptive_selection`、`adaptive_selection_compat`、`evolution_apply`、`ai_adoption_writer` | 不同 owner 各写自己的字段（现金/周期挂接/策略参数） |
| `paper_cycles` | **pending** | `paper_trading`、`paper_cycle_service` | 第七批补核查：创建/配置/启停及兼容补齐 vs 周期归档，按阶段登记，不据此认定多写缺陷 |

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
| `paper_orders` | `paper_order_ledger`（A 包新建） | planned |
| `paper_signals` | `signal_service` | existing |
| `paper_accounts`（现金） | `paper_shared_cash` | existing |
| `paper_accounts`（创建） | `paper_user_account_provisioning` | existing |
| `paper_accounts`（cycle 身份/启停） | `paper_user_cycle_attachment` | existing |
| `paper_accounts`（参数/版本/限额/冷却） | `paper_account_config`（C 包新建） | planned |
| `paper_cycles` | `paper_cycle_service` | existing |

`paper_order_ledger` 只承担**订单行持久化**：创建 INSERT、状态迁移、成交结果字段、
核验戳、归档删除。成交判定仍唯一属于 `commit_fill`；`paper_order_ledger` 不允许
自己算成交价、费用、可卖量或 BUY 数量，也不 commit。

账户不接受"任意字段字典"接口：现金、身份、配置三组字段分别归三个 owner，
`paper_accounts` 不会因为收口而变成一个大 Manager。

### 16.3 工作包与出口

| 包 | 范围 | 出口证据 |
| --- | --- | --- |
| **A** | 订单/信号非成交生命周期：重试接管、等待池替代、手动取消/终止、派发等待与回收 | 调用方不再自行拼订单/信号状态 UPDATE；退役范围、原因、时钟、预占调用、统计与基线逐项对照 |
| **B** | 订单创建、成交结果、核验戳与信号执行反馈 | 保留 `commit_fill` 唯一成交权威；自动/手动/延期/部分成交/核验/失败路径投影一致 |
| **C** | `paper_accounts` 四组：创建补齐、现金、身份挂接、配置补偿 | 每组字段有唯一入口；现金算法与版本检查不变；13 个写方逐项归类完毕 |
| **D** | `paper_cycles` 完整生命周期与跨表归档事务 | 创建/配置/启停/归档/恢复都有 owner；归档不删历史事实、不覆盖未知归属 |
| **E** | 十张核心表读取契约、守卫校准、跨包最终回归与关闭报告 | 逐表权威读/投影/允许调用方/事务归属成文；关键负向测试与完整 CI 通过 |

A–D 完成、且守卫与源码一致之后，四张表才允许从 `pending` 改 `owned`；只改标签不算完成。
E 不是新一轮重构：不要求清空全仓直接 SELECT、不设行数目标、不建通用数据库审计框架。

### 16.4 剩余写入清单（自动生成）

以下表格由 `python backend/tools/ledger_write_inventory.py --markdown` 生成，
`test_ledger_owner_plan.py` 会比对"源码扫描结果 ↔ 计划 ↔ 本节内容"三者一致。

<!-- ledger-owner-plan:start -->
### 工作包 A

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_orders` | 重试接管：退役旧执行重试 | `execution_dispatch::_retire_for_retry` | execution_dispatch 重试策略 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 重试接管：信号状态同步 | `execution_dispatch::_retire_for_retry` | execution_dispatch 重试策略 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 派发终止：订单转终态 | `execution_dispatch::_terminate` | execution_dispatch 派发阶段 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_position_risk_state` |
| `paper_signals` | 派发终止：信号终态 | `execution_dispatch::_terminate` | execution_dispatch 派发阶段 | `signal_service` | 调用方同一连接与原事务 | `test_position_risk_state` |
| `paper_signals` | 信号 payload 标记写入 | `execution_dispatch::_write_signal_marker` | execution_dispatch 批次/释放标记 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 订单自身过期终态 expired | `execution_dispatch::expire_order` | entry_lifecycle 判定，dispatch 落库 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_order_ttl_lifecycle` |
| `paper_orders` | 等待池重复记录退役（订单） | `execution_dispatch::retire_duplicate_waitlist` | paper_trading 启动恢复 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_ledger_owner_plan`、`test_ledger_ownership`、`test_waitlist_dedupe_write` |
| `paper_signals` | 等待池重复记录退役（信号） | `execution_dispatch::retire_duplicate_waitlist` | paper_trading 启动恢复 | `signal_service` | 启动恢复/初始化连接 | `test_ledger_owner_plan`、`test_ledger_ownership`、`test_waitlist_dedupe_write` |
| `paper_orders` | 挂起订单退役（batch/verification 等待超时） | `execution_dispatch::retire_gated_orders` | execution_dispatch 等待窗口 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_execution_dispatch` |
| `paper_orders` | 重复执行重试退旧 superseded | `execution_dispatch::supersede_duplicate_execution_retry` | paper_trading 对账循环 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_ledger_ownership`、`test_signal_order_reconcile_write` |
| `paper_orders` | 失效信号名下买单回收 superseded | `execution_dispatch::supersede_for_expired_signal` | entry_lifecycle 失效判定 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_paper_slot_service` |
| `paper_orders` | 终态信号遗留委托回收 | `execution_dispatch::supersede_for_terminal_signal` | paper_trading 对账循环 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_ledger_ownership`、`test_signal_order_reconcile_write` |
| `paper_orders` | 手动撤单 cancelled | `manual_orders::cancel_manual_order` | 用户请求 + 手动入口校验 | `paper_order_ledger` | 服务入口传入的连接 | `test_adaptive_dependency_boundary`、`test_learning_dataset`、`test_learning_evaluation` |
| `paper_orders` | 冻结等待池标记订单创建（INSERT） | `paper_trading::_record_entry_frozen_waitlist` | paper_trading 入场闸门 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_order_cycle_provenance` |
| `paper_orders` | 新一轮执行重试接管：退役全部旧重试 | `paper_trading::_supersede_signal_execution_retries` | paper_trading 重试发起 | `paper_order_ledger` | 调用方同一连接与原事务 | 待补 |
| `paper_signals` | 订单过期联动信号 expired | `signal_service::expire_for_expired_order` | entry_lifecycle 过期判定 | `signal_service` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_order_ttl_lifecycle` |
| `paper_signals` | 信号自身失效 expired | `signal_service::expire_stale_signal` | entry_lifecycle 失效判定 | `signal_service` | 调用方同一连接与原事务 | `test_entry_lifecycle`、`test_ledger_ownership`、`test_paper_slot_service` |
| `paper_signals` | 信号状态对账 | `signal_service::reconcile_signal_status` | paper_trading 对账循环 | `signal_service` | 启动恢复/初始化连接 | `test_ledger_owner_plan`、`test_ledger_ownership`、`test_signal_order_reconcile_write` |

### 工作包 B

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_orders` | 成交结果写入（状态/成交价/费用/证据） | `execution_planner::commit_fill` | commit_fill 唯一成交权威 | `paper_order_ledger` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_deferred_fill_cycle_binding`、`test_execution_planner`、`test_execution_verification` |
| `paper_orders` | 历史订单核验戳补齐（维护） | `execution_verification::backfill_legacy_orders` | execution_verification 兼容规则 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_execution_verification`、`test_tradability_position_shadow_architecture_guard` |
| `paper_orders` | 执行核验戳写入 | `execution_verification::stamp_order` | execution_verification 核验结论 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_execution_verification`、`test_execution_verification_wiring`、`test_paper_trading_architecture_guard` |
| `paper_orders` | 零成交终态订单核验戳 | `execution_verification::stamp_terminal_zero_orders` | execution_verification 核验结论 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_portfolio_read_model` |
| `paper_orders` | 策略买入订单创建（INSERT） | `manual_orders::_commit_strategy_buy` | manual_orders 策略入口 | `paper_order_ledger` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_adaptive_dependency_boundary`、`test_execution_planner`、`test_learning_dataset` |
| `paper_signals` | 策略成交联动信号状态 | `manual_orders::commit_strategy_entry_fill` | commit_fill 成交结果 | `signal_service` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_paper_trading_architecture_guard` |
| `paper_orders` | 手动委托执行状态推进 | `manual_orders::process_pending_manual_orders` | manual_orders 执行循环 | `paper_order_ledger` | 服务入口传入的连接 | `test_deferred_fill_cycle_binding`、`test_paper_timing`、`test_paper_trading_architecture_guard` |
| `paper_orders` | 策略成交失败回写（订单侧） | `manual_orders::strategy_fill_failure` | commit_fill 失败结果 | `paper_order_ledger` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_paper_trading_architecture_guard`、`test_strategy_buy_commit_convergence` |
| `paper_signals` | 成交失败：信号回 deferred_capacity | `manual_orders::strategy_fill_failure` | commit_fill 失败结果 | `signal_service` | commit_fill 的 SAVEPOINT 内，调用方提交/回滚 | `test_paper_trading_architecture_guard`、`test_strategy_buy_commit_convergence` |
| `paper_orders` | 手动下单订单创建（INSERT） | `manual_orders::submit_manual_order` | 手动入口校验 + 风险决定 | `paper_order_ledger` | 服务入口传入的连接 | `test_adaptive_dependency_boundary`、`test_learning_dataset`、`test_learning_evaluation` |
| `paper_orders` | 风险拒绝订单创建（INSERT） | `paper_risk_writes::<module>` | 风控拒绝结论（paper_risk_writes 模块级工厂） | `paper_order_ledger` | 调用方同一连接与原事务 | `test_activity_overview_slim`、`test_adaptive_dependency_boundary`、`test_adaptive_experiment_evidence_ownership` |
| `paper_signals` | 当日信号引导：替代旧信号 | `paper_trading::_bootstrap_signals_for_today` | paper_trading 引导规则 | `signal_service` | 启动恢复/初始化连接 | `test_paper_timing`、`test_paper_trading_architecture_guard`、`test_provenance_inflight_change` |
| `paper_orders` | 自动买入订单创建（INSERT） | `paper_trading::_buy_order` | paper_trading 入场决策 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_activity_overview_slim`、`test_ai_advice_service`、`test_deferred_fill_cycle_binding` |
| `paper_signals` | 买入联动信号状态/原因/payload | `paper_trading::_buy_order` | paper_trading 入场决策 | `signal_service` | 调用方同一连接与原事务 | `test_activity_overview_slim`、`test_ai_advice_service`、`test_deferred_fill_cycle_binding` |
| `paper_signals` | 订单意图收紧：信号 rejected | `paper_trading::_enforce_order_intent` | paper_trading 意图规则 | `signal_service` | 调用方同一连接与原事务 | `test_order_intent_contract`、`test_paper_trading_architecture_guard` |
| `paper_orders` | 卖出订单创建（INSERT） | `paper_trading::_intraday_sell` | paper_trading 风控/止盈决策 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_ai_advice_service`、`test_execution_verification_wiring`、`test_paper_trading_architecture_guard` |
| `paper_orders` | 已实现盈亏回填 realized_pnl | `paper_trading::_rebuild_realized_pnl` | paper_trading 重建口径 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_execution_verification` |
| `paper_signals` | 开盘信号过期（expired） | `paper_trading::execute_open` | paper_trading 开盘窗口规则 | `signal_service` | 调用方同一连接与原事务 | `test_adaptive_dependency_boundary`、`test_learning_dataset`、`test_learning_evaluation` |
| `paper_signals` | 快速入场等待：信号 deferred_capacity | `paper_trading::monitor_fast_entries` | paper_trading 盘中复核 | `signal_service` | 调用方同一连接与原事务 | 待补 |
| `paper_orders` | 执行结果状态推进（含 execution_version 乐观锁） | `paper_trading::process_pending_execution_orders` | paper_trading 执行循环 | `paper_order_ledger` | 调用方同一连接与原事务 | `test_open_execution_risk_reuse`、`test_paper_timing`、`test_production_path_golden_replay` |
| `paper_signals` | 执行结果回写信号状态 | `paper_trading::process_pending_execution_orders` | paper_trading 执行循环 | `signal_service` | 调用方同一连接与原事务 | `test_open_execution_risk_reuse`、`test_paper_timing`、`test_production_path_golden_replay` |
| `paper_signals` | 信号写入语句（INSERT OR IGNORE / REFRESH） | `signal_service::conflict_statement` | signal_service 冲突策略 | `signal_service` | 调用方同一连接与原事务 | `test_signal_pipeline`、`test_strategy_selection_provenance` |

### 工作包 C

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_accounts` | 参数回滚恢复（params/version） | `adaptive_engine::_restore_paper_account` | adaptive_engine 回滚规则 | `paper_account_config` | 调用方同一连接与原事务 | `test_paper_repository` |
| `paper_accounts` | 风险自适应参数回滚收尾 | `adaptive_risk::_finish_rollback` | adaptive_risk 回滚规则 | `paper_account_config` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 风险自适应候选参数应用 | `adaptive_risk::apply_candidate` | adaptive_risk 采纳规则 | `paper_account_config` | 调用方同一连接与原事务 | `test_adaptive_experiment_evidence_ownership`、`test_ai_adoption_writer`、`test_evolution_reward_attribution` |
| `paper_accounts` | 选择自适应参数回滚收尾 | `adaptive_selection::_finish_rollback` | adaptive_selection 回滚规则 | `paper_account_config` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 选择自适应候选参数应用 | `adaptive_selection::apply_candidate` | adaptive_selection 采纳规则 | `paper_account_config` | 调用方同一连接与原事务 | `test_adaptive_experiment_evidence_ownership`、`test_ai_adoption_writer`、`test_evolution_reward_attribution` |
| `paper_accounts` | legacy 参数单位迁移 | `adaptive_selection_compat::migrate_legacy_selection_units` | 兼容迁移规则 | `paper_account_config` | 服务入口传入的连接 | `test_legacy_factor_unit_migration` |
| `paper_accounts` | 进化结果参数写入 | `evolution_apply::write_account_params` | evolution_apply 版本检查 | `paper_account_config` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 共享现金回补 | `paper_shared_cash::credit_shared_cash` | paper_shared_cash 资金规则 | `paper_shared_cash` | 调用方同一连接与原事务 | `test_deferred_fill_cycle_binding`、`test_execution_planner`、`test_execution_verification` |
| `paper_accounts` | 共享现金扣减 | `paper_shared_cash::debit_shared_cash` | paper_shared_cash 资金规则 | `paper_shared_cash` | 调用方同一连接与原事务 | `test_deferred_fill_cycle_binding`、`test_execution_planner`、`test_execution_verification` |
| `paper_accounts` | 内置账户创建与字段补齐 | `paper_trading::_ensure_accounts` | paper_trading 账户引导 | `paper_user_account_provisioning` | 启动恢复/初始化连接 | `test_execution_verification_probe`、`test_paper_user_account_provisioning` |
| `paper_accounts` | legacy 引导：账户 cycle_id 清空与暂停 | `paper_trading::_ensure_cycle` | paper_trading 兼容引导 | `paper_user_cycle_attachment` | 启动恢复/初始化连接 | `test_dashboard_refresh_semantics`、`test_deferred_fill_cycle_binding`、`test_legacy_position_rematerialization` |
| `paper_accounts` | 共享现金对账调整 | `paper_trading::_reconcile_shared_cash` | paper_trading 现金对账口径 | `paper_shared_cash` | 调用方同一连接与原事务 | `test_paper_user_cycle_attachment`、`test_shared_pool_late_join` |
| `paper_accounts` | 账户冷却窗口 cooldown_until | `paper_trading::_shared_risk_state` | paper_trading 风控状态 | `paper_account_config` | 调用方同一连接与原事务 | `test_cycle_funds`、`test_r32e1_active_buy_provenance`、`test_strategy_buy_commit_convergence` |
| `paper_accounts` | 周度参数应用（params/version） | `paper_trading::_weekly_review` | 周度复盘结论 | `paper_account_config` | 调用方同一连接与原事务 | `test_paper_schedule_contract` |
| `paper_accounts` | 周期资金配置（账户侧现金/基准净值） | `paper_trading::configure_capital` | 配置入口校验 | `paper_shared_cash` | 调用方同一连接与原事务 | `test_cycle_ledger_ownership`、`test_paper_cycle_ownership`、`test_strategy_roster` |
| `paper_accounts` | 新建周期时账户挂接重置 | `paper_trading::create_cycle` | 周期创建入口校验 | `paper_user_cycle_attachment` | 调用方同一连接与原事务 | `test_deferred_fill_cycle_binding`、`test_demo_seed`、`test_order_intent_contract` |
| `paper_accounts` | 账户风格字段 | `paper_trading::set_account_style` | 配置入口校验 | `paper_account_config` | 调用方同一连接与原事务 | 待补 |
| `paper_accounts` | 账户启停状态字段 | `paper_trading::set_accounts_status` | 启停入口校验 | `paper_user_cycle_attachment` | 调用方同一连接与原事务 | `test_cycle_ledger_ownership`、`test_paper_cycle_ownership`、`test_resume_three_strategy_cycle` |
| `paper_accounts` | 用户策略账户创建（INSERT） | `paper_user_account_provisioning::provision_user_accounts` | provisioning 规则 | `paper_user_account_provisioning` | 服务入口传入的连接 | `test_paper_user_account_provisioning` |
| `paper_accounts` | 用户账户周期挂接对账 | `paper_user_cycle_attachment::reconcile_user_cycle_accounts` | 账户挂接规则 | `paper_user_cycle_attachment` | 服务入口传入的连接 | `test_paper_user_cycle_attachment` |

### 工作包 D

| 表 | 业务操作 | 现有写入点 | 业务判定者 | 最终写入 owner | 调用方事务 | 相关测试 |
| --- | --- | --- | --- | --- | --- | --- |
| `paper_orders` | 周期切换时终止跨周期残留委托 | `manual_orders::_terminalize_cycle_stale_order` | manual_orders 周期归属判定 | `paper_order_ledger` | 服务入口传入的连接 | `test_entry_capital_asof`、`test_paper_trading_architecture_guard` |
| `paper_cycles` | 周期归档 archived | `paper_cycle_service::archive_cycle` | 归档入口规则 | `paper_cycle_service` | 服务入口传入的连接 | `test_order_cycle_provenance`、`test_paper_cycle_ownership`、`test_paper_cycle_service` |
| `paper_orders` | 归档/清理超龄订单行（DELETE） | `paper_trading::_cleanup_stale_data` | paper_trading 归档策略 | `paper_order_ledger` | 启动恢复/初始化连接 | `test_paper_timing`、`test_paper_trading_architecture_guard`、`test_position_review_provenance` |
| `paper_signals` | 归档/清理超龄信号行（DELETE） | `paper_trading::_cleanup_stale_data` | paper_trading 归档策略 | `signal_service` | 启动恢复/初始化连接 | `test_paper_timing`、`test_paper_trading_architecture_guard`、`test_position_review_provenance` |
| `paper_cycles` | legacy 周期引导创建（INSERT） | `paper_trading::_ensure_cycle` | paper_trading 兼容引导 | `paper_cycle_service` | 启动恢复/初始化连接 | `test_dashboard_refresh_semantics`、`test_deferred_fill_cycle_binding`、`test_legacy_position_rematerialization` |
| `paper_cycles` | 周期资金配置（capital） | `paper_trading::configure_capital` | 配置入口校验 | `paper_cycle_service` | 调用方同一连接与原事务 | `test_cycle_ledger_ownership`、`test_paper_cycle_ownership`、`test_strategy_roster` |
| `paper_cycles` | 周期创建（INSERT） | `paper_trading::create_cycle` | 周期创建入口校验 | `paper_cycle_service` | 调用方同一连接与原事务 | `test_deferred_fill_cycle_binding`、`test_demo_seed`、`test_order_intent_contract` |
| `paper_cycles` | 周期启停状态/started_at | `paper_trading::set_accounts_status` | 启停入口校验 | `paper_cycle_service` | 调用方同一连接与原事务 | `test_cycle_ledger_ownership`、`test_paper_cycle_ownership`、`test_resume_three_strategy_cycle` |
<!-- ledger-owner-plan:end -->
