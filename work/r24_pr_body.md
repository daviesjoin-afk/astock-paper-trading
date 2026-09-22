# refactor(market-data): establish verified market-data boundary

> **复审修正（第二轮）**：首轮提交后有 4 项被判定为合并前必须收掉的问题，均已在
> 本 head 修复并加了永久回归。逐条见文末「复审修正」一节。核心结论未变：
> R24 主方向正确、`allocation-explain` 债务已修；修正的是**收口完整性**、
> **verification 语义真实性**、**health 元数据/新鲜度 parity** 与**证据精确性**。

## 这一轮实际建立/取消了什么

**建立**了 Market Data 的**唯一业务权威**：`market_data_contract.py`（纯契约，零 I/O /
零时钟 / 零项目依赖）持有状态语义与 freshness policy，`market_data_service.py` 持有
唯一取数入口，分两个**显式** access mode：

```text
read_snapshot()     ACCESS_READ    只读缓存/持久化事实，绝不联网
refresh_snapshot()  ACCESS_REFRESH 显式允许联网，必须由调用方主动选择
```

**取消了**上层对 provider 的直接 ownership：上层（`paper_trading` / `main` /
`manual_orders` / `universe` / `trade_attribution` / `close_snapshot_runner` /
`dashboard_queries`）的 `fetch_market_snapshot_full` 调用点从 **18 处收敛到 0 处**。
现在全仓只剩 **1 处**调用，且在 authority 自己内部
（`market_data_service.refresh_snapshot`）——这正是"唯一权威"应有的形状。
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

`work/r24_readpath_no_network_check.py` 把**所有会发起 HTTP 的 provider 入口**
（含 `fetch_hot_rank`）全部 monkeypatch 成 `raise AssertionError`，逐个驱动 9 条只读
入口：

```text
    0.000s  market_data_service.read_snapshot
    0.000s  market_data_service.read_projection
    0.000s  market_data_service.read_snapshot_legacy_shape
    0.071s  strategy_allocation_explain() market_data=unavailable
    0.174s  dashboard() market_data=unavailable
    0.001s  linkage.sector_linkage()
    0.049s  adaptive_engine._snapshot_rows
    0.000s  deepseek_advisor._read_snapshot
    0.000s  ai_analysis._read_snapshot

total provider calls attempted from read paths: 0
PASS: 只读路径零 provider 网络调用
```

**声明边界（本轮精确化，不夸大）**：本 PR 收敛的是 **full-market snapshot 这一类事实**
的只读消费。以下**不是** GET 只读路径，仍允许联网，脚本与 `test_MDG06` 都显式钉住这个事实：

- `GET /api/hot` → `fetch_hot_rank`（东财**人气榜**，独立 artifact，非 full-market 快照）。
  它**仍会**同步取榜；首轮把 `/api/hot` 描述成"network-free read path"是不准确的。
- `POST /api/selection-evaluation/refresh` → `selection_tracking.update_observations`
  （显式刷新动作）。
- 盘后归因在无已知事实时的 fallback refresh（`trade_attribution._quote_maps`）。

因此正确的指标口径是：**full-market snapshot 的同步 provider 刷新从 4 处降到 0 处**，
而不是"整个 read API 都不联网"。

## fresh / stale / degraded / unavailable 如何解释

保留**五个正交维度**，明确**不**压成 `quality_score`：

| 维度 | 取值 |
| --- | --- |
| `availability` | `available` / `unavailable` |
| `freshness` | `fresh` / `stale` / `unknown` |
| `verification` | `verified` / `single_source` / `disagreement` / `unavailable` / `not_attempted` |
| `verification_method` | `cross_source` / `coverage_integrity` / `none` |
| `as_of` | 这条事实对应的业务日 |

派生 `status` 只有 5 个值：`fresh` / `stale` / `degraded` / `unverified` / `unavailable`，
外加稳定的 `reason`（`missing` / `stale` / `incomplete` / `provider_unavailable` /
`refresh_failed` / `cross_source_failed` / `asof_unprovable` / `asof_mismatch`）。

