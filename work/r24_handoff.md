# R24 交接文档 —— Market Data Boundary 建立 + 复审修正 + 机械 LOC 门槛移除

- 日期：2026-09-22
- 状态：**已完成、已推送、exact-head CI 9/9 全绿（browser 日志人工核对无 flaky / retry / timeout）**；**未合并、未部署**
- base：`2afcbecad0ee7966130d41b30711bbc5ebc38660`（= PR #181 的 merge，**无漂移**）
- merge-base：`2afcbecad0ee7966130d41b30711bbc5ebc38660`
- **最终 head：`5d90b2b428b660bbc20e40170335ccda54937fa5`**
- PR：[#182](https://github.com/daviesjoin-afk/astock-paper-trading/pull/182)（OPEN，MERGEABLE）
- 分支：`codex/r24-market-data-boundary`
- 前置：R23（PR #181）已由人工合并进 master，本轮**从合并点开分支**，未在 R23 分支继续开发

本轮共 6 个提交：

```text
5d90b2b test(architecture): make paper trading size metrics non-blocking
c6215de docs(r24): record the review-fix head and exact-head CI result in the PR body source
cf82261 fix(market-data): close the review findings before merge
cf9cbe8 docs(r24): make the provider call-site counts precise in the PR body source
1714aa4 docs(r24): record the final SHAs and exact-head CI result in the PR body source
45e6763 refactor(market-data): establish verified market-data boundary
```

---

## 一、本轮解决了什么

R23 交接文档里列为「维护债 #1」的那条，正是本轮的主目标：

> `allocation-explain` 只读视图同步刷新网络快照（~4.4s 有网 / ~13.8s 无网）→ 归 R24 market-data boundary。

R24 建立了 Market Data 的**唯一业务权威**，并把上层对 provider 的直接 ownership 取消。

### 1. 实测收益（同一条件：代理指向不可达端口，近似 CI `--network none`）

```text
                            before        after
strategy_allocation_explain 13.82s        0.104s
provider 调用数              ≥1            0
返回的 market-data status    （无此概念）   unavailable / stale（如实）
```

验收不是固定毫秒数，而是：**NO NETWORK 模式下 read path 延迟不再由 provider timeout 主导**。

### 2. 建立/取消的架构事实

```text
建立：market_data_contract.py（纯契约，零 I/O / 零时钟 / 零项目依赖）
      market_data_service.py（唯一取数入口，两个显式 access mode）

      read_snapshot()     ACCESS_READ    只读缓存/持久化事实，绝不联网
      refresh_snapshot()  ACCESS_REFRESH 显式允许联网，必须由调用方主动选择

取消：上层对 full-market snapshot 的直接 ownership
```

**三条通道**（首轮只报了第一条，复审指出收口不完整，第二轮补齐）：

```text
channel A  fetch_market_snapshot_full(...)          17 → 1（仅在 authority 内部）
channel B  fetch_market_snapshot(pages=None...)      4 → 0
channel C  按路径裸读 market_snapshot*.json          5 个读取器 / 4 模块 → 0

合计 26 → 1
```

channel C 的 5 个裸读读取器（`trade_attribution._market_snapshot`、
`adaptive_engine._snapshot_rows` / `_data_input_state`、`deepseek_advisor._read_snapshot`、
`ai_analysis._read_snapshot`）**全部**会回退到 20 页风险样本 `market_snapshot.json` ——
那是约 1/25 个市场，把它当全市场快照算板块/个股涨跌会**系统性歪曲统计**，
且都绕过 `_full_snapshot_payload_is_complete` 的完整性校验。这是实质正确性问题，
不只是"绕过 authority"。

同时 `universe.star_leader_mapping()` 全仓零调用方（含前端/E2E/测试），
按「零消费者的 compat 路径应删除」直接删除，而不是把死代码迁移。

### 3. 状态语义（禁止压成一个分数）

保留**五个正交维度**：

| 维度 | 取值 |
| --- | --- |
| `availability` | `available` / `unavailable` |
| `freshness` | `fresh` / `stale` / `unknown` |
| `verification` | `verified` / `single_source` / `disagreement` / `unavailable` / `not_attempted` |
| `verification_method` | `cross_source` / `coverage_integrity` / `none` |
| `as_of` | 这条事实对应的业务日 |

关键语义分离（此前靠 `None` / `{}` / 空 list / magic string 让调用方猜）：

- **STALE ≠ UNAVAILABLE**：有最后一份可信 snapshot 但过期 → 返回 `stale` **加**完整 rows；
  完全没有 → `unavailable`，不携带 payload。
- **DISAGREEMENT ≠ UNAVAILABLE ≠ VERIFIED**：多源冲突报 `unverified`，绝不静默挑一个源；
  核验源本身不可用报 `degraded`。
- **`verified` ≠ 多源核验过**（复审修正，见第二节）。
- **横截面新鲜度是"多少行够新"**（复审修正，见第二节）。
- **未来漂移的源时间戳不可采信**：`abs()` 语义与既有 `_fresh_full_snapshot_from_disk` 一致。

---

## 二、复审修正（第二轮）—— 4 项合并前必须收掉的问题

首轮提交后经人工复核判定 4 项需在合并前收掉。全部修复并加永久回归。

### 1. full-market authority 收口不完整（P1/P2）

见第一节「三条通道」。**根因**：迁移只覆盖 `fetch_market_snapshot_full`，
漏掉 `fetch_market_snapshot` 与按路径裸读；且首轮的 `MDG08` 守卫只匹配属性名
`MARKET_SNAPSHOT_FULL_CACHE_PATH`，而上述模块用的是**字面文件名**，所以守卫**假绿**。

**修法**：全部改经 authority；新增 `data_fetcher.load_market_snapshot_full_payload()`
（保留元数据的校验读取器）替代裸 `open()`；新增 `MDG10` 按**字面路径**检查，
已验证能精确抓出（在 `linkage.py` 插入 `open('market_snapshot_full.json')`
→ RED，报告 `linkage.py:open@line56`）。

### 2. `verification="verified"` 的语义是假的（P1/P2）

**问题**：契约把 `VERIFICATION_VERIFIED` 定义为"多源核验通过"，但
`_load_cached_snapshot()` / `refresh_snapshot()` 对**单一 Eastmoney** 全市场快照，
只要 completeness/coverage 通过就直接标 `verified` —— 单源在契约层看起来像双源验证过，
而 R25/R27 会直接消费这个 contract。

**根因**：把"哪个 kind 的 policy 通过了"和"通过的是哪一套 policy"压成了一个枚举值。

**修法**：新增正交维度 `verification_method`：

```text
cross_source        逐票第二独立源核验（这才是"多源核验"的字面含义）
coverage_integrity  完整性与覆盖（complete marker + 4000 行 + 90% 期望覆盖）
none                未做任何核验
```

`verified` 含义固定为"**该 kind 的 verification policy 已通过**"；full-market 标
`verified` + `coverage_integrity`。**构造期校验强制状态与 method 相容**
（`verified` 不许配 `none`；`not_attempted` 不许配任何真实 method），
所以这类错误**不可能被写出来**。新增 `is_cross_source_verified()` 作为显式判据。

### 3. `/api/health` metadata 与 freshness 行为未声明变化（P2）

三个独立缺陷：

- **(a)** `_load_cached_snapshot()` 只取 `rows`，把 `saved_at=None` / `expected_rows=0`，
  于是 `live_snapshot.saved_at` 与基于它算的 `age_seconds` **实际丢失**。
- **(b)** 旧 health 判 `fresh` 要求盘中至少 4000 条当日且 30 分钟内的 fresh rows；
  authority 只看**最新一条 quote_at** 是否在 240s 内 → "3999 条旧 + 1 条新"
  理论上可能被投影成 fresh。
- **(c)** health 的 1800s 展示口径被 240s 实时口径顶替。

**修法**：

- **(a)** authority 保留完整 payload：新增 `read_snapshot_with_meta()` 返回
  `(reading, payload)`；`market_health_projection()` 增加 `snapshot_meta` 段。
- **(b)** 契约新增 `min_fresh_ratio`（横截面默认 90%，
  `CROSS_SECTION_MIN_FRESH_RATIO`），并把 `fresh_ratio` 写进 `verification_detail`
  以便审计。单点 kind（名单构建）不设比例要求，退化为"最新一条在窗口内"。
- **(c)** 新增 `MARKET_HEALTH_POLICY`（1800s）由 authority 执行，
  **不**并入 `LIVE_MARKET_POLICY`（240s）—— 两者业务问题不同
  （"还能不能做实时决策" vs "这份切片还值不值得展示"）。

`main.health` 同时被简化为**一次** authority 读取，并移除它在 authority 之外的
第二份 `live_snapshot.status` 判决。

### 4. 证据口径过宽：`/api/hot` 不是零网络（P2）

**问题**：`main.hot()` 虽不再为补价格调 `fetch_market_snapshot_full`，但开头的
`dfc.fetch_hot_rank(50)` 内部就 `http_post_json`。因此"`/api/hot` 已成为 network-free
read path"不成立；`MDG06` 只禁 `fetch_market_snapshot_full`，旧探针也只验证
`strategy_allocation_explain()`，这部分会假绿。

**修法**：选择**精确化声明**而不是为无消费者的端点新建缓存设施：

- `MDG06` 现在检查**所有会联网的 provider 入口**（含 `fetch_hot_rank`），
  并显式断言 `hot()` **仍会取榜** —— 这样"它不联网"的说法一旦被写回来就会变红。
- 探针覆盖扩展到 9 条只读入口 / 12 个 provider 入口，并在输出里显式列出
  **不属于**只读路径因而允许联网的三项。
- 指标口径统一改为"**full-market snapshot 的同步 provider 刷新** 4 → 0"。

### 5. 小的维护性问题

`_market_state()` 里连续两次完全相同的 `elif live_universe is None: live_universe = []`
（首轮编辑的残留）已删除。

---

## 三、收尾修复（第三轮）—— 移除机械 LOC/defs Hard Gate

第三轮**只做一件架构清理，未改任何 production code**。

### 移除内容

`backend/test_paper_trading_architecture_guard.py`：

```text
删除  PAPER_TRADING_LOC_BASELINE = 14895
删除  PAPER_TRADING_DEF_BASELINE = 280
删除  test_guard3_line_count_does_not_exceed_the_round2_baseline
删除  test_guard3b_top_level_function_count_does_not_exceed_the_baseline
删除  PaperTradingDoesNotRegrow._size()（只为上述两个测试存在）
删除  header docstring 的 Guard 3 声明
```

AST 验证过（不是只改字符串）：`test_guard3*` 方法 = NONE、`_size` 不存在、
`PaperTradingDoesNotRegrow` class 不存在。

**未替换成** 15000 / 15500 / 16000 新阈值，**未引入** warning / soft threshold /
growth budget —— size-based CI gate **整体取消**。

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

### 保留的语义 guard

删除的是 size guard，**不是** architecture guard：

```text
本文件 13 个语义 guard class 全部保留
  DomainModulesNeverDependOnPaperTrading / RiskStateCrudStaysInItsOwner /
  RiskStateModuleIsAPureDomainBoundary / RiskDecisionModuleIsDeterministic /
  EveryProductionSellPathFinalizesTheEpisode / RiskScanStateIsACycleOwnedBoundary /
  PositionReviewIsProvenanceBound / ReplacementIsAsOfAndCycleBound /
  EntryCapitalPlanningIsBounded / SellFillCommitConvergenceIsBounded /
  RiskApplicationServiceBoundary / PortfolioReadModelIsCycleAsOfBounded /
  SelectionProvenanceIsVersionPinned

test_market_data_boundary.py 的 10 条 MDG-* guard 全部保留
```

### 文档化的分界（写入 `ARCHITECTURE.md` 新节「验证与架构护栏（guard policy）」）

```text
CI hard fail   重复 authority / authority 回流 / 新增直接 DB write owner /
               provider bypass / dependency violation / historical current-fill /
               implicit current-state re-resolution / frontend 业务规则重复 /
               network-in-writer-transaction / service locator / 边界被破坏

review signal  单文件 LOC / 单文件模块级 def 数 / 单函数行数上限 / 模块数量
```

同节还写入**拆分与抽象纪律**（新增模块须说明 responsibility / authority /
dependency direction / 认知复杂度为何下降；禁止为降 LOC 而机械拆分；
禁止为减少 `if` 而机械抽象）与 **L0→L3 分层验证模型**。

---

## 四、验证结果

### L0 / L1（收尾轮，test-only 修改）

```text
L0 ruff                      PASS
L0 compileall                PASS（exit 0）
L1 architecture guard        112/112 PASS（原 114 —— 精确减少被删的两个 size test）
L1 受影响模块（含 doc-coupled）219 tests OK, skipped=1
production evidence invalidated  NO
```

### 继承的 production 证据（production bytes 未变，未重跑）

| 门禁 | 结果 |
|---|---|
| R24 targeted | **54 tests**（MD 契约 19 / MDG guard 10 / MDP refresh 11 / MDR 只读 7 / MDPIT 4 / MDPR 元数据 3） |
| 语义 mutation | **9/9 CAUGHT；survived=0；fake=0；other=0；restore sha256 PASS** |
| 全量后端（CI 3.12） | **Ran 4058 tests, OK (skipped=1)**（= 4060 − 2 个被删 size test） |
| 全量后端（本地） | Ran 4060 tests, OK (skipped=5)（收尾前） |
| 前端 unit | **118/118**（含 7 条 R24 新增行为断言） |
| paper-runtime 连续 5 轮 | **5/5 clean**（每轮 2 passed，0 retry，0 flaky） |
| 浏览器全量（CI 等效） | **32 passed，0 flaky，0 retry，0 timeout** |
| 安全扫描 worktree / all | `kinds: none; values: 0`；EXIT=0 |
| exact-head CI | **9/9 pass**（tests 3.11 / tests 3.12 / syntax / quality / docker-smoke / frontend / browser-e2e / security-leak-scan ×2） |

**前端测试非空性**：把 `var status=String(md.status||'unavailable')` 改成
`var status='fresh'` 后 **3/7 条变 RED**，还原后 7/7 绿。

**`MDG10` 非空性**：在 `linkage.py` 插入 `open('market_snapshot_full.json')`
→ RED，精确报告 `linkage.py:open@line56`。

### 9 条 mutation

```text
M-MD1  read_snapshot 偷偷允许联网
M-MD2  stale 被标成 fresh（超窗/覆盖不足仍判新鲜）
M-MD3  provider 冲突被静默当成可用（不报 unverified）
M-MD4  historical 请求 fallback 到 current snapshot（PIT 被绕过）
M-MD5  unavailable 被默认值填充（没有事实却报可用）
M-MD6  verified 可以不带核验方法（单源冒充双源）        ← 复审修正
M-MD7  横截面只按最新一行判新鲜（覆盖不足仍报 fresh）    ← 复审修正
M-MD8  单源全市场快照自称 cross_source 核验过           ← 复审修正
M-MD9  只读路径丢弃 saved_at（health 元数据回退）        ← 复审修正
```

---

## 五、维护性核对

- Market-data authority：**1**（`market_data_service` + `market_data_contract` 状态语义）
- 新增 facade / wrapper 层：**0**（刻意不做 compatibility facade，无 v1/v2/legacy）
- 显式 current-state lookup 新增：**0**
- 前端重复业务规则：**0**（只渲染，不重算 freshness，不理解 provider 机制）
- 新增长 `if/elif` 决策链：**NO**（`classify()` 是单一纯函数 + 固定 `_VERIFICATION_RANK`
  数据表；简单 guard 保持直接 `if`）
- 为 LOC 拆模块：**无**
- 删除的零消费者路径：`universe.star_leader_mapping()`

```text
理解"当前行情是否可信"需要查看：
before = 4 production modules
         （data_fetcher cache TTL / main.health 二次判决 / paper_trading 各调用点
           max_age 字面量 / dashboard_queries 读取路径）
after  = 2 production modules（market_data_contract 判状态语义 +
          market_data_service 判能不能联网），且二者构成**同一个 authority**，
          不再有第二个业务判决（MDG08 / MDG09 锁住）

paper_trading.py: LOC=14895  defs=280  ← trend only / non-blocking
```

---

## 六、为 R25 / R27 留的接口与债务

### 接口

`MarketDataReading.projection()` 是给 R25 Signal Pipeline 与 R27 AI Research 的稳定输入
（market facts + as_of + source evidence + freshness + verification）。
R27 的 AI 只能在**可信事实**之上做解释/研究/假设，不会自己变成行情事实来源 ——
本轮不接 LLM。contract 刻意保持 `small / explicit / stable`，未写死成
`only-for-paper-trading`。

### ⚠️ R25 pre-flight 必须做的检查

`verification == "verified"` 只表示"该 kind 的 verification policy 通过"，
**不等于**通过双源核验。只有 `MDC.is_cross_source_verified(snapshot)` 才能回答后者。

```text
R25 Signal Pipeline 开工前必须：
  搜索全部新增/修改 production code，禁止用
      snapshot.verification == "verified"
  推断 cross-source verified；需要双源保证必须用
      MDC.is_cross_source_verified(snapshot)
  （可考虑增加小型 AST guard，但本 R24 未实现）
```

这条规则已在**三处**写明：`ARCHITECTURE.md` 不变量 3、
`market_data_contract.is_cross_source_verified()` 的 docstring、本 PR body。

**这是对 R25/R27 的破坏性契约变更** —— 如果未来代码写
`snapshot.verification == "verified"` 就断言"双源已核验"，会得到错误结论。

### 其他维护债（不在本 PR 范围）

1. `api_adaptive._fetch_rebalance_quotes` 用 `fetch_market_snapshot(pages=20)`
   —— 那是 `market_snapshot_sample_20.json`，**不同** artifact（风险样本用途），
   不在 R24 收敛范围。
2. `GET /api/hot` 的 `fetch_hot_rank` 仍同步取东财人气榜（独立 artifact）。
3. `POST /api/selection-evaluation/refresh` 与盘后归因的 fallback refresh 允许联网
   （非 GET 只读路径）。
4. 前端历史 warning：esbuild 报 2 处 `Duplicate key ... in object literal`
   （`cross_source_failed` / `cross_source_unavailable`，均在 `frontend/src/core/format.js`）
   —— 非本 PR 引入，本 PR 未触碰该文件。
5. `work/` 下历史脚本的 ruff 告警（历史遗留，本轮未触碰）。
6. `work/r15..r19_pr_body.md` 仍有历史 ratchet 叙述
   （"Baseline ratcheted down to..."）—— 属允许保留的历史说明；
   若后续希望仓库里不再出现这类表述，需单独一轮 docs 清理。

---

## 七、本轮明确没做的事

```text
未改策略 / 因子 / 选股逻辑        未改收益优化 / 风险阈值
未接 AI（归 R27）                未做 Signal Pipeline 全量重构（归 R25）
未做 Execution Fidelity 重构（归 R26）
未扩范围把全仓 44 个 provider 调用全部迁完
未加任何新的 LOC/defs 阈值替代旧门槛
未新增永久趋势统计工具（用一次性 wc -l / AST 统计）
```

---

## 八、接手须知

### 证据继承规则（本轮起正式采用）

证据是否需要重跑**由修改内容决定**，不再每次跑全量：

```text
docs-only    (*.md / 注释 / PR 描述)  → 本地不跑 production 验证，等 exact-head CI
test-only    (test_*.py / guard)      → L0 ruff + compile + 相关 test module
production   subsystem                → L0 → 相关 targeted → 相关 mutations
                                        → consumer regression（不要立刻 full backend）
最终 production head                  → 一次性 L3 全量，然后 push
```

其中 L2 = 相关 targeted tests + 相关 mutations + consumer regression；
L3 = backend full + frontend + E2E + security + full mutation。

### PR body 与 SHA 的规则

merge authority 是**当前 PR HEAD 的 exact-head CI**，不是文档里写的某个旧 SHA。
因此 PR body **不再记录** `HEAD = <sha>` 快照（那会造成"为更新 SHA 再 commit →
SHA 又变"的循环），改为写 "GitHub Actions checks on current PR HEAD"，
人工审核时从 GitHub API 读取。

### 若要把 reviewer 关切落到代码

`work/r24_*` 是自查工具，不是 CI 门禁 —— 它们可以就地变异 production 文件、
跑完自动还原并校验 sha256。**必须串行运行，期间不要编辑 production 文件**。

---

## 九、证据脚本

| 文件 | 用途 |
|---|---|
| `work/r24_authority_audit.md` | 逐调用链审计（provider 调用点三通道分类、freshness owner、§18 network/transaction 审计、逐项判定真缺口） |
| `work/r24_readpath_no_network_check.py` | 只读路径零网络证明（12 个 provider 入口全部 monkeypatch 断言失败；9 条只读入口） |
| `work/r24_mutation_check.py` | 9 条语义 mutation，带 `--non-vacuity` |
| `work/r24_pr_body.md` | PR 正文源（与 GitHub 上内容一致） |
| `work/r23_round4_nonet_probe.py` | 本轮 before/after 延迟对照所用探针（沿用 R23） |

---

## 十、一句话状态

> R23 列为「维护债 #1」的 `allocation-explain` 同步网络刷新已修
> （**13.82s → 0.104s，provider 调用 0**），full-market snapshot 的三条访问通道
> 从 **26 → 1**（仅在 authority 内部），`verified` 不再被误读为多源核验过，
> health 的 metadata/freshness parity 已恢复，机械 LOC/defs hard gate 已整体移除
> 而语义 guard 全部保留。
> **MERGE: NOT MERGED；DEPLOY: NOT DEPLOYED；STATUS: AWAITING HUMAN REVIEW。**
