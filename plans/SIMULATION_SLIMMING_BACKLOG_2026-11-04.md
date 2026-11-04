# 模拟盘瘦身待办（P1）

基线：`1f65ff9541b1f0dfc9b5e745ddd71ba34c866d4d`  
关联审计：[P0 基线审计](../docs/SIMULATION_P0_BASELINE_2026-11-04.md)

本清单的核心原则：任务、流程、模块都可以删除；但先证明它没有独立消费者或可由更短链路替代。删除代码前先停入口、迁消费者、补回归，再清理旧实现。

## 优先级

| ID | 候选 | 初步动作 | 风险 | 状态 |
|---|---|---|---|---|
| P1-01 | 周度复盘自动改参 | 保留周报，移除直接写参数；建议改为 advisory | 中 | **已合并 #339（ff789c4）** |
| P1-01A | 周报收益口径与职责文档 | 修正周收益/累计收益混用；更新参数写入 owner 说明；不虚构 advisory 直达 apply | 中 | 待修正（PR #339 审核发现） |
| P1-02 | close 多个固定重试时间 | 改成持久化数据就绪/续跑，不立即删除 | 中高 | 待观测（已核查，见 §代码核查） |
| P1-03 | risk 独立 lane + intraday fallback | 在可靠性证据充分后，考虑删除重复兜底触发 | 高 | 待观测 |
| P1-04 | adaptive_runner + evolution_loop_runner | 保留证据生产者→候选生命周期消费者；只评估统一调度/观测/资源管理，不合并业务阶段 | 高 | 已完成静态关系核查，待运行数据 |
| P1-05 | selection_runner + paper_selection_runner | 共享同轮行情/因子输入，不合并不同结果/身份 | 中 | 待分析（已核查，见 §代码核查） |
| P1-06 | fast-entry 30秒任务 | 以增量成交质量/确认率决定保留或删除 | 高 | 待观测（仅排除孤儿 Cron，未证明业务收益） |
| P1-07 | close_snapshot_runner 与 close/selection | 验证快照消费者后，评估合并行情刷新 | 中 | 待分析 |
| P1-08 | 竞价跟踪 | 暂保留；有 AI review / auction_analysis 消费者 | 中 | 保留 |
| P1-09 | dashboard refresh | 保留持久队列，测量 idle tick 后决定是否减少触发频次 | 低 | 待观测 |
| P1-10 | 一次性三策略启动 cron | 生产确认 marker 和安装状态后清理模板/安装脚本 | 高 | 待运维确认 |
| P1-11 | history recovery 重复 cron | 先保留 resumable cursor，后续评估常驻/数据就绪 worker | 中高 | 待观测 |
| P1-12 | research worker 与 adaptive worker | 区分任务队列、算法与生命周期，避免简单合并 | 高 | 待分析 |

## 每项删除的强制检查

1. 找出唯一触发入口和所有调用者。
2. 找出数据库/文件/API/前端消费者。
3. 确认是否存在隐式消费者（Cron、systemd、Windows Task Scheduler、shell、Docker entrypoint）。
4. 确认任务是否承载重试、补偿、幂等、恢复或故障兜底职责。
5. 明确被删除步骤的业务输出由谁接管；若无消费者，写出无消费者证据。
6. 对历史数据采取保留策略，不因删任务而删表/删记录。
7. 增加删除后的反向测试：旧入口不再可触发、无残留 Cron、无孤儿任务、无重复执行。
8. 在隔离数据库和测试行情上回归；不以生产账本试验。
9. 记录任务数量、资源占用、响应时间、失败率前后差异。
10. 删除后保留回滚 commit/PR，不做不可审计的强制推送。

## 删除完成定义

- 调度入口、业务代码、测试、前端/ API、部署脚本同步收敛。
- 无已知下游引用。
- 无遗留持久任务会继续执行旧 handler。
- 交易账本、策略版本、历史审计与 PIT 证据不受影响。
- exact-head CI 全绿。
- 性能改善有同口径数据支持。
### P1-01A：周报收益口径与职责文档

**结论：P1-01 的主要安全改造正确，但需要一个小型修正 PR。**

- `_weekly_review` 当前 `ret = latest_nav / reference_capital - 1` 是累计收益，报告中的“累计收益”表述正确；但参数建议文字使用“本周正/负收益”，口径不一致。
- 建议按 ISO 周起点前最后一个有效 NAV 作为周初基准，计算 week-to-date return；没有周内 NAV 或可证明的周初基准时，不生成方向性参数建议。
- 当前 `max_dd` 使用账户全部 NAV 历史，建议明确标为“历史最大回撤”，不要称作滚动/本周回撤。
- `docs/LEDGER_OWNERSHIP.md` 仍把周度复盘列为参数写入的业务决策者，需要改成“仅生成 advisory；不拥有参数采纳/回滚权”。
- 周报 advisory 目前不是 `evolution_apply.apply_tuner_proposals` 的可直接输入。文档应说明“文本建议不自动进入正式候选”，除非另行实现经过验证的转换/审批链。

### P1-04：adaptive 与 evolution loop 的真实关系

**结论：不能把两个 runner 认定为重复流程后直接合并或删除。**

源码显示它们更接近上游证据生产者与下游候选生命周期消费者：

