# 模拟盘 P0 基线审计与瘦身入口

审计日期：2026-11-04  
仓库：`daviesjoin-afk/astock-paper-trading`  
审计分支：`codex/simulation-slimming-p0-20261104`  
基线 master：`1f65ff9541b1f0dfc9b5e745ddd71ba34c866d4d`  
性质：基于 GitHub 源码的静态架构与调度审计；未连接生产服务器、未读取生产账本、未运行真实行情或交易。

## 1. 历史 PR 与远端分支清理核验

当前 GitHub 远端分支列表核对结果：

- 远端 heads：10 个，包含 `master`。
- 非 master 分支：9 个。
- 开放 PR：0 个。
- PR #337 已合并，merge commit 为 `1f65ff9541b1f0dfc9b5e745ddd71ba34c866d4d`。
- 用户执行的第一批删除：32 个成功、0 个失败。
- 8 个 C 类历史分支仍保留；另有 `codex/tencent-market-fallback`，该分支在原审计 A+B 可删清单中，但不在本次执行命令中，因此按“严格按命令执行”保留。

结论：本轮远端删除没有误删 master、没有开放 PR 受影响；但不是所有原审计可删分支都已删除，`codex/tencent-market-fallback` 是唯一明确遗漏项。它不阻塞 P0，也不应在本轮顺手删除。

此前运行记录称生产仍为 `79d4390`，但本次没有连接服务器核验。该 SHA 只能作为待核对的历史部署信息，不能视为当前生产事实。本轮只以 GitHub master 为开发审计基线，不部署。

## 2. 当前源码规模基线

来自 master 的 Git tree 与文件内容读取：

| 指标 | 当前值 | 口径 |
|---|---:|---|
| backend 生产 Python 模块 | 333 | 排除文件名以 `test_` 开头的 Python 文件 |
| backend 测试 Python 文件 | 423 | `backend/test_*.py` |
| Git tree 条目 | 1,074 | 含目录、源码、测试、文档、部署文件等 |
| `paper_trading.py` | 14,640 行 | 296 个顶层函数/类定义，105 条顶层 import |
| `adaptive_engine.py` | 4,626 行 | 自适应/学习主引擎 |
| `paper_schema_migrations.py` | 2,804 行 | 数据库结构迁移 |
| `learning_evaluation.py` | 2,813 行 | 学习与评估 |
| `data_fetcher.py` | 2,397 行 | 行情与历史数据获取 |
| `ai_review_service.py` | 2,096 行 | AI 槽位、审核与共识 |
| `execution_planner.py` | 2,197 行 | 成交模拟与唯一成交提交 |
| `adaptive_risk.py` | 1,997 行 | 自适应风险逻辑 |
| `tradability_position_evidence.py` | 1,872 行 | 可交易性/持仓证据 |
| `shadow_comparison.py` | 1,854 行 | Shadow 对照 |
| `strategies.py` | 1,888 行 | 策略实现 |
| `adaptive_selection.py` | 1,747 行 | 自适应选股 |
| `learning_dataset.py` | 1,985 行 | 学习数据集 |
| `paper_portfolio_read_model.py` | 1,795 行 | 组合只读模型 |
| `news_learning.py` | 1,559 行 | 新闻学习 |
| `main.py` | 1,590 行 | FastAPI 应用入口 |

重要解释：

1. 大文件是审计入口，不是自动删除目标。
2. 296 个顶层定义、105 条 import 是对 `paper_trading.py` 的文件内计数，不等于运行时调用数或项目依赖数。
3. 本轮没有重新运行 coupling AST 工具，也没有重新测量全项目 import cycle。历史 `docs/COUPLING_BUDGET.md` 的冻结数据仍是历史基线，不冒充 11-04 新测值。
4. 行数下降不作为主要成功指标；优先减少任务、重复事实获取、重复规则和不必要的运行时工作。

## 3. 当前交易调度基线

### 3.1 调度定义

`backend/paper_schedule_contract.py` 当前定义：

- `RUN_SLOTS`：7 个，分别为 auction、open、risk、execution-resume、close、weekly-review、intraday。
- `RUNNER_ONLY_SLOTS`：2 个，分别为 fast-entry、auction-track。
- `RUNNER_SLOTS`：9 个。
- `SCHEDULE_TASKS`：11 条定义。
- 其中实际进入 `SCHEDULED_SLOTS` 的唯一 slot 类型为 8 个；fast-entry 由独立 cron 文件调度。

