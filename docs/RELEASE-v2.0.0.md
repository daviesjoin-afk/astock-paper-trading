# v2.0.0 发布说明 — 确定性策略平台、Point-in-Time 研究、权威账本与风险闭环

发布日期：2026-09-20。功能基线截至 PR #174（merge commit `d754fd4e8ac966a8f0e50f079906aa029c1cb4c0`）；release-prep 仅更新版本标识、README、CHANGELOG 与本发布说明。

v2.0.0 是自 v1.3.0（2026-09-08）以来的一次**平台级升级**。期间共合入 **126 个 PR（#45–#174，未合并编号不计）**，后端全量回归从 v1.3.0 发布说明记录的约 285 项增长到 **3727 项**。这不是单纯功能堆叠：项目从“带自进化和策略选股的 A 股模拟盘”演进为一个强调 **声明式策略、Point-in-Time 正确性、可验证执行、cycle-owned ledger、deterministic risk/replay、可审计学习评估** 的本地优先研究平台。

> **仍然是 paper trading only。** 本版本不连接券商、不触碰真实资金，也不提供杠杆、做空或普通股票 T+0 回转。

## 版本重点

### 1. 动态策略平台取代“固定五策略”产品模型

v2.0.0 把策略从硬编码配置升级为完整的平台对象：

- 动态 Strategy Registry 与生命周期：draft / validated / active / paused / retiring / archived；
- 不可变策略版本与 structure checksum，历史 cycle 固定自己使用的版本；
- 声明式 Strategy DSL v1：用户策略不执行 Python，不上传代码，不启动外部进程；
- 统一 `StrategyRuntimeContext`，把 DSL、风险画像、执行画像和资金阶段绑定为同一运行契约；
- 自定义策略与内置模板统一走 `OrderIntent → allocation/sizing → execution planner → system risk gate`；
- 内置模板只是基线，不再是平台的全部；
- Strategy Workbench 提供浏览器内创建、验证、预览、版本、克隆和生命周期管理；
- Strategy API 引入 typed contracts 与 application service；
- registry-driven Settings 取代固定五策略 UI；
- 自定义策略生产路径、归档与历史 replay 均有完整回归覆盖。

平台语义在 v2.0.0 被明确锁定：

1. 自定义策略不运行任意 Python；
2. 策略不能指定最终下单数量；
3. 激活策略只决定**下一周期**资格；
4. 当前周期参与者来自 cycle snapshot，而不是当前 registry；
5. 零启用策略是合法 Idle 模式；
6. cycle 拥有经济资本，lifecycle 只控制执行许可。

### 2. OrderIntent、Execution Planner 与 N-strategy Allocation

执行和资金分配从策略实现中被抽成中央契约：

- 统一 `OrderIntent`，策略只能表达交易意图，不能越权写最终 qty；
- 中央 Execution Planner 负责计划、复核、commit；
- N-strategy allocation engine v2 支持任意数量策略共享资金池；
- cold-start、capital eligibility、cluster-first anti-sybil budgeting；
- risk-based position sizing；
- strategy execution profiles；
- batch window、verification queue、TTL sweep；
- signal expiry、order TTL、staged entry；
- cross-strategy exposure coordinator 和 correlation clusters；
- lifecycle-aware capital deployment；
- order TTL 由单一 owner 推进，避免重复/竞争状态机；
- cycle capital ownership 与 execution participation 分离。

### 3. Strategy evolution / Champion-Challenger 变成可审计闭环

自进化不再只是参数写回：

- per-strategy evolution profiles；
- asymmetric risk evolution，任何风险变化必须经过非对称门禁；
- Champion/Challenger 作为真正 shadow 路径，不直接取得正式执行权；
- first-class strategy parameter schema；
- candidate validation 与 activation 分离；
- scientific challenger promotion gate；
- reward evaluation 绑定可归因 evidence；
- AI reviewer 泛化为可配置 ai1/ai2 slots；
- 区分 hold、disagreement 和 reviewer failure；
- reviewer failure 与 strategy learning 隔离；
- disagreement taxonomy 结构化持久化；
- evolution loop 的 daily guard、重试和 cron 执行可靠性加固。