1. `adaptive_runner --session close` 调用 `adaptive_engine.run_learning_cycle`，完成市场画像、alpha 样本与收益成熟、数据集、reward、reward attribution、bandit 决策、risk/selection evolution 和双 AI 调参。
2. `run_learning_cycle` 的 dual-AI 调参结果通过 `dual_ai_tuner.track_run` 写入 `evolution_tracking`，代码注释明确说明供 `evolution_loop` 的 EVALUATE / MUTATE 消费。
3. `evolution_loop.ProductionBackend` 不执行 AI 网络调用；它读取 `self_evolution` 指标、调用 `auto_evolve_if_needed` 生成候选、验证候选，并将其保留为 `pending_activation`。
4. `evolution_loop.apply` 不激活 runtime 参数；正式激活仍走显式人工确认、候选验证与 stale-CAS 入口。

因此：保留两个不同业务阶段。可以研究统一 task metadata、调度入口、观测、lease/资源管理，但不能把上游证据生产与下游参数候选生命周期压成一个不可区分的步骤，也不能删掉 `evolution_tracking` 的生产/消费关系。

在生产数据可用后，先统计每个 adaptive run 的 tracking 产出、evolution loop 的消费数、候选生成/验证/激活数量、跳过原因和耗时，再决定是否简化重试或调度。


## 代码核查（2026-11-04，master `ff789c4`）

以下结论来自源码与调度表实测，不是推测。复核者可按列出的位置自行验证。

### P1-02：close 固定重试

**结论：不删除，保持待观测。**

- `deploy/astock-codex.cron` 的 close 窗口为 15:05 / 15:10 / 15:12 / 15:25 / 15:40 / 16:00，全部 `flock -n`（拿不到锁即跳过）。
- 生产实测单轮 552–558 秒（9.2 分钟）。按此推演：15:05 那轮约 15:14 结束，
  因此 **15:10 与 15:12 在"正常长跑"场景下必然被跳过**。
- 但这两个窗口并非无用：它们是"进程被 SIGKILL 后的早重试"。`flock` 由 cron 行持有，
  进程死亡即释放锁，此时 15:10/15:12 是唯一能立刻接手的窗口。
- `universe.refresh_history` 已有：持久化游标 `kline_refresh_state.json`、失败代码的
  指数退避重试队列（5→60 分钟、跨进程持久化）、失败原因持久化、
  以及"按已提交代码推进游标"的轮转。数据就绪驱动机制**已存在**。
- `deploy/run_paper_slot.sh` 已把被锁挡住的情况写成 `paper_host_lock/not_acquired`
  事件，运维可见，不是静默失败。

因此"固定重试"与"数据就绪"并非互斥的两套机制，而是重试窗口 + 增量续跑的组合。
删除任何窗口都会削弱"进程崩溃后当天仍能完成"的保障，而收益仅是减少被跳过的 cron
触发（成本近似为一次 `flock`）。**在没有生产调度数据证明 15:10/15:12 从未在崩溃场景
生效之前，不删除、不改等待策略。**

### P1-05：selection_runner 与 history_recovery_runner 的重复刷新

**结论：重复存在，但不值得改。**

- `selection_runner`（15:45/16:15/16:45/17:15）与 `history_recovery_runner`
  （15:20 起每 15 分钟）**共用 `resource-heavy.lock`**，且都调用
  `U.refresh_history` + `P.rebuild_selection_factor_cache`。
- 每个 `selection_runner` 窗口前后 15 分钟内都存在 history_recovery 窗口，
  即同一条 `refresh_history` 在相邻时间被调用两次。
- 但 `refresh_history` 在数据已齐时走 `up_to_date` 早退（只读 manifest），
  重复成本约为一次 manifest 扫描，不是全市场重新下载。
- `selection_runner` 保留 refresh 是**安全边界**：它必须在选股前证明"当日完整日线
  已落盘"，否则会把昨天的候选记成今天的运行。删除该调用会让这条门禁失去数据前提。

因此不合并、不删除。若未来要优化，正确方向是让两者共享一次
`refresh_history` 的结果（例如由 history_recovery 发布"数据就绪"事实），
而不是从 selection_runner 移除门禁前置条件。

### P1-06：无效任务扫描

**结论：当前调度中没有孤儿任务。**

- `deploy/astock-codex.cron` 的 17 类目标（7 个 slot + 10 个 runner 脚本）全部有
  对应实现文件，不存在"指向已删除脚本"的条目。
- 一次性三策略启动器（P1-10）仍在，但它由 `.start_three_strategy_cycle` 标记文件
  保护且幂等，是否清理需生产确认标记状态，不属于代码侧可判定项。


## P0-G：架构守卫治理（先于 P2 大规模目录迁移）

- 守卫只 hard fail 业务语义不变量；文件规模、函数规模、模块数量和 fan-out 等指标改为 review signal。
- 已移除 risk facade 40 行与 risk service 925 行硬上限。
- 保留唯一成交、PIT、cycle/as-of/version、风险、lease/fencing、AI 授权和操作员安全守卫。
- P2 大规模 package 移动前，必须先完成递归源码扫描、package-aware import graph 和 capability owner registry，避免旧路径守卫漏扫或强迫保留旧 facade。
- 具体分类和修改协议见 [架构守卫治理规则 v2](../docs/ARCHITECTURE_GUARD_GOVERNANCE.md)。
