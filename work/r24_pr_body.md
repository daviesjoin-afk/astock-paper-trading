# refactor(market-data): establish verified market-data boundary

## 这一轮实际建立/取消了什么

**建立**了 Market Data 的**唯一业务权威**：`market_data_contract.py`（纯契约，零 I/O /
零时钟 / 零项目依赖）持有状态语义与 freshness policy，`market_data_service.py` 持有
唯一取数入口，分两个**显式** access mode：

```text
read_snapshot()     ACCESS_READ    只读缓存/持久化事实，绝不联网
refresh_snapshot()  ACCESS_REFRESH 显式允许联网，必须由调用方主动选择
```

**取消了**上层对 provider 的直接 ownership：生产 `fetch_market_snapshot_full`
调用点从 **18 处收敛到 0 处**。`paper_trading.py` 现在**没有任何**直接 provider 调用。
provider 实现本身仍留在 `data_fetcher.py`——R24 不重写 provider。

## 哪些 read path 不再同步访问网络

`GET /api/paper/allocation-explain` 是 R23 已确认的真实维护债。它此前每次只读请求都会
穿透到 provider。

在同一条件下（代理指向不可达端口，近似 CI `--network none`）实测：

```text
                    迁移前          迁移后
fetch_market_snapshot_full   13.79s / 13.67s   （未变，provider 机制不动）
strategy_allocation_explain  13.82s / 13.83s   →  0.104s / 0.091s
provider 调用数              ≥1                →  0
```

`work/r24_readpath_no_network_check.py` 把 6 个 provider 入口全部 monkeypatch 成
`raise AssertionError`，production read entry 仍正常返回：

```text
elapsed=0.108s  network attempts: 0
market_data: {'status': 'unavailable', 'availability': 'unavailable', ...'reason': 'missing'}
strategies: 5
```

同时迁走的只读路径：runtime view（`dashboard` 的 `market_data` 投影）、`/api/hot`、
`/api/health`、`trade_attribution`。**8 个只读 API 的同步 provider 刷新从 4 处降到 0 处。**

## fresh / stale / degraded / unavailable 如何解释

保留**四个正交维度**，明确**不**压成 `quality_score`：

| 维度 | 取值 |
| --- | --- |
| `availability` | `available` / `unavailable` |
| `freshness` | `fresh` / `stale` / `unknown` |
| `verification` | `verified` / `single_source` / `disagreement` / `unavailable` / `not_attempted` |
| `as_of` | 这条事实对应的业务日 |

派生 `status` 只有 5 个值：`fresh` / `stale` / `degraded` / `unverified` / `unavailable`，
外加稳定的 `reason`（`missing` / `stale` / `incomplete` / `provider_unavailable` /
`refresh_failed` / `cross_source_failed` / `asof_unprovable` / `asof_mismatch`）。

关键语义分离（此前靠 `None` / `{}` / 空 list / magic string 让调用方猜）：

- **STALE ≠ UNAVAILABLE**：有最后一份可信 snapshot 但过期 → 返回 `stale` **加**完整 rows
  （`test_MDR03` 断言 rows 仍为 1 条）；完全没有 → `unavailable`，不携带 payload。
- **DISAGREEMENT ≠ UNAVAILABLE，也 ≠ VERIFIED**：多源冲突报 `unverified`，绝不静默挑一个
  源；核验源本身不可用报 `degraded`——两者是不同结论（`test_MD07`）。
- **未来漂移的源时间戳不可采信**：`abs()` 语义与既有
  `data_fetcher._fresh_full_snapshot_from_disk` 一致，不因"看起来更新"判 fresh（`test_MD10`）。

## 多源核验如何保持

**未弱化，也未迁移**。逐票双源核验（价格差 0.5% / 涨跌差 0.20pp、`|pct|>=3%` 时容差 ×2、
日期与时间戳校验、`cross_source_unavailable` 与 `cross_source_failed` 的区分）仍在
`paper_trading._quotes` 原位，语义一字未改。