### 4. Point-in-Time 数据与 Learning/Evaluation 正确性

v2.0.0 大幅收紧历史研究中的“当时是否可知”：

- point-in-time reproducible dataset foundation；
- model-agnostic reproducible evaluation gate；
- exact timestamp cutoff semantics；
- selection/replay 强制 point-in-time boundary；
- 选股 label 改为 point-in-time correct、outcome-based；
- leakage-safe purged walk-forward evaluation；
- learning split bypass 被关闭；
- alpha 与 evidence quality 分离；
- replay clock 冻结，历史 gate 不再随机器当前时间漂移；
- candidate trace 持久化，支持 replayable selection provenance。

核心原则：

```text
历史时点不知道的事实，后来的数据库也不能替历史“补知道”。
unknown != false != zero
```

### 5. 历史可交易性从“当前状态猜测”升级为事实资产

v2.0.0 建立完整的 historical tradability 事实链：

- point-in-time historical tradability archive；
- provider ingestion 与运行审计；
- tradability replay hardening + shadow validation；
- append-only Tradability Observation Ledger；
- 区分 `effective_at`、`source_observed_at` 与 `recorded_at`；
- evidence / unknown / error 三态全部记录；
- archive fact 与 observation event 的 provenance linkage；
- legacy rows 不伪造 first-seen time；
- uncertain historical cycle attribution fail closed；
- position-aware T+1 shadow validation。

因此系统现在能够区分：

```text
事实当时不存在
事实后来才观察到
上游当时返回 unknown/error
升级前的历史 provenance 不可证明
```

而不是把这些情况统统压成“缺数据”。

### 6. Execution Reality：从“订单说 filled”升级为“证据证明成交”

执行真实性是 v2.0.0 的另一条主线：

- evidence-based execution reality layer；
- execution lifecycle / outcome contract；
- 禁止 self-attest 一个根本没有发生过的 identity check；
- `execution_status / execution_verified / evidence_source` 正式进入订单事实；
- 只有真实 fill evidence 才能提升为 verified；
- legacy filled order 会按成交流水证据回填，证据不足保持 unknown；
- paper trading 的盈亏、NAV 和执行绩效只消费 verified fills；
- deferred fills 保留 immutable cycle provenance；
- order cycle provenance 进入 schema，并且写入后不可变。

原则：

```text
stored status='filled'
!=
execution verified
```

### 7. 权威持仓与 cycle-owned ledger

v2.0.0 完成了持仓事实的 authority 收敛：

- `paper_position_lots` 是数量 / 成本 / 来源订单的执行权威；
- `paper_positions` 降级为 compatibility/display projection，零执行权威；
- 禁止 legacy position mirror 被重新 materialize 到当前 cycle；
- current-position consumers 统一通过 `paper_position_read_model`；
- current holdings、rebalance、candidate exclusion、adaptive shadow 等均不再直接把 projection 当“当前持仓”；
- rebalance state 变成 cycle-owned；
- order provenance、lot provenance、position episode provenance 均显式携带 cycle identity。

### 8. Risk：从隐式 current-state 推断升级为 cycle/as-of deterministic

PR #170–#174 完成了一条连续的风险正确性闭环：

- `paper_position_risk_state` 成为 cycle-owned runtime state：
  - peak_price；
  - take_stage；
  - episode 起点；
  - opening order provenance；
- SELL episode 结束统一按 authoritative lots 判断；
- sell decision 抽成 deterministic `paper_risk_decision`，显式消费 `asof_day`；
- 同日新仓不会吸收入场前 high，历史 replay 不再读取机器 today；
- risk scan lifecycle 从 audit marker 升级为 durable `paper_risk_scan_runs`：
  - identity = `(cycle_id, asof_date, scan_minute)`；
  - running/completed/failed/retry；
  - cycle rollover during external I/O fail closed；
