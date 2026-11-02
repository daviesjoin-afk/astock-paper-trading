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

1. **`risk` 是合法 slot、也在 `HOT_PATH_SLOTS` 里，但没有任何独立 cron 条目**
   （`SCHEDULED_SLOTS` 明确记录了这个决定，cron 里也有对应注释）。风控现在跑在
   `intraday` 里，与全市场刷新、候选生成、执行共用一条 3 分钟节拍。这就是 PR-11 要拆的对象。
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
| `paper_slot_start` | `deploy/run_paper_slot.sh`（拿到锁之后） | `slot`、`tick_id`、`wait_seconds`、`at` | 这一轮**确实要启动 runner** |
| `paper_slot_exit` | `deploy/run_paper_slot.sh`（`docker exec` 返回后） | `slot`、`tick_id`、`exit_code`、`at` | runner 进程返回（含崩溃/被杀） |
| `paper_slot_timing` | `paper_timing.capture` | `status`、`timing.slot`、`timing.started_at`、`timing.stages`、`timing.scheduler_tick_id` | 这一轮的结束状态 |

硬约束（都是复审指出的错位点）：

1. **slot 的唯一事实来源是 `timing.slot`**。`paper_timing` 把 slot 写在 `timing` 里，
   顶层没有；从顶层读会把所有真实任务记成 `unknown`，start-to-start 与"上次成功风控"随之失真。
   主机脚本事件（锁/启动/退出）的 slot 在顶层，两种形状分别取、互不回落。
2. **日志路径跟随 `data_paths.report_dir()`**（容器内 `/app/reports/scheduler.log`），
   不再写死宿主绝对路径 `/opt/astock-codex/...`；需要指向别处时用 `ASTOCK_SCHEDULER_LOG`。
   读不到时 `dispatch` 返回 `available=False` 并给出 `error=missing|unreadable|bad_path|parse_error`；
   `parse_error` 是"观测坏了但 dashboard 不跟着 500"的最后一道闸门。
3. **状态四分类**：`completed`（`completed` / `ok`）才算跑完一次新扫描；`skipped` /
   `already_done` / `up_to_date` 归 `idle`，**不计**为成功风控；`failed` / `error` /
   `blocked` 归 `failed`；其余归 `unknown`。格式异常的 `timing` 只降级该条证据，不抛错
   （包括 `started_at` 时区不可比时丢掉该对间隔，而不是让 naive/aware 相减抛 `TypeError`）。
4. **风控阶段只认 `risk_pass` 与 `risk.*`**：`risk_snapshot_enqueue` 是展示快照入队
   （`run_slot` 吞掉它的异常），既不能让展示失败抹掉真实风控成功，也不能让没有风控阶段的
   一轮虚报成功。
5. **tick 可对账**：`tick_id` 由主机脚本生成并注入容器（`ASTOCK_SCHEDULER_TICK_ID`），
   启动事件与结束事件按同一个 id 配对。于是 `started`（进入 runner）、
   `unfinished`（启动了但没有结束事件：仍在跑或被杀）、`exited_without_result`
   （退出但从未打印结果）是三个不同的数字。
6. **queue age 按轮次计**：有启动事件的轮次用进入时刻的等待；没有启动事件的轮次
   （旧日志、`weekly-review` 直连 `docker exec`）退回结束事件里的等待，同一轮不重复。

`paper_dispatch_metrics` 的输出字段（`schedule_status()["dispatch"]`）：

| 字段 | 含义 |
| --- | --- |
| `ticks.started` / `ticks.skipped` | 进入 runner 的轮次 / 被主机锁挡住的轮次 |
| `ticks.end_events` | `paper_slot_timing` 条数（结束事件） |
| `ticks.completed` / `idle` / `failed` / `unknown_status` | 结束事件的四分类 |
| `ticks.unfinished` / `exited_without_result` | 启动未结束 / 退出无结果 |
| `ticks.unparsed` | 形状不成立、只降级不计入的结束事件条数 |
| `ticks.source` | 日志里是否存在启动事件（`runner_start_events` / `slot_timing_end_events`） |
| `runs[slot]` | 每个 lane 的 started / end_events / skipped / 四分类 |
| `skips` / `skips_by_slot` | 跳过原因与 lane 归因 |
| `queue_age_seconds` | 进入 runner 前的主机锁等待（每轮只计一次） |
| `start_to_start_seconds[slot]` | 同一 lane 相邻 `started_at` 间隔的 p50/p95/max/breaches |
| `last_successful_risk_scan` | `at` / `source`（`risk_slot` 或 `intraday_risk_phase`）/ `status` / `slot` |

