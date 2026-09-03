# Exec plan: Architecture hardening

## Purpose / success criteria

将审计建议转成可分阶段验收的架构硬化工作：今天从服务器取源并完成本地行为保持的改造和测试，再推送服务器；2026-09-04 验证服务器，服务器明确通过且用户确认后才推送 GitHub。

## Context map

- Baseline: `master` / `c9add450`（实施前重新确认）。
- Existing PR #4: `codex/fix-test-sqlite-resource-warnings`，与本任务隔离。
- Primary risk areas: `backend/paper_trading.py`, `backend/db_migrate.py`, `backend/decision_engine.py`, `backend/data_fetcher.py`, `backend/adaptive_engine.py`, `frontend/`。
- Product invariant: paper trading only; no broker order path.

## Milestones

- [x] M1：评审并确认 `docs/PRD-architecture-hardening.md`，开始补齐服务器运行信息。
- [x] M1a：从服务器 `/root/codex` 只读拉取源码快照并记录 commit、工作区和运行版本。
- [x] M2：本地 P0/P1 小步改造与回归，形成完整测试和回滚证据。
- [x] M3a：抽出决策证据快照契约与加载适配器，保持 `decision_engine` 旧入口兼容，并增加显式注入回归测试。
- [x] M3b：抽出不依赖数据源的决策评分、时机、止盈和止损规则，保持 `decision_engine` 兼容导出。
- [x] M3c：抽出共享行情 HTTP 传输层，保持 `data_fetcher` 旧名称、重试和熔断行为兼容。
- [x] M3d：抽出模拟盘交易日、费用、证券类型与权限规则，保持 `paper_trading` 旧内部入口兼容。
- [x] M3e：抽出 SQLite 连接生命周期、只读连接、WAL 检查点和锁重试，保持 `paper_trading` 兼容包装。
- [x] M3f：抽出持仓 lot 到聚合读模型的纯计算，保持 `paper_trading._position_rows` 的输出兼容。
- [x] M3g：抽出行情代码、时间戳、行情行与 K 线 DataFrame 的无副作用标准化，保持 `data_fetcher` 旧入口兼容。
- [x] M3h：为 adaptive 读取 paper ledger 提供 `mode=ro/query_only` 只读端口，保留明确的补偿写路径。
- [x] M3i：建立策略身份 registry，统一 active/legacy 标签展示，不改变现有调度。
- [x] M3j：建立 ledger 通用仓储薄接口，保留 `_rows`/`_audit` 兼容包装，为对象级 SQL 迁移留入口。
- [x] M3k：抽出今日报价新鲜度和今日盈亏纯计算，保持 `paper_trading` `_today_*` 入口兼容。
- [x] M3l：集中 paper ledger 增量 schema、运行时租约和点火影子表迁移；`db_migrate.py` 以 v1-v4 事务化调用，旧表 fixture 验证幂等、回滚和字段补齐。
- [ ] M3：本地 P2/P3/P4/P5 按小步提交完成，保持 API/交易语义兼容。
- [x] M4：2026-09-03 将本地验收通过的同一版本部署到服务器并保留备份/回滚点。
- [ ] M4a：2026-09-04 完成服务器健康、进程、数据库、模拟周期测试。
- [ ] M5：服务器通过且用户确认后，push 分支/创建或更新 PR，记录 CI 与 GitHub 状态。

## Decision log

- 先取服务器工作区快照，再在本地进行最小范围、行为保持的改造。
- 采用渐进拆分，不做一次性重写。
- 服务器未通过或证据不完整时，硬性停止 GitHub 阶段；服务器部署与 GitHub push 分开处理。

## Completion record

- PRD/implementation branch: `codex/architecture-hardening-local`（本地）。
- GitHub push: 未执行。
- Server deployment: 已执行；本地提交 `b60d3a6` 对应部署包已校验并解包，镜像重建/重启完成，服务器备份位于 `/root/backups/20260903-architecture-predeploy/`。
- Tests for this PRD: 159 backend tests passed with ResourceWarning as errors; compileall, diff check and prior Node/HTTP smoke checks passed.
- Remote source pull: completed with SHA-256 verification; HTTP health read-only check returned 200 on `:8600`.
