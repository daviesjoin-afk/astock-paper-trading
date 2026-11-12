> ⚠️ **已被 [`HANDOFF-2026-11-11-V3.md`](HANDOFF-2026-11-11-V3.md) 取代。**
> 本文是本机勘误补丁，内容已并入 V3 的 §2、§3、§6。保留作为历史记录。

# AStock V2 交接文档 — 本机勘误与本地化（Windows / `<PROJECT_ROOT>`）

> 配套主文档：[`HANDOFF-2026-11-11-V2.md`](HANDOFF-2026-11-11-V2.md)。
> 主文档写于另一台机器。**凡是涉及本机路径、工具链、端口、数据位置的地方，以本文为准。**
> 本文所有数字都是在本机实测复现过的，不是抄的。核对时间：2026-11-11。

---

## 0. 一句话

主文档的**代码事实全部正确**，但它的**本地运行部分（§4.1、§4.2）在这台机器上会直接失败**：
仓库根没有 `.cargo` / `.rustup` / `.toolchain`，且本机没有 MSVC 构建工具，必须用
已全局安装的 GNU 工具链。另外 `.data/` 里那 500MB 行情**不在本机**，需要重新摄取。

---

## 1. 代码比对结果：一致

拉取 `origin/codex/v2-store-boundaries` 后逐项核对：

| 项 | 主文档 | 本机实测 | 结论 |
| --- | --- | --- | --- |
| 分支 | `codex/v2-store-boundaries` | 同名分支，已检出 | ✅ |
| HEAD | `96dbd55` | `002840a` | ⚠️ 多 2 个提交 |
| 提交数（`e42f778..HEAD`） | 19 | **21** | ⚠️ 见下 |
| 代码变更 | 137 文件 / +12,812 / −27,496 | **140 文件 / +13,662 / −27,503** | ⚠️ 同上 |
| Schema | v2 | v2（`store/mod.rs:18`） | ✅ |
| 测试 | 51 / 86 / 15 / 8 | **51 / 86 / 15 / 8 全绿** | ✅ |

**为什么多 2 个提交**：主文档写完后作者又提交了两个：

1. `f18f11f` `docs: handoff for the rewrite, plus a roadmap with progress`
   （就是主文档本身 + `ROADMAP.md`）
2. `002840a` `fix(v2-web): drop opacity from the curve's mid label`
   （`styles.css` 改 3 行，修 11px 文字对比度）

主文档 §1 的「137 文件 / +12,812 / −27,496」正好是 **`96dbd55` 这个提交点**的 diffstat，
不是分支头的。所以数字没写错，只是快照早了一步。**接手时以 `002840a` 为准。**

§3.2 那张行数表，**15 个文件逐个 `wc -l` 全部精确命中**（`main.rs` 615、
`store/mod.rs` 2072、`strategy.rs` 433、`market.rs` 493、`account.rs` 339、
`allocation.rs` 54、`calendar.rs` 155、`instrument.rs` 143、`ingest_market.py` 417、
`import_v1_klines.py` 255、`realtime_quotes.py` 162、`daily_cycle.sh` 135、
`router.ts` 112、`chart.ts` 170、`main.ts` 939）。这份表可以完全信任。

另外主文档漏写了几样实际存在的：`ROADMAP.md`（根目录，273 行，带进度的路线图）、
`v2/docs/REWRITE-SPEC.md`、`v2/scripts/bar_smoke.sh`、`v2/scripts/test_daily_cycle.sh`。

---

## 2. 本机路径与工具链（覆盖主文档 §4.1）

| 项 | 值 |
| --- | --- |
| 仓库根 | `<PROJECT_ROOT>` |
| 分支 | `codex/v2-store-boundaries`（跟踪 `origin/` 同名分支） |
| Rust 工具链 | `<USER_HOME>\.cargo\bin`（全局安装） |
| **缺少的** | 仓库根**没有** `.cargo` / `.rustup` / `.toolchain` 目录 |
| **缺少的** | 本机**没有** Visual Studio / MSVC 构建工具 |
| 可用的 GNU | `1.99.1-x86_64-pc-windows-gnu`（已安装，直接用） |
| Node / npm | v24.22.2 / 11.15.0 |
| Python | 3.14.5 |