关键语义分离（此前靠 `None` / `{}` / 空 list / magic string 让调用方猜）：

- **STALE ≠ UNAVAILABLE**：有最后一份可信 snapshot 但过期 → 返回 `stale` **加**完整 rows
  （`test_MDR03` 断言 rows 全部保留）；完全没有 → `unavailable`，不携带 payload。
- **DISAGREEMENT ≠ UNAVAILABLE，也 ≠ VERIFIED**：多源冲突报 `unverified`，绝不静默挑一个
  源；核验源本身不可用报 `degraded`——两者是不同结论（`test_MD07`）。
- **`verified` ≠ 多源核验过**（复审修正）：`verified` 只表示"该 kind 的 policy 通过了"，
  具体是哪一套由 `verification_method` 表达；构造期即拒绝 `verified` 配 `none`
  （`test_MD15` / `test_MD16`），双源保证必须用 `is_cross_source_verified()`。
- **横截面新鲜度看覆盖比例，不看最新一行**（复审修正）：`MIN_FRESH_RATIO = 90%`。
  "3999 条隔夜旧数据 + 1 条刚更新"因为完整 payload 且最新一行够新而被判 `fresh` 是错的，
  现在正确判 `stale`（`test_MD18`）。
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

Direct access to the full-market snapshot artifact（按**通道**拆分，因为有三条通道）:

channel A `fetch_market_snapshot_full(...)`:
  before: 17（paper_trading 9 / main 2 / manual_orders 3 / universe 1 /
           trade_attribution 1 / close_snapshot_runner 1）
  after:  1（且只在 authority 内部：market_data_service.refresh_snapshot）
          上层调用点 = 0

channel B `fetch_market_snapshot(pages=None...)`（同一 artifact 的第二个入口）:
  before: 4（paper_trading 1 / universe 1 / linkage 1 / selection_tracking 1）
  after:  0
  （`api_adaptive.fetch_market_snapshot(pages=20)` 不在此列：pages=20 写的是
    `market_snapshot_sample_20.json`，是**不同** artifact，属风险样本用途）

channel C 按路径裸读 `market_snapshot_full.json` / `market_snapshot.json`:
  before: 5 个裸读读取器，分布在 4 个模块
            （trade_attribution._market_snapshot、adaptive_engine._snapshot_rows、
              adaptive_engine._data_input_state、deepseek_advisor._read_snapshot、
              ai_analysis._read_snapshot）
          其中**全部**都会回退到 20 页风险样本 `market_snapshot.json`，
          且都绕过 `_full_snapshot_payload_is_complete` 的完整性校验。
  after:  0（写方 data_fetcher 与演示夹具 demo_seed 除外）
          —— `MDG10` 按**字面路径**检查，能抓出这类改写（已验证）。

三条通道合计：before **26** 处 → after **1** 处（仅在 authority 内部）。
首轮只报了 channel A 的 17 处，且把 channel C 误判为"已迁移"（实际只迁移了它的
fallback 分支），这正是复审第 1 条指出的收口不完整。

Read paths that can synchronously hit provider（**full-market snapshot 这一类**）:
before: 4（allocation-explain / health / trade_attribution / adaptive 系）
after:  0
注（复审修正后的精确口径）：这不是"整个 read API 都不联网"。仍允许联网且**不是**
GET 只读路径的有：`GET /api/hot` 的 `fetch_hot_rank`（东财人气榜，独立 artifact）、
`POST /api/selection-evaluation/refresh`、盘后归因的 fallback refresh。
`test_MDG06` 显式断言 `hot()` 仍会取榜，防止这类声明再次被写宽。

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
- health 的 1800s 展示口径现已由 `MARKET_HEALTH_POLICY` 在 authority 内执行
  （此前硬编码在 `main.health`）——它是**同一 owner 的第二个 policy**，
  不是第二个 owner。