### 3.2 Cron 任务规模

`deploy/astock-codex.cron` 当前有 59 条活动命令记录（不含注释与环境变量）：

| 任务类别 | Cron 记录数 | 主要职责 |
|---|---:|---|
| auction-track | 1 | 09:15–09:24 竞价轨迹采集 |
| auction | 3 | 09:25 预选及失败重试 |
| intraday | 5 | 盘中全市场扫描 |
| risk | 5 | 独立风险快通道 |
| execution-resume | 5 | 已批准委托恢复 |
| open | 3 | 开盘审批及失败重试 |
| close | 6 | 盘后评分、历史数据/因子未就绪时重试 |
| weekly-review | 1 | 周度报告 |
| 其他数据/研究/运维任务 | 30 | 选股、历史恢复、新闻、学习、研究、快照、备份、推送等 |
| **合计** | **59** | — |

另外存在：

- `deploy/cron/astock-fast-entry`：每分钟两次（间隔 30 秒）的快速入场确认任务。
- `deploy/astock-three-strategy-start.cron`：一次性三策略周期启动器，注释计划日期为 2026-10-26。是否已在生产完成、启动标记是否仍存在，本轮无法从 GitHub 源码确认。

因此当前至少有 60 条常规/高频调度入口，加一条历史一次性启动器；不能只统计 `SCHEDULE_TASKS` 来代表整个项目调度规模。

## 4. 业务职责与唯一事实负责人

以下能力属于当前架构的核心资产，应保留其业务能力，不代表必须保留现有文件形态：

| 能力 | 当前负责人/边界 | 处理原则 |
|---|---|---|
| 模拟成交提交 | `execution_planner.commit_fill` | 唯一成交提交权威，不建立第二条写入路径 |
| 持仓批次 | `paper_lot_ledger` | 保留 lot 数量、成本与成交关联事实 |
| 共享现金 | `paper_shared_cash` | 保留资金算术与写入负责人 |
| 资金预占 | `paper_capital_reservations` | 保留预占、释放与订单关系 |
| 风控应用流程 | `paper_risk_service.run` | 保留风险规则，继续减少对主文件的反向依赖 |
| 组合读取 | `paper_portfolio_read_model` | 保留周期化、只读的组合事实 |
| 任务生命周期 | `paper_job_lifecycle` | 保留 claim/heartbeat/fencing/CAS/恢复 |
| 调度合同 | `paper_schedule_contract` | 保留唯一 slot 与 schedule 事实来源 |
| 行情权威 | `market_data_service` / `market_data_contract` | 保留数据来源、时效与完整性边界 |
| 策略身份/生命周期 | `strategy_registry` / `strategy_lifecycle` | 保留不可变版本和显式晋级 |
| AI 传输 | `ai_provider_transport` | 保留唯一网络传输和错误归一化 |

瘦身应删除无价值的流程和重复实现，而不是删除这些事实边界。

## 5. 任务环节瘦身审计

状态含义：

- **保留**：当前有独立业务职责或明确消费者。
- **合并候选**：输出有价值，但调度/取数/编排可合并。
- **删除候选**：存在明显冗余或越权行为，需补回归后移除。
- **待观测**：没有生产耗时、调用频率或使用证据，暂不删除。

