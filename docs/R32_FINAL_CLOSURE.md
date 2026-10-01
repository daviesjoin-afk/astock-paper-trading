# R32 Final Closure — Final Architecture Inventory

本文件是 R32 Final PR 的**前置 inventory**（对应工单 §3「先不要写代码」）。它记录 merged master 上的真实
owner/caller 分布，以及两个**阻断「一次性 R32 Final」的硬事实**。结论见 §F：**最终 PR 在解决 §F 之前不
启动**（工单 §5.4 允许的唯一中断条件）。

## 0. 基线与分支

```text
base  = 6f66a4317a343d0211759c9115101786f5dedad0   (PR #220 merge, 人工合并)
        其 tree 与 36e01c7be14ccc2d06a78a4b5ca47af448043090 完全一致（零漂移）
branch = codex/r32-final-closure
R32-E1 = COMPLETE（已合并）
```

## A. Promotion

### A.1 `PromotionEvidenceBundle`（`backend/strategy_promotion.py:56-88`）

| 字段 | 类型 | 声明处 | 是否被决策读取 | 写入者 |
| --- | --- | --- | --- | --- |
| `r29_run_key` | `str \| None` | `:58` | 是（`:336`, `:349`, `:354`） | API 调用方 |
| `r30_report_key` | `str \| None` | `:59` | 是（`:360`, `:363`） | API 调用方 |
| `future_shadow_evidence_ref` | `str \| None` | `:60` | **否**（只被 shape 校验 `:68-71` 与 projection `:87`） | **无生产写入者** |
| `future_paper_evidence_ref` | `str \| None` | `:61` | **否**（同上，`:88`） | **无生产写入者** |

`PROMOTION_RULES`（`:146-154`）：`("shadow","paper") -> ("future_shadow_evidence_ref",)`。

### A.2 为什么 `shadow → paper` 永远返回 `shadow_evidence_owner_unavailable`

`strategy_promotion.py:371-374` 无条件落 block，完全不看 bundle：

```python
elif (from_state, target_state) in {("shadow", "paper"), ("paper", "production_sim")}:
    reason = ("shadow_evidence_owner_unavailable" if target_state == "paper"
              else "paper_runtime_evidence_owner_unavailable")
    reasons.append(reason)
```

因此 shadow→paper 目前**永久 ineligible**（`eligible = not reasons`，`:386`）。

### A.3 谁 evaluate / create / apply

| 函数 | 定义 | 生产 caller | 测试 caller |
| --- | --- | --- | --- |
| `evaluate` | `:286` | `strategy_service.py:404`（lifecycle read model） | `test_strategy_lifecycle_promotion.py`、`test_strategy_registry_lifecycle.py:38` |
| `create_proposal` | `:402` | `strategy_service.py:450` ← `api_strategies.py:240` ← route `:235` | `test_strategy_lifecycle_promotion.py` |
| `apply_proposal` | `:498` | `strategy_service.py:506` ← `api_strategies.py:220` ← route `:217` | `test_strategy_lifecycle_promotion.py` |
| `get_proposal` | `:450` | `strategy_service.py:495` | — |
| `list_proposals` | `:484` | `strategy_service.py:413,466` ← `api_strategies.py:255` | — |

### A.4 唯一性判定

- **Promotion Policy = `strategy_promotion`**（唯一 lifecycle-edge 资格判定）。
- **Lifecycle Transition Authority = `strategy_lifecycle.transition`（`strategy_lifecycle.py:323`）**：
  lifecycle 表的原生写只出现在该文件（`:261`, `:270`, `:412`, `:422`）；生产 caller 只有
  `strategy_service.py:482`（safety/resume）与 `strategy_promotion.py:513`（promotion）。
- **但 promotion 决策并非唯一**（见 §F.2）：`strategy_champion` + `promotion_science` 另有一套
  `promotable` 决策。

## B. Comparison

