# 模拟盘运行交接 · 2026-11-24 10:25 (+08:00)

> 范围：**服务器模拟盘的实际运行与 AI 介入情况**。工程改动只在与运行相关处提及。
> 所有数字来自本轮实测：服务器 SSH 命令、HTTP 接口、只读账本查询。未使用历史文档数字。

---

## 〇、接入方式（交接第一站）

### 服务器

```bash
ssh -F /dev/null -i ~/.ssh/id_ed25519_astock     -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10     ubuntu@138.2.225.86
```

| 项 | 值 |
| --- | --- |
| 主机 | `138.2.225.86`（Oracle Cloud，公网 IP） |
| 用户 | `ubuntu`（有免密 sudo） |
| 私钥 | `~/.ssh/id_ed25519_astock`（当前用户 `Administrator`） |
| 架构 | `aarch64` / ARM64 — **必须在服务器本机编译**，不能传 Windows 产物 |
| 公网前端 | `http://138.2.225.86:8600`（nginx，默认 `#/live/dashboard`） |
| 内核 API | `127.0.0.1:18765`（仅本机回环，不对外） |

固定路径：

| 路径 | 用途 |
| --- | --- |
| `/opt/astock-live/bin/astock-v2` | 内核二进制 |
| `/opt/astock-live/scripts/` | 调度与 AI 脚本（`live_paper_loop.py` 等） |
| `/opt/astock-live/data_sources/` | 数据层 |
| `/etc/astock-live.env` | 环境变量与 LLM 凭据（`root` 可读） |
| `/var/lib/astock-live/intraday.sqlite3` | **盘中账本**（唯一事实来源） |
| `/var/lib/astock-live-memory/resident.json` | AI 常驻记忆 |
| `/opt/astock-v2-rewrite/web/` | nginx 静态根（**不是** `/opt/astock-live/web`） |
| `/opt/astock-live/backups/` | 部署备份，回滚从这里取 |

服务（systemd）：

| 单元 | 作用 |
| --- | --- |
| `astock-live.service` | Rust 内核（HTTP + 账本） |
| `astock-live-loop.service` | Python 调度：每 180 s 一轮，含 AI 观察 |
| `astock-live-watch.timer` | 看门狗（只记录，不改状态） |
| `astock-live-evolution.timer` | 收盘进化 |
| `nginx.service` | 公网 8600 |

### GitHub

| 项 | 值 |
| --- | --- |
| 仓库 | `https://github.com/daviesjoin-afk/astock-paper-trading` |
| PR | **#493** `codex/v2-ai-context-and-daily-pnl` → `release/v2-source-2026-11-20`（OPEN / MERGEABLE） |
| 工作副本 | `D:/Codex/astock-v2/work/v2-today-pnl` |
| 当前 HEAD | `1934db5`（已推送到 PR 分支） |

**注意分支名不一致**：本地分支叫 `codex/v2-today-pnl`，推送目标是 `codex/v2-ai-context-and-daily-pnl`（即 PR #493 的 head）。推送时必须显式指定：

```bash
cd D:/Codex/astock-v2/work/v2-today-pnl
git push origin HEAD:refs/heads/codex/v2-ai-context-and-daily-pnl
```

CI（5 项，PR 触发）：`rust-kernel`、`data-sources`、`web-ui`、`web-e2e`、`security-leak-scan`。全部为绿才算可合并。

### 本轮连接状态（2026-11-24 10:30）

**当前连不上服务器。** 实测：

- `ping 138.2.225.86` → 2 发包 0 收，100% 丢包
- TCP `22` / `8600` / `443` → 全部 blocked
- 同期 `github.com`、`api.github.com` → 200，出网正常

即本机到该主机的路由/防火墙中断，不是密钥或账号问题（`known_hosts` 有该主机记录，私钥存在）。
上一节「当前运行状态」的数据采集于 **10:24–10:25**，当时连接正常；之后失去连接。
**恢复方式**：先在 Oracle Cloud 控制台确认实例是否在运行、安全组是否放行 22；如在运行则多半是网络侧，重启实例或更换出网路径。

---

## 一、当前运行状态（事实）

| 项 | 实测值 |
| --- | --- |
| 服务 | `astock-live` / `astock-live-loop` / `astock-live-watch.timer` / `astock-live-evolution.timer` / `nginx` — **全部 active** |
| 内核二进制 | `936824455679ce91fede6569`（2026-11-23 17:20 部署） |
| 今日轮次 | **10 轮**（09:41 起） |
| 今日成交 | **10 笔**（6 卖 4 买，截至 10:23） |
| AI 成功轮次 | **0 / 10** |
| 轮次间隔 | 中位 **182 s**，最小 133 s，最大 **1108 s** |
| 总资产 | **¥97,415.79**（10:23 轮，接口口径） |
| 今日盈亏 | **+¥3,349.11** |
| 现金 | ¥35,229.10 |
| 持仓 | 10 只 |

