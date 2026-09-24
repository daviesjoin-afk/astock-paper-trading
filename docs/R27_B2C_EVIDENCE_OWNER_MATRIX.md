# R27-B2C —— Evidence Owner Matrix（剩余 research runtime 的 owner / provenance 准备）

本文件是 **audit 产物，不是设计提案**。它只回答一个问题：

> 剩余 research 输入事实分别归谁拥有？能不能在不伪造 provenance 的前提下签发 typed
> evidence reference？

范围：`deepseek_research` 的五个 purpose（`pnl_attribution` / `candidate_challenge` /
`incident_triage` / `overfit_watch` / `event_evidence`）与 `ai_analysis` 实际消费的全部事实。
proposal / tuning 路径（`run_realtime_tuning`、dual AI review）**不在本表内** —— 它们不是
research，见文末「明确排除」。

基线：`master` @ `649cabe0829af4d9aba5c31d06f6e230b21832a8`（R27-B2B 合并后）。

本文件的表格是**盘点时的审计产物**，其"safe?"结论刻意保持当时的判断；每个 substage
的实际进展记录在各家自己的 "B2C-n 进展" 小节（当前已到 **B2C-3 COMPLETE**）。

---

## 一、判定规则（先说清"safe"到底在问什么）

一条事实能被安全地变成 typed evidence reference，必须**同时**满足四条。缺任何一条，
下游的 `status='supported'` 就建立在一个没有被任何人拥有过的声明之上：

| # | 条件 | 为什么不可省 |
| --- | --- | --- |
| 1 | **单一 owner** —— 事实的生产写入只有一个权威模块 | 两个 writer 意味着"这条事实是谁说的"没有答案；reference 指向的可以是另一条链路写的行 |
| 2 | **typed 投影** —— owner 提供 frozen 投影（契约形状），不是裸行 / JSON blob | 研究层自己解释裸行 = 重新实现 owner 的语义 |
| 3 | **可证明的业务日（PIT）** —— owner 记录（而非推断）业务日 / 可用时间 | `as_of` 由调用方猜 = 用未来事实解释过去 |
| 4 | **owner 签发的核验维度** —— owner 用**自己**的闭集词表声明核验状态与方式 | 研究层翻译 / 借用别人的词表 = 发明核验结论 |

第 4 条是本轮的决定性约束。**B2C-2 之前**，research 契约把它**硬绑定到市场数据的词表**：

```text
ai_research_contract._owner_verification_pair()          （B2C-2 已改名为 _market_verification_pair）
    → 构造 market_data_contract.MarketDataSnapshot(kind="research_evidence", ...) 让 R24 拒绝非法组合

合法闭集（R24 拥有）                                       backend/market_data_contract.py:117-167
    verification        ∈ verified / single_source / disagreement / unavailable / not_attempted
    verification_method ∈ cross_source / coverage_integrity / none
```

因此**任何非市场 owner 的事实，都无法在不翻译的前提下填出合法的
`(verification, verification_method)`**。翻译就是发明，而 R27-A 的整个存在理由就是禁止它。

**B2C-2 已消除这个耦合**：canonical 存储改为 owner-neutral 的 `OwnerVerification`
（`outcome` 三态 + owner 自己的 `status` + owner-specific `attributes`），research core
只消费 `outcome`，**不解释**任何 owner 的状态字符串。market 的校验与归口仍然完全由
R24 负责（`_market_verification_pair` / `_market_owner_verification`，
`backend/ai_research_contract.py:338-489`）。

另外两条今天已经成立、也一并确认的事实：

```text
ResearchEvidenceRef 无公开构造器                      backend/ai_research_contract.py:620-630
已批准的 owner adapter registry                        backend/ai_research_contract.py:119-133
    market_data  evidence_ref_from_market_reading      （契约模块；typed R24 reading）
    execution    evidence_ref_from_execution_projection（ai_research_execution_adapter）
生产里构造 InformationEvent 的地方**恰好一处**          backend/deepseek_advisor.py:577（由 R24 reading 喂入）
私有签发口的调用者 = (模块, enclosing function) 精确 allowlist
    ai_research_contract.evidence_ref_from_market_reading
    ai_research_execution_adapter.evidence_ref_from_execution_projection
```

### 两层不变量：本 PR 强制的是哪一层

必须分清"已经强制了什么"与"最终还必须证明什么"，否则会把必要条件当成终局。

| 层 | 名称 | 状态 |
| --- | --- | --- |
| 第 1 层 | **contract-issued evidence boundary** —— ref 只能由契约登记的 factory 签发，研究层不能自己发明 `source_id` / `verification` / `verification_method`，也不能新增未登记的 legacy-dict adapter | **已强制、可 CI 化**（`backend/test_ai_research_evidence_ownership_guard.py`） |
| 第 2 层 | **owner-origin provenance** —— factory 的**输入本身**必须可证明来自该 canonical owner，而不是调用方手工造了一份长得一样的 typed object | **OPEN / REQUIRED，尚未完成** |