### 2.1 为什么 `link.exe` 会失败

本机默认工具链是 `1.99.1-x86_64-pc-windows-msvc`，但没有 MSVC 的 `link.exe`。
PATH 里的 `/usr/bin/link.exe` 是 Git Bash 自带的 **coreutils `link`**（硬链接工具），
不是链接器，所以报 `link: extra operand ...`.

**主文档 §4.1 的 PowerShell 绕法在这台机器上不能用**——它假设仓库根有
`.cargo`、`.rustup`、`.toolchain\mingw\mingw64`，这三个目录本机都不存在。

### 2.2 本机正确做法：直接用 GNU 工具链

```bash
export PATH="<USER_HOME>/.cargo/bin:$PATH"

# 测试（51 个）
cd <PROJECT_ROOT>/v2
cargo +1.99.1-x86_64-pc-windows-gnu test --locked --all-targets

# 构建 release（e2e 需要）
cargo +1.99.1-x86_64-pc-windows-gnu build --release --locked
```

不需要设 `CARGO_HOME` / `RUSTUP_HOME`，也不需要 mingw 路径。

---

## 3. 端口：本机默认值和主文档不一致

这是个真坑。同一个仓库里三个组件写了三个默认值：

| 组件 | 位置 | 默认端口 |
| --- | --- | --- |
| Rust 内核 | `v2/src/main.rs:593`（`ASTOCK_V2_ADDR`） | **8765** |
| Vite dev | `v2/web/vite.config.ts:3`（`ASTOCK_V2_API_URL`） | **8765** |
| Vite preview | `v2/web/vite.config.ts:6` | **18766** |
| `daily_cycle.sh` | `v2/scripts/daily_cycle.sh:30`（`ASTOCK_API`） | **18765** |
| 部署 env 样例 | `v2/deploy/astock-v2.env.example` | **18765** |
| nginx | `v2/deploy/nginx.conf:24` | 反代到 **18765** |

所以：

- **本地开发**（主文档 §4.1）用 8765，开箱即用，不用配。
- **本机跑 `daily_cycle.sh` 必须显式指定 `ASTOCK_API`**，否则它会去打 18765 而你的内核在 8765：

```bash
ASTOCK_API=http://127.0.0.1:8765 \
ASTOCK_V2_DB=<PROJECT_ROOT>/.data/v2.sqlite3 \
  bash v2/scripts/daily_cycle.sh --day 2026-11-11
```

- e2e 自己起在 **18766**（`v2/web/playwright.config.ts`），和上面都不冲突，不用管。

---

## 4. 数据：本机是空的

| 项 | 主文档（另一台） | 本机 |
| --- | --- | --- |
| `.data/` | 500MB，491 万行 CSV | **不存在** |
| 数据库 | 515 万行 / 5980 只 / 933 天 | **不存在**（最先只有一个空壳 `v2/.preview/v2-preview.sqlite3`，且它没有 `bar` 表） |
| V1 归档 | 服务器 `/opt/astock-v1-archive-2026-11-07/` | 本机无 |

`.data/` 在 `.gitignore:22`，**所以从 GitHub 拉下来永远是空的**。要复现主文档那一年回测，
需要两台机器之一：

1. 从另一台机器 / 服务器拷 `.data/v1-klines/`（5574 个 CSV，491 万行）；
2. 或者重新从通达信盘后包摄取（本机实测可行，见下）。

### 4.1 本机实测：摄取可用，速度与主文档一致

我在本机实跑过（用临时库，跑完已清理）：

```bash
# 单日：实测 5576 只 / 9.6~9.9 秒，和主文档的「5576 只、9 秒」一致
python v2/data_sources/ingest_market.py \
  --db <PROJECT_ROOT>/.data/v2.sqlite3 \
  --day 2026-11-10 --workers 4
```

注意两个本机特有的坑：

- **库必须先存在**。`ingest_market.py` 不会建库，会报 `database not found`。
  先让内核起一次（`--day` 之前），或者从别处拿一个 schema v2 的库。
