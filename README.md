# A 股量化模拟盘引擎

本地优先的 A 股**模拟盘**量化研究引擎。当前公开运行版本启用两套策略（`tq_breakout`、`main_force_top10`），共用一套撮合、分层风控、资金分配与审计引擎；覆盖竞价预选、开盘审批、盘中风控与日内调仓、收盘信号生成与周度复盘全流程，并带 Web 看板与自进化观测。系统**不连接任何券商、不触碰真实资金**，成交全部为带全路径交易约束的模拟成交。

本仓库只包含源码与架构说明，不含部署编排、运维脚本、任何主机信息与运行时数据。

## 当前阶段边界

- **纯模拟**：撮合发生在引擎内部，无券商接口、无实盘路由；模拟盘净值仅供策略与风控研究。
- **A 股规则硬门禁**：T+1 锁定、最小 100 股整手、跌停不可卖/涨停不可买/停牌不可成交、佣金与印花税、买卖滑点全路径校验；异常行情（陈旧/缺失报价）默认拒绝而非放宽成交。
- **盘中自动运行由外部调度触发**：引擎按 `auction / open / risk / intraday / close / weekly-review` 六类 slot 工作；交易日盘中 `intraday` 周期扫描，收盘后固化候选与快照。仓库不附带调度表，由部署侧自行编排。
- **写入串行化**：多进程扫描通过「全局运行时租约 + fencing token + 心跳续期 + 完成 CAS」保证同一时刻只有一个写者；进程被杀或挂死可被自动回收，不产生重复订单、不留幽灵租约。
- **不做的事**：不加杠杆、不做空、不做 T+0 回转（当日买入当日卖出）、不追一字板、不让“单只强势但板块不共振”的标的进入板块轮动仓位。
- **可回放**：信号、风控决策、订单、成交、NAV、每轮扫描结果全部留痕，可按时间逐轮回放审计，用于定位历史行为偏差。

## 当前启用策略

新周期只会创建并运行以下两套独立策略账户，各有独立的入场车道、席位上限与退出规则。历史周期中的旧策略账户和审计记录保留可读，但不会再进入新周期或调度运行：

| 账户 | 风格 | 定位 |
|---|---|---|
| `tq_breakout` | 强势突破 | 放量突破、主力资金共振的短线打板/强势候选 |
| `main_force_top10` | 主力资金 | 主力净流入居前标的的跟随 |

模拟周期设置支持总资金池、周期长度（1–365 个交易日）和交易风格：激进、宽松、平缓。风格只调整有界的软阈值与仓位节奏，T+1、涨跌停、行情新鲜度、实时覆盖率、席位/资金硬上限和止损门禁始终有效。

## 核心设计

### 撮合与资金模型

- 共享资金池按账户做预算归因（席位最大持仓数 + 单票预算），含公平性保护与地板保护，防止策略间互相挤占；资金预留与扣款分离，配合 SQLite savepoint 保证并发下单不双扣。
- 仓位计算做价格感知（涨跌幅/停牌/滑点后的可买量），预算缩水不静默放大成一手空转仓。

### 分层卖出状态机

同一只持仓可能被多重退出规则触发，按优先级依次评估（权限 → 容量 → 质量轮换 → 集中度守卫 → 常规退出）：

- **下行守卫三道防线**：预警一次性减仓 → 连续两次独立扫描确认（按级别去重）→ 跌破确认全清；杜绝“同一理由每分钟重复卖”。
- **硬止损**：首触止损线先减一部分，后续仍在线下则全清；崩盘形态直接全清。
- **止盈**：移动止损 + 阶梯止盈（跳空越档单轮连续消费，不拖到下一轮）。
- **轮换退出**：席位已满时按质量评分择强换股、按集中度轮换、按容量压缩超额持仓；退出原因结构化落单。

### 风控与数据

- 每笔买卖先过独立风控决策层，形成 `rejected / approved / deferred_capacity / downside_warning` 等结构化决策并留痕；`risk_decisions` 与 `audit` 双台账可交叉核对。
- 实时行情多源抓取（东财 ulist、腾讯、新浪独立核验），**价格防陈旧**：宁可标记缺失/拒绝成交，也不把缓存旧价拼到实时时间戳上制造“看似通过的成交”；全市场快照带覆盖率门禁（不足 4,000 只实时价即熔断放行）。

### 并发与状态机

- `run_slot` 统一受理六类 slot；租约过期/心跳丢失自动回收僵尸，fencing token 保证后写者不能覆盖先写者。
- 入场时机 `entry_timing` 状态机管理候选（观察 → 连续确认 → 放行/失效），避免单次脉冲信号直接成交。

### 自进化观测（影子，不直接改实盘）

`adaptive_*`、`news_learning`、`neural_shadow`、`dual_ai_tuner` 等在盘中/盘后保存观测与影子权重，用于参数研究；观测写入不影响真实撮合路径，只作为独立证据流。

## 目录结构

