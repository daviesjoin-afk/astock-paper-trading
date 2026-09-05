# 本地直跑手册（Local Runbook）

> 目标：把本仓库 clone 到一台**能访问公网行情接口**的机器后，不依赖任何部署配置即可跑起来 —— Web 看板、模拟盘引擎、单测。
> 本模式为**单机研究模式**：不自动定时下单，扫描由你手动触发（或自行挂系统定时器）。

## 0. 前置

- Python 3.11+
- 可访问公网（A 股行情/码表/日K 来自公开接口：东方财富/腾讯/新浪）
- 建议 8 GB 内存以上（pandas 全市场数据处理）

## 1. 安装

```bash
git clone <本仓库地址> astock-paper-trading && cd astock-paper-trading
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.lock
```

可选：`cp .env.example .env` 后按需填写（见下）。不设 `.env` 也能以基础模式运行。

## 2. 启动 Web 看板

```bash
# 仓库根目录执行
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8600
```

打开 http://127.0.0.1:8600

首次访问数据相关页面时，引擎会**自动初始化**：
- 在 `data_cache/` 下创建 SQLite（`paper_trading.sqlite3`，建表 + 两套启用策略账户 + 初始资金周期；旧策略历史记录保留但停用）
- 若本地还没有股票池 `data_cache/universe.json`，会**懒构建**全 A 码表（联网，约 1~3 分钟）
- 前端静态页由 API 伺服（`/assets`、`/`）

## 3. 初始化行情/因子数据（跑扫描前必读）

模拟盘扫描（`open` / `intraday` 等 slot）依赖本地缓存的日K 与候选因子。
首次 clone 后是空库，按下面顺序补数据（均需联网，耗时取决于网络与全市场范围）：

```bash
# 3.1 预建股票池（可选：上面懒构建已建的跳过）
python -c "import sys; sys.path.insert(0, 'backend'); import universe as U; U.build_universe(); print('universe built:', U.UNIVERSE_PATH)"

# 3.2 补齐全市场日K（分批增量；workers=2 每轮约 13 分钟，可重复执行续跑）
python backend/history_recovery_runner.py --workers 2 --max-seconds 780

# 3.3 生成当日候选池与因子快照（需 3.2 已有当日日K）
python backend/selection_runner.py --slot daily
```

> 若只是先体验看板/框架，可跳过 3.2/3.3；此时打开看板、执行单测均正常，仅扫描类 slot 会因缺数据被拒绝或降级。

## 4. 手动触发模拟盘扫描

slot 语义：
| slot | 时机 | 作用 |
|---|---|---|
| `auction` | 09:25 后 | 集合竞价预选（快照） |
| `open` | 09:30 后 | 开盘审批 + 首次候选 |
| `intraday` | 交易时段周期性 | 盘中做 T + 下行风控 + NAV |
| `close` | 15:00 后 | 收盘信号生成 + 收盘 NAV |
| `risk` | 任意 | 手动风控扫描 |
| `weekly-review` | 周五收盘后 | 周度复盘 |

```bash
cd backend
python paper_runner.py --slot open
python paper_runner.py --slot intraday
python paper_runner.py --slot close
```

交易日自动化的最简方式（类 cron）示例：

```cron
*/3 9-11,13-14 * * 1-5  cd <仓库绝对路径>/backend && .venv/bin/python paper_runner.py --slot intraday
```

> slot 是**幂等 + 租约保护**的：并发/重复触发只会有一个执行，不会重复下单。

## 5. 运行单元测试

```bash
python -m unittest discover -s backend -p "test_*.py" -v
```

离线可跑的核心逻辑单测（撮合/风控状态机/租约/估值兜底）不需要行情数据；少量用例需联网/数据会 skip。

## 6. 可选：LLM / AI 调优特性

基础模式默认关闭所有 LLM 能力。要开启 AI 可选特性（自适应调参证据、deepseek 顾问等）：

```bash
cp .env.example .env
# 编辑 .env，填入：
#   DEEPSEEK_API_KEY=sk-...
#   LLM_ADVISOR_ENABLED=1
```

不填不影响模拟盘核心。

## 7. 运行时数据与重置

- 全部运行时数据位于 `data_cache/`（已 gitignore，不入库）：SQLite、`universe.json`、日K 缓存、快照缓存；仓库克隆和 Docker 首次启动均从空账本开始，自进化样本不会从维护机迁移
- **重置**：停服务后删除 `data_cache/` 重启即恢复出厂（重新初始化 + 重新补数据）
- Compose 默认使用新的 `astock_repo_data` 和 `astock_repo_reports` 卷；不要把维护机已有的 `astock_data` / `astock_reports` 卷挂载到开源仓库实例，除非明确需要继续使用那套历史账本

## 8. 与"生产部署"的差异（本仓库范围外）

仓库不含：宿主 cron 调度文件（示例见第 4 节）、多容器 worker 编排、服务器主机配置。单机直跑即把本手册当"调度者"；如需自动值守，把第 4 节命令挂到系统定时器即可。

## 常见问题

- **端口占用**：换 `--port 8601`
- **universe 构建失败**：多为网络受限，重试或检查能否访问公开行情域名
- **scans 报缺数据**：先做第 3 步，确认 `data_cache` 下日K/候选已生成
- **想只看 UI**：无需 `.env`、无需 3.2/3.3，直接到第 2 步