- **路径要用 Windows 原生绝对路径的 正斜杠形式**。`/tmp/...` 这种 Git Bash 路径 Python 打不开。

区间摄取实测（2026-11-03 → 2026-11-10，`--skip-missing --workers 4`）：

```
2026-11-05  wrote 5572 rows in 9.68s
2026-11-06  wrote 5574 rows in 9.37s
2026-11-07  skipped (package_not_published)     ← 周六，正常
2026-11-08  skipped (package_not_published)     ← 周日，正常
2026-11-09  wrote 5574 rows in 9.69s
2026-11-10  wrote 5576 rows in 9.48s
total written: 33441
```

### 4.2 本机实测：推进接口

| 场景 | 主文档 | 本机实测 |
| --- | --- | --- |
| `GET /v2/account` | 0.22 秒 | 0.04 秒（空账户） |
| 单日推进（有 5 天历史） | 9.6 秒 | **0.08~0.17 秒** |

快很多是因为本机只有 6 天数据、`SETTLEMENT_WARMUP_DAYS = 120` 的窗口几乎空。
**这个数字没有参考价值**，主文档的 9.6 秒才是全历史（933 天）的真实量级。

#### 本机实测：灌多少天才会成交

| 灌入天数 | 推进结果 |
| --- | --- |
| 5 天 | `trade` 0 行、`position` 0 行、`slot_equity_day` 正常写 8 行 |
| **149 天** | **正常成交**（见下） |

5 天不成交是设计：`SETTLEMENT_WARMUP_DAYS = 120`（`v2/src/main.rs:473`），
`main.rs:578` 用它算结算窗口下界，窗口里没有 K 线就没有信号。
**要看到成交，实际需要灌 120 个交易日以上。**

#### 本机实测：149 个交易日的完整旅程

灌 `2026-04-01 → 2026-11-10`（149 天 / 824,132 行，8 线程，约 3 分钟），
本金 10 万，逐日推进：

```
2026-09-24  7.08s  equity 9,999,000  成交 2 笔
            稳健   买 000001.SZ  600 股 @ 11.30
            激进   买 000672.SZ  900 股 @ 15.56
2026-09-28  6.58s  equity 9,921,339  pos=1  （一笔已平）
2026-09-29  4.48s  equity 9,923,839  pos=2
2026-09-30  4.12s  equity 9,929,439  pos=2
2026-10-08  3.98s  equity 9,938,239  pos=2
```

两个可以直接用的结论：

1. **`POST /v2/account/run` 返回 200 才代表真的推进了**。中间误传过一个库里没有的日期
   （`2026-09-25`，那天缺包），返回的是 `422 day_not_in_bar_table`——
   它把 `latest` 一起带回来，很好排查。
2. **本机单日推进 4~7 秒**（149 天历史、5576 只股票）。主文档说的 9.6 秒是 933 天全历史，
   量级一致，说明性能改动（`IndexedPriceSet` + 120 天滚动窗口）是真的。

推进响应里解析注意：JSON 含中文（策略名「稳健」「激进」），
Windows 下 Python 用 `open()` 默认 GBK 会炸，要 `io.open(..., encoding='utf-8')`。

### 4.3 本机实测：gaps 按板块算门槛是对的

```
GET /v2/market/gaps?day=2026-11-10
→ count=1，N钶锐 688827.SH（科创板新股，prev 45.37 → close 129.20）
```

正好是主文档 §3.4 说的「按板块算门槛」在起作用——科创板 20% 涨停不该被当成除权跳空。
验证通过。

---

## 5. 本机完整测试命令（实测全绿）

```bash
export PATH="<USER_HOME>/.cargo/bin:$PATH"
cd <PROJECT_ROOT>

# 1) Rust：51 passed
cd v2 && cargo +1.99.1-x86_64-pc-windows-gnu test --locked --all-targets && cd ..

# 2) Python：86 tests, OK (skipped=3)
python -m unittest discover -s v2/data_sources -p 'test_*.py'

# 3) Shell：15 passed, 0 failed
bash v2/scripts/test_daily_cycle.sh

# 4) 前端 + e2e：8 passed
cd v2 && cargo +1.99.1-x86_64-pc-windows-gnu build --release --locked && cd web
npm run build
ASTOCK_V2_BIN="<PROJECT_ROOT>/v2/target/release/astock-v2.exe" npx playwright test
```

