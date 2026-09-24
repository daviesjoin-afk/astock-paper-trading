# refactor(execution): publish attribution-ready execution facts

R27-B2C-4A —— execution attribution fact completeness。

```text
base:    d2dda470f542d6513a1d396e681424dcebde17be   (master, #196 merged)
branch:  codex/r27b2c4a-execution-attribution-facts
MERGE:   NOT MERGED
DEPLOY:  NOT DEPLOYED
```

---

## Review blocker resolution（#197 人工审核发现）

第一轮审核在 `d6ecb55e` 上发现了 **1 个 contract blocker**：投影发布了成交事实，却没有发布
**execution 自己的归属身份** `account_id` / `cycle_id`。这会让 B2C-4C 在真正迁
`pnl_attribution` 时重新建立一条事实来源：

```text
order_id → 重新查 paper_orders → account_id / cycle_id
    或
依赖调用方"记得自己刚才按哪个 account 过滤"
```

旧 `_pnl_evidence()` 明确按**账户**归因，B2C-4B 的 portfolio fact 也以
`cycle_id / account_id / asof_day` 为上下文；缺这两项，未来的跨 owner PnL join 无法完全由
typed facts 证明。而 `cycle_id` 本身**不是** portfolio 才拥有的事实 ——
`execution_planner` 早已把"订单归属哪个 account、属于哪个 cycle"当成写入与成交的不变量。

本次修订把这两项补上，并补齐了读取路径、指纹、回归与 mutation。修法：

```text
Execution owner:   这笔 order / fill 属于哪个 account / cycle     ← 补在这里
Portfolio owner:   这个 account / cycle 在 D 日的 cash / positions / realized pnl / NAV
```

**没有**新建 module，也**没有**新增第二套 evidence class：沿用既有 `FACT_FIELDS` 机制，
把 owner fact metadata 从 `(business_day, observed_at)` 扩展为
`(account_id, cycle_id, business_day, observed_at)`。

---

## Why B2C-4 was split

原计划是 `B2C-4 → 直接迁 pnl_attribution`。动手前复核源码发现**不能安全直接迁**：

```text
deepseek_research._pnl_evidence() 同时读
    paper_accounts
    paper_nav
    paper_orders
    paper_positions

其语义包括：NAV 变化 / daily return / 成交 / fees / realized PnL /
            position cost / exposure / account-cycle context
```

今天真正具备 **typed owner contract + research adapter** 的只有 execution 一族。于是只有两条
错误路线：

```text
1. 把整个 legacy _pnl_evidence dict 塞进一个 execution InformationEvent.payload
   → NAV / position cost / realized PnL / account state 冒充 execution owner 事实
   → 这正是 R27 禁止的 provenance 伪造

2. 为了"现在就能迁"而删掉这些能力
   → 删除 roadmap 能力，同样禁止
```

因此**保持最终目标不变**，只把 B2C-4 拆成三段实现顺序：

```text
B2C-4A  execution attribution fact completeness                  ← 本 PR
B2C-4B  portfolio/accounting owner facts required by pnl_attribution
B2C-4C  migrate pnl_attribution runtime to canonical typed research
```

这是实现顺序调整，**不是** roadmap 缩减。后续路线不变：

```text
B2C-5  news owner
B2C-6  adaptive / experiment owner
B2C-7  runtime / incident owner
B2C-8  remaining deepseek_research convergence
B2C-9  ai_analysis lifecycle convergence
B3     canonical research API/UI + 删除 compatibility projection
R27 COMPLETE
R28–R37 保持原 roadmap
```

---

## Scope

**只关闭一个缺口**：B2C-1 的 `ExecutionFactProjection` 只有 identity / lifecycle / verdict /
PIT / verification，因此 B2C-3 能证明"发生了 `partial` / `verified` / `not_executed`"，
却拿不出 `pnl_attribution` 真正需要的**成交数量 / 成交价格 / 费用 / 方向 / 股票代码 /
归属账户 / 归属周期**作为 owner-published typed fact。