第 2 层今天是**做不到**的：`MarketDataSnapshot` / `MarketDataReading` 与 `ExecutionEvidence`
都是公开可构造的类型，所以"手工造 typed object → factory"仍能得到一个 ref。这条限制由
`AI_TYPED_06_two_step_forgery_is_documented_not_claimed_closed` 与
`EXEC_REF_18_owner_origin_provenance_is_still_open_and_required` 记录。B2C-3 新增第二个
adapter 时**没有**顺带宣称关闭它。

```text
第 1 层是**必要条件**，不是最终条件。
第 2 层是 R27 必须关闭的架构目标，必须由 owner/provenance 架构逐个关闭
（execution → news → adaptive/experiment → runtime/incident）。
在 R27 宣布完成之前不得降级，也不得写成"以后不需要证明"。
```

### owner-native verification：不要复制 market 语义

research 契约今天把 `(verification, verification_method)` 绑定到 R24 的市场词表。**这不能被
复制给其它 owner**：

```text
错误方向（会产生四五套平行契约）：
    execution 抄 VERIFICATIONS / VERIFICATION_METHODS / _VERIFICATION_METHODS_BY_STATE
    news / adaptive / runtime 各再抄一份
    → 所有 owner 都伪装成 market_data

正确方向：
    每个 owner 发布**自己语义**的核验闭集
      （execution 已有 execution_status 四态 + 证据来源 + EXECUTION_VERIFICATION_VERSION）
    再由 research 契约学会消费 owner-native verification
```

因此下一阶段要解决的是"research contract 如何消费 **owner-native** verification"，
而不是"所有 owner 如何把语义翻译成 market_data"。

---

## 二、Family A —— Paper trading facts

| evidence type | current source | current reader | canonical owner | typed contract? | PIT identity? | verification semantics? | safe? | missing prerequisite | target substage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 一笔成交 `paper_fills` | `execution_planner.commit_fill` INSERT `execution_planner.py:1486` | `deepseek_research._pnl_evidence:81-84` | ✅ 单一 writer（allowlist `test_r26_execution_authority_guard.py:47`） | ⚠️ `execution_evidence.ExecutionEvidence`（frozen，`execution_evidence.py:281-394`）只有 `as_dict()`/`fingerprint()`，**无 `projection()`** | ✅ `fill_date` + `quote_at` + `execution_asof` 由 owner 记录（`execution_planner.py:1492-1493`）；identity `event_key = sha256(order_id\|quote_at\|ruleset_version)`（`:262-278`，有唯一索引） | ❌ 行级**无**核验列；核验在 order 级 | ❌ **NO** | ① `ExecutionEvidence` 上的 typed 投影；② 契约形状的业务日字段；③ **owner 自己**的核验词表 | R27-B2C-1（第一个可补齐的 owner contract） |
| 委托 / 拒单 / 未成交 `paper_orders` | `paper_trading` 多处 + `paper_risk_service.py:682,712`；执行裁定由 `execution_verification.stamp_order:543` 落 | `deepseek_research._incident_evidence:129`、`_candidate_evidence:99` | ⚠️ **分裂**：订单行与执行裁定不同 owner | ⚠️ `ExecutionEvidence`（纯函数 `evidence_from_order:585-721`）、`execution_lifecycle.OrderLifecycle:281-347`，均无 `projection()` | ⚠️ `created_at/executed_at/execution_asof` 有；**业务日不是 `paper_orders` 的列** | ✅ **真权威**：`EXECUTION_STATUSES`(4 态) + `EVIDENCE_SOURCES` + 唯一判定 `VERIFIED_PREDICATE:114` / `is_verified_row:246` | ❌ **NO** | 同上；且 4 态词表**没有 method 维度**，不能直接当 research pair | R27-B2C-1 |
| 核验结论本身 | `execution_verification.verification_for_order:370`（写入路径唯一入口） | 多处只读消费（`paper_repository.py:7-10` 等） | ✅ 单一 owner | ❌ 返回**裸 dict**（`:340-344`） | ❌ 裁定无 as-of（从 order/fill 派生） | ✅ 见上 | ❌ **NO** | 同上 | R27-B2C-1 |
| NAV 观测 `paper_nav` | `paper_trading._record_nav:13060`（另有 `:2441/:2473/:14993`、`paper_user_cycle_attachment.py:63`） | `deepseek_research._pnl_evidence:64`、`:153` | ❌ **无单一 owner**，无 writer guard | ❌ 无（裸 dict 读，`paper_repository.py:114-129`） | ✅ `nav_date` 由 owner 派生，`UNIQUE(account_id,nav_date)` | ❌ `quote_status ∈ {verified, cost_fallback}` 是**模块内自造词表**，无 method，不委托任何权威 | ❌ **NO** | 先把 writer 收敛到单一 owner；再建 typed NAV 契约（含 `projection()`）；再决定 `quote_status` 归谁 | R27-B2C-2（需先做 owner 收敛） |
| 手续费 / 已实现盈亏 | `commit_fill` 写 `paper_fills.fees`、`paper_orders.realized_pnl`；`execution_evidence.reconcile_fees:448` 对账 | `_pnl_evidence:85-86` | ⚠️ 列有 owner，但对账结论无 owner | ❌ 无（`EvidenceField` 三态 `:190-278` 不是契约） | ✅ 随 fill | ❌ `reconcile_fees` 返回 `reconciled: bool`，**不是**事实级核验声明 | ❌ **NO** | 需要 owner 明确"对账结论"是不是一条核验维度；今天不是 | R27-B2C-2 |
| 当前持仓 | `paper_position_lots`（执行权威）+ `paper_position_read_model`（唯一只读权威） | `_overfit_evidence:166`（positions） | ⚠️ 读取权威明确，事实无 owner 契约 | ⚠️ `tradability_position_evidence` 有 frozen dataclass + `verification_status`，但不是"持仓观测"契约 | ✅ `feature_available_at` 类字段存在于相邻契约 | ⚠️ `verification_status` 词表未作为闭集发布 | ❌ **NO** | 发布持仓观测的 typed 契约 + 它自己的核验闭集 | R27-B2C-2 |
| 账户 / 参数版本 | `paper_accounts`；`paper_parameter_versions` 有 4+ writer | `_pnl_evidence:60`、`_overfit_evidence:152` | ❌ 参数版本**无单一 owner** | ❌ 无 | ⚠️ `effective_date` | ❌ 无 | ❌ **NOT MIGRATABLE** | 属于配置 / 生命周期状态，不是"事实"；不进入 research evidence | — |