本轮的增量是**给这套既有语义一个契约表达**：`verification_from_cross_status()` 把既有的
`quote_validation` 术语映射成核验维度（`cross_source_checked` → `verified`、
`cross_source_failed` → `disagreement`），复用既有业务术语而不是发明第五个名字。
全市场横截面快照的核验机制是**完整性与覆盖**（既有 `complete` marker + 4000 行门槛 +
90% 期望覆盖），不是逐票双源核验——contract 不冒充（`test_MDP01b`：一行"全市场快照"
被判 `incomplete`，不标 fresh）。

## historical PIT 如何保持

`classify(..., asof_day=)` 强制"只能使用该日或更早可证明的观测"：

- `observed_day > requested` → `asof_mismatch`，**不携带 payload**（`test_MDPIT01`）
- 无法证明业务日 → `asof_unprovable`（`test_MDPIT03`）
- `observed_day <= requested` → 允许（`test_MDPIT04`，不是"任何晚于都拒绝"）

**绝不拿 current snapshot 回填历史。** 这是 §15 的绝对禁止项，`M-MD4` mutation 专门锁它。

## 前端新增了什么用户可见状态

不新增顶级页面，融入现有两处：

- **运行策略页**（`paper-runtime-strategies`）：`data-testid="paper-market-data-status"`，
  显示 `行情：可信/已过期/降级/未通过核验/不可用 · 最后可信时间 … · 原因：…`
- **当前持仓页**总资金池卡片：同一投影。

前端**只渲染**：消费后端给的 `status` / `as_of` / `reason` / `verification`，
不重算 freshness（无 `Date.now()-timestamp > 240000` 式逻辑），也不理解 provider 机制
（重试/熔断/缓存键不进普通 UI）。`test_MDG05` 静态锁这两条。

## 哪些旧重复路径被删除

- `paper_trading` 里 `_market_state` / `generate_signals` / `backfill_research_shadow` /
  `execute_open` / `run_auction_preselection` / `monitor_intraday` / `risk_dashboard` /
  `strategy_allocation_explain` 各自的 `try/except + max_age=240` 内联块 → 统一为
  `MDSvc.read_snapshot` / `MDSvc.refresh_rows` 调用。
- `main.health` 曾用裸 `open(dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH)` 读快照，
  **绕过** `_full_snapshot_payload_is_complete` 的完整性校验 → 改走 authority
  （由 `test_MDG08` 的 AST 守卫发现并锁住）。
- 调用层散落的 `max_age=240/120/90/300/900/0` → 6 个具名 policy
  （`LIVE_MARKET` / `OPENING_EVENT` / `AUCTION_PRESELECTION` / `UNIVERSE_BUILD` /
  `ATTRIBUTION` / `CLOSE_SNAPSHOT`）。
- `universe.build_universe` / `manual_orders`（3 处）/ `close_snapshot_runner` /
  `main._safe_live_snapshot` / `trade_attribution._quote_maps` 的同名直接调用一并迁移。

## 与后续 R25 / R27 的接口

contract 刻意保持 `small / explicit / stable`，但不写死成 `only-for-paper-trading`：
`MarketDataReading.projection()` 是给 R25 Signal Pipeline 与 R27 AI Research 的稳定输入
（market facts + as_of + source evidence + freshness + verification）。R27 的 AI 只能在
**可信事实**之上做解释/研究/假设，不会自己变成行情事实来源——本轮不接 LLM。

## 本轮明确不做

策略/因子/选股逻辑、收益优化、风险阈值、AI（R27）、Signal Pipeline 全量重构（R25）、
Execution Fidelity（R26）一律未动。未新增订单/成交/资金/风控写入路径。

---

## Architecture / Maintainability