改动集中在两个 production 文件，另有一个既有模块的**两处**最小改动：

```text
backend/execution_verification.py          （owner fact contract：投影新增字段）
backend/ai_research_execution_adapter.py   （research adapter：指纹扩展）
backend/execution_evidence.py              （2 处：provenance 带出归属身份；
                                            读路径只读探测 cycle_id 列）
```

`execution_evidence.py` 的**字段集、`fill_verdict`、`inconsistencies`、核验结论零改动** ——
这也是该模块既有的只读护栏（"证据模块只认识 orders + fills"）仍然成立的原因。

---

## Execution factual fields added

```text
ExecutionFactProjection 新增（EXECUTION_FACTUAL_FIELDS，顺序即契约顺序）
    code
    action
    requested_qty
    filled_qty
    fill_price
    fees

ExecutionFactProjection 新增（EXECUTION_OWNER_FACT_FIELDS，与既有 PIT 字段同组）
    account_id
    cycle_id
```

刻意**不新增第二套 execution authority**。没有出现：

```text
ExecutionAttributionEvidence
TradeResearchEvidence
PnLExecutionEvidence
ExecutionResearchFact
```

`ExecutionEvidence → ExecutionFactProjection` 仍是**唯一** execution fact contract。

### 成交事实：复用而不是复制

```text
fact_projection(evidence) 直接复制
    evidence.code / evidence.action / evidence.requested_qty
    evidence.filled_qty / evidence.fill_price / evidence.fees
```

* caller **不能**传入这些值（签名里没有这些参数）；
* **不**重新查 DB、**不**重新计算 fill_price / fees、**不**从 `paper_orders` 的兼容列补值；
* 回归用 `assertIs` 锁住"投影里的字段就是 owner 的那个 `EvidenceField` **对象**"，
  因此将来有人"顺手在投影里重算一遍"会立刻变红（EXFACT-20）。

### 归属身份：由 owner 的订单行发布，缺失即 unknown

```text
evidence_from_order()   → provenance["account_id"] / provenance["cycle_id"]（原样存下）
fact_projection()       → account_id / cycle_id 三态 EvidenceField
```

* `account_id` 必须是**非空文本**，`cycle_id` 必须是**正整数**；
* 缺失（列不存在 / NULL / 空串）或不可证明（`0` / `True` / `8.5` / `"8"`）→ **`unknown`**；
* **绝不** fallback 到"当前 active cycle" / "当前账户" / `0` / `None` ——
  那会让一条历史成交出现在一个它并不属于的账户/周期上；
* `0` 不是合法 cycle id（自增主键从 1 起），`True` 也不是 `1`：判断身份时不允许布尔量
  混进来，否则会发布一个指向真实周期的 `known(1)`（EXFACT-26 / 27）。

### 读取路径：老账本没有 `cycle_id` 列时不许崩

`load_execution_evidence` 先只读探测 `paper_orders` 的列集，**没有** `cycle_id` 就不请求它：

```text
请求一个不存在的列 → OperationalError
    而"owner 没有记录这条事实的周期归属"是一个应当如实发布的 unknown
```

也**不**回填：升级前的订单属于哪个周期无法从任何当前状态反推，该列的历史 NULL 正是诚实的
legacy provenance 状态（EXEC-REF-28 覆盖了"有该列"与"没有该列"两种形态）。

### 三态必须保留

八个字段都是真正的 `execution_evidence.EvidenceField`：

```text
known           （含 known(0)）
unknown
not_applicable
```

`__post_init__` 逐一要求 `isinstance(field, EvidenceField)` 且 `field.name == 字段名`：

```text
裸数字 / 裸字符串 / None / dict / duck-typed 对象 / 名字错位的真 EvidenceField
    → field_not_an_evidence_field（fail closed）
```

