# R24 Market Data Call-Site Audit

- 仓库：`daviesjoin-afk/astock-paper-trading`
- Base：`2afcbecad0ee7966130d41b30711bbc5ebc38660`（= PR #181 merge）
- 审计方式：**逐调用链读源码 + AST 定位所属函数 + 实机复现延迟**，不按函数名推断。

---

## 一、真实的 provider 调用面

`data_fetcher.py` 是唯一的 provider 编排入口（HTTP/重试/熔断在 `marketdata_transport`，
分页/解析在 `marketdata_providers`，TTL 内存缓存与快照锁在 `marketdata_cache`）。
生产代码直接调用它的地方共 **44 处**（不含 `test_*.py`）。

### 1.1 全市场快照 `fetch_market_snapshot_full`（14 处）

| caller | 调用 | 允许 network | read path | 参与 decision | freshness 要求 | provider 失败行为 | R24 后 owner |
|---|---|---|---|---|---|---|---|
| `paper_trading._market_state:3740` | `max_age=240` | ✅ 默认 | 否（内部门控） | 是 | 当日 `quote_at` | 空 → light=unknown | Market Data Authority (refresh) |
| `paper_trading.generate_signals:7524` | `max_age=0, force=True` | ✅ | 否（收盘任务） | 是 | 收盘口径 | 空 → 停止候选 | 保持 refresh |
| `paper_trading.backfill_research_shadow:7760` | `max_age=0, force=True` | ✅ | 否（研究补齐） | 否 | 强刷新 | 空 | 保持 refresh |
| **`paper_trading.strategy_allocation_explain:8902`** | **`max_age=240`** | **✅ 隐式** | **✅ 是** | 否（只读解释） | 无（任何缓存都收） | 空 → `{}` | **Market Data Authority (read)** |
| `paper_trading.execute_open:10213` | `max_age=120` | ✅ | 否 | 是 | 20 分钟 | 空 → 阻断 | 保持 refresh |
| `paper_trading.run_auction_preselection:11464` | `max_age=90, force=True` | ✅ | 否 | 是 | 竞价窗口 | 空 → blocked | 保持 refresh |
| `paper_trading.monitor_intraday:12469` | `max_age=240` | ✅ | 否 | **是（signal provider stage）** | 20 分钟 | 二次 force 后仍空 → 停扫 | Market Data Authority (refresh) |
| `paper_trading.monitor_intraday:12486` | `max_age=0, force=True` | ✅ | 否 | 是 | 强刷新 | 同上 | 保持 refresh |
| `paper_trading.monitor_intraday:12590` | `_market_state(..., allow_network=True)` | ✅ | 否 | 是 | 当日 | unknown | 保持 refresh |
| `paper_trading.risk_dashboard:14327` | `max_age=240` | ✅ **但被 `allow_network=False` 挡住** | **✅ 是** | 否 | 300s/1800s | 占位 snapshot | Market Data Authority (read) |
| `main._select` 内 `_safe_live_snapshot:1388` | `max_age=240, force=…` | ✅ | 否 | 是 | 当日 + 20 分钟 | fail closed | 保持 refresh |
| `main.hot:1699` | `max_age=240` | ✅ | **✅ 是（`/api/hot`）** | 否 | 无 | 空 | Authority (read) |
| `manual_orders._manual_order_plan:247` | `max_age=240` | ⚠️ 受 `conn.in_transaction` 保护 | 否 | 是 | 无 | 空 → fail closed | 保持 |
| `manual_orders.submit_manual_order:731` | `max_age=240` | ✅ | 否 | 是 | 无 | 空 | 保持 |
| `manual_orders.process_pending_manual_orders:1045` | `max_age=240` | ✅ | 否 | 是 | 无 | 空 | 保持 |
| `universe.build_universe:89` | `max_age=300` | ✅ | 否（运维动作） | 否 | 300s | 保留旧名单 | Authority (refresh) |
| `trade_attribution._quote_maps:179` | `max_age=900` | ✅ | **✅ 是（归因报告）** | 否 | 900s | 空 | Authority (read) |
| `close_snapshot_runner.main:29` | `max_age=0, force=True` | ✅ | 否（cron） | 否 | 收盘 15:00 | stale_rejected | 保持 refresh |

### 1.2 个股实时 `fetch_realtime_for_codes` / `_independent_`（10 处）