- position review 的 entry-model evidence 不再使用“同账户同代码最新 signal”，而是：
  `risk_state.opened_order_id → verified BUY order → exact signal_id`；
- signal 已归档时仍按 exact id 读取 archive，不回退 latest guess；
- missing provenance 保持 unknown，沿用 neutral model score；
- replacement decision 只允许：
  - `intended_date == asof_day`；
  - `signal_date <= asof_day`；
- historical review 必须 `review_date <= asof_day`；
- slot upgrade / borrow / rollback、pending BUY occupancy、allocation budget 都绑定同一 explicit cycle + as-of；
- 明日 candidate 不再能触发今日先卖后买失败。

### 9. Paper monolith 持续收敛为 application facade

v2.0.0 没有用“大爆炸重写”替换 `paper_trading.py`，而是随着真实 correctness 问题逐步抽边界：

- slot preflight service；
- decision audit；
- account specs；
- cycle ownership resolver；
- shared cash ledger；
- user account provisioning / cycle attachment；
- capital reservation ledger；
- pending slot occupancy；
- risk exit eligibility；
- cycle capital attribution；
- authoritative position read model；
- position risk state；
- deterministic risk decision；
- risk scan state；
- position review evidence / decision；
- replacement evidence / decision。

截至 #174，`paper_trading.py` 已从此前更大的单体继续下降到约 **16049 LOC / 282 top-level defs**；后续继续按“correctness 主线 + 顺手抽离”收敛，而不是为了拆文件而拆文件。

### 10. DataFeed、StrategyPlugin 与可替换基础设施

- pluggable DataFeed adapters；
- unified feed reliability / health state；
- fail-closed market-data regression suite；
- unified StrategyPlugin contract；
- deterministic synthetic plugin demo；
- replayable candidate traces；
- live quote source whitelist；
- legacy manifest signature compatibility。

这些能力为后续更完整的 market-data service 和 deterministic replay 平台打基础。

### 11. Web / API / Security

- Strategy Workbench 成为策略定义的唯一前端编辑器；
- 前端 monolith 拆成 feature modules；
- Playwright 覆盖关键产品旅程、deep link、响应式与可访问性；
- API request contract 加固；
- 所有写接口统一经过 Operator Security Boundary；
- local-only 模式要求 loopback client + local Host，降低 DNS rebinding 风险；
- authenticated 模式使用标准 Bearer token，localhost 不豁免；
- invalid token 配置 fail closed；
- security leak scan 恢复为 CI 强制门禁；
- cron backup 不再留在 active cron directory；
- operator-auth UI 与 pre-market PnL/selection coverage 等生产问题修复。

## Breaking / behavior changes

v2.0.0 是 major release，以下变化需要升级方主动理解：

### Strategy scope

不要再假设“系统只有五个固定策略”。五个 builtin 是模板；运行范围由 registry + cycle snapshot 决定。

### Cycle ownership

当前 cycle 的经济事实不能由“当前 registry”“最新 account state”或“最大 cycle id”反推。执行路径逐步改为显式 `cycle_id`。

### Position authority

不要再把 `paper_positions` 当执行持仓来源。执行数量来自 `paper_position_lots`；projection 只用于兼容展示/元数据。

### Execution verification

历史 `status='filled'` 不再自动等于真实成交。升级时 v12 migration 会按 fill evidence 计算 verified/unknown；证据不足会保持 unknown。

### Historical data

v2.0.0 不会给无法证明的旧数据伪造 provenance。升级前的 cycle attribution、tradability observation time、risk episode state 等若无法可靠重建，会明确保持 NULL / unknown / legacy-unprovable，而不是猜。

### HTTP write security

写请求现在统一受 Operator Security Boundary 保护：

- 未配置 token：仅 local-only；
- 合法 token：所有写请求（含 localhost）都必须 Bearer；
- 非法 token：全部写请求 503；
- 对外反代写服务必须配置 token、TLS/ACL，并正确保留 Host。