| 项 | 事实 |
| --- | --- |
| exact persistent identity | `report_id == report_fingerprint`（`shadow_comparison.py:1841-1844`），**单一身份，不需要再造 `comparison_key`** |
| exact getter | `shadow_comparison_repository.get_report(conn, report_id)`（`:68-109`），强制 64 位 id（`:71`），自校验指纹（`:84-85`） |
| latest/current/head 查找 | **不存在**。repository docstring 明示「no latest/list operation」（`:3-4`）；`shadow_run_repository.get_run` 同为 exact（`:88-90`） |
| availability 语义 | `AVAILABLE` 仅当四个 required dimension 全 AVAILABLE **且** coverage 完整（`:1799-1807`），否则 `PARTIAL`；blocked ⇒ `UNAVAILABLE` |
| provenance | report 自带 `provenance` map（`:1649-1679`），逐路径标注 OWNER_ISSUED / CAPTURED_INPUT / DECLARED / DERIVED / UNAVAILABLE |
| coverage | `CoverageEvidence`（`:504-552`）：denominator 恒为 expected，`coverage_ratio == available/expected` |

## C. Entry Authority

### C.1 canonical API 与生产 caller

| 符号 | 定义 | 生产 caller |
| --- | --- | --- |
| `EntryGateState`（14 字段） | `execution_planner.py:1345-1374` | `plan_entry:1647`、`shadow_runtime.py:610`（`replace`） |
| `evaluate_entry_state` | `:1492-1595` | **1**：`shadow_runtime.py:656`（+ `plan_entry:1663`） |
| `plan_entry` | `:1598-1667` | **0** |
| `security_gate` | `:1254` | `manual_orders.py:207`（内部复用 `PT._security_scope`） |
| `market_gate` | `:1266` | `manual_orders.py:256` |
| `account_risk_gate` | `:1285` | `manual_orders.py:258` |
| `quote_gate` | `:1235` | `manual_orders.py:342`（内部复用 `PT._execution_quote_status`） |
| `cash_gate` | `:1293` | `manual_orders.py:373` |
| `capacity_gate` / `seat_reserve_gate` | `:1163` / `:1091` | `manual_orders.py:213`、`paper_trading.py:9531`、`manual_orders.py:1178` |

`evaluate_entry_state` 只决定 7 个 gate（security_scope / market / account / position_count / seat_reserve /
execution_quote / cash），**不决定** sizing、timing、Q-tier、news、deployment、dispatch、lease、TTL。

### C.2 Active BUY（`paper_trading.py:_buy_order`，`:9193`）

29 个 gate 站点：early-return 6 处（`:9197` lease、`:9200` account/cycle、`:9210` participant、`:9216`
dedupe、`:9230` entry freeze、`:9276` OrderIntent、`:9283` signal TTL）+ append-to-`reasons` 23 处
（security_scope `:9330`、market_policy `:9309`、price `:9339`、execution_quote `:9310`、pct/lim `:9343`、
dynamic_news `:9346`、gap/spec `:9360`、entry_model `:9373`、Q-tier `:9432`、chase_entry `:9437`、
timing_gate `:9442`、new-entry-price `:9460`、open_codes `:9479`、timing `:9502`、entry_deployment `:9555`、
seat/capacity `:9562-9615`、shared risk state `:9621`、allocation `:9650`、symbol_headroom `:9847`、
pending/shared cash `:9941-9969`、entry economics `:9970`、dispatch `:10040`、dispatch duplicate `:10067`），
外加 `limit_deferred` 在 `:10047` 强制 `allowed=False`。

`_buy_order` **不调用** `plan_entry` / `evaluate_entry_state`。

### C.3 Manual 路径（`manual_orders.py`）

- 单一样板：`_manual_order_plan:51`（caller：`preview_manual_order:409`、`submit_manual_order:706`、
  `process_pending_manual_orders:1011`→`EP.revalidate_order_plan:1137`）。
- 已 delegate 到 canonical：`security_gate:207`、`capacity_gate:213`、`market_gate:256`、
  `account_risk_gate:258`、`quote_gate:342`、`cash_gate:373`。
- 仍内联重复：entry freeze `:97-99`、订单/账户校验 `:100-111`、quote snapshot 日期 `:124-135`、
  BUY model tier `:264-271`、ST/risk_flag `:261-262`、manual_entry_review `:274-282`、
  qty-vs-safe_qty `:305-308`、lot `:333-334`、SELL T+1/limit-down `:322-362`。