`as_dict()` 写 `EvidenceField.as_dict()`，**不**写 `maybe()` —— `maybe()` 会把 `unknown`
与 `not_applicable` 一起压成 `None`，于是"我们不知道成交了多少"与"这笔委托从未提交、
成交数量问题不存在"在序列化结果里变成同一句话。B2C-4C 的 payload 与冲突指纹都读
`as_dict()`，所以这一条是下游能否区分三态的前提（EXFACT-22 / EXFACT-25）。

已知零与"不适用"各自保留，不互相伪装（EXFACT-24 / mutation M-EXATTR-7 / M-EXATTR-8）。

---

## Authority unchanged

```text
ExecutionEvidence                 字段真值 owner（只多带出两个归属列，判定不改）
ExecutionFactProjection           只发布，不重算
ai_research_execution_adapter     只映射 research contract，不重新计算 execution facts
deepseek_research                 本轮不增加任何 order / fill SQL
```

`ExecutionFactProjection` 仍然**没有公开 raw 构造器**，唯一签发路径仍是
`fact_projection(evidence)` → 私有 `_issue_fact_projection`。

`account_id` / `cycle_id` **不进入 identity**：`source_id` 仍只由 owner 的 `event_key` 派生。
它们进的是**事实内容**与内容指纹 —— 这正是 EXEC-REF-26 / 27 要的语义：同一次成交被搬到
另一个 account / cycle 时 identity 相同、事实不同，因此是**冲突**而不是一条新事实。

---

## PIT unchanged

`business_day` / `observed_at` 的派生方式、格式校验、逐行完整性判定**一个字都没动**。
adapter 仍然要求 `business_day.is_known` 才签发，unknown 时**拒绝签发**，绝不 fallback 到
`created_at` / `observed_at` 的日期 / `order_time` / 墙钟。

---

## Verification unchanged

status × source → owner-neutral outcome 的映射逐字不变：

```text
verified + ledger         → verified
partial + ledger          → verified
not_executed + ledger     → verified
unknown + ledger          → unverified
legacy / absent / inconsistent → source_unusable
```

B2C-4A 只增加**事实内容**，不重新讨论**核验语义**。这两件事必须分开 —— 由
EXEC-REF-24 锁定（两条只差一个 factual 字段的投影必须给出逐字相同的
`owner_verification.canonical()`，`fact_state` 的差异只来自内容指纹）。

---

## Research adapter fingerprint update

`ai_research_execution_adapter._content_fingerprint` 从

```text
version / identity_kind / order_id / lifecycle_state / fill_verdict /
business_day / observed_at / inconsistencies
```

扩展到再覆盖

```text
code / action / requested_qty / filled_qty / fill_price / fees
account_id / cycle_id
```

于是同一条 execution identity 下：

```text
qty 被改 / price 被改 / fees 被改 / code 或 action 被改
account 被改 / cycle 被改
    → fact_state 不同
    → EvidenceConflict
```

不再被静默去重（EXEC-REF-21 / 22 / 23 / 26 / 27）。八个字段以
`EvidenceField.as_dict()` 入指纹，**不是** `maybe()`。

注意 `account_id` / `cycle_id` 是加入**事实内容**，不是加入 identity ——
identity 仍然只由 owner 的 `event_key` 决定。

### detail 不膨胀

`ResearchEvidenceRef.detail` **不**复制这八个字段，仍然只有 identity / 核验 / 内容指纹 /
最小审计元数据（EXEC-REF-25 逐名断言它们不在 detail 里）。职责保持三分：

```text
ExecutionFactProjection      owner factual truth
InformationEvent.payload     一次 research observation 投影（B2C-4C 直接从投影产生）
ResearchEvidenceRef          identity + verification + fingerprint
```

---

## Explicit exclusions

没有被塞进 execution contract 的东西：

```text
realized_pnl        ✗ 依赖 position cost basis / sell quantity / portfolio accounting
NAV                 ✗
daily_pnl           ✗
daily_return        ✗
position_cost       ✗        portfolio / accounting owner 事实（B2C-4B）
market_value        ✗
unrealized_pnl      ✗
benchmark           ✗
account cash        ✗
```