Provider disagreement decision owners:
before: 2（paper_trading._quotes 逐票核验；deepseek_advisor._secondary_quote_check 独立采样）
after:  2（未合并——两者业务消费者不同；新增 contract 表达，不改变既有 owner）

New business authority:
1 —— market_data_service（含 market_data_contract 的状态语义）

Removed duplicate authority:
0 个模块整体删除；1 处重复读取实现被收敛（main.health 的裸 open 绕过校验）
+ 调用层 6 个散落 TTL 收敛为具名 policy

Guard policy (R24 final cleanup):
R24 final cleanup removed the legacy LOC/top-level-def hard gates.
Architecture CI now protects semantic ownership and dependency invariants
instead of mechanical file-size ceilings.

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
before = 4 production modules（data_fetcher 的 cache TTL / main.health 的二次判决 /
         paper_trading 各调用点的 max_age 字面量 / dashboard_queries 的读取路径）
after  = 2 production modules（market_data_contract.py 判状态语义 +
         market_data_service.py 判能不能联网），且二者构成**同一个 authority**，
         不再有第二个业务判决（MDG08 / MDG09 锁住）

paper_trading.py:
LOC = 14895
defs = 280
仅作为 **architecture trend observation**，不参与 pass/fail
（R24 收尾已删除旧的 LOC / top-level-def hard gate，见下方「收尾修复」）
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
R24 targeted: 54 tests（backend/test_market_data_boundary.py）

modules:
  MD-01..MD-14      契约：状态维度 / policy 单一来源 / 边界 / 投影 / 纯函数无时钟
  MDR-01..MDR-07    只读不联网 + stale & unavailable positive control
  MDP-01..MDP-10    refresh / provider 失败 / 缓存 / 多源映射 parity
  MDPIT-01..04      点时可证明性（严禁 current 回填）
  MDG-01..MDG-09    架构守卫（AST / import-level）

mutation: caught=9 survived=0 fake=0（work/r24_mutation_check.py --non-vacuity）
  M-MD1 read_snapshot 偷偷允许联网
  M-MD2 stale 被标成 fresh
  M-MD3 provider 冲突被静默当成可用
  M-MD4 historical 请求 fallback current
  M-MD5 unavailable 被默认值填充

backend full:  4060 tests, OK, skipped=5
frontend unit: 118/118（含 7 条 R24 新增：frontend/tests/market-data-status.test.mjs）
architecture guard: 114/114

paper-runtime repeated: 5/5 clean（每轮 2 passed, 0 retry, 0 flaky）
full browser: 32/32, flaky=0, retry=0

security worktree: PASS（kinds: none, values: 0, exit 0）
security all:      PASS（kinds: none, values: 0, exit 0）
exact-head CI:     PASS（9/9 —— tests 3.11 / tests 3.12 / syntax / quality /
                   docker-smoke / frontend / browser-e2e / security-leak-scan ×2；
                   browser 日志人工核对：32 passed, 0 retry, 0 flaky, 无 timeout）
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
PR:    #182（https://github.com/daviesjoin-afk/astock-paper-trading/pull/182）

BASE:  2afcbecad0ee7966130d41b30711bbc5ebc38660
MASTER: 2afcbecad0ee7966130d41b30711bbc5ebc38660
MERGE-BASE: 2afcbecad0ee7966130d41b30711bbc5ebc38660
BASE DRIFT: NO

EXACT-HEAD VERIFICATION:
  GitHub Actions checks on current PR HEAD（不在此记录 SHA 快照 ——
  merge authority 是当前 HEAD 的 exact-head CI；人工审核时从 GitHub API 读取）
  要求：tests 3.11 / tests 3.12 / syntax / quality / docker-smoke /
        frontend / browser-e2e / security-leak-scan 全绿，
        且 browser log 无 flaky / 无 retry / 无 timeout。