```text
Market-data authority:
before: 无（散落在 data_fetcher / main.health / risk_dashboard /
        paper_trading._market_state / dashboard_queries，各写一套 freshness）
after:  market_data_contract.py（状态语义 + policy）+ market_data_service.py（唯一取数入口）

Direct provider production call sites (fetch_market_snapshot_full):
before: 18
after:  0

Read paths that can synchronously hit provider:
before: 4（allocation-explain / hot / health / trade_attribution）
after:  0

Freshness decision owners（**全市场行情事实的业务判决**）:
before: 3
  1. data_fetcher._fresh_full_snapshot_from_disk（cache 层：mtime + 内容 quote_at）
  2. main.health（业务判决：自算 `age <= 1800` 再拼一个 fresh/closed_snapshot/stale 三元）
  3. paper_trading 各调用点（240 / 120 / 90 / 300 / 900 / 0 六套字面量）
after:  1（market_data_contract.MarketDataPolicy + classify）

注（诚实边界）：
- `data_fetcher._fresh_full_snapshot_from_disk` 仍在。它回答的是 cache 层
  "要不要真的去取源"，属存储/投放机制，**不是**业务判决；R24 把业务判决收敛到
  唯一 owner，并由 MDG08/MDG09 锁住"其他模块不得再判第二次"。
- `risk_dashboard` 的 `snapshot_ttl = 300/1800` **不在**这个计数里：它判的是
  `risk_center` 自己持久化的风控快照（另一个 artifact），不是全市场行情事实。
  本轮不合并这两个口径（消费者不同）。
- `paper_quote_policy.quote_is_fresh`（个股 20 分钟）同样不在计数里：它是逐票
  成交门禁，与横截面快照是不同生命周期。

Provider disagreement decision owners:
before: 2（paper_trading._quotes 逐票核验；deepseek_advisor._secondary_quote_check 独立采样）
after:  2（未合并——两者业务消费者不同；新增 contract 表达，不改变既有 owner）

New business authority:
1 —— market_data_service（含 market_data_contract 的状态语义）

Removed duplicate authority:
0 个模块整体删除；1 处重复读取实现被收敛（main.health 的裸 open 绕过校验）
+ 调用层 6 个散落 TTL 收敛为具名 policy

New facade/wrapper count:
0（刻意不做 compatibility facade；调用方直接调 authority，无 v1/v2/legacy 层）

Removed facade/wrapper count:
0

Implicit current-state lookup added:
0

Frontend duplicated business rules:
0

Large if/elif decision chain added:
NO（classify() 是单一纯函数 + 固定 _VERIFICATION_RANK 数据表；简单 guard 保持直接 if）

Compatibility paths removed:
main.health 的裸文件读取；6 处调用层 max_age 字面量；8 处 try/except+provider 内联块

理解"当前行情是否可信"需要查看：
before = 4 production modules（data_fetcher / main / paper_trading / dashboard_queries）
after  = 1 production module（market_data_contract.py 的状态语义；
         + market_data_service.py 只回答"能不能联网"）

paper_trading.py:
LOC = 14895（baseline 14896，净 -1）
defs = 280（不变）
仅趋势，不作为 blocker
```

## Frontend sync

```text
Market data status visible:        YES
Freshness visible:                 YES（status + as_of/observed_at）
as-of visible:                     YES
degraded/unavailable reason:       YES
Frontend recalculates freshness:   NO
Frontend knows provider mechanics: NO
New standalone page added:         NO
```

---

## 测试

```text
R24 targeted: 45 tests（backend/test_market_data_boundary.py）

modules:
  MD-01..MD-14      契约：状态维度 / policy 单一来源 / 边界 / 投影 / 纯函数无时钟
  MDR-01..MDR-07    只读不联网 + stale & unavailable positive control
  MDP-01..MDP-10    refresh / provider 失败 / 缓存 / 多源映射 parity
  MDPIT-01..04      点时可证明性（严禁 current 回填）
  MDG-01..MDG-09    架构守卫（AST / import-level）

mutation: caught=5 survived=0 fake=0（work/r24_mutation_check.py --non-vacuity）
  M-MD1 read_snapshot 偷偷允许联网
  M-MD2 stale 被标成 fresh
  M-MD3 provider 冲突被静默当成可用
  M-MD4 historical 请求 fallback current
  M-MD5 unavailable 被默认值填充

backend full:  4051 tests, OK, skipped=5
frontend unit: 118/118（含 7 条 R24 新增：frontend/tests/market-data-status.test.mjs）
architecture guard: 114/114

paper-runtime repeated: 5/5 clean（每轮 2 passed, 0 retry, 0 flaky）
full browser: 32/32, flaky=0, retry=0

security worktree: PASS（kinds: none, values: 0, exit 0）
security all:      PASS（kinds: none, values: 0, exit 0）
```