legacy `pnl_attribution` 确实读 `paper_orders.realized_pnl`，但 `realized_pnl` **不是纯
execution fact**。本 PR 没有为了方便把它加进来，也没有把改动扩大成"复制整份
`ExecutionEvidence`"：

```text
order_time / reject_reason / cancel_reason / available_qty / commission / slippage
    → 都**没有**加进投影（本轮最小集合是那 8 个字段）
```

同样**没有**加进去的还有 market valuation 一侧的任何东西（市值 / NAV 的估值腿）：那属于
R24 typed market fact，既不是 portfolio owner 也不是 execution owner 能自述的事实。

---

## Architecture Impact

```text
Roadmap capability removed:                0
Roadmap invariant weakened:                0
Production modules added:                  0
Production modules removed:                0
New service / manager / repository / facade: 0
New owner:                                 0
Execution fact authorities:                before = 1   after = 1
Execution research adapters:               before = 1   after = 1

Execution factual fields published:
    before = identity / lifecycle / verdict / PIT / verification
    after  = 上面 + code / action / requested_qty / filled_qty / fill_price / fees
                    + account_id / cycle_id

Execution verification semantics changed:  NO
PIT semantics changed:                     NO
Execution identity derivation changed:     NO（account/cycle 进的是事实内容，不是 identity）
Existing modules touched:                  execution_evidence.py（2 处，见 Scope）
Runtime migration:                          0
Legacy pnl writer changed:                  NO
deepseek_research 新增 order/fill SQL:      0
Net architecture surface:                  NEUTRAL
```

这次**不是**新增一层，而是让已有 owner contract 达到原 roadmap runtime 需要的完整度。

---

## Maintainability Impact

```text
ExecutionEvidence 仍是字段真值 owner                     YES
ExecutionFactProjection 只发布、不重算                     YES
adapter 只映射 research contract、不重算 execution facts   YES
deepseek_research 本轮不增加 order/fill SQL                YES
realized_pnl 没有被塞进 execution contract                 YES
NAV / position cost 没有被塞进 execution contract           YES
新的 production module                                     0
```

`execution_evidence.py` 的改动受它自己的只读护栏约束（
`test_execution_evidence.ArchitectureGuardTests`：无写 SQL、不 import `paper_trading`、
源码里不得出现 orders/fills 之外的账本表名）—— 因此"归属身份"只能从**订单行本身**带出，
不能顺手去 join 账户/持仓表。本 PR 没有放宽那条护栏。

### owner-origin provenance 仍然 OPEN / REQUIRED

本轮增加字段**不等于**关闭

```text
ExecutionEvidence 公开构造 → fact_projection → research adapter
```

这条 two-step forgery 路径。仍然 **OPEN / REQUIRED**，没有改成 CLOSED。

---

## Tests

### Targeted（`backend/test_execution_fact_contract.py`）

```text
EXFACT-20  projection 发布 code/action/requested_qty/filled_qty/fill_price/fees，
           且与 owner 的 EvidenceField 是**同一个对象**；六个字段都进 as_dict；
           组合/记账事实一个都不在
EXFACT-21  **每个**已发布字段（8 个）必须是真 EvidenceField 且 name 精确匹配
           （裸数字 / 裸字符串 / None / dict / duck-typed / 名字错位 → fail closed），
           且没有发明新的 evidence 词表
EXFACT-22  known / unknown / not_applicable 状态逐字保留
EXFACT-23  partial fill projection 保留真实 filled_qty / 加权 fill_price / fees
EXFACT-24  not_executed 不把不存在的成交数字伪造成 known zero；
           owner 的肯定性零也不被降级成 unknown
EXFACT-25  as_dict 保留完整 EvidenceField 状态（明确对比 maybe() 会压平三态）
EXFACT-26  account_id / cycle_id 由 owner 订单行发布；与 EE.FACT_FIELDS 逐字一致；
           没有变成 ExecutionEvidence 的证据字段；投影不可变
EXFACT-27  缺 account / cycle 时如实报 unknown（key 缺失 / NULL / 空串）；
           0 / True / False / -1 / 8.5 / "8" / [8] / {...} 等不可证明值同样 unknown；
           缺一个不影响另一个，也不影响 PIT 与成交事实
```

