# 公司电脑接续入口（2026-11-12 更新）

最新已实现：[公共行情/公司资料/公告层](PUBLIC-MARKET-DATA.md) 与 [候选排序/单票预算修复核验](SELECTION-VALIDATION-2026-11-12.md)。
日线、报价、公司资料、公告可直接 SDK/CLI 按需取用；现有三个取数器共享传输/缓存。
新增数据源不自动参与策略或公司行为入账。以下构建和取数步骤继续适用。

本文件是本轮代码与真实历史研究的接续入口。分支为 **`codex/v2-store-boundaries`**，现有 [PR #492](https://github.com/daviesjoin-afk/astock-paper-trading/pull/492) 用于远端检查。本轮只涉及 V2 模拟盘，不部署、不合并、不修改交易参数；`risk_limits_v1.md` 不属于本次交付。

`HANDOFF-2026-11-11-V3.md` 等旧文档保留为历史快照，其中“本机无数据”“尚未完成全期”和旧收益表均由本文件及本轮研究记录取代。以公司电脑拉到的本文件所在提交为代码版本，不使用旧文档里的提交号回退。

## 已完成

- 两个现有评分算法接入统一接口和显式注册表；固定两槽、手续费、T+1、整手和资金分配规则保持。
- 槽收益/回撤按现金流中性口径计算，金额盈亏扣净转入；账户曲线显示实际资产。
- TDX 停牌行保留官方参考昨收用于估值，`tradable=0` 的行不能成交、评分或进入市场状态；缺行/缺参考仍明确失败。
- Rust/Python 两个旧表初始化入口首次增加 `tradable` 时，在同一事务分类已有 `none/tdx` 零报价行；重复初始化保留显式标志。
- 离线回放 `v2/examples/regime_ab.rs`、独立核算 `v2/research/verify_ab.py`、停牌回填 `v2/research/backfill_halted.py` 及其测试随代码提供。
- 全期 813 日完整产物已独立核算，账本、收益、日期差异 0；停牌错误成交回归、正常成交对照和迁移故障回滚均通过。

最新排序/预算修复后的本金 ¥100,000、2023-07-05 至 2026-11-11 对照：关闭过滤器期末 ¥79,018.73，开启 ¥119,318.05；精确最大回撤约 32.0198% / 13.6873%。新回放与独立核算均退出 0。明细与风险边界见 [11-12 新核验](SELECTION-VALIDATION-2026-11-12.md)。11-11 基线的 ¥97,657.55 / ¥98,654.20 与两组亏损结论保留在 [基线研究](REAL-HISTORY-REGIME-2026-11-11.md)，不代表修复后的新结果。

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

11-12 最终实测：Rust 库 **101** + 例程 **5**，Python 数据源 **187** + 研究 **13**，Shell 行为 **15**，均通过无跳过；Web build 与浏览器 **9/9** 通过。release HTTP 隔离验证 **80 日、5 条成交**、重复结算零新增及读取；四类公共数据真实请求与离线缓存复用通过，包括巨潮 **2 页、3 条公告**。首次浏览器运行因缺测试浏览器失败，临时工作目录补齐后 9/9 通过，临时浏览器随后清理。前轮真实行情 `bar_smoke.sh` 两日 **11,157 行**是基线证据，本轮未把它冒充重新执行；本轮 TDX 单包两股共享缓存已实测。

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

先读 [V2 规则](../AGENTS.md)、[重写规格](REWRITE-SPEC.md)、[策略库](STRATEGY-LIBRARY.md)、
[公共数据层](PUBLIC-MARKET-DATA.md) 和 [新回放核验](SELECTION-VALIDATION-2026-11-12.md)。
上一轮全期口径见 [11-11 基线研究](REAL-HISTORY-REGIME-2026-11-11.md)。

顺序建议：

1. ✅ 已完成时序分离：`--fills next-session` 用前一交易日收盘决策、当日收盘成交，止损用前一收盘触发，
   `same-close` 保留对照。结果见 [成交时点对照](FILLS-TIMING-2026-11-12.md)：开启组 +19.32% → +10.55%，
   关闭组 −20.98% → −28.20%，开启组回撤基本不变。
   下一步是涨跌停/停牌可成交性：**封板尚未建模**，实测全期触及涨停 0.879%、跌停 0.282%，
   其中 806 行是全天同一价位的跌停（想卖也卖不掉），因此当前成交仍偏乐观，不能当作可执行收益。
2. 公司行为经济权益（分红、送转、拆并股）与明确可见时间；公告可用不等于已核实入账。
3. 冻结参数做分期/样本外和相近仓位暴露对照，区分排序、仓位、过滤器、费用各自贡献。
4. 之后才进入禁止未来引用的因子表达式、影子晋升与挖掘。

公共数据层可以按需取公司资料和公告，但当前每个请求只保留最新快照，不是多版本 PIT
档案；要支持严格历史可知性，需新增持久化的版本/首见记录与验收，不能把今天的资料
回填成过去已知。公司资料和公告尚未接入策略评分或公司行为账本。

独立核算通过不证明未来盈利：它不重新生成候选排名，也没有补齐分红送转、历史税费和涨跌停流动性。当前未部署或合并；服务器状态本轮未改动、未核验。