- `_manual_risk_state:18-48` 复制 `_shared_risk_state`，但**无 caller（死码）**。

### C.4 Shadow

`ShadowCandidate.entry_state: EntryGateState`（`shadow_runtime.py:460`）；`evaluate_shadow` 内
`:610-627` 用 `replace` 组合 frozen state，`:628` 取指纹，`:656` 调 `EP.evaluate_entry_state(...)`。
该调用点不读 DB / current state（模块无 DB 依赖，输入全部显式）。

## D. Workspace

- 已有路由（`api_strategies.py`，prefix `/api/strategies`）：list/create/update/patch/validate/preview/
  `GET /{id}`/`POST /{id}/transition`/`GET /{id}/lifecycle`/`POST|GET /{id}/promotion/proposals`/clone/
  versions/events/delete → **可扩展，无需新 endpoint 家族**。
- `strategy_service`：read model 已有 `detail_payload:218`、`lifecycle_read_model:367`；
  `strategy_api_models.py` 只装输入契约。
- 前端 `frontend/src/features/strategies.js`：`wbOpenDetail:665`（detail + versions + lifecycle），
  已有 promotion 动作按钮 `wbCreatePromotionProposal:566-605`、`wbApplyPromotionProposal`、
  `promotion-apply:716-717`；E2E 已有 `strategy-lifecycle.spec.js`。
- **当前没有任何 UI/HTTP 暴露 `shadow_comparison` 证据**（`shadow_comparison_service` 只被测试 import）。

## E. Legacy / cleanup audit

| symbol | responsibility | 生产 caller | 测试 caller | replacement | delete |
| --- | --- | --- | --- | --- | --- |
| `PromotionEvidenceBundle.future_shadow_evidence_ref` | 占位引用，无决策、无写入者 | 0 | 若干 | `shadow_comparison_report_id`（本 PR 新增） | **YES** |
| `PromotionEvidenceBundle.future_paper_evidence_ref` | 同上（paper→production_sim） | 0 | 若干 | 无 owner（§9：保持 blocked，不 fabricate） | NO |
| `strategy_champion.collect_ledger_metrics:170` | legacy ledger 指标，自述已死 `:178,:238` | 0 | `test_strategy_invariants:110` | 无 | **YES** |
| `strategy_champion.compare_for_promotion:268` | 参数头晋升的指标/容差判定 | 仅内部 `:451` | 0 | 见 §F.2（**未决**） | 待定 |
| `strategy_champion.evaluate_challenger:410` + `promotion_science` | 参数头晋升决策（含科学门禁） | `paper_trading.py:8929` | `test_strategy_champion` | 见 §F.2 | 待定 |
| `strategy_champion.promote_challenger:479` / `rollback:530` / `open_challenger:290` | 正式参数头切换（经 `self_evolution.activate_params_candidate`） | `paper_trading.py:8958,8966-8976,8979-8987`；`api_paper.py:284-332` | 多个 | 无（`strategy_promotion` 管的是 lifecycle state，不是参数头） | **NO** |
| `strategy_champion.run_shadow_counterfactual/run_shadow_context` | 同快照影子账本机制 | 0（生产） | `test_strategy_champion` 等 | 无 | NO |
| `manual_orders._manual_risk_state:18-48` | 复制 `_shared_risk_state`，无 caller | 0 | 0 | `PT._shared_risk_state` | **YES** |
| `plan_entry` | orchestration boundary，无生产 caller | 0 | `test_execution_planner:1031,1057,1086` | 见 §F.1 | 待定 |

**不要误删**（名字带 shadow 但业务不同）：`tradability_shadow`、`tradability_position_shadow`、
`factor_quality_shadow`、`execution_quality_shadow`、`portfolio_shadow`（均非 Strategy Challenger 业务）。

## F. 阻断「一次性 R32 Final」的两个硬事实

### F.1 Entry Authority：Active 的 market gate 不是纯 gate，它同时是 **sizing owner**（工单 §5.1 直接冲突）