主文档 §4.4 写的是 `cd v2 && cargo test`、`python -m unittest discover -s data_sources`、
`bash scripts/test_daily_cycle.sh`——这三条在**仓库根**执行都会找不到路径，
必须按上面的 `v2/` 前缀改。`ASTOCK_V2_BIN` 也要显式给，因为
`e2e/start-server.mjs` 默认找 `v2/target/release/astock-v2.exe`，在 Git Bash 下能命中，
但用绝对路径更稳。

---

## 6. 本机没有的东西（别浪费时间找）

- ❌ `.data/v1-klines/`（491 万行前复权 CSV）— 在另一台机器/服务器
- ❌ `/opt/astock-v1-archive-2026-11-07/` — 在服务器
- ❌ MSVC 构建工具 — 用 GNU 工具链替代
- ❌ `v2/web/node_modules` 里的 Playwright 浏览器 — 需要 `npx playwright install chromium`
- ❌ 网络代理：git 全局配了 `http.proxy=127.0.0.1:7897` 但该端口**没有监听**，
  访问 GitHub 会失败。**直连是通的**，拉代码时用：
  `git -c http.proxy= -c https.proxy= fetch origin ...`

---

## 7. 主文档其他仍然有效 / 已失效的条目

| 主文档 | 状态 |
| --- | --- |
| §1.2 一整年 -9.32%、稳健 +32.84%、激进 -51.50% | ✅ 数据在另一台机器上，本机无法复现（缺 `.data/`） |
| §3.2 行数表 | ✅ 全部精确（见 §1） |
| §3.3 表结构（`bar`/`slot`/`slot_equity_day`/`day_run`） | ✅ 已在空库实测确认 |
| §3.4 六个新 API | ✅ 全部存在且可访问 |
| §4.1 本地开发端口 8765 | ✅ 端口对，但 GNU 工具链那段要按本文 §2.2 改 |
| §4.2 补数据命令 | ⚠️ 路径要按本文 §4 改；库必须先存在 |
| §4.3 `daily_cycle.sh` | ⚠️ 必须显式传 `ASTOCK_API`（默认 18765 vs 内核 8765） |
| §4.4 测试命令 | ⚠️ 三个路径要加 `v2/` 前缀，见本文 §5 |
| §5.1 激进策略在震荡市亏损 | ✅ 策略问题，与机器无关，本机同样适用 |
| §5.2「19 个提交未推送」 | ❌ **已失效**——代码已在 GitHub（`origin/codex/v2-store-boundaries`） |
| §5.2「服务器是旧二进制」 | ⚠️ 本机无法验证，需要连服务器 |
| §6 服务器部署 | ⚠️ 本机不执行；服务器 ARM64，本机 x86_64，必须在服务器上编译 |
| §7 P0 市场状态判断 | ✅ 下一步，与本机无关 |
| §10 接手人第一步 | ⚠️ 第 1 步改成 `git log --oneline e42f778..HEAD`（21 个，不是 19） |

**§5.2 的「未推送」现在是错的**：分支已经推到 GitHub 了，`git rev-list --count @{u}..HEAD` = 0。

---

## 8. 本机接手第一步

```bash
cd <PROJECT_ROOT>
git log -1 --format='%h %s'        # 应为 002840a
git status --short                  # 应只有 untracked 的 risk_limits_v1.md
export PATH="<USER_HOME>/.cargo/bin:$PATH"
```

然后按本文 §5 的四条命令验一遍（51/86/15/8）。**跑通再谈别的。**

要做真实回测的话，第一个阻塞点是数据——先解决 `.data/`（从另一台机器拷，或本机重新摄取）。
本机实测**需要 120 个交易日以上才会产生第一笔成交**；149 天（3 分钟、82 万行）已经能正常买卖，
单日推进 4~7 秒。