**Family A 结论**：**execution verification / fill-backed facts** 是第一个最接近完整
owner contract 的事实族 —— 它的**成交流水**写入有 guard 强制的单一 owner
（`execution_planner.commit_fill`），它的**执行裁定**有统一 authority
（`execution_verification.verification_for_order` / `VERIFIED_PREDICATE`），它的业务日由
owner 记录（`fill_date` / `execution_asof`），identity 无碰撞（`event_key`）。

但必须把这句收窄，不要扩张成"整个 execution domain 已经单一 owner"：

```text
paper_fills 的 writer            → 单一 owner（allowlist 强制）
execution verification verdict   → 统一 authority
paper_orders 整体 writer         → **不是**单一 owner
                                   （paper_trading 多处 + paper_risk_service，
                                     详见本表第 2 行）
```

因此 B2C-1 的范围是"**fill-backed execution fact** 的 owner contract"，而不是"把
`paper_orders` 收编成一个 owner"。后者是另一件事，本轮不声称已经具备。

### B2C-1 进展（owner contract 已发布）

`execution_verification.py` 现在正式发布 execution 自己的 fact contract：
`EXECUTION_FACT_CONTRACT_VERSION` / `EXECUTION_VERIFICATION_SCOPE` /
`verification_contract(status, source)`（四态 + 证据来源 + 穷尽合法组合表）/
`ExecutionFactProjection`（identity · `identity_kind` · business_day · observed_at ·
owner-native verification）/ `fact_projection(evidence)`。

**已经解决的**：本表 §五 第 2 条之前的"owner 没有发布核验闭集"这件事（按
owner-native 方式，不是复制 market 词表）；identity 与业务日的 owner 派生与
逐成交可达性；`quote_at` / `event_key` 随流水读出。

**仍然没有解决的（路线不变）**：

```text
B2C-2  research 契约学会消费 owner-native verification（**已完成**）
B2C-3  execution → ResearchEvidenceRef adapter（**已完成**，见下文 "B2C-3 进展"）
B2C-4  迁移 pnl_attribution（**未开始**）
```

以及一条 B2C-1 明确记录、**没有**被掩盖的缺口：被拒 / 被撤的委托今天在
`paper_orders` 上**没有** owner 记录的交易日列，因此 `business_day` 如实报 `unknown`。
要让它变成 `known`，需要 owner 自己记录业务日（而不是让消费者用 `created_at` 推断）。

### B2C-2 进展（research core 可以携带 owner-native 核验）

`ResearchEvidenceRef` 的 canonical 核验存储从 **market-shaped** 改为 **owner-neutral**：

```text
B2C-2 之前  verification + verification_method        （一对 market 形状的字段）
            ↓
            _owner_verification_pair() 实际只问 MarketDataSnapshot 是否合法
            ⇒ research contract 本质上仍是 "market verification vocabulary"

B2C-2 之后  OwnerVerification（outcome / status / attributes）
            ↓
            market 的归口函数 = _market_owner_verification()（R24 仍是唯一 authority）
            非 market owner 只需发布自己的 OwnerVerification，无需伪装或翻译
```