前端新增测试做的是**行为断言**，不是源码存在性断言：直接 import 真实的
`src/features/paper.js`，调用真实 `paperMarketDataHtml()`，断言可观测输出
（五种状态 → 标签、as-of 原样、reason 按后端语义、未知值透出、缺失 payload 降级为
`unavailable`），并用源码守卫锁住"不重算 freshness / 不泄露 provider 机制"。
非空验证：把 `var status=String(md.status||'unavailable')` 改成 `var status='fresh'`
后 **3/7 条变 RED**，还原后 7/7 绿。

### 性能对照（no-network 模式）

```text
                            before        after
allocation-explain          13.82s        0.104s
provider calls from read     ≥1            0
返回的 market-data status    （无此概念）    unavailable / stale（如实）
```

关键验收不是固定毫秒数，而是：**NO NETWORK 模式下 read path 延迟不再由 provider
timeout 主导**——本轮从 13.8s 降到 0.10s，且 read path 在无缓存时明确报 `unavailable`
而不是制造"看起来是正常行情"的假数据。

## 交付格式

```text
PR:    （待创建）

BASE:  2afcbecad0ee7966130d41b30711bbc5ebc38660
HEAD:  （见 PR head）
MASTER: 2afcbecad0ee7966130d41b30711bbc5ebc38660
MERGE-BASE: 2afcbecad0ee7966130d41b30711bbc5ebc38660
BASE DRIFT: NO

R24 MARKET DATA AUTHORITY

authority before: 无
authority after:  market_data_contract + market_data_service

direct provider call sites:      before = 18   after = 0
read paths w/ sync provider net: before = 4    after = 0
freshness owners:                before = 3    after = 1
provider disagreement owners:    before = 2    after = 2（未合并，消费者不同）

KNOWN R23 DEBT

allocation-explain synchronous refresh: FIXED
offline read path provider wait:        before = 13.8s  after = 0.10s
network calls from allocation-explain:  before = ≥1     after = 0

CONTRACT

fresh:                 PASS
stale:                 PASS
unavailable:           PASS
provider disagreement: PASS
one-provider failure:  PASS
all-provider failure:  PASS
PIT / historical:      PASS
explicit network policy: PASS

FRONTEND

market-data status:   PASS
freshness/as-of:      PASS
degraded reason:      PASS
frontend duplicated freshness logic: 0
frontend provider mechanics:         0

TESTS

R24 targeted: 45
mutation: caught=5 survived=0 fake=0
backend full: 4051 tests, skipped=5
frontend unit: 118/118
paper-runtime repeated: 5/5 clean
full browser: 32/32, flaky=0, retry=0
security worktree: PASS
security all: PASS

ARCHITECTURE / MAINTAINABILITY

new authority: market_data_service（+ market_data_contract 状态语义）
removed duplicate authority: main.health 裸文件读取绕过校验
new facade/wrapper: 0
removed facade/wrapper: 0
implicit current-state lookup added: 0
large if/elif chain added: NO
compatibility path removed: 6 处调用层 max_age 字面量 + 8 处 try/except 内联块
business-rule lookup: before = 4 modules   after = 1 module
paper_trading.py: LOC=14895  defs=280

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
STATUS: AWAITING HUMAN REVIEW
```

## 证据脚本

- `work/r24_authority_audit.md` —— 逐调用链审计（18 个 provider 调用点分类、
  freshness 4 owner、§18 network/transaction 审计、逐项判定真缺口）
- `work/r24_readpath_no_network_check.py` —— 只读路径零网络证明（provider 入口全部
  monkeypatch 成断言失败）
- `work/r24_mutation_check.py` —— 5 条语义 mutation，带 `--non-vacuity`
- `work/r23_round4_nonet_probe.py` —— 本轮 before/after 延迟对照所用探针（沿用 R23）
