# A 股量化模拟盘引擎

[English](README_EN.md) · 中文

[![CI](https://github.com/daviesjoin-afk/astock-paper-trading/actions/workflows/ci.yml/badge.svg)](https://github.com/daviesjoin-afk/astock-paper-trading/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

> **Local-first A-share paper-trading and strategy platform with declarative custom strategies, dynamic risk/allocation, T+1-aware execution, deterministic replay and auditable evolution.**

一个面向中国 A 股微观交易规则的、本地优先的**量化模拟盘与策略平台**。项目把 T+1、整手、涨跌停、停牌、行情时效、费用/滑点、风险决策与审计回放直接放进撮合路径，而不是在回测结束后再做近似修正。

系统**不连接券商、不触碰真实资金**。公开仓库只包含源码、容器配置和脱敏文档，不包含真实持仓、运行时数据库、服务器凭据或 API Key。

## 这是什么：内置模板 + 声明式用户策略

仓库不再只是"五套固定策略"。它是一个**策略平台**：

- **内置策略模板（builtin）**：五套经过验证的模型，作为可运行的模板与基线；
- **用户自定义策略（user）**：用**声明式 DSL** 定义，不写 Python、不放代码、不起进程；
- 两者走**同一条**执行、风控、分配与审计链路：不可变版本 → DSL 编译 → RuntimeContext → 风险画像 → 信号 → OrderIntent → 分配与 sizing → Execution Planner → 系统风控门禁 → 订单/成交/持仓 → 绩效与自进化。

平台能力覆盖：不可变 **Strategy Version**、**生命周期**状态机、**RuntimeContext** 运行契约、**风险编译**（策略只能收紧）、**动态分配**与共享资金池、统一的 **OrderIntent / Execution Planner**、**T+1 感知**执行、**确定性回放**、**策略自进化 / Champion-Challenger**，以及浏览器端的**策略工坊（Strategy Workbench）**。

细节见 [策略平台文档](docs/STRATEGY_PLATFORM.md)、[自进化架构](docs/EVOLUTION_ARCHITECTURE.md) 与 [架构说明](ARCHITECTURE.md)。

## 必须知道的六条平台语义

1. **自定义策略不运行 Python**：用户策略只是声明式 DSL，由平台的白名单编译器与离线求值器执行。
2. **策略不能指定最终下单数量**：`qty`/`shares`/`amount` 等字段属于执行器，出现即被终态拒绝；数量由现金、风险预算、敞口、行业与整手约束计算。
3. **策略"激活"不等于加入当前周期**：`active` 只决定**下一**周期资格。
4. **当前周期的参与者来自周期快照**：创建周期时冻结的启用集合 ∩ 当期账户，再减去生命周期 `paused` 的策略。
5. **零启用策略是合法的 Idle 模式**：不产生新信号，风控扫描、存量退出与系统调度照常。
6. **周期拥有经济资本，生命周期只控制执行许可**：`pause` 会立即移除执行资格，但**不会**删除该策略在当前周期里已有的经济所有权；`resume` 只是把执行资格放回来，不会凭空放大资本。

> 一句话：策略定义"想做什么"，平台决定"能不能做、能做多少、怎么做"。

## Dashboard 预览

当前发布版本：**v1.3.0**（见 [GitHub Releases](https://github.com/daviesjoin-afk/astock-paper-trading/releases)；[CHANGELOG](CHANGELOG.md) 的详细条目截至 v1.2.0）。查看 [架构说明](ARCHITECTURE.md)、[策略平台](docs/STRATEGY_PLATFORM.md)、[仓库结构地图](docs/REPOSITORY_LAYOUT.md) 和 [安全边界](SECURITY.md)。CI 验证 Python 3.11/3.12；API 返回的历史内部版本 2.0.0 不代表 Release 标签。

![模拟盘 Dashboard 预览](docs/assets/dashboard.png)

截图来自全新公开克隆的空账本，展示可视化看板、委托记录和风控审计入口；运行时数据库与历史记录不会随仓库发布。该截图拍摄于策略工坊上线前（尚未体现策略平台界面），取舍理由与更新前提见 [`docs/DEMO.md`](docs/DEMO.md)。

## 为什么做这个项目

很多通用 backtesting / paper-trading 框架默认“信号出现后就能成交”，但 A 股的实际可交易性受市场规则和数据质量强约束。这个项目的目标不是再做一个选股脚本，而是提供一套**可以复现、拒绝错误成交、事后可审计**的 A 股研究执行层。

- **A 股规则是一等约束**：股票 T+1、100 股整手、涨跌停、停牌、佣金、印花税和滑点都进入下单校验。
- **行情默认 fail-closed**：陈旧、缺失或覆盖率不足的行情不会被静默拼接成“实时价格”；证据不足时宁可拒绝模拟成交。
- **决策链可回放**：信号 → 风控 → 订单 → 成交 → NAV → 扫描结果全部留痕，可定位历史行为偏差。
- **并发写入有明确语义**：运行时租约、heartbeat、fencing token 与 CAS 防止重复订单、僵尸写者和并发覆盖。
- **研究与正式撮合隔离**：自适应、新闻和 LLM 相关能力首先作为影子证据流，不因为研究模块异常而放宽正式交易门禁。

## 策略：内置模板 + 用户自定义

仓库完整保留五套**内置策略模板**，它们与用户自定义策略统一纳入策略注册表、自适应研究、历史回放和审计展示。内置模板是平台自带的基线，不是平台的全部；可以复制后修改，也可以在策略工坊里从零创建自己的声明式策略。

| 内置模板 | 状态 | 风格 | 主要用途 |
|---|---|---|---|
| `tq_breakout` | active | 强势突破 | 放量、资金共振后的短周期突破候选 |
| `trend_pullback` | active | 趋势回调 | 中期上行趋势中的缩量回调观察与正式模拟 |
| `sector_rotation` | active | 板块轮动 | 热点板块排名、资金共振与个股相对强度轮动模拟 |
| `reported_profit_breakout` | active | 质量突破 | 财报/预告驱动的业绩突破评分与正式模拟 |
| `main_force_top10` | active | 主力资金 | 跟踪主力净流入靠前、通过实时确认的候选 |

**谁参与某一轮扫描？** 不是"注册表里 active 就算"。执行层的权威口径是**当前周期的快照**：

```text
本轮参与者 = 周期创建时冻结的 enabled_strategies
           ∩ 绑定到该周期的账户
           − 生命周期为 paused 的策略
```

因此：激活一个策略只让它**有资格进入下一周期**；已在运行的周期不会被自动改写；把参与集合清空是合法的 Idle 模式（见上文六条语义）。用户策略默认从**试点阶段**（25% 预算）起步，验证后再人工晋升额度。

用户策略与内置模板共用撮合、共享资金池、风控与审计基础设施，但拥有独立的候选车道、仓位席位和退出规则；任何定义都不会被自动删除、重命名或隐式替换。已有历史周期不会被启动迁移强行重分配。

**明确不做：** 实盘路由、杠杆、做空，以及普通股票的 T+0 当日买卖回转。

## 策略平台能力

| 能力 | 说明 | 文档/代码入口 |
| --- | --- | --- |
| 内置模板 | 五套可运行基线，可复制后修改 | `backend/strategy_registry.py` |
| 声明式 DSL | 白名单节点/字段/指标，无代码执行通道，离线 fail-closed 求值 | `backend/strategy_dsl_schema.py`、`backend/strategy_dsl_evaluator.py` |
| 不可变版本 | 每次改动追加版本 + structure checksum；周期绑定自己使用的版本，后续编辑不会改写历史证据 | `backend/strategy_registry.py` |
| 生命周期 | 草稿 / 已验证 / 运行中 / 已暂停 / 退役中 / 已归档，状态机 + 合法边校验 | [`docs/STRATEGY_PLATFORM.md`](docs/STRATEGY_PLATFORM.md) |
| RuntimeContext | 版本、DSL、风险画像、执行画像、资金阶段的**同一份** pinned 运行契约 | `backend/strategy_runtime.py` |
| 风险编译 | 指纹 → 画像 → 强制收紧（`min(生产现值, 模板值)`），审计每键 before/after | `backend/strategy_risk_enforcement.py` |
| 动态分配 | N 策略共享池：席位/预算/敞口按有效权重分配，Σ 分配恒 ≤ 池上限 | `backend/paper_allocation.py` |
| 订单意图 / 执行计划 | 策略只表达意图；计划、复核、落库由中央执行层统一，自动与手动同一口径 | `backend/order_intent.py`、`backend/execution_planner.py` |
| 共享资金池 + T+1 感知 | 周期拥有经济资本；T+1、整手、涨跌停、停牌、费用/滑点在下单前校验 | `backend/paper_trading_rules.py`、`backend/paper_quote_policy.py` |
| 确定性回放 | 信号→风控→订单→成交→NAV 全链路留痕，golden replay 逐字节比对 | `backend/test_production_path_golden_replay.py` |
| 自进化 / Champion-Challenger | 证据→提案→A/B 验证→非对称风险门→影子挑战者→晋升；AI 不能自行放宽风险 | [`docs/EVOLUTION_ARCHITECTURE.md`](docs/EVOLUTION_ARCHITECTURE.md) |
| 策略工坊 Web UI | 定义、DSL、预览、版本、生命周期、克隆；浏览器 E2E 覆盖关键旅程 | `frontend/src/features/strategies.js`、`frontend/e2e/specs/` |

## 核心能力

### 1. 撮合与资金模型

- 共享资金池按策略做预算归因，包含席位上限、单票预算、公平性保护和资金硬上限。
- 最小建仓金额按 `周期本金 × 共享池敞口上限 ÷ 股票持仓上限 × 60%` 动态计算，并向下取整到 100 元；例如 10 万周期、82% 敞口、15 席上限时为 ¥3,200。剩余 40% 由风控机制根据趋势确认、回撤保护和追加仓位条件动态决定，不再固定使用 ¥10,000。
- 资金预留与正式扣款分离，配合 SQLite savepoint，避免并发扫描下重复占用资金。
- 下单前校验证券权限、行情新鲜度、涨跌停/停牌、整手、滑点和可买数量。

### 2. 分层风险状态机

- 每笔买卖先经过独立风控层，产生结构化的 `approved`、`rejected`、`deferred_capacity`、`downside_warning` 等结果。
- 下行保护采用分段减仓 → 独立扫描确认 → 全清的状态机，避免同一理由反复卖出。
- 支持硬止损、移动止损、阶梯止盈、质量轮换、容量压缩和集中度守卫。
- 风控原因使用稳定标记和审计台账，避免仅依赖人类可读文案做历史归因。

### 3. 多源行情与数据质量

- 公开行情源采用多源抓取和独立核验。
- 实时价格具有明确时间戳和来源；缓存旧价不会伪装成实时行情。
- 全市场快照设置覆盖率门禁，数据不完整时阻断依赖全市场截面的正式路径。
- 数据源故障优先降级信号丰富度或阻断对应路径，而不是降低风险门槛。

### 4. 并发与调度

引擎统一支持：

`auction / open / risk / intraday / close / weekly-review`

六类 slot。

一键启动默认启用内置的 **3 分钟盘中模拟盘调度器**；如果部署侧已经有完整宿主机计划任务，可关闭内置调度，避免重复执行。运行时租约 + heartbeat + fencing token 保证同一批次只由有效写者推进。

### 5. 审计与研究

信号、候选、风险决策、订单、成交、持仓、NAV 和逐轮扫描结果都会形成可追踪证据。`adaptive_*`、`news_learning`、`neural_shadow`、`dual_ai_tuner` 等研究模块保存独立影子观测，不直接绕过正式撮合路径。

## 项目结构

```text
backend/
  paper_trading.py      撮合 / 风控 / 审计 / 资金 / slot 调度主引擎
  strategy_registry.py  策略身份 · 不可变版本 · 生命周期状态机
  strategy_service.py   策略应用服务（HTTP 与领域之间唯一边界）
  strategy_dsl_*.py     声明式 DSL 的规范/校验与离线求值
  strategy_runtime.py   RuntimeContext：版本 + 风险/执行画像 + 资金阶段
  strategy_risk_*.py    风险指纹 / 风险画像 / 强制收紧（只能更严）
  order_intent.py       策略→执行器的订单意图契约（拒绝数量越权）
  execution_planner.py  中央执行计划器：计划 / 复核 / 落库
  paper_allocation.py   共享池席位与预算分配（纯计算，N 策略）
  evolution_*.py        自进化闭环：提案 / 落地 / A/B 验证
  strategy_champion.py  Champion-Challenger 影子与晋升
  manual_orders.py      手动下单链（预览 / 提交 / 撤单 / 待单推进）
  dashboard_queries.py  看板读模型（overview / portfolio / activity）
  paper_runner.py       auction/open/risk/intraday/close/weekly-review CLI
  data_fetcher.py       行情、快照、公告与数据质量
  entry_timing.py       入场时机状态机
  decision_engine.py    候选车道、因子判定与实时确认
  strategies.py         内置策略与公开研究策略定义
  adaptive_*.py         影子观测与参数研究
  news_learning.py      新闻证据研究
  api_*.py              HTTP API
  main.py               FastAPI + Web 看板入口
  test_*.py             回归测试（含离线确定性演示 golden replay）
frontend/src/           前端 ESM 模块（入口 app.js + 模块地图见 src/README.md）
frontend/styles/        样式片段；frontend/dist/ 是构建产物（提交物）
docs/                   运行手册、策略平台、自进化、设置 PRD、测试矩阵、发布说明、结构地图
docs/archive/           已完成的历史计划与快照（只作追溯，不代表当前设计）
.github/workflows/       GitHub Actions CI（含离线测试层与浏览器 E2E）
Dockerfile              应用镜像
docker-compose.yml      本地/单机容器运行
```

## 一键启动

要求：**Python 3.11+**。Docker 可选。

### Windows

```powershell
.\start.ps1                 # 自动选择 Docker；失败后回退本地模式
.\start.ps1 -Local          # 强制本地 Python
.\start.ps1 -Docker         # 强制 Docker Compose
.\start.ps1 -Port 8601      # 自定义端口
.\start.ps1 -NoBrowser      # 不自动打开浏览器
.\start.ps1 -NoScheduler    # 已有外部调度器时关闭内置调度
```

也可以直接双击 `start.bat`。

### Linux / macOS

```bash
chmod +x start.sh
./start.sh
./start.sh --local --port 8601 --no-browser
./start.sh --no-scheduler
```

启动 Web/API 后访问 `http://localhost:8600`。启动服务本身不会自动创建新的模拟交易周期。

### 设置中心

打开看板顶部的“设置中心”即可调整四类运行参数：

- **模拟盘与资金**：默认启动金额、15/30/60/90/180 个交易日或长期的周期长度、下一周期启用的策略（候选由策略注册表提供，可全部取消 → Idle 周期）。
- **仓位与风控**：共享池席位/敞口、单票最大金额（0 为按策略权重自动计算）、动态最小建仓席位利用率。
- **策略参数**：按注册表渲染的策略级运行参数（内置模板与用户策略各自）——风格、最大席位、单票权重和策略敞口。
- **AI 与自进化**：供应商、审阅/有界调参开关、掩码 Key 状态和收盘学习间隔。

**边界**：设置中心只决定**运行参数**与**下一周期参与集合**；策略的身份、DSL、版本和生命周期在**策略工坊**里编辑，设置中心不提供第二套 DSL 编辑器。启用某策略只让它有资格进入**下一**周期，不会改写当前周期快照。

默认值与生效时机见 [`docs/SETTINGS_PRD.md`](docs/SETTINGS_PRD.md)。资金、周期和策略集合在下个新周期初始化；共享池风控在下一次扫描读取。每次保存都会经过后端白名单校验并写入设置审计，AI Key 不会在页面、日志或仓库中回显明文。

完整的 **clone → 安装依赖 → 看板 → 数据准备 → 手动扫描** 流程见 [`docs/RUNBOOK.md`](docs/RUNBOOK.md)。

## 离线确定性演示（ASTOCK_DEMO=1）

不想先接真实行情？设置环境变量 `ASTOCK_DEMO=1` 再启动，系统会在首次启动时注入一套**完全合成的演示数据**，无需网络、无需 API key、无需任何凭据：

```bash
# Linux / macOS
ASTOCK_DEMO=1 ./start.sh --local --no-scheduler
# Windows PowerShell
$env:ASTOCK_DEMO="1"; .\start.ps1 -Local -NoScheduler
```

`--no-scheduler` / `-NoScheduler` 关闭内置 3 分钟盘中调度器：演示账本是静态叙事，不应被盘中扫描改写或触发联网行情刷新。

演示数据包含 10 只合成标的（6009xx）与一条完整叙事账本，看板每个页面都有内容可看：

- **信号 → 风控 → 订单 → 成交 → NAV** 的全链路审计证据
- 一笔正常买入成交、一笔 **T+1 当日卖出被拒**、一笔**陈旧报价拒单**、一笔**硬止损卖出**、一笔**涨停价买入**、持仓质量复盘（卖出/继续持有）与 5 日净值曲线
- 多账户共享资金池、席位容量与策略级风控参数的运行快照

特性：

- **幂等**：重复启动不会重复注入（以 `paper_audit event='demo_seeded'` 为标记）；`ASTOCK_DEMO_FORCE=1` 可强制重建。
- **结构确定**：订单 / 成交 / 风控决策 / 持仓 / NAV 等结构化内容完全确定（时间戳与日期随运行当天取值），CI 以 golden replay 逐字节比对结构摘要，防止叙事漂移（见 [`docs/DEMO.md`](docs/DEMO.md)）。
- **生产安全**：不设置该环境变量时，注入逻辑完全不触发。

## 本地开发与验证

```bash
python -m venv .venv
# 激活对应平台的虚拟环境后：
python -m pip install -r requirements.lock

python -m uvicorn backend.main:app --port 8600
python -m unittest discover -s backend -p "test_*.py" -v
```

手动运行一个 slot：

```bash
cd backend
python paper_runner.py --slot open
```

`requirements.txt` 声明允许的依赖范围，`requirements.lock` 固定可复现安装版本。修改依赖范围后必须重新生成并提交锁文件。

GitHub Actions 会在 **Python 3.11 / 3.12** 上安装锁定依赖并执行后端回归，同时运行 Ruff 静态检查、锁文件一致性检查、pip-audit 已知漏洞审计、前端构建一致性校验、**Playwright 浏览器 E2E**（策略工坊关键旅程：创建草稿 / 生命周期 / 设置集成 / 版本 / 克隆 / 深链接 / 响应式与可访问性）和 Docker 冒烟。Docker 冒烟阶段以 `--network none` + tmpfs 缓存目录运行**全量离线测试层**：回归用例默认禁止联网，联网用例须显式设置 `ASTOCK_NET_TESTS=1` 才运行（分层规范见 [`CONTRIBUTING.md`](CONTRIBUTING.md)）。测试覆盖撮合门禁、point-in-time 数据、行情新鲜度、风险审计、并发租约、策略入场与策略平台不变量（版本不可变、草稿删除边界、周期所有权、动态分配性质）、确定性演示回放与共享资金行为，完整场景矩阵见 [`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md)。

## Docker

本地 Compose 只把宿主端口绑定到环回地址（`127.0.0.1:8600:8600`），容器内部 Uvicorn 仍监听 `0.0.0.0` 以配合端口发布、健康检查与反代。

HTTP 控制面有统一的操作员边界（PR-2）：`POST`/`PUT`/`PATCH`/`DELETE` 需要 operator
凭据，`GET` 只读、无需凭据。三种模式：

- **未配置 token**（local-only）：仅"本机环回地址 + 本地 `Host`"可写，其余写请求
  `403`（`Host` 必须是 `localhost` 或环回 IP 字面量，用以挡住 DNS rebinding）；
- **配置合法 token**（>= 24 字符，authenticated）：任何客户端的写请求都必须带
  标准 Authorization 头（Bearer 方案，值形如「Bearer <凭据>」），localhost 无豁免；
- **token 非法**（misconfigured）：所有写请求 `503`。

**使用自定义主机名（内网域名等）必须配置 token**，让边界进入 authenticated 模式。
反向代理转发 `Host` 时必须保留 `host:port`（nginx 用 `$http_host`，不要用会丢端口的
`$host`），否则浏览器 `Origin` 里的端口与后端推断的默认端口不一致，合法的同源写请求
会被判 `cross-origin` → `403`。

**不存在关闭边界的开关。** 先准备密钥：

```bash
# 生成强随机 token（>= 24 字符），写入 .env（已被 gitignore，勿提交）
python -c "import secrets; print('ASTOCK_OPERATOR_TOKEN=' + secrets.token_urlsafe(32))" >> .env
```

浏览器端不需要手工操作：打开看板 → **设置中心 → 操作员授权** → 粘贴凭据 →
点「本标签页解锁」。凭据只保存在该标签页的 `sessionStorage`，关闭标签页即失效；
写操作会自动带上标准 Authorization 头（Bearer 方案），只读请求绝不携带。细节见
[`SECURITY.md`](SECURITY.md)。

```bash
docker compose up -d --build
docker compose logs -f app
```

停止但保留模拟盘数据：

```bash
docker compose down
```

重置当前容器实例的模拟盘数据：

```bash
docker compose down -v
```

Docker 使用仓库专用命名卷，不会自动连接其他实例的私有运行时数据库。

## 开源维护

项目使用 **MIT License**，欢迎可复现的 bug、边界条件、数据源适配和小范围 PR。

当前公开 roadmap（欢迎在 [Discussions](https://github.com/daviesjoin-afk/astock-paper-trading/discussions) 交流，good-first-issue 子任务见对应主任务）：

- [#1 扩展行情数据源适配与故障降级](https://github.com/daviesjoin-afk/astock-paper-trading/issues/1)
- [#2 设计可插拔策略接口与策略回放规范](https://github.com/daviesjoin-afk/astock-paper-trading/issues/2)（策略平台与声明式 DSL 已落地，见 [`docs/STRATEGY_PLATFORM.md`](docs/STRATEGY_PLATFORM.md)；issue 保留用于跟踪剩余缺口）
- [#3 补充回测、纸面撮合与审计回放验证](https://github.com/daviesjoin-afk/astock-paper-trading/issues/3)（场景矩阵与缺口见 [`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md)）

提交代码前请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)。版本变化见 [`CHANGELOG.md`](CHANGELOG.md) 和 [GitHub Releases](https://github.com/daviesjoin-afk/astock-paper-trading/releases)。安全问题请通过 [私密漏洞报告](https://github.com/daviesjoin-afk/astock-paper-trading/security/advisories/new) 提交，不要公开包含敏感信息的复现材料。

## 可选 LLM 研究能力

基础模拟盘**不依赖 LLM**。如果需要启用可选顾问/影子研究能力，可复制 `.env.example` 为 `.env` 并配置文档中的环境变量。真实密钥不会进入仓库。

LLM 输出属于研究证据，不被当作确定事实或收益保证，也不会绕过正式交易风控。

## 安全与隐私边界

请勿在 Issue / PR 中提交：

- API key、token、Cookie 或 `.env`；
- 服务器地址、SSH 凭据或账户密码；
- 真实证券账户、真实持仓或未脱敏运行数据库；
- 含私人信息的日志或截图。

`.env`、`data_cache/`、`reports/` 等运行时内容均应保留在本机或部署环境。

## 风险声明

本项目仅用于**模拟交易和量化研究**。实时行情来自公开接口，不代表交易所完整盘口；撮合包含滑点和成交假设。模拟盘、回测、影子观测和历史结果均不能视为未来收益保证，也不构成投资建议。

## License

MIT，详见 [`LICENSE`](LICENSE)。