**已经解决的**：research core 不再比较任何 owner 的状态字符串
（`HypothesisEvidence.is_verified` 读 `ref.is_verified` → `OwnerVerification.outcome`）；
`fact_state` / 冲突检测 owner-neutral（market 的 `verification_method` 仍参与冲突，
因为它是 owner attributes）；market 兼容面逐字不变；market 校验器诚实改名为
`_market_verification_pair`。

**归口是显式穷尽表，不是 catch-all。** `_MARKET_OUTCOME_BY_VERIFICATION` 必须**恰好**
覆盖 `MDC.VERIFICATIONS`，`_market_owner_verification()` 每次归口前做双向漂移检查
（缺已知状态 / 多未知状态都拒绝）。因此 R24 新增合法状态时 research 层 **fail closed**，
必须人工决定它归哪一态；**不会**静默落进 `else` 被当成 `unverified`。由 `RVERIFY-11`
锁定（含"模拟 R24 新增状态必须拒绝"的行为用例）。

**仍然没有解决的**：

```text
B2C-3  execution → ResearchEvidenceRef adapter（**已完成**）
B2C-4  迁移 pnl_attribution（**未开始**）
B2C-5  news owner readiness
B2C-6  adaptive / experiment owner readiness
B2C-7  runtime / incident owner readiness
B2C-8  remaining deepseek_research typed convergence
B2C-9  ai_analysis lifecycle convergence
B2C-10 / B3  canonical research API/UI + 删除 B2B 兼容投影
```

**记入 B2C-3 的一项**：`_issue_evidence_ref()` 在 B2C-2 时 production 里只有
`evidence_ref_from_market_reading()` 一个调用点（正确），#193 guard 保证契约**外**零调用；
B2C-3 引入第二个 approved factory，因此把 issuer caller set 升级成**精确 allowlist**：

```text
ai_research_contract.py            → evidence_ref_from_market_reading
ai_research_execution_adapter.py   → evidence_ref_from_execution_projection
```

只需 `(模块, enclosing function)` caller-set 等值，不做 CFG。

**owner-origin provenance 仍然 OPEN / REQUIRED。** owner-neutral 化解决的是"research 能
携带谁的核验"（能力问题），**不是**"输入对象确实由该 owner 产生"（provenance 问题）：
`MarketDataReading` / `ExecutionEvidence` 都仍是公开可构造的，两步伪造路径照旧。不得
因为 B2C-2 的 generic 化或 B2C-3 的 adapter 就宣称 provenance 已关闭。

### B2C-3 进展（execution fact adapter 已落地）

新增**一个且只有一个** production 模块：`backend/ai_research_execution_adapter.py`。

```text
execution_verification
        ↓
ai_research_execution_adapter     ← 唯一同时认识两套词表的 production 模块
        ↓
ai_research_contract
```

**为什么必须是一个独立 adapter，而不是在契约里加一行 import。**
`execution_verification` 同时含 owner fact contract（纯 value type）与 SQLite 读路径 /
回填 / 闸门谓词。让 research domain contract 直接 import 它，等于把手写研究契约绑到
execution 的 DB 读实现上；反过来让 `execution_verification` import research 又会反转依赖
方向（AIG-02 / RG-04 已禁止）。这个接缝是本轮 roadmap **本身要求**的真实边界。

公开面**只有**一个函数：`evidence_ref_from_execution_projection(projection)` ——
签名里**只有** `projection`，调用方不能提供 `source_id` / `as_of` / `verification` /
`outcome` / `business_day` / `verification_source`。刻意**没有**新增
service / manager / repository / facade / registry framework / `BaseAdapter`。

#### 关键：两个不同的问题（最容易犯错的地方）

```text
execution verification["is_verified"]   "**整张订单**是否被证明完整成交？"
OwnerVerification.is_verified           "这个 owner-native 结论是否可作为 research fact 依赖？"
```

owner 明确区分四态，其中两种是**可以依赖的事实结论**：

```text
partial        账本证明**发生过真实的部分成交**，只是整单没有成交完
not_executed   owner 有肯定性证据确认"没有执行"
```

因此**禁止**把 `verification["is_verified"]` 直接当成 research 判据：那会把这两种结论一律
降级成"不可信事实"。正确形态：

```text
status = partial        execution is_verified = False   research outcome = verified
                        ⇔ 完整成交？NO；"部分成交"这个事实可信？YES
status = not_executed   execution is_verified = False   research outcome = verified
                        ⇔ 完整成交？NO；确认没有执行？YES
```