R24 MARKET DATA AUTHORITY

authority before: 无
authority after:  market_data_contract + market_data_service

direct provider call sites:      before = 26   after = 1（仅在 authority 内部；
                                              上层调用点 = 0）
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

R24 targeted: 54
mutation: caught=9 survived=0 fake=0
backend full: 4060 tests, skipped=5
frontend unit: 118/118
paper-runtime repeated: 5/5 clean
full browser: 32/32, flaky=0, retry=0
security worktree: PASS
security all: PASS
exact-head CI: PASS（9/9 checks，head cf8226196d5572e2540cc366d3c0a3a8c08104be；
               CI Python 3.12 实际 4060 tests OK skipped=1，
               browser-e2e 32/32、1 worker、0 retry / 0 flaky）

ARCHITECTURE / MAINTAINABILITY

new authority: market_data_service（+ market_data_contract 状态语义）
removed duplicate authority: main.health 裸文件读取绕过校验
new facade/wrapper: 0
removed facade/wrapper: 0
implicit current-state lookup added: 0
large if/elif chain added: NO
compatibility path removed: 6 处调用层 max_age 字面量 + 8 处 try/except 内联块
business-rule lookup: before = 4 modules   after = 2 modules（同属一个 authority）
paper_trading.py: LOC=14895  defs=280（trend only，non-blocking）

MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
STATUS: AWAITING HUMAN REVIEW
```

## 证据脚本

- `work/r24_authority_audit.md` —— 逐调用链审计（18 个 provider 调用点分类、
  freshness owner、§18 network/transaction 审计、逐项判定真缺口）
- `work/r24_readpath_no_network_check.py` —— 只读路径零网络证明（**所有**会发起 HTTP 的
  provider 入口 monkeypatch 成断言失败；9 条只读入口）
- `work/r24_mutation_check.py` —— 9 条语义 mutation，带 `--non-vacuity`
- `work/r23_round4_nonet_probe.py` —— 本轮 before/after 延迟对照所用探针（沿用 R23）

---

## 复审修正（第二轮）

首轮提交后有 4 项被判定为合并前必须收掉。以下逐条说明**问题、根因、修法与回归**。

### 1. full-market authority 没有真正唯一（P1/P2）

**问题**：同一批 full-market artifact 仍有多条绕过 authority 的路径：

- `paper_trading.monitor_intraday()` 的诊断分支直接 `dfc.fetch_market_snapshot(pages=None, allow_disk_fallback=True)`
- `universe.star_leader_mapping()` 直接 `dfc.fetch_market_snapshot()`
- `trade_attribution._market_snapshot()` 直接 `open(market_snapshot_full.json)`，
  **并在失败时回退 `market_snapshot.json`**
- 复查中另外发现：`adaptive_engine`（`_snapshot_rows` / `_data_input_state`）、
  `deepseek_advisor._read_snapshot`、`ai_analysis._read_snapshot` 也都裸读同一 artifact，
  且同样回退 20 页风险样本

**根因**：迁移只覆盖了 `fetch_market_snapshot_full`，漏掉了 **`fetch_market_snapshot`
与按路径裸读**两条同 artifact 通道；首轮的 `MDG08` 守卫只匹配属性名
`MARKET_SNAPSHOT_FULL_CACHE_PATH`，而上述模块用的是**字面文件名**，所以守卫假绿。
回退到 `market_snapshot.json` 是实质正确性问题：那是 20 页风险样本（约 1/25 个市场），
把它当全市场快照算板块/个股涨跌会系统性歪曲归因结论。

**修法**：

- 全部改经 authority（`read_snapshot` / `read_snapshot_with_meta` /
  `read_snapshot_legacy_shape` / `refresh_rows`）。归因与 adaptive 的 raw cache read 已删除。
- 新增 `data_fetcher.load_market_snapshot_full_payload()`：**保留元数据**的校验读取器，
  替代裸 `open()`。
- `universe.star_leader_mapping()` 全仓**零调用方**（含前端/E2E/测试），按 §35
  「零消费者的 compat 路径删除」直接删除，而不是把死代码迁移。

**回归**：`test_MDG10`（按**字面路径**检查裸读，排除写方与演示夹具），
已用变异验证能精确抓出（在 `linkage.py` 插入 `open('market_snapshot_full.json')`
→ `MDG10` RED，报告 `linkage.py:open@line56`）。另新增 `MDG08` 的属性名通道。

### 2. `verification="verified"` 的语义是假的（P1/P2）

**问题**：契约把 `VERIFICATION_VERIFIED` 定义为"多源核验通过"，但
`_load_cached_snapshot()` / `refresh_snapshot()` 对**单一 Eastmoney** 全市场快照，
只要 completeness/coverage 通过就直接标 `verified`。这会让单源快照在契约层看起来像
双源验证过——而 R25/R27 会直接消费这个 contract。

**根因**：把"哪个 kind 的 policy 通过了"和"通过的是哪一套 policy"压成了一个枚举值。

**修法**：新增正交维度 `verification_method`：

- `cross_source` —— 逐票第二独立源核验（这才是"多源核验"的字面含义）
- `coverage_integrity` —— 完整性与覆盖（complete marker + 4000 行门槛 + 90% 期望覆盖）
- `none` —— 未做任何核验

`verified` 的含义固定为"**该 kind 的 verification policy 已通过**"；
full-market 标 `verified` + `coverage_integrity`。构造期校验强制状态与 method 相容
（`verified` 不许配 `none`；`not_attempted` 不许配任何真实 method），因此这类错误
**不可能**被写出来。新增 `is_cross_source_verified()` 作为显式判据，需要双源保证的
消费者（AI 调参门禁、R25/R27）必须调用它，而不是比较 `verification == "verified"`。
`verification_detail` 同时带上 `policy` / `rows` / `unique_codes` / `expected_rows`，
让"凭什么说它完整"可审计，而不是一个裸布尔。

**回归**：`test_MD15`（verified 必须带 method + `is_cross_source_verified` 语义）、
`test_MD16`（状态/method 相容性）、`test_MD17`（投影必须给出 method）、
`test_MDR01`（单源完整快照不得自称双源）；mutation `M-MD6` / `M-MD8`。

### 3. `/api/health` 的 metadata 与 freshness 行为未声明变化（P2）

**问题**（三个独立缺陷）：

1. `_load_cached_snapshot()` 只取 `rows`，把 `saved_at=None` / `expected_rows=0`，
   于是 `live_snapshot.saved_at` 与基于它算的 `age_seconds` 实际丢失。
2. 旧 health 判 `fresh` 要求盘中至少 4000 条当日且 30 分钟内的 fresh rows；
   authority 只看**最新一条 quote_at** 是否在 240s 内 → "3999 条旧 + 1 条新"
   理论上可能被投影成 fresh。
3. health 的 1800s 展示口径被 240s 实时口径顶替。

**根因**：authority 只保留 rows、丢掉了 payload metadata；契约缺少**横截面**新鲜度概念
（只有单点"最新观测时点"）；且把两个不同业务问题的 policy 合并成了一个。

**修法**：

1. authority 保留完整 payload：新增 `read_snapshot_with_meta()` 返回
   `(reading, payload)`，`market_health_projection()` 增加 `snapshot_meta`
   段（`saved_at` / `expected_rows` / `rows` / `complete`）。
2. 契约新增 `min_fresh_ratio`：横截面 policy 要求窗口内行数占**可解析总行数**达 90%
   （`CROSS_SECTION_MIN_FRESH_RATIO`），并把 `fresh_ratio` 写进 `verification_detail`
   以便审计。单点 kind（名单构建）不设比例要求，退化为"最新一条在窗口内"。
3. 新增 `MARKET_HEALTH_POLICY`（1800s）由 authority 执行，health 消费它，
   **不**并入 `LIVE_MARKET_POLICY`（240s）——两者的业务问题不同
   （"还能不能做实时决策" vs "这份切片还值不值得展示"）。

`main.health` 同时被简化为**一次** authority 读取（此前是"authority 读一次 + 自己拼
metadata"），并移除了它在 authority 之外的第二份 `live_snapshot.status` 判决。

**回归**：`MDPR-01 ~ MDPR-03`（元数据保留、health 投影、1800s vs 240s 口径分离）、
`test_MD18`（3999 旧 + 1 新 → stale）、`test_MD19`（单点 kind 不受影响）、
`test_MDG09`（health 不得再有第二份 freshness 判决）；
mutation `M-MD7`（横截面只看最新行）、`M-MD9`（丢弃 saved_at）。

### 4. 证据口径仍然过宽：`/api/hot` 不是零网络（P2）

**问题**：`main.hot()` 虽不再为补价格调用 `fetch_market_snapshot_full`，但开头的
`dfc.fetch_hot_rank(50)` 内部就 `http_post_json`。因此"`/api/hot` 已成为 network-free
read path"不成立；`MDG06` 只禁 `fetch_market_snapshot_full`，旧探针也只验证
`strategy_allocation_explain()`，这部分会假绿。

**根因**：把"迁移了 full-market snapshot 的取数"表述成了"整个 read API 都不联网"。

**修法**：选择**精确化声明**而不是为无消费者的端点新建缓存设施：

- `MDG06` 现在检查**所有会联网的 provider 入口**（含 `fetch_hot_rank`），
  并显式断言 `hot()` **仍会**取榜——这样"它不联网"的说法一旦被写回来就会变红。
- 探针覆盖扩展到 9 条只读入口、12 个 provider 入口，并在输出里显式列出
  **不属于**只读路径因而允许联网的三项（`/api/hot`、`POST /selection-evaluation/refresh`、
  盘后归因 fallback）。
- 指标口径统一改为"**full-market snapshot 的同步 provider 刷新** 4 → 0"。

### 小的维护性问题

`_market_state()` 里连续两次完全相同的 `elif live_universe is None: live_universe = []`
已删除（首轮编辑的残留）。

### 复审修正后的验证

```text
R24 targeted: 54 tests（原 45，新增 9：MD15-19 契约语义 / MDPR-01-03 元数据 parity）
mutation:     9/9 CAUGHT, survived=0, fake=0（原 5 条，新增 M-MD6..M-MD9 锁复审修正）
backend full: 4060 tests, OK, skipped=5
frontend:     118/118
E2E:          32/32, 0 flaky, 0 retry；paper-runtime 连续 5 轮
security:     kinds=none, values=0, exit 0（worktree + all）
paper_trading.py: LOC=14895  defs=280（trend only，non-blocking）
```

---

## 收尾修复：移除机械 LOC/defs Hard Gate

R24 功能正确性、full-market authority 收口、read/refresh 边界、PIT 语义、
health metadata parity、横截面新鲜度、verification_method 语义、前端投影均已 PASS。
本轮**只**做一件架构清理，**未改任何 production code**。

### 移除内容

`backend/test_paper_trading_architecture_guard.py`：

- 删除 `PAPER_TRADING_LOC_BASELINE = 14895` / `PAPER_TRADING_DEF_BASELINE = 280`
- 删除 `test_guard3_line_count_does_not_exceed_the_round2_baseline`
- 删除 `test_guard3b_top_level_function_count_does_not_exceed_the_baseline`
- 删除只为它们存在的 `PaperTradingDoesNotRegrow._size()`
- 删除 header docstring 里的 `Guard 3` 声明，并在原位置留下**为何删除**的说明

未替换成 15000 / 15500 / 16000 新阈值，也未引入 warning / soft threshold /
growth budget —— **size-based CI gate 整体取消**。

### 为什么删除

size 不是架构性质。把它设成 hard gate 会逼出这种错误优化：

```text
确实需要新增一个有业务意义的 orchestration function
        ↓