同输入对比（`EP.market_gate` vs `PT._strategy_market_policy`，实测输出）：

```text
tq_breakout  light=yellow
  canonical : blocked=False  reason=None
  active    : allowed=True   risk_scale=0.5   reason='市场黄灯，强势日内候选实时确认按50%仓位执行'
tq_breakout  light=unknown
  canonical : blocked=True   reason='市场红灯，首板接力暂停新开仓'
  active    : allowed=False  reason='市场数据未知'
sector_rotation light=red (板块热点)
  canonical : blocked=True   reason='市场红灯，板块轮动策略暂停新开仓'
  active    : allowed=False  reason='市场红灯，板块轮动策略暂停新开仓'  shadow_exception=True
```

- `risk_scale` 是 **sizing 输入**：`paper_trading.py:9772` `sizing["market_risk_scale"] = market_policy["risk_scale"]`
  （另见 `:8704`）。黄灯下按策略分别 0.5 / 0.75 / 0.65。
- canonical `market_gate`（`execution_planner.py:1266-1282`）只有 `blocked = light in ("red","unknown")`，
  没有 `risk_scale`，没有 `shadow_exception`；unknown 时复用红灯文案。
- 工单 §5.1 明定：Entry Authority **只**决定「能不能下 / gate evidence」，**不负责** sizing。
  因此把 market gate 收敛进 canonical owner，必然二选一：把 per-strategy 仓位缩放搬进 Entry Authority
  （与 §5.1 冲突），或在 canonical 侧丢掉它（改变 Active 真实 sizing，违反 §5.3/§21）。
- 另有 **20+ Active gate 在 canonical 契约里没有任何字段**，只能先扩约再迁移；这不是 caller rewire，
  而是对**实盘买入决策主路径**（`_buy_order` 29 个 gate 站点）的重写。

⇒ 该项无法在「保持两边既有业务语义」的前提下等价完成（§5.4 的中断条件）。

### F.2 两套并存的 promotion 决策（工单 §8/§25 要求唯一）

- lifecycle promotion：`strategy_promotion.evaluate`（证据完整性）。
- **参数头 promotion**：`strategy_champion.evaluate_challenger:410` + `compare_for_promotion:268`
  （收益必须改善、回撤/成交率/集中度/换手在容差内）+ `promotion_science.evaluate_promotion_evidence:426`，
  结果写 `strategy_champion_versions.status ∈ {ready, promoted, rolled_back}`。
- 它是**活的生产能力**：`paper_trading.py:8966-8976` → `/api/paper/strategy-champion/promote`
  （`api_paper.py:307-319`）→ `self_evolution.activate_params_candidate` **真实切换正式参数头**。
- 它是**不同业务事实**（参数头 vs lifecycle state）：`strategy_promotion` 无法替代它；删掉它等于移除
  一个活的实盘能力并改变参数头切换路径。
- 保留它则与 §8「只能 strategy_promotion 拥有 Promotion Policy」/§25「duplicate Promotion decision = 0」冲突。

⇒ 这是**归属/策略决策**，不是实现细节，必须由人审明确。

## G. 需要人审决策的问题

1. **Entry Authority 边界**：per-strategy 黄灯仓位缩放（`risk_scale`）与 `shadow_exception` 属于
   Entry Authority 还是 sizing policy？
   - (a) 视为 sizing policy，**留在 Active**（Entry Authority 保持纯 gate）：则「Active 收敛到单一 Entry
     Authority」只能覆盖真正的 gate 子集，必须在工单里改写该里程碑的验收口径。
   - (b) 搬进 Entry Authority 并扩契约：需要显式批准「Entry Authority 拥有仓位缩放输入」这一语义扩张，
     并为 manual 路径定义它是否继承缩放（否则 manual 行为会变）。
2. **参数头 promotion（§F.2）**：删除 champion 晋升决策，还是承认它是与 Promotion Policy 并列的
   **参数头**机制（并在 §8/§25 的口径里排除它）？

## H. 若上述问题获批，可安全交付的子集（不影响交易行为）

