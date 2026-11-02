# PR-11 计划：调度 worker 分层与资源隔离

**基线**：`c78cc63`（master，PR-10 五包 A–E 合并后的第一个提交）
**冻结日期**：2026-11-02
**来源**：`模拟盘升级路线图.docx`（2026-10-30，基线 `3c6d1a6`）§七「PR-11：调度 worker 分层与资源隔离」
与 §六 P2「调度独立 worker」
**度量入口**：`backend/tools/coupling_metrics.py`；门禁 `backend/test_architecture_boundary_audit.py`

路线图原文的目标与验收：

> 目标：让风险快通道真正接近每 3 分钟执行，同时候选全市场扫描不阻塞风险。
> 实施：risk-fast、execution-resume、candidate-full、dashboard-refresh 分离；为 worker 明确
> CPU/memory、独立锁、任务 lease、SQLite busy/backoff；按资源容量设置并发，**不直接删除现有全局锁**。
> 验收：risk-fast 的实际 start-to-start interval 达到目标；candidate-full 运行时风险任务仍能按时执行；
> 无重复成交、cycle 越权、SQLite 长事务或 OOM；队列年龄和跳过次数可观测。

## 1. 前置条件核对（在 `c78cc63` 上实测，不是推断）

路线图要求"PR-1 至 PR-6 的风险/执行边界和持久任务基础稳定"才进入 PR-11。逐条对 master：

| 前置 | 路线图任务 | master 上的落地 | 状态 |
| --- | --- | --- | --- |
| PR-1 | 风险扫描优先 + 已批准委托恢复前置 | `c321e39` / #298 | 已并入 |
| PR-2 | Dashboard 快照刷新持久任务化 | `6d1f9df` / #299 | 已并入 |
| PR-3 | 风险输入按消费者收窄 | `4ee5bf0` / #300 | 已并入 |
| PR-4 | 风险服务接入 RoundKlineCache | `32f2a99` / #301 | 已并入 |
| PR-5 | 风险 SQL 重复调用治理 | `acaa797` / #302 | 已并入 |
| PR-6 | Slot bootstrap 与 init_db 生命周期拆分 | `a95cff9` / #303 | 已并入 |
| PR-7 | 候选公共事实层收敛 | `e622955` / #304 | 已并入 |
| PR-8 | 自动/手动 sizing 公共化 | `a042c2b` / #305 | 已并入 |
| PR-9 | 依赖桥按 owner 簇收敛 | `7c4bd3b` / #290（私有访问 201→6） | 已并入 |
| PR-10 | 核心账本表逐表 read/write owner 化 | #317–#323（A–E 五包） | 已并入 |

结论：**可以进入 PR-11**。

## 2. 当前调度事实（读 `deploy/astock-codex.cron` 与 `paper_schedule_contract.py`）

同一份契约有两个渲染：`PSch.SCHEDULE_TASKS` / `render_cron_lines()` ↔ `deploy/astock-codex.cron`，
由 `test_paper_schedule_contract.CronRenderingFidelityGuard` 校验。

| 时刻 | 任务 | slot | 锁 |
| --- | --- | --- | --- |
| 09:15–09:24 每 3 分钟 | 竞价观测 | `auction-track` | `auction.lock`（`flock -n`） |
| 09:25 / 09:27 / 09:29 | 竞价预选（失败重试） | `auction` | `auction.lock` |
| 09:31 | 开盘审批 | `open` | 与 `auction` 共用 `paper-scan.lock` |
| 09:33–11:24、13:00–14:57 每 3 分钟 | 日内监控（**含风控**） | `intraday` | `intraday.lock`（`flock -w 60`） |
| 15:05 | 盘后评分 | `close` | — |
| 15:20（周五） | 周度复盘 | `weekly-review` | — |

关键事实：

1. **`risk` 是合法 slot、也在 `HOT_PATH_SLOTS` 里，但在 P11-B 之前没有任何独立 cron 条目**
   （当时 `SCHEDULED_SLOTS` 明确记录了这个决定，cron 里也有对应注释）。风控原来跑在
   `intraday` 里，与全市场刷新、候选生成、执行共用一条 3 分钟节拍。这就是 PR-11 要拆的对象。
   P11-B 已把它搬上独立条目与独立锁，见下方「P11-B 实际落地」。