整单布尔位不丢，但换了一个**准确**的名字进入 research attributes：
`execution_fully_verified`。刻意不沿用 `is_verified` —— 否则同一份对象上会同时出现
`ref.is_verified = True` 与 `verification_attributes["is_verified"] = False`，
而两者对 `partial` 本来就不同，极易误读。

#### status × source → owner-neutral outcome

```text
verified       + ledger        → verified          完整成交
partial        + ledger        → verified          可信的"部分成交"事实
not_executed   + ledger        → verified          可信的"确认未执行"事实
unknown        + ledger        → unverified        核验做了，但得不出明确 execution fact

evidence_inconsistent （四种状态都可配） → source_unusable
legacy_row_without_fill_evidence         → source_unusable
no_evidence_available                    → source_unusable
```

最后三条是**来源**层面的判定，与结论内容无关。例如 `not_executed + legacy` 归
`source_unusable` **不是**说"确认未执行"这个结论不可信，而是说**证据基础**是一条 owner
不愿背书的旧行。因此假设层给的原因不同：来源不可用 → `evidence_unavailable`（去修数据）；
`unknown + ledger` → `evidence_not_verified`（这条事实还不够好）。

合法组合集合由 owner **公开的** `verification_contract(status, source)` 推导（遍历
`EXECUTION_STATUSES × EVIDENCE_SOURCES`），并断言映射与它**精确相等**；刻意**不缓存**，
因此 owner 增删状态 / 来源 / 组合时 adapter 在**下一次调用**就 fail closed。与 B2C-2 的
`_MARKET_OUTCOME_BY_VERIFICATION` 同一手法。**execution 状态词逐字保留**在
`OwnerVerification.status`，绝不翻译成 market 词（`partial` 不会变成 `single_source`，
`unknown` 不会变成 `unavailable`，`not_executed` 不会变成 `not_attempted`）。

#### PIT：只有 owner 记录的业务日能进

`ResearchEvidenceRef.as_of` 取自 `projection.business_day`，必须 `is_known`；unknown 时
**拒绝签发**，绝不 fallback 到 `created_at` / `observed_at` 的日期 / `order_time` / 墙钟。

```text
execution fact without owner business_day
    → NOT ADAPTABLE YET（owner data prerequisite 未满足）
    → owner data gap = OPEN
```

这不是删能力：owner 一旦记录业务日，同一条事实立刻可签发（`EXEC-REF-09` 用 owner 自己的
签发口断言了这一点）。

#### identity 与内容指纹

```text
source_id  ←  <identity_kind>|<identity>      完全由 owner 投影派生
```

adapter 不重算 event key、不接受 `order_id`：identity authority 是 execution owner。
fact contract 版本刻意**不**进入 identity —— 版本升级不应该把同一条事实变成另一条事实。

内容指纹覆盖 factual projection（version / identity_kind / order_id / lifecycle_state /
fill_verdict / business_day / observed_at / inconsistencies），用
`json.dumps(sort_keys=True)` + sha256；刻意不用 `hash()`（跨进程不稳定）/ `repr(object)` /
地址 / 时间 / 随机数。verification statement 不重复进指纹 —— 它已经由
`OwnerVerification.canonical()` 单独进入 `fact_state`。

#### 本轮没有 production consumer（刻意）

B2C-3 的交付物是**能力 + 契约 + 回归**：factory 存在、research contract 支持 execution、
测试覆盖它。production runtime 仍然**没有**调用本 factory —— `pnl_attribution` 的迁移是
B2C-4。因此"本模块今天零调用点"是预期状态，不是空转。

**owner-origin provenance 仍然 OPEN / REQUIRED**（见上）。adapter 关闭的是"调用方不能自述
身份 / 业务日 / 核验结论"，**不是**"输入对象确实由 owner 产生"。B2C-3 不宣称关闭
two-step forgery。

---

## 三、Family B —— Adaptive / experiment facts