```text
backend/
  paper_trading.py      模拟盘撮合/风控/审计主引擎（租约、风控决策、下单、NAV、slot 调度）
  paper_runner.py       slot 命令行入口（auction/open/risk/intraday/close/weekly-review）
  data_fetcher.py       行情/快照/公告多源数据层（本地缓存 + 覆盖率门禁）
  entry_timing.py       入场时机状态机
  decision_engine.py    车道/因子判定与实时确认
  strategies.py         策略定义与候选车道
  adaptive_*.py         自进化观测/调参（影子）
  news_learning.py      新闻舆情学习
  api_*.py              HTTP API
  main.py               FastAPI 入口（伺服前端 + 看板/操作端点）
  test_*.py             单元测试（unittest）
frontend/               Web 看板（账户/持仓/风控/审计回放；静态资源）
Dockerfile              容器镜像（API 与应用镜像，不含编排）
```

## 一键启动

仓库根目录提供跨平台启动脚本。脚本会优先尝试 Docker Compose；Docker 不可用时自动切换到本地 Python 虚拟环境，首次运行自动创建 `.venv` 并按 `requirements.txt` 安装依赖。启动成功后会打开 Web 看板；脚本只启动服务，不会自动创建模拟交易周期。

Windows：双击 [start.bat](start.bat)，或在 PowerShell 中运行：

```powershell
.\start.ps1                 # 自动选择 Docker，失败后使用本地模式
.\start.ps1 -Local          # 强制本地 Python 模式
.\start.ps1 -Docker         # 强制 Docker Compose（端口固定为 8600）
.\start.ps1 -Port 8601      # 使用自定义端口时自动使用本地模式
.\start.ps1 -NoBrowser      # 不自动打开浏览器
```

Linux/macOS：

```bash
chmod +x start.sh
./start.sh                  # 自动选择 Docker 或本地模式
./start.sh --local --port 8601 --no-browser
```

启动脚本不包含密钥、服务器配置或运行时记录；`.env`、`.venv`、`data_cache/` 与 `reports/` 均只保留在本机。Docker 模式使用仓库专用的命名卷，从空账本开始，不会连接其他实例的数据卷。

## 本地开发

Python 3.11+，从仓库根目录执行。以下是手动启动方式，适合开发和调试。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 1) 启动 Web 看板与 API（http://localhost:8600）
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8600

# 2) 后端单元测试（unittest 风格，离线核心逻辑可跑）
.\.venv\Scripts\python.exe -m unittest discover -s backend -p "test_*.py" -v
```

手动触发一轮模拟盘扫描（slot：auction/open/intraday/risk/close/weekly-review）：

```powershell
cd backend
.\.venv\Scripts\python.exe paper_runner.py --slot open
```

可选 LLM 特性（自适应调参证据/顾问）：复制 `.env.example` 为 `.env` 并填入
`DEEPSEEK_API_KEY` 与 `LLM_ADVISOR_ENABLED=1`；不配置则以基础模式运行。

**完整「clone → 装依赖 → 看板 → 补数据 → 跑扫描」步骤见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。**

构建 API 镜像（仅应用镜像，不含多容器编排与宿主调度）：

```powershell
docker build -t astock-codex:latest .
```

## Docker 运行

仓库同时提供单容器镜像和 Compose 配置。仓库不包含任何本机模拟成交、持仓、审计或自进化记录；首次启动会在新的 `astock_repo_data` / `astock_repo_reports` 卷中从空账本开始。之后这些卷会持久化当前实例的运行数据，容器重建不会丢失这些数据，也不会自动连接旧的私有 `astock_data` / `astock_reports` 卷。

```bash
# 构建并启动 Web 看板/API
docker compose up -d --build

# 查看启动日志
docker compose logs -f app

# 打开 http://localhost:8600
```

停止服务但保留数据：

```bash
docker compose down
```

连同模拟盘运行数据一起重置（不可恢复，请确认后执行）：

```bash
docker compose down -v
```

也可以不使用 Compose 直接运行镜像：

```bash
docker build -t astock-codex:latest .
docker run --rm -p 8600:8600 -v astock_repo_data:/app/backend/data_cache astock-codex:latest
```

容器内默认只启动 API/Web 看板，不会自动定时触发交易扫描；需要扫描时通过 `docker compose exec app` 手动执行 `backend/paper_runner.py`，或在部署侧配置调度器。

## 风险结论

模拟盘成交与回测结果不能视为盈利保证。实时行情来自公开接口（非真实盘口深度），撮合采用滑点/成交假设，样本受 A 股当前成分与停牌处理影响；自进化与新闻观测均不构成交易建议。策略历史表现不代表未来收益，本项目不构成任何投资建议。

## 项目状态与验证

- 当前仓库包含 25 个后端测试模块，GitHub Actions 在 Python 3.11/3.12 上自动运行测试。
- 本仓库不发布未经验证的收益率或实盘运行结果；测试、回放和模拟盘运行数据应以对应提交或本地环境中的可复现记录为准。
- 当前版本为研究基础设施，后续重点包括：补充公开脱敏演示、扩展数据源故障回归、完善策略回放报告和持续维护文档。

许可证：MIT，详见 [LICENSE](LICENSE)。贡献请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。