2. `intraday` 用 `flock -w 60`：上一轮超时就等 60 秒，等不到就整轮放弃。
   这是"跑不完就丢一轮"的现状，也是"风险快通道"无法保证的根因。
3. `open` 与 `auction` 共用 `paper-scan.lock`；`intraday` 有独立锁。
   **PR-11 不删除任何现有锁**，只按 lane 增加独立锁。
4. 路线图 §2.3 在旧基线测得 `intraday_scan` 约 644–678 秒、`risk_snapshot` 约 298–309 秒。
   这是 `3c6d1a6` 时代的数字，**不能当作 PR-11 的 before**；PR-11 的 before 必须自己量（见 P11-A）。

## 3. 现有观测能力与缺口

已有：

- `paper_timing`：每个 slot 一次结束日志，含 `stages`（`seconds` / `thread_cpu_seconds` /
  `calls` / `errors`）、`elapsed_seconds`、`runtime_commit`、`host_paper_lock_wait_seconds`；
- `paper_job_lifecycle` / `paper_job_runs`：持久任务的 lease、heartbeat、失败重试与完成时间；
- `test_paper_scheduler_priority`：`flock` 争用语义的进程级验证。

缺（正是路线图 §0 与 PR-11 验收要求、当前**没有**的）：

| 指标 | 现状 |
| --- | --- |
| `scheduled tick`（cron 真正触发了几次） | 只在 `scheduler.log` 里有文本，无结构化计数 |
| `runner started` / `lock skipped` / `in_progress` | 无结构化记录，只能从日志文本推断 |
| `queue age`（进入 runner 前的等待） | 只有 `host_paper_lock_wait_seconds`，不含外层 cron 锁与排队 |
| `last successful risk scan` | 无独立时间戳可查（风控藏在 intraday 里） |
| lane 级别的 start-to-start interval | 无 |

没有这些，"risk-fast 的实际 start-to-start interval 达到目标"无法验收。因此 **P11-A 先补观测，
不改交易行为**——这也正是路线图"阶段 0"对新基线的要求。

### 3.1 P11-A 落地的观测契约（#324 复审后修正）

事件由两个地方产出，聚合器只看这两类真事件，不猜格式：

| 事件 | 产出者 | 关键字段 | 含义 |
| --- | --- | --- | --- |
| `paper_host_lock` | `deploy/run_paper_slot.sh` | `status`（`not_acquired` / `yielded_to_full_scan`）、`slot`、`wait_seconds` | 这一轮**没有进入 runner** |
| `paper_slot_start` | `deploy/run_paper_slot.sh`（拿到锁之后） | `slot`、`tick_id`、`wait_seconds`、`at` | 拿锁后的启动尝试，尚不能证明进入 Python |
| `paper_slot_exit` | `deploy/run_paper_slot.sh`（`docker exec` 返回后） | `slot`、`tick_id`、`exit_code`、`at` | docker exec 返回（也可能容器不存在、从未进入 Python） |
| `paper_slot_timing` | `paper_timing.capture` | `status`、`timing.slot`、`timing.started_at`、`timing.stages`、`timing.risk_scans`、`timing.scheduler_tick_id` | 这一轮的结束状态 |

硬约束（都是复审指出的错位点）：

1. **slot 的唯一事实来源是 `timing.slot`**。`paper_timing` 把 slot 写在 `timing` 里，
   顶层没有；从顶层读会把所有真实任务记成 `unknown`，start-to-start 与"上次成功风控"随之失真。
   主机脚本事件（锁/启动/退出）的 slot 在顶层，两种形状分别取、互不回落。
2. **日志路径跟随 `data_paths.report_dir()`**（容器内 `/app/reports/scheduler.log`），
   不再写死宿主绝对路径 `/opt/astock-codex/...`；需要指向别处时用 `ASTOCK_SCHEDULER_LOG`。
   读不到时 `dispatch` 返回 `available=False` 并给出 `error=missing|unreadable|bad_path|parse_error`；
   `parse_error` 是"观测坏了但 dashboard 不跟着 500"的最后一道闸门。
