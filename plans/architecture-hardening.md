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
- [x] M3m：将 dashboard 账户卡片的批量账本投影迁入 `paper_repository.py`，保留 `_account_metric_inputs` 兼容入口并验证旧 schema 字段回退。
- [x] M3n：将行情 TTL 缓存、全市场快照 single-flight 锁和数据源健康文件读写迁入 `marketdata_cache.py`，保留 `data_fetcher.py` 旧入口与 monkeypatch 兼容。
- [x] M3o：将历史归档快照到只读订单行的投影迁入 `paper_archive_projection.py`，隔离损坏快照并保持活动列表字段兼容。
- [x] M3p：将腾讯/新浪实时行情响应解析迁入 `marketdata_providers.py`，保留 `data_fetcher.py` 请求、重试、源切换和返回顺序。
- [x] M3q：增加 adaptive 依赖边界静态回归，禁止直接导入订单模块或调用提交/取消/开仓入口。
- [x] M3r：将行情新鲜度、活跃度和成交核验门禁迁入 `paper_quote_policy.py`，保留 `paper_trading.py` 兼容包装并增加固定输入测试。
- [x] M3s：将腾讯/新浪 K 线响应解析迁入 `marketdata_providers.py`，保留 `data_fetcher.py` provider 请求与回退顺序。
- [x] M3t：将共享池席位分配和策略预算的纯计算迁入 `paper_allocation.py`，保留 `paper_trading.py` 数据读取、版本写入和兼容入口。
- [x] M3u：将 adaptive alpha 的基因归一化、适应度、变异和交叉迁入 `adaptive_genetics.py`，保留 `adaptive_engine.py` 数据集与训练编排。
- [x] M3v：将 `_price_aware_qty` 的股数 sizing 与约束解释迁入 `paper_sizing.py`，保留订单编排入口。
- [x] M3w：将 adaptive 组合影子风控的历史归一化、波动率、集中度和压力测试迁入 `adaptive_shadow_risk.py`，保持只读/影子边界。
- [x] M3x：将 adaptive risk 与 strategy-center 的展示身份接入 strategy registry，公开 active/legacy 元数据但不改变账户调度。
- [x] M3y：为版本化 SQLite migration 增加迁移前一致性备份，并以旧库 fixture 验证备份保留原始 schema/数据。
- [x] M3z：CI 新增独立 Docker 构建与健康端点冒烟 job；本机 Docker 引擎未启动，待服务器/GitHub 阶段验证实际容器运行。
- [x] M3aa：将活动订单的轻量列表 SQL 与账户名称投影迁入 `paper_repository.py`，保持归档合并和日期配额编排不变。
- [x] M3ab：将东财 `clist` 分页、主机冷却和完整性元数据迁入 provider 适配器；`data_fetcher.py` 只注入 HTTP、配置和健康状态，保留旧入口兼容。
- [x] M3ac：将东财概念成员分页与完整性判定迁入 provider 适配器；保留概念筛选、缓存和旧 `_fetch_concept_members` 入口编排。
- [x] M3ad：将个股到概念板块的东财响应解析迁入 provider 适配器；保留网络请求、主机回退和概念过滤口径。
- [ ] M3：本地 P2/P3/P4/P5 按小步提交完成，保持 API/交易语义兼容。
- [x] M4：2026-09-03 将本地验收通过的同一版本部署到服务器并保留备份/回滚点。
- [ ] M4a：2026-09-04 完成服务器健康、进程、数据库、模拟周期测试。00:05 初检中容器健康、`overview`/`strategy-center` 为 200；但服务器工作区有 100 个修改和 37 个未跟踪文件，且非交易时段，待保留该工作区并在交易时段继续验证。
- [ ] M5：服务器通过且用户确认后，push 分支/创建或更新 PR，记录 CI 与 GitHub 状态。

## Decision log

- 先取服务器工作区快照，再在本地进行最小范围、行为保持的改造。
- 采用渐进拆分，不做一次性重写。
- 服务器未通过或证据不完整时，硬性停止 GitHub 阶段；服务器部署与 GitHub push 分开处理。

## Completion record

- PRD/implementation branch: `codex/architecture-hardening-local`（本地）。
- GitHub push: 未执行。
- Server deployment: 已执行；本地提交 `b60d3a6` 对应部署包已校验并解包，镜像重建/重启完成，服务器备份位于 `/root/backups/20260903-architecture-predeploy/`。
- Tests for this PRD: 198 backend tests passed with ResourceWarning as errors; compileall, diff check and prior Node/HTTP smoke checks passed.
- Remote source pull: completed with SHA-256 verification. 2026-09-04 00:05 read-only server check: source `22762e2` is dirty (100 modified, 37 untracked); running image was built at `2026-09-03T21:11:43+08:00`, container is healthy, and `:8600` read-only endpoints returned 200. Health is degraded only because the previous trade-day live snapshot is stale for the new calendar day.
- Handoff: `docs/HANDOFF-architecture-hardening.md` records the local verification, server dirty-worktree gate, storage limit, verified source-only server archive, and verified full-history Git bundle for next-machine continuation.