| evidence type | current source | current reader | canonical owner | typed contract? | PIT identity? | verification semantics? | safe? | missing prerequisite |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reward 观测 `adaptive_rewards` | `adaptive_engine._evaluate_rewards:1777` | `_overfit_evidence:141`、`_candidate_evidence` | ✅ | ❌ | ⚠️ `start/end_date` 复制自 `paper_nav.nav_date`，无可用性证明列 | ❌ **完全没有** | ❌ **NO** | 无核验维度；无 typed 契约；无可用性证明 |
| alpha 候选适应度 `adaptive_alpha_candidates` | `_run_alpha_lab:1611`（**每次硬删重插** `:1594/:1722`） | `_overfit_evidence:145` | ✅ | ❌ | ⚠️ `run_date` 是标签不是可用瞬间 | ❌ 无 | ❌ **NOT MIGRATABLE** | 无自然唯一键、行会被删 → reference 会悬挂 |
| alpha 样本特征行 `adaptive_alpha_samples` | `_capture_alpha_samples:1153` | 经 `learning_dataset` | ✅ | ✅ `learning_dataset.CanonicalSample`（frozen，`:676-728`） | ✅ **per-row** `feature_available_at` 由 owner 从快照 `saved_at` 派生（`:1171-1176`） | ⚠️ `pit_status ∈ {verified, unproven, legacy_unproven, unknown, future}` 由 owner 派生（`:644-670`，无制造分支）—— 但这是**可用性**声明，不是核验声明 | ❌ **NO** | 见 §五 第 4 条：词表不兼容 |
| alpha 前向收益标签 `adaptive_alpha_returns` | `_mature_alpha_returns:1210` | 同上 | ✅ | ✅ 同上 | ✅ `label_available_at` 继承端点样本，且有**保守继承规则**（端点非 verified 则 `legacy_unproven`，`:1245-1246`） | ⚠️ 同上（可用性，非核验） | ❌ **NO** | 同上；另 `horizon` 明确**不是**交易日 |
| 数据集 manifest / fingerprint | `learning_dataset.persist_manifest:1465` | 无 research 消费者 | ✅ | ✅ `DatasetBuild:1388-1406` | ⚠️ **dataset-scoped** `cutoff` | ❌ `label_pit_must_be_verified: True` 是契约标志，不是逐事实裁定 | ❌ **NO** | dataset 级裁定**不能**复制到单条 reference 上 |
| risk 候选 / 部署 post metrics / 日结果 / 下行事件 | `adaptive_risk._upsert_candidate:894`、`_register_deployment:645`、`:588`、`:364` | `_candidate_evidence:99-104` | ⚠️ 分表各有 owner，语义分散 | ❌（`evidence` / `post_metrics` 是 JSON blob） | ⚠️ `run_date`/`effective_date`/`outcome_date` | ⚠️ `status`/`decision` 生命周期词由 owner 确定性设置，**不是**核验词表 | ❌ **NO** | 需要 owner 把核验相关结论提升为**声明列 + 闭集词表** |
| selection 候选 | `adaptive_selection._upsert:399` **与** `deepseek_advisor:1068`（AI 调参） | `_candidate_evidence:100` | ❌ **两个生产 writer** | ❌ | ⚠️ `run_date` | ❌ | ❌ **NOT MIGRATABLE** | 双 writer；先收敛才能谈 owner |
| 市场画像 `adaptive_market_profiles` | `_store_profile:1077` | `_overfit_evidence`、`_pnl_evidence` | ✅ | ❌（`features`/`drivers` JSON） | ⚠️ `profile_date` + `observed_at`(=now) + `source_at` | ⚠️ `quality ∈ {valid_close, degraded}` 由 owner 内联阈值算出 | ❌ **NO** | 2 值质量词 ≠ 核验；无 typed 契约 |
| 证据链 `adaptive_evidence_chains` | `_sync_evidence_chains:2695` | `_candidate_evidence`（间接） | ✅ | ❌ | ✅ **最完整**：`signal_date/decision_at/order_at/fill_at/snapshot_at` 逐行 | ⚠️ `integrity_status ∈ {valid, legacy_gap, invalid}` 由 owner 从账本连边确定性算出 | ❌ **NO** | Family B 里 identity+PIT 最强，但 `valid`→`verified` 就是发明声明 |
| 执行证据指标 `adaptive_execution_evidence` | `:3289` | `_incident_evidence` 间接 | ✅ | ❌ | ⚠️ `evidence_date` | ⚠️ 自由字符串 status | ❌ **NO** | 无声明词表 |

**Family B 结论**：**全部 NOT MIGRATABLE（本轮）**。identity 与 PIT 在多处已经很好
（`adaptive_evidence_chains`、`alpha_samples`/`alpha_returns`），但 every single case 都缺
第 4 条：**没有任何 owner 发布过核验闭集**。

---

## 四、Family C —— Information / news facts

Owner 模块是 `news_learning.py`；`linkage.py` / `alt_data.py` / `disclosure_timeline.py`
**都不拥有**这些事实（前者不落库，后者只拥有财报可见性元数据）。