（同时把 EXFACT-08 / EXFACT-16 的显式字段表收敛到 `_all_projection_fields()`，避免契约
新增字段时只有部分用例被更新；EXFACT-11 增加 `EE.FACT_FIELDS` 与
`EV.EXECUTION_OWNER_FACT_FIELDS` 的逐字一致性断言。）

### Targeted（`backend/test_ai_research_execution_adapter.py`）

```text
EXEC-REF-21  同 identity + filled_qty 改变 → EvidenceConflict
EXEC-REF-22  同 identity + fill_price 改变 → EvidenceConflict
EXEC-REF-23  同 identity + fees 改变       → EvidenceConflict
EXEC-REF-24  新增 factual fields 不影响 owner verification mapping
EXEC-REF-25  load_execution_evidence → fact_projection → adapter 全链路可运行
             （并断言 detail 不复制事实 payload）
EXEC-REF-26  同 identity + account_id 改变 → EvidenceConflict
EXEC-REF-27  同 identity + cycle_id 改变   → EvidenceConflict
EXEC-REF-28  load_execution_evidence 保留 account / cycle provenance；
             没有 cycle_id 列的 legacy 账本照常可读且如实报 unknown
```

EXEC-REF-21 ~ 23、26 ~ 27 的每一对投影都由测试**自带隔离自检**：断言这一对在指纹的全部
输入维度上**只差那一个**字段。少了这一步，用例会以错误的原因变绿（例如 verdict 也变了，
那么去掉指纹里那个字段之后冲突仍由别的维度触发）。改归属身份时 order 与 fill 必须一起改，
否则会命中 `fill_identity_mismatch` 而多出一个维度。

```text
L1  python -m unittest test_execution_fact_contract test_ai_research_execution_adapter
    → 56 tests OK

L2  python -m unittest test_execution_fact_contract test_ai_research_execution_adapter \
                        test_ai_research_contract \
                        test_ai_research_evidence_ownership_guard \
                        test_ai_provider_transport
    → 182 tests OK
```

---

## Mutation

`work/r27b2c4a_execution_attribution_mutation_check.py` —— M-EXATTR-1 .. M-EXATTR-10：

```text
M-EXATTR-1   filled_qty 不进入 projection                      → CAUGHT
M-EXATTR-2   fees 不进入 projection                            → CAUGHT
M-EXATTR-3   EvidenceField 被压成 maybe()                      → CAUGHT
M-EXATTR-4   adapter fingerprint 忽略 filled_qty               → CAUGHT
M-EXATTR-5   adapter fingerprint 忽略 fill_price               → CAUGHT
M-EXATTR-6   adapter fingerprint 忽略 fees                     → CAUGHT
M-EXATTR-7   not_executed 的 known zero 被改成 unknown         → CAUGHT
M-EXATTR-8   not_applicable 被伪造成 known(0)                  → CAUGHT
M-EXATTR-9   投影不发布 owner 记录的 account_id                → CAUGHT
M-EXATTR-10  adapter fingerprint 忽略 cycle_id                 → CAUGHT
```

```text
all CAUGHT            yes (10/10)
survived              0
fake                  0
restore sha256        PASS
```

矩阵用 `--non-vacuity` 跑：每条 mutation 先跑 baseline，因此**目标用例路径写错会被报成
BASELINE-RED 而不是静默通过**（这一轮就是靠它发现 M-EXATTR-9 一度指向了错误的测试类）。
SyntaxError / ImportError / NameError 计入 FAKE，在 `BROKEN_RE` 里显式排除。