## Schema 升级

当前正式 migration chain：

- `paper_trading`: **v21**
- `adaptive_learning`: **v1**

v1.3.0 的旧数据库升级时会顺序执行后续迁移，并在首次应用待迁移项前通过 SQLite backup API 创建一致性备份。

推荐升级前先自行额外备份运行目录，然后：

```bash
python backend/db_migrate.py all --dry-run
python backend/db_migrate.py all
```

关键 migration 包括：

- strategy registry / immutable versions / DSL；
- order retry lineage；
- evolution proposal lifecycle；
- momentum overlay 单位兼容；
- execution verification columns + evidence-based backfill；
- historical tradability archive / ingestion / observation / shadow / provenance link；
- immutable order cycle provenance；
- cycle-owned rebalance state；
- cycle-owned position risk state；
- cycle-owned risk scan runs。

**不会**对无法证明的历史数据做“看起来完整”的伪回填。

## 升级建议

1. 停止 scheduler / cron / 写入进程；
2. 备份 `paper_trading.sqlite3`、`adaptive_learning.sqlite3` 与自定义策略配置；
3. 拉取 v2.0.0；
4. 执行 migration dry-run；
5. 正式迁移；
6. 如果通过反向代理或远程访问写接口，配置 `ASTOCK_OPERATOR_TOKEN`；
7. 重建前端/容器；
8. 验证 `GET /api/version` 与浏览器 Settings 中 build identity；
9. 检查 health、strategy registry、当前 cycle snapshot、账户/lot/订单数量；
10. 再恢复 scheduler。

Docker：

```bash
docker compose down
docker compose up -d --build
```

本地 Python：

```bash
python -m pip install -r requirements.lock
python backend/db_migrate.py all
python -m uvicorn backend.main:app --port 8600
```

## 验证

#174 合并后的 master（`d754fd4e8ac966a8f0e50f079906aa029c1cb4c0`）：

- Python 3.11：**3727 tests OK**（1 skipped）
- Python 3.12：**3727 tests OK**（1 skipped）
- Docker smoke：**3727 tests OK**（28 skipped）
- syntax：PASS
- quality / Ruff / dependency checks：PASS
- frontend build/unit：PASS
- Playwright Chromium E2E：PASS
- security leak scan：PASS
- exact-head + post-merge master CI：全绿

## 已知边界

- paper-only，无券商/真实资金路由；
- historical provenance 无法证明时保持 unknown，不做猜测性“修复”；
- `paper_trading.py` 仍是大型 orchestration facade，后续会继续收敛 buy/execution/risk application service；
- deterministic replay 已有 golden / candidate / PIT 基础，但“任意 cycle 全链路重算并逐事实 diff”仍属于后续路线；
- Adaptive/AI 仍是 shadow/recommend/evaluate 边界，不直接取得交易 execution authority。

## 从 v1.3.0 到 v2.0.0 的 merged PR 索引

以下为正式纳入 v2.0.0 的全部 post-v1.3.0 merged PR（未合并编号不列）：