| evidence type | current source | current reader | canonical owner | typed contract? | PIT identity? | verification semantics? | safe? | missing prerequisite |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 新闻事件 `news_events` | `news_learning.capture_events:465` | `deepseek_research._event_evidence:184-245` | ✅ | ❌ 裸行 | ✅ `first_seen_at` 由 owner 观测（明确"不把发布时间当已知时间"，`:4-6`）；identity `event_key` UNIQUE（`:136`） | ❌ `news_events` **根本没有核验列**；只有 `evidence_grade`，且它是**抓取端硬编码常量**（`data_fetcher.py:1609`→`"C"`、`:1659`→`"B"`），`capture_events:480` 只做复制 | ❌ **NO** | 核验必须由 owner 签发；今天 grade 是**调用方**提供的，且无"B/C 必须带 URL"的任何强制 |
| 事件结果 `news_event_outcomes` | `mature_outcomes:639` | 经 `_event_evidence` | ✅ | ❌ | ✅ 从缓存 K 线日历严格取 `first_seen_at` 之后的交易日 | ❌ 无 | ❌ **NO** | 无核验维度；`price_source` 是自由字符串 |
| 来源信誉 `news_source_reputation` | `recalibrate:695`（**DELETE+INSERT**） | `_event_evidence:193` | ✅ | ❌ | ❌ **无可变快照的业务日**（`updated_at` = now） | ⚠️ `credibility_score` 是确定性公式，但其输入是**调用方提供**的 grade | ❌ **NOT MIGRATABLE** | 可变快照 + 无 as-of + 依赖调用方 grade |
| 因子版本 `news_factor_versions` | `recalibrate:763` | `_event_evidence:196` | ✅ | ❌ | ⚠️ 只有 `created_at` | ⚠️ `status ∈ {shadow, micro_eligible}` 是门禁词，不是核验 | ❌ **NOT MIGRATABLE** | 是模型配置行，不是"关于世界的事实" |
| 重大市场事件 `market_major_events` | `capture_major_events:542` | `_event_evidence:206-224` | ✅ | ❌ | ✅ `first_seen_at` + `published_at` | ⚠️ 有 `verification_status`，但取值是**内联二值** `"single_source_linked" if source_url else "unverified"`（`:573`）—— 只表达"有没有 URL"，不是核验结果 | ❌ **NO** | 需要一个真正的核验闭集（含 method），且 `evidence_grade` 必须由 owner 而非 fetcher 决定 |
| 事件↔候选映射 | `:587` | `_event_evidence:219` | ✅ | ❌ | ⚠️ `created_at` | ⚠️ 启发式 `confidence`（0.95 直接 / 0.72 行业） | ❌ **NO** | 启发式置信度不是核验 |
| 公司公告 | `data_fetcher.fetch_company_announcements:1615` | `_event_evidence:227` | ⚠️ fetch 端 | ❌ | ⚠️ | ❌ fetch 端的 `"verified": True`（`:1653`）**在落库时被丢弃** —— `news_events` 没有该列 | ❌ **NO** | "抓取端布尔"不是 owner 核验；且它现在根本没有被持久化 |

**Family C 结论**：**全部 NOT MIGRATABLE（本轮）**。最致命的一条是：唯一像核验的
`evidence_grade` 是**抓取适配器按 endpoint 硬编码**的常量，`verification_status` 只等价于
"有没有 URL"。把这两者当核验维度使用，就是**用抓取方式冒充事实核验**。

---

## 五、跨族结论与前置条件

### 结论

```text
Family A  execution facts            可补齐（唯一一条） → 见下
Family A  NAV / positions / config    NOT MIGRATABLE（owner 未收敛或无"事实"语义）
Family B  adaptive / experiment      NOT MIGRATABLE（无核验闭集）
Family C  news / events              NOT MIGRATABLE（grade 由调用方提供）
Family D  runtime / incident         NOT MIGRATABLE（owner、契约、PIT、核验四缺）
```

**本轮不存在可以安全实现的 adapter。** 每一个候选 today 都只能二选一：
(a) 发明一个核验词，或 (b) 把所有事实降级成 `not_attempted`。
(b) 看起来"typed"，但它让 `supported` / `insufficient_evidence` / `unsupported` 的区分失去
意义（所有事实永远 insufficient），同时把"我们没核验"伪装成"已按契约登记"。两者都是
invariant 2 禁止的伪造 provenance。

### 让第一个 adapter 成为可能，必须先有的四件事

按顺序，且**每一步都必须是 owner 自己的动作**，不能由研究层代做：

1. **owner 发布自己的核验闭集**（Family A → `execution_verification`）。**已完成（B2C-1）**：
   owner 发布了 `EXECUTION_FACT_CONTRACT_VERSION` / `EXECUTION_VERIFICATION_SCOPE` /
   `verification_contract(status, source)`（四态 + 证据来源 + 状态×来源穷尽合法组合表）。
   注意它**没有**照抄 market 的 `(verification, verification_method)` 形状 —— 那正是
   B2C-1 拒绝的"翻译"。owner 发布的是**它自己的**词表。
2. **研究契约能携带 owner 自己的核验结论**（`ai_research_contract`）。**已完成（B2C-2）**：
   canonical 存储改为 owner-neutral 的 `OwnerVerification`（`outcome` 三态 + owner 自己的
   `status` + owner-specific `attributes`），research core 只消费 `outcome`。
   关键约束不变：**研究层不做翻译** —— 每个 owner 的 factory 把自己的状态词归口到三态
   （market 的那份是 `_market_owner_verification`），research core 看不到任何 owner 词表。
   方向也不变：owner **不得** import research 契约（AIG-02 / RG-04 已禁止），归口函数住在
   研究契约里并**引用** owner 的公开判定（例如 `MDC.is_cross_source_verified`）。