3. **状态四分类**：`completed`（`completed` / `ok`）仅代表外层任务完成；`skipped` /
   `already_done` / `up_to_date` 归 `idle`，**不计**为成功风控；`failed` / `error` /
   `blocked` 归 `failed`；其余归 `unknown`。格式异常的 `timing` 只降级该条证据，不抛错
   （包括 `started_at` 时区不可比时丢掉该对间隔，而不是让 naive/aware 相减抛 `TypeError`）。
4. **风控阶段只认 `risk_pass` 与 `risk.*`**：`risk_snapshot_enqueue` 是展示快照入队
   （`run_slot` 吞掉它的异常），既不能让展示失败抹掉真实风控成功，也不能让没有风控阶段的
   一轮虚报成功。两个 slot（risk / intraday）都还必须有 `timing.risk_scans` 中的
   `completed` 标记：它只在原有 durable scan 完成事务提交后记录，包含 cycle、日期、
   scan_minute 与扫描开始时间。未取得 claim 记录 `already_scanned`，抛错记录 `failed`；
   外层 completed 和 `risk_pass` 零错误都不能替代这个标记。旧日志缺标记时保留未知。
   阶段 errors 必须为非负有限整数，缺失、布尔、负数、小数、NaN/Infinity 均不作为成功证据。
   若同一 capture 含失败重试，累积阶段错误仍会保守地排除该条成功证据。
5. **tick 可对账**：`tick_id` 由主机脚本生成并注入容器（`ASTOCK_SCHEDULER_TICK_ID`），
   启动尝试与结束事件按同一个 id 配对。`start_attempts` 是尝试次数，
   `unfinished` 是没有结束/退出证据的尝试，`exited_without_result` 是 docker exec 已返回
   但没有 timing 结果的尝试。后二者都不能直接证明 Python 正在运行。
6. **queue age 按轮次计**：有启动事件的轮次用进入时刻的等待；没有启动事件的轮次
   （旧日志、`weekly-review` 直连 `docker exec`）退回结束事件里的等待，同一轮不重复。

`paper_dispatch_metrics` 的输出字段（`schedule_status()["dispatch"]`）：

| 字段 | 含义 |
| --- | --- |
| `ticks.start_attempts` / `ticks.skipped` | docker exec 前的启动尝试 / 被主机锁挡住的轮次 |
| `ticks.end_events` | `paper_slot_timing` 条数（结束事件） |
| `ticks.completed` / `idle` / `failed` / `unknown_status` | 结束事件的四分类 |
| `ticks.unfinished` / `exited_without_result` | 尝试后没有结束/退出证据 / docker exec 返回但无结果 |
| `ticks.unparsed` | 形状不成立、只降级不计入的结束事件条数 |
| `ticks.source` | 日志里是否存在启动事件（`host_start_attempt_events` / `slot_timing_end_events`） |
| `runs[slot]` | 每个 lane 的 start_attempts / end_events / skipped / 四分类 |
| `skips` / `skips_by_slot` | 跳过原因与 lane 归因 |
| `queue_age_seconds` | 进入 runner 前的主机锁等待（每轮只计一次） |
| `start_to_start_seconds[slot]` | 同一 lane 相邻 `started_at` 间隔的 p50/p95/max/breaches |
| `last_successful_risk_scan` | `at` / `source`（`risk_slot` 或 `intraday_risk_phase`）/ `status` / `slot` |

### 3.2 仍然存在的盲区（如实记录，不假装已覆盖）

- **外层 cron 的 `flock -n` 拒绝不会执行脚本，因此不产生任何日志行**：`auction` / `open` /
  `close` / `fast-entry` 的外层锁拒绝不可见。`ticks.start_attempts` 是"拿锁后的启动尝试数"，
  不是"cron 触发次数"；两者的差额只有在本包之外改造 cron 行（`|| 记录拒绝`）后才能补齐，
  这属于 P11-B 起的工作，本包不动 cron。
- **`fast-entry` 的事件写在 `/var/log/astock-fast-entry.log`**（它的 cron 行重定向不同），
  不在 `scheduler.log` 里。因此 P11-A 的 `skips_by_slot` 默认看不到 `fast-entry` 的让路；
  当前只能用 `ASTOCK_SCHEDULER_LOG` 切换为该文件，不能同时聚合两份日志。