净值轨迹（cents）：昨日收盘 9,411,199 → 今日开盘首轮 9,569,900 → 10:23 轮 9,741,579。

---

## 二、今日成交明细

| 轮次(时分秒) | 槽 | 方向 | 标的 | 数量 | 价格 | 手续费 |
| --- | --- | --- | --- | --- | --- | --- |
| 094101 | 激进 | 卖 | 000056.SZ | 4600 | 1.64 | 8.77 |
| 094101 | 稳健 | 卖 | 600750.SH | 100 | 24.41 | 6.22 |
| 094101 | 激进 | 买 | 000530.SZ | 200 | 14.11 | 5.00 |
| 094101 | 激进 | 买 | 300812.SZ | 100 | 18.23 | 5.00 |
| 094101 | 激进 | 买 | 300324.SZ | 100 | 4.48 | 5.00 |
| 101209 | 激进 | 卖 | 600156.SH | 1000 | 9.50 | 9.75 |
| 101557 | 稳健 | 卖 | 301030.SZ | 1900 | 4.37 | 9.15 |
| 101810 | 激进 | 买 | 000826.SZ | 100 | 1.10 | 5.00 |
| 102307 | 稳健 | 卖 | 601975.SH | 1300 | 3.56 | 7.31 |
| 102307 | 稳健 | 买 | 002148.SZ | 400 | 11.61 | 5.00 |

**T+1 换日已验证**：昨日买入的持仓今日全部解锁（`sellable_quantity == quantity`），`301030.SZ *ST仕净` 1900 股在 10:15 售出。这一条是本轮唯一跑通的功能性验证。

现持仓 10 只（括号内为 `可卖/总量`）：000530.SZ(700/900)、000711.SZ(1700/1700)、000826.SZ(0/100)、002642.SZ(900/900)、300324.SZ(700/800)、300812.SZ(0/100)、300949.SZ(200/200)、301391.SZ(100/100)、603324.SH(200/200)，另 002148.SZ 与 601975.SH 见下。

今日新买的 000826.SZ、300812.SZ、002148.SZ 可卖量为 0，T+1 生效；601975.SH 已在 10:23 清仓。**上表持仓清单取自 10:23 之前的快照，10:23 那轮又有 2 笔成交，最新持仓以接口为准。**

---

## 三、AI 介入情况：**完全未介入**

10 轮全部 `ai_ok = 0`，每轮日志为：

```
{"event":"ai_unavailable","reason":"URLError","attempts":1,"elapsed_ms":30030}
```

**根因**：LLM 中转站 `http://175.24.164.56:8788/v1` 已下线。

| 探测 | 结果 |
| --- | --- |
| `ping 175.24.164.56` | 2 包发出，0 收到，**100% 丢包** |
| TCP `8788` | **unreachable** |
| `curl /v1/models` | 8 秒超时，无响应 |

**配置现状**（`GET /v2/settings`）：

| 项 | 值 |
| --- | --- |
| `ai_base_url` | `http://175.24.164.56:8788/v1`（**已下线**） |
| `ai_model` | `deepseek-v4.1-flash` |
| `ai_depth` | `deep` |
| `ai_timeout_secs` | 25 |
| `ai_api_key` | **空** |

`ai_api_key` 为空导致内核走不到自己的 provider 调用，回退到 Python 直连 `ASTOCK_LLM_BASE`，同样指向那台中转站，同样失败。直连 DeepSeek 官方用现有密钥试过：**401**，该密钥是中转站的，不能用于官方端点。

**结论：当前交易全部由确定性规则策略产生，AI 的新闻判断、资金流判断、错题本记忆一条都没有参与。** 今日 +¥3,349 是规则策略的结果，不能归功于 AI。

---

## 四、外部数据源可用性

| 源 | 用途 | 状态 |
| --- | --- | --- |
| `qt.gtimg.cn` | 行情快照（核心） | ✅ 200 |
| `push2.eastmoney.com` | 个股新闻、资金流 | ❌ 不可达 |
| `dq.10jqka.com.cn` | 人气榜/概念标签 | ❌ 不可达 |
| `data.hexin.cn` | 北向资金 | ❌ 不可达 |
| `cninfo.com.cn` | 公告 | ❌ 不可达 |
| `175.24.164.56:8788` | LLM | ❌ 已下线 |

只有行情可用。新闻、北向、热度、公告四类输入今早**全部为空**。

---

## 五、当前问题清单（按影响排序）

### P1 · AI 完全不可用，模拟盘退化为纯规则策略

- 影响：这是本项目"AI 深度介入"目标的直接失效。所有 AI 相关能力（新闻解读、资金流判断、自记忆成绩单、深度模式的个股仓位决策）当前均为零。
- 现状：规则策略独立跑通，账户和账本正常，但**不能称之为 AI 模拟盘**。
- 需要你提供：可用 LLM 端点 + 对应密钥。要么中转站恢复，要么给官方/其它兼容 OpenAI 协议的地址与 key。
- 我能做的：拿到地址后写入 `ai_base_url` + `ai_api_key`，跑一轮真实探针验证 `ai_ok=1` 再确认。