---

## Full backend

本地运行时为 **Python 3.14.5**（canonical runtime 的 CI/Docker 改造是紧随其后的独立
runtime PR，不混在本 PR 里）。

```text
python -m unittest discover -s backend -p "test_*.py"       （Python 3.14）
    → Ran 4433 tests ... OK (skipped=5)
```

静态（Python 3.14）：

```text
ruff check backend/execution_verification.py backend/ai_research_execution_adapter.py \
           backend/execution_evidence.py \
           backend/test_execution_fact_contract.py backend/test_ai_research_execution_adapter.py
    → All checks passed!

python -m compileall -q 同上五个文件
    → exit 0
```

exact-head GitHub CI 为最终 gate（当前 CI 仍是 3.11 / 3.12 双版本，按本 PR 合并时的既有
gate 跑完即可）。

---

## Known provenance limitation

```text
contract-issued execution projection   = CLOSED（未放宽）
owner-origin provenance                = OPEN / REQUIRED（未改变）
```

`ExecutionEvidence` 仍然公开可构造，因此两步伪造路径照旧存在。本 PR 不声称关闭它。

---

## B2C-4B / B2C-4C deferred

### B2C-4B —— portfolio / accounting typed owner facts

legacy `pnl_attribution` 当前仍依赖、且**不能**由 execution adapter 冒充的事实：

```text
latest / prior NAV
daily PnL
daily return
realized PnL
position cost summary
account / cycle context
```

优先复用**已经存在**的 `paper_portfolio_read_model.py`（R22 建立的 cycle/as-of bounded
portfolio read model：`PortfolioReadContext` / `positions_for_context_with_status` /
`realized_pnl` / `cash` / `portfolio_for_context` / `STATUS_VERIFIED` / `STATUS_UNKNOWN`），
在该 owner 上建立 typed portfolio/accounting fact projection。**不**创建
`new_pnl_repository` / `pnl_fact_manager` / `pnl_owner_service`。

提前写清的一条限制：`portfolio_for_context(... valuations=...)` 今天仍接受**调用方提供**的
valuation Mapping。因此 B2C-4B **不得**把"调用了 `portfolio_for_context`"当成
"market valuation 的 owner provenance 已成立" —— market valuation 仍必须来自 R24 typed
market fact。两种 authority 必须继续分开：

```text
portfolio ledger authority   持仓数量 / 成本 / 已实现盈亏 / 现金
market valuation authority   市值 / 未实现盈亏 / NAV 的估值腿
```

### B2C-4C —— runtime migration

迁移 `deepseek_research._pnl_evidence` → typed `InformationEvent` → `ai_research_service`
→ `ai_research_runs`，并删除 `pnl_attribution` 的 legacy writer path。

### 本 PR 明确**没有**改动

```text
deepseek_research._pnl_evidence
deepseek_research._run_evidence_task
deepseek_research.run_task
deepseek_research.run_suite
adaptive_engine.run_advisor_review
adaptive_engine.run_advisor_suite
```

`evidence_ref_from_execution_projection` 在 production 里的调用点数量仍然是 **0** ——
这是本轮的**正确状态**，不是空转。

---

## Docs

```text
ARCHITECTURE.md                       新增 "execution attribution facts（R27-B2C-4A）"
docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md 新增 "B2C-4A 进展"，状态更新为：
    B2C-1  COMPLETE
    B2C-2  COMPLETE
    B2C-3  COMPLETE
    B2C-4A COMPLETE
    B2C-4B NOT STARTED
    B2C-4C NOT STARTED
```

并解释为什么拆分：`pnl_attribution` 是**多 owner research purpose**，不能为了迁移方便把
NAV / realized PnL / portfolio cost 塞进 execution evidence。

---

```text
MERGE:   NOT MERGED
DEPLOY:  NOT DEPLOYED
STATUS:  AWAITING HUMAN REVIEW
```