- **`weekly-review` 的 cron 行直接 `docker exec`**，不经过 `run_paper_slot.sh`，因此没有
  启动/退出事件，只有 `paper_timing` 结束事件。
- `queue age` 只覆盖主机 `paper-scan.lock` 等待，不含外层 cron 锁排队与 Docker/Python 启动。
- `paper_slot_start` 在 `docker exec` 之前发出，因此 `ticks.start_attempts` 的精确含义是
  "拿到主机锁、准备启动 runner"。容器不存在或镜像缺失时同样是 start_attempts +
  `exited_without_result`，不会冒充"跑完一轮"。

## 4. 工作包与出口

每包一个 PR，重基到当时 master，跑 exact-head CI，合并后重新确认 master SHA。
**任何时候不删除现有全局锁，不提前拆进程，不改评分/阈值/风控规则。**

### P11-A（#324）：调度级可观测性收尾，部分观测完成

- 范围：为 tick 级事实增加结构化记录与只读查询，覆盖上表五项缺口
  （`scheduled tick` / `runner started` / `lock skipped` / `in_progress` / `queue age` /
  `last successful risk scan`），并在 `schedule_status` 暴露。
- 不做：不改 cron 频率，不改锁，不改 slot 词表语义，不接 RoundKlineCache，不动交易规则。
- 出口：同一份数据能回答"每个 scheduled tick 是否真正进入 runner"；全量回归绿；
  所有耦合预算**不上升**。
- 出口的实际达成度（第二轮复审修复后）：**本包代码交付收尾，部分观测完成**。
  启动尝试、脚本可见的锁跳过、未配对尝试、主机锁等待、带独立完成标记的成功风控、
  有结束日志的 slot start-to-start 可查。实际 runner 启动总数、真实 in_progress、全部 cron
  触发数不可回答；fast-entry 与 scheduler.log 尚不能同时读。没有部署或生产节拍验收证据。
  保留原目标及这些缺口，不能以本包完成声称 PR-11 总体验收通过。
- 本次验证：真实 `monitor_risk` 同分钟两入口先完成再幂等跳过，外层均 completed，
  但只有第一次有独立成功标记且只产生一笔成交；非法阶段错误数在直接聚合和文件读取
  两层都只影响该条成功证据，其他指标仍可用。完整 CI 以最终 HEAD 的 Actions 为准。

### P11-B：`risk-fast` 独立 lane

- 范围：给 `risk` 独立 cron 条目与独立锁（`risk.lock`），沿用现有 lease / fencing /
  `execution_planner` 权威；`intraday` 内保留"同轮已跑过就不重复"的兜底语义。
- 出口：`risk` 的 start-to-start interval 达到目标（P11-A 的量法）；同一分钟不出现第二次
  风险 pass；保护性减仓结果逐字段等价。

#### P11-B 实际落地（#325 已合并，不部署）

最终审核 HEAD `f1c276b`，5 个提交；宿主与断网容器各 7,845 项通过，CI 六项与
两轮安全扫描成功。Ready 后 Squash 为 `b8b38751301bad23efe51fc7c81a7b4f68109517`。
`max_file_lines` 的额外 +16 行门禁接线例外已登记，不能表述为所有预算均未放宽。

