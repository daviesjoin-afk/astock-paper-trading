# 仓库结构地图（REPOSITORY_LAYOUT）

本文回答"**这个文件属于哪一层、能不能动**"。设计意图与边界见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)；
历史计划与快照见 [`archive/`](archive/README.md)。

## 顶层

```
backend/            Python 后端：HTTP、策略域、模拟盘域、数据与研究
frontend/src/       前端源码：ESM 模块（入口 src/app.js + bridge.js + boot.js）
frontend/styles/    样式片段（styles/index.css 按原顺序 @import）
frontend/dist/      **构建产物（提交物）**：由 esbuild 打包，服务端直接下发
frontend/e2e/       Playwright 浏览器 E2E（合成数据 server.py + specs/ 关键旅程）
docs/               运行手册、设置 PRD、策略平台、自进化、测试矩阵、发布说明、本文件
docs/archive/       已完成的历史计划与快照（只作追溯）
deploy/             服务器脚本（备份/恢复/健康检查/cron）；`native-centos9` 原生 profile 是 Python 3.14 canonical runtime 的唯一 legacy 例外（仍用 3.11）
.github/workflows/  CI（语法、ruff、锁文件、pip-audit、前端、后端测试 Python 3.14、浏览器 E2E、Docker 冒烟）
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

- **身份与版本**：`strategy_registry`（唯一权威；不可变版本与 current head）
- **生命周期**：`strategy_lifecycle`（exact-version state、迁移、CAS、append-only history）
- **晋级证据**：`strategy_promotion`（R29/R30 exact evidence policy 与 append-only proposals）
- **应用服务**：`strategy_service`（用例、事务边界、异常翻译）、`strategy_api_models`（请求契约）
- **DSL**：`strategy_dsl_schema`（规范/校验）、`strategy_dsl_evaluator`（求值）、
  `strategy_dsl`（**对外 facade**，保留）
- **风险与执行**：`strategy_risk_fingerprint`、`strategy_risk_profiles`、
  `strategy_risk_enforcement`（非对称风险门）、`strategy_parameter_schema`
- **运行时**：`strategy_runtime`（编译期画像 + 缓存）、`strategy_creation_preview`（创建预览）、
  `user_strategy_participation`（用户策略接入生产链路）
- **订单意图与执行**：`order_intent`（意图契约，拒绝数量越权）、`execution_planner`
  （中央计划/复核/落库）、`execution_dispatch`
- **可比较与 Shadow 运行时（R32 COMPLETE）**：
  `market_data_contract.snapshot_fingerprint()` 拥有 Market Snapshot identity；
  `simulation_runtime_context` 冻结 Active exact facts；`shadow_runtime` 拥有环境身份与
  reference state 纯转移；`shadow_run_service` 按 exact stamp 解析 owner 并执行；
  `shadow_run_repository` 只读写显式 ID 的
  append-only ShadowRun evidence，不提供 latest continuation 查询。
  R32-D 另加三个模块：`shadow_comparison`（纯 domain/report contract，不碰 DB/时钟/provider）、
  `shadow_comparison_repository`（只按显式 report ID 幂等追加/读取 `shadow_comparison_reports`）、
  `shadow_comparison_service`（按显式 ID 加载 Active 与 ShadowRun 证据并编排）。

R33-A 再加三个模块（事实层，不建 policy）：`strategy_health`（纯 contract + 纯
`build_strategy_health` + 指纹，不碰 DB/时钟/provider）、`strategy_health_repository`
（只按显式 snapshot ID 幂等追加/读取 `strategy_health_snapshots`，不做业务解释）、
`strategy_health_service`（按显式 exact version + 显式窗口读 owner 证据并编排采集）。
健康快照**只写自己的表**：不写 lifecycle、不写正式账本；R33-B policy 只追加 decision，R33-C workflow 经人工审批后调用 R31 lifecycle。
`strategy_registry.runtime_readiness` 新增可选 `version`/`checksum` 参数（缺省仍是 head，
行为不变）：健康事实必须绑 exact version，head ready 不能证明历史 version ready。

R33-B 再加三个模块（policy 层，不做 lifecycle mutation）：`strategy_retirement_policy`
（纯 contract + 纯 `evaluate_retirement_policy`，只依赖事实层，不 import paper_trading /
strategy_lifecycle / sqlite3）、`strategy_retirement_repository`（只按显式 decision ID 幂等追加/读取
`strategy_retirement_decisions`，不做业务判断）、`strategy_retirement_service`（按显式 snapshot ID
读既有快照、调用 policy、追加决策，并只**描述** proposal；不调用 `transition`）。
决策表**只有建议**：没有 current_retirement_state / latest_decision 这类列。

R33-C 再加 `strategy_retirement_workflow`（proposal/approval 不可变契约）、
`strategy_retirement_workflow_repository`（append + exact-id read）与
`strategy_retirement_workflow_service`（重新验证 exact decision/snapshot/version/state，
仅在人工 APPROVE 后调用 `strategy_lifecycle.transition`）。它不增加状态机、latest 查询、
后台扫描或正式交易账本写入。
R34-A 加入组合运行事实链：`portfolio_runtime`（纯、确定性 exact cycle/as-of snapshot 契约）、
`portfolio_runtime_service`（只读 portfolio owners 并追加快照）、`portfolio_runtime_repository`
（append-only + exact snapshot ID 查询）。schema DDL/migration 仍由
`paper_schema_migrations` / `db_migrate` 唯一维护；新增路由仅提供
`POST /api/portfolio/runtime/snapshots` 与 `GET /api/portfolio/runtime/snapshots/{snapshot_id}`。
它不调用 allocator/coordinator，不写正式 paper ledger、lifecycle 或订单，也没有 current/latest 路由。
R34-B 加入多策略分配计划链：`portfolio_allocation_policy`（纯、确定性 policy contract：
canonical 意图词汇、严格权重/声明覆盖校验、证据闸门、`PortfolioAllocationPlan` 构建；
算术仍全部委托 `paper_allocation.position_limits_from_weights`）、
`portfolio_allocation_repository`（append-only + exact plan ID 查询）、
`portfolio_allocation_service`（读取显式 snapshot、验证指纹、装配 exact cycle-pinned 声明、
评估 policy 并追加计划）。同时 `paper_allocation.position_limits` 被重构为
`StrategyRuntime → effective_weight → position_limits_from_weights` 的 legacy 适配层，
公开签名/输出结构与行为不变。新增路由仅
`POST /api/portfolio/allocation/plans` 与 `GET /api/portfolio/allocation/plans/{plan_id}`。
旧 `portfolio_coordinator` 已在确认生产调用者为 0 后整模块删除；其 coordinator 专用行为测试和属性断言也已退休。Typed intents 和 exact reservation facts 由 runtime owner 提供。
它不写正式 paper ledger、lifecycle、orders 或 risk decision，也不 apply/execute 计划。
R34-C 生产接线位于 `paper_trading` / `manual_orders`：入口传入显式
`ResourceIntent`、从 exact snapshot 构建 exact plan、校验 reservation/revalidation，
随后仍由 Risk 与 Execution owners 决定许可和成交。`portfolio_workspace_service`
及 `GET /api/portfolio/workspace` 只接收明确的 cycle/plan ID，读取并验证快照、计划与
订单 provenance；没有 latest/current 选择或交易权限。旧 allocation/slot helper 的无调用
定义仍待删除，general dashboard 在缺 plan identity 时返回 `UNAVAILABLE`。
R35-A 加入受约束的**策略候选**链（事实层，无 promotion/execution 权限）：
`strategy_candidate`（纯契约：唯一 `StrategyCandidate` 身份与 canonical fingerprint，
不含任何评估事实）、`strategy_generator`（纯生成边界：`GeneratorInput → StrategyCandidate`，
无 DB / registry / 时钟 / current 查询，受约束表示复用既有 bounded DSL 与参数契约）、
`strategy_candidate_repository`（append-only：只按显式 candidate ID 幂等追加/读取
`strategy_candidates` + `strategy_candidate_proposals`，没有 latest/current getter）、
`strategy_candidate_service`（按显式 exact version + checksum 从 registry 建立 parent pin，
再编排生成与追加；不写 lifecycle、不下单）。新增路由仅
`POST /api/strategies/{id}/candidates`、`GET /api/strategies/{id}/candidates`
（按 exact version+checksum）与 `GET /api/strategies/{id}/candidates/{candidate_id}`。
DDL 由 `paper_schema_migrations.ensure_strategy_candidates` 唯一持有（migration v33）。

R35-B 把它扩展成 **Deterministic Candidate Expansion**：新增
`strategy_candidate_search_space`（纯契约：显式、有限、可 fingerprint 的搜索空间声明 +
组合基数 + slot 继承语义；无 DB / registry / 时钟），`strategy_generator` 改为显式
capability registry（`parameter_variant` / `factor_variant` / `entry_variant` /
`exit_variant` / `bounded_combination`），并新增 generation batch 追加表
`strategy_candidate_generation_batches`（一次生成请求 → 一个 batch identity → N 条
proposal 事件；无 current/latest 指针）。candidate 升级为内容身份
（`strategy-candidate-v2`：generator 能力身份与提案 provenance（hypothesis / research
source / seed / model）全部移到 proposal 事件与 batch 上、**不**参与 candidate fingerprint ——
同一 specification 由不同 generator 或不同 model 提出必须是同一个 candidate；候选表
同样是**纯内容持久化**，不含任何 generation provenance 列，`append_candidate()` 也不写；
migration **v34** 重建候选表，forward-only、不回填、历史 v1 行仍自证）。新增路由
`GET /api/strategies/{id}/candidate-generations/{batch_id}`；候选列表发布**全部**提案
证据引用，不投影成隐含 latest。R35-B **不**拥有
evaluation / promotion / execution / allocation 权限（见 `plans/r35b-deterministic-candidate-expansion.md`）。

R35-C 再把 **AI research** 接进这条链路：`strategy_ai_proposal`（AI 可以提出什么的纯契约：
形状 / 资源上界 / 禁止字段 / no-op；无 DB / 网络 / registry / 时钟）、
`strategy_ai_provider`（prompt → `ai_provider_transport.call_json` → 严格 proposal）、
`strategy_ai_candidate_service`（exact R27 research run + exact parent pin → AI bounded
proposal → R35-B batch 的编排与失败语义）。新增
`POST /api/strategies/{id}/candidate-generations/ai`。AI 路径**零新 DB 表**：产物继续写
既有 candidate / proposal / batch 三张表；generation batch 的 `batch_json` 额外持久化
可自验的 canonical search-space material。AI 不拥有 candidate identity / parent /
universe / regime / constraints / evaluation / promotion（见
`plans/r35c-ai-hypothesis-candidate-generation.md`）。

R36-A 补上**中间这层控制面**：`experiment_search_contract`（一次 bounded search request
是**什么**的纯契约：`SearchBudget` / `ExperimentSearchSpec` / `SearchJobSpec` / 状态转换
表 / queue policy；无 DB / 网络 / registry / current state / 时钟）、
`experiment_search_repository`（search run / job declaration / job event 三张 append-only
台账的唯一 owner）、`experiment_search_service`（exact R35 generation batch →
verified candidate pool → bounded search run + queued jobs；以及 claim 下一件运营工作）。
migration **v35** 新增 `experiment_search_runs` / `experiment_search_jobs` /
`experiment_search_job_events`。R36-A **零**前端、**零**公开 API、**零**实验指标列，
也**不执行** R29/R30 runner：它只调度"需要验证什么"，不制造证据、不做 selection、
不做 promotion（见 `plans/r36a-experiment-search-controller-foundation.md`）。
`strategy_candidate.candidate_from_projection()` 同时收紧为**显式 v1/v2 allowlist**：
未知 schema 版本 fail closed，不再"不是 v2 就按 legacy 读"。
- **执行真实性证据**：`execution_evidence`（三态成交证据契约）、`execution_lifecycle`
  （成交状态机与非法跳转拒绝）、`execution_outcome`（`selection_executable` ×
  `execution_verified` 连接与收益分层）
- **治理与进化**：`strategy_clusters`（同构归簇）、`strategy_champion`（晋升/回滚）、
  `strategy_policies`（策略级策略表）、`evolution_loop`、`evolution_apply`、
  `evolution_validation`、`asymmetric_risk`、`self_evolution`
- 策略数据模型、生命周期与删除边界的完整说明见 [`STRATEGY_PLATFORM.md`](STRATEGY_PLATFORM.md)；
  自进化链路见 [`EVOLUTION_ARCHITECTURE.md`](EVOLUTION_ARCHITECTURE.md)。

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

## R36-B1 候选实验桥接

- `backend/candidate_experiment.py`：纯候选回放契约，版本化 entry/factor/exit、历史成员过滤和 as-of 检查。
- `backend/candidate_experiment_service.py`：exact job/plan/candidate 到 R29 证据的编排及独立完成核验。
- `backend/experiment_contract.py`：既有实验权威内增加候选 subject/spec，共享环境归一和投影。
- `backend/experiment_search_contract.py`：v2 canonical plan；v1 仍可读，不补默认计划。
- `backend/experiment_validation_repository.py`：升级同一 ledger，v1 历史 hash 原样保留，v2 候选不冒充正式策略。
- `backend/test_r36b1_candidate_experiment_execution.py`、`backend/fixtures/r36b1_legacy_*.json`：CEX 行为测试与 exact base 冻结证据。
- `work/r36b1_candidate_experiment_mutation_check.py`：M-CEX 可逆真实故障注入。

回放仍由现有 R29 runner/PIT/execution model 执行；没有新 facade、重复 runner 或永久结果表。

## R36-B2 候选鲁棒性执行

- `backend/candidate_robustness_service.py`：exact PIT 证据 → 既有 R30 robustness runner 的候选编排与独立完成核验（本阶段唯一新增生产模块）。

鲁棒性仍由既有 R30 robustness runner、execution loop 与 PIT validator 执行；没有新 facade、重复 runner 或永久结果表。

## frontend/ 结构

```
frontend/src/        当前前端源码（ESM）：入口 app.js + bridge.js + boot.js
  core/              api.js（唯一 HTTP 出口）· dom.js · format.js · navigation.js（页面与路由）· state.js · strategy_labels.js
  features/          strategies（策略工坊）· settings（设置中心）· paper（模拟盘）· selection · execution · risk · adaptive
  ui/                dialog.js（toast / 内联错误 / 确认框 / 输入框）
frontend/styles/     样式片段（index.css 按原顺序 @import）
frontend/dist/       **构建产物（提交物）**：由 esbuild 打包，服务端直接下发 /app.js 与 /app.css
frontend/e2e/        Playwright 浏览器 E2E（server.py 起合成数据实例 + specs/ 关键旅程）
frontend/build.mjs   构建脚本
```

模块级说明、功能归属与依赖表见 [`../frontend/src/README.md`](../frontend/src/README.md)。

两个需要留意的**迁移缝**（过渡结构，不是目标架构）：

- `frontend/src/bridge.js`：把 inline `onclick` 需要的函数挂到 `window` 的兼容层；新代码不应新增 inline handler，长期目标是改为事件绑定并删除该文件。
- `frontend/src/boot.js`：集中执行顶层语句以保持拆分前的就绪顺序；待各模块显式 `init()` 后应退化为普通入口装配点。

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
  `STRATEGY_PLATFORM`（策略数据模型与生命周期）、`R32_COMPARABLE_RUNTIME_CONTEXT` 与
  `R32B_ACTIVE_COMPARABLE_EVIDENCE`（可比较运行证据）、`EVOLUTION_ARCHITECTURE`（自进化链路）、
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