| 环节 | 当前事实 | 初步决定 | 原因与前置条件 |
|---|---|---|---|
| risk + intraday 风控兜底 | risk 独立 lane；intraday 仍调用 monitor_risk；通过 `paper_risk_scan_state` 的 (cycle_id, asof_date, scan_minute) durable claim 去重 | 待观测 | 不是简单的双倍风控执行。intraday 兜底是独立 risk lane 被跳过/失败时的保护。只有证明独立 lane 有可靠恢复、无漏扫，才考虑删除兜底 |
| execution-resume | 只恢复已批准的 pending/partial 委托，并先确认风险扫描已 settled | 保留 | 不是候选扫描；删除会让已批准委托滞留 |
| fast-entry | 30 秒二次确认，独立 lease；确认后排队，不直接创建委托 | 待观测 | 与 3 分钟 intraday 不同，可能承担 T 策略时效价值。需要看确认后进入正式扫描的比例、增量成交质量和资源成本 |
| auction-track | 保存竞价轨迹；AI 审核消费 `auction_tracking`，`auction_analysis` 记录后续方向反馈 | 保留 | 有明确研究与审核消费者，不是无人消费的采集任务 |
| auction | 09:25 预选，后续 open 仍用最新有效行情审批 | 保留 | 预选与最终审批分离是正确的交易安全边界 |
| close retries | 15:05、15:10、15:12、15:25、15:40、16:00；后几次用于完整日线/因子尚未就绪后的增量续传 | 合并候选 | 当前多条固定 cron 代表数据就绪重试。目标是持久化 data-ready 任务/有界续跑；未完成迁移前不能直接删重试 |
| weekly-review | 生成 `paper_reviews` 和 Markdown；API `/api/paper/reviews`、dashboard 与前端消费。函数还会直接调用 `PAC.write_params` 并插入 `paper_parameter_versions` 自动改参数 | 删除候选（仅自动改参步骤） | 报告有消费者，不能删整个报告任务；自动改参是独立于人工确认/双 AI 共识 apply 的另一条参数写入路径。建议保留报告，删除自动写入，改为仅生成建议 |
| selection_runner | 盘后研究策略快照、forward validation；15:45–17:15 多次尝试 | 合并候选 | 多次重跑主要处理行情/历史数据就绪。与 paper_selection 输出不同，先共享事实和数据就绪状态，不合并不同研究产物 |
| paper_selection_runner | 按正式策略版本输出 Top-N 研究候选，17:25/17:55/次日 08:50 | 保留/合并候选 | 与 selection_runner 的产物、数据库和 provenance 不同；不应只因名称相似就删。可复用同一日行情/因子快照 |
| history_recovery_runner | 每 15 分钟分段恢复，使用持久化游标与 resource-heavy lock | 保留 | 是可恢复的数据维护工作，不应因 Cron 条目多而删除；后续可评估事件驱动/持久队列 |
| news_runner | 盘前、午间、盘后及 22:00 更新事件证据 | 待观测 | 时点不同，公告流有实际变化。需要对比事件增量、调用成本和下游消费后再决定是否减少频次 |
| adaptive_runner + evolution_loop_runner | 共用 adaptive_learning.sqlite3 / self_evolution 参数与学习数据，但各自运行不同阶段：adaptive_engine 的观察、AI 审核、reward；evolution_loop 的 OBSERVE/EVALUATE/MUTATE/VALIDATE/APPLY 候选生命周期 | 合并候选 | 两套进化编排同时存在，存在参数候选与生命周期管理重叠；先梳理各自输出、候选创建量、晋级路径和是否相互消费，再决定统一 orchestrator，不能直接删除任一套 |
| research_worker | 推进持久化 Agent/实验任务队列，默认 ADVISORY | 保留 | 不等于 adaptive_runner；是另一类任务执行器 |
| dashboard snapshot refresh | 每分钟触发轻量 precheck，真正工作由持久队列 claim/coalesce | 保留/待观测 | Cron 高频不等于每分钟都做重计算；先量 idle tick 成本和队列等待 |
| close_snapshot_runner | 15:15 按 CLOSE_SNAPSHOT_POLICY 获取并验证全市场收盘快照 | 合并候选 | 可能与 close/selection 的行情输入共享，但策略不同；先核对读取者与快照身份，避免重复刷新 |
| 三策略一次性启动器 | 独立 cron 模板，注释日期 2026-10-26，受标记文件保护 | 删除候选（运维确认后） | 需要在生产核对启动标记是否仍存在、是否已完成及是否还有安装任务；不能只删 Git 文件而留下服务器 cron |

### 5.1 第一项建议执行的瘦身

`_weekly_review` 的报告生成必须保留，但自动改参不应继续作为隐藏的第三条参数应用通道：

1. 目前满足至少 6 笔核验平仓、10 条信号后，按一周累计收益自动改变 `entry_score_delta`。
2. 该路径直接写账户参数并追加 `paper_parameter_versions`，不经过 `evolution_apply.apply_tuner_proposals` 的双 AI 共识、人工确认、版本与回滚门禁。
3. 目标：周度报告继续计算表现与建议；参数调整仅形成 advisory，不直接修改运行参数。
4. 验收：周报仍可查询和展示；运行参数与版本表不再被周报任务改写；历史数据不迁移、不删除；测试覆盖样本不足、正收益、负收益、回撤和重复执行。