### 3.2 仍然存在的盲区（如实记录，不假装已覆盖）

- **外层 cron 的 `flock -n` 拒绝不会执行脚本，因此不产生任何日志行**：`auction` / `open` /
  `close` / `fast-entry` 的外层锁拒绝不可见。`ticks.started` 是"进入 runner 的次数"，
  不是"cron 触发次数"；两者的差额只有在本包之外改造 cron 行（`|| 记录拒绝`）后才能补齐，
  这属于 P11-B 起的工作，本包不动 cron。
- **`fast-entry` 的事件写在 `/var/log/astock-fast-entry.log`**（它的 cron 行重定向不同），
  不在 `scheduler.log` 里。因此 P11-A 的 `skips_by_slot` 默认看不到 `fast-entry` 的让路；
  要看它需要把该路径一起纳入（`ASTOCK_SCHEDULER_LOG` 指向对应文件）。
- **`weekly-review` 的 cron 行直接 `docker exec`**，不经过 `run_paper_slot.sh`，因此没有
  启动/退出事件，只有 `paper_timing` 结束事件。
- `queue age` 只覆盖主机 `paper-scan.lock` 等待，不含外层 cron 锁排队与 Docker/Python 启动。
- `paper_slot_start` 在 `docker exec` 之前发出，因此 `ticks.started` 的精确含义是
  "拿到主机锁、准备启动 runner"。容器不存在或镜像缺失时同样是 started +
  `exited_without_result`，不会冒充"跑完一轮"。

## 4. 工作包与出口

每包一个 PR，重基到当时 master，跑 exact-head CI，合并后重新确认 master SHA。
**任何时候不删除现有全局锁，不提前拆进程，不改评分/阈值/风控规则。**

### P11-A（本 PR 的下一包）：调度级可观测性（行为零变化）

- 范围：为 tick 级事实增加结构化记录与只读查询，覆盖上表五项缺口
  （`scheduled tick` / `runner started` / `lock skipped` / `in_progress` / `queue age` /
  `last successful risk scan`），并在 `schedule_status` 暴露。
- 不做：不改 cron 频率，不改锁，不改 slot 词表语义，不接 RoundKlineCache，不动交易规则。
- 出口：同一份数据能回答"每个 scheduled tick 是否真正进入 runner"；全量回归绿；
  所有耦合预算**不上升**。
- 出口的实际达成度（`#324` 复审后）：`runner started` / `lock skipped`（按 lane）/
  `in_progress`（= `unfinished` + `exited_without_result`）/ `queue age` /
  `last successful risk scan` / lane 级 start-to-start **可回答**；
  "cron 真的触发了几次"**不可回答**，因为外层 `flock -n` 拒绝不产生日志行（见 §3.2）。
  这条缺口如实留在计划里，不用近似值冒充。

### P11-B：`risk-fast` 独立 lane

- 范围：给 `risk` 独立 cron 条目与独立锁（`risk.lock`），沿用现有 lease / fencing /
  `execution_planner` 权威；`intraday` 内保留"同轮已跑过就不重复"的兜底语义。
- 出口：`risk` 的 start-to-start interval 达到目标（P11-A 的量法）；同一分钟不出现第二次
  风险 pass；保护性减仓结果逐字段等价。

### P11-C：`execution-resume` 独立 lane

- 范围：把已批准 pending execution 的恢复拆成独立短任务（独立锁 + lease），每单重新取新鲜报价，
  仍走 `execution_planner` / execution authority / `commit_fill`。
- 出口：恢复不再等候选重算；同轮不重复恢复同一批委托；成交结果逐字段等价。

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