3. **owner 提供 typed 投影 + 契约形状的业务日**。**已完成（B2C-1）**：
   `ExecutionFactProjection`（identity / `identity_kind` / `business_day` / `observed_at` /
   owner-native verification），由唯一发布入口 `fact_projection(evidence)` 产出。
4. **一个 owner 侧的 row→typed 读取口**（不是 dict 包装器）。
   已有的无 DB 纯函数 `execution_evidence.evidence_from_order:585-721` 就是正确的接缝。

四件前提都已在 B2C-1 / B2C-2 就位，**adapter 本身由 B2C-3 落地**：execution 的
`evidence_ref_from_execution_projection` 已登记进 `SUPPORTED_OWNER_ADAPTERS`，并在生产里
**零调用点**（迁移是 B2C-4）。

### 建议的迁移顺序（与 §六 的排除项一致）

```text
1. pnl_attribution        前提：execution facts 先有 owner 核验闭集（上面 1–4）
2. event_evidence         前提：news/information owner 能签发 PIT identity 与核验闭集
                          （今天 grade 由抓取端硬编码 → 先解决"谁签核验"）
3. candidate_challenge / overfit_watch
                          前提：experiment/adaptive facts 有明确 owner 与核验闭集
4. incident_triage        最后：它混合 research history + runtime failures +
                          paper jobs + order status，多个 authority，最难
ai_analysis               单独处理：还有 business_key / UPDATE 生命周期 / 前端 timeline
```

---

## 六、明确排除（不得与 research migration 混在一起）

| 对象 | 为什么排除 |
| --- | --- |
| `deepseek_advisor.run_realtime_tuning` → `adaptive_selection_candidates` | **proposal** 边界：它产出待人工确认的影子候选。而且该表有**两个**生产 writer（`adaptive_selection._upsert` 与 `deepseek_advisor`），连单一 owner 都不成立 |
| dual AI review → `dual_ai_tuning_runs` | 是 `evolution_apply` 的 **apply 门禁**（`status='consensus'` + `merged_proposals`）。改它等于改策略配置变更权限 |
| `ai_analysis.run_analysis` | 有 `business_key` + INSERT/UPDATE 生命周期（与 append-only 台账冲突），且前端 timeline 属 R27-B3 |

---

## 七、Maintainability 指标（本轮基线）

```text
Typed fact owners:                                 before = 1   after = 1        （只有 market_data）
Fake / manual ResearchEvidenceRef construction:     before = 0   after = 0
ResearchEvidenceRef public construction:            仍然不可绕 owner
Legacy dict → typed event wrapper:                  0
Implicit current lookup:                            before = 0   after = 0
Modules needed to understand one fact:              before = 1   after = 1
```

B2C-2 之后这些数字**不变**（owner-neutral 化没有新增 adapter，也没有放宽签发边界）：

```text
Public evidence factories:                          before = 1   after = 1   （只有 market_data）
Owner verification model:                           before = market-shaped
                                                    after  = owner-neutral
Market verification authority:                      before = R24   after = R24
Market-specific checks in generic hypothesis logic: before = >0  after  = 0
```

`after = 0` 这两条**只有在本轮不写 adapter 时才成立**，而且必须被永久锁住 ——
见 `backend/test_ai_research_evidence_ownership_guard.py`：它把 **contract-issued
evidence boundary（第 1 层）** 变成可执行的不变量，而不是一句承诺。

### B2C-3 之后的数字（adapter 新增，能力新增）

```text
Typed fact owners:                                  before = 1   after = 2   （+ execution）
Public evidence factories:                          before = 1   after = 2   （+ execution）
Owner verification model:                           owner-neutral（B2C-2 起不变）
Execution verification authority:                   before = execution_verification
                                                    after  = execution_verification（未迁移）
Research-owned execution status semantics:          0（状态与来源词只出现在 adapter 的归口表）
Execution → market vocabulary translation:         0
Modules needed to understand execution → evidence: before = capability absent
                                                    after  = 3
                                                             execution_verification
                                                             ai_research_execution_adapter
                                                             ai_research_contract
Production modules added:                           1（ai_research_execution_adapter）
service / manager / repository / facade:            0
Registry framework / BaseAdapter:                   0
Runtime migrations:                                 0
```

架构面**增大**了，而且是**有理由的**：新增的是 roadmap 明确要求的 owner boundary，不是
forwarding layer —— 它隔离 execution owner 与 generic research contract，避免
`ai_research_contract` 直接依赖带 DB / read helpers 的 `execution_verification`。

它**不**声称第 2 层（owner-origin provenance）已经成立。第 2 层仍是 OPEN / REQUIRED，
且是 R27 完成的前置条件。