1. **Promotion Policy 接入 exact `ShadowComparisonReport`**：`shadow→paper` 走
   `("shadow","paper") -> ("shadow_comparison_report_id",)`，exact id 加载 + 12 项校验（存在性、
   identity 自校验、challenger id/version/checksum 相等、spec identity、environment equality、
   `availability == AVAILABLE`、coverage 完整、blocking 为空、provenance owner-complete），
   `PARTIAL`/`UNAVAILABLE` 一律 BLOCK；不引入任何 winner/score/ranking；不动 stale-proposal 语义。
2. **删除 `future_shadow_evidence_ref`**（当前 0 生产 caller、0 决策读取，删除是行为等价的）。
3. **删除死码**：`strategy_champion.collect_ledger_metrics`、`manual_orders._manual_risk_state`。
4. **Workspace**：扩展现有 `/api/strategies/{id}/...` + `strategies.js` 的 detail 视图，展示
   Active/Challenger 的 exact identity、environment、coverage、availability、blocking reasons、
   promotion decision（eligible/blocking）与 lifecycle allowed-next，全部按后端语义渲染
   （UNAVAILABLE/PARTIAL/MISSING 不得降级为 0/false/pass）。

以上 4 项都不改变 `allowed` / `order_status` / sizing / 阈值 / 执行经济学，可作为缩小范围的
R32-FINAL（Promotion + Workspace）先交人审；Entry Authority 收敛与参数头 promotion 归属另行决策。

---

# DECISION RESOLUTION（人工裁决，随后按此实现）

## R1. Q1 = (a)：Entry 边界按 owner 分裂，不做「单一 Entry Authority」重写

`risk_scale` **不归** Entry Authority，它是 **策略市场状态 → sizing/budget modifier**；
`shadow_exception` **不归** Entry Authority，它是 **research/shadow observation policy**，不能授权正式成交。

```text
Entry / Admission Authority   → 只拥有「能不能进入」及其 owner evidence
Sizing / Market Exposure      → risk_scale、budget/exposure scaling
Research / Shadow Observation → shadow_exception
Execution Authority           → dispatch / fill / execution constraints
```

因此 §C 探出的 20+ Active gate **本轮不迁移**；`_buy_order` 不被重写；
`manual` 路径**不继承** Active 的 per-strategy risk_scale。取而代之的验收口径是：

```text
[ ] 每个业务事实只有一个 owner
[ ] risk_scale 只有一个 sizing-policy owner
[ ] shadow_exception 只有一个 research owner
[ ] Execution Dispatch 只有一个 execution owner
[ ] Shadow entry 使用 frozen explicit owner facts
[ ] Active comparison 使用实际 persisted owner evidence
[ ] 不同 vocabulary 不制造等价映射
[ ] Comparison 不重新执行 entry / risk / sizing
[ ] 本 PR 不改变 Active/manual 的交易行为
[ ] 没有新增重复 gate implementation
```

真正 dead、caller=0 的旧 helper 仍然删除（见 R3），但不为「单一 Entry Authority」重写主路径。
后续若要彻底整理 Active/manual admission，作为独立的 production-entry convergence 工程，不夹在 R32 里。

## R2. Q2：两条晋级链并列，互不越权

```text
Lifecycle Promotion
  owner            = strategy_promotion
  target fact      = strategy lifecycle state
  mutation executor= strategy_lifecycle.transition()
  authority count  = 1

Parameter-Head Champion Activation
  owner            = strategy_champion
  target fact      = formal parameter/version head
  mutation executor= self_evolution.activate_params_candidate
  authority count  = 1
```

二者**不是** duplicate authority，禁止再写「Promotion authority count = 1」这种笼统口径。
`strategy_champion.compare_for_promotion` / `promote_challenger` /
`/api/paper/strategy-champion/promote` / `self_evolution.activate_params_candidate` **全部保留**；
仅删除经 caller audit 确认的 dead code。

文档与 UI 口径固定为「生命周期晋级 / Lifecycle Promotion」与「参数头晋升 /
Parameter-Head Champion Activation」，不做重命名 churn。

## R3. 已执行的删除（caller audit = 0）