| caller | 说明 |
|---|---|
| `paper_trading._quotes:6453` | 主源；失败保留 `source_errors["live"]`，不 abort 整轮风控 |
| `paper_trading._quotes:6478` | **独立核验源**（腾讯→新浪），失败保留 `source_errors["cross"]` |
| `decision_context.load_evidence:51` | **兼容适配器**，模块 docstring 已注明"未来应迁到 marketdata service" |
| `main.scanner:1780/1784`、`main.stock_detail:1926`、`main.industry_peers:2059` | 只读查询 |
| `tracker:74/93/148`、`trade_attribution.run_close_attribution:744` | 研究/归因 |
| `deepseek_advisor._secondary_quote_check:189` | **独立的**采样核验（与 `_quotes` 的 cross 不是同一套） |
| `data_fetcher.check_data_source_health:157` | provider 内部探活 |

### 1.3 其它 provider 面

- `fetch_indices`：仅 `_market_state:3773`（腾讯实时指数）。
- `fetch_sector_flow`：`main._warm_market_context:469`、`main.sectors:756`、`main.sector_events:762`、`main.stock_detail:1994`、`decision_context.load_evidence:66`。
- `fetch_hot_sector_snapshot`：`paper_trading.risk_dashboard:14343` 等；provider 内部 TTL 75s。
- `fetch_finance_latest`：未在 read path 直接调用。

---

## 二、已确认的真实维护债（实机复现）

`work/r23_round4_nonet_probe.py`（代理指向不可达端口，近似 CI `--network none`）：

```text
run1  13.786s  fetch_market_snapshot_full(max_age=240)
run2  13.665s  fetch_market_snapshot_full(max_age=240)

run1  13.823s  strategy_allocation_explain()
run2  13.827s  strategy_allocation_explain()
```

结论：`GET /api/paper/allocation-explain` 的**只读**请求，每次都同步穿透到 provider，
在 provider 不可达时支付 ~13.8s 连接超时/重试。这不是 R23 回归。

根因（`paper_trading.py:8900`）：

```python
quotes_map = {… for row in (dfc.fetch_market_snapshot_full(max_age=240) or []) …}
```

`fetch_market_snapshot_full` 在缓存陈旧时**自己决定**去访问网络（`data_fetcher.py:709-726`，
single-flight 锁内 `fetch_market_snapshot(pages=None, allow_disk_fallback=False)`）。
调用方无法表达"我只要当前已知事实，不许联网"。

同类问题（同一根因、不同入口）：
- `main.hot:1699`（`/api/hot` 只读）
- `trade_attribution._quote_maps:179`
- `risk_dashboard:14327` —— 已用 `allow_network=False` 手工挡住，但**用的是另一个机制**
  （占位 snapshot + 后台刷新触发），与 `_market_state(allow_network=…)` 并存，属于第 4 节要收敛的重复。

---

## 三、freshness / TTL 的散落情况（§14 审计）

| 值 | 位置 | 业务含义 | R24 处置 |
|---|---|---|---|
| 240 | `fetch_market_snapshot_full` 默认 | 全市场快照（实时/运行时/扫描） | 集中为 `REALTIME_QUOTE_POLICY` / `RUNTIME_VIEW_POLICY` / `SIGNAL_DECISION_POLICY` |
| 120 | `data_fetcher.fetch_market_snapshot` 内存 TTL；`paper_trading.execute_open:10213` | 分页快照内存缓存 / 开盘事件 | 保留（属 cache 机制 / 不同消费者） |
| 90 | `run_auction_preselection` | 集合竞价窗口 | 保留（窗口语义，非同一策略） |
| 300 | `universe.build_universe`；`risk_dashboard` 盘中 snapshot TTL | 名单构建 / 风控快照 | 保留 |
| 900 | `trade_attribution` | 归因报告 | 保留 |
| 75 | `fetch_hot_sector_snapshot` | 热点板块 | 保留（provider 内部） |
| 1800 | `risk_dashboard` 非盘中 | 风控快照 | 保留 |
| 240 | `data_fetcher.check_data_source_health:102` | 探活缓存 | 保留（provider health，非 data fact） |
| clock 判定 | `main.health:893` `age <= 1800` | `/api/health` 的 `fresh_rows` | **重复**：与 authority 的 freshness 判定并行 → 收敛为消费 authority projection |
| `RC.snapshot_ttl = 300/1800` | `risk_dashboard` | 快照陈旧 | **重复**（第 4 方）→ 收敛 |

结论：240 在**调用层**出现 4 次（同一业务策略，却各写一遍）；`/api/health`、
`risk_dashboard`、`main._select` 各自实现了一套"是否够新/是否完整"的判定。

---

## 四、真正值得收敛的重复实现（§24 证据）