defs 281 会失败
        ↓
机械把另一个无关函数搬到新文件
        ↓
新增 wrapper / helper / import，调用链更长
        ↓
可维护性反而下降
```

### 保留的语义 guard（仍然 hard fail）

删除的是 size guard，**不是** architecture guard。本文件 13 个语义 guard class
全部保留（Guard 1/2/4/5/6/7/8/9/14，含 dependency direction、零项目级 import、
零 I/O / 零 wall-clock / 零事务所有权、episode finalizer、provenance 不得取 current
head 等），`test_market_data_boundary.py` 的 10 条 MDG-* 环境 guard 亦全部保留
（含 full-market cache 不得裸读、provider bypass、health 不得二次判 freshness）。

现在文档化的分界见 `ARCHITECTURE.md` 的「验证与架构护栏（guard policy）」：

```text
CI hard fail  重复 authority / authority 回流 / provider bypass /
              dependency violation / historical current-fill /
              implicit current-state re-resolution / frontend 业务规则重复 /
              network-in-writer-transaction / service locator / 边界被破坏

review signal 单文件 LOC / 单文件 defs / 函数行数上限 / 模块数量
```

### 拆分类纪律（写入 ARCHITECTURE.md）

新增模块必须说明 `business responsibility` / `authority` / `dependency direction` /
`为什么认知复杂度真的下降`，否则不构成架构改进。禁止为降低单文件 LOC 而机械拆分；
禁止为减少 `if` 数量而机械抽象（简单 guard 允许保留，只有重复规则 / 长 if/elif /
深层嵌套 / 状态硬编码分派才考虑 predicate / policy table / dispatch / transition table）。

### 分层验证模型（本轮起正式采用）

```text
L0 快速静态   ruff check + compileall 修改涉及的文件
L1 定向验证   受影响模块 test module / architecture guard
L2 子系统     相关 targeted tests + 相关 mutations + consumer regression
L3 最终全量   backend full + frontend + E2E + security + full mutation
```

证据是否需要重跑由**修改内容**决定：docs-only 本地不跑 production 验证；
test-only 只跑 L0 + 相关 test module；production subsystem 先 L0 → targeted →
mutations → consumer regression；只有 production 修改真正结束才做一次 L3。

merge authority 是**当前 PR HEAD 的 exact-head CI**，不是文档里写的某个旧 SHA。
因此本 PR body 不再记录 `HEAD = <sha>` 快照（那会造成"为更新 SHA 再 commit →
SHA 又变"的循环），改由 GitHub API 读取当前 HEAD。

### 本轮验证（按分层模型）

```text
production changed: NO（diff 只有 test_paper_trading_architecture_guard.py）
L0 ruff:     PASS
L0 compile:  PASS（exit 0）
L1 guard:    112/112 PASS（原 114 —— 精确减少被删的两个 size test）
```

继承的 production 证据未失效（production bytes 未变）：见上一节 54 / 9：9 / 4060 /
118 / 32：32。本轮本地**不**重跑全量，由 exact-head CI 负责最终确认。

### R25 pre-flight 债务（本轮只记录，不实现）

`verification == "verified"` 只表示"该 kind 的 verification policy 通过"，
**不等于**通过双源核验。只有 `MDC.is_cross_source_verified(snapshot)` 才能回答后者。

```text
R25 Signal Pipeline 开工前必须：
  搜索全部新增/修改 production code，禁止用
      snapshot.verification == "verified"
  推断 cross-source verified；需要双源保证必须用
      MDC.is_cross_source_verified(snapshot)
  （可考虑增加小型 AST guard，但本 R24 收尾不实现）
```

该规则已在 `ARCHITECTURE.md` 不变量 3、`market_data_contract` 的
`is_cross_source_verified` docstring 与本 PR body 三处写明。