### P1 · 开盘后 18 分钟只跑了 1 轮

- 实测：09:41 → 09:59 间隔 **1108 秒**，正常应为 180 秒。
- 根因：进程向已下线的中转站建连，socket 停在 `SYN-SENT`，操作系统默认重传窗口内 `urllib` 的单次操作超时不生效。
- 已处置：给 AI 调用与三项附加抓取加了**硬性时限**，超时即放弃并继续本轮。部署后间隔回到 182 s。
- 剩余风险：这是**应急补丁**。中转站不恢复，每轮仍要白白等满时限（现为 26 s）。10 轮里有 10 轮在空等。

### P2 · 追高拦截约占主导，但无法区分是规则还是 AI 判断

- 拦截分布（今日累计）：`max_positions` 4285、`lot_too_expensive` 1300、`morning_cooldown_guard` 549、`limit_up_sealed` 204、`no_live_price` 80、`cash_budget` 35。
- `max_positions` 占绝大多数，说明**仓位上限是当前最主要的约束**，不是选股。10 只持仓已接近上限。
- `lot_too_expensive` 1300 —— 高价股按整手算买不起一手，属正常但值得关注：它在持续排除高价位标的。

### P2 · 今早 4 类数据输入为空，AI 即使恢复也缺料

- 恢复 LLM 后，若东财/同花顺/巨潮仍不可达，AI 只能看行情 + 记忆。新闻驱动的禁买（如昨天 `*ST仕净` 处罚）将不起作用。
- 需要确认：这些站点是**服务器侧网络被封**，还是站点自身问题。腾讯行情能通说明出网正常，更像是这些域名被限。

### P3 · 观察项

- 昨日 4 条进化提案仍为 `accepted=false`（自进化不落地），今日未见新提案。
- `ai_depth=deep` 配置在 AI 不可用时无实际作用。
- 账本规模：`decision` 4,697,100 行、`quote_snapshot` 2,348,550 行，10 日归档清理策略在跑，暂未见空间问题。

---

## 六、明天开盘要盯的

1. **AI 是否恢复** —— 看 `ai_ok` 是否出现 1。若仍为 0，先查中转站。
2. **轮次间隔是否稳定 180 s** —— 若再出现 >300 s，说明还有阻塞点。
3. **数据源是否补齐** —— 日志里 [`同花顺热榜失败`] 是否消失。
4. **T+1 次日解锁** —— 今日买入的 000826.SZ / 300812.SZ / 002148.SZ 明日应变为可卖。
5. **净值与归因** —— 今日 +3,349 是规则策略所得，恢复 AI 后要分开看，不能混算。

---

## 七、快速核查命令（交接用）

```bash
# 服务与二进制
ssh -i ~/.ssh/id_ed25519_astock ubuntu@138.2.225.86 \
  'systemctl is-active astock-live astock-live-loop astock-live-watch.timer astock-live-evolution.timer nginx; sha256sum /opt/astock-live/bin/astock-v2'

# 账户
ssh ... 'curl -s http://127.0.0.1:18765/v2/live/account'

# 今日轮次与 AI 状态
ssh ... 'sudo -u astock python3 -c "import sqlite3;d=sqlite3.connect(\"file:/var/lib/astock-live/intraday.sqlite3?mode=ro\",uri=True);print(d.execute(\"SELECT round_id,trade_count,ai_ok FROM round WHERE round_id LIKE ? ORDER BY rowid\",(\"2026-11-24-%\",)).fetchall())"'

# AI 失败原因
ssh ... 'sudo journalctl -u astock-live-loop --since today | grep -E "ai_unavailable|ai_usage" | tail -5'

# 外部源连通性
ssh ... 'for u in https://qt.gtimg.cn/ https://push2.eastmoney.com/ https://dq.10jqka.com.cn/ https://data.hexin.cn/; do printf "%-34s %s\n" "$u" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 $u)"; done'
```

---

## 八、边界声明

- 本轮只做**运行观测与取证**，未改动线上 AI 配置（`ai_base_url` / `ai_api_key` 原样保留）。
- 今日 +¥3,349 的归因只能到"规则策略"，**不能归因于 AI**。
- 交易时段内改了调度脚本（加超时上限）并重启过一次 loop，09:59 之后轮次才恢复。09:41–09:59 的轮次缺失是这个原因，不是账本问题。
- 未验证：AI 恢复后的实际决策质量、新闻输入恢复后的禁买效果、恢复后的缓存命中率。
- **10:30 之后无法连接服务器**（ping 与 22/8600/443 全不通，同期 GitHub 正常）。文档里所有运行数据止于 10:25，之后的状态未知，不能当作仍在正常运行。