这是首个 P1 候选，不在本 P0 文档提交中修改交易行为。

## 6. 新的目标架构与瘦身顺序

继续采用模块化单体，不引入微服务、不新建第二套 scheduler、不新建第二本账本。

### 目标运行主链

```text
MarketData Snapshot
  -> Candidate Scan
  -> Strategy Evaluation
  -> Signal / Entry Approval
  -> Capital Reservation
  -> Order Intent
  -> Execution Planner (唯一成交提交)
  -> Lot / Cash Ledger
  -> Portfolio Read Model
  -> Risk / Exit
```

研究链：

```text
Research Question + PIT Evidence
  -> AI Research (provider-neutral)
  -> Experiment / Replay
  -> OOS + Robustness + Counter-evidence
  -> Shadow
  -> Human Approval
  -> Strategy Lifecycle
```

运行链：

```text
Scheduler Contract
  -> Small Worker Entry
  -> Durable Job / Lease / Fencing
  -> Business Application
  -> Timing / Audit / Recovery
```

### 分阶段执行

1. **P0 基线审计（本文件）**：确认 master、远端 PR/branch、模块规模、任务表、业务 authority、瘦身候选。
2. **P1 模拟盘瘦身**：先删除不再需要的自动改参、重复任务/无消费者路径；对可合并的多次 cron retry 改为数据就绪驱动；每个删除项有消费者证据和回归。
3. **P2 交易架构扁平化**：让 Risk、Execution Resume、Candidate 使用独立轻量入口；收敛 `paper_trading` 中的候选和买入协调。
4. **P3 数据与组合瘦身**：同轮行情事实复用，减少重复 SQL/因子计算，明确缓存身份与失效。
5. **P4 研究/AI 瘦身**：统一研究与实验编排，保留不同模型/算法作为 provider 或 experiment backend，不保留重复 orchestration。
6. **P5 前端与运行时**：移除无消费者页面/API/桥接；按真实资源占用调整 Worker 和调度。

## 7. 必须保持的安全不变量

- 唯一成交提交权威不变；不新增可绕过 `execution_planner.commit_fill` 的写入路径。
- 现金、lot、预占、订单与成交在同一正确事务边界内保持一致。
- T+1、停牌、涨跌停、报价时效、来源校验和执行真实性不放宽。
- 风控退出不能被候选扫描、AI 或 dashboard 阻塞。
- 任务 lease、heartbeat、fencing token、CAS 和恢复保护不能因删任务而失效。
- 策略版本、周期身份、PIT、样本外验证、审计历史不能丢失。
- AI 仍然是 ADVISORY；任何正式参数应用必须走已有人工授权/版本门禁。
- 删除任务不删除历史账本、成交、审计和研究证据；不在本轮操作生产数据库。

## 8. 性能验收基线的缺口

本轮读取到的是源码、任务定义和历史文档，不是当前生产运行采样。下一步需要在受控环境采集：

- 各 slot 的 start-to-start、queue wait、实际运行耗时 p50/p95。
- Risk lane 跳过、失败、intraday fallback、durable claim 的对应关系。
- fast-entry 确认率、正式扫描接续率、后续核验成交率及成本。
- close 每次 retry 的状态变化、数据覆盖增量、因子重建是否真正有新增。
- selection_runner 与 paper_selection_runner 的实际产物增量及共用行情/因子成本。
- adaptive_runner 与 evolution_loop_runner 的候选生成、有效验证、人工审批、激活数量。
- Dashboard refresh 的 idle tick、队列等待、实际刷新时长。
- 冷/热启动、模块导入、SQL、provider I/O、CPU 计算与锁等待。

没有这些数据前，不设“删除 50% 任务”或“缩短到 300ms”等拍脑袋目标。

## 9. 本轮明确未做

- 未修改任何交易规则、策略参数或风控阈值。
- 未删除任何调度任务、代码模块或数据库表。
- 未运行真实行情、模拟成交或生产账本操作。
- 未部署服务器。
- 未把历史 `docs/CURRENT_ARCHITECTURE_AUDIT.md`、`docs/SIMULATED_TRADING_ARCHITECTURE_REVIEW.md`、`docs/COUPLING_BUDGET.md` 的旧基线当成 11-04 当前测量。

本文件作为后续 P1 瘦身和 P2 架构扁平化的当前 GitHub master 基线。