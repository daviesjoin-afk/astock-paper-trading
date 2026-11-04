# 模拟盘瘦身待办（P1）

基线：`1f65ff9541b1f0dfc9b5e745ddd71ba34c866d4d`  
关联审计：[P0 基线审计](../docs/SIMULATION_P0_BASELINE_2026-11-04.md)

本清单的核心原则：任务、流程、模块都可以删除；但先证明它没有独立消费者或可由更短链路替代。删除代码前先停入口、迁消费者、补回归，再清理旧实现。

## 优先级

| ID | 候选 | 初步动作 | 风险 | 状态 |
|---|---|---|---|---|
| P1-01 | 周度复盘自动改参 | 保留周报，移除直接写参数；建议改为 advisory | 中 | 待实现 |
| P1-02 | close 多个固定重试时间 | 改成持久化数据就绪/续跑，不立即删除 | 中高 | 待观测 |
| P1-03 | risk 独立 lane + intraday fallback | 在可靠性证据充分后，考虑删除重复兜底触发 | 高 | 待观测 |
| P1-04 | adaptive_runner + evolution_loop_runner | 统一 orchestration，保留不同算法与证据产物 | 高 | 待分析 |
| P1-05 | selection_runner + paper_selection_runner | 共享同轮行情/因子输入，不合并不同结果/身份 | 中 | 待分析 |
| P1-06 | fast-entry 30秒任务 | 以增量成交质量/确认率决定保留或删除 | 高 | 待观测 |
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