| 决策 | 取值 | 理由 |
| --- | --- | --- |
| 触发分钟 | 与 `intraday` 同一三分钟网格（`RISK_CRON_MINUTES = INTRADAY_CRON_MINUTES`） | 同分钟进入时 durable scan claim 只放一方；若错开一分钟，同一窗口会出现两次风控 |
| 外层 cron 锁 | `flock -n .locks/risk.lock` | 风险 lane 不排队：上一轮还在跑就跳过这一跳，兜底由 intraday 承担 |
| 脚本内主机锁 | `risk-scan.lock`（`PSch.host_lock_name("risk")`） | 全市场候选扫描占着 `paper-scan.lock` 也挡不住风险 pass |
| 全扫描意图标记 | 风险 lane 不参与 | 该协议只服务共用 `paper-scan.lock` 的 lane（`fast-entry` 让路） |
| 运行租约 | `paper-runner-risk` | 挂在全局 `paper-runner-global` 下等于没有独立 lane；其它 slot 仍共用全局租约 |
| 运行身份 | `risk:YYYYMMDDHHMM`（三分钟取整，与 `intraday:` 同一规则） | 当日一次性 `paper_jobs` 行会让第二跳起全部 `already_done`，节拍停在第一分钟 |
| 租约 TTL | 900s（原 2400s） | 三分钟 lane 只做风险 pass；死亡进程仍由心跳缺失（约 6 分钟）更早判定 |
| `already_scanned` | 归 `idle`，runner 退出码 0。**仅当对端已经把同一身份推进到 `completed`** | 同窗口被对端跑完是幂等成功，不是故障。注意：`run_slot` 的**外层**状态仍会被归成 `completed`，所以"这一分钟到底跑没跑真扫描"只能看 `timing.risk_scans` 标记 |
| `running` 的语义 | **不是完成**：`monitor_risk` 在写事务之外有界等待对端完成，超时即返回 `status=risk_running` | 认领只证明"这一窗口有人在跑"，不证明保护性减仓已经做完。把 `running` 当 `completed` 会让 intraday 在减仓之前恢复委托、扫候选、买入 |
| 执行顺序门禁 | `monitor_intraday` 只接受已完成的风控结论；否则整轮返回 `risk_in_progress` 并不进入委托恢复/候选/买入 | 独立 lane 的前提是"风控先于交易"仍然成立；宁丢一跳也不能倒序 |
| 扫描身份 | `scan_minute` 取三分钟窗口分钟（`_scan_window_minute`），与 `risk:YYYYMMDDHHMM` 同一取整规则 | 以前取自然分钟，09:34 与 09:35 是两个不同 claim，同一窗口可能跑两次真实风控 |
| 等待事务边界 | 等待发生在认领事务**之外** | 在 `BEGIN IMMEDIATE` 里 sleep 会一直占着写锁，对端的完成事务只能撞锁，最后双方都失败 |
| 观测 | `dispatch.risk_scans`（按状态/按 lane）+ 交易窗口内的 start-to-start | 只看外层 slot completed 无法区分"风险 lane 真跑"与"整段 intraday 兜底" |

节拍与告警校准（本轮复审修复）：

- start-to-start 的"同一时段"判定必须同时比较**日期**：T 日 14:55 → T+1 09:30 落在同一
  `HH:MM` 窗口，只比时刻会把跨夜间隔算成样本，次日第一跳凭空 breach。
- `schedule_status()` 现在汇总 `risk:` 运行记录；否则独立 lane 的失败不会进入运行状态告警。
- 并发回归用**可控 barrier**：真实 SQLite + 线程，risk lane 认领后阻塞在执行阶段，
  intraday 兜底进入时必须得到 `risk_running`；反向场景（对端在等待窗口内完成）必须得到
  `already_scanned`。

验收映射（离线部分）：`test_paper_schedule_contract::RiskLaneGuard` 锁住渲染与锁归属；
`test_paper_scheduler_priority` 用真实 `flock` 证明风险不被共享锁阻塞、且只被自己的锁串行；
`test_paper_risk_lane` 证明窗口身份/租约隔离/同窗口幂等/完成标记归属；
`test_paper_risk_exit_production_path::RISK-SCAN-*` 冻结同一分钟最多一次风控、跨周期、
失败重试与逐字段等价。**真实 start-to-start 必须等部署后按 `dispatch` 实测，CI 不作数。**

### P11-C：`execution-resume` 独立 lane

- 范围：把已批准 pending execution 的恢复拆成独立短任务（独立锁 + lease），每单重新取新鲜报价，
  仍走 `execution_planner` / execution authority / `commit_fill`。
- 出口：恢复不再等候选重算；同轮不重复恢复同一批委托；成交结果逐字段等价。

#### P11-C 实际落地（待审核，不部署）

- 从 #325 合并后的 master `b8b38751301bad23efe51fc7c81a7b4f68109517` 开始。
- 新 slot `execution-resume` 与风险采用相同三分钟网格：Linux 09:33–11:24、
  13:00–14:57，独立外层锁 `execution-resume.lock`、主机锁 `execution-scan.lock`、
  租约 `paper-runner-execution`（TTL 600 秒，沿用心跳与 fencing）。保留所有旧全局锁。
