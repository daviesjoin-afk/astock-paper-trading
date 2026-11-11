# AStock V2：单账户、稳健 / 激进

用 Rust 重写的纯模拟盘。你只输入一次本金，系统把这笔钱分给两个策略：

- **稳健**：趋势向上且波动较低时买入，单票最多 15%，现金至少留 10%；
- **激进**：5 日上涨且突破前 20 日高点时买入，单票最多 30%，现金至少留 5%。

初始各 50%。之后只在有两个完整交易日的行情后，按“收益 − 回撤”评分，
每次最多调整 10 个百分点，权重始终在 20%–80% 之间。调仓只调整两个策略的
可用现金预算，不会把稳健已持有的股票转给激进，也不会伪造真实资产占比。

## 它做什么

- 一个账户、一个本金，没有“实验”或第二个账户入口；
- 收盘价驱动、T+1、100 股整手、费用、缺失价格整日拒绝；
- 每个交易日一笔 SQLite 事务：结算、下单、成交、估值、调仓全成或全不成；
- 同一交易日重复推进返回原成交，不重复交易；
- 纯模拟，不接券商、不下真实单。

## 快速开始

前置：Rust 1.99.1（见 `rust-toolchain.toml`）、Node.js 20+、Python 3.11+。

```bash
cd v2
cargo test --locked --all-targets
cargo build --release --locked

cd web
npm ci
npm run build
```

本机试跑（当前目录为 `v2/`）：

```bash
export ASTOCK_V2_DB=./v2.sqlite3
export ASTOCK_PRICE_SETS_DIR=./data/price-sets
./target/release/astock-v2
```

API 默认在 `http://127.0.0.1:8765`（本机开发）。服务器重写版沿用公开
8600 入口，内核改为 18765；切换前先停掉旧服务，详见 `deploy/README.md`。

## 行情数据

服务自身不联网。行情先通过离线命令生成“已审核价格集合”：

```bash
python data_sources/a_stock_data.py fetch \
  --symbols 600000.SH,000001.SZ \
  --days 2026-09-01,2026-09-02 \
  --out ../work/prices.csv --report ../work/prices.report.json

bash scripts/fetch_and_publish_reviewed.sh \
  --symbols 600000.SH,000001.SZ --days 2026-09-01,2026-09-02 \
  --set-id cn-close-20260902 --pack cn-a-share --review
```

`a-stock-data` 适配器同时核对腾讯日线与通达信盘后包，并检查新浪复权因子。
任一股缺日、停牌、零成交、价格或成交量不一致，整批拒绝，不会写出半份数据。
细节见 `data_sources/README.md`。

生产目录只接受 `trust=reviewed`。演示/测试可用 `ASTOCK_ALLOW_SYNTHETIC=1`
在隔离目录读取合成数据，不得把合成数据放进生产价格目录。

## 页面与接口

页面只有：本金入口、总资产、稳健/激进两张卡、持仓、成交、调仓记录、
“推进到最新交易日”。

```text
GET  /v2/health
GET  /v2/account
POST /v2/account                    {"initial_cash_cents":10000000}
POST /v2/account/run                {} 或 {"day":"YYYY-MM-DD"}
GET  /v2/account/positions
GET  /v2/account/trades
GET  /v2/account/rebalances
GET  /v2/prices/coverage
```

没有实验、回放、研究复现或进化事件接口。

## 验证

```powershell
# Rust
cargo test --locked --all-targets

# 数据源（离线 fixture）
python -m unittest discover -s data_sources -p 'test_*.py' -v

# 浏览器（需先构建 release 二进制和 web/dist）
cd web
npx playwright install chromium
npx playwright test

# 隔离的 HTTP / SQLite 冒烟
pwsh scripts/rewrite_smoke.ps1
```

## 备份与部署

```bash
python scripts/db_tool.py backup --db /var/lib/astock-v2-rewrite/v2.sqlite3 \
  --out /var/backups/astock-v2-rewrite/v2-$(date +%F).sqlite3
```

Oracle 单机部署、nginx、cron 与回滚见 `deploy/README.md`。
旧 V2 数据库（`PRAGMA user_version > 0`）会被明确拒绝，保留原文件并用新
`ASTOCK_V2_DB` 启动；本项目不尝试就地迁移。

接手人请从 [交接文档](docs/HANDOFF-2026-11-11.md) 开始，里面包含当前部署状态、
日常命令、备份恢复、验收证据和未完成项。