1. **freshness 判定 owner 共 4 个**：
   `data_fetcher._fresh_full_snapshot_from_disk`（mtime + 最新 `quote_at`）、
   `main.health:866-909`（`saved_at` 年龄 + `fresh_rows`）、
   `risk_dashboard:14305-14314`（`RC.snapshot_age_seconds` + 300/1800）、
   `paper_quote_policy.quote_is_fresh`（个股 20 分钟）。
   四者口径不同、互不知情，都没有唯一的"这条行情事实可不可信"答案。

2. **network 允许性靠 call-site 手工表达**：`_market_state(allow_network=…)`、
   `risk_dashboard(allow_network=…/allow_stale=…)`、`manual_orders` 的
   `conn.in_transaction` 检查、`data_fetcher` 内部"cache miss → 自动联网"。
   **没有一个显式 contract**。

3. **同一事实两个权威**：`dfc.load_source_health()`（provider health）与
   `dfc.load_market_snapshot_full_cached()`（data fact）都被 `/api/health` 消费，
   但 `health` 在 `healthy=False` 时只加 warning，不影响 `live_snapshot.status`；
   而在 `_entry_freeze_status` 里 provider health **会**阻断建仓。两处语义不统一。

4. **`decision_context.load_evidence`** 的兼容适配器自己做了 provider 选择
   （`fetch_realtime_for_codes` / `load_cached_kline` / `fetch_sector_flow`），
   模块 docstring 明确写着"后续可以把适配器迁移到 marketdata service"。

---

## 五、§18 强制审计：network 与 DB writer transaction

当前 `BEGIN IMMEDIATE` / 写事务附近**已有**防护，R24 不得破坏：

- `manual_orders._manual_order_plan:244` 显式 `if conn.in_transaction: live_universe = []`
- `paper_trading._risk_base_dashboard` / `_cached_close_market(allow_network=False)`（`:10683`）
- `api_paper.risk_overview` 用 `allow_network=False` + 占位快照
- Guard 14s 已断言 signal commit phase 的写锁内**不含** provider 调用

R24 引入 authority 后，refresh 仍必须在写事务**之外**发生；read 模式
**不可能**触网（不是靠调用方自觉，而是靠 authority 自己不开网络）。

---

## 六、R24 判定

### 真缺口（本 PR 修）

- **G1**：只读路径无法表达"不联网"，`allocation-explain` 每次 read 支付 ~13.8s。
- **G2**：freshness 判定有 4 个 owner，没有唯一答案。
- **G3**：`None` / `{}` / `[]` / "stale" 字符串混用，调用方靠猜区分
  「没数据」「网络失败」「陈旧」「provider 冲突」。
- **G4**：network 允许性不是显式 contract，靠 `allow_network` / `in_transaction` 手工传递。
- **G5**：provider health 与 data fact 没有分离的 projection（`/api/health` 自己混合）。
- **G6**：`decision_context.load_evidence` 的隐式 provider 选择未收敛。

### 不是缺口（写 regression 锁住，不弱化）

- 多源核验（`_quotes:6530-6587`）：价格差 0.5% / 涨跌差 0.20pp、`|pct|>=3%` 时容差 ×2、
  日期/时间戳校验、`cross_source_unavailable` 与 `cross_source_failed` 区分 —— **必须逐条保留**。
- 单票 freshness（`paper_quote_policy.quote_is_fresh` 20 分钟）—— 不同业务消费者，不合并。
- `_validated_live_universe` / `_live_scan_gate` —— 横截面覆盖门禁，保留在决策层。
- provider 内部机制（分页完整性、主机轮换、熔断、single-flight）—— `data_fetcher` 继续持有。

---

## 七、R24 落地方案

```text
marketdata_transport / providers / normalizers / cache     （provider mechanics，不动）
              ↓
        data_fetcher  （provider + cache 存储/投放机制）
              ↓
  ┌───────────────────────────────────────────────┐
  │ backend/market_data_contract.py   纯契约       │
  │   status / freshness / verification / reason   │
  │   MarketDataPolicy（唯一的 freshness 来源）    │
  │   classify_market_data()  纯分类函数           │
  ├───────────────────────────────────────────────┤
  │ backend/market_data_service.py   唯一 authority│
  │   snapshot_for_read()     ← 只读缓存，绝不联网 │
  │   snapshot_for_refresh()  ← 显式允许联网       │
  └───────────────────────────────────────────────┘
              ↓  MarketDataSnapshot / MarketDataReading
   allocation-explain / runtime view / health projection /
   signal provider stage / risk dashboard
```

迁移顺序：`allocation-explain` → runtime read projection → 前端 → `monitor_intraday`
（signal provider stage）→ 删除零消费者的旧直接路径。