- 窗口身份 `execution-resume:YYYYMMDDHHMM` 进入原 `paper_job_runs`；同窗口完成后
  不再次恢复。running 返回 `in_progress`，重试耗尽的失败仍返回非成功，不能冒充
  `already_done`。相邻窗口也由同一租约串行，失去租约后不能完成写入。
- **自动调用的调度变化**：open/intraday 不再自行调用恢复函数；委托统一在独立任务
  的下一次触发时恢复。原返回字段 `execution_orders` 保留空列表，显式传入的结果
  仍可复用。避免保留三个独立入口后又添加嵌套任务、重复恢复或重新审批。
- 独立任务先调用原 `monitor_risk`：只有已完成结果才能恢复；未完成、不可用、失败或
  cycle 翻转仍由 B 的门禁阻止。已完成风险的窗口 claim 不重跑风险 pass，缺少完成证据
  时沿用现有风控兜底。不拉全市场候选，不评分、不生成或审批新信号。
- `process_pending_execution_orders` 保持冻结实现：每次恢复重新抓取逐票报价，在
  报价 I/O 后开启写事务，逐单重新读取状态，沿用 `commit_fill` 的执行权威、
  事件幂等、资金、T+1、停牌/涨跌停、cycle、savepoint 与异常处理。该恢复函数排除手工单，
  风控兜底内部既有的手工单处理不变。
- 新 lane 失败进入 `schedule_status` 告警；真实风险完成标记按新 lane 归因，
  不将仅完成恢复的任务计作新的成功风控。
- Windows 任务从契约表注册。沿用的重复窗口终点差异、共享 cgroup 竞争、生产180秒
  节拍仍待部署/P11-D 实测；独立锁不代表资源隔离已完成或 PR-11 总体验收通过。

离线验收：真实 SQLite 与线程屏障覆盖同窗口/跨窗口竞争、租约隔离、失败重试和
fencing；克隆账本逐字段比较冻结恢复函数与新 slot 的订单、成交、持仓、lots、账户、
信号、预占、风险决策与审计。原 partial-fill golden replay 继续验证相同报价不重复成交、
新报价继续同一订单；POSIX 主机锁由远端宿主与断网容器验收。

### P11-D：`candidate-full` lane 与资源预算

- 范围：候选全市场扫描独立 lane（独立锁、CPU/memory 上限、SQLite `busy_timeout`/backoff、
  按容量设并发），保证 `risk-fast` 在其运行时仍按时执行。
- 出口：`candidate-full` 跑满时 `risk-fast` 的 interval 不劣化；无 SQLite 长事务、无 OOM。

### P11-E：`dashboard-refresh` lane 收口

- 范围：展示刷新 lane 的并发与队列年龄上限、跳过原因可观测；展示失败不回滚交易结果。
- 出口：队列年龄与跳过次数可查；展示链不占用交易 lane 的锁。

## 5. 每包验收模板

1. `python -m unittest test_architecture_boundary_audit` 绿，15 项实测逐项**不上升**；
2. 受影响模块回归绿；
3. 全量后端回归绿（`python scripts/run_tests_parallel.py --shards 4`）；
4. 交易结果等价性：冻结输入下候选顺序/评分/拒绝理由、数量、资金占用、成交状态逐字段一致；
5. 合并后重新 `git rev-parse origin/master`，下一包以新 SHA 重基，绝不沿用旧 head 的 CI 结论。

## 6. 基线卫生：归还 PR-10 借的 fanout 额度

`paper_table_fanout_max` 的 `FEATURE_ALLOWANCE = 1` 是 PR-10 工作包 A 建立
`paper_order_ledger` 时登记的（50 → 51），登记原文写明"B/C/D 把写入搬离后扇出应当下降，
届时额度必须收回"。实测扇出已经回到 **50**（= `HEAD_BUDGET`），额度空转。
本 PR 把它收回（1 → 0，上限 51 → 50），登记见 `docs/COUPLING_BUDGET.md`。
PR-11 后续每一包都必须在这个天花板下工作。

## 7. 明确不做

- 不按行数拆 `paper_trading.py`（路线图 PR-12 的范围，且上一轮已实测"切块净收益为负"）；
- 不为 PR-11 顺手改账本读路径（那是 PR-10 的收尾，不是 PR-11 的目标）；
- 不在本批删除任何全局锁、不提高并发、不改 cron 频率之外的交易语义。
