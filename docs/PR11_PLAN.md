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
