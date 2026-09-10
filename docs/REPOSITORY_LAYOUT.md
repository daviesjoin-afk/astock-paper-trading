# 仓库结构地图（REPOSITORY_LAYOUT）

本文回答"**这个文件属于哪一层、能不能动**"。设计意图与边界见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)；
历史计划与快照见 [`archive/`](archive/README.md)。

## 顶层

```
backend/            Python 后端：HTTP、策略域、模拟盘域、数据与研究
frontend/src/       前端源码：ESM 模块（入口 src/app.js + bridge.js + boot.js）
frontend/styles/    样式片段（styles/index.css 按原顺序 @import）
frontend/dist/      **构建产物（提交物）**：由 esbuild 打包，服务端直接下发
docs/               运行手册、设置 PRD、测试矩阵、发布说明、本文件
docs/archive/       已完成的历史计划与快照（只作追溯）
deploy/             服务器脚本（备份/恢复/健康检查/cron）
.github/workflows/  CI（语法、ruff、锁文件、pip-audit、前端、测试 3.11/3.12、Docker 冒烟）
Dockerfile          应用镜像；docker-compose*.yml 本地与服务器编排
```

## backend/ 分层

### 1. HTTP 入口：`api_*.py`

| 文件 | 职责 |
| --- | --- |
| `api_paper.py` | 模拟盘只读看板/审计/风控视图，以及手动下单与周期控制 |
| `api_adaptive.py` | 自进化与 AI 研究（含 modlens 状态/读图路由） |
| `api_settings.py` | 统一设置读写与审计 |
| `api_strategies.py` | **Strategy Admin**（`/api/strategies`）：只做请求解析、调用用例、异常→状态码、响应 |
| `main.py` | FastAPI 应用装配、静态页下发（`/`、`/app.js`、`/app.css`）、若干历史只读端点 |

约定：`api_*.py` **不**打开数据库、**不**复制业务规则、**不**按错误字符串猜状态码；
策略相关一律走 `strategy_service`。

### 2. 调度入口：`*_runner.py`

容器/定时任务调用的**薄入口**，只负责"取参数 → 调领域函数 → 落日志"，
业务规则在对应领域模块里。它们都被 compose/cron 真实调用，不是脚本残渣：

`paper_runner`（盘中 slot）、`paper_selection_runner`、`selection_runner`、
`close_snapshot_runner`（收盘快照）、`history_recovery_runner`（历史补拉）、
`news_runner`（情报采集）、`adaptive_runner`（自适应）、`evolution_loop_runner`（进化循环）。

### 3. 策略域：`strategy_*.py`

- **身份与版本**：`strategy_registry`（唯一权威；不可变版本 + 生命周期状态机）
- **应用服务**：`strategy_service`（用例、事务边界、异常翻译）、`strategy_api_models`（请求契约）
- **DSL**：`strategy_dsl_schema`（规范/校验）、`strategy_dsl_evaluator`（求值）、
  `strategy_dsl`（**对外 facade**，保留）
- **风险与执行**：`strategy_risk_fingerprint`、`strategy_risk_profiles`、
  `strategy_risk_enforcement`（非对称风险门）、`strategy_parameter_schema`
- **运行时**：`strategy_runtime`（编译期画像 + 缓存）、`strategy_creation_preview`（创建预览）
- **治理**：`strategy_clusters`（同构归簇）、`strategy_champion`（晋升/回滚）、
  `strategy_policies`（策略级策略表）

### 4. 模拟盘域：`paper_*.py`

- **账本与撮合**：`paper_trading`（主模块）、`paper_storage`（SQLite 连接生命周期）、
  `paper_repository`、`paper_schema_migrations`、`db_migrate`
- **纯计算**：`paper_allocation`（共享池席位/预算）、`paper_sizing`（股数）、
  `paper_performance`（盈亏）、`paper_portfolio`（lot 聚合）、`paper_trading_rules`
  （交易日/费用/证券权限）、`paper_quote_policy`（行情新鲜度与成交核验门禁）
- **只读投影**：`paper_archive_projection`、`paper_ledger_reader`
- **研究/选股**：`paper_selection`、`paper_research`、`paper_replay_regression`

### 5. 数据层与其他

`data_fetcher`（兼容入口）+ `marketdata_*`（transport / cache / providers / normalizers）、
`universe`、`factors`、`decision_*`、`adaptive_*`、`asymmetric_risk`、`build_info`。

## frontend/dist

**构建产物，必须与源码同步提交**：

```bash
cd frontend && node build.mjs      # 生成 dist/app.js、dist/app.css
```

CI 会重建并校验一致性；服务端 `/app.js`、`/app.css` 直接下发 `frontend/dist/` 里的文件。
改 `frontend/src/**` / `frontend/styles/**` / `index.html` 后**必须重新构建并提交 dist**；
模块地图与依赖概览见 [`frontend/src/README.md`](../frontend/src/README.md)。

## docs/ 与 docs/archive/

- 现行文档：`RUNBOOK`（运行手册）、`SETTINGS_PRD`、`TEST_MATRIX`、`DEMO`、
  `RELEASE-v*`（**发布说明，不删除**）、`PRD-architecture-hardening`（仍然有效的分阶段计划）、本文件。
- `docs/archive/`：**已完成**的历史计划与快照（含带日期的交接记录与仓库规范评审）。
  归档不等于删除——保留可追溯性，但不再代表当前设计；其中的路径/提交可能已过时。

## 维护规则

- 生产源码目录（`backend/`）不允许出现**没有说明**的 0 字节 `.py` 文件；
  仅允许显式 allowlist 中的包标记文件（当前为空列表）。由
  `backend/test_repository_hygiene.py` 强制。
- 删除任何模块前先证明无用：`git grep` 搜模块名/类名/路径（含 `api_*.py`、
  `*_runner.py`、`deploy/`、`.github/`、`Dockerfile*`、`docker-compose*`、`*.sh|bat|ps1`、
  `docs/`、`README*`、`pyproject.toml`、`requirements.txt`）。
  注意接口路径常被**动态拼接**（`'/api/x/'+id`），只搜字面量会漏调用方。
