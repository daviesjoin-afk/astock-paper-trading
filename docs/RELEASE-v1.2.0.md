# v1.2.0 发布说明 — 确定性演示、离线 CI 与引擎模块化

发布日期：2026-09-07。对应提交 `86860d8`。

本版本聚焦三件事：让新用户**零凭据体验完整系统能力**（确定性离线演示 + golden replay 契约）、让 CI **彻底离线可复现**（无网络测试层 + 测试分层规范）、继续把单文件引擎**拆成可维护的模块**（Phase 1/2 纵向切分）。撮合语义、schema 与账本格式无任何变化。

## 新增

### 确定性离线演示（ASTOCK_DEMO=1，#20）

- 设置环境变量 `ASTOCK_DEMO=1` 启动时，注入一套完全合成的演示数据：10 只合成标的（6009xx）+ 完整叙事账本，**无需网络、无需 API key**。
- 叙事覆盖全链路审计证据：正常买入、T+1 当日卖出被拒、陈旧报价拒单、硬止损卖出、涨停价买入、持仓质量复盘与 5 日净值曲线。
- 幂等注入：以 `paper_audit event='demo_seeded'` 为标记，重复启动不重复注入；`ASTOCK_DEMO_FORCE=1` 可强制重建。
- 生产安全：不设置该环境变量时注入逻辑完全不触发；docker-compose 仅在宿主显式导出变量时透传（#31）。

### Golden Replay 契约（#28，#32）

- `test_demo_replay_golden.py` 在 CI 中对演示账本的结构摘要（订单/成交/风控决策/持仓/NAV/审计事件）做**逐字节比对**，防止演示叙事漂移；墙钟时间不在比对范围。
- 两个测试：全新克隆重注入的确定性 + 与内置 golden 基准的一致性。

### 离线 CI 测试层（#29，#33）

- docker-smoke 阶段以 `--network none` + tmpfs `data_cache` 运行全量后端回归：**任何用例默认不许联网**。
- `CONTRIBUTING.md` 新增测试分层规范：offline-mandatory / network-optional（`ASTOCK_NET_TESTS=1` 才运行）/ manual-verification。
- 7 个此前从未被 unittest 收集的 pytest 风格用例改造为 TestCase，回归总数增至 **239 项（3 项联网用例默认跳过）**。

### 测试场景矩阵（#27，#35）

- `docs/TEST_MATRIX.md` 按 规则层 / 数据质量层 / 撮合执行层 × 各门禁 整理：实现位置、正反用例、覆盖状态（✅/⚠️）。
- 新增 3 项规则层离线测试：涨跌停按主板/创业板/ST 分层（9.5/19.5/5.0）、卖出印花税与滑点常量、证券权限短代码归一化。
- ⚠️ 缺口已拆为 good-first-issue 子任务（#21–26），欢迎认领。

## 变更

### 引擎模块化（Phase 1/2）

- **Phase 1（#18）**：看板读模型拆入 `dashboard_queries.py`。
- **Phase 2（#34）**：手动下单链 8 个函数拆入 `manual_orders.py`（1019 行），`paper_trading.py` 从 15492 行降至 14682 行；公开 API 通过 facade 保持 100% 兼容，AST 级逐函数等价验证。
- 撮合/风控/审计行为零变化；仅代码组织调整。

### 前端构建管线（#19）

- 引入 esbuild 构建管线，移除历史镜像副本与手动压缩文件，`frontend/package.json` 提供标准 build 入口。

### 性能：活动概览瘦身（#16）

- `/api/paper/overview?activity=1` 不再返回委托页不消费的 signals / position_reviews / jobs 投影，payload 约 **-59%**（gzip 约 140KB → 57KB），冷重建同步减负；portfolio 响应不变。
- 手动下单提交增加防重复守卫。

### 工程治理

- 新增 `.gitattributes`（#36）：全仓文本强制 LF（Windows 脚本除外），根治 autocrlf 假性全量 diff 与部署包 CRLF 污染。
- 依赖加固与锁文件一致性检查修复（#13/#14）。
- 开启 GitHub Discussions；roadmap #1/#2/#3 拆分为 #21–29 子任务并打 good-first-issue / help-wanted 标签；清理历史 `codex/*` 分支；合并后自动删除分支。

## 升级说明

1. 拉取最新代码后按原方式重建即可（`docker compose up -d --build` 或 `./start.sh`）。schema 仍为 v4，无迁移。
2. 确定性演示默认关闭；需要体验时设置 `ASTOCK_DEMO=1`（建议同时 `--no-scheduler` 关闭盘中调度）。
3. 本版本不修改撮合语义、风控参数或账本格式，历史周期与审计记录完全兼容。
4. Windows 贡献者克隆后 worktree 现在统一为 LF；`*.bat` / `*.ps1` 保持 CRLF。

## 验证

- CI：Python 3.11/3.12 全量回归、Ruff、锁文件一致性、pip-audit、前端检查、Docker 冒烟（含离线测试层）全绿。
- 本地离线套件：239 tests OK（3 skipped 为可选联网用例）。
- 生产容器部署验证：md5 与 git 对象逐文件比对一致，health 200，universe 覆盖 100%。
