# PR6 执行计划

目标：以 acaa797 为基线保持账本行为，降低重复结构检查与CSV开销；init_db仍完整恢复运行状态。

- [x] PR302 squash合并；最新master、干净隔离worktree、指定分支核对。
- [x] 初始化调用链/步骤分类审计，先于生产修改。
- [x] 原owner拆结构与seed；事务安全脚本及持久结构receipt，不改变db_migrate版本语义。
- [x] 失败/并发/lease/对账/换DB/损坏结构专项与风险/账本对照。
- [x] 空库/旧库/正常库固定输入性能：P50/P95、锁等待、SQL/DDL/schema检查/CSV、风险启动前阶段。
- [x] L0-L4、最终CI；ARCHITECTURE/验收文档，Draft PR停止。

关键决定：冷路径完整执行并校验后才确认receipt；暖路径每个真实DB检查结构摘要，业务恢复不依赖receipt完成与否。
保留原未跟踪文件与所有合法成交，不部署、不自动合并PR6。