- #45 — feat(strategy): dynamic strategy registry and lifecycle
- #46 — feat(strategy): versioned strategy definitions
- #47 — feat(strategy): risk fingerprint compiler
- #48 — feat(risk): strategy risk profile templates
- #49 — feat(strategy): unified OrderIntent contract with five-strategy adapter
- #50 — refactor(execution): central execution planner
- #51 — feat(allocation): N-strategy allocation engine v2
- #52 — feat(allocation): cold-start and capital eligibility
- #53 — feat(risk): risk-based position sizing
- #54 — feat(execution): strategy execution profiles
- #55 — feat(execution): batch window, verification queue and TTL sweep
- #56 — feat(ui): execution profile center
- #57 — feat(execution): signal expiry, order TTL and staged entry
- #58 — feat(portfolio): cross-strategy exposure and intent coordinator
- #59 — feat(portfolio): strategy correlation clusters
- #60 — feat(evolution): per-strategy evolution profiles
- #61 — feat(evolution): asymmetric risk evolution
- #62 — feat(evolution): champion/challenger strategy versions
- #63 — feat(api): strategy runtime and allocation explainability
- #64 — feat(api): strategy creation risk and execution preview
- #65 — test(strategy): dynamic strategy invariant suite
- #66 — test(demo): custom strategy deterministic golden replay
- #67 — feat(strategy): executable strategy DSL v1
- #68 — feat(strategy): strategy CRUD and runtime readiness
- #69 — refactor(settings): registry-backed dynamic strategy settings
- #70 — feat(strategy-runtime): unified StrategyRuntimeContext
- #71 — fix(allocation): enforce lifecycle-aware capital deployment in production
- #72 — fix(allocation): cluster-first anti-sybil budgeting
- #73 — fix(execution): mandatory OrderIntent for user strategies
- #74 — fix(execution): single-owner order TTL lifecycle (PR-29)
- #75 — feat(risk): enforce compiled strategy risk profiles (PR-30)
- #76 — feat(evolution): add first-class strategy parameter schema
- #77 — fix(evolution): make champion challenger truly shadow
- #78 — fix(evolution): wire asymmetric risk gate to every risk-changing path
- #79 — feat(ui): custom strategy builder and lifecycle management
- #80 — feat(paper): wire user strategies into the production run path (PR-35)
- #81 — test(paper): strategy archive and historical replay (PR-36)
- #82 — refactor(paper): extract declarative strategy policies (PR-37)
- #83 — fix(paper): make cycle snapshot the canonical participant set (PR-38)
- #84 — fix(strategy): remove remaining unsafe ACCOUNT_SPECS lookup (PR-39)
- #85 — test(e2e): strengthen custom strategy production invariants (PR-40)
- #86 — refactor(strategy): finish remaining policy-driven builtin adapters (PR-41)
- #87 — feat(strategy-api): expose strategy administration lifecycle API (PR-45)
- #88 — feat(frontend): add first-class Strategy Workbench
- #89 — feat(settings): replace fixed-five strategy UI with registry-driven strategies
- #90 — fix(paper): separate cycle capital ledger ownership from execution lifecycle
- #91 — chore(release): close the Custom Strategy Web product line with a verifiable build identity
- #92 — fix(strategy-registry): preserve immutable-version invariants during draft deletion
- #93 — fix(tests): freeze the offline replay clock so factor gates stop drifting with the wall clock
- #94 — refactor(strategy-api): introduce typed contracts and strategy application service
- #95 — refactor(strategy-ui): make Strategy Workbench the single definition editor
- #96 — chore(repo): remove superseded compatibility and demo artifacts
- #97 — test(strategy): add property-driven invariant coverage for dynamic strategies
- #98 — refactor(frontend): split monolithic app source into feature modules
- #99 — fix(strategy-registry): block hard delete once a draft left the draft lifecycle (PR-56)
- #101 — test(frontend): add Playwright smoke coverage for critical product journeys
- #102 — test(frontend): complete critical Playwright journeys and fix strategy deep links
- #103 — chore(security): restore the leak-scan gate and stop gitleaks false positives
- #104 — feat(frontend): improve strategy workflow readability and interaction hierarchy
- #105 — docs: align repository documentation with the current dynamic strategy platform
- #106 — refactor(paper): extract one cohesive service from the paper trading monolith
- #107 — fix(factors): enforce momentum fraction units across strategy scoring
- #108 — fix(evolution): migrate legacy momentum overlay units safely
- #109 — security(api): enforce operator boundary for state changes
- #110 — fix(evolution): separate candidate validation from activation
- #111 — feat(evolution): add scientific challenger promotion gate
- #112 — feat(factors): separate alpha from evidence quality
- #113 — refactor(paper): extract slot preflight service
- #114 — fix(frontend): harden the API request contract
- #115 — feat(learning): add point-in-time reproducible dataset foundation
- #116 — feat(learning): add model-agnostic reproducible evaluation gate
- #117 — fix(learning): preserve exact timestamp cutoff semantics
- #118 — test(data): harden fail-closed market data regressions
- #119 — refactor(data): introduce pluggable DataFeed adapters
- #120 — feat(data): add unified feed reliability and health state
- #121 — feat(strategy): add unified strategy plugin contract
- #122 — feat(strategy): persist replayable candidate traces
- #123 — feat(strategy): add deterministic synthetic plugin demo
- #125 — refactor(paper): add decision audit parity module
- #126 — refactor(paper): cut decision audit facade over to dedicated module
- #127 — refactor(paper): extract paper account specs boundary
- #128 — refactor(paper): extract cycle ownership resolver
- #129 — refactor(paper): extract shared cash ledger boundary
- #130 — refactor(paper): extract user account provisioning boundary
- #131 — refactor(paper): extract user cycle attachment boundary
- #132 — refactor(paper): extract capital reservation ledger boundary
- #133 — refactor(paper): extract pending slot occupancy boundary
- #134 — refactor(paper): extract risk exit eligibility boundary
- #135 — test(paper): harden risk exit production path
- #136 — refactor(paper): extract cycle capital attribution boundary
- #137 — fix(market): resolve pre-market pnl display, 5-strategy selection coverage, and operator auth UX
- #138 — fix(security): adjust MIN_TOKEN_LENGTH to 8 characters for custom operator tokens
- #139 — fix: harden paper trading and evolution closed-loop reliability
- #140 — fix: keep cron backups outside active cron directory
- #141 — fix: bind evolution evaluation to attributable reward evidence
- #142 — refactor(ai-review): generalize AI tuning into configurable ai1/ai2 slots
- #143 — fix(ai-review): distinguish hold, disagreement, and reviewer failure
- #144 — fix(evolution): isolate reviewer failures from strategy learning
- #145 — feat(ai-review): persist structured disagreement taxonomy
- #146 — fix(data): enforce point-in-time boundaries in selection and replay
- #147 — fix(selection): make stock-selection labels point-in-time correct and outcome-based
- #148 — feat(validation): add leakage-safe purged walk-forward evaluation
- #149 — fix(selection): enforce point-in-time tradability and executable outcomes
- #150 — fix(execution): add evidence based execution reality layer
- #151 — fix(execution): never self-attest an identity check that did not happen
- #153 — feat(execution): enforce verified fills in paper trading
- #154 — feat(execution): complete the verified-fill enforcement wiring
- #155 — fix(learning): close alpha evaluation split bypass
- #156 — feat(data): add point-in-time historical tradability archive
- #157 — feat(data): ingest point-in-time tradability evidence
- #158 — feat(data): harden tradability replay and add shadow validation
- #159 — feat(data): add point-in-time tradability observation ledger
- #160 — fix(server): whitelist live quote sources and accept legacy manifest signatures
- #162 — fix(execution-verification): 回填与盖章不再假定 row_factory（解除 v12 迁移阻塞）
- #163 — fix(data): reconcile legacy tradability rows with observation provenance
- #164 — feat(validation): add position-aware T+1 shadow validation
- #165 — fix(validation): fail closed on uncertain historical cycle attribution
- #166 — feat(ledger): persist immutable order cycle provenance (v18)
- #167 — fix(execution): preserve immutable cycle provenance across deferred fills
- #168 — fix(ledger): stop rematerializing legacy position mirrors into active cycles
- #169 — fix(ledger): route current-position consumers through authoritative lots
- #170 — fix(risk): make position risk state cycle-owned
- #171 — fix(risk): make sell decisions as-of deterministic
- #172 — fix(risk): make risk scan lifecycle cycle-owned
- #173 — fix(risk): bind position reviews to episode provenance
- #174 — fix(risk): bind replacement decisions to cycle and as-of