| 被删对象 | production caller | test caller | replacement |
| --- | --- | --- | --- |
| `PromotionEvidenceBundle.future_shadow_evidence_ref` | 0 | 0 | `shadow_comparison_report_id` |
| `strategy_champion.collect_ledger_metrics` | 0 | 1（`test_strategy_invariants`，随函数一并删除） | 无 |
| `manual_orders._manual_risk_state` + `paper_trading._manual_risk_state` facade | 0 | 0 | `paper_trading._shared_risk_state` |

保留：`strategy_champion` 参数头激活系统、`tradability_shadow`、`tradability_position_shadow`、
`factor_quality_shadow`、`execution_quality_shadow`、`portfolio_shadow`。

## R4. 唯一 authority graph（本 PR 之后）

```text
Lifecycle chain（生命周期晋级）
  Strategy Version
        ↓
  Comparable Runtime Context
        ↓
  Active Evidence + ShadowRun
        ↓
  ShadowComparisonReport          ← 唯一比对事实 owner（exact id == fingerprint）
        ↓
  strategy_promotion              ← 唯一 Lifecycle Promotion Policy
        ↓
  PromotionProposal / PromotionDecision
        ↓
  strategy_lifecycle.transition() ← 唯一 lifecycle 写入口
        ↓
  SHADOW → PAPER

Parameter-head chain（参数头晋升）
  Parameter Challenger
        ↓
  strategy_champion scientific comparison（指标/容差）
        ↓
  Champion activation decision
        ↓
  self_evolution.activate_params_candidate
        ↓
  formal parameter head

NO CROSS-MUTATION：
  Lifecycle Promotion → parameter head  = 0
  Champion Activation → lifecycle state = 0
```

UI 只读取并展示这两条链；AI 只能 propose / explain / summarize，不能改变 evidence、readiness 或 lifecycle。

## R5. 本 PR 交付内容

1. Lifecycle Promotion 消费 exact `ShadowComparisonReport`（`shadow_comparison_report_id`，64-hex
   单一身份），12 项校验，`PARTIAL` / `UNAVAILABLE` / 身份不符 / 环境不符 / coverage 不完整 /
   blocking / provenance 不完整 / 损坏一律 BLOCK；`paper → production_sim` 仍无 owner，保持 blocked。
2. 删除 `future_shadow_evidence_ref` 与上述 dead code。
3. Workspace：`GET /api/strategies/{id}/challenger`（单一读端点）+ `strategy_service.challenger_read_model`
   （只组装 owner 事实）+ 现有 Strategy Workbench 的 Active vs Challenger 段落。**不新建第二个页面**，
   前端只渲染后端决定，两条 readiness 分列。
   **identity 契约（评审 blocker 修复）**：命名了 exact `ShadowComparisonReport` 时，两条腿都取自
   report 自己的 strategy stamp（`active_strategy_stamp` / `challenger_strategy_stamp`），**不**从
   registry current head 推导；report 的 Challenger 不是本 endpoint 的策略、或显式 `version` 与
   report 冲突时，以 `shadow_comparison_identity_mismatch` fail closed（400）；没有 report 时
   `active.available=false`（**不存在** Active comparator fact），Challenger 仅作为
   `registry_candidate` 展示。历史 report 因而不随 head 漂移（W1b），Active 的 lifecycle state 用
   自己的 exact `SL.get_state` 查询，查不到即 `None`。
4. 回归：`test_r32_final_promotion.py`（R-F1…R-F6、W1…W7、authority 隔离守卫）、
   `test_r32_final_ownership_boundary.py`（R-F9…R-F12，冻结 sizing / shadow_exception 语义）、
   `frontend/tests/challenger-workspace.test.mjs`（W8…W16）。
5. Mutation：`work/r32_final_mutation_check.py`（M-F1…M-F13）。

不变量（本轮最终）与工单 §Q 一致：`No duplicated owner for the same business fact`、
`Lifecycle Promotion Policy authority count = 1`、`Parameter-head Champion Activation authority count = 1`、
`Lifecycle mutation authority count = 1`、`Parameter-head mutation authority count = 1`、
`No lifecycle promotion path mutates parameter head`、`No parameter-head promotion path mutates lifecycle`。
