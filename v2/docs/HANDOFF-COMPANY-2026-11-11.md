# 公司电脑接续入口（2026-11-11）

本文件是本轮代码与真实历史研究的接续入口。分支为 **`codex/v2-store-boundaries`**，现有 [PR #492](https://github.com/daviesjoin-afk/astock-paper-trading/pull/492) 用于远端检查。本轮只涉及 V2 模拟盘，不部署、不合并、不修改交易参数；`risk_limits_v1.md` 不属于本次交付。

`HANDOFF-2026-11-11-V3.md` 等旧文档保留为历史快照，其中“本机无数据”“尚未完成全期”和旧收益表均由本文件及本轮研究记录取代。以公司电脑拉到的本文件所在提交为代码版本，不使用旧文档里的提交号回退。

## 已完成

- 两个现有评分算法接入统一接口和显式注册表；固定两槽、手续费、T+1、整手和资金分配规则保持。
- 槽收益/回撤按现金流中性口径计算，金额盈亏扣净转入；账户曲线显示实际资产。
- TDX 停牌行保留官方参考昨收用于估值，`tradable=0` 的行不能成交、评分或进入市场状态；缺行/缺参考仍明确失败。
- Rust/Python 两个旧表初始化入口首次增加 `tradable` 时，在同一事务分类已有 `none/tdx` 零报价行；重复初始化保留显式标志。
- 离线回放 `v2/examples/regime_ab.rs`、独立核算 `v2/research/verify_ab.py`、停牌回填 `v2/research/backfill_halted.py` 及其测试随代码提供。
- 全期 813 日完整产物已独立核算，账本、收益、日期差异 0；停牌错误成交回归、正常成交对照和迁移故障回滚均通过。

本金 ¥100,000，2023-07-05 至 2026-11-11：关闭过滤器期末 ¥97,657.55，开启 ¥98,654.20；精确最大回撤分别约 14.70% / 6.59%。**两组仍亏损**。明细、精度、来源和模型边界见 [真实历史核验](REAL-HISTORY-REGIME-2026-11-11.md)。

## 拉取代码

新电脑：

```bash
git clone --branch codex/v2-store-boundaries https://github.com/daviesjoin-afk/astock-paper-trading.git
cd astock-paper-trading
git log -1 --oneline
```

已有克隆且工作区干净：

```bash
git fetch origin codex/v2-store-boundaries
git switch codex/v2-store-boundaries
git pull --ff-only origin codex/v2-store-boundaries
```

如果有本地修改或历史分叉，先保留并核对差异；不要 `reset --hard`。网络代理按公司电脑实际配置设置，不照搬家用电脑旧代理。

## 构建与验证

在仓库根执行。Linux 或已有匹配 MSVC 环境时用项目固定的 Rust 1.99.1；本机 Windows 缺 MSVC 构建工具，实际用 GNU：

```bash
export PATH="$HOME/.cargo/bin:$PATH"
rustup toolchain install 1.99.1-x86_64-pc-windows-gnu --profile minimal
cargo +1.99.1-x86_64-pc-windows-gnu test --locked --all-targets --manifest-path v2/Cargo.toml
cargo +1.99.1-x86_64-pc-windows-gnu build --release --locked --manifest-path v2/Cargo.toml
cargo +1.99.1-x86_64-pc-windows-gnu build --release --locked --manifest-path v2/Cargo.toml --examples
python -m unittest discover -s v2/data_sources -p 'test_*.py' -v
python -m unittest discover -s v2/research -p 'test_*.py' -v
bash v2/scripts/test_daily_cycle.sh
cd v2/web
npm ci --no-audit --no-fund
npm run build
npx playwright install chromium
npx playwright test
```

Linux 将 `cargo +1.99.1-x86_64-pc-windows-gnu` 换为 `cargo +1.99.1`，可执行文件去掉 `.exe`。

本机最终实测：Rust 库 **95** + 例程 **5**，Python 数据源 **92** + 研究 **13**，Shell 行为 **15**，均退出 0、无跳过。Web build 与浏览器 **9/9** 通过；网页代码在之后的初始化修正中未变。另有 release HTTP 冒烟验证 125 日推进、合法成交、重复结算幂等和读取接口。真实行情 `bar_smoke.sh` 摄取两日 **11,157 行**并通过；脚本显式指定它要测试的 `threshold_bps=1100`，不改变接口默认的分板块阈值。

## 数据带什么

Git 中包含源码、锁文件、[精确核算摘要](assets/real-history-summary-2026-11-11.json)、[933 日包指纹清单](assets/tdx-package-manifest-2026-11-11.json)。**`.data/`、`work/`、SQLite、原始日包和编译缓存没有上传。** GitHub 拉取完成不代表公司电脑已有行情。

直接复核旧结果所需最小输入：

| 相对路径 | 用途 |
| --- | --- |
| `work/real-history/db/research-market-halt-boundary.sqlite3` | 最终停牌研究副本，约 665MB 逻辑文件 |
| `work/real-history/ab-full-final.json` | 完整全期结果，约 1.88MB |

研究副本 SHA256 为 `683646804d062c882ff2456a4c1b1e51eab0d04839430789a5e6b3268c7dd609`；完整报告 SHA256 为 `416595c8832545b0c15bc5a64842fb1d305433541953a9959182d1a1d8fcb172`。取得相同输入后运行：

```bash
python -B v2/research/verify_ab.py work/real-history/ab-full-final.json \
  --db work/real-history/db/research-market-halt-boundary.sqlite3 \
  --out work/real-history/ab-full-final.company-verification.json
```

核算只读打开输入库，输出使用新名字；不要覆盖旧证据。路径字符串不同会改变报告文件哈希，数值比较同时看逐日输入摘要与独立核算结果。

### 没带数据库时重新取数

1. 在公司电脑建全新研究库：

```bash
mkdir -p .data work/company-price-sets work/company
ASTOCK_V2_DB="$PWD/.data/company-research.sqlite3" \
ASTOCK_PRICE_SETS_DIR="$PWD/work/company-price-sets" \
ASTOCK_V2_ADDR=127.0.0.1:18795 \
  ./v2/target/release/astock-v2.exe
```

确认 `http://127.0.0.1:18795/v2/health` 可读后，按 Ctrl+C 停止这个新进程。它只用于初始化隔离库，不创建账户、不连接券商。

2. 从官方盘后包摄取：

```bash
python v2/data_sources/ingest_market.py --db .data/company-research.sqlite3 \
  --days 2023-01-03:2026-11-11 --skip-missing --workers 8
```

检查日包下载失败原因；`--skip-missing` 会跳过不可取得的日期，不能把“命令结束”当成覆盖完整。预期 933 个数据日、5,979 只历史并集、5,012,662 行、11,633 行停牌。若官方日包有修订，应与随代码的日包指纹清单对比，记录变化后再比较回放，不强行修改价格凑旧结果。新代码已在摄取时保留停牌行，无需对这个新库重复做旧数据回填。

3. 运行同口径全期对照并独立核算，先让回放成功退出，再运行第二条：

```bash
cargo +1.99.1-x86_64-pc-windows-gnu run --release --locked \
  --manifest-path v2/Cargo.toml --example regime_ab -- \
  --db .data/company-research.sqlite3 \
  --from 2023-07-05 --through 2026-11-11 --initial-cash-cents 10000000 \
  > work/company/ab-full.json 2> work/company/ab-full.log

python -B v2/research/verify_ab.py work/company/ab-full.json \
  --db .data/company-research.sqlite3 \
  --out work/company/ab-full.verification.json
```

回放失败退出 2 时不能使用重定向产生的空文件或部分日志计算收益。新建 SQLite 的字节布局可能不同，数据库文件 SHA256 不必与旧副本一致；要比较原始日包、逻辑行和逐日窗口输入。

`backfill_halted.py` 用于修复**关闭的旧研究副本**，要求已具备 `bar.tradable`，不能直接对原始留存库或活动账本运行。它的 manifest 支持续跑，失败日须归零再进入回放。

## 公司电脑从哪里继续优化

先读 [V2 规则](../AGENTS.md)、[重写规格](REWRITE-SPEC.md)、[策略库](STRATEGY-LIBRARY.md) 和 [路线](OPTIMIZATION-2026-11-11.md)。

下一片先明确并实现公司行为的经济权益、信号与成交时点，保留当前同日收盘模型作对照；之后做禁止未来引用的因子表达式。评分接口/注册表已经完成，无需重做。影子晋升、回退和挖掘排在口径稳定之后。

独立核算通过不证明未来盈利：它不重新生成候选排名，也没有补齐分红送转、历史税费和涨跌停流动性。当前未部署或合并；服务器状态本轮未改动、未核验。
